"""
Market data layer — fetches live data from IBKR or accepts manual input.
Provides a unified interface for all engine modules.
"""

import asyncio
import logging
from datetime import datetime, date, timedelta
from typing import Optional, List, Dict, Any

from .models import (
    MarketSnapshot, OptionsChain, OptionContract, Greeks, OptionType
)
from .config import EngineConfig
from .black_scholes import (
    bs_delta, bs_gamma, bs_theta, bs_vega, bs_call_price, bs_put_price,
    expected_move, implied_vol
)

logger = logging.getLogger(__name__)


class MarketDataProvider:
    """
    Market data provider that attempts IBKR live data,
    falls back to manual input mode.
    """

    def __init__(self, config: EngineConfig):
        self.config = config
        self._ib = None
        self._connected = False
        self._snapshot_cache: Optional[MarketSnapshot] = None

    # ─────────────────────────────────────────────────────────────
    # Connection Management
    # ─────────────────────────────────────────────────────────────

    def connect(self) -> bool:
        """Attempt to connect to IBKR TWS/Gateway."""
        try:
            from ib_insync import IB
            self._ib = IB()
            self._ib.connect(
                host=self.config.ibkr.host,
                port=self.config.ibkr.port,
                clientId=self.config.ibkr.client_id,
                timeout=self.config.ibkr.timeout,
            )
            self._connected = True
            logger.info("✅ Connected to IBKR TWS/Gateway")
            return True
        except Exception as e:
            logger.warning(f"⚠️  IBKR connection failed: {e}")
            logger.info("📝 Falling back to manual input mode")
            self._connected = False
            return False

    def disconnect(self):
        """Disconnect from IBKR."""
        if self._ib and self._connected:
            self._ib.disconnect()
            self._connected = False

    @property
    def is_live(self) -> bool:
        return self._connected

    # ─────────────────────────────────────────────────────────────
    # Market Snapshot
    # ─────────────────────────────────────────────────────────────

    def get_snapshot(self, manual_data: Optional[Dict[str, Any]] = None) -> MarketSnapshot:
        """
        Get current market snapshot. Uses IBKR if connected,
        otherwise uses manual_data dict.
        """
        if self._connected:
            return self._fetch_live_snapshot()
        elif manual_data:
            return self._build_manual_snapshot(manual_data)
        else:
            return self._interactive_snapshot()

    def _fetch_live_snapshot(self) -> MarketSnapshot:
        """Fetch live data from IBKR."""
        from ib_insync import Index, Stock

        snap = MarketSnapshot(timestamp=datetime.now())

        try:
            # SPX
            spx = Index("SPX", "CBOE")
            self._ib.qualifyContracts(spx)
            [spx_ticker] = self._ib.reqTickers(spx)
            snap.spx_price = spx_ticker.last or spx_ticker.close
            snap.prev_close = spx_ticker.close or 0

            # VIX
            vix = Index("VIX", "CBOE")
            self._ib.qualifyContracts(vix)
            [vix_ticker] = self._ib.reqTickers(vix)
            snap.vix_level = vix_ticker.last or vix_ticker.close

            # SPY for ETF-level data
            spy = Stock("SPY", "ARCA")
            self._ib.qualifyContracts(spy)
            [spy_ticker] = self._ib.reqTickers(spy)
            snap.spy_price = spy_ticker.last or spy_ticker.close

            # Calculate expected move
            snap = self._calculate_expected_moves(snap)

            logger.info(f"📊 Live snapshot: SPX={snap.spx_price}, VIX={snap.vix_level}")

        except Exception as e:
            logger.error(f"Error fetching live data: {e}")

        return snap

    def _build_manual_snapshot(self, data: Dict[str, Any]) -> MarketSnapshot:
        """Build snapshot from manually provided data."""
        snap = MarketSnapshot(timestamp=datetime.now())

        snap.spx_price = data.get("spx_price", 0)
        snap.spy_price = data.get("spy_price", snap.spx_price / 10)
        snap.qqq_price = data.get("qqq_price", 0)
        snap.iwm_price = data.get("iwm_price", 0)
        snap.vix_level = data.get("vix_level", 18)
        snap.vix_1d_change = data.get("vix_1d_change", 0)
        snap.vix_term_structure = data.get("vix_term_structure", "contango")
        snap.vix_futures_front = data.get("vix_futures_front", snap.vix_level * 1.05)
        snap.vix_futures_second = data.get("vix_futures_second", snap.vix_level * 1.10)
        snap.spx_futures_price = data.get("spx_futures", snap.spx_price)
        snap.spx_futures_overnight_change = data.get("overnight_change", 0)
        snap.globex_high = data.get("globex_high", snap.spx_price + 10)
        snap.globex_low = data.get("globex_low", snap.spx_price - 10)
        snap.iv_rank = data.get("iv_rank", 50)
        snap.iv_percentile = data.get("iv_percentile", 50)
        snap.realized_vol_20d = data.get("realized_vol", snap.vix_level * 0.8 / 100)
        snap.implied_vol_30d = data.get("implied_vol", snap.vix_level / 100)
        snap.advance_decline_ratio = data.get("adv_dec_ratio", 1.0)
        snap.put_call_ratio = data.get("put_call_ratio", 0.8)
        snap.new_highs = data.get("new_highs", 100)
        snap.new_lows = data.get("new_lows", 50)
        snap.prev_close = data.get("prev_close", snap.spx_price)
        snap.prev_high = data.get("prev_high", snap.spx_price + 15)
        snap.prev_low = data.get("prev_low", snap.spx_price - 15)
        snap.economic_events = data.get("economic_events", [])
        snap.is_fed_day = data.get("is_fed_day", False)
        snap.is_cpi_day = data.get("is_cpi_day", False)
        snap.is_opex_day = data.get("is_opex_day", False)
        snap.major_earnings_today = data.get("earnings_today", [])

        snap = self._calculate_expected_moves(snap)

        return snap

    def _interactive_snapshot(self) -> MarketSnapshot:
        """Interactively collect market data from user."""
        print("\n" + "=" * 60)
        print("📝 MANUAL MARKET DATA INPUT")
        print("=" * 60)
        print("(Enter values from TradingView or your broker)\n")

        data = {}
        data["spx_price"] = _input_float("SPX price", 5800)
        data["vix_level"] = _input_float("VIX level", 18)
        data["spy_price"] = _input_float("SPY price", data["spx_price"] / 10)
        data["overnight_change"] = _input_float("Overnight futures change (points)", 0)
        data["prev_close"] = _input_float("SPX previous close", data["spx_price"])
        data["iv_rank"] = _input_float("IV Rank (0-100)", 50)
        data["put_call_ratio"] = _input_float("Put/Call ratio", 0.80)

        events_str = input("Economic events today (comma-separated, or Enter for none): ").strip()
        data["economic_events"] = [e.strip() for e in events_str.split(",") if e.strip()] if events_str else []

        data["is_fed_day"] = input("Fed day? (y/n) [n]: ").strip().lower() == "y"
        data["is_cpi_day"] = input("CPI day? (y/n) [n]: ").strip().lower() == "y"

        earnings_str = input("Major earnings today (comma-separated, or Enter for none): ").strip()
        data["earnings_today"] = [e.strip() for e in earnings_str.split(",") if e.strip()] if earnings_str else []

        return self._build_manual_snapshot(data)

    def _calculate_expected_moves(self, snap: MarketSnapshot) -> MarketSnapshot:
        """Calculate expected moves from IV."""
        iv = snap.vix_level / 100 if snap.vix_level > 1 else snap.vix_level

        # 1-day expected move
        snap.expected_move_1d = expected_move(snap.spx_price, iv, 1 / 252)
        # 1-week expected move
        snap.expected_move_1w = expected_move(snap.spx_price, iv, 5 / 252)
        # 1-month expected move
        snap.expected_move_1m = expected_move(snap.spx_price, iv, 21 / 252)

        # ATM straddle estimate from IV
        snap.atm_straddle_price = snap.expected_move_1d / 0.85  # Reverse the 0.85 convention

        return snap

    # ─────────────────────────────────────────────────────────────
    # Options Chain
    # ─────────────────────────────────────────────────────────────

    def get_options_chain(self, underlying: str = "SPX",
                          expiration: Optional[date] = None,
                          num_strikes: int = 30) -> OptionsChain:
        """
        Get options chain. Live from IBKR or synthesized from Black-Scholes.
        """
        if self._connected:
            return self._fetch_live_chain(underlying, expiration, num_strikes)
        else:
            # Build synthetic chain from current snapshot
            snap = self._snapshot_cache or MarketSnapshot()
            price = snap.spx_price if underlying == "SPX" else snap.spy_price
            iv = snap.vix_level / 100 if snap.vix_level > 1 else 0.18
            return self._build_synthetic_chain(underlying, price, iv, expiration, num_strikes)

    def _fetch_live_chain(self, underlying: str, expiration: Optional[date],
                          num_strikes: int) -> OptionsChain:
        """Fetch live options chain from IBKR."""
        from ib_insync import Index, Stock, Option

        chain = OptionsChain(underlying=underlying)

        try:
            if underlying in ("SPX", "VIX"):
                contract = Index(underlying, "CBOE")
            else:
                contract = Stock(underlying, "SMART")

            self._ib.qualifyContracts(contract)
            [ticker] = self._ib.reqTickers(contract)
            chain.underlying_price = ticker.last or ticker.close

            # Get option parameters
            chains = self._ib.reqSecDefOptParams(
                contract.symbol, "", contract.secType, contract.conId
            )
            if not chains:
                return chain

            chain_data = chains[0]
            if expiration:
                exp_str = expiration.strftime("%Y%m%d")
            else:
                # Get nearest expiration (0DTE)
                today_str = date.today().strftime("%Y%m%d")
                exps = sorted(chain_data.expirations)
                exp_str = exps[0] if exps else today_str

            chain.expiration = datetime.strptime(exp_str, "%Y%m%d").date()

            # Select strikes around current price
            all_strikes = sorted(chain_data.strikes)
            atm_idx = min(range(len(all_strikes)),
                          key=lambda i: abs(all_strikes[i] - chain.underlying_price))
            start = max(0, atm_idx - num_strikes // 2)
            end = min(len(all_strikes), atm_idx + num_strikes // 2)
            strikes = all_strikes[start:end]

            # Build call and put contracts
            for strike in strikes:
                for right in ("C", "P"):
                    opt = Option(underlying, exp_str, strike, right, chain_data.exchange)
                    self._ib.qualifyContracts(opt)
                    [opt_ticker] = self._ib.reqTickers(opt)

                    oc = OptionContract(
                        symbol=f"{underlying}{exp_str}{right}{strike}",
                        underlying=underlying,
                        strike=strike,
                        expiration=chain.expiration,
                        option_type=OptionType.CALL if right == "C" else OptionType.PUT,
                        bid=opt_ticker.bid or 0,
                        ask=opt_ticker.ask or 0,
                        mid=(opt_ticker.bid + opt_ticker.ask) / 2 if opt_ticker.bid and opt_ticker.ask else 0,
                        last=opt_ticker.last or 0,
                        volume=opt_ticker.volume or 0,
                        dte=(chain.expiration - date.today()).days,
                    )

                    # Greeks
                    if hasattr(opt_ticker, 'modelGreeks') and opt_ticker.modelGreeks:
                        mg = opt_ticker.modelGreeks
                        oc.greeks = Greeks(
                            delta=mg.delta or 0,
                            gamma=mg.gamma or 0,
                            theta=mg.theta or 0,
                            vega=mg.vega or 0,
                            iv=mg.impliedVol or 0,
                        )

                    if right == "C":
                        chain.calls.append(oc)
                    else:
                        chain.puts.append(oc)

        except Exception as e:
            logger.error(f"Error fetching live chain: {e}")

        return chain

    def _build_synthetic_chain(self, underlying: str, price: float, iv: float,
                               expiration: Optional[date] = None,
                               num_strikes: int = 30) -> OptionsChain:
        """
        Build a synthetic options chain using Black-Scholes.
        Used when IBKR is not connected — generates realistic greeks & prices.
        """
        if not price:
            price = 5800  # Default SPX
        if not iv:
            iv = 0.18

        exp = expiration or date.today()
        dte = max((exp - date.today()).days, 0)
        T = max(dte / 365, 1 / (365 * 24))  # At least 1 hour to expiry
        r = 0.045  # Risk-free rate approximation

        chain = OptionsChain(
            underlying=underlying,
            underlying_price=price,
            expiration=exp,
        )

        # Generate strikes centered on price
        if underlying in ("SPX",):
            strike_interval = 5
        elif underlying in ("SPY", "QQQ", "IWM"):
            strike_interval = 1
        else:
            strike_interval = max(1, round(price * 0.005))

        center_strike = round(price / strike_interval) * strike_interval
        strikes = [center_strike + i * strike_interval
                   for i in range(-num_strikes // 2, num_strikes // 2 + 1)]

        for K in strikes:
            # Call
            call_price = bs_call_price(price, K, T, r, iv)
            call_delta = bs_delta(price, K, T, r, iv, "call")
            call = OptionContract(
                symbol=f"{underlying}_{exp.strftime('%y%m%d')}_C_{K}",
                underlying=underlying,
                strike=K,
                expiration=exp,
                option_type=OptionType.CALL,
                bid=round(max(call_price * 0.95, 0.01), 2),
                ask=round(call_price * 1.05, 2),
                mid=round(call_price, 2),
                last=round(call_price, 2),
                volume=max(100, int(5000 * (1 - abs(call_delta - 0.5) * 2))),
                open_interest=max(500, int(20000 * (1 - abs(call_delta - 0.5) * 2))),
                greeks=Greeks(
                    delta=round(call_delta, 4),
                    gamma=round(bs_gamma(price, K, T, r, iv), 6),
                    theta=round(bs_theta(price, K, T, r, iv, "call"), 4),
                    vega=round(bs_vega(price, K, T, r, iv), 4),
                    iv=round(iv + _skew_adjustment(price, K, iv), 4),
                ),
                dte=dte,
            )
            chain.calls.append(call)

            # Put
            put_price = bs_put_price(price, K, T, r, iv)
            put_delta = bs_delta(price, K, T, r, iv, "put")
            put = OptionContract(
                symbol=f"{underlying}_{exp.strftime('%y%m%d')}_P_{K}",
                underlying=underlying,
                strike=K,
                expiration=exp,
                option_type=OptionType.PUT,
                bid=round(max(put_price * 0.95, 0.01), 2),
                ask=round(put_price * 1.05, 2),
                mid=round(put_price, 2),
                last=round(put_price, 2),
                volume=max(100, int(5000 * (1 - abs(put_delta + 0.5) * 2))),
                open_interest=max(500, int(20000 * (1 - abs(put_delta + 0.5) * 2))),
                greeks=Greeks(
                    delta=round(put_delta, 4),
                    gamma=round(bs_gamma(price, K, T, r, iv), 6),
                    theta=round(bs_theta(price, K, T, r, iv, "put"), 4),
                    vega=round(bs_vega(price, K, T, r, iv), 4),
                    iv=round(iv + _skew_adjustment(price, K, iv), 4),
                ),
                dte=dte,
            )
            chain.puts.append(put)

        return chain

    def cache_snapshot(self, snap: MarketSnapshot):
        """Cache a snapshot for use by chain builder."""
        self._snapshot_cache = snap


def _input_float(prompt: str, default: float) -> float:
    """Helper for interactive float input with default."""
    val = input(f"  {prompt} [{default}]: ").strip()
    try:
        return float(val) if val else default
    except ValueError:
        return default


def _skew_adjustment(S: float, K: float, base_iv: float) -> float:
    """
    Simulate realistic volatility skew.
    OTM puts have higher IV (fear premium), OTM calls slightly lower.
    """
    moneyness = (K - S) / S
    if moneyness < 0:
        # OTM puts — IV increases as strike goes lower
        return abs(moneyness) * base_iv * 0.8
    elif moneyness > 0:
        # OTM calls — slight IV decrease then increase for far OTM
        return moneyness * base_iv * 0.2
    return 0.0

"""
IBKR Live Data Provider
========================
Full-featured Interactive Brokers data provider for:
  • Intraday stock bars (1s → daily, full history)
  • Real options chains with LIVE Greeks (not Black-Scholes estimates)
  • Real-time streaming quotes
  • 0DTE option prices at every 5-min bar

This replaces synthetic Black-Scholes pricing with actual market data,
which captures real IV skew, bid-ask spreads, and gamma dynamics.

Requirements:
  pip install ib_insync
  IBKR TWS or IB Gateway running with API enabled

Setup:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Step 1: Install ib_insync
    pip install ib_insync

  Step 2: Start TWS or IB Gateway
    - TWS Paper Trading: port 7497
    - TWS Live:          port 7496
    - IB Gateway Paper:  port 4001
    - IB Gateway Live:   port 4002

  Step 3: Enable API in TWS/Gateway
    TWS → Edit → Global Configuration → API → Settings
    ✓ Enable ActiveX and Socket Clients
    ✓ Socket port: 7497 (paper) / 7496 (live)
    ✓ Allow connections from localhost only

  Step 4: Set environment variables (optional)
    export IBKR_HOST=127.0.0.1
    export IBKR_PORT=7497
    export IBKR_CLIENT_ID=10
    export IBKR_ACCOUNT=DU1234567  (optional, for account-specific ops)

  Step 5: Subscribe to market data (IBKR account)
    - US Securities Snapshot & Futures Value Bundle: ~$10/mo
    - US Equity and Options Add-On Streaming Bundle: ~$4.50/mo
    - Waived if you generate $30+/mo in commissions

Usage:
    from trading_engine.data.ibkr_provider import IBKRDataProvider

    # Intraday stock bars
    provider = IBKRDataProvider()
    provider.connect()
    bars = provider.get_historical_bars("SPY", days=30, interval="5m")

    # Real options chain with live Greeks
    chain = provider.get_options_chain("SPY", expiry="20260307")
    for opt in chain:
        print(f"  {opt['strike']} {opt['right']}: "
              f"bid={opt['bid']:.2f} ask={opt['ask']:.2f} "
              f"Δ={opt['delta']:.3f} Γ={opt['gamma']:.4f} θ={opt['theta']:.3f}")

    # 0DTE option snapshots over time (for backtesting with real prices)
    snapshots = provider.get_option_snapshots(
        "SPY", strike=580, right="P", expiry="20260307",
        interval_sec=300  # every 5 min
    )

    provider.disconnect()
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import os
import math
import time
import logging
from datetime import datetime, date, timedelta
from typing import Optional, List, Dict, Any, Tuple

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


def _isnan(v) -> bool:
    """Safe NaN check for any numeric type (float, numpy, None)."""
    try:
        return math.isnan(float(v))
    except (TypeError, ValueError):
        return True


class IBKRDataProvider:
    """
    All-in-one IBKR data provider for the trading engine.

    Handles:
    - Connection lifecycle (connect/disconnect/reconnect)
    - Historical stock bars (any timeframe, full history)
    - Real options chains with live Greeks
    - Option price snapshots for 0DTE backtesting
    - Real-time streaming (for live trading)
    """

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        client_id: Optional[int] = None,
    ):
        self.host = host or os.getenv("IBKR_HOST", "127.0.0.1")
        self.port = port or int(os.getenv("IBKR_PORT", "7497"))
        self.client_id = client_id or int(os.getenv("IBKR_CLIENT_ID", "10"))
        self._ib = None
        self._ib_insync = None

    # ─── Connection Management ────────────────────────────────────

    def connect(self) -> bool:
        """Connect to TWS/Gateway. Returns True if successful."""
        try:
            import ib_insync
            self._ib_insync = ib_insync
        except ImportError:
            raise RuntimeError(
                "ib_insync not installed.\n"
                "  pip install ib_insync\n"
                "Then start TWS or IB Gateway with API enabled."
            )

        if self.is_connected():
            return True

        # ── P0-2: Paper vs live port guard ─────────────────────
        trading_mode = os.getenv("TRADING_MODE", "").lower()  # "paper" or "live"
        _PAPER_PORTS = {7497, 4001}
        _LIVE_PORTS = {7496, 4002}

        if trading_mode == "paper" and self.port in _LIVE_PORTS:
            raise RuntimeError(
                f"SAFETY ABORT: TRADING_MODE=paper but port {self.port} is a LIVE port.\n"
                f"  Paper ports: {_PAPER_PORTS}\n"
                f"  Set IBKR_PORT=7497 or IBKR_PORT=4001 for paper trading."
            )
        if trading_mode == "live" and self.port in _PAPER_PORTS:
            logger.warning(
                f"TRADING_MODE=live but port {self.port} is a PAPER port. "
                f"Set IBKR_PORT=7496 or IBKR_PORT=4002 for live trading."
            )
        if trading_mode == "live" and self.port in _LIVE_PORTS:
            print(f"  ⚠️  LIVE TRADING MODE on port {self.port}")

        try:
            self._ib = ib_insync.IB()
            self._ib.connect(
                self.host, self.port,
                clientId=self.client_id,
                timeout=10,
            )

            # ── P0-1: Request LIVE data first, delayed as fallback ──
            # Type 1 = live streaming (requires market data subscription).
            # Type 3 = delayed (15-min lag, free). NEVER acceptable for
            #          0DTE trading — only used as dev/testing fallback.
            self._ib.reqMarketDataType(1)  # Try live first
            logger.info("Requested LIVE market data (Type 1)")

            # Verify live data is actually flowing by fetching a quick quote
            _live_ok = False
            try:
                _test_contract = ib_insync.Stock("SPY", "SMART", "USD")
                self._ib.qualifyContracts(_test_contract)
                _test_ticker = self._ib.reqMktData(_test_contract, "", True, False)
                self._ib.sleep(3)
                _price = _test_ticker.marketPrice()
                self._ib.cancelMktData(_test_contract)
                if _price and not math.isnan(_price) and _price > 0:
                    _live_ok = True
            except Exception as e:
                logger.debug(f"Live data probe failed: {e}")

            if _live_ok:
                print(f"  📡 Market data: LIVE (Type 1) ✅")
            else:
                # Fall back to delayed — but warn loudly
                self._ib.reqMarketDataType(3)
                logger.warning(
                    "LIVE market data unavailable — falling back to DELAYED (Type 3). "
                    "Subscribe to IBKR US Equity/Options bundle ($4.50/mo) for live data."
                )
                print(f"  ⚠️  Market data: DELAYED (Type 3) — 15min lag!")
                print(f"       Subscribe to IBKR market data for live quotes.")
                if trading_mode == "live":
                    raise RuntimeError(
                        "SAFETY ABORT: TRADING_MODE=live but only DELAYED data available.\n"
                        "  Subscribe to US Equity & Options data on IBKR, or set "
                        "TRADING_MODE=paper to continue with delayed data."
                    )

            # ── P0-3: Validate IBKR account ID ─────────────────────
            acct = self._ib.managedAccounts()
            expected_account = os.getenv("IBKR_ACCOUNT", "").strip()
            if expected_account and acct:
                if expected_account not in acct:
                    self._ib.disconnect()
                    self._ib = None
                    raise RuntimeError(
                        f"SAFETY ABORT: IBKR_ACCOUNT={expected_account} not found "
                        f"in managed accounts {acct}.\n"
                        f"  Connected account(s): {', '.join(acct)}\n"
                        f"  Fix IBKR_ACCOUNT env var or log into the correct account."
                    )
                print(f"  🔐 Account verified: {expected_account}")
            elif not expected_account:
                logger.info(
                    f"IBKR_ACCOUNT not set — trading on default account: "
                    f"{acct[0] if acct else 'unknown'}. "
                    f"Set IBKR_ACCOUNT for explicit validation."
                )

            print(f"  ✅ Connected to IBKR at {self.host}:{self.port}")
            print(f"     Account(s): {', '.join(acct) if acct else 'unknown'}")
            print(f"     Server time: {self._ib.reqCurrentTime()}")
            return True
        except Exception as e:
            self._ib = None
            print(f"  ❌ IBKR connection failed: {e}")
            print(f"     Make sure TWS/Gateway is running on {self.host}:{self.port}")
            print(f"     and API connections are enabled.")
            return False

    def disconnect(self):
        """Disconnect from TWS/Gateway."""
        if self._ib and self._ib.isConnected():
            self._ib.disconnect()
            print("  🔌 Disconnected from IBKR")
        self._ib = None

    def is_connected(self) -> bool:
        """Check if still connected."""
        return bool(self._ib and self._ib.isConnected())

    def _require_connection(self):
        """Ensure we're connected, auto-reconnect if needed."""
        if not self.is_connected():
            if not self.connect():
                raise RuntimeError(
                    "Not connected to IBKR. Start TWS/Gateway and call .connect()"
                )

    # ─── Contract Helpers ───────────────────────────────────────────

    # Index tickers (SPX, VIX, NDX, etc.) require ib_insync.Index()
    # instead of ib_insync.Stock(). The exchange is CBOE, not SMART.
    INDEX_TICKERS = {"SPX", "VIX", "NDX", "RUT", "DJX"}

    def _make_underlying_contract(self, ticker: str):
        """
        Create the correct underlying contract for a ticker.

        SPX/VIX/NDX → Index("SPX", "CBOE")
        SPY/QQQ/etc → Stock("SPY", "SMART", "USD")

        This is critical: using Stock() for SPX will fail to qualify.
        """
        ib_insync = self._ib_insync
        ticker_upper = ticker.upper()

        if ticker_upper in self.INDEX_TICKERS:
            return ib_insync.Index(ticker_upper, "CBOE")
        else:
            return ib_insync.Stock(ticker_upper, "SMART", "USD")

    def _make_option_contract(self, ticker: str, expiry: str, strike: float,
                               right: str, exchange: str = "SMART"):
        """
        Create an option contract with correct exchange routing.

        For most tickers, SMART routing works. For SPX options,
        SMART also works (IBKR routes to CBOE automatically).
        """
        ib_insync = self._ib_insync
        return ib_insync.Option(ticker.upper(), expiry, strike, right.upper(), exchange)

    # ─── Historical Stock/Index Bars ──────────────────────────────

    def get_historical_bars(
        self,
        ticker: str = "SPY",
        days: int = 60,
        interval: str = "5m",
        rth_only: bool = True,
    ) -> pd.DataFrame:
        """
        Fetch historical OHLCV bars from IBKR.

        Args:
            ticker:    Stock symbol
            days:      Number of calendar days to look back
            interval:  Bar size: "1s", "5s", "1m", "5m", "15m", "30m", "1h", "1d"
            rth_only:  True = regular trading hours only (9:30-16:00)

        Returns:
            DataFrame with columns: open, high, low, close, volume, vwap
        """
        self._require_connection()
        ib_insync = self._ib_insync

        # Map interval to IBKR barSizeSetting
        bar_size_map = {
            "1s": "1 secs", "5s": "5 secs", "10s": "10 secs", "30s": "30 secs",
            "1m": "1 min", "2m": "2 mins", "5m": "5 mins", "15m": "15 mins",
            "30m": "30 mins", "60m": "1 hour", "1h": "1 hour", "1d": "1 day",
        }
        bar_size = bar_size_map.get(interval)
        if not bar_size:
            raise ValueError(f"Unsupported interval '{interval}'. Use: {list(bar_size_map.keys())}")

        # Duration string
        if days <= 1:
            duration = "1 D"
        elif days <= 365:
            duration = f"{days} D"
        else:
            years = max(1, days // 365)
            duration = f"{years} Y"

        contract = self._make_underlying_contract(ticker)
        self._ib.qualifyContracts(contract)

        # Index data uses whatToShow="TRADES" on CBOE
        what_to_show = "TRADES"

        print(f"  📊 Fetching {ticker} {interval} bars ({duration}) from IBKR ...")
        bars = self._ib.reqHistoricalData(
            contract,
            endDateTime="",
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow="TRADES",
            useRTH=rth_only,
            formatDate=1,
            keepUpToDate=False,
        )

        if not bars:
            raise RuntimeError(f"IBKR returned no bars for {ticker}")

        records = []
        for bar in bars:
            records.append({
                "timestamp": pd.Timestamp(bar.date),
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
                "vwap": getattr(bar, "average", 0) or 0,
            })

        df = pd.DataFrame(records).set_index("timestamp")
        if df.index.tz is None:
            df.index = df.index.tz_localize("US/Eastern")

        print(f"  ✅ {len(df)} bars ({df.index[0]} → {df.index[-1]})")
        return df

    # ─── Options Chain (Real Prices + Live Greeks) ────────────────

    def get_options_chain(
        self,
        ticker: str = "SPY",
        expiry: Optional[str] = None,
        right: Optional[str] = None,
        strikes_around_atm: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        Fetch real options chain from IBKR with live Greeks.

        This is the KEY advantage over synthetic Black-Scholes:
        - Real bid/ask spreads (not estimated 15% slippage)
        - Real implied volatility from the market
        - Real Greeks from IBKR's model (not our BS estimate)
        - Real open interest and volume

        Args:
            ticker:             Underlying symbol
            expiry:             Expiry in YYYYMMDD format (None = nearest)
            right:              "C" for calls, "P" for puts, None for both
            strikes_around_atm: How many strikes above/below ATM to fetch

        Returns:
            List of dicts with keys: symbol, expiry, strike, right,
            bid, ask, mid, last, volume, open_interest,
            iv, delta, gamma, theta, vega, rho
        """
        self._require_connection()
        ib_insync = self._ib_insync

        # Get underlying price
        underlying = self._make_underlying_contract(ticker)
        self._ib.qualifyContracts(underlying)
        [stock_ticker] = self._ib.reqTickers(underlying)
        underlying_price = stock_ticker.marketPrice()

        if not underlying_price or underlying_price <= 0 or _isnan(underlying_price):
            underlying_price = stock_ticker.close

        # Fallback: if still NaN (no market data subscription), use last daily bar
        if not underlying_price or underlying_price <= 0 or _isnan(underlying_price):
            print(f"  ⚠️  {ticker} live price unavailable — falling back to last daily close")
            bars = self.get_historical_bars(ticker, days=2, interval="1d")
            if bars is not None and not bars.empty:
                underlying_price = float(bars["close"].iloc[-1])
            else:
                raise RuntimeError(
                    f"Cannot determine {ticker} price — no live data and no historical bars"
                )
        print(f"  📈 {ticker} underlying: ${underlying_price:.2f}")

        # Get available expirations and strikes
        chains = self._ib.reqSecDefOptParams(
            underlying.symbol, "", underlying.secType, underlying.conId
        )
        if not chains:
            raise RuntimeError(f"No option parameters available for {ticker}")

        # Pick SMART exchange chain
        chain = next((c for c in chains if c.exchange == "SMART"), chains[0])

        # Pick expiry (nearest if not specified)
        expirations = sorted(chain.expirations)
        if expiry:
            chosen_expiry = expiry
        else:
            # Find nearest expiry (today or next trading day)
            today_str = date.today().strftime("%Y%m%d")
            chosen_expiry = expirations[0]
            for exp in expirations:
                if exp >= today_str:
                    chosen_expiry = exp
                    break
        print(f"  📅 Expiry: {chosen_expiry}")

        # Select strikes around ATM
        all_strikes = sorted(chain.strikes)
        atm_idx = min(range(len(all_strikes)),
                      key=lambda i: abs(all_strikes[i] - underlying_price))
        lo = max(0, atm_idx - strikes_around_atm)
        hi = min(len(all_strikes), atm_idx + strikes_around_atm + 1)
        selected_strikes = all_strikes[lo:hi]

        # Build option contracts
        rights = []
        if right is None or right.upper() in ("C", "CALL"):
            rights.append("C")
        if right is None or right.upper() in ("P", "PUT"):
            rights.append("P")

        contracts = []
        for r in rights:
            for strike in selected_strikes:
                opt = self._make_option_contract(
                    ticker, chosen_expiry, strike, r, chain.exchange
                )
                contracts.append(opt)

        # Qualify in batches (IBKR limit)
        batch_size = 50
        for i in range(0, len(contracts), batch_size):
            batch = contracts[i:i + batch_size]
            self._ib.qualifyContracts(*batch)

        # Request market data
        print(f"  📡 Requesting quotes for {len(contracts)} options ...")
        tickers = self._ib.reqTickers(*contracts)

        # Allow time for Greeks to populate (IBKR computes them async)
        self._ib.sleep(2)

        results = []
        for t in tickers:
            c = t.contract
            if not c or c.secType != "OPT":
                continue

            greeks = t.modelGreeks or t.lastGreeks
            bid = t.bid if t.bid and t.bid > 0 else 0
            ask = t.ask if t.ask and t.ask > 0 else 0
            mid = (bid + ask) / 2 if bid and ask else (t.last or t.close or 0)

            # Defensive getattr for all Greeks — delayed OptionComputation
            # objects may be missing fields like rho, pvDividend, etc.
            def _greek(field):
                if not greeks:
                    return 0
                v = getattr(greeks, field, None)
                if v is None or _isnan(v):
                    return 0
                return v

            result = {
                "symbol": c.symbol,
                "expiry": c.lastTradeDateOrContractMonth,
                "strike": c.strike,
                "right": c.right,
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "last": t.last or 0,
                "volume": t.volume or 0,
                "open_interest": getattr(t, 'openInterest', 0) or 0,
                # Greeks from IBKR's model (real, not estimated)
                "iv": _greek("impliedVol"),
                "delta": _greek("delta"),
                "gamma": _greek("gamma"),
                "theta": _greek("theta"),
                "vega": _greek("vega"),
                "rho": _greek("rho"),
                "underlying_price": underlying_price,
            }
            results.append(result)

        print(f"  ✅ Got {len(results)} option quotes with live Greeks")
        return results

    # ─── 0DTE Option Price Snapshots ──────────────────────────────

    def snapshot_spread_over_day(
        self,
        ticker: str = "SPY",
        short_strike: float = 580,
        long_strike: float = 579,
        right: str = "P",
        expiry: Optional[str] = None,
        interval_sec: int = 300,
        duration_hours: float = 6.5,
    ) -> pd.DataFrame:
        """
        Record real option spread prices throughout the trading day.
        This is the ULTIMATE backtest: real prices, real Greeks, real spreads.

        Sits and watches a specific spread, capturing snapshots every N seconds.
        Perfect for understanding 0DTE dynamics with actual market data.

        Args:
            ticker:         Underlying symbol
            short_strike:   Short leg strike
            long_strike:    Long leg strike  
            right:          "P" for puts, "C" for calls
            expiry:         YYYYMMDD (default = today)
            interval_sec:   Seconds between snapshots (300 = 5 min)
            duration_hours: How long to record (6.5 = full day)

        Returns:
            DataFrame with columns per snapshot:
            time, underlying, short_bid, short_ask, short_mid, short_delta,
            short_gamma, short_theta, long_bid, long_ask, long_mid, long_delta,
            spread_mid, spread_delta, spread_gamma, spread_theta
        """
        self._require_connection()
        ib_insync = self._ib_insync

        if expiry is None:
            expiry = date.today().strftime("%Y%m%d")

        # Create contracts
        stock = self._make_underlying_contract(ticker)
        short_opt = self._make_option_contract(ticker, expiry, short_strike, right)
        long_opt = self._make_option_contract(ticker, expiry, long_strike, right)

        self._ib.qualifyContracts(stock, short_opt, long_opt)

        # Subscribe to streaming data
        self._ib.reqMktData(stock, "", False, False)
        self._ib.reqMktData(short_opt, "", False, False)
        self._ib.reqMktData(long_opt, "", False, False)

        snapshots = []
        total_snapshots = int(duration_hours * 3600 / interval_sec)

        print(f"  📸 Recording {ticker} {short_strike}/{long_strike} {right} spread")
        print(f"     Expiry: {expiry}")
        print(f"     Interval: {interval_sec}s, ~{total_snapshots} snapshots over {duration_hours}h")
        print(f"     Press Ctrl+C to stop early\n")

        try:
            for i in range(total_snapshots):
                self._ib.sleep(interval_sec)

                # Get current tickers
                stock_data = self._ib.ticker(stock)
                short_data = self._ib.ticker(short_opt)
                long_data = self._ib.ticker(long_opt)

                underlying = stock_data.marketPrice() or stock_data.last or 0

                def _extract(t):
                    bid = t.bid if t.bid and t.bid > 0 else 0
                    ask = t.ask if t.ask and t.ask > 0 else 0
                    mid = (bid + ask) / 2 if bid and ask else (t.last or 0)
                    g = t.modelGreeks or t.lastGreeks
                    return {
                        "bid": bid, "ask": ask, "mid": mid,
                        "iv": g.impliedVol if g and g.impliedVol else 0,
                        "delta": g.delta if g and g.delta else 0,
                        "gamma": g.gamma if g and g.gamma else 0,
                        "theta": g.theta if g and g.theta else 0,
                        "vega": g.vega if g and g.vega else 0,
                    }

                short_info = _extract(short_data)
                long_info = _extract(long_data)

                snap = {
                    "time": datetime.now(),
                    "underlying": underlying,
                    "short_strike": short_strike,
                    "long_strike": long_strike,
                    "short_bid": short_info["bid"],
                    "short_ask": short_info["ask"],
                    "short_mid": short_info["mid"],
                    "short_iv": short_info["iv"],
                    "short_delta": short_info["delta"],
                    "short_gamma": short_info["gamma"],
                    "short_theta": short_info["theta"],
                    "long_bid": long_info["bid"],
                    "long_ask": long_info["ask"],
                    "long_mid": long_info["mid"],
                    "long_iv": long_info["iv"],
                    "long_delta": long_info["delta"],
                    "long_gamma": long_info["gamma"],
                    "long_theta": long_info["theta"],
                    # Spread level
                    "spread_mid": short_info["mid"] - long_info["mid"],
                    "spread_delta": short_info["delta"] - long_info["delta"],
                    "spread_gamma": short_info["gamma"] - long_info["gamma"],
                    "spread_theta": short_info["theta"] - long_info["theta"],
                }
                snapshots.append(snap)

                # Progress indicator
                elapsed_min = (i + 1) * interval_sec / 60
                if (i + 1) % 12 == 0 or i == 0:  # Print every hour (12 × 5min)
                    print(f"    [{elapsed_min:.0f}m] ${underlying:.2f} | "
                          f"spread: ${snap['spread_mid']:.2f} | "
                          f"Δ={snap['spread_delta']:.3f} Γ={snap['spread_gamma']:.4f}")

        except KeyboardInterrupt:
            print(f"\n  ⏹️  Stopped after {len(snapshots)} snapshots")

        finally:
            # Cancel market data subscriptions
            self._ib.cancelMktData(stock)
            self._ib.cancelMktData(short_opt)
            self._ib.cancelMktData(long_opt)

        df = pd.DataFrame(snapshots)
        if not df.empty:
            print(f"\n  ✅ Captured {len(df)} snapshots")
            print(f"     Spread range: ${df['spread_mid'].min():.2f} → ${df['spread_mid'].max():.2f}")
            print(f"     Underlying range: ${df['underlying'].min():.2f} → ${df['underlying'].max():.2f}")

        return df

    # ─── Historical Options Data (reconstruct past 0DTE) ─────────

    def get_historical_option_bars(
        self,
        ticker: str,
        expiry: str,
        strike: float,
        right: str,
        days: int = 1,
        interval: str = "5m",
    ) -> pd.DataFrame:
        """
        Fetch historical OHLCV for a specific option contract.

        Note: IBKR historical option data is available but limited:
        - Requires active market data subscription
        - Some contracts may have sparse data
        - Best for recent expirations

        Args:
            ticker:   Underlying symbol
            expiry:   Option expiry YYYYMMDD
            strike:   Strike price
            right:    "C" or "P"
            days:     Days of history
            interval: Bar size

        Returns:
            DataFrame with OHLCV for the option contract
        """
        self._require_connection()
        ib_insync = self._ib_insync

        bar_size_map = {
            "1m": "1 min", "5m": "5 mins", "15m": "15 mins",
            "30m": "30 mins", "1h": "1 hour",
        }
        bar_size = bar_size_map.get(interval, "5 mins")

        opt = ib_insync.Option(ticker.upper(), expiry, strike, right, "SMART")
        self._ib.qualifyContracts(opt)

        bars = self._ib.reqHistoricalData(
            opt,
            endDateTime="",
            durationStr=f"{days} D",
            barSizeSetting=bar_size,
            whatToShow="MIDPOINT",
            useRTH=True,
            formatDate=1,
            keepUpToDate=False,
        )

        if not bars:
            return pd.DataFrame()

        records = [
            {
                "timestamp": pd.Timestamp(bar.date),
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": getattr(bar, "volume", 0),
            }
            for bar in bars
        ]

        df = pd.DataFrame(records).set_index("timestamp")
        print(f"  ✅ {ticker} {expiry} {strike}{right}: {len(df)} option bars")
        return df

    # ─── Account Info ─────────────────────────────────────────────

    def get_account_summary(self) -> Dict[str, Any]:
        """Get account summary (buying power, positions, etc.)."""
        self._require_connection()

        summary = {}
        for item in self._ib.accountSummary():
            summary[item.tag] = {
                "value": item.value,
                "currency": item.currency,
            }

        positions = []
        for pos in self._ib.positions():
            positions.append({
                "symbol": pos.contract.symbol,
                "secType": pos.contract.secType,
                "position": pos.position,
                "avgCost": pos.avgCost,
                "strike": getattr(pos.contract, "strike", None),
                "right": getattr(pos.contract, "right", None),
                "expiry": getattr(pos.contract, "lastTradeDateOrContractMonth", None),
            })

        return {
            "net_liquidation": summary.get("NetLiquidation", {}).get("value"),
            "buying_power": summary.get("BuyingPower", {}).get("value"),
            "cash": summary.get("TotalCashValue", {}).get("value"),
            "unrealized_pnl": summary.get("UnrealizedPnL", {}).get("value"),
            "realized_pnl": summary.get("RealizedPnL", {}).get("value"),
            "positions": positions,
            "position_count": len(positions),
        }

    # ─── Convenience: Quick Status ────────────────────────────────

    def status(self) -> str:
        """Print connection status and account info."""
        lines = []
        lines.append("=" * 60)
        lines.append("IBKR Connection Status")
        lines.append("=" * 60)

        if not self.is_connected():
            lines.append(f"  ❌ Not connected ({self.host}:{self.port})")
            lines.append(f"  Start TWS/Gateway and call .connect()")
        else:
            lines.append(f"  ✅ Connected to {self.host}:{self.port}")
            try:
                acct = self.get_account_summary()
                lines.append(f"  💰 Net Liquidation: ${float(acct['net_liquidation'] or 0):,.2f}")
                lines.append(f"  💳 Buying Power:    ${float(acct['buying_power'] or 0):,.2f}")
                lines.append(f"  📊 Open Positions:  {acct['position_count']}")
                if acct["positions"]:
                    for p in acct["positions"][:5]:
                        extra = ""
                        if p["secType"] == "OPT":
                            extra = f" {p['strike']}{p['right']} exp={p['expiry']}"
                        lines.append(f"     • {p['symbol']} {p['secType']}{extra}: "
                                    f"{p['position']} @ ${p['avgCost']:.2f}")
            except Exception as e:
                lines.append(f"  ⚠️  Account info unavailable: {e}")

        lines.append("=" * 60)
        result = "\n".join(lines)
        print(result)
        return result


# ─── Standalone quick-test ────────────────────────────────────────

if __name__ == "__main__":
    import sys

    provider = IBKRDataProvider()

    if not provider.connect():
        print("\n💡 Quick troubleshooting:")
        print("   1. Is TWS or IB Gateway running?")
        print("   2. Is API enabled? (Edit → Global Config → API → Settings)")
        print(f"   3. Is it listening on port {provider.port}?")
        print("   4. Try: export IBKR_PORT=4001  (for IB Gateway)")
        sys.exit(1)

    provider.status()

    # Quick test: fetch 5 days of 5-min SPY bars
    print("\n📊 Fetching SPY 5-min bars (5 days) ...")
    df = provider.get_historical_bars("SPY", days=5, interval="5m")
    print(df.tail())

    # Quick test: fetch today's 0DTE options chain
    print("\n📋 Fetching SPY options chain (nearest expiry) ...")
    chain = provider.get_options_chain("SPY", strikes_around_atm=5)
    for opt in chain[:10]:
        print(f"  {opt['strike']:>7.1f} {opt['right']}: "
              f"bid=${opt['bid']:.2f} ask=${opt['ask']:.2f} "
              f"Δ={opt['delta']:+.3f} Γ={opt['gamma']:.4f} "
              f"θ={opt['theta']:.3f} IV={opt['iv']:.1%}")

    provider.disconnect()

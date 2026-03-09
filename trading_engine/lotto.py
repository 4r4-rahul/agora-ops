"""
Momentum Lotto Scanner
========================
Systematic long-option scanner for asymmetric (lotto) plays on 0DTE options.

Instead of selling premium (credit spreads), this module BUYS cheap OTM
options when specific momentum triggers fire. The edge comes from
gamma acceleration near expiration — a $0.10 option can go to $2.00+
on a 0.5% sustained move.

Professional Methodologies Implemented:
  • Opening Range Breakout (ORB) — SMB Capital, prop shop staple
  • Mean-Reversion Snap — Kris Sidial / Ambrus Group style
  • VWAP Reclaim/Rejection — Institutional flow confirmation
  • Volume Surge + Direction — Market microstructure signal

Risk Management:
  • Fixed dollar budget per day (1% of account = $100 on $10K)
  • Max $50 per single trade (0.5% of account)
  • Max 2 open lotto positions at once
  • Hard stop at 50% loss of premium paid
  • Auto-close 30 min before market close
  • Profit target at 5x with 30% runner

Usage:
    from trading_engine.lotto import LottoScanner, LottoTrigger

    scanner = LottoScanner(provider, executor, config)
    triggers = scanner.scan(ticker="SPY")
    for t in triggers:
        print(f"  {t.trigger_type}: {t.direction} @ {t.strike} — {t.reason}")

    # Execute best trigger
    if triggers:
        scanner.execute_lotto(triggers[0])
"""

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, date, time as dtime
from typing import List, Optional, Dict, Any

import numpy as np
import pandas as pd

from .config import EngineConfig, LottoConfig

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# Data Structures
# ─────────────────────────────────────────────────────────────────

@dataclass
class LottoTrigger:
    """A momentum trigger signal for a lotto entry."""
    trigger_type: str = ""       # "ORB_BREAKOUT", "MEAN_REV", "VWAP_RECLAIM", "VOLUME_SURGE"
    direction: str = ""          # "CALL" or "PUT"
    ticker: str = ""
    underlying_price: float = 0.0
    strike: float = 0.0
    expiry: str = ""
    right: str = ""              # "C" or "P"
    estimated_premium: float = 0.0
    num_contracts: int = 1
    reason: str = ""
    confidence: float = 0.0      # 0.0-1.0 signal strength
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class LottoPosition:
    """An open lotto (long option) position."""
    ticker: str = ""
    strike: float = 0.0
    right: str = ""
    expiry: str = ""
    num_contracts: int = 0
    entry_price: float = 0.0     # Premium paid per contract
    entry_time: str = ""
    trigger_type: str = ""
    # Tracking
    current_price: float = 0.0
    high_water_mark: float = 0.0  # Highest price seen (for trailing)
    unrealized_pnl: float = 0.0
    # State
    took_partial: bool = False    # True if we already took partial profits
    runner_contracts: int = 0     # Contracts left as runner


# ─────────────────────────────────────────────────────────────────
# Momentum Trigger Detection
# ─────────────────────────────────────────────────────────────────

class MomentumDetector:
    """
    Detects momentum triggers from intraday 1-min bars.

    All triggers are designed around 0DTE gamma mechanics:
    you need a sustained move in one direction to profit from
    gamma acceleration. These triggers identify the START of
    such moves.
    """

    def __init__(self, config: LottoConfig):
        self.cfg = config

    def detect_all(
        self,
        bars: pd.DataFrame,
        ticker: str,
        underlying_price: float,
    ) -> List[LottoTrigger]:
        """
        Run all trigger detectors on current bars.

        Args:
            bars: DataFrame with columns [open, high, low, close, volume]
                  Index is timestamp. Should be 1-min bars, last ~60 bars.
            ticker: Symbol
            underlying_price: Current price

        Returns:
            List of triggered signals, sorted by confidence (best first)
        """
        triggers = []

        if bars is None or len(bars) < 20:
            return triggers

        expiry = date.today().strftime("%Y%m%d")

        # 1. Opening Range Breakout
        orb = self._check_orb_breakout(bars, ticker, underlying_price, expiry)
        if orb:
            triggers.append(orb)

        # 2. Mean-Reversion Snap
        mr = self._check_mean_reversion(bars, ticker, underlying_price, expiry)
        if mr:
            triggers.append(mr)

        # 3. VWAP Reclaim / Rejection
        vwap = self._check_vwap_signal(bars, ticker, underlying_price, expiry)
        if vwap:
            triggers.append(vwap)

        # 4. Volume Surge
        vol = self._check_volume_surge(bars, ticker, underlying_price, expiry)
        if vol:
            triggers.append(vol)

        # Sort by confidence
        triggers.sort(key=lambda t: t.confidence, reverse=True)
        return triggers

    def _check_orb_breakout(
        self, bars: pd.DataFrame, ticker: str, price: float, expiry: str,
    ) -> Optional[LottoTrigger]:
        """
        Opening Range Breakout (ORB) — first 15-min high/low break.

        Logic:
          1. Compute the high and low of the first 15 1-min bars (9:30-9:45)
          2. If current price breaks above the high → buy call
          3. If current price breaks below the low → buy put
          4. Require at least 0.3% beyond range edge (avoid noise)

        Professional basis: SMB Capital ORB framework.
        """
        if len(bars) < 16:
            return None

        # First 15 bars = opening range
        orb_bars = bars.iloc[:15]
        orb_high = orb_bars["high"].max()
        orb_low = orb_bars["low"].min()
        orb_range = orb_high - orb_low

        if orb_range <= 0 or price <= 0:
            return None

        orb_range_pct = orb_range / price

        # Current price vs ORB
        breakout_threshold = price * self.cfg.orb_breakout_min_pct

        # Check for breakout above
        if price > orb_high + breakout_threshold:
            distance_pct = (price - orb_high) / price
            confidence = min(1.0, distance_pct / 0.005)  # Scale: 0.5% = full confidence

            strike = _round_strike(price + price * self.cfg.otm_distance_pct, ticker)
            return LottoTrigger(
                trigger_type="ORB_BREAKOUT",
                direction="CALL",
                ticker=ticker,
                underlying_price=price,
                strike=strike,
                expiry=expiry,
                right="C",
                reason=f"Price ${price:.2f} broke above ORB high ${orb_high:.2f} "
                       f"(+{distance_pct*100:.2f}%, range={orb_range_pct*100:.2f}%)",
                confidence=confidence * 0.8,  # ORB is solid but not the best
            )

        # Check for breakdown below
        if price < orb_low - breakout_threshold:
            distance_pct = (orb_low - price) / price
            confidence = min(1.0, distance_pct / 0.005)

            strike = _round_strike(price - price * self.cfg.otm_distance_pct, ticker)
            return LottoTrigger(
                trigger_type="ORB_BREAKOUT",
                direction="PUT",
                ticker=ticker,
                underlying_price=price,
                strike=strike,
                expiry=expiry,
                right="P",
                reason=f"Price ${price:.2f} broke below ORB low ${orb_low:.2f} "
                       f"(-{distance_pct*100:.2f}%, range={orb_range_pct*100:.2f}%)",
                confidence=confidence * 0.8,
            )

        return None

    def _check_mean_reversion(
        self, bars: pd.DataFrame, ticker: str, price: float, expiry: str,
    ) -> Optional[LottoTrigger]:
        """
        Mean-Reversion Snap — buy the reversal after a sharp flush.

        Logic:
          1. Detect a 0.8%+ move down from session high (or up from low)
          2. Look for first reversal candle (green after reds, or vice versa)
          3. Buy in the reversal direction

        Professional basis: Kris Sidial / Ambrus Group tail-reversal framework.
        """
        if len(bars) < 10:
            return None

        session_high = bars["high"].max()
        session_low = bars["low"].min()
        flush_threshold = self.cfg.mean_rev_flush_pct

        # Check for downside flush + reversal
        drop_pct = (session_high - price) / session_high if session_high > 0 else 0
        if drop_pct >= flush_threshold:
            # Look for reversal: last bar is green after red bars
            recent = bars.tail(5)
            last_bar_green = recent.iloc[-1]["close"] > recent.iloc[-1]["open"]
            prev_bars_red = sum(
                1 for _, b in recent.iloc[:-1].iterrows()
                if b["close"] < b["open"]
            )

            if last_bar_green and prev_bars_red >= 2:
                confidence = min(1.0, drop_pct / 0.012)  # Full at 1.2% drop
                strike = _round_strike(price + price * self.cfg.otm_distance_pct, ticker)
                return LottoTrigger(
                    trigger_type="MEAN_REV",
                    direction="CALL",
                    ticker=ticker,
                    underlying_price=price,
                    strike=strike,
                    expiry=expiry,
                    right="C",
                    reason=f"Reversal after {drop_pct*100:.2f}% flush from high "
                           f"${session_high:.2f} → ${price:.2f}",
                    confidence=confidence * 0.9,  # Mean-rev is high confidence
                )

        # Check for upside flush + reversal
        rise_pct = (price - session_low) / session_low if session_low > 0 else 0
        if rise_pct >= flush_threshold:
            recent = bars.tail(5)
            last_bar_red = recent.iloc[-1]["close"] < recent.iloc[-1]["open"]
            prev_bars_green = sum(
                1 for _, b in recent.iloc[:-1].iterrows()
                if b["close"] > b["open"]
            )

            if last_bar_red and prev_bars_green >= 2:
                confidence = min(1.0, rise_pct / 0.012)
                strike = _round_strike(price - price * self.cfg.otm_distance_pct, ticker)
                return LottoTrigger(
                    trigger_type="MEAN_REV",
                    direction="PUT",
                    ticker=ticker,
                    underlying_price=price,
                    strike=strike,
                    expiry=expiry,
                    right="P",
                    reason=f"Reversal after {rise_pct*100:.2f}% rip from low "
                           f"${session_low:.2f} → ${price:.2f}",
                    confidence=confidence * 0.9,
                )

        return None

    def _check_vwap_signal(
        self, bars: pd.DataFrame, ticker: str, price: float, expiry: str,
    ) -> Optional[LottoTrigger]:
        """
        VWAP Reclaim / Rejection — institutional flow confirmation.

        Logic:
          1. Calculate session VWAP
          2. If price was below VWAP for 30+ bars, then closes above → buy call
          3. If price was above VWAP for 30+ bars, then closes below → buy put
          4. VWAP cross with volume surge = stronger signal

        Professional basis: Every institutional desk watches VWAP.
        Reclaiming VWAP after extended time below = trapped shorts covering.
        """
        if len(bars) < 30 or "volume" not in bars.columns:
            return None

        # Calculate session VWAP
        typical_price = (bars["high"] + bars["low"] + bars["close"]) / 3
        cum_tp_vol = (typical_price * bars["volume"]).cumsum()
        cum_vol = bars["volume"].cumsum()
        vwap = cum_tp_vol / cum_vol.replace(0, np.nan)

        if vwap.isna().all():
            return None

        current_vwap = vwap.iloc[-1]
        if np.isnan(current_vwap) or current_vwap <= 0:
            return None

        # How many recent bars were below/above VWAP?
        recent_30 = bars.tail(min(30, len(bars)))
        recent_vwap = vwap.tail(min(30, len(bars)))

        below_vwap_count = sum(
            1 for i in range(len(recent_30) - self.cfg.vwap_reclaim_bars)
            if recent_30.iloc[i]["close"] < recent_vwap.iloc[i]
        )
        above_vwap_count = len(recent_30) - self.cfg.vwap_reclaim_bars - below_vwap_count

        # Check last N bars for reclaim
        reclaim_bars = self.cfg.vwap_reclaim_bars
        last_n = recent_30.tail(reclaim_bars)
        last_n_vwap = recent_vwap.tail(reclaim_bars)

        all_above = all(
            last_n.iloc[i]["close"] > last_n_vwap.iloc[i]
            for i in range(len(last_n))
            if not np.isnan(last_n_vwap.iloc[i])
        )
        all_below = all(
            last_n.iloc[i]["close"] < last_n_vwap.iloc[i]
            for i in range(len(last_n))
            if not np.isnan(last_n_vwap.iloc[i])
        )

        # VWAP Reclaim (was below, now above)
        if all_above and below_vwap_count >= 20:
            confidence = min(1.0, below_vwap_count / 25) * 0.75
            strike = _round_strike(price + price * self.cfg.otm_distance_pct, ticker)
            return LottoTrigger(
                trigger_type="VWAP_RECLAIM",
                direction="CALL",
                ticker=ticker,
                underlying_price=price,
                strike=strike,
                expiry=expiry,
                right="C",
                reason=f"VWAP reclaim after {below_vwap_count} bars below "
                       f"(VWAP=${current_vwap:.2f}, price=${price:.2f})",
                confidence=confidence,
            )

        # VWAP Rejection (was above, now below)
        if all_below and above_vwap_count >= 20:
            confidence = min(1.0, above_vwap_count / 25) * 0.75
            strike = _round_strike(price - price * self.cfg.otm_distance_pct, ticker)
            return LottoTrigger(
                trigger_type="VWAP_REJECT",
                direction="PUT",
                ticker=ticker,
                underlying_price=price,
                strike=strike,
                expiry=expiry,
                right="P",
                reason=f"VWAP rejection after {above_vwap_count} bars above "
                       f"(VWAP=${current_vwap:.2f}, price=${price:.2f})",
                confidence=confidence,
            )

        return None

    def _check_volume_surge(
        self, bars: pd.DataFrame, ticker: str, price: float, expiry: str,
    ) -> Optional[LottoTrigger]:
        """
        Volume Surge + Directional Move — market microstructure signal.

        Logic:
          1. Compare last 3 bars' average volume to 20-bar average
          2. If volume is 3x+ average AND price moving in one direction → signal
          3. High volume = institutional participation, not noise

        Professional basis: Market microstructure / order flow analysis.
        """
        if len(bars) < 25 or "volume" not in bars.columns:
            return None

        avg_vol_20 = bars["volume"].iloc[-25:-5].mean()
        if avg_vol_20 <= 0:
            return None

        recent_vol = bars["volume"].iloc[-3:].mean()
        vol_ratio = recent_vol / avg_vol_20

        if vol_ratio < self.cfg.volume_surge_mult:
            return None

        # Determine direction from recent 3 bars
        recent_close = bars["close"].iloc[-3:]
        recent_open = bars["open"].iloc[-3:]
        net_move = recent_close.iloc[-1] - recent_open.iloc[0]
        move_pct = net_move / price if price > 0 else 0

        # Need at least 0.15% move with the volume surge
        if abs(move_pct) < 0.0015:
            return None

        confidence = min(1.0, vol_ratio / 5.0) * 0.7

        if net_move > 0:
            strike = _round_strike(price + price * self.cfg.otm_distance_pct, ticker)
            return LottoTrigger(
                trigger_type="VOLUME_SURGE",
                direction="CALL",
                ticker=ticker,
                underlying_price=price,
                strike=strike,
                expiry=expiry,
                right="C",
                reason=f"Volume surge {vol_ratio:.1f}x avg with "
                       f"+{move_pct*100:.2f}% move over 3 bars",
                confidence=confidence,
            )
        else:
            strike = _round_strike(price - price * self.cfg.otm_distance_pct, ticker)
            return LottoTrigger(
                trigger_type="VOLUME_SURGE",
                direction="PUT",
                ticker=ticker,
                underlying_price=price,
                strike=strike,
                expiry=expiry,
                right="P",
                reason=f"Volume surge {vol_ratio:.1f}x avg with "
                       f"{move_pct*100:.2f}% move over 3 bars",
                confidence=confidence,
            )


# ─────────────────────────────────────────────────────────────────
# Lotto Scanner (orchestrator)
# ─────────────────────────────────────────────────────────────────

class LottoScanner:
    """
    Orchestrates lotto scanning, strike selection, sizing, and execution.

    Lifecycle (called from LiveTradingLoop):
      1. scan()    → Detect momentum triggers
      2. select()  → Pick best strike from chain for top trigger
      3. size()    → Calculate position size within budget
      4. execute() → Place order via OrderExecutor
      5. monitor() → Track open lottos for exit

    Budget discipline:
      • Daily spend capped at account × lotto_daily_budget_pct
      • Per-trade capped at account × lotto_max_per_trade_pct
      • Max open positions = lotto_max_positions
    """

    def __init__(
        self,
        provider,           # IBKRDataProvider
        executor,           # OrderExecutor
        config: EngineConfig,
        account_size: float = 10_000.0,
    ):
        self.provider = provider
        self.executor = executor
        self.config = config
        self.lotto_cfg = config.lotto
        self.account_size = account_size

        self.detector = MomentumDetector(self.lotto_cfg)

        # State
        self.open_positions: List[LottoPosition] = []
        self.daily_spent: float = 0.0
        self.daily_realized_pnl: float = 0.0
        self.trades_today: int = 0

    # ─── Budget ──────────────────────────────────────────────────

    @property
    def daily_budget(self) -> float:
        return self.account_size * self.config.account.lotto_daily_budget_pct

    @property
    def per_trade_budget(self) -> float:
        return self.account_size * self.config.account.lotto_max_per_trade_pct

    @property
    def budget_remaining(self) -> float:
        return max(0, self.daily_budget - self.daily_spent)

    def can_enter(self) -> tuple:
        """Check if we can enter a new lotto. Returns (bool, reason)."""
        if len(self.open_positions) >= self.config.account.lotto_max_positions:
            return False, f"Max {self.config.account.lotto_max_positions} lotto positions reached"
        if self.budget_remaining <= 0:
            return False, f"Daily lotto budget exhausted (${self.daily_spent:.2f}/${self.daily_budget:.2f})"
        return True, "OK"

    # ─── Scan ────────────────────────────────────────────────────

    def scan(self, ticker: str) -> List[LottoTrigger]:
        """
        Scan for momentum triggers on a ticker.

        Fetches recent 1-min bars and runs all detectors.
        """
        if not self.provider:
            return []

        try:
            bars = self.provider.get_historical_bars(
                ticker, days=1, interval="1m"
            )
            if bars is None or len(bars) < 16:
                return []

            current_price = float(bars["close"].iloc[-1])

            triggers = self.detector.detect_all(bars, ticker, current_price)

            if triggers:
                logger.info(f"Lotto scan: {len(triggers)} triggers for {ticker}")
                for t in triggers:
                    logger.info(f"  {t.trigger_type} {t.direction} conf={t.confidence:.2f}: {t.reason}")

            return triggers

        except Exception as e:
            logger.warning(f"Lotto scan failed for {ticker}: {e}")
            return []

    # ─── Strike Selection from Live Chain ────────────────────────

    def select_strike(self, trigger: LottoTrigger) -> Optional[Dict]:
        """
        Given a trigger, find the best strike from the live options chain.

        Selects based on:
          - Delta between target_delta_min and target_delta_max
          - Premium within budget
          - Reasonable bid-ask spread (< 50% of mid)
        """
        if not self.provider:
            return None

        try:
            chain = self.provider.get_options_chain(
                trigger.ticker,
                expiry=trigger.expiry,
                strikes_around_atm=20,
            )
        except Exception as e:
            logger.warning(f"Failed to get chain for lotto: {e}")
            return None

        if not chain:
            return None

        # Filter to correct side
        options = [
            o for o in chain
            if o.get("right") == trigger.right
        ]

        if not options:
            return None

        # Find best option: within delta range, cheapest that meets criteria
        candidates = []
        for opt in options:
            delta = abs(opt.get("delta", 0))
            ask = opt.get("ask", 0)
            bid = opt.get("bid", 0)
            mid = opt.get("mid", (bid + ask) / 2 if bid and ask else 0)

            # Delta filter
            if delta < self.lotto_cfg.target_delta_min:
                continue
            if delta > self.lotto_cfg.target_delta_max:
                continue

            # Price filter
            if ask <= 0 or ask < self.lotto_cfg.min_premium:
                continue
            if ask > self.lotto_cfg.max_premium_per_trade:
                continue

            # Spread quality: bid-ask should be < 50% of mid
            if mid > 0 and (ask - bid) / mid > 0.50:
                continue

            candidates.append({
                "strike": opt["strike"],
                "right": trigger.right,
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "delta": delta,
                "gamma": opt.get("gamma", 0),
                "iv": opt.get("iv", 0),
            })

        if not candidates:
            logger.info(f"No suitable lotto strikes for {trigger.ticker} {trigger.right}")
            return None

        # Sort by gamma (higher gamma = more bang for the buck on 0DTE)
        candidates.sort(key=lambda c: abs(c.get("gamma", 0)), reverse=True)

        best = candidates[0]
        logger.info(
            f"Lotto strike selected: {trigger.ticker} {best['strike']}{best['right']} "
            f"ask=${best['ask']:.2f} Δ={best['delta']:.3f} Γ={best['gamma']:.4f}"
        )
        return best

    # ─── Position Sizing ─────────────────────────────────────────

    def size_position(self, ask_price: float) -> int:
        """
        Calculate number of contracts within budget.

        Rules:
          - Never spend more than per_trade_budget on one trade
          - Never exceed daily budget remaining
          - Never exceed max_contracts_per_trade
          - At least 1 contract
        """
        if ask_price <= 0:
            return 0

        cost_per_contract = ask_price * 100  # Options = 100 shares

        # Budget constraint
        max_by_per_trade = int(self.per_trade_budget / cost_per_contract)
        max_by_daily = int(self.budget_remaining / cost_per_contract)
        max_by_config = self.lotto_cfg.max_contracts_per_trade

        num = min(max_by_per_trade, max_by_daily, max_by_config)
        return max(1, num) if num >= 1 else 0

    # ─── Execute ─────────────────────────────────────────────────

    def execute_lotto(
        self,
        trigger: LottoTrigger,
        strike_info: Dict,
        dry_run: bool = False,
    ) -> Optional[LottoPosition]:
        """
        Execute a lotto trade: buy the selected option.

        Returns LottoPosition if filled, None otherwise.
        """
        can, reason = self.can_enter()
        if not can:
            print(f"  🎰 Lotto blocked: {reason}")
            return None

        ask = strike_info["ask"]
        num_contracts = self.size_position(ask)
        if num_contracts <= 0:
            print(f"  🎰 Lotto: can't afford (ask=${ask:.2f}, budget=${self.budget_remaining:.2f})")
            return None

        total_cost = ask * num_contracts * 100

        print(f"\n  🎰 LOTTO ENTRY:")
        print(f"    Trigger:    {trigger.trigger_type} ({trigger.confidence:.0%} confidence)")
        print(f"    Direction:  {trigger.direction}")
        print(f"    Strike:     {trigger.ticker} {strike_info['strike']}{strike_info['right']}")
        print(f"    Ask:        ${ask:.2f}/contract")
        print(f"    Contracts:  {num_contracts}")
        print(f"    Total cost: ${total_cost:.2f}")
        print(f"    Budget:     ${self.budget_remaining:.2f} remaining today")
        print(f"    Reason:     {trigger.reason}")

        if dry_run:
            print(f"  🧪 DRY RUN — would buy {num_contracts}x {trigger.ticker} "
                  f"{strike_info['strike']}{strike_info['right']}")
            return None

        # Place the order (use limit at the ask for faster fill)
        fill = self.executor.buy_option(
            ticker=trigger.ticker,
            expiry=trigger.expiry,
            strike=strike_info["strike"],
            right=trigger.right,
            num_contracts=num_contracts,
            limit_price=ask,
        )

        if fill.status.value == "FILLED":
            entry_price = abs(fill.avg_fill_price)
            actual_cost = entry_price * fill.num_filled * 100

            pos = LottoPosition(
                ticker=trigger.ticker,
                strike=strike_info["strike"],
                right=trigger.right,
                expiry=trigger.expiry,
                num_contracts=fill.num_filled,
                entry_price=entry_price,
                entry_time=datetime.now().isoformat(),
                trigger_type=trigger.trigger_type,
                current_price=entry_price,
                high_water_mark=entry_price,
            )

            self.open_positions.append(pos)
            self.daily_spent += actual_cost
            self.trades_today += 1

            print(f"  ✅ LOTTO FILLED: {fill.num_filled}x @ ${entry_price:.2f} "
                  f"(${actual_cost:.2f} total)")

            return pos
        else:
            print(f"  ❌ Lotto not filled: {fill.status.value} — {fill.error_msg}")
            return None

    # ─── Monitor & Exit ──────────────────────────────────────────

    def check_exits(self, minutes_to_close: int) -> List[LottoPosition]:
        """
        Check all open lotto positions for exit triggers.

        Exit rules:
          1. Stop-loss: premium dropped 50% → sell all
          2. Profit target: premium hit 5x → sell 70%, keep 30% as runner
          3. Runner exit: after partial, if price drops below 3x → sell rest
          4. Time exit: 30 min before close → sell everything
          5. High-water trailing: if hit 3x then drops 40% from HWM → sell

        Returns list of closed positions.
        """
        if not self.open_positions:
            return []

        closed = []
        remaining = []

        for pos in self.open_positions:
            exit_reason = self._check_lotto_exit(pos, minutes_to_close)

            if exit_reason:
                # Determine how many to sell
                contracts_to_sell = pos.num_contracts

                # Partial profit taking at 5x
                if (exit_reason == "PROFIT_TARGET" and
                        not pos.took_partial and
                        pos.num_contracts >= 2):
                    # Sell 70%, keep 30% as runner
                    sell_count = max(1, int(pos.num_contracts * (1 - self.lotto_cfg.runner_keep_pct)))
                    runner_count = pos.num_contracts - sell_count
                    contracts_to_sell = sell_count

                    print(f"  🎰 Partial take: sell {sell_count}, keep {runner_count} as runner")
                    pos.took_partial = True
                    pos.runner_contracts = runner_count
                else:
                    contracts_to_sell = pos.num_contracts

                # Execute the exit
                fill = self.executor.sell_option(
                    ticker=pos.ticker,
                    expiry=pos.expiry,
                    strike=pos.strike,
                    right=pos.right,
                    num_contracts=contracts_to_sell,
                )

                if fill.status.value == "FILLED":
                    exit_price = abs(fill.avg_fill_price)
                    realized = (exit_price - pos.entry_price) * fill.num_filled * 100
                    realized -= fill.commission
                    self.daily_realized_pnl += realized

                    mult = exit_price / pos.entry_price if pos.entry_price > 0 else 0
                    print(f"  🎰 LOTTO EXIT: {pos.ticker} {pos.strike}{pos.right}"
                          f" → {exit_reason} | {mult:.1f}x | P&L: ${realized:+.2f}")

                    # If partial, update position
                    if pos.took_partial and pos.runner_contracts > 0 and exit_reason == "PROFIT_TARGET":
                        pos.num_contracts = pos.runner_contracts
                        remaining.append(pos)
                        continue

                pos.unrealized_pnl = realized if fill.status.value == "FILLED" else 0
                closed.append(pos)
            else:
                remaining.append(pos)

        self.open_positions = remaining
        return closed

    def _check_lotto_exit(self, pos: LottoPosition, minutes_to_close: int) -> Optional[str]:
        """Check exit conditions for a single lotto position."""
        # Time exit
        if minutes_to_close <= self.lotto_cfg.time_exit_minutes_before_close:
            return "TIME_EXIT"

        # Get current price
        try:
            chain = self.provider.get_options_chain(
                pos.ticker, expiry=pos.expiry, strikes_around_atm=20,
            )
            opt = next(
                (o for o in chain
                 if abs(o["strike"] - pos.strike) < 0.01 and o["right"] == pos.right),
                None,
            )
            if not opt:
                return None

            current = opt.get("mid", 0) or opt.get("bid", 0)
            if current <= 0:
                return None

            pos.current_price = current
            pos.high_water_mark = max(pos.high_water_mark, current)

        except Exception as e:
            logger.warning(f"Failed to price lotto {pos.ticker} {pos.strike}{pos.right}: {e}")
            return None

        entry = pos.entry_price
        if entry <= 0:
            return None

        mult = current / entry

        # 1. Stop-loss: premium dropped 50%
        if mult <= (1 - self.lotto_cfg.stop_loss_pct):
            return f"STOP_LOSS ({mult:.2f}x entry)"

        # 2. Profit target: hit 5x (or runner_keep_pct threshold)
        if mult >= self.lotto_cfg.profit_target_mult and not pos.took_partial:
            return "PROFIT_TARGET"

        # 3. Runner exit: if took partial and price drops below 3x from entry
        if pos.took_partial and mult < 3.0:
            return f"RUNNER_EXIT ({mult:.1f}x, below 3x floor)"

        # 4. Trailing from high-water mark: if dropped 40% from peak
        if pos.high_water_mark > entry * 2:  # Only trail if we've seen 2x+
            drop_from_peak = (pos.high_water_mark - current) / pos.high_water_mark
            if drop_from_peak >= 0.40:
                return f"TRAILING_EXIT (dropped {drop_from_peak:.0%} from ${pos.high_water_mark:.2f})"

        return None

    # ─── Status ──────────────────────────────────────────────────

    def print_status(self):
        """Print current lotto scanner status."""
        print(f"\n  🎰 LOTTO SCANNER STATUS")
        print(f"  {'─' * 50}")
        print(f"  Budget:     ${self.budget_remaining:.2f} / ${self.daily_budget:.2f} remaining")
        print(f"  Spent:      ${self.daily_spent:.2f}")
        print(f"  Realized:   ${self.daily_realized_pnl:+.2f}")
        print(f"  Trades:     {self.trades_today}")
        print(f"  Open:       {len(self.open_positions)}")

        for pos in self.open_positions:
            mult = pos.current_price / pos.entry_price if pos.entry_price > 0 else 0
            icon = "🟢" if mult > 1 else "🔴"
            print(f"    {icon} {pos.ticker} {pos.strike}{pos.right} "
                  f"({pos.trigger_type}) {pos.num_contracts}x "
                  f"entry=${pos.entry_price:.2f} now=${pos.current_price:.2f} "
                  f"({mult:.1f}x)")

    def reset_daily(self):
        """Reset daily counters (call on new trading day)."""
        self.daily_spent = 0.0
        self.daily_realized_pnl = 0.0
        self.trades_today = 0
        # Don't clear open_positions — they persist intraday


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────

def _round_strike(price: float, ticker: str) -> float:
    """Round to nearest valid strike increment."""
    ticker_upper = ticker.upper()
    if ticker_upper in ("SPY", "QQQ", "IWM", "AAPL"):
        return round(price)  # $1 increments
    elif ticker_upper in ("SPX", "NDX"):
        return round(price / 5) * 5  # $5 increments
    else:
        return round(price)

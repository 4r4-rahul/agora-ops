"""
SPX 0DTE Gamma Scalper — Professional Directional Scalping
===========================================================

This replaces the "lottery ticket" approach (buy cheap OTM options and pray)
with the strategy used by professional retail 0DTE algo traders:

  BUY ATM OPTIONS → SCALP GAMMA ON DIRECTIONAL MOVES → EXIT FAST

Seven Fundamental Shifts from Old Strategy:
───────────────────────────────────────────
  OLD: "Buy $0.10 OTM lotto → hope for 10x"
  NEW: "Buy $2.00 ATM → scalp 40-80% on a directional move"

  OLD: "Stop if premium drops 50%" (fires from theta alone!)
  NEW: "Stop if underlying moves 3×ATR against entry"

  OLD: "Wait for 3x return (300% gain)"
  NEW: "Take profit at 5×ATR move (R:R ≈ 1:1.9)"

  OLD: "Any single trigger = entry"
  NEW: "2+ confirmations required"

  OLD: "Scan all day"
  NEW: "Morning window + Power hour only"

  OLD: "5 trades/day, unlimited re-entry"
  NEW: "3 max, no re-entry same direction"

  OLD: "Hold until stop or target"
  NEW: "Time stop if no move in 12 min"

Why ATM Options Work for Scalping (data-calibrated):
────────────────────────────────────────────────────
  • Delta ~0.50: every $1 underlying move = $0.50 option gain
  • Gamma asymmetry: $1 up = +$0.54, $1 down = -$0.48 (buyer's edge)
  • Theta at open: $0.01/15min on $3 ATM option (0.3% — negligible!)
  • ATR(15) = $0.26 median → stop at $0.78, target at $1.30
  • 54% of the time SPY makes ≥$1.00 move in 30 min

Architecture:
─────────────
  SignalEngine  → 5 confirmation components voting BULL/BEAR/NEUTRAL
                  + Chop Gate (anti-signal filter)
                  → Need 2+ to agree for entry

  ScalpExitEngine → Exits based on UNDERLYING PRICE movement
                    (NOT option premium — the critical difference)
                    → Stop / Target / Trail / Time / MaxHold / EOD

  ScalpScanner  → Live orchestrator (provider + executor)
                  → Budget, risk management, position tracking

Usage:
    from trading_engine.scalper import SignalEngine, ScalpExitEngine, ScalpScanner

    # Backtesting (uses SignalEngine + ScalpExitEngine)
    engine = SignalEngine(config.scalp)
    signal = engine.evaluate(bars, price)

    # Live trading
    scanner = ScalpScanner(provider, executor, config)
    scanner.scan("SPX")
"""

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, date, time as dtime
from typing import List, Optional, Dict, Any

import numpy as np
import pandas as pd

from .config import EngineConfig, ScalpConfig, get_ticker_profile, TickerProfile

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# Data Structures
# ─────────────────────────────────────────────────────────────────

@dataclass
class ScalpSignal:
    """A confirmed directional scalp signal (2+ confirmations)."""
    direction: str = ""                  # "CALL" or "PUT"
    confirmations: List[str] = field(default_factory=list)  # e.g. ["VWAP", "EMA", "BREAKOUT"]
    confidence: float = 0.0              # 0.0-1.0 (num_confirmations / 5)
    entry_underlying: float = 0.0        # Underlying price at signal
    atr: float = 0.0                     # Current ATR for exit calculation
    timestamp: datetime = field(default_factory=datetime.now)
    ticker: str = ""


@dataclass
class ScalpPosition:
    """An open scalp position being tracked."""
    # Identity
    ticker: str = ""
    strike: float = 0.0
    right: str = ""                      # "C" or "P"
    direction: str = ""                  # "CALL" or "PUT"
    expiry: str = ""
    confirmations: List[str] = field(default_factory=list)
    confidence: float = 0.0

    # Entry
    entry_time: datetime = field(default_factory=datetime.now)
    entry_underlying: float = 0.0        # Underlying price at entry
    entry_premium: float = 0.0           # Option premium paid
    num_contracts: int = 1
    atr_at_entry: float = 0.0            # ATR for stop/target calc

    # Pre-computed exit levels (underlying price)
    stop_price: float = 0.0              # Exit if underlying hits this
    target_price: float = 0.0            # Take profit here

    # Tracking
    current_underlying: float = 0.0
    current_premium: float = 0.0
    best_favorable_underlying: float = 0.0  # Best underlying in our direction
    is_open: bool = True

    # Exit
    exit_time: Optional[datetime] = None
    exit_underlying: float = 0.0
    exit_premium: float = 0.0
    exit_reason: str = ""
    total_pnl: float = 0.0


# ─────────────────────────────────────────────────────────────────
# Signal Engine — Confirmation-Based Entry
# ─────────────────────────────────────────────────────────────────

class SignalEngine:
    """
    Confirmation-based directional signal generation.

    Instead of firing on any SINGLE trigger (old approach), this engine
    evaluates 5 independent signal components. Each votes BULL, BEAR,
    or NEUTRAL. A signal only fires when 2+ components agree.

    Components:
      1. VWAP — Institutional flow shift (fresh VWAP cross)
      2. EMA Trend — 9/21 EMA alignment + slope
      3. Breakout — Compression → expansion breakout (LEADING signal)
      4. Volume — Above-average volume confirming direction
      5. ORB Context — Opening range breakout/breakdown (first 60 min)

    Anti-Signal Gate:
      Chop Filter — If EMA9/21 are tangled AND price hugs VWAP,
      the market is in chop and NO signals are generated.

    This produces far fewer but far higher-quality signals than
    the old MomentumDetector.
    """

    def __init__(self, config: ScalpConfig, bar_minutes: int = 1):
        self.cfg = config
        self.bar_minutes = bar_minutes

        # Pre-compute time-scaled lookback windows
        self.ema_fast = max(3, 9 // bar_minutes)
        self.ema_slow = max(5, 21 // bar_minutes)
        self.vol_lookback = max(10, 20 // bar_minutes)
        self.vol_recent = max(2, 3 // bar_minutes)
        self.atr_period = max(5, config.atr_period // bar_minutes)

        # ORB: first 15 minutes
        self.orb_bar_count = max(3, 15 // bar_minutes)

    def evaluate(
        self,
        bars: pd.DataFrame,
        price: float,
        ticker: str = "",
    ) -> Optional[ScalpSignal]:
        """
        Evaluate all signal components and return a signal if 2+ confirm.

        Args:
            bars: DataFrame with [open, high, low, close, volume].
                  Must have enough history for EMAs (21+ bars).
            price: Current underlying price.
            ticker: Symbol (for signal metadata).

        Returns:
            ScalpSignal if 2+ components agree, else None.
        """
        if bars is None or len(bars) < max(30, self.ema_slow + 10):
            return None

        # Compute session VWAP
        vwap = self._compute_session_vwap(bars)

        # Compute current ATR
        atr = self.compute_atr(bars, self.atr_period)
        if atr <= 0 or atr < self.cfg.min_atr:
            return None  # Too low volatility — no directional moves to scalp

        # ── Anti-Signal Gate: Chop Filter ────────────────────────
        if self._is_chop(bars, price, vwap, atr):
            return None

        # ── Evaluate Components ──────────────────────────────────
        bullish: List[str] = []
        bearish: List[str] = []

        # 1. VWAP
        v = self._check_vwap(bars, price, vwap)
        if v == "BULL":
            bullish.append("VWAP")
        elif v == "BEAR":
            bearish.append("VWAP")

        # 2. EMA Trend
        e = self._check_ema_trend(bars, price)
        if e == "BULL":
            bullish.append("EMA")
        elif e == "BEAR":
            bearish.append("EMA")

        # 3. Breakout (compression → expansion)
        b = self._check_breakout(bars, price)
        if b == "BULL":
            bullish.append("BREAKOUT")
        elif b == "BEAR":
            bearish.append("BREAKOUT")

        # 4. Volume
        vol = self._check_volume(bars)
        if vol == "BULL":
            bullish.append("VOLUME")
        elif vol == "BEAR":
            bearish.append("VOLUME")

        # 5. ORB Context
        orb = self._check_orb_context(bars, price)
        if orb == "BULL":
            bullish.append("ORB")
        elif orb == "BEAR":
            bearish.append("ORB")

        # ── Require min_confirmations to agree ───────────────────
        min_conf = self.cfg.min_confirmations

        if len(bullish) >= min_conf:
            # MANDATORY: VOLUME + VWAP must be present
            # Data shows combos without these are noise (negative WR drag)
            if "VOLUME" not in bullish or "VWAP" not in bullish:
                return None
            return ScalpSignal(
                direction="CALL",
                confirmations=bullish,
                confidence=len(bullish) / 5.0,
                entry_underlying=price,
                atr=atr,
                timestamp=bars.index[-1] if hasattr(bars.index[-1], 'hour') else datetime.now(),
                ticker=ticker,
            )

        if len(bearish) >= min_conf:
            # MANDATORY: VOLUME + VWAP must be present
            if "VOLUME" not in bearish or "VWAP" not in bearish:
                return None
            return ScalpSignal(
                direction="PUT",
                confirmations=bearish,
                confidence=len(bearish) / 5.0,
                entry_underlying=price,
                atr=atr,
                timestamp=bars.index[-1] if hasattr(bars.index[-1], 'hour') else datetime.now(),
                ticker=ticker,
            )

        return None

    # ─── Component: VWAP ─────────────────────────────────────────

    def _check_vwap(
        self, bars: pd.DataFrame, price: float, vwap: pd.Series,
    ) -> str:
        """
        VWAP cross dynamics — institutional flow shift.

        BULL: Price CROSSED above VWAP recently (was below for 5+ bars,
              now above for last 2+ bars) — trapped shorts covering.
        BEAR: Price CROSSED below VWAP recently (was above for 5+ bars,
              now below for last 2+ bars) — longs liquidating.
        NEUTRAL: No recent cross or price stuck near VWAP.

        This is stricter than just "above/below VWAP" — it requires
        a TRANSITION which signals a flow regime change.
        """
        if vwap is None or len(vwap) < 10:
            return "NEUTRAL"

        current_vwap = vwap.iloc[-1]
        if pd.isna(current_vwap) or current_vwap <= 0:
            return "NEUTRAL"

        # Must be meaningfully away from VWAP
        distance_pct = (price - current_vwap) / current_vwap
        if abs(distance_pct) < self.cfg.vwap_distance_min_pct:
            return "NEUTRAL"

        # Check for cross: look back 10 bars, need at least 5 on other side
        lookback = min(10, len(bars) - 2)
        if lookback < 5:
            return "NEUTRAL"

        prior_closes = bars["close"].iloc[-lookback-2:-2]
        prior_vwap = vwap.iloc[-lookback-2:-2]

        below_count = sum(
            1 for c, v in zip(prior_closes, prior_vwap)
            if not pd.isna(v) and c < v
        )
        above_count = sum(
            1 for c, v in zip(prior_closes, prior_vwap)
            if not pd.isna(v) and c > v
        )

        # Last 2 bars confirm current side
        last_2_above = all(
            bars["close"].iloc[-j] > vwap.iloc[-j]
            for j in range(1, 3)
            if not pd.isna(vwap.iloc[-j])
        )
        last_2_below = all(
            bars["close"].iloc[-j] < vwap.iloc[-j]
            for j in range(1, 3)
            if not pd.isna(vwap.iloc[-j])
        )

        # VWAP Reclaim: was below 5+ bars, now above
        if last_2_above and below_count >= 5:
            return "BULL"

        # VWAP Rejection: was above 5+ bars, now below
        if last_2_below and above_count >= 5:
            return "BEAR"

        return "NEUTRAL"

    # ─── Component: EMA Trend ────────────────────────────────────

    def _check_ema_trend(self, bars: pd.DataFrame, price: float) -> str:
        """
        EMA 9/21 alignment + slope — trend structure.

        BULL: EMA9 > EMA21, EMA9 rising, price above EMA9
        BEAR: EMA9 < EMA21, EMA9 falling, price below EMA9
        NEUTRAL: EMAs tangled or flat
        """
        close = bars["close"]
        ema9 = close.ewm(span=self.ema_fast, adjust=False).mean()
        ema21 = close.ewm(span=self.ema_slow, adjust=False).mean()

        cur_ema9 = ema9.iloc[-1]
        cur_ema21 = ema21.iloc[-1]
        prev_ema9 = ema9.iloc[-4]  # 3 bars ago for slope

        # EMA slope (per bar)
        slope = (cur_ema9 - prev_ema9) / (3 * price) if price > 0 else 0
        min_slope = self.cfg.ema_slope_min_pct

        # Bullish: 9 > 21, positive slope, price above 9
        if cur_ema9 > cur_ema21 and slope > min_slope and price > cur_ema9:
            return "BULL"

        # Bearish: 9 < 21, negative slope, price below 9
        if cur_ema9 < cur_ema21 and slope < -min_slope and price < cur_ema9:
            return "BEAR"

        return "NEUTRAL"

    # ─── Component: Compression Breakout ──────────────────────────

    def _check_breakout(self, bars: pd.DataFrame, price: float) -> str:
        """
        Compression → Expansion Breakout detector.

        Detects when price breaks out of a consolidation range.
        This is a LEADING signal (the breakout IS the move starting)
        unlike candle momentum which CONFIRMS a past move.

        Logic:
          1. Look at prior 10 bars (consolidation period)
          2. If total range < 2.5× average bar range → compressed
          3. If current bar BREAKS above/below compression range → signal
          4. Must be fresh (previous bar was still within range)

        Professional basis: Bollinger squeeze, NR7/NR4 patterns,
        Keltner Channel compression — all variations of this concept.
        """
        lookback = max(5, 10 // self.bar_minutes)
        if len(bars) < lookback + 3:
            return "NEUTRAL"

        # Consolidation = bars before the last 2
        consol_bars = bars.iloc[-(lookback + 2):-2]
        if len(consol_bars) < 3:
            return "NEUTRAL"

        consol_high = consol_bars["high"].max()
        consol_low = consol_bars["low"].min()
        consol_range = consol_high - consol_low

        # Average bar range within consolidation
        avg_bar_range = (consol_bars["high"] - consol_bars["low"]).mean()
        if avg_bar_range <= 0:
            return "NEUTRAL"

        # Is range compressed? (total < 2.5× average bar range)
        if consol_range > avg_bar_range * 2.5:
            return "NEUTRAL"  # Not compressed — normal volatility

        # Check for FRESH breakout (last bar broke, prior was inside)
        last_close = bars["close"].iloc[-1]
        prev_close = bars["close"].iloc[-2]

        # Bullish breakout: close above consolidation high (fresh)
        if last_close > consol_high and prev_close <= consol_high:
            return "BULL"

        # Bearish breakdown: close below consolidation low (fresh)
        if last_close < consol_low and prev_close >= consol_low:
            return "BEAR"

        return "NEUTRAL"

    # ─── Component: Volume ───────────────────────────────────────

    def _check_volume(self, bars: pd.DataFrame) -> str:
        """
        Above-average volume confirming direction.

        BULL: Recent volume > 1.5x average AND net price direction is up
        BEAR: Recent volume > 1.5x average AND net price direction is down
        NEUTRAL: Volume normal or doesn't confirm direction
        """
        min_bars = self.vol_lookback + self.vol_recent + 5
        if len(bars) < min_bars or "volume" not in bars.columns:
            return "NEUTRAL"

        # Average volume (20 bars, offset by 5 to not include recent)
        avg_vol = bars["volume"].iloc[-(self.vol_lookback + 5):-5].mean()
        if avg_vol <= 0:
            return "NEUTRAL"

        # Recent volume
        recent_vol = bars["volume"].iloc[-self.vol_recent:].mean()
        vol_ratio = recent_vol / avg_vol

        if vol_ratio < self.cfg.volume_surge_mult:
            return "NEUTRAL"

        # Direction from recent price action
        net_move = bars["close"].iloc[-1] - bars["close"].iloc[-self.vol_recent - 1]

        if net_move > 0:
            return "BULL"
        elif net_move < 0:
            return "BEAR"
        return "NEUTRAL"

    # ─── Component: ORB Context ──────────────────────────────────

    def _check_orb_context(self, bars: pd.DataFrame, price: float) -> str:
        """
        Opening Range Breakout/Breakdown context.

        Only active in the first 60 minutes after open.

        BULL: Price above the 15-min ORB high (breakout)
        BEAR: Price below the 15-min ORB low (breakdown)
        NEUTRAL: Within ORB range or after first hour
        """
        if len(bars) < self.orb_bar_count + 1:
            return "NEUTRAL"

        # Only in first 60 minutes (60 // bar_minutes bars)
        max_orb_bars = max(30, 60 // self.bar_minutes)
        if len(bars) > max_orb_bars:
            return "NEUTRAL"  # Past first hour, ORB context expires

        orb_bars = bars.iloc[:self.orb_bar_count]
        orb_high = orb_bars["high"].max()
        orb_low = orb_bars["low"].min()

        if price > orb_high:
            return "BULL"
        if price < orb_low:
            return "BEAR"
        return "NEUTRAL"

    # ─── Anti-Signal: Chop Gate ──────────────────────────────────

    def _is_chop(
        self, bars: pd.DataFrame, price: float,
        vwap: pd.Series, atr: float,
    ) -> bool:
        """
        Detect micro-chop (VWAP hugging + tight EMAs).

        If EMAs are tangled AND price is on VWAP, the market is
        in a low-edge chop zone. No signals should fire.
        """
        close = bars["close"]
        ema9 = close.ewm(span=self.ema_fast, adjust=False).mean()
        ema21 = close.ewm(span=self.ema_slow, adjust=False).mean()

        cur_ema9 = ema9.iloc[-1]
        cur_ema21 = ema21.iloc[-1]

        # EMAs within chop threshold of each other
        ema_gap_pct = abs(cur_ema9 - cur_ema21) / price if price > 0 else 0
        emas_tangled = ema_gap_pct < self.cfg.chop_ema_pct

        # Price near VWAP
        if vwap is not None and len(vwap) > 0 and not pd.isna(vwap.iloc[-1]):
            vwap_dist_pct = abs(price - vwap.iloc[-1]) / price if price > 0 else 0
            price_on_vwap = vwap_dist_pct < self.cfg.chop_vwap_pct
        else:
            price_on_vwap = False

        return emas_tangled and price_on_vwap

    # ─── Utilities ───────────────────────────────────────────────

    @staticmethod
    def _compute_session_vwap(bars: pd.DataFrame) -> pd.Series:
        """Compute cumulative session VWAP from bar data."""
        if "volume" not in bars.columns:
            return pd.Series(dtype=float)

        typical_price = (bars["high"] + bars["low"] + bars["close"]) / 3
        cum_tp_vol = (typical_price * bars["volume"]).cumsum()
        cum_vol = bars["volume"].cumsum()
        return cum_tp_vol / cum_vol.replace(0, np.nan)

    @staticmethod
    def compute_atr(bars: pd.DataFrame, period: int = 15) -> float:
        """
        Compute current ATR (Average True Range) from bar data.

        Uses standard True Range formula:
          TR = max(H-L, |H-prev_close|, |L-prev_close|)
          ATR = SMA(TR, period)
        """
        if len(bars) < period + 1:
            # Fallback: just use average range
            return bars["high"].sub(bars["low"]).tail(period).mean()

        high = bars["high"]
        low = bars["low"]
        prev_close = bars["close"].shift(1)

        tr = np.maximum(
            high - low,
            np.maximum(abs(high - prev_close), abs(low - prev_close)),
        )
        return tr.tail(period).mean()


# ─────────────────────────────────────────────────────────────────
# Exit Engine — Price-Based (the critical difference)
# ─────────────────────────────────────────────────────────────────

class ScalpExitEngine:
    """
    Manages exits based on UNDERLYING PRICE movement, not option premium.

    This is THE critical difference from the old strategy.

    Old system: "Option dropped 50% → stop loss"
      Problem: Option drops 50% from theta alone with zero adverse move.
      Result: 85% stop-loss rate, -88% total return.

    New system: "Underlying moved 3×ATR against entry → stop loss"
      This measures whether the TRADE THESIS is wrong.
      Theta is irrelevant to the exit decision.

    Exit hierarchy (checked each bar using HIGH/LOW for realism):
      1. Hard stop: underlying moves stop_atr_mult × ATR against → exit
      2. Profit target: underlying moves target_atr_mult × ATR in favor → exit
      3. Trailing: after activation, trail by distance from best
      4. Time stop: no meaningful move in N minutes → scratch
      5. Max hold: absolute time limit → exit
      6. EOD: close before market close

    Important: When both stop and target are hit within the same bar
    (bar spans both levels), we conservatively assume the STOP hit first.
    """

    def __init__(self, config: ScalpConfig):
        self.cfg = config

    def check_exit(
        self,
        pos: ScalpPosition,
        bar_high: float,
        bar_low: float,
        bar_close: float,
        current_time: datetime,
        minutes_to_close: float,
    ) -> Optional[tuple]:
        """
        Check all exit conditions for a scalp position.

        Uses bar HIGH and LOW (not just close) for realistic intrabar
        stop/target detection.

        Args:
            pos: Open position
            bar_high: Current bar's high price
            bar_low: Current bar's low price
            bar_close: Current bar's close price
            current_time: Current timestamp
            minutes_to_close: Minutes until market close

        Returns:
            Tuple of (exit_reason, exit_underlying_price) or None
        """
        entry = pos.entry_underlying
        stop = pos.stop_price
        target = pos.target_price
        atr = pos.atr_at_entry

        # ── Update best favorable underlying ─────────────────────
        if pos.direction == "CALL":
            pos.best_favorable_underlying = max(pos.best_favorable_underlying, bar_high)
        else:
            pos.best_favorable_underlying = min(pos.best_favorable_underlying, bar_low)

        # ── 1. Hard Stop (using bar extremes) ────────────────────
        if pos.direction == "CALL":
            stop_hit = bar_low <= stop
            target_hit = bar_high >= target
        else:  # PUT
            stop_hit = bar_high >= stop
            target_hit = bar_low <= target

        # Conservative: if both could have triggered, assume stop first
        if stop_hit and target_hit:
            return ("STOP_LOSS", stop)

        if stop_hit:
            return ("STOP_LOSS", stop)

        # ── 2. Profit Target ─────────────────────────────────────
        if target_hit:
            return ("PROFIT_TARGET", target)

        # ── 3. Trailing Stop ─────────────────────────────────────
        trail_activation = atr * self.cfg.trailing_activation_atr
        trail_dist = atr * self.cfg.trailing_distance_atr

        if pos.direction == "CALL":
            favorable_move = pos.best_favorable_underlying - entry
            if favorable_move >= trail_activation:
                trail_level = pos.best_favorable_underlying - trail_dist
                if bar_low <= trail_level:
                    return ("TRAILING_STOP", trail_level)
        else:
            favorable_move = entry - pos.best_favorable_underlying
            if favorable_move >= trail_activation:
                trail_level = pos.best_favorable_underlying + trail_dist
                if bar_high >= trail_level:
                    return ("TRAILING_STOP", trail_level)

        # ── 4. Time Stop (no movement) ───────────────────────────
        hold_seconds = (current_time - pos.entry_time).total_seconds()
        hold_minutes = hold_seconds / 60

        if hold_minutes >= self.cfg.time_stop_minutes:
            if pos.direction == "CALL":
                move = bar_close - entry
            else:
                move = entry - bar_close

            threshold = atr * self.cfg.time_stop_atr_mult
            if abs(move) <= threshold:
                return ("TIME_STOP", bar_close)

        # ── 5. Max Hold ──────────────────────────────────────────
        if hold_minutes >= self.cfg.max_hold_minutes:
            return ("MAX_HOLD", bar_close)

        # ── 6. EOD Close ─────────────────────────────────────────
        if minutes_to_close <= self.cfg.eod_exit_minutes:
            return ("EOD_CLOSE", bar_close)

        return None


# ─────────────────────────────────────────────────────────────────
# Scalp Scanner — Live Trading Orchestrator
# ─────────────────────────────────────────────────────────────────

class ScalpScanner:
    """
    Orchestrates live 0DTE gamma scalping.

    Lifecycle (called from LiveTradingLoop):
      1. scan()       → Evaluate confirmation signals
      2. select_atm() → Find ATM strike from live chain
      3. size()       → Position size within risk budget
      4. execute()    → Place order via OrderExecutor
      5. check_exits() → Monitor with price-based exits

    Risk discipline:
      • Max 3 trades per day
      • No re-entry in same direction after stop
      • Daily loss limit stops all trading
      • Only trade in defined time windows
      • Cooldown between trades
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
        self.scalp_cfg = config.scalp
        self.account_size = account_size

        self.signal_engine = SignalEngine(self.scalp_cfg)
        self.exit_engine = ScalpExitEngine(self.scalp_cfg)

        # State
        self.open_position: Optional[ScalpPosition] = None  # Only 1 at a time
        self.daily_pnl: float = 0.0
        self.trades_today: int = 0
        self.stopped_directions: set = set()  # Directions stopped out today
        self.cooldown_remaining: int = 0

    # ─── Time Window Check ───────────────────────────────────────

    def in_time_window(self, minutes_since_open: float) -> bool:
        """Check if current time is within a valid trading window."""
        cfg = self.scalp_cfg
        w1 = cfg.window_1_start <= minutes_since_open <= cfg.window_1_end
        w2 = cfg.window_2_start <= minutes_since_open <= cfg.window_2_end

        if cfg.enable_midday:
            mid = cfg.window_1_end < minutes_since_open < cfg.window_2_start
            return w1 or w2 or mid

        return w1 or w2

    # ─── Scan ────────────────────────────────────────────────────

    def scan(self, ticker: str, minutes_since_open: float = 0) -> Optional[ScalpSignal]:
        """
        Scan for a confirmed directional signal.
        Returns ScalpSignal if conditions met, None otherwise.
        """
        # Pre-checks
        if self.open_position is not None:
            return None
        if self.cooldown_remaining > 0:
            self.cooldown_remaining -= 1
            return None
        if self.trades_today >= self.scalp_cfg.max_trades_per_day:
            return None
        if self.daily_pnl <= -self.scalp_cfg.daily_loss_limit:
            return None
        if not self.in_time_window(minutes_since_open):
            return None

        try:
            bars = self.provider.get_historical_bars(
                ticker, days=1, interval="1m"
            )
            if bars is None or len(bars) < 30:
                return None

            price = float(bars["close"].iloc[-1])
            signal = self.signal_engine.evaluate(bars, price, ticker)

            if signal and signal.direction not in self.stopped_directions:
                logger.info(
                    f"Scalp signal: {signal.direction} on {ticker} "
                    f"({', '.join(signal.confirmations)}) conf={signal.confidence:.0%}"
                )
                return signal

        except Exception as e:
            logger.warning(f"Scalp scan failed for {ticker}: {e}")

        return None

    # ─── Strike Selection ────────────────────────────────────────

    def select_atm_strike(self, signal: ScalpSignal) -> Optional[Dict]:
        """
        Select ATM/near-ATM strike from live options chain.

        Key difference from old strategy: we want HIGH DELTA (0.40-0.55),
        not low delta OTM lottery tickets. We sort by proximity to ATM.
        """
        if not self.provider:
            return None

        ticker = signal.ticker
        right = "C" if signal.direction == "CALL" else "P"
        profile = get_ticker_profile(ticker)
        premium_scale = profile.premium_scale

        try:
            chain = self.provider.get_options_chain(
                ticker,
                expiry=date.today().strftime("%Y%m%d"),
                strikes_around_atm=10,
            )
        except Exception as e:
            logger.warning(f"Failed to get chain: {e}")
            return None

        if not chain:
            return None

        options = [o for o in chain if o.get("right") == right]
        if not options:
            return None

        candidates = []
        for opt in options:
            delta = abs(opt.get("delta", 0))
            ask = opt.get("ask", 0)
            bid = opt.get("bid", 0)
            mid = opt.get("mid", (bid + ask) / 2 if bid and ask else 0)

            # ATM zone: delta 0.40-0.55
            if delta < self.scalp_cfg.target_delta_min:
                continue
            if delta > self.scalp_cfg.target_delta_max:
                continue

            # Premium limits (scaled for SPX)
            min_prem = self.scalp_cfg.min_premium * premium_scale
            max_prem = self.scalp_cfg.max_premium * premium_scale
            if ask <= 0 or ask < min_prem or ask > max_prem:
                continue

            # Reasonable spread (< 30% of mid for ATM)
            if mid > 0 and (ask - bid) / mid > 0.30:
                continue

            candidates.append({
                "strike": opt["strike"],
                "right": right,
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "delta": delta,
                "gamma": opt.get("gamma", 0),
                "iv": opt.get("iv", 0),
            })

        if not candidates:
            return None

        # Sort by closeness to ATM (highest delta = closest to ATM)
        candidates.sort(key=lambda c: abs(c["delta"] - 0.50))
        best = candidates[0]

        logger.info(
            f"ATM strike selected: {ticker} {best['strike']}{best['right']} "
            f"ask=${best['ask']:.2f} Δ={best['delta']:.3f} Γ={best['gamma']:.4f}"
        )
        return best

    # ─── Position Sizing ─────────────────────────────────────────

    def size_position(self, ask_price: float, atr: float) -> int:
        """
        Size based on dollar risk, not premium cost.

        Risk per contract = stop_distance × delta × 100
        Max contracts = max_risk_per_trade / risk_per_contract
        """
        if ask_price <= 0 or atr <= 0:
            return 0

        stop_dist = atr * self.scalp_cfg.stop_atr_mult
        # Approximate risk: delta ~0.50 × stop_dist × 100 shares
        risk_per_contract = 0.50 * stop_dist * 100

        if risk_per_contract <= 0:
            return 0

        max_by_risk = int(self.scalp_cfg.max_risk_per_trade / risk_per_contract)
        max_by_config = self.scalp_cfg.max_contracts
        max_by_budget = int(self.account_size * 0.05 / (ask_price * 100)) if ask_price > 0 else 0

        num = min(max_by_risk, max_by_config, max_by_budget)
        return max(1, num) if num >= 1 else 0

    # ─── Execute ─────────────────────────────────────────────────

    def execute_scalp(
        self,
        signal: ScalpSignal,
        strike_info: Dict,
        dry_run: bool = False,
    ) -> Optional[ScalpPosition]:
        """Execute a gamma scalp entry."""
        ask = strike_info["ask"]
        num = self.size_position(ask, signal.atr)
        if num <= 0:
            return None

        right = "C" if signal.direction == "CALL" else "P"
        total_cost = ask * num * 100

        # Compute exit levels
        stop_dist = signal.atr * self.scalp_cfg.stop_atr_mult
        target_dist = signal.atr * self.scalp_cfg.profit_target_atr_mult

        if signal.direction == "CALL":
            stop_price = signal.entry_underlying - stop_dist
            target_price = signal.entry_underlying + target_dist
        else:
            stop_price = signal.entry_underlying + stop_dist
            target_price = signal.entry_underlying - target_dist

        print(f"\n  ⚡ GAMMA SCALP ENTRY:")
        print(f"    Signal:     {' + '.join(signal.confirmations)} ({signal.confidence:.0%})")
        print(f"    Direction:  BUY {signal.direction}")
        print(f"    Strike:     {signal.ticker} {strike_info['strike']}{right} (Δ={strike_info['delta']:.3f})")
        print(f"    ATR:        ${signal.atr:.3f}")
        print(f"    Stop:       ${stop_price:.2f} ({self.scalp_cfg.stop_atr_mult}×ATR = ${stop_dist:.2f})")
        print(f"    Target:     ${target_price:.2f} ({self.scalp_cfg.profit_target_atr_mult}×ATR = ${target_dist:.2f})")
        print(f"    Ask:        ${ask:.2f}/contract × {num}")
        print(f"    Total:      ${total_cost:.2f}")

        if dry_run:
            print(f"  🧪 DRY RUN")
            return None

        fill = self.executor.buy_option(
            ticker=signal.ticker,
            expiry=date.today().strftime("%Y%m%d"),
            strike=strike_info["strike"],
            right=right,
            num_contracts=num,
            limit_price=ask,
        )

        if fill.status.value == "FILLED":
            entry_price = abs(fill.avg_fill_price)
            pos = ScalpPosition(
                ticker=signal.ticker,
                strike=strike_info["strike"],
                right=right,
                direction=signal.direction,
                expiry=date.today().strftime("%Y%m%d"),
                confirmations=signal.confirmations,
                confidence=signal.confidence,
                entry_time=datetime.now(),
                entry_underlying=signal.entry_underlying,
                entry_premium=entry_price,
                num_contracts=fill.num_filled,
                atr_at_entry=signal.atr,
                stop_price=stop_price,
                target_price=target_price,
                best_favorable_underlying=signal.entry_underlying,
            )
            self.open_position = pos
            self.trades_today += 1
            print(f"  ✅ SCALP FILLED: {fill.num_filled}x @ ${entry_price:.2f}")
            return pos

        print(f"  ❌ Not filled: {fill.status.value}")
        return None

    # ─── Monitor Exits ───────────────────────────────────────────

    def check_exits(self, minutes_to_close: int) -> Optional[ScalpPosition]:
        """Check open position for exit conditions."""
        if not self.open_position or not self.open_position.is_open:
            return None

        pos = self.open_position

        # Get current underlying price
        try:
            bars = self.provider.get_historical_bars(pos.ticker, days=1, interval="1m")
            if bars is None or len(bars) < 1:
                return None

            last_bar = bars.iloc[-1]
            bar_high = float(last_bar["high"])
            bar_low = float(last_bar["low"])
            bar_close = float(last_bar["close"])
            current_time = bars.index[-1]

        except Exception as e:
            logger.warning(f"Failed to get data for exit check: {e}")
            return None

        result = self.exit_engine.check_exit(
            pos, bar_high, bar_low, bar_close, current_time, minutes_to_close,
        )

        if result:
            exit_reason, exit_underlying = result

            # Get current option price for P&L
            try:
                chain = self.provider.get_options_chain(
                    pos.ticker, expiry=pos.expiry, strikes_around_atm=10,
                )
                opt = next(
                    (o for o in chain
                     if abs(o["strike"] - pos.strike) < 0.01 and o["right"] == pos.right),
                    None,
                )
                exit_premium = opt.get("bid", 0) if opt else 0
            except Exception:
                exit_premium = 0

            pnl = (exit_premium - pos.entry_premium) * pos.num_contracts * 100
            pos.is_open = False
            pos.exit_time = current_time
            pos.exit_underlying = exit_underlying
            pos.exit_premium = exit_premium
            pos.exit_reason = exit_reason
            pos.total_pnl = round(pnl, 2)

            self.daily_pnl += pnl
            self.cooldown_remaining = self.scalp_cfg.cooldown_bars

            if exit_reason == "STOP_LOSS":
                self.stopped_directions.add(pos.direction)

            self.open_position = None

            mult = exit_premium / pos.entry_premium if pos.entry_premium > 0 else 0
            print(f"  ⚡ SCALP EXIT: {pos.ticker} {pos.strike}{pos.right} "
                  f"→ {exit_reason} | {mult:.2f}x | P&L: ${pnl:+.2f}")

            return pos

        return None

    # ─── Status ──────────────────────────────────────────────────

    def print_status(self):
        """Print current scalp scanner status."""
        print(f"\n  ⚡ GAMMA SCALP STATUS")
        print(f"  {'─' * 50}")
        print(f"  Daily P&L:  ${self.daily_pnl:+.2f}")
        print(f"  Trades:     {self.trades_today}/{self.scalp_cfg.max_trades_per_day}")
        print(f"  Stopped:    {', '.join(self.stopped_directions) or 'none'}")
        print(f"  Cooldown:   {self.cooldown_remaining} bars")

        if self.open_position:
            pos = self.open_position
            print(f"  Position:   {pos.direction} {pos.ticker} {pos.strike}{pos.right}")
            print(f"    Entry:    ${pos.entry_underlying:.2f} (premium ${pos.entry_premium:.2f})")
            print(f"    Stop:     ${pos.stop_price:.2f}")
            print(f"    Target:   ${pos.target_price:.2f}")
            print(f"    Signals:  {', '.join(pos.confirmations)}")

    def reset_daily(self):
        """Reset daily counters."""
        self.daily_pnl = 0.0
        self.trades_today = 0
        self.stopped_directions.clear()
        self.cooldown_remaining = 0
        self.open_position = None

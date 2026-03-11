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

from .config import EngineConfig, ScalpConfig, MeanReversionConfig, ORBConfig, RangeFadeConfig, VWAPMRConfig, get_ticker_profile, TickerProfile

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
        prev_day_high: float = None,
        prev_day_low: float = None,
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

        # 6. Consecutive Candle Momentum
        cm = self._check_candle_momentum(bars, price)
        if cm == "BULL":
            bullish.append("CANDLE_MOM")
        elif cm == "BEAR":
            bearish.append("CANDLE_MOM")

        # 7. Range Breakout (Donchian channel — works all day)
        rb = self._check_range_breakout(bars, price)
        if rb == "BULL":
            bullish.append("RANGE_BRK")
        elif rb == "BEAR":
            bearish.append("RANGE_BRK")

        # 8. Previous Day High/Low
        ph = self._check_prev_day_hl(bars, price, prev_day_high, prev_day_low)
        if ph == "BULL":
            bullish.append("PREV_HL")
        elif ph == "BEAR":
            bearish.append("PREV_HL")

        # ── Require min_confirmations to agree ───────────────────
        # MANDATORY: VOLUME + VWAP must both be present for any signal.
        # Data shows combos without these are noise (negative WR drag).
        n_components = 8  # Total signal components
        min_conf = self.cfg.min_confirmations

        for direction, sigs in [("CALL", bullish), ("PUT", bearish)]:
            if len(sigs) < min_conf:
                continue
            if "VOLUME" not in sigs or "VWAP" not in sigs:
                continue
            return ScalpSignal(
                direction=direction,
                confirmations=sigs,
                confidence=len(sigs) / n_components,
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

        Fires in TWO modes:
          1. Cross mode: Price crossed VWAP recently (was on other side
             for 3+ bars, now on this side for 2+). Catches reversals.
          2. Trend mode: Price has been on one side for 8+ consecutive
             bars with strong distance (>0.1%). Catches trend days where
             no cross ever happens — the biggest intraday moves.
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

        # MODE 1: VWAP Cross (reclaim/rejection after 5+ bars on other side)
        if last_2_above and below_count >= 5:
            return "BULL"
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

    # ─── Component: Consecutive Candle Momentum ──────────────────

    def _check_candle_momentum(self, bars: pd.DataFrame, price: float) -> str:
        """
        Consecutive candle momentum — directional conviction.

        BULL: 3+ consecutive bars with close > open (bullish bodies)
              and each body ratio > 40% (not dojis)
        BEAR: 3+ consecutive bars with close < open (bearish bodies)
        NEUTRAL: Mixed candles or weak bodies

        Professional basis: "Three white soldiers" / "Three black crows"
        patterns. Consecutive strong-body candles show institutional
        conviction — the market is trending, not just spiking.
        """
        n_bars = self.cfg.candle_mom_bars  # default 3
        min_body_pct = self.cfg.candle_mom_body_pct  # default 0.40
        if len(bars) < n_bars + 1:
            return "NEUTRAL"

        bull_count = 0
        bear_count = 0
        for j in range(n_bars):
            bar_slice = bars.iloc[-(j + 1)]
            bar_open = bar_slice["open"]
            bar_close = bar_slice["close"]
            bar_range = bar_slice["high"] - bar_slice["low"]
            if bar_range <= 0:
                return "NEUTRAL"
            body = bar_close - bar_open
            body_ratio = abs(body) / bar_range
            if body_ratio < min_body_pct:
                break  # Weak candle breaks the streak
            if body > 0:
                bull_count += 1
            elif body < 0:
                bear_count += 1
            else:
                break

        if bull_count >= n_bars:
            return "BULL"
        if bear_count >= n_bars:
            return "BEAR"
        return "NEUTRAL"

    # ─── Component: Range Breakout (Donchian Channel) ────────────

    def _check_range_breakout(self, bars: pd.DataFrame, price: float) -> str:
        """
        Donchian Channel breakout — 20-bar high/low range break.

        Complements ORB (which dies after 60 min) by providing
        a breakout signal that works ALL DAY. Detects when price
        breaks above the 20-bar high or below the 20-bar low.

        Logic:
          1. Compute 20-bar high and low (excluding current bar)
          2. Current close breaks above/below the range
          3. Previous close was inside (freshness check)

        Professional basis: Donchian channels, turtle trading rules,
        Darvas box — all variants of N-bar range breakout.
        """
        lookback = self.cfg.range_brk_lookback  # default 20
        if len(bars) < lookback + 3:
            return "NEUTRAL"

        # Range from prior bars (not including last 1)
        range_bars = bars.iloc[-(lookback + 1):-1]
        range_high = range_bars["high"].max()
        range_low = range_bars["low"].min()

        last_close = bars["close"].iloc[-1]
        prev_close = bars["close"].iloc[-2]

        # Bullish breakout: fresh break above range high
        if last_close > range_high and prev_close <= range_high:
            return "BULL"
        # Bearish breakdown: fresh break below range low
        if last_close < range_low and prev_close >= range_low:
            return "BEAR"
        return "NEUTRAL"

    # ─── Component: Previous Day High/Low ────────────────────────

    def _check_prev_day_hl(
        self, bars: pd.DataFrame, price: float,
        prev_day_high: float = None, prev_day_low: float = None,
    ) -> str:
        """
        Previous day's high/low breakout — cross-day reference level.

        Yesterday's high and low are among the most-watched levels
        by institutional traders. Breaking above/below with conviction
        signals genuine directional commitment beyond noise.

        BULL: Price breaks above yesterday's high (fresh)
        BEAR: Price breaks below yesterday's low (fresh)
        NEUTRAL: Inside yesterday's range or no prior data

        Professional basis: Every institutional desk marks prior
        day H/L on charts. These are key support/resistance levels.
        """
        if prev_day_high is None or prev_day_low is None:
            return "NEUTRAL"
        if len(bars) < 3:
            return "NEUTRAL"

        last_close = bars["close"].iloc[-1]
        prev_close = bars["close"].iloc[-2]

        if last_close > prev_day_high and prev_close <= prev_day_high:
            return "BULL"
        if last_close < prev_day_low and prev_close >= prev_day_low:
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

    # ─── Fast Precomputation for Backtesting ────────────────────

    def precompute_day_indicators(self, day_bars: pd.DataFrame) -> dict:
        """
        Pre-compute ALL indicators for a full day at once.

        Returns a dict of numpy arrays / series indexed by bar position.
        Used by evaluate_fast() to avoid O(n²) recomputation.
        """
        close = day_bars["close"].values
        high = day_bars["high"].values
        low = day_bars["low"].values
        open_arr = day_bars["open"].values if "open" in day_bars.columns else np.zeros(len(day_bars))
        volume = day_bars["volume"].values if "volume" in day_bars.columns else np.zeros(len(day_bars))
        n = len(day_bars)

        # ── VWAP (cumulative) ────────────────────────────────────
        typical = (high + low + close) / 3.0
        cum_tp_vol = np.cumsum(typical * volume)
        cum_vol = np.cumsum(volume)
        with np.errstate(divide='ignore', invalid='ignore'):
            vwap = np.where(cum_vol > 0, cum_tp_vol / cum_vol, np.nan)

        # ── EMA 9/21 ────────────────────────────────────────────
        close_series = day_bars["close"]
        ema9 = close_series.ewm(span=self.ema_fast, adjust=False).mean().values
        ema21 = close_series.ewm(span=self.ema_slow, adjust=False).mean().values

        # ── ATR (rolling) ────────────────────────────────────────
        prev_c = np.empty(n)
        prev_c[0] = close[0]
        prev_c[1:] = close[:-1]
        tr = np.maximum(
            high - low,
            np.maximum(np.abs(high - prev_c), np.abs(low - prev_c)),
        )
        # Rolling mean ATR
        atr_arr = np.full(n, np.nan)
        atr_period = self.atr_period
        for i in range(min(3, n), n):
            start = max(0, i - atr_period + 1)
            atr_arr[i] = tr[start:i+1].mean()

        # ── Volume rolling average ───────────────────────────────
        # Slow path uses bars_so_far[:i+1] with negative indexing:
        #   avg_vol = bars.iloc[-(vl+5):-5].mean()  → bars[i+1-vl-5:i+1-5] = bars[i-vl-4:i-4]
        #   recent_vol = bars.iloc[-vr:].mean()      → bars[i+1-vr:i+1] = bars[i-vr+1:i+1]
        # We pre-compute matching indices for each bar position.
        vol_avg = np.full(n, np.nan)
        vol_recent_arr = np.full(n, np.nan)
        vl = self.vol_lookback
        vr = self.vol_recent
        for i in range(vl + vr + 5, n):
            # Match: bars_so_far has i+1 bars, avg=iloc[-(vl+5):-5]
            avg_start = i + 1 - vl - 5   # i-vl-4
            avg_end = i + 1 - 5          # i-4
            vol_avg[i] = volume[avg_start:avg_end].mean()
            # Match: recent=iloc[-vr:]
            rec_start = i + 1 - vr       # i-vr+1
            rec_end = i + 1              # i+1 (exclusive)
            vol_recent_arr[i] = volume[rec_start:rec_end].mean()

        # ── ORB (first 15 min) ───────────────────────────────────
        orb_end = min(self.orb_bar_count, n)
        orb_high = high[:orb_end].max()
        orb_low = low[:orb_end].min()
        max_orb_bars = max(30, 60 // self.bar_minutes)

        # ── Rolling Realized Volatility (for IV discount filter) ──
        # Annualized realized vol from recent returns.
        # Compare against day_iv to detect "cheap" options.
        rv_lookback = self.cfg.rv_lookback_bars if hasattr(self.cfg, 'rv_lookback_bars') else 20
        bars_per_day = 390 // (self.bar_minutes or 1)
        rv_annual_factor = math.sqrt(bars_per_day * 252)
        rv_arr = np.full(n, np.nan)
        log_returns = np.empty(n)
        log_returns[0] = 0.0
        for j in range(1, n):
            if close[j - 1] > 0 and close[j] > 0:
                log_returns[j] = math.log(close[j] / close[j - 1])
            else:
                log_returns[j] = 0.0
        for j in range(rv_lookback, n):
            window_rets = log_returns[j - rv_lookback + 1:j + 1]
            bar_std = np.std(window_rets, ddof=1)
            rv_arr[j] = bar_std * rv_annual_factor

        return {
            'close': close,
            'high': high,
            'low': low,
            'open': open_arr,
            'volume': volume,
            'vwap': vwap,
            'ema9': ema9,
            'ema21': ema21,
            'atr': atr_arr,
            'vol_avg': vol_avg,
            'vol_recent': vol_recent_arr,
            'orb_high': orb_high,
            'orb_low': orb_low,
            'max_orb_bars': max_orb_bars,
            'rv': rv_arr,
            'n': n,
            # Set by backtester before evaluate_fast() calls:
            'prev_day_high': None,
            'prev_day_low': None,
        }

    def evaluate_fast(
        self,
        precomp: dict,
        idx: int,
        price: float,
        ticker: str = "",
        bars_index=None,
    ) -> Optional[ScalpSignal]:
        """
        Fast signal evaluation using pre-computed indicators.

        Same logic as evaluate() but uses O(1) lookups instead of
        recomputing from scratch.
        """
        n = precomp['n']
        if idx < max(30, self.ema_slow + 10):
            return None

        close = precomp['close']
        high = precomp['high']
        low = precomp['low']
        volume = precomp['volume']
        vwap = precomp['vwap']
        ema9 = precomp['ema9']
        ema21 = precomp['ema21']
        atr_val = precomp['atr'][idx]

        if np.isnan(atr_val) or atr_val <= 0 or atr_val < self.cfg.min_atr:
            return None

        # ── Anti-Signal Gate: Chop Filter ────────────────────────
        cur_ema9 = ema9[idx]
        cur_ema21 = ema21[idx]
        cur_vwap = vwap[idx]

        ema_gap_pct = abs(cur_ema9 - cur_ema21) / price if price > 0 else 0
        emas_tangled = ema_gap_pct < self.cfg.chop_ema_pct

        if not np.isnan(cur_vwap) and cur_vwap > 0:
            vwap_dist_pct = abs(price - cur_vwap) / price if price > 0 else 0
            price_on_vwap = vwap_dist_pct < self.cfg.chop_vwap_pct
        else:
            price_on_vwap = False

        if emas_tangled and price_on_vwap:
            return None  # Chop gate

        # ── Evaluate Components ──────────────────────────────────
        bullish: List[str] = []
        bearish: List[str] = []

        # 1. VWAP
        v = self._check_vwap_fast(close, vwap, idx, price)
        if v == "BULL":
            bullish.append("VWAP")
        elif v == "BEAR":
            bearish.append("VWAP")

        # 2. EMA Trend
        e = self._check_ema_trend_fast(ema9, ema21, idx, price)
        if e == "BULL":
            bullish.append("EMA")
        elif e == "BEAR":
            bearish.append("EMA")

        # 3. Breakout
        b = self._check_breakout_fast(close, high, low, idx, price)
        if b == "BULL":
            bullish.append("BREAKOUT")
        elif b == "BEAR":
            bearish.append("BREAKOUT")

        # 4. Volume
        vol = self._check_volume_fast(precomp, close, idx)
        if vol == "BULL":
            bullish.append("VOLUME")
        elif vol == "BEAR":
            bearish.append("VOLUME")

        # 5. ORB Context
        orb = self._check_orb_fast(precomp, idx, price)
        if orb == "BULL":
            bullish.append("ORB")
        elif orb == "BEAR":
            bearish.append("ORB")

        # 6. Consecutive Candle Momentum
        cm = self._check_candle_momentum_fast(precomp, close, high, low, idx)
        if cm == "BULL":
            bullish.append("CANDLE_MOM")
        elif cm == "BEAR":
            bearish.append("CANDLE_MOM")

        # 7. Range Breakout (Donchian — works all day)
        rb = self._check_range_breakout_fast(close, high, low, idx)
        if rb == "BULL":
            bullish.append("RANGE_BRK")
        elif rb == "BEAR":
            bearish.append("RANGE_BRK")

        # 8. Previous Day High/Low
        ph = self._check_prev_day_hl_fast(precomp, close, idx, price)
        if ph == "BULL":
            bullish.append("PREV_HL")
        elif ph == "BEAR":
            bearish.append("PREV_HL")

        # ── Require min_confirmations to agree ───────────────────
        # MANDATORY: VOLUME + VWAP must both be present for any signal.
        # Data shows combos without these are noise (negative WR drag).
        n_components = 8  # Total signal components
        min_conf = self.cfg.min_confirmations

        ts = bars_index[idx] if bars_index is not None and hasattr(bars_index[idx], 'hour') else datetime.now()

        for direction, sigs in [("CALL", bullish), ("PUT", bearish)]:
            if len(sigs) < min_conf:
                continue
            if "VOLUME" not in sigs or "VWAP" not in sigs:
                continue
            return ScalpSignal(
                direction=direction,
                confirmations=sigs,
                confidence=len(sigs) / n_components,
                entry_underlying=price,
                atr=atr_val,
                timestamp=ts,
                ticker=ticker,
            )

        return None

    # ─── Fast Component Checks (numpy arrays, O(1) each) ────────

    def _check_vwap_fast(self, close: np.ndarray, vwap: np.ndarray,
                         idx: int, price: float) -> str:
        if idx < 10:
            return "NEUTRAL"
        cur_vwap = vwap[idx]
        if np.isnan(cur_vwap) or cur_vwap <= 0:
            return "NEUTRAL"

        distance_pct = (price - cur_vwap) / cur_vwap
        if abs(distance_pct) < self.cfg.vwap_distance_min_pct:
            return "NEUTRAL"

        # Slow path: lookback = min(10, len(bars)-2) where len=i+1
        lookback = min(10, idx - 1)
        if lookback < 5:
            return "NEUTRAL"

        # Count bars on each side of VWAP in prior window
        # Slow: bars.iloc[-lookback-2:-2] = bars[i+1-lookback-2 : i+1-2] = bars[i-lookback-1 : i-1]
        below_count = 0
        above_count = 0
        for j in range(idx - lookback - 1, idx - 1):
            if j < 0:
                continue
            v = vwap[j]
            if np.isnan(v):
                continue
            if close[j] < v:
                below_count += 1
            elif close[j] > v:
                above_count += 1

        # Last 2 bars confirm
        last_2_above = all(
            close[idx - j] > vwap[idx - j]
            for j in range(0, 2)
            if not np.isnan(vwap[idx - j])
        )
        last_2_below = all(
            close[idx - j] < vwap[idx - j]
            for j in range(0, 2)
            if not np.isnan(vwap[idx - j])
        )

        # MODE 1: VWAP Cross (reclaim/rejection after 5+ bars on other side)
        if last_2_above and below_count >= 5:
            return "BULL"
        if last_2_below and above_count >= 5:
            return "BEAR"

        return "NEUTRAL"

    def _check_ema_trend_fast(self, ema9: np.ndarray, ema21: np.ndarray,
                              idx: int, price: float) -> str:
        if idx < 4:
            return "NEUTRAL"
        cur_ema9 = ema9[idx]
        cur_ema21 = ema21[idx]
        prev_ema9 = ema9[idx - 3]

        slope = (cur_ema9 - prev_ema9) / (3 * price) if price > 0 else 0
        min_slope = self.cfg.ema_slope_min_pct

        if cur_ema9 > cur_ema21 and slope > min_slope and price > cur_ema9:
            return "BULL"
        if cur_ema9 < cur_ema21 and slope < -min_slope and price < cur_ema9:
            return "BEAR"
        return "NEUTRAL"

    def _check_breakout_fast(self, close: np.ndarray, high: np.ndarray,
                             low: np.ndarray, idx: int, price: float) -> str:
        lookback = max(5, 10 // self.bar_minutes)
        if idx < lookback + 3:
            return "NEUTRAL"

        # Slow path: consol_bars = bars.iloc[-(lookback+2):-2]
        # With len=i+1: bars[i+1-lookback-2 : i+1-2] = bars[i-lookback-1 : i-1]
        consol_start = idx - lookback - 1
        consol_end = idx - 1  # exclusive end
        if consol_start < 0:
            return "NEUTRAL"

        consol_high = high[consol_start:consol_end].max()
        consol_low = low[consol_start:consol_end].min()
        consol_range = consol_high - consol_low

        bar_ranges = high[consol_start:consol_end] - low[consol_start:consol_end]
        avg_bar_range = bar_ranges.mean()
        if avg_bar_range <= 0:
            return "NEUTRAL"

        if consol_range > avg_bar_range * 2.5:
            return "NEUTRAL"

        last_close = close[idx]
        prev_close = close[idx - 1]

        if last_close > consol_high and prev_close <= consol_high:
            return "BULL"
        if last_close < consol_low and prev_close >= consol_low:
            return "BEAR"
        return "NEUTRAL"

    def _check_volume_fast(self, precomp: dict, close: np.ndarray,
                           idx: int) -> str:
        avg_vol = precomp['vol_avg'][idx]
        recent_vol = precomp['vol_recent'][idx]
        if np.isnan(avg_vol) or np.isnan(recent_vol) or avg_vol <= 0:
            return "NEUTRAL"

        vol_ratio = recent_vol / avg_vol
        if vol_ratio < self.cfg.volume_surge_mult:
            return "NEUTRAL"

        # net_move: slow path = close.iloc[-1] - close.iloc[-vol_recent - 1]
        # With i+1 bars: close[i] - close[i+1-vol_recent-1] = close[i] - close[i-vol_recent]
        net_move = close[idx] - close[idx - self.vol_recent]
        if net_move > 0:
            return "BULL"
        elif net_move < 0:
            return "BEAR"
        return "NEUTRAL"

    def _check_orb_fast(self, precomp: dict, idx: int, price: float) -> str:
        # Slow: len(bars) < orb_bar_count + 1 → i+1 < orb_bar_count + 1 → i < orb_bar_count
        if idx < self.orb_bar_count:
            return "NEUTRAL"
        # Slow: len(bars) > max_orb_bars → i+1 > max_orb_bars → i >= max_orb_bars
        if idx >= precomp['max_orb_bars']:
            return "NEUTRAL"

        if price > precomp['orb_high']:
            return "BULL"
        if price < precomp['orb_low']:
            return "BEAR"
        return "NEUTRAL"

    def _check_candle_momentum_fast(self, precomp: dict, close: np.ndarray,
                                     high: np.ndarray, low: np.ndarray,
                                     idx: int) -> str:
        """Fast consecutive candle momentum check using precomputed open array."""
        n_bars = self.cfg.candle_mom_bars  # default 3
        min_body_pct = self.cfg.candle_mom_body_pct  # default 0.40
        if idx < n_bars:
            return "NEUTRAL"

        open_arr = precomp['open']
        bull_count = 0
        bear_count = 0
        for j in range(n_bars):
            bar_idx = idx - j
            body = close[bar_idx] - open_arr[bar_idx]
            bar_range = high[bar_idx] - low[bar_idx]
            if bar_range <= 0:
                return "NEUTRAL"
            body_ratio = abs(body) / bar_range
            if body_ratio < min_body_pct:
                break  # Weak candle breaks the streak
            if body > 0:
                bull_count += 1
            elif body < 0:
                bear_count += 1
            else:
                break

        if bull_count >= n_bars:
            return "BULL"
        if bear_count >= n_bars:
            return "BEAR"
        return "NEUTRAL"

    def _check_range_breakout_fast(self, close: np.ndarray, high: np.ndarray,
                                    low: np.ndarray, idx: int) -> str:
        """Fast Donchian channel breakout check."""
        lookback = self.cfg.range_brk_lookback  # default 20
        if idx < lookback + 2:
            return "NEUTRAL"

        # Range from bars [idx-lookback, idx-1) — not including current bar
        range_start = idx - lookback
        range_end = idx  # exclusive (so up to idx-1)
        range_high = high[range_start:range_end].max()
        range_low = low[range_start:range_end].min()

        last_close = close[idx]
        prev_close = close[idx - 1]

        if last_close > range_high and prev_close <= range_high:
            return "BULL"
        if last_close < range_low and prev_close >= range_low:
            return "BEAR"
        return "NEUTRAL"

    def _check_prev_day_hl_fast(self, precomp: dict, close: np.ndarray,
                                 idx: int, price: float) -> str:
        """Fast previous day high/low breakout check."""
        prev_high = precomp.get('prev_day_high')
        prev_low = precomp.get('prev_day_low')
        if prev_high is None or prev_low is None:
            return "NEUTRAL"
        if idx < 2:
            return "NEUTRAL"

        prev_close = close[idx - 1]

        if price > prev_high and prev_close <= prev_high:
            return "BULL"
        if price < prev_low and prev_close >= prev_low:
            return "BEAR"
        return "NEUTRAL"

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
# Mean-Reversion Signal Engine — Fade Extremes in Chop
# ─────────────────────────────────────────────────────────────────

class MeanReversionSignalEngine:
    """
    Mean-reversion signal generation for midday chop zones.

    The OPPOSITE of the momentum SignalEngine:
      Momentum: "Price broke out, ride the trend"
      MeanRev:  "Price hit an extreme, fade it back to VWAP"

    Designed for 10:30 AM - 2:00 PM when SPX oscillates around VWAP
    in tight ranges. The momentum engine's WR drops to 34% here, but
    mean-reversion THRIVES in exactly this environment.

    Components:
      1. BB_EXTREME — Price at/beyond Bollinger Band (2σ)
      2. RSI_EXTREME — RSI overbought (>70) or oversold (<30)
      3. VWAP_REVERT — Price extended from VWAP and turning back

    Anti-Trend Gate:
      If EMA9/21 spread > 0.1%, market is trending — skip.
      Don't fade a strong trend. That's the momentum engine's job.
    """

    def __init__(self, config: MeanReversionConfig, bar_minutes: int = 1):
        self.cfg = config
        self.bar_minutes = bar_minutes
        self.atr_period = max(5, config.atr_period // bar_minutes)

    def precompute_day_indicators(self, day_bars: pd.DataFrame) -> dict:
        """
        Pre-compute Bollinger Bands, RSI, and VWAP for the full day.
        Returns arrays indexed by bar position for O(1) evaluate_fast().
        """
        close = day_bars["close"].values
        high = day_bars["high"].values
        low = day_bars["low"].values
        open_ = day_bars["open"].values
        volume = day_bars["volume"].values if "volume" in day_bars.columns else np.zeros(len(day_bars))
        n = len(day_bars)

        # ── VWAP (cumulative) ────────────────────────────────────
        typical = (high + low + close) / 3.0
        cum_tp_vol = np.cumsum(typical * volume)
        cum_vol = np.cumsum(volume)
        with np.errstate(divide='ignore', invalid='ignore'):
            vwap = np.where(cum_vol > 0, cum_tp_vol / cum_vol, np.nan)

        # ── Bollinger Bands (rolling SMA + std) ──────────────────
        bb_period = self.cfg.bb_period
        bb_std_mult = self.cfg.bb_std
        bb_mid = np.full(n, np.nan)
        bb_upper = np.full(n, np.nan)
        bb_lower = np.full(n, np.nan)
        for i in range(bb_period - 1, n):
            window = close[i - bb_period + 1:i + 1]
            mid = window.mean()
            std = window.std(ddof=1)
            bb_mid[i] = mid
            bb_upper[i] = mid + bb_std_mult * std
            bb_lower[i] = mid - bb_std_mult * std

        # ── RSI (Wilder's smoothing) ─────────────────────────────
        rsi_period = self.cfg.rsi_period
        rsi = np.full(n, np.nan)
        if n > rsi_period + 1:
            deltas = np.diff(close)
            gains = np.where(deltas > 0, deltas, 0.0)
            losses = np.where(deltas < 0, -deltas, 0.0)

            # Initial average (SMA of first rsi_period values)
            avg_gain = gains[:rsi_period].mean()
            avg_loss = losses[:rsi_period].mean()

            if avg_loss > 0:
                rs = avg_gain / avg_loss
                rsi[rsi_period] = 100.0 - 100.0 / (1.0 + rs)
            else:
                rsi[rsi_period] = 100.0

            # Wilder's smoothing for remaining
            for i in range(rsi_period, len(deltas)):
                avg_gain = (avg_gain * (rsi_period - 1) + gains[i]) / rsi_period
                avg_loss = (avg_loss * (rsi_period - 1) + losses[i]) / rsi_period
                if avg_loss > 0:
                    rs = avg_gain / avg_loss
                    rsi[i + 1] = 100.0 - 100.0 / (1.0 + rs)
                else:
                    rsi[i + 1] = 100.0

        # ── EMA 9/21 (for anti-trend gate) ───────────────────────
        close_series = day_bars["close"]
        ema9 = close_series.ewm(span=9, adjust=False).mean().values
        ema21 = close_series.ewm(span=21, adjust=False).mean().values

        # ── ATR ──────────────────────────────────────────────────
        prev_c = np.empty(n)
        prev_c[0] = close[0]
        prev_c[1:] = close[:-1]
        tr = np.maximum(
            high - low,
            np.maximum(np.abs(high - prev_c), np.abs(low - prev_c)),
        )
        atr_arr = np.full(n, np.nan)
        atr_period = self.atr_period
        for i in range(min(3, n), n):
            start = max(0, i - atr_period + 1)
            atr_arr[i] = tr[start:i + 1].mean()

        return {
            'close': close,
            'high': high,
            'low': low,
            'open': open_,
            'volume': volume,
            'vwap': vwap,
            'bb_mid': bb_mid,
            'bb_upper': bb_upper,
            'bb_lower': bb_lower,
            'rsi': rsi,
            'ema9': ema9,
            'ema21': ema21,
            'atr': atr_arr,
            'n': n,
        }

    def evaluate_fast(
        self,
        precomp: dict,
        idx: int,
        price: float,
        ticker: str = "",
        bars_index=None,
    ) -> Optional[ScalpSignal]:
        """
        Fast mean-reversion signal evaluation using pre-computed indicators.

        Returns a ScalpSignal if 2+ mean-reversion components agree.
        Direction is OPPOSITE of the extreme (fade the move).
        """
        n = precomp['n']
        if idx < max(30, self.cfg.bb_period + 5, self.cfg.rsi_period + 5):
            return None

        atr_val = precomp['atr'][idx]
        if np.isnan(atr_val) or atr_val <= 0 or atr_val < self.cfg.min_atr:
            return None

        # ── Anti-Trend Gate ──────────────────────────────────────
        ema9 = precomp['ema9'][idx]
        ema21 = precomp['ema21'][idx]
        if price > 0:
            ema_spread = abs(ema9 - ema21) / price
            if ema_spread > self.cfg.ema_trend_threshold:
                return None  # Strong trend — let momentum handle it

        # ── Evaluate Components ──────────────────────────────────
        # Mean-reversion signals: CALL when oversold, PUT when overbought
        bullish: List[str] = []   # Oversold → buy CALL
        bearish: List[str] = []   # Overbought → buy PUT

        # 1. Bollinger Band Extreme
        bb_upper = precomp['bb_upper'][idx]
        bb_lower = precomp['bb_lower'][idx]
        if not np.isnan(bb_upper) and not np.isnan(bb_lower):
            if price <= bb_lower:
                bullish.append("BB_EXTREME")
            elif price >= bb_upper:
                bearish.append("BB_EXTREME")

        # 2. RSI Extreme
        rsi_val = precomp['rsi'][idx]
        if not np.isnan(rsi_val):
            if rsi_val <= self.cfg.rsi_oversold:
                bullish.append("RSI_EXTREME")
            elif rsi_val >= self.cfg.rsi_overbought:
                bearish.append("RSI_EXTREME")

        # 3. VWAP Reversion (extended from VWAP + turning back)
        vwap_val = precomp['vwap'][idx]
        if not np.isnan(vwap_val) and vwap_val > 0:
            vwap_dist = (price - vwap_val) / vwap_val

            if abs(vwap_dist) >= self.cfg.vwap_extreme_pct:
                # Check if price is turning back toward VWAP
                close = precomp['close']
                turn_bars = self.cfg.vwap_turn_bars
                if idx >= turn_bars:
                    # Price below VWAP and turning up (last N bars closing higher)
                    if vwap_dist < 0:
                        turning_up = all(
                            close[idx - j] > close[idx - j - 1]
                            for j in range(turn_bars)
                            if idx - j - 1 >= 0
                        )
                        if turning_up:
                            bullish.append("VWAP_REVERT")

                    # Price above VWAP and turning down
                    elif vwap_dist > 0:
                        turning_down = all(
                            close[idx - j] < close[idx - j - 1]
                            for j in range(turn_bars)
                            if idx - j - 1 >= 0
                        )
                        if turning_down:
                            bearish.append("VWAP_REVERT")

        # ── Require min_confirmations to agree ───────────────────
        min_conf = self.cfg.min_confirmations

        ts = bars_index[idx] if bars_index is not None and hasattr(bars_index[idx], 'hour') else datetime.now()

        if len(bullish) >= min_conf:
            return ScalpSignal(
                direction="CALL",
                confirmations=bullish,
                confidence=len(bullish) / 3.0,
                entry_underlying=price,
                atr=atr_val,
                timestamp=ts,
                ticker=ticker,
            )

        if len(bearish) >= min_conf:
            return ScalpSignal(
                direction="PUT",
                confirmations=bearish,
                confidence=len(bearish) / 3.0,
                entry_underlying=price,
                atr=atr_val,
                timestamp=ts,
                ticker=ticker,
            )

        return None


# ─────────────────────────────────────────────────────────────────
# Mean-Reversion Exit Engine — Quick Captures
# ─────────────────────────────────────────────────────────────────

class MeanReversionExitEngine:
    """
    Exit engine for mean-reversion positions.

    Key differences from momentum ScalpExitEngine:
      • Tighter stops (2.0×ATR vs 3.0×ATR) — mean-rev thesis fails faster
      • Quicker targets (2.0×ATR vs 3.5×ATR) — don't expect big runs
      • Earlier trailing (1.5×ATR activation vs 3.5×ATR)
      • Shorter time stop (15 min vs 30 min)
      • Shorter max hold (30 min vs 60 min)

    Mean-reversion trades should work quickly. If price doesn't snap
    back to VWAP within 15 minutes, the thesis is probably wrong.
    """

    def __init__(self, config: MeanReversionConfig):
        self.cfg = config

    def check_exit(
        self,
        pos,  # SimScalpPosition or ScalpPosition
        bar_high: float,
        bar_low: float,
        bar_close: float,
        current_time: datetime,
        minutes_to_close: float,
    ) -> Optional[tuple]:
        """Check exit conditions for mean-reversion position."""
        entry = pos.entry_underlying
        stop = pos.stop_price
        target = pos.target_price
        atr = pos.atr_at_entry

        # ── Update best favorable underlying ─────────────────────
        if pos.direction == "CALL":
            pos.best_favorable_underlying = max(pos.best_favorable_underlying, bar_high)
        else:
            pos.best_favorable_underlying = min(pos.best_favorable_underlying, bar_low)

        # ── 1. Hard Stop ─────────────────────────────────────────
        if pos.direction == "CALL":
            stop_hit = bar_low <= stop
            target_hit = bar_high >= target
        else:
            stop_hit = bar_high >= stop
            target_hit = bar_low <= target

        if stop_hit and target_hit:
            return ("MR_STOP_LOSS", stop)

        if stop_hit:
            return ("MR_STOP_LOSS", stop)

        # ── 2. Profit Target ─────────────────────────────────────
        if target_hit:
            return ("MR_PROFIT_TARGET", target)

        # ── 3. Trailing Stop ─────────────────────────────────────
        trail_activation = atr * self.cfg.trailing_activation_atr
        trail_dist = atr * self.cfg.trailing_distance_atr

        if pos.direction == "CALL":
            favorable_move = pos.best_favorable_underlying - entry
            if favorable_move >= trail_activation:
                trail_level = pos.best_favorable_underlying - trail_dist
                if bar_low <= trail_level:
                    return ("MR_TRAILING_STOP", trail_level)
        else:
            favorable_move = entry - pos.best_favorable_underlying
            if favorable_move >= trail_activation:
                trail_level = pos.best_favorable_underlying + trail_dist
                if bar_high >= trail_level:
                    return ("MR_TRAILING_STOP", trail_level)

        # ── 4. Time Stop ─────────────────────────────────────────
        hold_seconds = (current_time - pos.entry_time).total_seconds()
        hold_minutes = hold_seconds / 60

        if hold_minutes >= self.cfg.time_stop_minutes:
            if pos.direction == "CALL":
                move = bar_close - entry
            else:
                move = entry - bar_close

            threshold = atr * self.cfg.time_stop_atr_mult
            if abs(move) <= threshold:
                return ("MR_TIME_STOP", bar_close)

        # ── 5. Max Hold ──────────────────────────────────────────
        if hold_minutes >= self.cfg.max_hold_minutes:
            return ("MR_MAX_HOLD", bar_close)

        # ── 6. EOD Close ─────────────────────────────────────────
        if minutes_to_close <= self.cfg.eod_exit_minutes:
            return ("MR_EOD_CLOSE", bar_close)

        return None


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

        # ── 1. Hard Stop ─────────────────────────────────────────
        # stop_close_confirm: use bar CLOSE (filters wick whipsaws)
        # otherwise: use bar extremes (LOW for calls, HIGH for puts)
        use_close = self.cfg.stop_close_confirm
        if pos.direction == "CALL":
            stop_hit = bar_close <= stop if use_close else bar_low <= stop
            target_hit = bar_high >= target
        else:  # PUT
            stop_hit = bar_close >= stop if use_close else bar_high >= stop
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
# Runner Exit Engine — Let Winners Ride
# ─────────────────────────────────────────────────────────────────

class RunnerExitEngine:
    """
    Exit engine for OTM "runner" positions — fundamentally different
    from scalp exits.

    Philosophy:
      • Scalp exits: tight stops, quick targets, time discipline
      • Runner exits: wide stops, NO targets, let winners run

    The runner is a cheap OTM option ($0.50-$2.00) bought in power hour.
    If SPX makes a big directional move, this 90x's. If not, we lose
    the premium — which is small by design.

    Exit hierarchy:
      1. Wide stop: 5×ATR adverse (give it room to breathe)
      2. Trailing stop: activate at 3×ATR favorable, trail 2×ATR
         (captures big moves while letting them develop)
      3. NO time stop (this is the key difference — runners need time)
      4. Max hold: 120 min (practical limit)
      5. EOD: close 5 min before market close (ride to the end)
    """

    def __init__(self, config: ScalpConfig):
        self.cfg = config

    def check_exit(
        self,
        pos,  # SimScalpPosition or ScalpPosition
        bar_high: float,
        bar_low: float,
        bar_close: float,
        current_time: datetime,
        minutes_to_close: float,
    ) -> Optional[tuple]:
        """Check runner exit conditions. Returns (reason, price) or None."""
        entry = pos.entry_underlying
        stop = pos.stop_price
        atr = pos.atr_at_entry

        # ── Update best favorable underlying ─────────────────────
        if pos.direction == "CALL":
            pos.best_favorable_underlying = max(pos.best_favorable_underlying, bar_high)
        else:
            pos.best_favorable_underlying = min(pos.best_favorable_underlying, bar_low)

        # ── 1. Wide Stop ─────────────────────────────────────────
        if pos.direction == "CALL":
            stop_hit = bar_low <= stop
        else:
            stop_hit = bar_high >= stop

        if stop_hit:
            return ("RUNNER_STOP", stop)

        # ── 2. Trailing Stop (wider than scalp) ──────────────────
        trail_activation = atr * self.cfg.runner_trail_activation_atr
        trail_dist = atr * self.cfg.runner_trail_distance_atr

        if pos.direction == "CALL":
            favorable_move = pos.best_favorable_underlying - entry
            if favorable_move >= trail_activation:
                trail_level = pos.best_favorable_underlying - trail_dist
                if bar_low <= trail_level:
                    return ("RUNNER_TRAIL", trail_level)
        else:
            favorable_move = entry - pos.best_favorable_underlying
            if favorable_move >= trail_activation:
                trail_level = pos.best_favorable_underlying + trail_dist
                if bar_high >= trail_level:
                    return ("RUNNER_TRAIL", trail_level)

        # ── 3. Max Hold ──────────────────────────────────────────
        hold_seconds = (current_time - pos.entry_time).total_seconds()
        hold_minutes = hold_seconds / 60

        if hold_minutes >= self.cfg.runner_max_hold_minutes:
            return ("RUNNER_MAX_HOLD", bar_close)

        # ── 4. EOD Close (ride nearly to the bell) ───────────────
        if minutes_to_close <= self.cfg.runner_eod_exit_minutes:
            return ("RUNNER_EOD", bar_close)

        # NO time stop — runners need time to develop
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


# ─────────────────────────────────────────────────────────────────
# ORB (Opening Range Breakout) Signal Engine
# ─────────────────────────────────────────────────────────────────

@dataclass
class ORBSignal:
    """An ORB breakout signal."""
    direction: str = ""          # "CALL" or "PUT"
    confirmations: List[str] = field(default_factory=list)
    confidence: float = 0.0
    entry_underlying: float = 0.0
    orb_high: float = 0.0
    orb_low: float = 0.0
    orb_range: float = 0.0
    timestamp: datetime = field(default_factory=datetime.now)
    ticker: str = ""


class ORBSignalEngine:
    """
    Opening Range Breakout signal engine.

    Computes the ORB range (high/low of first N bars), then watches
    for a close above/below the range as a breakout signal.

    Strategy B in the multi-strategy stack: fires only on days where
    momentum engine (Strategy A) has no signal.

    Confirmation components:
      1. ORB_BREAK — Close above ORB high / below ORB low (required)
      2. ORB_VOLUME — Volume at breakout bar > average ORB volume
      3. ORB_TREND  — Bar direction aligns with breakout
    """

    def __init__(self, config: ORBConfig, bar_minutes: int = 1):
        self.cfg = config
        self.bar_minutes = bar_minutes

    def compute_orb(self, day_bars: pd.DataFrame) -> Dict[str, Any]:
        """
        Compute ORB levels from the first N bars.

        Returns dict with:
          orb_high, orb_low, orb_range, orb_range_pct, orb_vol_avg,
          valid (bool — whether ORB passes filters).
        """
        n = len(day_bars)
        orb_n = self.cfg.orb_bars

        if n < orb_n + 10:
            return {"valid": False}

        h = day_bars["high"].values
        l = day_bars["low"].values
        v = day_bars["volume"].values
        o_open = day_bars["open"].values[0]

        orb_high = float(h[:orb_n].max())
        orb_low = float(l[:orb_n].min())
        orb_range = orb_high - orb_low
        orb_range_pct = orb_range / o_open if o_open > 0 else 0
        orb_vol_avg = float(v[:orb_n].mean())

        # Range filters
        valid = (
            self.cfg.min_range_pct <= orb_range_pct <= self.cfg.max_range_pct
        )

        return {
            "orb_high": orb_high,
            "orb_low": orb_low,
            "orb_range": orb_range,
            "orb_range_pct": orb_range_pct,
            "orb_vol_avg": orb_vol_avg,
            "valid": valid,
        }

    def evaluate(
        self,
        day_bars: pd.DataFrame,
        orb_data: Dict[str, Any],
        bar_idx: int,
    ) -> Optional[ORBSignal]:
        """
        Check if bar_idx is a breakout above/below ORB range.

        Args:
            day_bars: Full day bars DataFrame
            orb_data: Pre-computed ORB levels from compute_orb()
            bar_idx: Current bar index

        Returns:
            ORBSignal if breakout detected, None otherwise.
        """
        if not orb_data.get("valid", False):
            return None

        cfg = self.cfg
        if bar_idx < cfg.entry_start_bar or bar_idx > cfg.entry_end_bar:
            return None

        c = day_bars["close"].values
        v = day_bars["volume"].values
        o = day_bars["open"].values
        n = len(c)

        if bar_idx >= n or bar_idx < 1:
            return None

        cur_close = float(c[bar_idx])
        prev_close = float(c[bar_idx - 1])
        cur_vol = float(v[bar_idx])
        cur_open = float(o[bar_idx])

        orb_high = orb_data["orb_high"]
        orb_low = orb_data["orb_low"]
        orb_range = orb_data["orb_range"]
        orb_vol_avg = orb_data["orb_vol_avg"]

        # ── Breakout above ORB high ──────────────────────────────
        if cur_close > orb_high and prev_close <= orb_high:
            confirmations = ["ORB_BREAK"]

            # Volume confirmation
            if orb_vol_avg > 0 and cur_vol > orb_vol_avg * 1.2:
                confirmations.append("ORB_VOLUME")

            # Bar is green (close > open) — aligned with breakout
            if cur_close > cur_open:
                confirmations.append("ORB_TREND")

            return ORBSignal(
                direction="CALL",
                confirmations=confirmations,
                confidence=len(confirmations) / 3.0,
                entry_underlying=cur_close,
                orb_high=orb_high,
                orb_low=orb_low,
                orb_range=orb_range,
                ticker="",
            )

        # ── Breakout below ORB low ───────────────────────────────
        if cur_close < orb_low and prev_close >= orb_low:
            confirmations = ["ORB_BREAK"]

            if orb_vol_avg > 0 and cur_vol > orb_vol_avg * 1.2:
                confirmations.append("ORB_VOLUME")

            # Bar is red (close < open) — aligned with breakdown
            if cur_close < cur_open:
                confirmations.append("ORB_TREND")

            return ORBSignal(
                direction="PUT",
                confirmations=confirmations,
                confidence=len(confirmations) / 3.0,
                entry_underlying=cur_close,
                orb_high=orb_high,
                orb_low=orb_low,
                orb_range=orb_range,
                ticker="",
            )

        return None


class ORBExitEngine:
    """
    Exit engine for ORB breakout positions.

    Exits based on ORB-range-multiple stops and targets:
      - Target: price moves target_range_mult × ORB range beyond breakout level
      - Stop: price reverts stop_range_mult × ORB range back inside
      - Time stop: max_hold_bars without hitting target
      - EOD: close before market close

    Same hierarchy as ScalpExitEngine: stop assumed first if both trigger.
    """

    def __init__(self, config: ORBConfig):
        self.cfg = config

    def check_exit(
        self,
        pos,                    # SimScalpPosition
        bar_high: float,
        bar_low: float,
        bar_close: float,
        current_time: datetime,
        minutes_to_close: float,
    ) -> Optional[tuple]:
        """
        Check exit conditions for an ORB position.

        Returns:
            Tuple of (exit_reason, exit_underlying_price) or None.
        """
        stop = pos.stop_price
        target = pos.target_price
        entry = pos.entry_underlying

        # ── Update best favorable underlying (MFE tracking) ──────
        if pos.direction == "CALL":
            pos.best_favorable_underlying = max(
                pos.best_favorable_underlying, bar_high
            )
        else:
            pos.best_favorable_underlying = min(
                pos.best_favorable_underlying, bar_low
            )

        # ── 1. Hard Stop ─────────────────────────────────────────
        use_close = self.cfg.stop_close_confirm
        if pos.direction == "CALL":
            stop_hit = bar_close <= stop if use_close else bar_low <= stop
            target_hit = bar_high >= target
        else:
            stop_hit = bar_close >= stop if use_close else bar_high >= stop
            target_hit = bar_low <= target

        # Conservative: stop wins ties
        if stop_hit and target_hit:
            return ("STOP_LOSS", stop)

        if stop_hit:
            return ("STOP_LOSS", stop)

        # ── 2. Target ────────────────────────────────────────────
        if target_hit:
            return ("PROFIT_TARGET", target)

        # ── 3. Trailing Stop (protect profits on runners) ────────
        if self.cfg.trailing_enabled:
            hold_mins = (current_time - pos.entry_time).total_seconds() / 60
            past_min_bars = hold_mins >= self.cfg.trailing_min_bars

            if past_min_bars:
                if pos.direction == "CALL":
                    target_dist = target - entry
                    favorable_move = pos.best_favorable_underlying - entry
                else:
                    target_dist = entry - target
                    favorable_move = entry - pos.best_favorable_underlying

                activation_threshold = target_dist * self.cfg.trailing_activation_pct

                if favorable_move >= activation_threshold and target_dist > 0:
                    # Trailing is active — compute trail level
                    trail_offset = favorable_move * self.cfg.trailing_distance_pct
                    if self.cfg.trailing_breakeven:
                        # Trail from peak, but never below breakeven (entry)
                        if pos.direction == "CALL":
                            trail_level = max(
                                entry,
                                pos.best_favorable_underlying - trail_offset,
                            )
                            if bar_low <= trail_level:
                                return ("TRAILING_STOP", trail_level)
                        else:
                            trail_level = min(
                                entry,
                                pos.best_favorable_underlying + trail_offset,
                            )
                            if bar_high >= trail_level:
                                return ("TRAILING_STOP", trail_level)
                    else:
                        # Trail from peak without breakeven floor
                        if pos.direction == "CALL":
                            trail_level = pos.best_favorable_underlying - trail_offset
                            if bar_low <= trail_level:
                                return ("TRAILING_STOP", trail_level)
                        else:
                            trail_level = pos.best_favorable_underlying + trail_offset
                            if bar_high >= trail_level:
                                return ("TRAILING_STOP", trail_level)

        # ── 4. Time stop (max hold bars) ─────────────────────────
        hold_minutes = (current_time - pos.entry_time).total_seconds() / 60
        max_hold_minutes = self.cfg.max_hold_bars * 1  # 1 bar = 1 min

        if hold_minutes >= max_hold_minutes:
            return ("TIME_STOP", bar_close)

        # ── 5. EOD exit ──────────────────────────────────────────
        if minutes_to_close <= self.cfg.eod_exit_minutes:
            return ("EOD_CLOSE", bar_close)

        return None


# ─────────────────────────────────────────────────────────────────
# Range Fade Signal Engine (Strategy E: RANGE_BOUND days)
# ─────────────────────────────────────────────────────────────────

@dataclass
class RangeFadeSignal:
    """A range-fade signal: buy calls at range low, puts at range high."""
    direction: str = ""          # "CALL" or "PUT"
    confirmations: List[str] = field(default_factory=list)
    confidence: float = 0.0
    entry_underlying: float = 0.0
    range_high: float = 0.0
    range_low: float = 0.0
    range_size: float = 0.0
    timestamp: datetime = field(default_factory=datetime.now)
    ticker: str = ""


class RangeFadeSignalEngine:
    """
    Range-Bound Fade signal engine.

    Identifies range boundaries from the first N bars, then watches for
    price to reach the top/bottom boundary zone with confirmation.

    Strategy E: fires only on RANGE_BOUND (and optionally MIXED) days
    where ORB and momentum are both disabled.

    Confirmation components:
      1. RANGE_TOUCH — Price in top/bottom boundary zone of the range
      2. REVERSAL_BAR — Rejection wick or engulfing pattern at boundary
      3. VWAP_CROSS — Price crossing back toward VWAP (mean reversion)
      4. RSI_EXTREME — RSI confirms overbought/oversold at boundary
    """

    def __init__(self, config: RangeFadeConfig, bar_minutes: int = 1):
        self.cfg = config
        self.bar_minutes = bar_minutes

    def compute_range(self, day_bars: pd.DataFrame) -> Dict[str, Any]:
        """
        Compute range levels from the first N bars.

        Returns dict with:
          range_high, range_low, range_size, range_mid, vwap_at_form,
          valid (bool).
        """
        n = len(day_bars)
        form_n = self.cfg.formation_bars

        if n < form_n + 20:
            return {"valid": False}

        h = day_bars["high"].values
        l = day_bars["low"].values
        c = day_bars["close"].values
        o_open = c[0]

        range_high = float(h[:form_n].max())
        range_low = float(l[:form_n].min())
        range_size = range_high - range_low
        range_pct = range_size / o_open if o_open > 0 else 0

        # Need a meaningful range (>0.3% for SPX ~ $17)
        valid = range_pct > 0.003 and range_size > 0

        range_mid = (range_high + range_low) / 2.0

        # Compute VWAP if volume available
        vwap = float(c[:form_n].mean())  # Simple price mean as proxy
        if "volume" in day_bars.columns:
            v = day_bars["volume"].values[:form_n]
            total_vol = v.sum()
            if total_vol > 0:
                typical = (h[:form_n] + l[:form_n] + c[:form_n]) / 3.0
                vwap = float((typical * v).sum() / total_vol)

        return {
            "range_high": range_high,
            "range_low": range_low,
            "range_size": range_size,
            "range_pct": range_pct,
            "range_mid": range_mid,
            "vwap": vwap,
            "valid": valid,
        }

    def evaluate(
        self,
        day_bars: pd.DataFrame,
        range_data: Dict[str, Any],
        bar_idx: int,
    ) -> Optional[RangeFadeSignal]:
        """
        Check if bar_idx is a fade opportunity at range boundary.

        Generates PUT signals at range high (fade down),
        CALL signals at range low (fade up).
        """
        if not range_data.get("valid", False):
            return None

        cfg = self.cfg
        if bar_idx < cfg.entry_start_bar or bar_idx > cfg.entry_end_bar:
            return None

        c = day_bars["close"].values
        h = day_bars["high"].values
        l = day_bars["low"].values
        o = day_bars["open"].values
        n = len(c)

        if bar_idx >= n or bar_idx < 2:
            return None

        cur_close = float(c[bar_idx])
        prev_close = float(c[bar_idx - 1])
        cur_high = float(h[bar_idx])
        cur_low = float(l[bar_idx])
        cur_open = float(o[bar_idx])

        range_high = range_data["range_high"]
        range_low = range_data["range_low"]
        range_size = range_data["range_size"]
        range_mid = range_data["range_mid"]
        vwap = range_data["vwap"]

        boundary_zone = range_size * cfg.boundary_zone_pct

        # ── Compute RSI ──────────────────────────────────────────
        rsi = self._compute_rsi(c, bar_idx, cfg.rsi_period)

        # ── Check UPPER boundary (fade → PUT) ────────────────────
        upper_threshold = range_high - boundary_zone
        if cur_high >= upper_threshold:
            confirmations = ["RANGE_TOUCH"]

            # Reversal bar: wick rejection at top (upper wick > body)
            body = abs(cur_close - cur_open)
            upper_wick = cur_high - max(cur_close, cur_open)
            if upper_wick > body and cur_close < cur_open:
                confirmations.append("REVERSAL_BAR")

            # VWAP cross: price was above VWAP, closing back toward it
            if prev_close > vwap and cur_close < prev_close:
                confirmations.append("VWAP_CROSS")

            # RSI overbought
            if rsi > cfg.rsi_overbought:
                confirmations.append("RSI_EXTREME")

            if len(confirmations) >= cfg.min_confirmations:
                return RangeFadeSignal(
                    direction="PUT",
                    confirmations=confirmations,
                    confidence=len(confirmations) / 4.0,
                    entry_underlying=cur_close,
                    range_high=range_high,
                    range_low=range_low,
                    range_size=range_size,
                    ticker="",
                )

        # ── Check LOWER boundary (fade → CALL) ──────────────────
        lower_threshold = range_low + boundary_zone
        if cur_low <= lower_threshold:
            confirmations = ["RANGE_TOUCH"]

            # Reversal bar: wick rejection at bottom (lower wick > body)
            body = abs(cur_close - cur_open)
            lower_wick = min(cur_close, cur_open) - cur_low
            if lower_wick > body and cur_close > cur_open:
                confirmations.append("REVERSAL_BAR")

            # VWAP cross: price was below VWAP, closing back toward it
            if prev_close < vwap and cur_close > prev_close:
                confirmations.append("VWAP_CROSS")

            # RSI oversold
            if rsi < cfg.rsi_oversold:
                confirmations.append("RSI_EXTREME")

            if len(confirmations) >= cfg.min_confirmations:
                return RangeFadeSignal(
                    direction="CALL",
                    confirmations=confirmations,
                    confidence=len(confirmations) / 4.0,
                    entry_underlying=cur_close,
                    range_high=range_high,
                    range_low=range_low,
                    range_size=range_size,
                    ticker="",
                )

        return None

    @staticmethod
    def _compute_rsi(closes: np.ndarray, idx: int, period: int = 14) -> float:
        """Compute RSI at a given bar index."""
        if idx < period + 1:
            return 50.0  # Neutral default

        window = closes[idx - period:idx + 1]
        deltas = np.diff(window)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)

        avg_gain = gains.mean()
        avg_loss = losses.mean()

        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))


class RangeFadeExitEngine:
    """
    Exit engine for range-fade positions.

    Mean-reversion targets: fade back toward range midpoint.
      - Target: price reverts target_range_pct × range toward center
      - Stop: price breaks stop_range_pct × range beyond boundary
      - Time stop: max_hold_bars without reversion
      - EOD: close before market close
    """

    def __init__(self, config: RangeFadeConfig):
        self.cfg = config

    def check_exit(
        self,
        pos,                    # SimScalpPosition
        bar_high: float,
        bar_low: float,
        bar_close: float,
        current_time: datetime,
        minutes_to_close: float,
    ) -> Optional[tuple]:
        """
        Check exit conditions for a range-fade position.

        Returns:
            Tuple of (exit_reason, exit_underlying_price) or None.
        """
        stop = pos.stop_price
        target = pos.target_price

        # ── 1. Hard Stop ─────────────────────────────────────────
        use_close = self.cfg.stop_close_confirm
        if pos.direction == "CALL":
            stop_hit = bar_close <= stop if use_close else bar_low <= stop
            target_hit = bar_high >= target
        else:
            stop_hit = bar_close >= stop if use_close else bar_high >= stop
            target_hit = bar_low <= target

        # Conservative: stop wins ties
        if stop_hit and target_hit:
            return ("STOP_LOSS", stop)

        if stop_hit:
            return ("STOP_LOSS", stop)

        # ── 2. Target ────────────────────────────────────────────
        if target_hit:
            return ("PROFIT_TARGET", target)

        # ── 3. Time stop (max hold bars) ─────────────────────────
        hold_minutes = (current_time - pos.entry_time).total_seconds() / 60
        max_hold_minutes = self.cfg.max_hold_bars * 1  # 1 bar = 1 min

        if hold_minutes >= max_hold_minutes:
            return ("TIME_STOP", bar_close)

        # ── 4. EOD exit ──────────────────────────────────────────
        if minutes_to_close <= self.cfg.eod_exit_minutes:
            return ("EOD_CLOSE", bar_close)

        return None


# ═════════════════════════════════════════════════════════════════
# Strategy F — VWAP Mean-Reversion (DEAD_FLAT days)
# ═════════════════════════════════════════════════════════════════

@dataclass
class VWAPMRSignal:
    """A VWAP mean-reversion signal: fade deviations from VWAP on flat days."""
    direction: str = ""          # "CALL" (price below VWAP) or "PUT" (above)
    confirmations: List[str] = field(default_factory=list)
    confidence: float = 0.0
    entry_underlying: float = 0.0
    vwap_price: float = 0.0      # VWAP at signal time
    deviation_pct: float = 0.0   # How far price is from VWAP (%)
    timestamp: datetime = field(default_factory=datetime.now)
    ticker: str = ""


class VWAPMRSignalEngine:
    """
    VWAP Mean-Reversion signal engine for DEAD_FLAT days.

    On dead-flat days (range <0.8%), price oscillates tightly around VWAP.
    When price drifts ≥0.15% from VWAP, we fade the deviation expecting
    a snap-back. DEAD_FLAT days have avg max VWAP deviation of 0.286%,
    so a 0.15% threshold catches the meaningful oscillations.

    Strategy F: fires only on DEAD_FLAT days where all other strategies
    are disabled (ORB skips dead flat, momentum doesn't fire, RF needs
    RANGE_BOUND).

    Confirmation components:
      1. VWAP_DEV    — Price deviation ≥ threshold from VWAP
      2. REVERSAL_BAR — Wick rejection or engulfing at deviation extreme
      3. RSI_EXTREME  — RSI confirms overextension
      4. SNAP_BACK    — Price starting to revert (closing toward VWAP)
    """

    def __init__(self, config: VWAPMRConfig, bar_minutes: int = 1):
        self.cfg = config
        self.bar_minutes = bar_minutes

    def compute_vwap(self, day_bars: pd.DataFrame) -> Dict[str, Any]:
        """
        Compute rolling VWAP for the entire day.

        Returns dict with:
          vwap_arr (numpy array of VWAP per bar), valid (bool).
        """
        n = len(day_bars)
        if n < self.cfg.vwap_warmup_bars + 10:
            return {"valid": False}

        h = day_bars["high"].values
        l = day_bars["low"].values
        c = day_bars["close"].values
        typical = (h + l + c) / 3.0

        # Use volume-weighted VWAP if volume available
        if "volume" in day_bars.columns:
            v = day_bars["volume"].values.astype(float)
            cum_vol = np.cumsum(v)
            cum_tp_vol = np.cumsum(typical * v)
            # Avoid division by zero
            vwap_arr = np.where(
                cum_vol > 0,
                cum_tp_vol / cum_vol,
                typical
            )
        else:
            # Fallback: cumulative mean of typical price
            vwap_arr = np.cumsum(typical) / np.arange(1, n + 1)

        return {
            "vwap_arr": vwap_arr,
            "valid": True,
        }

    def evaluate(
        self,
        day_bars: pd.DataFrame,
        vwap_data: Dict[str, Any],
        bar_idx: int,
    ) -> Optional[VWAPMRSignal]:
        """
        Check if bar_idx has a VWAP mean-reversion opportunity.

        Generates CALL when price is below VWAP (expect snap-back up),
        PUT when price is above VWAP (expect snap-back down).
        """
        if not vwap_data.get("valid", False):
            return None

        cfg = self.cfg
        if bar_idx < cfg.entry_start_bar or bar_idx > cfg.entry_end_bar:
            return None

        c = day_bars["close"].values
        h = day_bars["high"].values
        l = day_bars["low"].values
        o = day_bars["open"].values
        n = len(c)

        if bar_idx >= n or bar_idx < 2:
            return None

        vwap_arr = vwap_data["vwap_arr"]
        cur_close = float(c[bar_idx])
        prev_close = float(c[bar_idx - 1])
        cur_high = float(h[bar_idx])
        cur_low = float(l[bar_idx])
        cur_open = float(o[bar_idx])
        cur_vwap = float(vwap_arr[bar_idx])

        if cur_vwap <= 0:
            return None

        # ── Compute deviation from VWAP ──────────────────────────
        deviation = cur_close - cur_vwap
        deviation_pct = abs(deviation) / cur_vwap * 100  # in %

        if deviation_pct < cfg.min_vwap_deviation_pct:
            return None

        # ── Compute RSI ──────────────────────────────────────────
        rsi = self._compute_rsi(c, bar_idx, cfg.rsi_period)

        # ── Price BELOW VWAP → buy CALL (expect snap-back up) ───
        if deviation < 0:
            confirmations = ["VWAP_DEV"]

            # Reversal bar: wick rejection at bottom (lower wick > body)
            body = abs(cur_close - cur_open)
            lower_wick = min(cur_close, cur_open) - cur_low
            if lower_wick > body * 1.2 and cur_close > cur_open:
                confirmations.append("REVERSAL_BAR")

            # RSI oversold
            if rsi < cfg.rsi_oversold:
                confirmations.append("RSI_EXTREME")

            # Snap-back: price closing back toward VWAP vs prior bar
            prev_dev = abs(prev_close - float(vwap_arr[bar_idx - 1]))
            cur_dev = abs(cur_close - cur_vwap)
            if cur_dev < prev_dev and cur_close > prev_close:
                confirmations.append("SNAP_BACK")

            if len(confirmations) >= cfg.min_confirmations:
                return VWAPMRSignal(
                    direction="CALL",
                    confirmations=confirmations,
                    confidence=len(confirmations) / 4.0,
                    entry_underlying=cur_close,
                    vwap_price=cur_vwap,
                    deviation_pct=deviation_pct,
                    ticker="",
                )

        # ── Price ABOVE VWAP → buy PUT (expect snap-back down) ──
        elif deviation > 0:
            confirmations = ["VWAP_DEV"]

            # Reversal bar: wick rejection at top (upper wick > body)
            body = abs(cur_close - cur_open)
            upper_wick = cur_high - max(cur_close, cur_open)
            if upper_wick > body * 1.2 and cur_close < cur_open:
                confirmations.append("REVERSAL_BAR")

            # RSI overbought
            if rsi > cfg.rsi_overbought:
                confirmations.append("RSI_EXTREME")

            # Snap-back: price closing back toward VWAP vs prior bar
            prev_dev = abs(prev_close - float(vwap_arr[bar_idx - 1]))
            cur_dev = abs(cur_close - cur_vwap)
            if cur_dev < prev_dev and cur_close < prev_close:
                confirmations.append("SNAP_BACK")

            if len(confirmations) >= cfg.min_confirmations:
                return VWAPMRSignal(
                    direction="PUT",
                    confirmations=confirmations,
                    confidence=len(confirmations) / 4.0,
                    entry_underlying=cur_close,
                    vwap_price=cur_vwap,
                    deviation_pct=deviation_pct,
                    ticker="",
                )

        return None

    @staticmethod
    def _compute_rsi(closes: np.ndarray, idx: int, period: int = 14) -> float:
        """Compute RSI at a given bar index."""
        if idx < period + 1:
            return 50.0

        window = closes[idx - period:idx + 1]
        deltas = np.diff(window)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)

        avg_gain = gains.mean()
        avg_loss = losses.mean()

        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))


class VWAPMRExitEngine:
    """
    Exit engine for VWAP mean-reversion positions.

    Mean-reversion targets: price snaps back toward VWAP.
      - Target: price returns within target_vwap_return_pct of VWAP
      - Stop: deviation grows past stop_deviation_pct (breakout)
      - Time stop: max_hold_bars without reversion
      - EOD: close before market close
    """

    def __init__(self, config: VWAPMRConfig):
        self.cfg = config

    def check_exit(
        self,
        pos,                    # SimScalpPosition
        bar_high: float,
        bar_low: float,
        bar_close: float,
        current_time: datetime,
        minutes_to_close: float,
        current_vwap: float,    # Current VWAP value
    ) -> Optional[tuple]:
        """
        Check exit conditions for a VWAP MR position.

        Returns:
            Tuple of (exit_reason, exit_underlying_price) or None.
        """
        if current_vwap <= 0:
            return None

        stop = pos.stop_price
        target = pos.target_price

        # ── 1. Hard Stop (deviation increases) ───────────────────
        if pos.direction == "CALL":
            stop_hit = bar_low <= stop
            target_hit = bar_high >= target
        else:
            stop_hit = bar_high >= stop
            target_hit = bar_low <= target

        # Conservative: stop wins ties
        if stop_hit and target_hit:
            return ("STOP_LOSS", stop)

        if stop_hit:
            return ("STOP_LOSS", stop)

        # ── 2. Target (snap-back to VWAP) ────────────────────────
        if target_hit:
            return ("PROFIT_TARGET", target)

        # ── 3. Dynamic VWAP target ───────────────────────────────
        # Also exit if price crosses VWAP (full reversion)
        if pos.direction == "CALL" and bar_high >= current_vwap:
            return ("VWAP_TOUCH", current_vwap)
        if pos.direction == "PUT" and bar_low <= current_vwap:
            return ("VWAP_TOUCH", current_vwap)

        # ── 4. Time stop ─────────────────────────────────────────
        hold_minutes = (current_time - pos.entry_time).total_seconds() / 60
        max_hold_minutes = self.cfg.max_hold_bars * 1  # 1 bar = 1 min

        if hold_minutes >= max_hold_minutes:
            return ("TIME_STOP", bar_close)

        # ── 5. EOD exit ──────────────────────────────────────────
        if minutes_to_close <= self.cfg.eod_exit_minutes:
            return ("EOD_CLOSE", bar_close)

        return None

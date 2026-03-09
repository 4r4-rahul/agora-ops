"""
Multi-Timeframe Signal Generator
===================================
Computes trading signals across D1, H1, M5, M1 timeframes for
professional-grade 0DTE entry/exit decisions.

Timeframe hierarchy:
  D1 (Daily)  → Regime, trend direction, ATR-based expected range
  H1 (Hourly) → Intraday structure, VWAP slope, session bias
  M5 (5-min)  → Entry trigger: pullback, momentum, volume confirm
  M1 (1-min)  → Execution: stop management, worst-case fills

Usage:
    from trading_engine.data.multi_tf import MultiTFAnalyzer, TFSignals

    analyzer = MultiTFAnalyzer()
    signals = analyzer.compute(today_bars, history_days)

    if signals.entry_allowed:
        # Enter with signals.direction_bias ("put_credit" or "call_credit")
        ...
    if signals.exit_early:
        # Close position — structure broken
        ...
"""

import math
import numpy as np
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from typing import List, Dict, Optional, Tuple

from .intraday import IntradayBar, TradingDay


# ─────────────────────────────────────────────────────────────────
# Signal models
# ─────────────────────────────────────────────────────────────────

@dataclass
class DailySignals:
    """D1 timeframe: trend + volatility context."""
    trend: str = "neutral"          # "bullish", "bearish", "neutral"
    price_vs_sma20: float = 0.0     # % above/below 20-SMA
    sma20: float = 0.0
    atr14: float = 0.0              # 14-day ATR for range sizing
    atr_pct: float = 0.0            # ATR as % of price
    prev_close: float = 0.0
    day_range_high: float = 0.0     # Expected upper bound (prev_close + ATR)
    day_range_low: float = 0.0      # Expected lower bound (prev_close - ATR)
    consecutive_up: int = 0         # Consecutive green D1 bars
    consecutive_down: int = 0       # Consecutive red D1 bars
    d1_momentum: float = 0.0       # 5-day rate of change


@dataclass
class HourlySignals:
    """H1 timeframe: intraday structure."""
    vwap: float = 0.0               # Running VWAP
    price_vs_vwap: float = 0.0      # % above/below VWAP
    vwap_slope: float = 0.0         # VWAP trend (positive = rising)
    structure: str = "neutral"       # "higher_lows", "lower_highs", "neutral"
    h1_trend: str = "neutral"        # "bullish", "bearish", "neutral"
    h1_high: float = 0.0            # H1 session high
    h1_low: float = 0.0             # H1 session low
    h1_range: float = 0.0           # H1 high - low
    above_vwap_pct: float = 0.0     # % of bars above VWAP


@dataclass
class M5Signals:
    """M5 timeframe: entry trigger signals."""
    rsi: float = 50.0               # 14-period RSI on M5
    rsi_zone: str = "neutral"       # "oversold" (<30), "overbought" (>70), "neutral"
    momentum: float = 0.0           # Price momentum (recent 3 bars)
    volume_ratio: float = 1.0       # Current volume vs 20-bar average
    pullback_to_vwap: bool = False  # Price just pulled back to VWAP
    candle_pattern: str = "none"    # "bullish_engulf", "bearish_engulf", "doji", "none"
    higher_lows_5m: bool = False    # 3 consecutive M5 higher lows
    lower_highs_5m: bool = False    # 3 consecutive M5 lower highs


@dataclass
class TFSignals:
    """
    Combined multi-timeframe signals for trade decisions.

    Key insight from backtesting: For far-OTM 0DTE credit spreads (Δ≤0.15),
    D1 trend direction is noise — the short strike is too far away for
    daily trend to matter. VIX regime (captured by regime filter) is the
    real edge because it predicts MOVE SIZE.

    Multi-TF value therefore comes from:
      1. POSITION SIZING (scale down on hostile days, not binary reject)
      2. ATR-based risk adjustment (expanding vol → smaller positions)
      3. Only HARD REJECT on extreme conditions (multiple D1 gates fail)
    """
    daily: DailySignals = field(default_factory=DailySignals)
    hourly: HourlySignals = field(default_factory=HourlySignals)
    m5: M5Signals = field(default_factory=M5Signals)

    # ── Combined entry decision ──
    entry_allowed: bool = True
    direction_bias: str = "neutral"   # "put_credit", "call_credit", "iron_condor"
    entry_confidence: float = 0.0     # 0-1 score (higher = more aligned)
    entry_rejection_reason: str = ""  # Why entry was rejected

    # ── Position sizing multiplier ──
    # 1.0 = normal, 0.75 = reduced, 0.5 = minimum
    size_multiplier: float = 1.0

    # ── Combined exit decision ──
    exit_early: bool = False
    exit_reason: str = ""

    # ── Strike guidance ──
    suggested_delta: float = 0.15
    suggested_width: float = 2.0
    strike_distance_from_atr: float = 0.0  # How many ATRs away the short strike is


# ─────────────────────────────────────────────────────────────────
# Multi-timeframe analyzer
# ─────────────────────────────────────────────────────────────────

class MultiTFAnalyzer:
    """
    Computes multi-timeframe signals from raw 1-min or 5-min bars.

    For backtesting: call compute_day_signals() with today's bars + history.
    For live trading: call compute_live_signals() with streaming bars.
    """

    def __init__(self, sma_period: int = 20, atr_period: int = 14,
                 rsi_period: int = 14):
        self.sma_period = sma_period
        self.atr_period = atr_period
        self.rsi_period = rsi_period

    # ─── Daily (D1) signals ───────────────────────────────────────

    def compute_daily(self, history: List[TradingDay],
                      today: TradingDay) -> DailySignals:
        """
        Compute daily-level signals from prior trading days.

        Args:
            history: Previous N trading days (oldest first)
            today:   Current day being evaluated
        """
        sig = DailySignals()

        if len(history) < self.sma_period:
            return sig

        # Daily closes
        closes = [d.close_price for d in history]
        highs = [d.high_price for d in history]
        lows = [d.low_price for d in history]

        # SMA-20
        sma20 = np.mean(closes[-self.sma_period:])
        sig.sma20 = round(sma20, 2)
        sig.prev_close = closes[-1]
        sig.price_vs_sma20 = round((closes[-1] - sma20) / sma20 * 100, 3)

        # Trend
        if closes[-1] > sma20 and closes[-2] > np.mean(closes[-self.sma_period - 1:-1]):
            sig.trend = "bullish"
        elif closes[-1] < sma20 and closes[-2] < np.mean(closes[-self.sma_period - 1:-1]):
            sig.trend = "bearish"
        else:
            sig.trend = "neutral"

        # ATR-14
        trs = []
        for i in range(max(1, len(closes) - self.atr_period), len(closes)):
            h, l, pc = highs[i], lows[i], closes[i - 1]
            tr = max(h - l, abs(h - pc), abs(l - pc))
            trs.append(tr)
        sig.atr14 = round(np.mean(trs), 2) if trs else 0
        sig.atr_pct = round(sig.atr14 / closes[-1] * 100, 3) if closes[-1] > 0 else 0

        # Expected range
        sig.day_range_high = round(closes[-1] + sig.atr14 * 1.0, 2)
        sig.day_range_low = round(closes[-1] - sig.atr14 * 1.0, 2)

        # Consecutive direction
        up = 0
        for i in range(len(closes) - 1, 0, -1):
            if closes[i] > closes[i - 1]:
                up += 1
            else:
                break
        down = 0
        for i in range(len(closes) - 1, 0, -1):
            if closes[i] < closes[i - 1]:
                down += 1
            else:
                break
        sig.consecutive_up = up
        sig.consecutive_down = down

        # 5-day momentum (rate of change)
        if len(closes) >= 6:
            sig.d1_momentum = round((closes[-1] - closes[-6]) / closes[-6] * 100, 3)

        return sig

    # ─── Hourly (H1) signals ─────────────────────────────────────

    def compute_hourly(self, bars: List[IntradayBar],
                       up_to_idx: int) -> HourlySignals:
        """
        Compute H1 signals from today's bars up to a given bar index.
        Resamples M1/M5 bars into H1 bars and computes structure.

        Args:
            bars:      All bars for today
            up_to_idx: Compute signals only using bars up to this index
        """
        sig = HourlySignals()

        active_bars = bars[:up_to_idx + 1]
        if len(active_bars) < 5:
            return sig

        # ── VWAP ──
        cum_tp_vol = 0.0
        cum_vol = 0
        for b in active_bars:
            typical_price = (b.high + b.low + b.close) / 3
            vol = max(b.volume, 1)
            cum_tp_vol += typical_price * vol
            cum_vol += vol

        sig.vwap = round(cum_tp_vol / cum_vol, 2) if cum_vol > 0 else active_bars[-1].close

        current_price = active_bars[-1].close
        sig.price_vs_vwap = round((current_price - sig.vwap) / sig.vwap * 100, 3)

        # VWAP slope: compare VWAP at current point vs VWAP 30 bars ago
        if len(active_bars) >= 30:
            old_tp_vol = sum((b.high + b.low + b.close) / 3 * max(b.volume, 1)
                             for b in active_bars[:len(active_bars) - 30])
            old_vol = sum(max(b.volume, 1) for b in active_bars[:len(active_bars) - 30])
            old_vwap = old_tp_vol / old_vol if old_vol > 0 else sig.vwap
            sig.vwap_slope = round(sig.vwap - old_vwap, 3)
        else:
            sig.vwap_slope = 0.0

        # % bars above VWAP
        above = 0
        running_tp_vol = 0.0
        running_vol = 0
        for b in active_bars:
            tp = (b.high + b.low + b.close) / 3
            vol = max(b.volume, 1)
            running_tp_vol += tp * vol
            running_vol += vol
            running_vwap = running_tp_vol / running_vol
            if b.close > running_vwap:
                above += 1
        sig.above_vwap_pct = round(above / len(active_bars) * 100, 1)

        # ── H1 structure (resample to hourly) ──
        hourly_bars = self._resample_hourly(active_bars)
        if len(hourly_bars) >= 3:
            lows = [h["low"] for h in hourly_bars[-3:]]
            highs = [h["high"] for h in hourly_bars[-3:]]

            if lows[-1] > lows[-2] > lows[-3]:
                sig.structure = "higher_lows"
            elif highs[-1] < highs[-2] < highs[-3]:
                sig.structure = "lower_highs"
            else:
                sig.structure = "neutral"

            if hourly_bars[-1]["close"] > hourly_bars[-2]["close"] > hourly_bars[-3]["close"]:
                sig.h1_trend = "bullish"
            elif hourly_bars[-1]["close"] < hourly_bars[-2]["close"] < hourly_bars[-3]["close"]:
                sig.h1_trend = "bearish"
            else:
                sig.h1_trend = "neutral"

        sig.h1_high = max(b.high for b in active_bars)
        sig.h1_low = min(b.low for b in active_bars)
        sig.h1_range = round(sig.h1_high - sig.h1_low, 2)

        return sig

    # ─── 5-Minute (M5) signals ───────────────────────────────────

    def compute_m5(self, bars: List[IntradayBar],
                   up_to_idx: int, vwap: float) -> M5Signals:
        """
        Compute M5 signals: RSI, momentum, volume, VWAP pullback.

        Args:
            bars:      All bars for today
            up_to_idx: Index up to which to compute
            vwap:      Current VWAP value (from H1 calc)
        """
        sig = M5Signals()

        # Resample to M5 if bars are 1-min
        m5_bars = self._resample_m5(bars[:up_to_idx + 1])
        if len(m5_bars) < self.rsi_period + 1:
            return sig

        closes = [b["close"] for b in m5_bars]

        # ── RSI-14 on M5 ──
        sig.rsi = self._compute_rsi(closes, self.rsi_period)
        if sig.rsi < 30:
            sig.rsi_zone = "oversold"
        elif sig.rsi > 70:
            sig.rsi_zone = "overbought"
        else:
            sig.rsi_zone = "neutral"

        # ── Momentum (3-bar rate of change) ──
        if len(closes) >= 4:
            sig.momentum = round((closes[-1] - closes[-4]) / closes[-4] * 100, 4)

        # ── Volume ratio ──
        volumes = [b["volume"] for b in m5_bars]
        if len(volumes) >= 20:
            avg_vol = np.mean(volumes[-20:])
            sig.volume_ratio = round(volumes[-1] / avg_vol, 2) if avg_vol > 0 else 1.0
        else:
            sig.volume_ratio = 1.0

        # ── VWAP pullback detection ──
        # Price was above VWAP, pulled back within 0.1% of VWAP
        if len(m5_bars) >= 3 and vwap > 0:
            prev_above = m5_bars[-3]["close"] > vwap
            near_vwap = abs(closes[-1] - vwap) / vwap < 0.001  # Within 0.1%
            sig.pullback_to_vwap = prev_above and near_vwap

        # ── Candle patterns (simplified) ──
        if len(m5_bars) >= 2:
            prev = m5_bars[-2]
            curr = m5_bars[-1]
            prev_body = prev["close"] - prev["open"]
            curr_body = curr["close"] - curr["open"]

            # Bullish engulfing
            if prev_body < 0 and curr_body > 0 and abs(curr_body) > abs(prev_body) * 1.5:
                sig.candle_pattern = "bullish_engulf"
            # Bearish engulfing
            elif prev_body > 0 and curr_body < 0 and abs(curr_body) > abs(prev_body) * 1.5:
                sig.candle_pattern = "bearish_engulf"
            # Doji (small body relative to range)
            elif abs(curr_body) < (curr["high"] - curr["low"]) * 0.15:
                sig.candle_pattern = "doji"

        # ── M5 structure ──
        if len(m5_bars) >= 4:
            lows = [b["low"] for b in m5_bars[-4:-1]]
            highs = [b["high"] for b in m5_bars[-4:-1]]
            if lows[-1] > lows[-2] > lows[-3]:
                sig.higher_lows_5m = True
            if highs[-1] < highs[-2] < highs[-3]:
                sig.lower_highs_5m = True

        return sig

    # ─── Combined signal decision ─────────────────────────────────

    def compute_entry_signals(self, daily: DailySignals,
                              hourly: HourlySignals,
                              m5: M5Signals,
                              strategy: str = "") -> TFSignals:
        """
        Evaluate multi-TF conditions for the GIVEN strategy.

        POSITION SIZING approach (not binary reject):
          - Normal (1.0×): Favorable or neutral conditions
          - Reduced (0.75×): One adverse D1 signal
          - Minimum (0.5×): Multiple adverse signals (hostile)
          - REJECT (0.0×): Extreme conditions (very rare, 2+ hard rejections)

        Why not binary? Far-OTM credit spreads (Δ≤0.15) profit from
        theta decay on MOST days regardless of trend. Binary filtering
        removes winners, not losers. Sizing captures the nuance.

        Args:
            strategy: Strategy from regime optimizer
        """
        signals = TFSignals(daily=daily, hourly=hourly, m5=m5)
        signals.direction_bias = strategy
        signals.entry_allowed = True  # Default: allow
        signals.size_multiplier = 1.0  # Default: full size

        penalties = 0  # Count adverse signals
        rejection_reasons = []

        # ══════════════════════════════════════════════════════════
        # D1 SIGNALS (primary — these drive position sizing)
        # ══════════════════════════════════════════════════════════

        # ── Check 1: ATR expansion (most predictive of large moves) ──
        # High ATR = higher probability of breaching short strike
        if daily.atr_pct > 1.8:
            penalties += 2  # Extreme volatility (>1.8% daily range)
            rejection_reasons.append(f"D1: extreme ATR {daily.atr_pct:.2f}%")
        elif daily.atr_pct > 1.3:
            penalties += 1  # Elevated volatility
            rejection_reasons.append(f"D1: elevated ATR {daily.atr_pct:.2f}%")

        # ── Check 2: Strong momentum against strategy ──
        if strategy == "call_credit" and daily.d1_momentum > 2.5:
            penalties += 1  # Strong 5-day rally → call credit risk
            rejection_reasons.append(f"D1: strong up momentum {daily.d1_momentum:+.2f}%")
        elif strategy == "put_credit" and daily.d1_momentum < -2.5:
            penalties += 1
            rejection_reasons.append(f"D1: strong down momentum {daily.d1_momentum:+.2f}%")

        # ── Check 3: Consecutive move exhaustion ──
        if strategy == "call_credit" and daily.consecutive_up >= 5:
            penalties += 1  # 5+ up days could mean breakout
            rejection_reasons.append(f"D1: {daily.consecutive_up} consecutive up days")
        elif strategy == "put_credit" and daily.consecutive_down >= 5:
            penalties += 1
            rejection_reasons.append(f"D1: {daily.consecutive_down} consecutive down days")

        # ── Check 4: Price far from SMA (overextended) ──
        if strategy == "call_credit" and daily.price_vs_sma20 > 3.0:
            penalties += 1  # Price >3% above SMA, could extend further
            rejection_reasons.append(f"D1: price {daily.price_vs_sma20:+.1f}% vs SMA20")
        elif strategy == "put_credit" and daily.price_vs_sma20 < -3.0:
            penalties += 1
            rejection_reasons.append(f"D1: price {daily.price_vs_sma20:+.1f}% vs SMA20")

        # ══════════════════════════════════════════════════════════
        # H1/M5 SIGNALS (secondary — mild adjustments)
        # ══════════════════════════════════════════════════════════

        # H1 VWAP: mild penalty if price is on the wrong side
        if strategy == "call_credit" and hourly.price_vs_vwap > 0.2:
            penalties += 0.5  # Above VWAP, mild concern for call credit
        elif strategy == "put_credit" and hourly.price_vs_vwap < -0.2:
            penalties += 0.5

        # M5 RSI extreme: mild penalty
        if strategy == "call_credit" and m5.rsi > 75:
            penalties += 0.5
        elif strategy == "put_credit" and m5.rsi < 25:
            penalties += 0.5

        # ══════════════════════════════════════════════════════════
        # DECISION: Position sizing based on penalties
        # ══════════════════════════════════════════════════════════

        if penalties >= 4:
            # HARD REJECT: truly extreme conditions (very rare)
            signals.entry_allowed = False
            signals.size_multiplier = 0.0
            signals.entry_rejection_reason = "; ".join(rejection_reasons)
        elif penalties >= 3:
            # Hostile: minimum size
            signals.size_multiplier = 0.5
        elif penalties >= 2:
            # Adverse: moderately reduce size
            signals.size_multiplier = 0.75
        else:
            # Normal/Favorable: full size
            signals.size_multiplier = 1.0

        # Confidence score (inverse of penalties, normalized)
        signals.entry_confidence = round(max(0, 1.0 - penalties / 5.0), 2)

        # ── Strike guidance from ATR ──
        if daily.atr14 > 0:
            # Wider ATR → use smaller delta (further OTM)
            signals.suggested_delta = 0.12 if daily.atr_pct < 1.0 else 0.08
            signals.suggested_width = 2.0 if daily.atr_pct < 1.5 else 1.0

        return signals

    def compute_exit_signals(self, bars: List[IntradayBar],
                             current_idx: int,
                             entry_idx: int,
                             direction: str,
                             daily: DailySignals,
                             current_pnl_pct: float = 0.0) -> Tuple[bool, str]:
        """
        Check multi-TF exit triggers at each bar.

        CONSERVATIVE approach: only trigger exits when:
          1. Position is already losing (current_pnl_pct < 0), AND
          2. Multiple TF signals confirm the move against us

        This avoids cutting winners prematurely (the #1 mistake).

        Args:
            bars:           All bars for the day
            current_idx:    Current bar being evaluated
            entry_idx:      Bar where position was entered
            direction:      "put_credit" or "call_credit" or "iron_condor"
            daily:          Daily signals for ATR context
            current_pnl_pct: Current P&L as % of entry credit (negative = losing)

        Returns:
            (should_exit, reason)
        """
        # Give at least 15 bars before checking (let trade develop)
        if current_idx - entry_idx < 15:
            return False, ""

        # CRITICAL: Never trigger multi-TF exit if trade is profitable
        # The existing stop-loss and profit-target handle those cases
        if current_pnl_pct >= 0:
            return False, ""

        active = bars[:current_idx + 1]

        # Compute current VWAP
        hourly = self.compute_hourly(bars, current_idx)
        vwap = hourly.vwap

        # ── Exit 1: Strong VWAP break with volume (losing + confirmed) ──
        m5 = self.compute_m5(bars, current_idx, vwap)
        current_price = bars[current_idx].close

        if direction == "put_credit" and current_price < vwap:
            # Price below VWAP = bearish, bad for put credit
            # Require: sustained (4+ of last 6 bars below), high volume, AND losing
            below_count = sum(1 for b in active[-6:] if b.close < vwap)
            if below_count >= 4 and m5.volume_ratio > 1.5 and current_pnl_pct < -0.3:
                return True, "vwap_break_bearish"

        elif direction == "call_credit" and current_price > vwap:
            above_count = sum(1 for b in active[-6:] if b.close > vwap)
            if above_count >= 4 and m5.volume_ratio > 1.5 and current_pnl_pct < -0.3:
                return True, "vwap_break_bullish"

        # ── Exit 2: Extreme RSI + strong momentum against us ──
        # Only if already significantly losing
        if current_pnl_pct < -0.5:
            if direction == "put_credit" and m5.rsi < 20 and m5.momentum < -0.5:
                return True, "rsi_extreme_bearish"
            if direction == "call_credit" and m5.rsi > 80 and m5.momentum > 0.5:
                return True, "rsi_extreme_bullish"

        # ── Exit 3: Late-day losing position with adverse VWAP ──
        if len(active) > 0:
            mins_since_open = active[-1].minutes_since_open
            if mins_since_open > 300 and current_pnl_pct < -0.4:  # After 2:30 PM
                if direction == "put_credit" and current_price < vwap * 0.998:
                    return True, "late_day_adverse"
                if direction == "call_credit" and current_price > vwap * 1.002:
                    return True, "late_day_adverse"

        return False, ""

    # ─── Helper: resample bars ────────────────────────────────────

    def _resample_hourly(self, bars: List[IntradayBar]) -> List[dict]:
        """Resample to hourly OHLCV bars."""
        if not bars:
            return []

        hourly = []
        current_hour = None
        bucket = {"open": 0, "high": 0, "low": float('inf'), "close": 0, "volume": 0}

        for b in bars:
            hour = b.timestamp.hour if hasattr(b.timestamp, 'hour') else 0
            if current_hour is None:
                current_hour = hour
                bucket = {"open": b.open, "high": b.high, "low": b.low,
                          "close": b.close, "volume": b.volume}
            elif hour != current_hour:
                hourly.append(bucket)
                current_hour = hour
                bucket = {"open": b.open, "high": b.high, "low": b.low,
                          "close": b.close, "volume": b.volume}
            else:
                bucket["high"] = max(bucket["high"], b.high)
                bucket["low"] = min(bucket["low"], b.low)
                bucket["close"] = b.close
                bucket["volume"] += b.volume

        if bucket["close"] > 0:
            hourly.append(bucket)

        return hourly

    def _resample_m5(self, bars: List[IntradayBar]) -> List[dict]:
        """Resample to 5-min OHLCV bars (from 1-min or already 5-min)."""
        if not bars:
            return []

        # If bars are already ~5 min apart, just convert
        if len(bars) >= 2:
            gap = abs((bars[1].timestamp - bars[0].timestamp).total_seconds())
            if gap >= 250:  # Already 5-min bars
                return [{"open": b.open, "high": b.high, "low": b.low,
                         "close": b.close, "volume": b.volume} for b in bars]

        # Resample 1-min → 5-min
        m5 = []
        for i in range(0, len(bars), 5):
            chunk = bars[i:i + 5]
            if not chunk:
                break
            m5.append({
                "open": chunk[0].open,
                "high": max(b.high for b in chunk),
                "low": min(b.low for b in chunk),
                "close": chunk[-1].close,
                "volume": sum(b.volume for b in chunk),
            })
        return m5

    def _compute_rsi(self, closes: List[float], period: int = 14) -> float:
        """Compute RSI on a price series."""
        if len(closes) < period + 1:
            return 50.0

        changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains = [max(c, 0) for c in changes]
        losses = [abs(min(c, 0)) for c in changes]

        avg_gain = np.mean(gains[:period])
        avg_loss = np.mean(losses[:period])

        # Smoothed
        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period

        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return round(100 - (100 / (1 + rs)), 1)

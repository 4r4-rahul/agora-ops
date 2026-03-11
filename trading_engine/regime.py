"""
Day Regime Detector
====================
Classifies a trading day into a regime using the first N bars.

Regimes:
  STRONG_TREND   — High range, directional, clear trend
  MODERATE_TREND — Medium range, some directionality
  RANGE_BOUND    — Wide range but oscillating (no net direction)
  MIXED          — Low-medium range, ambiguous
  CHOPPY         — High direction-change frequency
  DEAD_FLAT      — Tiny range (<0.8%), no movement

Used by the ORB strategy to skip DEAD_FLAT/CHOPPY days where
directional 0DTE options have negative expected value.

Can detect regime by bar 30 (15 min after open) for live use.

From 129 trading days (Sep 2025 – Mar 2026):
  DEAD_FLAT:      57 days (44%) — avg range 0.61%, IV 0.06
  MIXED:          34 days (26%) — avg range 1.08%, IV 0.10
  RANGE_BOUND:    19 days (15%) — avg range 1.11%, IV 0.13
  MODERATE_TREND: 13 days (10%) — avg range 1.58%, IV 0.15
  STRONG_TREND:    4 days  (3%) — avg range 2.26%, IV 0.20
  CHOPPY:          2 days  (2%)
"""

from dataclasses import dataclass
from typing import Tuple, Dict

import numpy as np
import pandas as pd


@dataclass
class RegimeInfo:
    """Classification result for a trading day."""
    regime: str             # STRONG_TREND, MODERATE_TREND, RANGE_BOUND, MIXED, CHOPPY, DEAD_FLAT
    day_range_pct: float    # (High - Low) / Open as fraction
    intraday_vol: float     # Annualized intraday vol from 1m returns
    net_return_pct: float   # |Close - Open| / Open
    trend_ratio: float      # |net return| / range — how directional
    chop_rate: float        # Direction changes / bar count
    confidence: float       # 0-1 how clearly the regime is detected


class RegimeDetector:
    """
    Classifies trading days into regimes.

    Can operate in two modes:
    1. Full-day classification (for backtesting): uses all bars
    2. Early detection (for live trading): uses first 30 bars only

    Thresholds calibrated from 129 SPY trading days:
      - DEAD_FLAT boundary: 0.8% range (captures 44% of days)
      - STRONG_TREND: range > 1.5% AND trend_ratio > 0.6
      - MODERATE_TREND: range > 1.0% AND trend_ratio > 0.4
      - CHOPPY: chop_rate > 0.55
    """

    # ── Classification thresholds ───────────────────────────────
    DEAD_FLAT_RANGE = 0.008       # < 0.8% range = dead flat
    STRONG_TREND_RANGE = 0.015    # > 1.5% range for strong
    STRONG_TREND_RATIO = 0.60     # > 60% of range is net direction
    MODERATE_TREND_RANGE = 0.010  # > 1.0% range
    MODERATE_TREND_RATIO = 0.40   # > 40% directional
    CHOPPY_RATE = 0.55            # > 55% bars change direction
    RANGE_BOUND_RANGE = 0.010     # > 1.0% range (wide but not directional)

    def classify(self, bars: pd.DataFrame) -> RegimeInfo:
        """
        Classify a full day's bars into a regime.

        Args:
            bars: DataFrame with open/high/low/close columns.
                  Can be full day or just ORB bars for early detection.

        Returns:
            RegimeInfo with classification and statistics.
        """
        o = bars["open"].values
        h = bars["high"].values
        l = bars["low"].values
        c = bars["close"].values
        n = len(bars)

        if n < 10:
            return RegimeInfo(
                regime="UNKNOWN", day_range_pct=0, intraday_vol=0,
                net_return_pct=0, trend_ratio=0, chop_rate=0, confidence=0,
            )

        # ── Compute metrics ──────────────────────────────────────
        day_range = (h.max() - l.min()) / o[0]

        # Intraday vol from log returns
        window = min(n, 60)
        lr = np.log(c[1:window] / c[:window - 1])
        intraday_vol = float(np.std(lr, ddof=1) * np.sqrt(390 * 252)) if len(lr) > 1 else 0.0
        intraday_vol = max(intraday_vol, 0.01)

        # Net return (directional)
        net_return = abs(c[-1] - o[0]) / o[0]
        trend_ratio = net_return / day_range if day_range > 0 else 0.0

        # Chop rate: fraction of bars that reverse direction
        direction_changes = 0
        for i in range(2, n):
            if (c[i] - c[i - 1]) * (c[i - 1] - c[i - 2]) < 0:
                direction_changes += 1
        chop_rate = direction_changes / n if n > 2 else 0.0

        # ── Classify ─────────────────────────────────────────────
        regime, confidence = self._classify_metrics(
            day_range, intraday_vol, net_return, trend_ratio, chop_rate,
        )

        return RegimeInfo(
            regime=regime,
            day_range_pct=round(day_range, 6),
            intraday_vol=round(intraday_vol, 4),
            net_return_pct=round(net_return, 6),
            trend_ratio=round(trend_ratio, 4),
            chop_rate=round(chop_rate, 4),
            confidence=round(confidence, 3),
        )

    def classify_early(self, bars: pd.DataFrame, orb_bars: int = 30) -> RegimeInfo:
        """
        Classify using only the first `orb_bars` for live early detection.

        Uses the ORB range as a proxy for full-day range.
        Empirical: ORB30 range explains ~60% of full-day range variance.
        A narrow ORB30 strongly predicts a DEAD_FLAT day.

        Args:
            bars: Full day bars (we only use first orb_bars).
            orb_bars: Number of bars for ORB formation (default 30).

        Returns:
            RegimeInfo (early estimate, lower confidence).
        """
        n = len(bars)
        if n < orb_bars:
            return RegimeInfo(
                regime="UNKNOWN", day_range_pct=0, intraday_vol=0,
                net_return_pct=0, trend_ratio=0, chop_rate=0, confidence=0,
            )

        # Use only ORB window for classification
        orb_slice = bars.iloc[:orb_bars]
        info = self.classify(orb_slice)

        # Early detection is less confident (only 30 of 390 bars)
        # Scale thresholds: ORB range is typically 50-70% of full day range
        # A dead-flat ORB almost always means a dead-flat day
        info.confidence = round(info.confidence * 0.75, 3)

        return info

    def _classify_metrics(
        self,
        day_range: float,
        intraday_vol: float,
        net_return: float,
        trend_ratio: float,
        chop_rate: float,
    ) -> Tuple[str, float]:
        """Apply classification rules to computed metrics."""

        # Dead flat — tiny range, nothing to trade
        if day_range < self.DEAD_FLAT_RANGE:
            return "DEAD_FLAT", 0.90

        # Strong trend — big range, highly directional
        if trend_ratio > self.STRONG_TREND_RATIO and day_range > self.STRONG_TREND_RANGE:
            return "STRONG_TREND", 0.85

        # Moderate trend — decent range, somewhat directional
        if trend_ratio > self.MODERATE_TREND_RATIO and day_range > self.MODERATE_TREND_RANGE:
            return "MODERATE_TREND", 0.75

        # Choppy — lots of direction changes
        if chop_rate > self.CHOPPY_RATE:
            return "CHOPPY", 0.70

        # Range-bound — wide range but not directional
        if day_range > self.RANGE_BOUND_RANGE:
            return "RANGE_BOUND", 0.65

        # Mixed — everything else
        return "MIXED", 0.50

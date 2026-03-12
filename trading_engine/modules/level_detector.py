"""
Multi-Timeframe Support/Resistance Level Detector
=====================================================
Aggregates 1-minute bars into 5/15/30-minute candles and detects
the nearest support and resistance zones that serve as realistic targets.

The Problem (user's insight):
  The system uses fixed ATR-based targets (e.g., 3× ATR = $9.50 move).
  But price doesn't move in a vacuum — it bounces off real S/R levels.
  If the nearest resistance is $5 away, a $9.50 target is unrealistic
  because price will likely stall at that resistance.

The Solution:
  Detect REAL S/R levels from multi-timeframe candle structure:
    1. 5-min swing points (recent microstructure)
    2. 15-min swing points (intraday structure)
    3. 30-min swing points (session structure)
    4. VWAP (institutional anchor)
    5. Previous day high/low (key technical levels)
    6. Round numbers (psychological levels: $50, $100 for SPX)
    7. Opening range high/low (ORB boundaries)

  Then feed the nearest S/R as realistic targets into the contract picker
  so it can forward-price the option at WHERE price actually goes.

Usage:
    detector = MultiTFLevelDetector()
    levels = detector.detect_levels(day_bars_1m, current_idx, prev_day_high, prev_day_low)
    # levels.nearest_resistance = 5673.50 (VWAP above)
    # levels.nearest_support = 5662.00 (15-min swing low)
"""

import logging
import math
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class PriceLevel:
    """A single support or resistance level."""
    price: float              # The price level
    level_type: str           # "resistance" or "support"
    source: str               # Where it came from (e.g., "15m_swing", "VWAP", "prev_day_high")
    strength: float = 1.0     # Higher = more confirmed (# of touches or confluence)
    distance: float = 0.0     # Distance from current price (absolute)
    distance_pct: float = 0.0  # Distance as % of current price


@dataclass
class LevelMap:
    """All detected S/R levels with nearest ones highlighted."""
    current_price: float
    levels: List[PriceLevel] = field(default_factory=list)

    # Nearest levels (pre-sorted for quick access)
    nearest_resistance: Optional[PriceLevel] = None
    nearest_support: Optional[PriceLevel] = None
    second_resistance: Optional[PriceLevel] = None
    second_support: Optional[PriceLevel] = None

    # Aggregated zone strength
    resistance_zone_strength: float = 0.0  # Confluence near resistance
    support_zone_strength: float = 0.0     # Confluence near support


class MultiTFLevelDetector:
    """
    Detects intraday support and resistance levels from multi-timeframe candles.

    Takes 1-minute bars, resamples to 5/15/30 min, finds swing highs/lows,
    and combines with VWAP, prev day H/L, round numbers, and ORB levels.
    """

    def __init__(
        self,
        swing_lookback: int = 3,       # Bars each side to confirm swing
        round_number_increment: float = 5.0,  # $5 for SPX, $1 for SPY
        zone_tolerance_pct: float = 0.001,    # 0.1% — levels within this merge
        max_levels: int = 20,          # Max levels to return
    ):
        """
        Args:
            swing_lookback: Number of candles each side to confirm a swing point
            round_number_increment: Spacing of round number levels
            zone_tolerance_pct: How close two levels must be to merge (% of price)
            max_levels: Maximum number of levels to return
        """
        self.swing_lookback = swing_lookback
        self.round_inc = round_number_increment
        self.zone_tol_pct = zone_tolerance_pct
        self.max_levels = max_levels

    def detect_levels(
        self,
        day_bars_1m: pd.DataFrame,
        current_idx: int,
        prev_day_high: Optional[float] = None,
        prev_day_low: Optional[float] = None,
        prev_day_close: Optional[float] = None,
        vwap_arr: Optional[np.ndarray] = None,
        orb_high: Optional[float] = None,
        orb_low: Optional[float] = None,
    ) -> LevelMap:
        """
        Detect all S/R levels visible at current_idx.

        Args:
            day_bars_1m: Full day's 1-minute bars (OHLCV)
            current_idx: Current bar index (0-based, within day_bars_1m)
            prev_day_high/low/close: Previous session levels
            vwap_arr: Pre-computed VWAP array (from precompute_day_indicators)
            orb_high/orb_low: Opening range boundaries

        Returns:
            LevelMap with all detected levels sorted by distance
        """
        bars = day_bars_1m.iloc[:current_idx + 1]  # Only use bars we can see
        if len(bars) < 5:
            return LevelMap(current_price=0.0)

        current_price = bars["close"].iloc[-1]
        levels: List[PriceLevel] = []

        # ── 1. Multi-timeframe swing points ──────────────────────
        for tf_minutes, tf_label in [(5, "5m"), (15, "15m"), (30, "30m")]:
            tf_levels = self._detect_swing_levels(bars, tf_minutes, tf_label, current_price)
            levels.extend(tf_levels)

        # ── 2. VWAP ─────────────────────────────────────────────
        if vwap_arr is not None and current_idx < len(vwap_arr):
            vwap_price = vwap_arr[current_idx]
            if not np.isnan(vwap_price):
                vwap_type = "resistance" if vwap_price > current_price else "support"
                levels.append(PriceLevel(
                    price=round(vwap_price, 2),
                    level_type=vwap_type,
                    source="VWAP",
                    strength=2.0,  # VWAP is strong (institutional anchor)
                ))

        # ── 3. Previous day high/low ─────────────────────────────
        if prev_day_high is not None:
            pd_h_type = "resistance" if prev_day_high > current_price else "support"
            levels.append(PriceLevel(
                price=round(prev_day_high, 2),
                level_type=pd_h_type,
                source="prev_day_high",
                strength=2.5,  # Very strong level
            ))
        if prev_day_low is not None:
            pd_l_type = "support" if prev_day_low < current_price else "resistance"
            levels.append(PriceLevel(
                price=round(prev_day_low, 2),
                level_type=pd_l_type,
                source="prev_day_low",
                strength=2.5,
            ))

        # ── 4. Previous day close (pivot area) ──────────────────
        if prev_day_close is not None:
            pc_type = "resistance" if prev_day_close > current_price else "support"
            levels.append(PriceLevel(
                price=round(prev_day_close, 2),
                level_type=pc_type,
                source="prev_close",
                strength=1.5,
            ))

        # ── 5. ORB high/low ─────────────────────────────────────
        if orb_high is not None:
            orb_h_type = "resistance" if orb_high > current_price else "support"
            levels.append(PriceLevel(
                price=round(orb_high, 2),
                level_type=orb_h_type,
                source="ORB_high",
                strength=2.0,
            ))
        if orb_low is not None:
            orb_l_type = "support" if orb_low < current_price else "resistance"
            levels.append(PriceLevel(
                price=round(orb_low, 2),
                level_type=orb_l_type,
                source="ORB_low",
                strength=2.0,
            ))

        # ── 6. Round numbers ────────────────────────────────────
        round_levels = self._detect_round_numbers(current_price)
        levels.extend(round_levels)

        # ── 7. Session high/low so far ──────────────────────────
        session_high = bars["high"].max()
        session_low = bars["low"].min()
        if session_high > current_price * 1.0001:  # Not at exact high
            levels.append(PriceLevel(
                price=round(session_high, 2),
                level_type="resistance",
                source="session_high",
                strength=1.5,
            ))
        if session_low < current_price * 0.9999:  # Not at exact low
            levels.append(PriceLevel(
                price=round(session_low, 2),
                level_type="support",
                source="session_low",
                strength=1.5,
            ))

        # ── 8. Merge nearby levels and compute distances ─────────
        levels = self._merge_close_levels(levels, current_price)
        levels = self._compute_distances(levels, current_price)

        # ── 9. Build the LevelMap ────────────────────────────────
        result = LevelMap(current_price=current_price, levels=levels)

        # Find nearest resistance/support
        resistances = sorted(
            [l for l in levels if l.level_type == "resistance" and l.price > current_price],
            key=lambda l: l.distance,
        )
        supports = sorted(
            [l for l in levels if l.level_type == "support" and l.price < current_price],
            key=lambda l: l.distance,
        )

        if resistances:
            result.nearest_resistance = resistances[0]
            result.second_resistance = resistances[1] if len(resistances) > 1 else None
        if supports:
            result.nearest_support = supports[0]
            result.second_support = supports[1] if len(supports) > 1 else None

        # Zone confluence: how many levels cluster near the nearest S/R
        zone_radius = current_price * 0.002  # 0.2% zone
        if result.nearest_resistance:
            result.resistance_zone_strength = sum(
                l.strength for l in levels
                if abs(l.price - result.nearest_resistance.price) < zone_radius
            )
        if result.nearest_support:
            result.support_zone_strength = sum(
                l.strength for l in levels
                if abs(l.price - result.nearest_support.price) < zone_radius
            )

        return result

    def _detect_swing_levels(
        self,
        bars_1m: pd.DataFrame,
        tf_minutes: int,
        tf_label: str,
        current_price: float,
    ) -> List[PriceLevel]:
        """
        Resample 1-min bars to `tf_minutes` and detect swing highs/lows.

        A swing high: candle whose high is higher than the `swing_lookback`
        candles on each side.
        A swing low: candle whose low is lower than `swing_lookback` on each side.
        """
        if len(bars_1m) < tf_minutes * 3:
            return []

        # Resample to higher timeframe
        tf_bars = self._resample(bars_1m, tf_minutes)
        if len(tf_bars) < self.swing_lookback * 2 + 1:
            return []

        levels = []
        highs = tf_bars["high"].values
        lows = tf_bars["low"].values
        n = len(tf_bars)
        lb = self.swing_lookback

        for i in range(lb, n - lb):
            # Swing high: highest high in the window
            is_swing_high = True
            for j in range(1, lb + 1):
                if highs[i] <= highs[i - j] or highs[i] <= highs[i + j]:
                    is_swing_high = False
                    break

            if is_swing_high:
                level_type = "resistance" if highs[i] > current_price else "support"
                levels.append(PriceLevel(
                    price=round(highs[i], 2),
                    level_type=level_type,
                    source=f"{tf_label}_swing_high",
                    strength=1.0 + (tf_minutes / 15),  # Higher TF = stronger
                ))

            # Swing low: lowest low in the window
            is_swing_low = True
            for j in range(1, lb + 1):
                if lows[i] >= lows[i - j] or lows[i] >= lows[i + j]:
                    is_swing_low = False
                    break

            if is_swing_low:
                level_type = "support" if lows[i] < current_price else "resistance"
                levels.append(PriceLevel(
                    price=round(lows[i], 2),
                    level_type=level_type,
                    source=f"{tf_label}_swing_low",
                    strength=1.0 + (tf_minutes / 15),
                ))

        # Only keep the most recent N swings to avoid noise
        # Higher TF → keep more (they are rarer and more meaningful)
        max_per_tf = max(4, 8 // (tf_minutes // 5))
        if len(levels) > max_per_tf:
            levels = levels[-max_per_tf:]

        return levels

    def _detect_round_numbers(self, current_price: float) -> List[PriceLevel]:
        """Detect round number levels near current price."""
        levels = []
        inc = self.round_inc

        # Find nearest round number below and above
        base = math.floor(current_price / inc) * inc

        for offset in range(-2, 4):
            level_price = base + offset * inc
            if abs(level_price - current_price) < current_price * 0.005:  # Within 0.5%
                level_type = "resistance" if level_price > current_price else "support"
                levels.append(PriceLevel(
                    price=round(level_price, 2),
                    level_type=level_type,
                    source="round_number",
                    strength=0.8,  # Weaker than structural levels
                ))

        return levels

    def _resample(self, bars_1m: pd.DataFrame, tf_minutes: int) -> pd.DataFrame:
        """Resample 1-minute bars to higher timeframe."""
        # Use integer grouping instead of time-based resampling
        # to avoid timezone issues
        n = len(bars_1m)
        groups = [i // tf_minutes for i in range(n)]

        result = bars_1m.copy()
        result["_group"] = groups

        resampled = result.groupby("_group").agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum" if "volume" in result.columns else "first",
        })

        return resampled

    def _merge_close_levels(
        self,
        levels: List[PriceLevel],
        current_price: float,
    ) -> List[PriceLevel]:
        """Merge levels that are very close together into zones."""
        if not levels:
            return []

        zone_tol = current_price * self.zone_tol_pct
        sorted_levels = sorted(levels, key=lambda l: l.price)
        merged = []
        i = 0

        while i < len(sorted_levels):
            cluster = [sorted_levels[i]]
            j = i + 1
            while j < len(sorted_levels) and sorted_levels[j].price - cluster[0].price < zone_tol:
                cluster.append(sorted_levels[j])
                j += 1

            # Merge cluster: take price from strongest, sum strengths
            best = max(cluster, key=lambda l: l.strength)
            merged_level = PriceLevel(
                price=best.price,
                level_type=best.level_type,
                source="+".join(sorted(set(l.source for l in cluster))),
                strength=sum(l.strength for l in cluster),
            )
            merged.append(merged_level)
            i = j

        # Limit total levels
        merged.sort(key=lambda l: l.strength, reverse=True)
        return merged[:self.max_levels]

    def _compute_distances(
        self,
        levels: List[PriceLevel],
        current_price: float,
    ) -> List[PriceLevel]:
        """Compute distance from current price for each level."""
        for level in levels:
            level.distance = abs(level.price - current_price)
            level.distance_pct = level.distance / current_price if current_price > 0 else 0
        return levels

    def get_realistic_target(
        self,
        level_map: LevelMap,
        direction: str,  # "CALL" or "PUT"
        atr: float,
        max_atr_mult: float = 5.0,
    ) -> Optional[float]:
        """
        Get the most realistic target price based on detected S/R levels.

        Logic:
          For CALL (bullish): nearest resistance is the realistic target
          For PUT (bearish): nearest support is the realistic target

          Caps at max_atr_mult × ATR from current price (don't aim for the moon).
          If no S/R found, returns None (use default ATR-based target).

        Args:
            level_map: Detected S/R levels
            direction: "CALL" or "PUT"
            atr: Current ATR value
            max_atr_mult: Maximum ATR multiples for target distance

        Returns:
            Realistic target price, or None if no S/R-based target
        """
        if direction == "CALL":
            target_level = level_map.nearest_resistance
        else:
            target_level = level_map.nearest_support

        if target_level is None:
            return None

        # Cap at max ATR distance
        max_distance = atr * max_atr_mult
        if target_level.distance > max_distance:
            return None  # S/R too far — default ATR target is fine

        # Don't target levels that are too close (< 0.5× ATR — not worth the slippage)
        if target_level.distance < atr * 0.5:
            # Use second level instead
            if direction == "CALL" and level_map.second_resistance:
                if level_map.second_resistance.distance <= max_distance:
                    return level_map.second_resistance.price
            elif direction == "PUT" and level_map.second_support:
                if level_map.second_support.distance <= max_distance:
                    return level_map.second_support.price
            return None

        return target_level.price

    def format_levels(self, level_map: LevelMap, top_n: int = 8) -> str:
        """Format levels as a readable summary."""
        if not level_map.levels:
            return "No S/R levels detected"

        lines = [f"S/R Levels @ ${level_map.current_price:.2f}:"]

        # Resistance above (sorted closest first)
        resistances = sorted(
            [l for l in level_map.levels if l.price > level_map.current_price],
            key=lambda l: l.distance,
        )[:top_n // 2]

        for r in reversed(resistances):
            lines.append(f"  R  ${r.price:>10.2f}  (+{r.distance_pct:.2%})  "
                         f"[{r.source}] str={r.strength:.1f}")

        lines.append(f"  → ${level_map.current_price:>10.2f}  ← CURRENT")

        # Support below (sorted closest first)
        supports = sorted(
            [l for l in level_map.levels if l.price < level_map.current_price],
            key=lambda l: l.distance,
        )[:top_n // 2]

        for s in supports:
            lines.append(f"  S  ${s.price:>10.2f}  (-{s.distance_pct:.2%})  "
                         f"[{s.source}] str={s.strength:.1f}")

        if level_map.nearest_resistance:
            lines.append(f"\n  Nearest R: ${level_map.nearest_resistance.price:.2f} "
                         f"[{level_map.nearest_resistance.source}] "
                         f"zone_str={level_map.resistance_zone_strength:.1f}")
        if level_map.nearest_support:
            lines.append(f"  Nearest S: ${level_map.nearest_support.price:.2f} "
                         f"[{level_map.nearest_support.source}] "
                         f"zone_str={level_map.support_zone_strength:.1f}")

        return "\n".join(lines)

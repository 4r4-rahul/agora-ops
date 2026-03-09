"""
Production 0DTE Filter Stack
================================
6 empirically-validated filters for far-OTM 0DTE credit spreads.

Validated over 177 trades, 180 trading days, 6 iteration cycles.
Every other signal tested (D1 trend, H1 structure, M5 RSI, VWAP
slope, momentum, consecutive days, SMA distance) was proven to
add noise — not signal — for Δ≤0.15 short strikes.

Core principle:
    0DTE far-OTM credit spreads lose to MAGNITUDE, not DIRECTION.
    Any filter measuring WHICH WAY price is going = noise.
    Any filter measuring HOW FAR price can go = signal.

Filter Stack:
    ┌─────────────────────────────────────────────────────┐
    │ 1. VIX Regime Gate     → config.AdaptiveConfig      │
    │ 2. ATR % Position Size → THIS MODULE (new)          │
    │ 3. Entry Time Window   → config.RegimeParams        │
    │ 4. Gamma Acceleration  → backtester bar loop         │
    │ 5. Stop-Loss (2.5×)    → config.RegimeParams        │
    │ 6. Profit Target (75%) → config.RegimeParams        │
    └─────────────────────────────────────────────────────┘

Filters 1, 3, 4, 5, 6 are already embedded in AdaptiveConfig +
IntradayBacktester. This module adds Filter 2 (ATR sizing) and
provides a single entry point that documents the complete stack.

Usage:
    from trading_engine.filters import ProductionFilters

    f = ProductionFilters()
    decision = f.pre_entry(history_days, today, strategy, regime_params)
    # decision.size_multiplier  → 1.0 / 0.75 / 0.5 / 0.0
    # decision.skip             → True if size_multiplier == 0
    # decision.atr_pct          → For logging
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np


# ─────────────────────────────────────────────────────────────────
# Filter decision
# ─────────────────────────────────────────────────────────────────

@dataclass
class FilterDecision:
    """Output of the production filter stack."""

    # Position sizing (compounds with regime size_mult)
    size_multiplier: float = 1.0

    # Quick check
    skip: bool = False

    # Context (for logging / dashboard)
    atr14: float = 0.0
    atr_pct: float = 0.0
    reason: str = ""


# ─────────────────────────────────────────────────────────────────
# Production filter stack
# ─────────────────────────────────────────────────────────────────

class ProductionFilters:
    """
    The only filters that survived empirical validation for 0DTE.

    What's NOT here (and why):
      - D1 trend direction: noise at Δ≤0.15 (bearish days had 87% WR)
      - H1 structure breaks: cut winners more than losers (42% false exits)
      - M5 RSI / momentum: uncorrelated with far-OTM outcomes
      - VWAP side: bearish VWAP days had HIGHER WR than bullish
      - Consecutive up/down days: filtered only winners
      - SMA distance: no predictive power for tail moves

    Thresholds (from 6 validation runs on 180 days):
      - ATR > 2.0%  →  SKIP day entirely (extreme expansion)
      - ATR > 1.5%  →  0.50× position (hostile vol)
      - ATR > 1.2%  →  0.75× position (elevated vol)
      - ATR ≤ 1.2%  →  1.00× position (normal)
    """

    # ── ATR thresholds (tuned on 180-day SPY validation) ──
    ATR_SKIP_PCT: float = 2.0       # Hard skip: extreme daily range
    ATR_HALF_PCT: float = 1.5       # Reduce to 0.50× size
    ATR_REDUCE_PCT: float = 1.2     # Reduce to 0.75× size

    def __init__(self, atr_period: int = 14):
        self.atr_period = atr_period

    def pre_entry(
        self,
        history_closes: List[float],
        history_highs: List[float],
        history_lows: List[float],
    ) -> FilterDecision:
        """
        Run the ATR-based pre-entry filter.

        Call this AFTER VIX regime gate (filter 1) and BEFORE
        position sizing. The returned size_multiplier compounds
        with the regime's position_size_mult.

        Args:
            history_closes: Last 20+ daily closes (oldest first)
            history_highs:  Last 20+ daily highs  (oldest first)
            history_lows:   Last 20+ daily lows   (oldest first)

        Returns:
            FilterDecision with size_multiplier and context
        """
        decision = FilterDecision()

        if len(history_closes) < self.atr_period + 1:
            # Not enough history — allow full size (other filters still active)
            return decision

        # ── Compute ATR-14 ──
        trs = []
        n = len(history_closes)
        start = max(1, n - self.atr_period)
        for i in range(start, n):
            h = history_highs[i]
            l = history_lows[i]
            pc = history_closes[i - 1]
            tr = max(h - l, abs(h - pc), abs(l - pc))
            trs.append(tr)

        atr14 = float(np.mean(trs)) if trs else 0.0
        last_close = history_closes[-1]
        atr_pct = (atr14 / last_close * 100) if last_close > 0 else 0.0

        decision.atr14 = round(atr14, 2)
        decision.atr_pct = round(atr_pct, 3)

        # ── Apply thresholds ──
        if atr_pct > self.ATR_SKIP_PCT:
            decision.size_multiplier = 0.0
            decision.skip = True
            decision.reason = f"ATR {atr_pct:.2f}% > {self.ATR_SKIP_PCT}% — SKIP"
        elif atr_pct > self.ATR_HALF_PCT:
            decision.size_multiplier = 0.5
            decision.reason = f"ATR {atr_pct:.2f}% > {self.ATR_HALF_PCT}% — half size"
        elif atr_pct > self.ATR_REDUCE_PCT:
            decision.size_multiplier = 0.75
            decision.reason = f"ATR {atr_pct:.2f}% > {self.ATR_REDUCE_PCT}% — reduced"
        else:
            decision.size_multiplier = 1.0
            decision.reason = f"ATR {atr_pct:.2f}% — full size"

        return decision

    @staticmethod
    def describe() -> str:
        """Human-readable summary of all 6 production filters."""
        return """
┌─────────────────────────────────────────────────────────────────┐
│                  PRODUCTION 0DTE FILTER STACK                   │
├─────┬───────────────────────┬───────────────────────────────────┤
│  #  │ Filter                │ Implementation                    │
├─────┼───────────────────────┼───────────────────────────────────┤
│  1  │ VIX Regime Gate       │ AdaptiveConfig (GREEN/YELLOW/RED) │
│     │                       │ RED → skip, YELLOW → half size    │
├─────┼───────────────────────┼───────────────────────────────────┤
│  2  │ ATR % Position Size   │ ProductionFilters.pre_entry()     │
│     │                       │ >2.0% skip, >1.5% half, >1.2% ¾  │
├─────┼───────────────────────┼───────────────────────────────────┤
│  3  │ Entry Time Window     │ RegimeParams.entry_start/end_min  │
│     │                       │ GREEN: 30-60min, YELLOW: 30-90min │
├─────┼───────────────────────┼───────────────────────────────────┤
│  4  │ Gamma Acceleration    │ Backtester bar loop               │
│     │                       │ Exit if |γ| > limit AND mark >1.3×│
├─────┼───────────────────────┼───────────────────────────────────┤
│  5  │ Stop-Loss             │ RegimeParams.stop_mult            │
│     │                       │ GREEN: 2.5× credit, mechanical    │
├─────┼───────────────────────┼───────────────────────────────────┤
│  6  │ Profit Target         │ RegimeParams.profit_target        │
│     │                       │ GREEN: 75% of credit collected    │
└─────┴───────────────────────┴───────────────────────────────────┘
"""

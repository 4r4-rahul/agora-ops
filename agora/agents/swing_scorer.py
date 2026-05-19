"""
SwingCandidateScorer — deterministic 0-100 multi-factor swing setup score.

Factors (100 pts total):
  Technical      /40  — trend, dip quality, momentum, volume
  Catalyst       /30  — news freshness, sector rotation, unusual flow
  Fundamentals   /15  — market interest, sector leadership
  Options setup  /15  — IV rank (cheap options to buy), IV/HV ratio

Score ≥ 40  → pass to SwingJudgeAgent (Claude go/no-go)
Score ≥ 60  → Claude receives a positive prior (still judges independently)
Score < 40  → skip (not enough edge)
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass
class SwingFactors:
    """Breakdown of each scoring component."""
    technical: float = 0.0       # 0–40
    catalyst: float = 0.0        # 0–30
    fundamental: float = 0.0     # 0–15
    options_setup: float = 0.0   # 0–15
    total: float = 0.0
    direction: str = "neutral"   # "bullish" | "bearish" | "neutral"
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "technical":     round(self.technical, 1),
            "catalyst":      round(self.catalyst, 1),
            "fundamental":   round(self.fundamental, 1),
            "options_setup": round(self.options_setup, 1),
            "total":         round(self.total, 1),
            "direction":     self.direction,
            "notes":         self.notes,
        }


class SwingCandidateScorer:
    """
    Pure Python scoring — no LLM calls, runs in the scan hot-path.

    Input: MarketSnapshot fields (price, RSI, SMAs, ATR, HV, IV rank,
           volume), plus optional catalyst and market-interest signals.
    """

    MIN_SCORE_FOR_JUDGE = 40.0

    def score(
        self,
        ticker: str,
        price: float,
        rsi_14: float | None,
        sma_20: float | None,
        sma_50: float | None,
        sma_200: float | None,
        atr_14: float | None,
        hist_vol_30: float | None,
        iv_rank: float | None,
        volume: int | None,
        avg_volume: int | None,         # 20-day average volume
        iv_atm: float | None,           # ATM implied vol (annualised, e.g. 0.35 = 35%)
        catalyst_type: str | None,      # e.g. "8-K", "upgrade", "sector_rotation"
        catalyst_freshness_h: float | None,   # hours since catalyst
        market_interest_score: float | None,  # 0-10 from MarketInterestAgent
        sector_leader: bool = False,
    ) -> SwingFactors:
        """Score a ticker for swing suitability. Returns SwingFactors."""
        factors = SwingFactors()
        notes = factors.notes

        # ── 1. TECHNICAL (0-40) ─────────────────────────────────────
        tech = 0.0
        long_signals = 0
        short_signals = 0

        # Trend alignment: price vs SMAs
        if sma_20 and sma_50:
            if price > sma_20 > sma_50:
                tech += 8.0
                long_signals += 1
                notes.append("Bullish SMA stack (price>SMA20>SMA50)")
            elif price < sma_20 < sma_50:
                tech += 8.0
                short_signals += 1
                notes.append("Bearish SMA stack (price<SMA20<SMA50)")
            elif sma_200 and price > sma_200 and price < sma_20:
                tech += 5.0
                long_signals += 1
                notes.append("Dip within long-term uptrend (price<SMA20, above SMA200)")

        # RSI setup
        if rsi_14 is not None:
            if 28 <= rsi_14 <= 45:
                tech += 10.0
                long_signals += 1
                notes.append(f"RSI oversold/recovering ({rsi_14:.1f}) — long setup")
            elif 55 <= rsi_14 <= 72:
                tech += 10.0
                short_signals += 1
                notes.append(f"RSI overbought/fading ({rsi_14:.1f}) — short setup")
            elif rsi_14 < 28:
                tech += 7.0          # extreme oversold — may need a few days to base
                long_signals += 1
                notes.append(f"RSI extreme oversold ({rsi_14:.1f})")

        # Dip quality: price relative to SMA20 + ATR
        if sma_20 and atr_14 and atr_14 > 0:
            dip_threshold = sma_20 - 0.4 * atr_14
            rip_threshold = sma_20 + 0.4 * atr_14
            if price <= dip_threshold:
                tech += 10.0
                long_signals += 1
                notes.append(f"Price at ATR dip ({price:.2f} ≤ {dip_threshold:.2f})")
            elif price >= rip_threshold:
                tech += 10.0
                short_signals += 1
                notes.append(f"Price at ATR rip ({price:.2f} ≥ {rip_threshold:.2f})")
            else:
                tech += 3.0     # partial credit — near the zone

        # Volume confirmation
        if volume and avg_volume and avg_volume > 0:
            vol_ratio = volume / avg_volume
            if vol_ratio >= 1.8:
                tech += 7.0
                notes.append(f"Volume spike {vol_ratio:.1f}× avg")
            elif vol_ratio >= 1.3:
                tech += 4.0
                notes.append(f"Volume elevated {vol_ratio:.1f}× avg")

        # SMA200 long-term alignment bonus
        if sma_200:
            if price > sma_200 and long_signals >= 2:
                tech += 5.0
                notes.append("Above SMA200 — long-term uptrend confirmed")
            elif price < sma_200 and short_signals >= 2:
                tech += 5.0
                notes.append("Below SMA200 — long-term downtrend confirmed")

        factors.technical = min(40.0, tech)

        # ── 2. CATALYST (0-30) ──────────────────────────────────────
        cat = 0.0
        if catalyst_type:
            if catalyst_type in ("8-K", "material_event", "merger"):
                cat += 20.0
                notes.append(f"Material catalyst: {catalyst_type}")
            elif catalyst_type in ("upgrade", "initiation"):
                cat += 15.0
                notes.append(f"Analyst action: {catalyst_type}")
            elif catalyst_type in ("sector_rotation", "sector_leader"):
                cat += 10.0
                notes.append(f"Sector catalyst: {catalyst_type}")
            elif catalyst_type in ("earnings_beat", "guidance_raise"):
                cat += 12.0
                notes.append(f"Earnings catalyst: {catalyst_type}")
            else:
                cat += 5.0
                notes.append(f"Catalyst: {catalyst_type}")

            # Freshness bonus: full points if < 4h old, decaying to 0 at 48h
            if catalyst_freshness_h is not None:
                freshness_mult = max(0.0, 1.0 - catalyst_freshness_h / 48.0)
                cat *= freshness_mult
                if freshness_mult < 0.5:
                    notes.append(f"Catalyst stale ({catalyst_freshness_h:.0f}h old)")

        factors.catalyst = min(30.0, cat)

        # ── 3. FUNDAMENTALS (0-15) ──────────────────────────────────
        fund = 0.0
        if market_interest_score is not None:
            # Scale 0-10 score into 0-10 points
            fund += min(10.0, market_interest_score)
        if sector_leader:
            fund += 5.0
            notes.append("Sector leader")

        factors.fundamental = min(15.0, fund)

        # ── 4. OPTIONS SETUP (0-15) ─────────────────────────────────
        opts = 0.0
        if iv_rank is not None:
            if iv_rank <= 25:
                opts += 10.0        # IV very cheap — great time to buy options
                notes.append(f"IV cheap (IVR={iv_rank:.0f})")
            elif iv_rank <= 45:
                opts += 6.0
                notes.append(f"IV moderate (IVR={iv_rank:.0f})")
            else:
                opts += 2.0         # IV elevated — options are expensive to buy
                notes.append(f"IV elevated (IVR={iv_rank:.0f}) — options pricey")

        # IV vs HV: if IV below realized vol (rare), options are extremely cheap
        if iv_atm and hist_vol_30 and hist_vol_30 > 0:
            iv_hv_ratio = iv_atm / hist_vol_30
            if iv_hv_ratio < 0.9:
                opts += 5.0
                notes.append(f"IV below realized vol (ratio={iv_hv_ratio:.2f}) — exceptional")
            elif iv_hv_ratio < 1.1:
                opts += 3.0

        factors.options_setup = min(15.0, opts)

        # ── Direction consensus ──────────────────────────────────────
        if long_signals > short_signals:
            factors.direction = "bullish"
        elif short_signals > long_signals:
            factors.direction = "bearish"
        else:
            factors.direction = "neutral"

        factors.total = (
            factors.technical + factors.catalyst +
            factors.fundamental + factors.options_setup
        )
        return factors

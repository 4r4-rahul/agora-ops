"""
Structured Disagreement Resolver — NOT a debate, a principled synthesis.

Three independent signals are computed without seeing each other:
  A. Macro signal (from MacroSynthesizer)
  B. Microstructure signal (GEX + IV premium + flow)
  C. Catalyst signal (from CatalystDiscoveryAgent / EarningsTranscriptAgent)

The resolver maps their agreement/disagreement into a size multiplier:
  - All three agree:               size_multiplier = 1.5 (max conviction)
  - Two agree, one neutral:        size_multiplier = 1.0 (standard)
  - Two agree, one disagrees:      size_multiplier = 0.5 (conflict — cut size)
  - All three disagree or neutral: size_multiplier = 0.0 (no trade)

This is deterministic rules, NOT another LLM call.
Claude is NOT used here — the inputs are already Claude-synthesized where needed.

The resolver also enforces:
  - Minimum conviction gate (total_score ≥ 55 for standard, ≥ 70 for 1.5x)
  - Direction consistency (macro and catalyst must agree on bull/bear)
  - Regime override: crisis regime → size_multiplier = 0 regardless
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class SignalInput:
    """One signal leg for the resolver."""
    source: str           # "macro" | "microstructure" | "catalyst"
    direction: str        # "bullish" | "bearish" | "neutral"
    confidence: float     # 0.0–1.0
    active: bool = True   # False = signal not present (treated as neutral)


class DisagreementResolver:
    """
    Deterministic 3-signal resolver → size_multiplier ∈ {0.0, 0.5, 1.0, 1.5}.
    Pure logic, no LLM calls.
    """

    # Minimum composite confidence for each multiplier tier
    _THRESHOLDS = {
        1.5: 0.70,
        1.0: 0.55,
        0.5: 0.40,
    }

    def resolve(
        self,
        macro: SignalInput,
        microstructure: SignalInput,
        catalyst: SignalInput | None,
        regime: str = "normal",
        total_conviction: float = 0.0,
    ) -> dict[str, Any]:
        """
        Resolve three signals into a size multiplier and gate.

        Returns dict:
          size_multiplier: float
          gate: "high" | "standard" | "low" | "no_trade"
          reason: str
          direction: str  (agreed direction or "neutral")
        """
        # Crisis kill switch
        if regime == "crisis":
            return {
                "size_multiplier": 0.0,
                "gate": "no_trade",
                "reason": "Crisis regime — all positions suspended",
                "direction": "neutral",
            }

        signals = [macro, microstructure]
        if catalyst and catalyst.active:
            signals.append(catalyst)

        # Map to canonical directions
        directions = [s.direction for s in signals if s.active]
        if not directions:
            return {"size_multiplier": 0.0, "gate": "no_trade",
                    "reason": "No active signals", "direction": "neutral"}

        bullish_count = directions.count("bullish")
        bearish_count = directions.count("bearish")
        neutral_count = directions.count("neutral")
        n = len(directions)

        # Determine consensus direction
        if bullish_count > bearish_count and bullish_count >= n // 2 + 1:
            consensus_dir = "bullish"
        elif bearish_count > bullish_count and bearish_count >= n // 2 + 1:
            consensus_dir = "bearish"
        else:
            consensus_dir = "neutral"

        # Count agreers (non-neutral signals matching consensus)
        if consensus_dir == "neutral":
            agreers = 0
            disagreers = 0
        else:
            agreers = directions.count(consensus_dir)
            disagreers = (bullish_count + bearish_count) - agreers

        # Weighted confidence (macro: 0.40, micro: 0.35, catalyst: 0.25)
        weights = {"macro": 0.40, "microstructure": 0.35, "catalyst": 0.25}
        weighted_conf = 0.0
        weight_sum = 0.0
        for sig in signals:
            if not sig.active:
                continue
            w = weights.get(sig.source, 0.25)
            weighted_conf += sig.confidence * w
            weight_sum += w
        avg_conf = weighted_conf / weight_sum if weight_sum > 0 else 0.0

        # Map agreement to multiplier
        if consensus_dir == "neutral" or agreers < 2:
            multiplier = 0.0
            gate = "no_trade"
            reason = f"No consensus direction (bull={bullish_count}, bear={bearish_count})"
        elif disagreers >= 2:
            multiplier = 0.0
            gate = "no_trade"
            reason = f"Strong disagreement ({disagreers}/{n} signals oppose)"
        elif disagreers == 1:
            multiplier = 0.5
            gate = "low"
            reason = f"{agreers}/{n} signals agree {consensus_dir}, 1 disagrees — half size"
        elif agreers == n and n >= 3:
            if avg_conf >= self._THRESHOLDS[1.5] and total_conviction >= 70:
                multiplier = 1.5
                gate = "high"
                reason = f"Full consensus ({n}/{n}), conf={avg_conf:.2f} — high conviction"
            else:
                multiplier = 1.0
                gate = "standard"
                reason = f"Full consensus ({n}/{n}) but confidence below high threshold"
        else:
            # 2/2 or 2/3 agreement, no disagreement (neutral third)
            multiplier = 1.0
            gate = "standard"
            reason = f"{agreers}/{n} signals agree {consensus_dir} — standard size"

        # Hard conviction floor for non-zero trades
        if multiplier > 0 and total_conviction < 40:
            multiplier = 0.0
            gate = "no_trade"
            reason = f"Conviction score {total_conviction:.0f} below minimum 40"

        # High vol regime: cut multiplier by 25% (extra caution)
        if regime == "high_volatility" and multiplier > 0:
            multiplier = round(multiplier * 0.75, 2)
            reason += " | -25% high vol haircut"

        return {
            "size_multiplier": multiplier,
            "gate": gate,
            "reason": reason,
            "direction": consensus_dir,
            "agreers": agreers,
            "total_signals": n,
            "avg_confidence": round(avg_conf, 3),
        }

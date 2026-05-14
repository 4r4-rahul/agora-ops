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
import math
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

    Signal weights and conviction thresholds are conditioned on the current
    vol/macro regime so that the right signal type is prioritised in each
    market environment.
    """

    def __init__(self) -> None:
        self._session_calls: int = 0
        self._session_no_trades: int = 0
        self._session_gates: dict[str, int] = {}

    def get_session_stats(self) -> dict:
        """Return session resolver statistics for CTO briefing."""
        return {
            "total_resolutions": self._session_calls,
            "no_trade_count": self._session_no_trades,
            "gate_distribution": dict(self._session_gates),
        }

    # ── Regime-conditional signal weights ─────────────────────────────────
    # risk_on / low_volatility: macro dominates — calm trending market
    # risk_off / high_volatility: micro (GEX+flow) dominates — fast-moving stress
    # neutral / normal: balanced baseline
    _WEIGHTS_BY_REGIME: dict[str, dict[str, float]] = {
        "risk_on":        {"macro": 0.50, "microstructure": 0.25, "catalyst": 0.25},
        "low_volatility": {"macro": 0.50, "microstructure": 0.25, "catalyst": 0.25},
        "risk_off":       {"macro": 0.25, "microstructure": 0.45, "catalyst": 0.30},
        "high_volatility":{"macro": 0.25, "microstructure": 0.45, "catalyst": 0.30},
        "neutral":        {"macro": 0.40, "microstructure": 0.35, "catalyst": 0.25},
        "normal":         {"macro": 0.40, "microstructure": 0.35, "catalyst": 0.25},
    }
    _DEFAULT_WEIGHTS = {"macro": 0.40, "microstructure": 0.35, "catalyst": 0.25}

    # ── Regime-conditional conviction thresholds ───────────────────────────
    # Stress regimes: raise bars — need more certainty before sizing up
    # Calm regimes:  lower bars slightly — conditions are predictable
    _THRESHOLDS_BY_REGIME: dict[str, dict[float, float]] = {
        "risk_on":        {1.5: 0.65, 1.0: 0.50, 0.5: 0.35},
        "low_volatility": {1.5: 0.65, 1.0: 0.50, 0.5: 0.35},
        "risk_off":       {1.5: 0.80, 1.0: 0.65, 0.5: 0.50},
        "high_volatility":{1.5: 0.80, 1.0: 0.65, 0.5: 0.50},
        "neutral":        {1.5: 0.70, 1.0: 0.55, 0.5: 0.40},
        "normal":         {1.5: 0.70, 1.0: 0.55, 0.5: 0.40},
    }
    _DEFAULT_THRESHOLDS = {1.5: 0.70, 1.0: 0.55, 0.5: 0.40}

    # ── Regime-conditional size multiplier haircuts ────────────────────────
    _HAIRCUT_BY_REGIME: dict[str, float] = {
        "risk_off":        0.75,
        "high_volatility": 0.75,
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
          regime_weights: dict  (weights actually used)
        """
        # Crisis kill switch
        if regime == "crisis":
            return {
                "size_multiplier": 0.0,
                "gate": "no_trade",
                "reason": "Crisis regime — all positions suspended",
                "direction": "neutral",
                "regime_weights": {},
            }

        weights    = self._WEIGHTS_BY_REGIME.get(regime, self._DEFAULT_WEIGHTS)
        thresholds = self._THRESHOLDS_BY_REGIME.get(regime, self._DEFAULT_THRESHOLDS)
        haircut    = self._HAIRCUT_BY_REGIME.get(regime, 1.0)

        signals = [macro, microstructure]
        if catalyst and catalyst.active:
            signals.append(catalyst)

        directions = [s.direction for s in signals if s.active]
        if not directions:
            return {"size_multiplier": 0.0, "gate": "no_trade",
                    "reason": "No active signals", "direction": "neutral",
                    "regime_weights": weights}

        bullish_count = directions.count("bullish")
        bearish_count = directions.count("bearish")
        n = len(directions)

        # Determine consensus direction
        if bullish_count > bearish_count and bullish_count >= n // 2 + 1:
            consensus_dir = "bullish"
        elif bearish_count > bullish_count and bearish_count >= n // 2 + 1:
            consensus_dir = "bearish"
        else:
            consensus_dir = "neutral"

        if consensus_dir == "neutral":
            agreers = disagreers = 0
        else:
            agreers    = directions.count(consensus_dir)
            disagreers = (bullish_count + bearish_count) - agreers

        # Regime-weighted composite confidence
        weighted_conf = 0.0
        weight_sum    = 0.0
        for sig in signals:
            if not sig.active:
                continue
            w = weights.get(sig.source, 0.25)
            weighted_conf += sig.confidence * w
            weight_sum    += w
        avg_conf = weighted_conf / weight_sum if weight_sum > 0 else 0.0

        # Dynamic minimum agreers: 60% of active signals, rounded up
        min_agreers = math.ceil(n * 0.6)

        # Map agreement to multiplier using regime-conditional thresholds
        if consensus_dir == "neutral" or agreers < min_agreers:
            multiplier = 0.0
            gate   = "no_trade"
            reason = f"No consensus direction (bull={bullish_count}, bear={bearish_count})"
        elif disagreers >= 2:
            multiplier = 0.0
            gate   = "no_trade"
            reason = f"Strong disagreement ({disagreers}/{n} signals oppose)"
        elif disagreers == 1:
            multiplier = 0.5
            gate   = "low"
            reason = f"{agreers}/{n} agree {consensus_dir}, 1 disagrees — half size"
        elif agreers >= min_agreers:
            if avg_conf >= thresholds[1.5] and total_conviction >= 70:
                multiplier = 1.5
                gate   = "high"
                reason = f"{agreers}/{n} agree {consensus_dir}, conf={avg_conf:.2f} — high conviction"
            else:
                multiplier = 1.0
                gate   = "standard"
                reason = f"{agreers}/{n} agree {consensus_dir} — standard size"
        else:
            multiplier = 1.0
            gate   = "standard"
            reason = f"{agreers}/{n} agree {consensus_dir} — standard size"

        # Hard conviction floor
        if multiplier > 0 and total_conviction < 40:
            multiplier = 0.0
            gate   = "no_trade"
            reason = f"Conviction score {total_conviction:.0f} below minimum 40"

        # Regime haircut (risk_off / high_volatility only)
        if multiplier > 0 and haircut < 1.0:
            multiplier = round(multiplier * haircut, 2)
            reason += f" | {int((1 - haircut) * 100)}% {regime} haircut"

        # Session tracking
        self._session_calls += 1
        self._session_gates[gate] = self._session_gates.get(gate, 0) + 1
        if gate == "no_trade":
            self._session_no_trades += 1

        return {
            "size_multiplier": multiplier,
            "gate": gate,
            "reason": reason,
            "direction": consensus_dir,
            "agreers": agreers,
            "total_signals": n,
            "avg_confidence": round(avg_conf, 3),
            "regime_weights": weights,
        }

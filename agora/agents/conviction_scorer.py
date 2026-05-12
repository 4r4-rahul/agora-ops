"""
ConvictionScorer — aggregates all signals into a 0–100 conviction score.

Scoring breakdown (100 points total):
  Vol Premium   /30  — IV premium screen (days above threshold + premium ratio)
  GEX           /20  — gamma exposure regime alignment
  Regime        /20  — vol regime + macro stance alignment
  Event         /15  — FOMC drift, CPI condor, post-earnings skew
  Macro         /5   — macro stance override (risk_on / risk_off)
  Smart Money   /5   — insider cluster or 13D activist signal
  Info Speed    /5   — catalyst freshness (filed < 2h ago = full points)

Gates:
  ≥ 70 → "high"       (1.5x size if resolver agrees)
  55–69 → "standard"  (1.0x size)
  40–54 → "low"       (0.5x size)
  < 40  → "no_trade"
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from ..core.models import (
    ConvictionScore,
    GexRegime,
    IvPremiumSignal,
    GexSignal,
    VolRegimeSignal,
    EventSignal,
    Catalyst,
    StrategyPillar,
)
from .macro_synthesizer import MacroContext

logger = logging.getLogger(__name__)


class ConvictionScorer:
    """
    Pure deterministic scoring — no LLM calls.
    All inputs are already computed by their respective signal generators.
    """

    def score(
        self,
        ticker: str,
        session_id: str,
        iv_premium: IvPremiumSignal | None,
        gex: GexSignal | None,
        regime: VolRegimeSignal | None,
        macro: MacroContext | None,
        event: EventSignal | None,
        catalyst: Catalyst | None,
        smart_money: Catalyst | None = None,
    ) -> ConvictionScore:

        vol_score    = self._score_vol_premium(iv_premium)
        gex_score    = self._score_gex(gex, iv_premium)
        regime_score = self._score_regime(regime, macro)
        event_score  = self._score_event(event)
        macro_score  = self._score_macro(macro)
        sm_score     = self._score_smart_money(smart_money)
        info_score   = self._score_info_speed(catalyst)

        total = (
            vol_score + gex_score + regime_score
            + event_score + macro_score + sm_score + info_score
        )
        total = round(min(100.0, total), 2)

        gate = (
            "high"     if total >= 70 else
            "standard" if total >= 55 else
            "low"      if total >= 40 else
            "no_trade"
        )

        # Primary pillar: which signal contributes most?
        pillar = self._dominant_pillar(
            vol_score, gex_score, event_score, catalyst, smart_money
        )

        return ConvictionScore(
            session_id=session_id,
            ticker=ticker,
            total_score=total,
            vol_premium_score=vol_score,
            gex_score=gex_score,
            regime_score=regime_score,
            event_score=event_score,
            macro_score=macro_score,
            smart_money_score=sm_score,
            info_speed_score=info_score,
            gate=gate,
            pillar=pillar,
            reasoning=self._build_reasoning(
                vol_score, gex_score, regime_score, event_score,
                macro_score, sm_score, info_score, gate,
            ),
        )

    # ── Component scorers ──────────────────────────────────────────

    def _score_vol_premium(self, iv: IvPremiumSignal | None) -> float:
        """Max 30 points — core edge for selling premium."""
        if not iv:
            return 10.0  # neutral default (market open, no data yet)

        if not iv.signal_active:
            # Partial credit based on ratio and days
            days_credit = min(iv.days_above_threshold / 15.0, 1.0) * 10.0
            ratio_credit = max(0.0, min(iv.premium_ratio / 0.25, 1.0)) * 5.0
            return round(days_credit + ratio_credit, 2)

        # Signal active: scale by strength
        ratio_bonus = min(max(0.0, iv.premium_ratio - 0.25) * 20, 10.0)  # extra up to +10 for high premium
        return round(min(30.0, 20.0 + ratio_bonus), 2)

    def _score_gex(self, gex: GexSignal | None, iv: IvPremiumSignal | None) -> float:
        """Max 20 points — GEX regime alignment with strategy."""
        if not gex:
            return 8.0

        if gex.regime == GexRegime.NEGATIVE:
            # Negative GEX → trending market → debit spreads work, credit spreads riskier
            # Give full score if we're doing directional plays
            return 18.0
        elif gex.regime == GexRegime.POSITIVE:
            # Positive GEX → mean-reversion → ideal for credit spreads
            return 20.0 if (iv and iv.signal_active) else 14.0
        else:
            return 10.0

    def _score_regime(self, regime: VolRegimeSignal | None, macro: MacroContext | None) -> float:
        """Max 20 points — regime supports the strategy."""
        if not regime:
            return 8.0

        regime_str = regime.regime.value if hasattr(regime.regime, "value") else str(regime.regime)
        conf = regime.confidence

        base = {
            "low_volatility":  12.0,   # low IV = harder to sell premium, partial credit
            "normal":          16.0,
            "high_volatility": 18.0,   # elevated IV = prime for credit spreads
            "crisis":          5.0,    # crisis = reduce everything
        }.get(regime_str, 10.0)

        conf_bonus = (conf - 0.5) * 10.0  # ±5 based on confidence

        # Macro alignment bonus
        macro_bonus = 0.0
        if macro:
            if macro.vol_selling_ok and regime_str in ("high_volatility", "normal"):
                macro_bonus = 2.0
            elif macro.macro_stance == "risk_off" and regime_str == "crisis":
                macro_bonus = -5.0

        return round(min(20.0, max(0.0, base + conf_bonus + macro_bonus)), 2)

    def _score_event(self, event: EventSignal | None) -> float:
        """Max 15 points — active event pattern."""
        if not event:
            return 0.0
        base = event.confidence * 15.0
        # Freshness: closer to event = more alpha
        days = getattr(event, "days_to_event", 5)
        freshness = max(0.0, 1.0 - (days - 1) / 5.0)
        return round(min(15.0, base * (0.7 + 0.3 * freshness)), 2)

    def _score_macro(self, macro: MacroContext | None) -> float:
        """Max 5 points — global macro gate."""
        if not macro:
            return 2.5
        if macro.macro_stance == "risk_on" and macro.vol_selling_ok:
            return round(5.0 * macro.confidence, 2)
        elif macro.macro_stance == "neutral":
            return 2.5
        else:
            return round(max(0.0, 5.0 * (1.0 - macro.confidence)), 2)

    def _score_smart_money(self, sm: Catalyst | None) -> float:
        """Max 5 points — insider/activist conviction boost."""
        if not sm:
            return 0.0
        return {
            "strong":   5.0,
            "moderate": 3.0,
            "weak":     1.0,
        }.get(sm.strength, 0.0)

    def _score_info_speed(self, catalyst: Catalyst | None) -> float:
        """Max 5 points — catalyst freshness (decay over 4h)."""
        if not catalyst:
            return 0.0
        age_hours = (
            datetime.now(tz=timezone.utc) - catalyst.filing_time
        ).total_seconds() / 3600.0
        # Full 5 points at 0h, 0 points at 4h
        return round(max(0.0, 5.0 * (1.0 - age_hours / 4.0)), 2)

    # ── Helpers ────────────────────────────────────────────────────

    def _dominant_pillar(
        self,
        vol_score: float,
        gex_score: float,
        event_score: float,
        catalyst: Catalyst | None,
        smart_money: Catalyst | None,
    ) -> StrategyPillar | None:
        if catalyst:
            return StrategyPillar.CATALYST
        if smart_money:
            return StrategyPillar.SMART_MONEY
        if event_score >= 10:
            return StrategyPillar.EVENT_FOMC   # approximation — event type resolved in strategies
        if vol_score >= 20:
            return StrategyPillar.VOL_PREMIUM
        if gex_score >= 16:
            return StrategyPillar.DIRECTIONAL
        return StrategyPillar.VOL_PREMIUM

    def _build_reasoning(
        self,
        vol: float, gex: float, regime: float, event: float,
        macro: float, sm: float, info: float, gate: str,
    ) -> str:
        parts = []
        if vol >= 20:
            parts.append(f"strong IV premium ({vol:.0f}/30)")
        elif vol >= 10:
            parts.append(f"moderate IV premium ({vol:.0f}/30)")
        if gex >= 16:
            parts.append(f"favorable GEX regime ({gex:.0f}/20)")
        if event >= 8:
            parts.append(f"event catalyst ({event:.0f}/15)")
        if sm > 0:
            parts.append(f"smart money signal ({sm:.0f}/5)")
        if info > 2:
            parts.append(f"fresh catalyst ({info:.0f}/5)")
        total = vol + gex + regime + event + macro + sm + info
        return f"Gate: {gate} | Total: {total:.0f}/100 | " + (", ".join(parts) if parts else "no dominant signal")

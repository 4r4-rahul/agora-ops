"""
RiskManagerAgent — deterministic + Claude hybrid with absolute VETO power.

Subscribes to: STRATEGY_CANDIDATES
Publishes:     RISK_ASSESSMENT

Hard rules are applied deterministically FIRST. Claude is only called for
nuanced portfolio-level analysis on trades that pass the hard gates.
Any VETO from this agent stops the trade regardless of other agents' opinions.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from ..core.models.agent import AgentMessage, AgentTopic
from ..core.models.risk import LiquidityCheck, PositionSizing, RiskAssessment, VetoReason
from ..core.models.trade import TradeRecommendation
from .base import BaseAgent

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a systematic risk manager for an options trading fund.
Your job is to catch risks that the strategy agent missed. You have veto power.

You are looking for:
1. Correlation risk — is this position too similar to existing positions?
2. Event risk — is there a macro event (FOMC, CPI, NFP) before expiration?
3. Liquidity risk — can we actually get filled at the stated price?
4. IV environment mismatch — is the strategy appropriate for current vol regime?
5. Position concentration — does this create too much directional exposure?
6. Tail risk — extreme scenarios where max loss exceeds stated max loss (pin risk, gap risk)

Your veto reasons must be specific and actionable, not generic warnings.
If you approve, explain WHY the trade is sound from a risk perspective.
"""


class RiskOutput(BaseModel):
    approved: bool
    veto_reasons: list[str] = Field(default_factory=list)
    portfolio_delta: float = Field(default=0.0)
    correlation_risk: str = Field(default="low", description="low | moderate | high")
    tail_risk_notes: str = ""
    liquidity_assessment: str = ""
    summary: str = Field(..., min_length=30)


class RiskManagerAgent(BaseAgent):
    name = "risk_manager"
    subscriptions = [AgentTopic.STRATEGY_CANDIDATES]

    async def handle(self, message: AgentMessage) -> None:
        session_id = message.session_id
        candidates_raw = message.payload.get("candidates", [])

        if not candidates_raw:
            self._log.warning("[%s] no strategy candidates to assess", session_id)
            return

        # Assess first candidate (primary recommendation)
        rec = TradeRecommendation.model_validate(candidates_raw[0])
        ticker = rec.ticker

        self._log.info("[%s] risk-assessing %s %s", session_id, ticker, rec.strategy)

        assessment = RiskAssessment(
            session_id=session_id,
            ticker=ticker,
            approved=True,
            max_loss_dollars=rec.max_loss_dollars,
            max_gain_dollars=rec.max_gain_dollars,
            reward_risk_ratio=rec.reward_risk_ratio,
        )

        # ── Hard Rules (deterministic, always applied) ─────────────
        self._check_strategy_tier(rec, assessment)
        self._check_position_size(rec, assessment)
        self._check_reward_risk(rec, assessment)
        self._check_iv_rank(rec, assessment)
        self._check_earnings_risk(rec, assessment)
        self._check_daily_loss_budget(session_id, assessment)

        # ── Position sizing (Kelly-adjusted when performance data available) ──
        kelly_fraction = self._load_kelly_fraction()
        sizing = PositionSizing.calculate(
            ticker=ticker,
            max_loss_per_contract=rec.max_loss_dollars / max(rec.contracts, 1),
            account_size=self._settings.account_size,
            max_position_pct=self._settings.max_position_size_pct,
            kelly_fraction=kelly_fraction,
        )
        assessment.position_sizing = sizing

        # ── Claude for nuanced portfolio risk ─────────────────────
        if assessment.approved:
            try:
                claude_risk = await self._call_claude_structured(
                    system_prompt=_SYSTEM_PROMPT,
                    user_message=self._build_prompt(rec),
                    output_schema=RiskOutput,
                    tool_name="structured_output",
                    max_tokens=2048,
                )
                assessment.portfolio_delta = claude_risk.portfolio_delta
                assessment.correlation_risk = claude_risk.correlation_risk
                assessment.summary = claude_risk.summary

                if not claude_risk.approved:
                    for reason in claude_risk.veto_reasons:
                        assessment.add_veto(
                            code="CLAUDE_VETO",
                            description=reason,
                            severity="reject",
                        )
            except Exception as exc:
                self._log.error("[%s] Claude risk assessment failed: %s", session_id, exc)
                assessment.summary = (
                    f"Hard-rule checks passed. Claude assessment failed: {exc}. "
                    "Proceed with caution."
                )
        else:
            assessment.summary = (
                f"REJECTED by hard rules: "
                + "; ".join(v.description for v in assessment.veto_reasons)
            )

        await self._state.update(
            session_id,
            risk_assessment=assessment.model_dump(mode="json"),
        )

        await self.publish(
            AgentTopic.RISK_ASSESSMENT,
            session_id=session_id,
            payload=assessment.model_dump(mode="json"),
        )

        self._log.info(
            "[%s] risk assessment: approved=%s vetos=%d",
            session_id,
            assessment.approved,
            len(assessment.veto_reasons),
        )

    @staticmethod
    def _load_kelly_fraction() -> float:
        """Read half_kelly from performance_feedback.json if available."""
        import json
        from pathlib import Path
        feedback_path = Path("./performance_feedback.json")
        if not feedback_path.exists():
            return 0.0
        try:
            data = json.loads(feedback_path.read_text())
            return float(data.get("half_kelly", 0.0))
        except Exception:
            return 0.0

    # ── Hard Rules ────────────────────────────────────────────────

    # Strategies forbidden at each tier (hard block, no override)
    _FORBIDDEN_BY_TIER: dict[str, set[str]] = {
        "starter":      {"iron_condor", "iron_butterfly", "calendar_spread", "covered_call"},
        "intermediate": {"calendar_spread", "iron_butterfly"},
        "advanced":     set(),
        "professional": set(),
    }

    def _check_strategy_tier(
        self, rec: TradeRecommendation, assessment: RiskAssessment
    ) -> None:
        tier = self._settings.account_tier
        forbidden = self._FORBIDDEN_BY_TIER.get(tier, set())
        if rec.strategy.value in forbidden:
            assessment.add_veto(
                code="STRATEGY_NOT_APPROVED_FOR_TIER",
                description=(
                    f"{rec.strategy.value} is not suitable for your account tier "
                    f"({tier}, ${self._settings.account_size:,.0f}). "
                    f"At this tier, forbidden strategies are: {', '.join(sorted(forbidden))}. "
                    "Use a simple debit or credit vertical spread instead."
                ),
                severity="reject",
            )

    def _check_position_size(
        self, rec: TradeRecommendation, assessment: RiskAssessment
    ) -> None:
        if rec.position_size_dollars > self._settings.max_position_dollars:
            assessment.add_veto(
                code="POSITION_TOO_LARGE",
                description=(
                    f"Position ${rec.position_size_dollars:,.0f} exceeds limit "
                    f"${self._settings.max_position_dollars:,.0f} "
                    f"({self._settings.max_position_size_pct:.0%} of account)"
                ),
                severity="reject",
            )

    def _check_reward_risk(
        self, rec: TradeRecommendation, assessment: RiskAssessment
    ) -> None:
        if rec.reward_risk_ratio < self._settings.min_reward_risk_ratio:
            assessment.add_veto(
                code="POOR_REWARD_RISK",
                description=(
                    f"R/R {rec.reward_risk_ratio:.1f}:1 is below minimum "
                    f"{self._settings.min_reward_risk_ratio:.1f}:1"
                ),
                severity="reject",
            )

    def _check_iv_rank(
        self, rec: TradeRecommendation, assessment: RiskAssessment
    ) -> None:
        if rec.iv_rank is None:
            return
        buying_strategies = {
            "long_call", "long_put", "bull_call_spread",
            "bear_put_spread", "strangle", "straddle", "calendar_spread",
        }
        if rec.strategy.value in buying_strategies and rec.iv_rank > 70:
            assessment.add_veto(
                code="IV_TOO_HIGH_FOR_LONG",
                description=(
                    f"Buying premium when IV Rank is {rec.iv_rank:.0f}/100. "
                    "You are overpaying for volatility. Wait for IV < 50."
                ),
                severity="reject",
            )
        selling_strategies = {
            "iron_condor", "iron_butterfly", "covered_call",
            "cash_secured_put",
        }
        if rec.strategy.value in selling_strategies and rec.iv_rank < 20:
            assessment.add_veto(
                code="IV_TOO_LOW_FOR_SHORT",
                description=(
                    f"Selling premium when IV Rank is {rec.iv_rank:.0f}/100. "
                    "Premium is too cheap for the risk. Wait for IV > 30."
                ),
                severity="reject",
            )

    def _check_earnings_risk(
        self, rec: TradeRecommendation, assessment: RiskAssessment
    ) -> None:
        high_risk_earnings = {"same_day", "day_before"}
        if rec.earnings_risk in high_risk_earnings:
            assessment.add_veto(
                code="EARNINGS_RISK",
                description=(
                    f"Earnings risk '{rec.earnings_risk}' before expiration. "
                    "Options price in binary event gap risk. Max loss could exceed "
                    "stated max loss due to gap through stop loss."
                ),
                severity="reject",
            )

    def _check_daily_loss_budget(
        self, session_id: str, assessment: RiskAssessment
    ) -> None:
        # In a real system, check DailyPnLTracker here
        # For now this is a placeholder hook
        pass

    def _build_prompt(self, rec: TradeRecommendation) -> str:
        return f"""Assess portfolio risk for this trade recommendation:

Trade: {rec.ticker} {rec.strategy.value}
Direction: {rec.direction}
Thesis: {rec.thesis}

Strikes/Expiration: {', '.join(f'{leg.action} {leg.strike}{leg.option_type[0].upper()} {leg.expiration}' for leg in rec.legs)}
Entry price: ${rec.entry_price:.2f}
Max loss: ${rec.max_loss_dollars:.0f}
Max gain: ${rec.max_gain_dollars:.0f}
R/R: {rec.reward_risk_ratio:.1f}:1
Contracts: {rec.contracts}

IV Rank: {rec.iv_rank or 'unknown'}/100
IV Crush risk: {rec.iv_crush_risk}
Earnings risk: {rec.earnings_risk}
Regime confirms: {rec.regime_confirms}

Assess:
1. Is there a risk the real max loss exceeds the stated ${rec.max_loss_dollars:.0f}?
2. Are there correlation or concentration risks to flag?
3. What macro events could impact this before {rec.expiration}?
4. Overall: APPROVE or VETO with specific reasons.
"""

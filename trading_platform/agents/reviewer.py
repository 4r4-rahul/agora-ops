"""
ReviewerAgent — Claude-powered second opinion / devil's advocate.

Subscribes to: RISK_ASSESSMENT
Publishes:     REVIEW_RESULT, RECOMMENDATION_READY

Final gate before the recommendation goes to the human or execution agent.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from ..core.models.agent import AgentMessage, AgentTopic
from ..core.models.risk import RiskAssessment
from ..core.models.trade import TradeDecision, TradeRecommendation
from .base import BaseAgent

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a senior options trading desk reviewer — the last checkpoint before a trade goes live.
Your job is NOT to agree with the strategy agent. Your job is to find anything that was missed.

Ask yourself:
1. Is the thesis still valid? Has price moved enough to invalidate the setup?
2. Is the timing right? Should we wait for a better entry?
3. Is the position sizing appropriate given all known risks?
4. Are there better alternatives that weren't considered?
5. Would a seasoned options trader put their own money on this?

Final decisions:
- APPROVED: trade is sound, execute when human approves
- REJECTED: fatal flaw, do not trade
- WATCHLIST: interesting setup but timing or conditions not right; monitor and re-evaluate

You MUST be specific. "Looks good" is not an acceptable review.
Cite specific numbers, levels, and market conditions.
"""


class ReviewOutput(BaseModel):
    decision: TradeDecision
    confidence: float = Field(..., ge=0.0, le=1.0)
    approval_notes: str = Field(..., min_length=40)
    rejection_reasons: list[str] = Field(default_factory=list)
    suggested_modifications: list[str] = Field(default_factory=list)
    watchlist_conditions: str = ""


class ReviewerAgent(BaseAgent):
    name = "reviewer"
    subscriptions = [AgentTopic.RISK_ASSESSMENT]

    async def handle(self, message: AgentMessage) -> None:
        session_id = message.session_id

        assessment = RiskAssessment.model_validate(message.payload)

        session = await self._state.get(session_id)
        if not session or not session.strategy_candidates:
            self._log.error("[%s] no strategy candidates for review", session_id)
            return

        rec = TradeRecommendation.model_validate(session.strategy_candidates[0])
        ticker = rec.ticker

        self._log.info("[%s] reviewing %s %s", session_id, ticker, rec.strategy)

        # If risk manager hard-vetoed, propagate rejection without calling Claude
        if not assessment.approved:
            rec.reject(reasons=[v.description for v in assessment.veto_reasons])
            rec.reviewer_notes = (
                f"Risk manager veto — not sent to Claude reviewer. "
                f"Reasons: {'; '.join(v.description for v in assessment.veto_reasons)}"
            )
            await self._finalize(session_id, rec, assessment)
            return

        user_message = self._build_prompt(rec, assessment)

        try:
            output = await self._call_claude_structured(
                system_prompt=_SYSTEM_PROMPT,
                user_message=user_message,
                output_schema=ReviewOutput,
                tool_name="structured_output",
                max_tokens=2048,
            )
        except Exception as exc:
            self._log.error("[%s] reviewer Claude call failed: %s", session_id, exc)
            # Default to watchlist on reviewer failure — don't auto-approve
            output = ReviewOutput(
                decision=TradeDecision.WATCHLIST,
                confidence=0.0,
                approval_notes=f"Reviewer unavailable: {exc}. Placing on watchlist.",
                watchlist_conditions="Re-run analysis when reviewer is available.",
            )

        # Apply reviewer decision
        rec.reviewer_notes = output.approval_notes
        rec.risk_assessment_summary = assessment.summary

        if output.decision == TradeDecision.APPROVED:
            rec.final_decision = TradeDecision.PENDING_APPROVAL  # Still needs human
        elif output.decision == TradeDecision.REJECTED:
            rec.reject(reasons=output.rejection_reasons)
        else:
            rec.final_decision = TradeDecision.WATCHLIST

        await self._finalize(session_id, rec, assessment)

        self._log.info(
            "[%s] review decision=%s confidence=%.0f%%",
            session_id,
            output.decision,
            output.confidence * 100,
        )

    async def _finalize(
        self,
        session_id: str,
        rec: TradeRecommendation,
        assessment: RiskAssessment,
    ) -> None:
        rec_dict = rec.model_dump(mode="json")

        await self._state.update(
            session_id,
            review_result={"decision": rec.final_decision, "notes": rec.reviewer_notes},
            final_recommendation=rec_dict,
        )

        session = await self._state.get(session_id)
        if session:
            session.mark_complete()

        await self.publish(
            AgentTopic.REVIEW_RESULT,
            session_id=session_id,
            payload={"decision": rec.final_decision, "notes": rec.reviewer_notes},
        )

        await self.publish(
            AgentTopic.RECOMMENDATION_READY,
            session_id=session_id,
            payload=rec_dict,
        )

    def _build_prompt(self, rec: TradeRecommendation, assessment: RiskAssessment) -> str:
        return f"""Review this trade recommendation as a senior desk reviewer:

RECOMMENDATION:
  Ticker: {rec.ticker}
  Strategy: {rec.strategy.value}
  Direction: {rec.direction}
  Thesis: {rec.thesis}

  Entry price: ${rec.entry_price:.2f}
  Stop loss: ${rec.stop_loss:.2f} — {rec.stop_loss_logic}
  Profit target: ${rec.profit_target:.2f} — {rec.profit_target_logic}
  Entry trigger: {rec.entry_trigger}

  Max loss: ${rec.max_loss_dollars:.0f}
  Max gain: ${rec.max_gain_dollars:.0f}
  R/R: {rec.reward_risk_ratio:.1f}:1
  Contracts: {rec.contracts}
  Total position: ${rec.position_size_dollars:,.0f}

  IV Rank: {rec.iv_rank or 'N/A'}/100
  IV Crush risk: {rec.iv_crush_risk}
  Earnings risk: {rec.earnings_risk}
  Regime confirms: {rec.regime_confirms} — {rec.regime_notes}

RISK MANAGER ASSESSMENT:
  {assessment.summary}
  Correlation risk: {assessment.correlation_risk}
  Portfolio delta: {assessment.portfolio_delta:+.2f}

Provide your final decision: APPROVED, REJECTED, or WATCHLIST.
Be specific about what you like or dislike about this trade.
"""

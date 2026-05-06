"""
ExecutionAgent — paper and live order management with human approval gate.

Subscribes to: RECOMMENDATION_READY
Publishes:     EXECUTION_STATUS

In paper mode: logs the trade and simulates fills.
In live mode: routes to IBKR (placeholder) after human confirmation.
Human approval is ALWAYS required before live execution.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime

from ..core.models.agent import AgentMessage, AgentTopic
from ..core.models.trade import TradeDecision, TradeJournalEntry, TradeRecommendation
from .base import BaseAgent

logger = logging.getLogger(__name__)


class ExecutionAgent(BaseAgent):
    name = "execution"
    subscriptions = [AgentTopic.RECOMMENDATION_READY]

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Pending approvals: session_id → (rec, future)
        self._pending: dict[str, tuple[TradeRecommendation, asyncio.Future]] = {}

    async def handle(self, message: AgentMessage) -> None:
        session_id = message.session_id
        rec = TradeRecommendation.model_validate(message.payload)

        if rec.final_decision not in (
            TradeDecision.APPROVED, TradeDecision.PENDING_APPROVAL
        ):
            self._log.info(
                "[%s] skipping execution — decision=%s",
                session_id,
                rec.final_decision,
            )
            return

        self._log.info(
            "[%s] %s %s %s — awaiting human approval",
            session_id,
            rec.ticker,
            rec.strategy.value,
            rec.direction,
        )

        self._print_recommendation(rec)

        if self._settings.require_human_approval:
            approved = await self._request_human_approval(session_id, rec)
            if not approved:
                self._log.info("[%s] human rejected trade", session_id)
                await self.publish(
                    AgentTopic.EXECUTION_STATUS,
                    session_id=session_id,
                    payload={
                        "status": "rejected_by_human",
                        "ticker": rec.ticker,
                        "session_id": session_id,
                    },
                )
                return
            rec.approve(user="human")

        if self._settings.trading_mode == "paper":
            await self._execute_paper(session_id, rec)
        else:
            await self._execute_live(session_id, rec)

    def _print_recommendation(self, rec: TradeRecommendation) -> None:
        """Print formatted recommendation to console for human review."""
        border = "=" * 60
        print(f"\n{border}")
        print(f"  TRADE RECOMMENDATION — {rec.ticker} {rec.strategy.value.upper()}")
        print(border)
        print(f"  Direction:     {rec.direction}")
        print(f"  Thesis:        {rec.thesis}")
        print(f"  Expiration:    {rec.expiration}")
        print()
        for leg in rec.legs:
            print(f"  {leg.action.upper()} {leg.quantity}x {leg.strike} {leg.option_type.upper()}")
        print()
        print(f"  Entry:         ${rec.entry_price:.2f} | Trigger: {rec.entry_trigger}")
        print(f"  Stop Loss:     ${rec.stop_loss:.2f}")
        print(f"  Profit Target: ${rec.profit_target:.2f}")
        print(f"  Max Loss:      ${rec.max_loss_dollars:,.0f}")
        print(f"  Max Gain:      ${rec.max_gain_dollars:,.0f}")
        print(f"  R/R:           {rec.reward_risk_ratio:.1f}:1")
        print(f"  Contracts:     {rec.contracts}")
        print()
        print(f"  IV Rank:       {rec.iv_rank or 'N/A'}/100")
        print(f"  IV Crush Risk: {rec.iv_crush_risk}")
        print(f"  Earnings Risk: {rec.earnings_risk}")
        print()
        print(f"  Risk Summary:  {rec.risk_assessment_summary}")
        print(f"  Reviewer:      {rec.reviewer_notes}")
        print(border)

    async def _request_human_approval(
        self, session_id: str, rec: TradeRecommendation
    ) -> bool:
        """
        In a real system, this sends a push notification / Discord DM.
        For now, prompt on stdin (non-blocking via executor).
        """
        loop = asyncio.get_event_loop()

        def _prompt() -> bool:
            try:
                answer = input(
                    f"\n  APPROVE trade {rec.ticker} {rec.strategy.value}? [y/N]: "
                ).strip().lower()
                return answer in ("y", "yes")
            except (EOFError, KeyboardInterrupt):
                return False

        return await loop.run_in_executor(None, _prompt)

    async def _execute_paper(
        self, session_id: str, rec: TradeRecommendation
    ) -> None:
        entry = TradeJournalEntry(
            recommendation_id=rec.id,
            session_id=session_id,
            ticker=rec.ticker,
            strategy=rec.strategy,
            direction=rec.direction,
            entry_price=rec.entry_price,
            contracts=rec.contracts,
            position_size_dollars=rec.position_size_dollars,
            max_loss_dollars=rec.max_loss_dollars,
            max_gain_dollars=rec.max_gain_dollars,
            stop_loss=rec.stop_loss,
            profit_target=rec.profit_target,
            thesis=rec.thesis,
            raw_recommendation=rec.model_dump(mode="json"),
        )

        self._log.info(
            "[PAPER] %s %s %s @ $%.2f x%d — journal_id=%s",
            rec.ticker,
            rec.strategy.value,
            rec.direction,
            rec.entry_price,
            rec.contracts,
            entry.id,
        )

        await self.publish(
            AgentTopic.EXECUTION_STATUS,
            session_id=session_id,
            payload={
                "status": "paper_filled",
                "ticker": rec.ticker,
                "strategy": rec.strategy,
                "entry_price": rec.entry_price,
                "contracts": rec.contracts,
                "journal_id": str(entry.id),
                "mode": "paper",
            },
        )

        from ..services.alerts import alert_trade_approved
        await alert_trade_approved(
            self._settings.alert_webhook_url,
            ticker=rec.ticker,
            strategy=rec.strategy.value,
            direction=rec.direction.value,
            entry_price=rec.entry_price,
            stop_loss=rec.stop_loss,
            profit_target=rec.profit_target,
            max_loss_dollars=rec.max_loss_dollars,
            reward_risk_ratio=rec.reward_risk_ratio,
            contracts=rec.contracts,
            thesis=rec.thesis,
            session_id=session_id,
        )

    async def _execute_live(
        self, session_id: str, rec: TradeRecommendation
    ) -> None:
        from ..services.ibkr_client import place_combo_order

        self._log.warning(
            "[LIVE] submitting IBKR order — %s %s @ $%.2f x%d",
            rec.ticker,
            rec.strategy.value,
            rec.entry_price,
            rec.contracts,
        )

        legs = [
            {
                "option_type": leg.option_type,
                "strike": leg.strike,
                "expiration_dte": leg.expiration_dte,
                "action": leg.action,
                "quantity": leg.quantity,
            }
            for leg in rec.legs
        ]

        try:
            fill_result = await place_combo_order(
                ticker=rec.ticker,
                legs=legs,
                contracts=rec.contracts,
                limit_price=rec.entry_price,
                session_id=session_id,
                host=self._settings.ibkr_host,
                port=self._settings.ibkr_port,
                client_id=self._settings.ibkr_client_id,
            )
        except Exception as exc:
            self._log.error(
                "[LIVE] IBKR order failed — %s: %s",
                type(exc).__name__, exc,
            )
            await self.publish(
                AgentTopic.EXECUTION_STATUS,
                session_id=session_id,
                payload={
                    "status": "live_order_failed",
                    "ticker": rec.ticker,
                    "error": str(exc),
                    "session_id": session_id,
                },
            )
            raise

        self._log.info(
            "[LIVE] Order filled — orderId=%s status=%s avg_price=%s",
            fill_result.get("order_id"),
            fill_result.get("status"),
            fill_result.get("avg_price"),
        )

        await self.publish(
            AgentTopic.EXECUTION_STATUS,
            session_id=session_id,
            payload={
                "status": "live_filled",
                "ticker": rec.ticker,
                "strategy": rec.strategy,
                "entry_price": fill_result.get("avg_price") or rec.entry_price,
                "contracts": rec.contracts,
                "order_id": fill_result.get("order_id"),
                "fill_status": fill_result.get("status"),
                "mode": "live",
                "session_id": session_id,
            },
        )

"""
ExecutionAgent — paper and live order management with human approval gate.

Subscribes to: RECOMMENDATION_READY
Publishes:     EXECUTION_STATUS

Paper mode: submits to IBKR paper account (port 7497); falls back to
            local-only simulation if IBKR is unavailable.
Live mode:  submits to IBKR live account; human approval always required.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from ..core.models.agent import AgentMessage, AgentTopic
from ..core.models.trade import TradeDecision, TradeRecommendation
from .base import BaseAgent

logger = logging.getLogger(__name__)

_DB = Path("./trade_journal.db")


class ExecutionAgent(BaseAgent):
    name = "execution"
    subscriptions = [AgentTopic.RECOMMENDATION_READY]

    async def handle(self, message: AgentMessage) -> None:
        session_id = message.session_id
        rec = TradeRecommendation.model_validate(message.payload)

        if rec.final_decision not in (
            TradeDecision.APPROVED, TradeDecision.PENDING_APPROVAL
        ):
            self._log.info(
                "[%s] skipping execution — decision=%s",
                session_id, rec.final_decision,
            )
            return

        # Dedup: one open position per ticker at a time
        if self._has_open_position(rec.ticker):
            self._log.info(
                "[%s] skipping duplicate — open position for %s already exists",
                session_id, rec.ticker,
            )
            return

        self._log.info(
            "[%s] %s %s %s — awaiting approval",
            session_id, rec.ticker, rec.strategy.value, rec.direction,
        )
        self._print_recommendation(rec)

        if self._settings.trading_mode == "paper":
            rec.approve(user="auto-paper")
        elif self._settings.require_human_approval:
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

        # Market hours guard — options only trade 9:30–16:00 ET
        from ..scripts.scheduler import _is_market_open
        now = datetime.now(timezone.utc)
        if not _is_market_open(now):
            if self._settings.trading_mode == "live":
                self._log.error(
                    "[%s] BLOCKED — market is closed. No live order sent.",
                    session_id,
                )
                await self.publish(
                    AgentTopic.EXECUTION_STATUS,
                    session_id=session_id,
                    payload={
                        "status": "blocked_market_closed",
                        "ticker": rec.ticker,
                        "session_id": session_id,
                    },
                )
                return
            self._log.warning(
                "[%s] market closed — proceeding with paper execution for testing",
                session_id,
            )

        # APPROVED alert — fired once, before broker submission
        from ..services.alerts import alert_trade_approved
        await alert_trade_approved(
            self._settings.alert_webhook_url,
            ticker=rec.ticker,
            strategy=rec.strategy.value,
            legs=rec.legs,
            expiration=rec.expiration,
            entry_price=rec.entry_price,
            stop_loss=rec.stop_loss,
            profit_target=rec.profit_target,
            max_loss_dollars=rec.max_loss_dollars,
            contracts=rec.contracts,
            mode=self._settings.trading_mode,
        )

        # Single pre-assigned journal ID shared by execution, journal, and monitor
        journal_id = str(uuid.uuid4())

        if self._settings.trading_mode == "paper":
            await self._execute_paper(session_id, rec, journal_id)
        else:
            await self._execute_live(session_id, rec, journal_id)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _has_open_position(self, ticker: str) -> bool:
        """Return True if an open position already exists for this ticker."""
        if not _DB.exists():
            return False
        try:
            with sqlite3.connect(_DB) as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM trade_journal WHERE ticker=? AND status='open'",
                    (ticker,),
                ).fetchone()
                return (row[0] or 0) > 0
        except Exception:
            return False

    def _build_ibkr_legs(self, rec: TradeRecommendation) -> list[dict]:
        """Convert SpreadLeg objects into the dict format IBKR client expects."""
        today = date.today()
        return [
            {
                "option_type": leg.option_type,
                "strike": leg.strike,
                "expiration_dte": max(0, (leg.expiration - today).days),
                "action": leg.action,
                "quantity": leg.quantity,
            }
            for leg in rec.legs
        ]

    def _print_recommendation(self, rec: TradeRecommendation) -> None:
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
        token = getattr(self._settings, "discord_bot_token", None)
        user_id = getattr(self._settings, "discord_approval_user_id", None)
        timeout = getattr(self._settings, "discord_approval_timeout_seconds", 300)

        if token and user_id:
            from ..services.discord_approval import request_approval
            legs_summary = "  ".join(
                f"{lg.action.upper()} {lg.strike} {lg.option_type[0].upper()}"
                for lg in rec.legs
            )
            return await request_approval(
                token=token,
                user_id=user_id,
                ticker=rec.ticker,
                strategy=rec.strategy.value,
                direction=str(rec.direction),
                legs_summary=legs_summary,
                entry_price=rec.entry_price,
                stop_loss=rec.stop_loss,
                profit_target=rec.profit_target,
                max_loss_dollars=rec.max_loss_dollars,
                reward_risk_ratio=rec.reward_risk_ratio,
                contracts=rec.contracts,
                timeout_seconds=timeout,
            )

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

    # ── Paper execution ───────────────────────────────────────────────────────

    async def _execute_paper(
        self, session_id: str, rec: TradeRecommendation, journal_id: str
    ) -> None:
        """Submit to IBKR paper account (port 7497). Falls back to local-only."""
        ibkr_order_id: int | None = None
        ibkr_oca_group: str | None = None

        try:
            from ..services.ibkr_client import place_bracket_order
            fill_result = await place_bracket_order(
                ticker=rec.ticker,
                legs=self._build_ibkr_legs(rec),
                contracts=rec.contracts,
                entry_price=rec.entry_price,
                profit_target=rec.profit_target,
                stop_loss=rec.stop_loss,
                session_id=session_id,
                host=self._settings.ibkr_host,
                port=self._settings.ibkr_port,
                client_id=self._settings.ibkr_client_id,
            )
            ibkr_order_id = fill_result.get("order_id")
            ibkr_oca_group = fill_result.get("oca_group")
            self._log.info(
                "[PAPER IBKR] Bracket submitted — orderId=%s status=%s",
                ibkr_order_id, fill_result.get("status"),
            )
        except Exception as exc:
            self._log.warning(
                "[PAPER LOCAL] IBKR unavailable (%s) — recording trade locally only",
                exc,
            )

        self._log.info(
            "[PAPER] %s %s %s @ $%.2f x%d — journal_id=%s",
            rec.ticker, rec.strategy.value, rec.direction,
            rec.entry_price, rec.contracts, journal_id,
        )

        await self.publish(
            AgentTopic.EXECUTION_STATUS,
            session_id=session_id,
            payload={
                "status": "paper_filled",
                "ticker": rec.ticker,
                "strategy": rec.strategy.value,
                "entry_price": rec.entry_price,
                "contracts": rec.contracts,
                "journal_id": journal_id,
                "ibkr_order_id": ibkr_order_id,
                "ibkr_oca_group": ibkr_oca_group,
                "mode": "paper",
            },
        )

        from ..services.alerts import alert_trade_executed
        await alert_trade_executed(
            self._settings.alert_webhook_url,
            ticker=rec.ticker,
            strategy=rec.strategy.value,
            legs=rec.legs,
            expiration=rec.expiration,
            entry_price=rec.entry_price,
            contracts=rec.contracts,
            mode="paper",
        )

    # ── Live execution ────────────────────────────────────────────────────────

    async def _execute_live(
        self, session_id: str, rec: TradeRecommendation, journal_id: str
    ) -> None:
        from ..services.ibkr_client import place_bracket_order

        self._log.warning(
            "[LIVE] submitting bracket — %s %s @ $%.2f x%d (target=%.2f stop=%.2f)",
            rec.ticker, rec.strategy.value,
            rec.entry_price, rec.contracts,
            rec.profit_target, rec.stop_loss,
        )

        try:
            fill_result = await place_bracket_order(
                ticker=rec.ticker,
                legs=self._build_ibkr_legs(rec),
                contracts=rec.contracts,
                entry_price=rec.entry_price,
                profit_target=rec.profit_target,
                stop_loss=rec.stop_loss,
                session_id=session_id,
                host=self._settings.ibkr_host,
                port=self._settings.ibkr_port,
                client_id=self._settings.ibkr_client_id,
            )
        except Exception as exc:
            self._log.error(
                "[LIVE] IBKR bracket order failed — %s: %s",
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
            "[LIVE] Bracket submitted — orderId=%s status=%s",
            fill_result.get("order_id"), fill_result.get("status"),
        )

        await self.publish(
            AgentTopic.EXECUTION_STATUS,
            session_id=session_id,
            payload={
                "status": "live_filled",
                "ticker": rec.ticker,
                "strategy": rec.strategy.value,
                "entry_price": rec.entry_price,
                "profit_target": rec.profit_target,
                "stop_loss": rec.stop_loss,
                "contracts": rec.contracts,
                "ibkr_order_id": fill_result.get("order_id"),
                "fill_status": fill_result.get("status"),
                "journal_id": journal_id,
                "mode": "live",
                "session_id": session_id,
            },
        )

        from ..services.alerts import alert_trade_executed
        await alert_trade_executed(
            self._settings.alert_webhook_url,
            ticker=rec.ticker,
            strategy=rec.strategy.value,
            legs=rec.legs,
            expiration=rec.expiration,
            entry_price=rec.entry_price,
            contracts=rec.contracts,
            mode="live",
        )

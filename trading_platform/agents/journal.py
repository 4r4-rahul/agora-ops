"""
TradeJournalAgent — SQLite-backed trade tracking and performance analytics.

Subscribes to: EXECUTION_STATUS, RECOMMENDATION_READY
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from ..core.models.agent import AgentMessage, AgentTopic
from ..core.models.trade import TradeJournalEntry, TradeRecommendation, TradeStatus
from .base import BaseAgent

logger = logging.getLogger(__name__)

DB_PATH = Path("./trade_journal.db")


class TradeJournalAgent(BaseAgent):
    name = "journal"
    subscriptions = [AgentTopic.EXECUTION_STATUS, AgentTopic.RECOMMENDATION_READY]

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._db_path = DB_PATH
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trade_journal (
                    id TEXT PRIMARY KEY,
                    recommendation_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    opened_at TEXT NOT NULL,
                    closed_at TEXT,
                    entry_price REAL NOT NULL,
                    contracts INTEGER NOT NULL,
                    position_size_dollars REAL NOT NULL,
                    max_loss_dollars REAL NOT NULL,
                    max_gain_dollars REAL NOT NULL,
                    stop_loss REAL NOT NULL,
                    profit_target REAL NOT NULL,
                    exit_price REAL,
                    realized_pnl REAL,
                    exit_reason TEXT,
                    thesis TEXT,
                    lessons_learned TEXT,
                    tags TEXT DEFAULT '[]',
                    raw_recommendation TEXT DEFAULT '{}'
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_ticker ON trade_journal(ticker);
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_status ON trade_journal(status);
            """)
            conn.commit()

    async def handle(self, message: AgentMessage) -> None:
        if message.topic == AgentTopic.EXECUTION_STATUS:
            await self._handle_execution(message)
        elif message.topic == AgentTopic.RECOMMENDATION_READY:
            await self._handle_recommendation(message)

    async def _handle_recommendation(self, message: AgentMessage) -> None:
        """Log final recommendations for audit trail."""
        try:
            rec = TradeRecommendation.model_validate(message.payload)
            self._log.debug(
                "[%s] logging recommendation %s %s",
                message.session_id,
                rec.ticker,
                rec.final_decision,
            )
        except Exception as exc:
            self._log.error("journal recommendation logging failed: %s", exc)

    async def _handle_execution(self, message: AgentMessage) -> None:
        payload = message.payload
        status = payload.get("status")
        session_id = message.session_id

        if status not in ("paper_filled", "live_filled"):
            return

        session = await self._state.get(session_id)
        if not session or not session.final_recommendation:
            return

        try:
            rec = TradeRecommendation.model_validate(session.final_recommendation)
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
            self._insert(entry)
            self._log.info("[%s] journaled trade %s", session_id, entry.id)
        except Exception as exc:
            self._log.error("[%s] journal insert failed: %s", session_id, exc)

    def _insert(self, entry: TradeJournalEntry) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                INSERT INTO trade_journal VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    str(entry.id),
                    str(entry.recommendation_id),
                    entry.session_id,
                    entry.ticker,
                    entry.strategy.value,
                    entry.direction.value,
                    entry.status.value,
                    entry.opened_at.isoformat(),
                    entry.closed_at.isoformat() if entry.closed_at else None,
                    entry.entry_price,
                    entry.contracts,
                    entry.position_size_dollars,
                    entry.max_loss_dollars,
                    entry.max_gain_dollars,
                    entry.stop_loss,
                    entry.profit_target,
                    entry.exit_price,
                    entry.realized_pnl,
                    entry.exit_reason,
                    entry.thesis,
                    entry.lessons_learned,
                    json.dumps(entry.tags),
                    json.dumps(entry.raw_recommendation),
                ),
            )
            conn.commit()

    def get_open_trades(self) -> list[dict[str, Any]]:
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM trade_journal WHERE status = 'open' ORDER BY opened_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_performance_summary(self) -> dict[str, Any]:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("""
                SELECT
                    COUNT(*) as total_trades,
                    SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as winners,
                    SUM(CASE WHEN realized_pnl <= 0 THEN 1 ELSE 0 END) as losers,
                    SUM(realized_pnl) as total_pnl,
                    AVG(realized_pnl) as avg_pnl,
                    MAX(realized_pnl) as best_trade,
                    MIN(realized_pnl) as worst_trade
                FROM trade_journal
                WHERE status = 'closed'
            """).fetchone()
        if not row:
            return {}
        total = row[0] or 0
        winners = row[1] or 0
        return {
            "total_trades": total,
            "winners": winners,
            "losers": row[2] or 0,
            "win_rate": winners / total if total > 0 else 0.0,
            "total_pnl": row[3] or 0.0,
            "avg_pnl": row[4] or 0.0,
            "best_trade": row[5] or 0.0,
            "worst_trade": row[6] or 0.0,
        }

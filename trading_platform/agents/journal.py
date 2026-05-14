"""
TradeJournalAgent — SQLite-backed trade tracking.

Subscribes to: EXECUTION_STATUS
Publishes:     nothing

Handles both paper_filled and live_filled statuses.
Uses the journal_id pre-assigned by ExecutionAgent so all agents agree
on the same UUID for a given trade.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date as _date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from ..core.models.agent import AgentMessage, AgentTopic
from ..core.models.trade import TradeJournalEntry, TradeRecommendation, TradeStatus
from .base import BaseAgent

logger = logging.getLogger(__name__)

DB_PATH = Path("./trade_journal.db")


class TradeJournalAgent(BaseAgent):
    name = "journal"
    subscriptions = [AgentTopic.EXECUTION_STATUS]

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
            # Schema migrations — idempotent
            for col, typedef in [
                ("underlying_at_entry", "REAL"),
                ("expiration_date", "TEXT"),
                ("original_dte", "INTEGER"),
                ("ibkr_order_id", "TEXT"),
                ("trading_mode", "TEXT"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE trade_journal ADD COLUMN {col} {typedef}")
                except Exception:
                    pass
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ticker ON trade_journal(ticker)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON trade_journal(status)")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_recommendation "
                "ON trade_journal(recommendation_id)"
            )
            conn.commit()

    async def handle(self, message: AgentMessage) -> None:
        if message.topic == AgentTopic.EXECUTION_STATUS:
            await self._handle_execution(message)

    async def _handle_execution(self, message: AgentMessage) -> None:
        payload = message.payload
        status = payload.get("status")
        session_id = message.session_id

        if status not in ("paper_filled", "live_filled"):
            return

        session = await self._state.get(session_id)
        if not session or not session.final_recommendation:
            self._log.warning("[%s] journal: no session/recommendation found", session_id)
            return

        try:
            rec = TradeRecommendation.model_validate(session.final_recommendation)

            # Use the journal_id pre-assigned by ExecutionAgent — all agents share this UUID
            pre_id = payload.get("journal_id")
            entry_id = UUID(pre_id) if pre_id else uuid4()

            # Underlying price at entry
            underlying_at_entry: float | None = None
            if session.market_snapshot:
                snap = session.market_snapshot
                underlying_at_entry = (
                    snap.get("price") if isinstance(snap, dict)
                    else getattr(snap, "price", None)
                )

            # DTE at entry from first leg
            original_dte: int | None = None
            expiration_date: str | None = None
            rec_dict = rec.model_dump(mode="json")
            legs = rec_dict.get("legs", [])
            if legs:
                exp = legs[0].get("expiration")
                if exp:
                    try:
                        exp_date = _date.fromisoformat(str(exp))
                        original_dte = max(0, (exp_date - _date.today()).days)
                        expiration_date = exp_date.isoformat()
                    except Exception:
                        pass

            entry = TradeJournalEntry(
                id=entry_id,
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
                raw_recommendation=rec_dict,
            )

            self._insert(
                entry,
                underlying_at_entry=underlying_at_entry,
                expiration_date=expiration_date,
                original_dte=original_dte,
                ibkr_order_id=payload.get("ibkr_order_id"),
                trading_mode=payload.get("mode", "paper"),
            )
            self._log.info(
                "[%s] journaled %s %s @ $%.2f x%d (mode=%s ibkr_order=%s) id=%s",
                session_id, rec.ticker, rec.strategy.value,
                rec.entry_price, rec.contracts,
                payload.get("mode", "paper"),
                payload.get("ibkr_order_id", "local"),
                str(entry_id)[:8],
            )
        except sqlite3.IntegrityError:
            # Duplicate recommendation_id — idempotent, already journaled
            self._log.debug(
                "[%s] duplicate recommendation %s — skipping insert",
                session_id, message.payload.get("journal_id", ""),
            )
        except Exception as exc:
            self._log.error("[%s] journal insert failed: %s", session_id, exc)

    def _insert(
        self,
        entry: TradeJournalEntry,
        underlying_at_entry: float | None = None,
        expiration_date: str | None = None,
        original_dte: int | None = None,
        ibkr_order_id: Any = None,
        trading_mode: str = "paper",
    ) -> None:
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
            # Set new optional columns separately (schema migration safe)
            conn.execute(
                """
                UPDATE trade_journal SET
                    underlying_at_entry = ?,
                    expiration_date = ?,
                    original_dte = ?,
                    ibkr_order_id = ?,
                    trading_mode = ?
                WHERE id = ?
                """,
                (
                    underlying_at_entry,
                    expiration_date,
                    original_dte,
                    str(ibkr_order_id) if ibkr_order_id is not None else None,
                    trading_mode,
                    str(entry.id),
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
                FROM trade_journal WHERE status = 'closed'
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

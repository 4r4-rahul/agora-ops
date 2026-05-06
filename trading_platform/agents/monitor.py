"""
MonitorAgent — periodic position monitor that enforces stop losses and profit targets.

Subscribes to: EXECUTION_STATUS  (to learn about new open positions)
Publishes:     POSITION_UPDATE   (when a position hits stop or target)

Runs a background asyncio loop every `poll_interval_seconds`.
In paper mode it simulates exit fills via yfinance spot price.
In live mode it would query IBKR for mark prices.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from ..core.bus import MessageBus
from ..core.config import Settings
from ..core.models.agent import AgentMessage, AgentStatus, AgentTopic
from ..core.state import SharedStateStore
from .base import BaseAgent

logger = logging.getLogger(__name__)

_DB_PATH = Path("./trade_journal.db")


class OpenPosition:
    """Lightweight in-memory record of a monitored position."""

    __slots__ = (
        "journal_id", "session_id", "ticker", "strategy",
        "entry_price", "stop_loss", "profit_target",
        "contracts", "max_loss_dollars", "direction",
    )

    def __init__(self, row: dict[str, Any]) -> None:
        self.journal_id: str = row["id"]
        self.session_id: str = row["session_id"]
        self.ticker: str = row["ticker"]
        self.strategy: str = row["strategy"]
        self.entry_price: float = float(row["entry_price"])
        self.stop_loss: float = float(row["stop_loss"])
        self.profit_target: float = float(row["profit_target"])
        self.contracts: int = int(row["contracts"])
        self.max_loss_dollars: float = float(row["max_loss_dollars"])
        self.direction: str = row["direction"]


class MonitorAgent(BaseAgent):
    """
    Monitors open paper positions for stop loss / profit target exits.

    Uses a background asyncio task (not pub/sub) for the polling loop.
    Pub/sub is used only to learn about newly opened positions and to
    publish exit notifications.
    """

    name = "monitor"
    subscriptions = [AgentTopic.EXECUTION_STATUS]

    def __init__(
        self,
        bus: MessageBus,
        state_store: SharedStateStore,
        settings: Settings | None = None,
        poll_interval_seconds: float = 60.0,
    ) -> None:
        super().__init__(bus, state_store, settings)
        self._poll_interval = poll_interval_seconds
        self._positions: dict[str, OpenPosition] = {}  # journal_id → position
        self._monitor_task: asyncio.Task | None = None

    # ── Lifecycle ─────────────────────────────────────────────────

    async def start(self) -> None:
        await super().start()
        self._load_open_positions_from_db()
        self._monitor_task = asyncio.create_task(
            self._monitor_loop(), name="position-monitor"
        )
        self._log.info(
            "monitor started — %d open positions, polling every %.0fs",
            len(self._positions),
            self._poll_interval,
        )

    async def stop(self) -> None:
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        await super().stop()

    # ── Pub/sub handler — learns about new positions ───────────────

    async def handle(self, message: AgentMessage) -> None:
        payload = message.payload
        status = payload.get("status")

        if status not in ("paper_filled", "live_filled"):
            return

        journal_id = payload.get("journal_id")
        if not journal_id:
            return

        pos = self._load_one_position(journal_id)
        if pos:
            self._positions[journal_id] = pos
            self._log.info(
                "monitoring new position %s — %s %s",
                journal_id[:8],
                pos.ticker,
                pos.strategy,
            )

    # ── Monitoring loop ────────────────────────────────────────────

    async def _monitor_loop(self) -> None:
        """Run indefinitely, checking positions every poll_interval seconds."""
        while True:
            try:
                await asyncio.sleep(self._poll_interval)
                await self._check_all_positions()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._log.error("monitor loop error: %s", exc, exc_info=True)

    async def _check_all_positions(self) -> None:
        if not self._positions:
            return

        tickers = {pos.ticker for pos in self._positions.values()}
        prices = await self._fetch_prices(tickers)

        closed: list[str] = []
        for journal_id, pos in self._positions.items():
            current_price = prices.get(pos.ticker)
            if current_price is None:
                continue

            exit_reason = self._check_exit_condition(pos, current_price)
            if exit_reason:
                await self._close_position(pos, current_price, exit_reason)
                closed.append(journal_id)

        for j in closed:
            self._positions.pop(j, None)

    def _check_exit_condition(
        self, pos: OpenPosition, current_price: float
    ) -> str | None:
        """
        Check if the current underlying price triggers a stop or target.

        NOTE: For options positions, this is an approximation using the
        underlying price as a proxy. Production should use the option mark price
        from the broker. A proper implementation maps underlying move → P&L
        via delta, but for paper trading this is a reasonable heuristic.
        """
        # Simple heuristic: estimate option price movement from underlying
        # For a long debit spread, underlying price above entry target → exit
        # For simplicity, compare entry_price adjusted by underlying move
        if pos.direction == "bullish":
            if current_price <= pos.stop_loss:
                return "stop_loss"
            if current_price >= pos.profit_target:
                return "profit_target"
        elif pos.direction == "bearish":
            if current_price >= pos.stop_loss:
                return "stop_loss"
            if current_price <= pos.profit_target:
                return "profit_target"

        return None

    async def _close_position(
        self, pos: OpenPosition, exit_price: float, reason: str
    ) -> None:
        pnl_sign = 1 if reason == "profit_target" else -1
        estimated_pnl = (
            abs(pos.profit_target - pos.entry_price) * pnl_sign
            * pos.contracts * 100
        )

        self._log.info(
            "CLOSING %s %s — reason=%s exit_price=%.2f est_pnl=$%.0f",
            pos.ticker,
            pos.strategy,
            reason,
            exit_price,
            estimated_pnl,
        )

        self._update_db_closed(pos.journal_id, exit_price, reason, estimated_pnl)

        await self.publish(
            AgentTopic.POSITION_UPDATE,
            session_id=pos.session_id,
            payload={
                "journal_id": pos.journal_id,
                "ticker": pos.ticker,
                "strategy": pos.strategy,
                "exit_reason": reason,
                "exit_price": exit_price,
                "estimated_pnl": estimated_pnl,
                "status": "closed",
            },
        )

    # ── Market data ────────────────────────────────────────────────

    async def _fetch_prices(self, tickers: set[str]) -> dict[str, float]:
        """Fetch current prices for a set of tickers via yfinance."""
        loop = asyncio.get_event_loop()

        def _sync_fetch() -> dict[str, float]:
            import yfinance as yf

            prices: dict[str, float] = {}
            for ticker in tickers:
                try:
                    info = yf.Ticker(ticker).info or {}
                    price = (
                        info.get("regularMarketPrice")
                        or info.get("currentPrice")
                        or info.get("previousClose")
                    )
                    if price:
                        prices[ticker] = float(price)
                except Exception as exc:
                    self._log.debug("price fetch failed for %s: %s", ticker, exc)
            return prices

        try:
            return await loop.run_in_executor(None, _sync_fetch)
        except Exception as exc:
            self._log.error("price fetch failed: %s", exc)
            return {}

    # ── Database helpers ───────────────────────────────────────────

    def _load_open_positions_from_db(self) -> None:
        if not _DB_PATH.exists():
            return
        try:
            with sqlite3.connect(_DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM trade_journal WHERE status = 'open'"
                ).fetchall()
            for row in rows:
                pos = OpenPosition(dict(row))
                self._positions[pos.journal_id] = pos
        except Exception as exc:
            self._log.error("failed to load open positions from DB: %s", exc)

    def _load_one_position(self, journal_id: str) -> OpenPosition | None:
        if not _DB_PATH.exists():
            return None
        try:
            with sqlite3.connect(_DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT * FROM trade_journal WHERE id = ?", (journal_id,)
                ).fetchone()
            return OpenPosition(dict(row)) if row else None
        except Exception as exc:
            self._log.error("failed to load position %s: %s", journal_id, exc)
            return None

    def _update_db_closed(
        self,
        journal_id: str,
        exit_price: float,
        reason: str,
        pnl: float,
    ) -> None:
        if not _DB_PATH.exists():
            return
        try:
            with sqlite3.connect(_DB_PATH) as conn:
                conn.execute(
                    """
                    UPDATE trade_journal
                    SET status = 'closed',
                        closed_at = ?,
                        exit_price = ?,
                        realized_pnl = ?,
                        exit_reason = ?
                    WHERE id = ?
                    """,
                    (
                        datetime.utcnow().isoformat(),
                        exit_price,
                        pnl,
                        reason,
                        journal_id,
                    ),
                )
                conn.commit()
        except Exception as exc:
            self._log.error("DB close update failed for %s: %s", journal_id, exc)

    @property
    def open_position_count(self) -> int:
        return len(self._positions)

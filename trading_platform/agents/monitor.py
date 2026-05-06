"""
MonitorAgent — periodic position monitor that enforces stop losses and profit targets.

Subscribes to: EXECUTION_STATUS  (to learn about new open positions)
Publishes:     POSITION_UPDATE   (when a position hits stop or target)

Runs a background asyncio loop every `poll_interval_seconds`.

Exit detection uses the same intrinsic-value + time-decay spread pricing
model as the walk-forward backtester, so paper-mode results are consistent
with backtested expectations. The underlying price fetched from yfinance is
converted to an estimated option spread price before comparing against the
option-denominated stop_loss and profit_target thresholds.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import date, datetime
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
    """In-memory record of a monitored position with full pricing context."""

    __slots__ = (
        "journal_id", "session_id", "ticker", "strategy",
        "direction", "entry_price", "stop_loss", "profit_target",
        "contracts", "max_loss_dollars",
        "underlying_at_entry", "expiration_date", "original_dte",
        "legs",
    )

    def __init__(self, row: dict[str, Any]) -> None:
        self.journal_id: str = row["id"]
        self.session_id: str = row["session_id"]
        self.ticker: str = row["ticker"]
        self.strategy: str = row["strategy"]
        self.direction: str = row["direction"]
        self.entry_price: float = float(row["entry_price"])
        self.stop_loss: float = float(row["stop_loss"])
        self.profit_target: float = float(row["profit_target"])
        self.contracts: int = int(row["contracts"])
        self.max_loss_dollars: float = float(row["max_loss_dollars"])

        # Pricing context — may be None for positions opened before schema migration
        self.underlying_at_entry: float | None = (
            float(row["underlying_at_entry"]) if row.get("underlying_at_entry") else None
        )
        self.expiration_date: date | None = (
            date.fromisoformat(row["expiration_date"]) if row.get("expiration_date") else None
        )
        self.original_dte: int = int(row.get("original_dte") or 21)

        # Parse legs from raw_recommendation JSON
        raw = row.get("raw_recommendation") or "{}"
        try:
            rec = json.loads(raw) if isinstance(raw, str) else raw
            self.legs: list[dict] = rec.get("legs", [])
        except Exception:
            self.legs = []


def _estimate_option_price(pos: OpenPosition, underlying_now: float) -> float:
    """
    Estimate current spread price using intrinsic value + linear time decay.

    Matches the pricing model in backtester/engine.py so paper-mode
    monitoring is consistent with what the backtest simulates.
    """
    legs = pos.legs
    if len(legs) < 2 or pos.underlying_at_entry is None:
        # Not enough context — fall back to entry price (treat as unchanged)
        return pos.entry_price

    strikes = sorted(float(l.get("strike", underlying_now)) for l in legs)
    low_strike, high_strike = strikes[0], strikes[-1]
    spread_width = high_strike - low_strike

    if spread_width <= 0:
        return pos.entry_price

    # Intrinsic value from current underlying
    direction = pos.direction.lower()
    if direction == "bullish":
        intrinsic = max(0.0, min(underlying_now - low_strike, spread_width))
    elif direction == "bearish":
        intrinsic = max(0.0, min(high_strike - underlying_now, spread_width))
    else:  # neutral / iron condor
        centre = (low_strike + high_strike) / 2.0
        half_width = spread_width / 2.0
        displacement = abs(underlying_now - centre)
        intrinsic = max(0.0, half_width - displacement)

    # Time value: decays linearly from entry_price to 0 at expiry
    dte_remaining = 0
    if pos.expiration_date:
        dte_remaining = max(0, (pos.expiration_date - date.today()).days)
    time_fraction = dte_remaining / max(1, pos.original_dte)
    time_value = pos.entry_price * time_fraction * 0.5

    return intrinsic + time_value


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

    # ── Lifecycle ─────────────────────────────────────────────────────────

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

    # ── Pub/sub: learn about newly opened positions ────────────────────────

    async def handle(self, message: AgentMessage) -> None:
        payload = message.payload
        if payload.get("status") not in ("paper_filled", "live_filled"):
            return
        journal_id = payload.get("journal_id")
        if not journal_id:
            return
        pos = self._load_one_position(journal_id)
        if pos:
            self._positions[journal_id] = pos
            self._log.info(
                "monitoring new position %s — %s %s entry=%.2f stop=%.2f target=%.2f",
                journal_id[:8], pos.ticker, pos.strategy,
                pos.entry_price, pos.stop_loss, pos.profit_target,
            )

    # ── Monitoring loop ────────────────────────────────────────────────────

    async def _monitor_loop(self) -> None:
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
            underlying_now = prices.get(pos.ticker)
            if underlying_now is None:
                continue

            option_price_now = _estimate_option_price(pos, underlying_now)
            exit_reason = self._check_exit_condition(pos, option_price_now)

            if exit_reason:
                pnl = self._compute_pnl(pos, option_price_now, exit_reason)
                await self._close_position(pos, underlying_now, option_price_now, exit_reason, pnl)
                closed.append(journal_id)
            else:
                self._log.debug(
                    "%s %s underlying=%.2f est_option=%.2f (stop=%.2f target=%.2f)",
                    pos.ticker, pos.strategy,
                    underlying_now, option_price_now,
                    pos.stop_loss, pos.profit_target,
                )

        for j in closed:
            self._positions.pop(j, None)

    def _check_exit_condition(self, pos: OpenPosition, option_price: float) -> str | None:
        """Compare estimated option price (not underlying) against thresholds."""
        if option_price <= pos.stop_loss:
            return "stop_loss"
        if option_price >= pos.profit_target:
            return "profit_target"
        if pos.expiration_date and pos.expiration_date <= date.today():
            return "expiry"
        return None

    def _compute_pnl(self, pos: OpenPosition, option_price: float, reason: str) -> float:
        exit_price = max(0.0, option_price) if reason == "expiry" else option_price
        return (exit_price - pos.entry_price) * pos.contracts * 100

    # ── Market data ────────────────────────────────────────────────────────

    async def _fetch_prices(self, tickers: set[str]) -> dict[str, float]:
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

    # ── Close notification ─────────────────────────────────────────────────

    async def _close_position(
        self,
        pos: OpenPosition,
        underlying_price: float,
        option_price: float,
        reason: str,
        pnl: float,
    ) -> None:
        self._log.info(
            "CLOSING %s %s — reason=%s underlying=%.2f option_est=%.2f pnl=$%.0f",
            pos.ticker, pos.strategy, reason,
            underlying_price, option_price, pnl,
        )
        self._update_db_closed(pos.journal_id, option_price, reason, pnl)
        await self.publish(
            AgentTopic.POSITION_UPDATE,
            session_id=pos.session_id,
            payload={
                "journal_id": pos.journal_id,
                "ticker": pos.ticker,
                "strategy": pos.strategy,
                "exit_reason": reason,
                "underlying_price": underlying_price,
                "option_exit_price": option_price,
                "estimated_pnl": pnl,
                "status": "closed",
            },
        )

        from ..services.alerts import alert_position_closed
        await alert_position_closed(
            self._settings.alert_webhook_url,
            ticker=pos.ticker,
            strategy=pos.strategy,
            exit_reason=reason,
            underlying_price=underlying_price,
            option_exit_price=option_price,
            estimated_pnl=pnl,
            journal_id=pos.journal_id,
        )

    # ── Database helpers ───────────────────────────────────────────────────

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
                        datetime.now().isoformat(),
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

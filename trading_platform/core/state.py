"""
Thread-safe shared state store.

Agents read and write session state here. The orchestrator creates a session;
agents accumulate partial results; the final state is available for the API.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from typing import Any

from .models.agent import AnalysisSession

logger = logging.getLogger(__name__)

_MAX_SESSIONS = 500  # LRU eviction after this many completed sessions


class SharedStateStore:
    """
    In-memory, asyncio-safe store of AnalysisSession objects.

    For production scale, swap the backing store to Redis with aioredis.
    The interface is intentionally kept simple so the swap is mechanical.
    """

    def __init__(self, max_sessions: int = _MAX_SESSIONS) -> None:
        self._sessions: OrderedDict[str, AnalysisSession] = OrderedDict()
        self._lock = asyncio.Lock()
        self._max = max_sessions

    async def create_session(self, ticker: str) -> AnalysisSession:
        session = AnalysisSession(ticker=ticker)
        async with self._lock:
            self._sessions[session.session_id] = session
            self._evict_if_needed()
        logger.debug("created session %s for %s", session.session_id, ticker)
        return session

    async def get(self, session_id: str) -> AnalysisSession | None:
        async with self._lock:
            return self._sessions.get(session_id)

    async def update(self, session_id: str, **kwargs: Any) -> AnalysisSession | None:
        """Partial update — only named fields are set."""
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                logger.warning("update on unknown session %s", session_id)
                return None
            for key, value in kwargs.items():
                if not hasattr(session, key):
                    raise AttributeError(f"AnalysisSession has no field '{key}'")
                setattr(session, key, value)
            return session

    async def list_sessions(
        self,
        status: str | None = None,
        limit: int = 50,
    ) -> list[AnalysisSession]:
        async with self._lock:
            sessions = list(self._sessions.values())
        if status:
            sessions = [s for s in sessions if s.status == status]
        return sessions[-limit:]

    async def delete(self, session_id: str) -> bool:
        async with self._lock:
            existed = session_id in self._sessions
            self._sessions.pop(session_id, None)
        return existed

    def _evict_if_needed(self) -> None:
        """Evict oldest sessions when over capacity (called under lock)."""
        while len(self._sessions) > self._max:
            oldest_id, oldest = next(iter(self._sessions.items()))
            if oldest.status in ("complete", "failed"):
                del self._sessions[oldest_id]
            else:
                break  # don't evict in-progress sessions


class DailyPnLTracker:
    """Lightweight daily P&L tracker for the safety monitor."""

    def __init__(self) -> None:
        self._daily: float = 0.0
        self._weekly: float = 0.0
        self._monthly: float = 0.0
        self._peak_equity: float = 0.0
        self._consecutive_losses: int = 0
        self._lock = asyncio.Lock()

    async def record_trade(self, pnl: float) -> None:
        async with self._lock:
            self._daily += pnl
            self._weekly += pnl
            self._monthly += pnl
            if pnl < 0:
                self._consecutive_losses += 1
            else:
                self._consecutive_losses = 0

    async def reset_daily(self) -> None:
        async with self._lock:
            self._daily = 0.0

    async def reset_weekly(self) -> None:
        async with self._lock:
            self._daily = 0.0
            self._weekly = 0.0

    async def reset_monthly(self) -> None:
        async with self._lock:
            self._daily = 0.0
            self._weekly = 0.0
            self._monthly = 0.0

    @property
    def daily_pnl(self) -> float:
        return self._daily

    @property
    def weekly_pnl(self) -> float:
        return self._weekly

    @property
    def monthly_pnl(self) -> float:
        return self._monthly

    @property
    def consecutive_losses(self) -> int:
        return self._consecutive_losses

"""
OrchestratorAgent — the central brain that coordinates the analysis pipeline.

This agent:
  1. Creates a new AnalysisSession per ticker
  2. Fires ANALYSIS_REQUEST to kick off parallel data gathering
  3. Monitors pipeline progress
  4. Returns the final recommendation

Pipeline flow:
  ANALYSIS_REQUEST → MarketData + News (parallel)
  MARKET_DATA_RESULT → Regime + Technical (parallel)
  TECHNICAL_RESULT → OptionsStrategy (after regime/news settle)
  STRATEGY_CANDIDATES → RiskManager
  RISK_ASSESSMENT → Reviewer
  RECOMMENDATION_READY → Execution + Journal
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from ..core.bus import MessageBus
from ..core.config import Settings, get_settings
from ..core.models.agent import AgentMessage, AgentTopic, AnalysisRequest, AnalysisSession
from ..core.state import SharedStateStore

logger = logging.getLogger(__name__)


class OrchestratorAgent:
    """
    Coordinates the full analysis pipeline for one or more tickers.
    Not a BaseAgent subclass — it owns the other agents, not a peer.
    """

    name = "orchestrator"

    def __init__(
        self,
        bus: MessageBus,
        state_store: SharedStateStore,
        settings: Settings | None = None,
    ) -> None:
        self._bus = bus
        self._state = state_store
        self._settings = settings or get_settings()
        self._log = logging.getLogger("platform.agents.orchestrator")
        self._active_sessions: dict[str, asyncio.Event] = {}

        # Subscribe to terminal events to know when sessions complete
        self._bus.subscribe(AgentTopic.RECOMMENDATION_READY, self._on_recommendation)
        self._bus.subscribe(AgentTopic.ERROR, self._on_error)

    async def analyze(
        self,
        ticker: str,
        timeout: float = 120.0,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Run the full analysis pipeline for a ticker.

        Pass session_id if a session was pre-created (e.g. by the API route)
        so the caller can poll for results immediately without a race condition.

        Returns the final recommendation dict, or an error dict.
        Waits up to `timeout` seconds for the pipeline to complete.
        """
        if session_id:
            session = await self._state.get(session_id)
            if not session:
                session = await self._state.create_session(ticker)
                session_id = session.session_id
        else:
            session = await self._state.create_session(ticker)
            session_id = session.session_id

        done_event = asyncio.Event()
        self._active_sessions[session_id] = done_event

        self._log.info("starting analysis session %s for %s", session_id, ticker)

        # Kick off the pipeline
        await self._fire_analysis_request(ticker, session_id)

        try:
            await asyncio.wait_for(done_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            self._log.error("[%s] pipeline timed out after %.0fs", session_id, timeout)
            await self._state.update(session_id, status="failed", error="pipeline timeout")
        finally:
            self._active_sessions.pop(session_id, None)

        session = await self._state.get(session_id)
        if session and session.final_recommendation:
            return session.final_recommendation
        return {
            "error": session.error if session else "unknown",
            "session_id": session_id,
            "ticker": ticker,
        }

    async def analyze_batch(
        self,
        tickers: list[str],
        concurrency: int = 3,
    ) -> list[dict[str, Any]]:
        """Analyze multiple tickers with bounded concurrency."""
        semaphore = asyncio.Semaphore(concurrency)

        async def _one(ticker: str) -> dict[str, Any]:
            async with semaphore:
                return await self.analyze(ticker)

        return list(await asyncio.gather(*[_one(t) for t in tickers]))

    async def _fire_analysis_request(self, ticker: str, session_id: str) -> None:
        req = AnalysisRequest(ticker=ticker, tickers=[ticker])
        msg = AgentMessage.create(
            topic=AgentTopic.ANALYSIS_REQUEST,
            session_id=session_id,
            sender=self.name,
            payload=req.model_dump(),
        )
        await self._bus.publish(msg)
        self._log.debug("[%s] fired ANALYSIS_REQUEST for %s", session_id, ticker)

    async def _on_recommendation(self, message: AgentMessage) -> None:
        session_id = message.session_id
        event = self._active_sessions.get(session_id)
        if event:
            self._log.info("[%s] pipeline complete — recommendation ready", session_id)
            event.set()

    async def _on_error(self, message: AgentMessage) -> None:
        session_id = message.session_id
        error = message.payload.get("error", "unknown error")
        agent = message.payload.get("agent", "unknown")
        self._log.error("[%s] error from %s: %s", session_id, agent, error)

        await self._state.update(session_id, status="failed", error=f"{agent}: {error}")

        # Don't immediately fail — some agents can recover or be non-fatal
        # Only signal done if the session is truly stuck
        session = await self._state.get(session_id)
        if session and session.status == "failed":
            event = self._active_sessions.get(session_id)
            if event:
                event.set()

    async def get_session(self, session_id: str) -> AnalysisSession | None:
        return await self._state.get(session_id)

    async def list_sessions(self, status: str | None = None) -> list[AnalysisSession]:
        return await self._state.list_sessions(status=status)

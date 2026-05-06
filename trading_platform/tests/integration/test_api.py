"""
FastAPI E2E tests — full HTTP request/response cycle.

Bypasses the lifespan by injecting singletons directly into deps.py.
No real Claude/yfinance calls — orchestrator is mocked.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI

from trading_platform.api.main import create_app
from trading_platform.api import deps
from trading_platform.core.bus import MessageBus
from trading_platform.core.config import Settings
from trading_platform.core.models.agent import AnalysisSession
from trading_platform.core.models.market import Bar, MarketSnapshot
from trading_platform.core.models.trade import TradeDecision
from trading_platform.core.state import SharedStateStore


# ── Canned analysis result ────────────────────────────────────────────────

_ANALYSIS_RESULT: dict[str, Any] = {
    "ticker": "SPY",
    "strategy": "bull_call_spread",
    "direction": "bullish",
    "entry_price": 2.20,
    "stop_loss": 1.10,
    "profit_target": 4.00,
    "max_loss_dollars": 220.0,
    "max_gain_dollars": 360.0,
    "reward_risk_ratio": 1.64,
    "contracts": 1,
    "position_size_dollars": 220.0,
    "thesis": "SPY bull trend confirmed",
    "final_decision": TradeDecision.PENDING_APPROVAL,
    "iv_rank": 42.0,
    "iv_crush_risk": "low",
    "earnings_risk": "none",
    "risk_assessment_summary": "All checks pass",
    "reviewer_notes": "Approved",
    "legs": [
        {"option_type": "call", "strike": 503.0, "expiration_dte": 14,
         "action": "buy", "quantity": 1},
        {"option_type": "call", "strike": 508.0, "expiration_dte": 14,
         "action": "sell", "quantity": 1},
    ],
    "entry_trigger": "SPY holds above 503",
    "expiration": (date.today() + timedelta(days=14)).isoformat(),
}


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def app() -> FastAPI:
    """App instance with lifespan disabled — we inject deps manually."""
    return create_app()


@pytest.fixture
async def platform(app: FastAPI):
    """
    Inject a mock orchestrator + real in-memory state store into the app.
    Yields the state_store so tests can pre-populate or inspect it.
    Tears down cleanly after each test.
    """
    bus = MessageBus()
    state_store = SharedStateStore()

    mock_orchestrator = MagicMock()
    mock_orchestrator.analyze = AsyncMock(return_value=_ANALYSIS_RESULT)
    mock_orchestrator.analyze_batch = AsyncMock(
        side_effect=lambda tickers, **kw: {t: _ANALYSIS_RESULT for t in tickers}
    )

    deps.set_platform(bus, state_store, mock_orchestrator, [])
    yield state_store, mock_orchestrator, bus
    deps.clear_platform()


@pytest.fixture
async def client(app: FastAPI, platform):
    """AsyncClient pointed at the app (no lifespan startup)."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ── Health ────────────────────────────────────────────────────────────────

class TestHealth:
    @pytest.mark.asyncio
    async def test_health_ok(self, client: httpx.AsyncClient):
        resp = await client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert "mode" in body


# ── Analysis endpoints ────────────────────────────────────────────────────

class TestAnalysisSync:
    @pytest.mark.asyncio
    async def test_sync_returns_recommendation(self, client: httpx.AsyncClient):
        resp = await client.post("/api/v1/analysis/sync", json={"ticker": "SPY"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ticker"] == "SPY"
        assert body["strategy"] == "bull_call_spread"
        assert body["max_loss_dollars"] == 220.0

    @pytest.mark.asyncio
    async def test_sync_ticker_uppercased(
        self, client: httpx.AsyncClient, platform
    ):
        _, orchestrator, _ = platform
        await client.post("/api/v1/analysis/sync", json={"ticker": "spy"})
        orchestrator.analyze.assert_awaited_with("SPY", timeout=120.0)

    @pytest.mark.asyncio
    async def test_sync_invalid_ticker_rejected(self, client: httpx.AsyncClient):
        """Empty ticker should fail Pydantic validation → 422."""
        resp = await client.post("/api/v1/analysis/sync", json={"ticker": ""})
        assert resp.status_code == 422


class TestAnalysisAsync:
    @pytest.mark.asyncio
    async def test_async_returns_session_id(self, client: httpx.AsyncClient):
        resp = await client.post("/api/v1/analysis/", json={"ticker": "SPY"})
        assert resp.status_code == 200
        body = resp.json()
        assert "session_id" in body
        assert body["ticker"] == "SPY"
        assert body["status"] == "started"
        assert "poll_url" in body

    @pytest.mark.asyncio
    async def test_get_session_not_found(self, client: httpx.AsyncClient):
        resp = await client.get("/api/v1/analysis/nonexistent-session-id")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_list_sessions_empty(self, client: httpx.AsyncClient):
        resp = await client.get("/api/v1/analysis/")
        assert resp.status_code == 200
        body = resp.json()
        assert "sessions" in body
        assert "count" in body
        assert body["count"] == 0

    @pytest.mark.asyncio
    async def test_list_sessions_after_create(
        self, client: httpx.AsyncClient, platform
    ):
        state_store, _, _ = platform
        session = await state_store.create_session("SPY")
        await state_store.update(session.session_id, status="complete")

        resp = await client.get("/api/v1/analysis/")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] >= 1

    @pytest.mark.asyncio
    async def test_list_sessions_filter_by_status(
        self, client: httpx.AsyncClient, platform
    ):
        state_store, _, _ = platform
        s1 = await state_store.create_session("SPY")
        await state_store.update(s1.session_id, status="complete")
        s2 = await state_store.create_session("QQQ")
        # s2 stays pending

        resp = await client.get("/api/v1/analysis/?status=complete")
        assert resp.status_code == 200
        sessions = resp.json()["sessions"]
        assert all(s["status"] == "complete" for s in sessions)

    @pytest.mark.asyncio
    async def test_get_session_returns_data(
        self, client: httpx.AsyncClient, platform
    ):
        state_store, _, _ = platform
        session = await state_store.create_session("QQQ")
        await state_store.update(session.session_id, status="complete")

        resp = await client.get(f"/api/v1/analysis/{session.session_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ticker"] == "QQQ"
        assert body["status"] == "complete"


class TestBatchAnalysis:
    @pytest.mark.asyncio
    async def test_batch_returns_all_tickers(self, client: httpx.AsyncClient):
        resp = await client.post(
            "/api/v1/analysis/batch",
            json={"tickers": ["SPY", "QQQ"]},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert set(body["tickers"]) == {"SPY", "QQQ"}
        assert "results" in body

    @pytest.mark.asyncio
    async def test_batch_empty_list_rejected(self, client: httpx.AsyncClient):
        resp = await client.post("/api/v1/analysis/batch", json={"tickers": []})
        assert resp.status_code == 422


class TestApprovalEndpoint:
    @pytest.mark.asyncio
    async def test_approve_nonexistent_session_404(self, client: httpx.AsyncClient):
        resp = await client.post(
            "/api/v1/analysis/nonexistent/approve",
            json={"approved": True, "user": "test"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_approve_session_without_recommendation_400(
        self, client: httpx.AsyncClient, platform
    ):
        state_store, _, _ = platform
        session = await state_store.create_session("SPY")

        resp = await client.post(
            f"/api/v1/analysis/{session.session_id}/approve",
            json={"approved": True, "user": "test"},
        )
        assert resp.status_code == 400


# ── Agent / bus endpoints ─────────────────────────────────────────────────

class TestAgentEndpoints:
    @pytest.mark.asyncio
    async def test_agent_status(self, client: httpx.AsyncClient):
        resp = await client.get("/api/v1/agents/")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "running"
        assert "bus_stats" in body

    @pytest.mark.asyncio
    async def test_bus_stats(self, client: httpx.AsyncClient):
        resp = await client.get("/api/v1/agents/bus/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert "published" in body
        assert "errors" in body


# ── Trade journal endpoints ───────────────────────────────────────────────

class TestTradeEndpoints:
    @pytest.mark.asyncio
    async def test_open_trades_no_db(self, client: httpx.AsyncClient, tmp_path, monkeypatch):
        """Returns empty list when journal DB doesn't exist yet."""
        monkeypatch.chdir(tmp_path)
        resp = await client.get("/api/v1/trades/open")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 0

    @pytest.mark.asyncio
    async def test_performance_no_db(self, client: httpx.AsyncClient, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        resp = await client.get("/api/v1/trades/performance")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_history_no_db(self, client: httpx.AsyncClient, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        resp = await client.get("/api/v1/trades/history")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 0

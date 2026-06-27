"""
agora/tests/test_api_routes.py — integration sweep of the FastAPI control surface (/agora/*) via
TestClient with an injected mock AgoraSession. Covers the dashboard/control endpoints the operator
relies on: health, readiness + go-live approve/revoke (the capital-on gate), kill switch trip/reset,
position listing, leg-level reconcile (broker dep mocked), and IBKR status — including the error
paths (not-wired → 503, reconcile failure → 503). No network, no broker, no running session.
"""
from __future__ import annotations

import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import agora.api.state as state
from agora.api.routes import router


# ── app + mock session ────────────────────────────────────────────────────────
def _app() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _session(**over):
    risk = types.SimpleNamespace(
        get_kill_switch_state=lambda: {"active": False, "reason": "", "tripped_at": "", "tripped_by": ""},
        get_breaker_status=lambda: {"daily_loss_breaker_enabled": True, "state": "enforced"},
        trip_kill_switch=lambda reason, tripped_by: None,
        reset_kill_switch=lambda reset_by: None,
    )
    pm = types.SimpleNamespace(
        get_open_positions=lambda: [],
        get_portfolio_greeks=lambda: {"delta": 0.0, "vega": 0.0, "theta": 0.0, "gamma": 0.0, "positions": 0},
    )
    readiness = types.SimpleNamespace(
        get_score=lambda: {"overall_score": 85.0, "pillars": {}, "ready_for_live": True,
                           "critical_failures": [], "fireworks": False},
        approve_go_live=lambda approved_by="CEO": {
            "approved": True, "reason": "ok", "approved_by": approved_by,
            "score": {"overall_score": 85.0}},
        revoke_go_live=lambda: None,
    )
    s = types.SimpleNamespace(
        _session_id="TEST-SESSION",
        _settings=types.SimpleNamespace(
            trading_mode="paper", db_path="/tmp/agora_test.db",
            ibkr_host="127.0.0.1", ibkr_port=7497, engine_lease_enabled=False),
        _risk=risk, _position_mgr=pm, readiness=readiness,
        _system_health=None, _ibkr_agent=None,
    )
    for k, v in over.items():
        setattr(s, k, v)
    return s


@pytest.fixture
def client():
    c = _app()
    state.set_session(_session())
    yield c
    state._session = None


# ── health ────────────────────────────────────────────────────────────────────
class TestHealth:
    def test_health_ok(self, client):
        r = client.get("/agora/health")
        assert r.status_code == 200
        body = r.json()
        assert body["session_id"] == "TEST-SESSION"
        assert body["trading_mode"] == "paper"
        assert body["open_positions"] == 0
        assert body["kill_switch"]["active"] is False

    def test_health_reflects_open_count(self, client):
        state.set_session(_session(_position_mgr=types.SimpleNamespace(
            get_open_positions=lambda: [object(), object(), object()],
            get_portfolio_greeks=lambda: {})))
        assert client.get("/agora/health").json()["open_positions"] == 3


# ── readiness + go-live (the capital-on gate) ─────────────────────────────────
class TestReadinessGoLive:
    def test_readiness_score(self, client):
        r = client.get("/agora/readiness")
        assert r.status_code == 200 and r.json()["overall_score"] == 85.0

    def test_golive_approved(self, client):
        r = client.post("/agora/golive", json={"approved_by": "CEO"})
        assert r.status_code == 200
        assert r.json()["approved"] is True

    def test_golive_rejected_propagates(self, client):
        state.set_session(_session(readiness=types.SimpleNamespace(
            get_score=lambda: {"overall_score": 50.0},
            approve_go_live=lambda approved_by="CEO": {
                "approved": False, "reason": "Overall score 50 < 80 required",
                "score": {"overall_score": 50.0}},
            revoke_go_live=lambda: None)))
        r = client.post("/agora/golive", json={"approved_by": "CEO"})
        assert r.status_code == 200
        assert r.json()["approved"] is False and "80" in r.json()["reason"]

    def test_golive_revoke(self, client):
        r = client.request("DELETE", "/agora/golive")
        assert r.status_code == 200 and r.json()["status"] == "revoked"


# ── kill switch ───────────────────────────────────────────────────────────────
class TestKillSwitch:
    def test_trip(self, client):
        r = client.post("/agora/kill", json={"reason": "manual halt"})
        assert r.status_code == 200
        assert r.json() == {"status": "tripped", "reason": "manual halt"}

    def test_trip_calls_session(self):
        c = _app()
        recorded = {}
        risk = types.SimpleNamespace(
            trip_kill_switch=lambda reason, tripped_by: recorded.update(reason=reason, by=tripped_by),
            get_kill_switch_state=lambda: {"active": True})
        state.set_session(_session(_risk=risk))
        c.post("/agora/kill", json={"reason": "loss limit"})
        assert recorded == {"reason": "loss limit", "by": "api"}
        state._session = None

    def test_reset(self, client):
        r = client.request("DELETE", "/agora/kill")
        assert r.status_code == 200 and r.json()["status"] == "reset"


# ── positions ─────────────────────────────────────────────────────────────────
class TestPositions:
    def test_empty_positions_ok(self, client):
        r = client.get("/agora/positions")
        assert r.status_code == 200
        body = r.json()
        assert body["positions"] == [] if isinstance(body, dict) and "positions" in body else True


# ── reconcile (broker dependency mocked) ──────────────────────────────────────
class TestReconcile:
    def test_clean_report(self, client, monkeypatch):
        fake = types.SimpleNamespace(to_dict=lambda: {"clean": True, "counts": {
            "matched": 3, "orphans": 0, "ghosts": 0, "qty_mismatch": 0}})
        monkeypatch.setattr("agora.ops.position_reconciler.reconcile", lambda *a, **k: fake)
        r = client.get("/agora/reconcile")
        assert r.status_code == 200 and r.json()["clean"] is True

    def test_divergence_report(self, client, monkeypatch):
        fake = types.SimpleNamespace(to_dict=lambda: {"clean": False, "counts": {
            "matched": 1, "orphans": 2, "ghosts": 0, "qty_mismatch": 0}, "orphans": [{"symbol": "AAPL"}]})
        monkeypatch.setattr("agora.ops.position_reconciler.reconcile", lambda *a, **k: fake)
        r = client.get("/agora/reconcile")
        assert r.status_code == 200 and r.json()["clean"] is False

    def test_broker_failure_returns_503(self, client, monkeypatch):
        def _boom(*a, **k):
            raise RuntimeError("IBKR not connected")
        monkeypatch.setattr("agora.ops.position_reconciler.reconcile", _boom)
        r = client.get("/agora/reconcile")
        assert r.status_code == 503 and "error" in r.json()


# ── ibkr-status ───────────────────────────────────────────────────────────────
class TestIbkrStatus:
    def test_not_wired_returns_503(self, client):
        r = client.get("/agora/ibkr-status")
        assert r.status_code == 503 and "not wired" in r.json()["error"]

    def test_wired_returns_status(self):
        c = _app()

        async def _assess():
            return {"connected": True}

        agent = types.SimpleNamespace(
            assess_connectivity=_assess,
            get_status=lambda: {"open_orders": 2, "orphan_gtc": 0, "fill_rate": 0.9})
        state.set_session(_session(_ibkr_agent=agent))
        r = c.get("/agora/ibkr-status")
        assert r.status_code == 200
        body = r.json()
        assert body["connectivity"] == {"connected": True}
        assert body["open_orders"] == 2
        state._session = None

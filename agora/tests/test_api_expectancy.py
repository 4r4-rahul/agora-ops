"""
agora/tests/test_api_expectancy.py — the /agora/expectancy + /agora/exit-regret endpoints via
TestClient with a mock session over a seeded temp DB. Confirms the meter + regret report reach the
dashboard wired end-to-end.
"""
from __future__ import annotations

import sqlite3
import tempfile
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import agora.api.state as state
from agora.api.routes import router


def _seed_db():
    p = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    c = sqlite3.connect(p)
    c.execute("""CREATE TABLE positions (strategy TEXT, status TEXT, close_date TEXT,
                 close_source TEXT, realized_pnl REAL, regime_at_entry TEXT DEFAULT 'neutral')""")
    c.executemany("INSERT INTO positions (strategy, status, close_date, close_source, realized_pnl) "
                  "VALUES ('bull_put_spread','closed',?,'thesis_exit',?)",
                  [("2026-06-01", -200.0), ("2026-06-20", 30.0), ("2026-06-21", 20.0)])
    c.commit(); c.close()
    return p


def _session(db):
    return types.SimpleNamespace(_settings=types.SimpleNamespace(
        db_path=db, expectancy_target_per_trade=25.0,
        expectancy_target_date="2026-09-30", expectancy_legacy_cutoff_date="2026-06-12"))


@pytest.fixture
def client():
    app = FastAPI(); app.include_router(router)
    c = TestClient(app)
    yield c
    state._session = None


class TestExpectancyEndpoint:
    def test_meter_payload(self, client):
        state.set_session(_session(_seed_db()))
        r = client.get("/agora/expectancy")
        assert r.status_code == 200
        m = r.json()
        assert m["target"]["per_trade"] == 25.0
        assert m["target"]["locked"] is True
        # post-fix excludes the −200 legacy → current = avg(30,20) = 25
        assert m["current"]["expectancy"] == 25.0
        assert m["baseline_expectancy"] < 25.0          # all-time dragged by legacy
        assert "today" in m["periods"] and "projection" in m

    def test_meter_uses_config_target(self, client):
        s = _session(_seed_db())
        s._settings.expectancy_target_per_trade = 50.0
        state.set_session(s)
        assert client.get("/agora/expectancy").json()["target"]["per_trade"] == 50.0


class TestExitRegretEndpoint:
    def test_empty_report_ok(self, client):
        # no post_close_watch table yet → report is created empty, not a 500
        state.set_session(_session(_seed_db()))
        r = client.get("/agora/exit-regret")
        assert r.status_code == 200
        body = r.json()
        assert body.get("total_evaluated", 0) == 0 and "by_exit_reason" in body

    def test_report_after_watch(self, client):
        from datetime import date, timedelta

        from agora.ops.post_close_watch import evaluate_due, record_close
        db = _seed_db()
        pos = types.SimpleNamespace(position_id="p1", ticker="QQQ", direction="bullish",
                                    unrealized_pnl=-50.0,
                                    strategy=types.SimpleNamespace(value="long_call"))
        record_close(db, pos, "stop_loss", spot=100.0)
        evaluate_due(db, price_fn=lambda t, s, e: [106.0], today=date.today() + timedelta(days=8))
        state.set_session(_session(db))
        r = client.get("/agora/exit-regret")
        assert r.status_code == 200
        assert r.json()["by_exit_reason"]["stop_loss"]["early_exit_rate"] == 1.0

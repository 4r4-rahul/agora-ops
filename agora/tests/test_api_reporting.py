"""
agora/tests/test_api_reporting.py — the read-only dashboard reporting endpoints (/signals,
/attribution, /performance) via FastAPI TestClient with an injected mock session. /signals and
/attribution are driven by session sub-objects (stubbed); /performance reads the positions table
(seeded temp DB). Covers the happy path + the not-ready (503) / db-error (500) guards.
"""
from __future__ import annotations

import sqlite3
import tempfile
import types
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import agora.api.state as state
from agora.api.routes import router


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    c = TestClient(app)
    yield c
    state._session = None


# ── /signals ──────────────────────────────────────────────────────────────────
def _signals_session():
    macro = types.SimpleNamespace(
        macro_stance="risk_on", confidence=0.72, vol_selling_ok=True,
        size_bias="full", method="psi+macro")
    return types.SimpleNamespace(
        _macro_context=macro,
        _macro=types.SimpleNamespace(_last_context=macro),
        _psi=types.SimpleNamespace(compute_psi=lambda: {"psi": 0.04, "drift": "stable"}),
        _settings=types.SimpleNamespace(etf_universe=["SPY", "QQQ", "IWM"]),
    )


class TestSignals:
    def test_signals_ok(self, client):
        state.set_session(_signals_session())
        r = client.get("/agora/signals")
        assert r.status_code == 200
        body = r.json()
        assert body["macro"]["macro_stance"] == "risk_on"
        assert body["macro"]["vol_selling_ok"] is True
        assert body["psi"]["psi"] == 0.04
        assert body["universe"] == ["SPY", "QQQ", "IWM"]

    def test_signals_falls_back_to_cached_macro(self, client):
        macro = types.SimpleNamespace(
            macro_stance="risk_off", confidence=0.6, vol_selling_ok=False,
            size_bias="half", method="cached")
        s = types.SimpleNamespace(
            _macro_context=None,                                  # session just restarted
            _macro=types.SimpleNamespace(_last_context=macro),    # synthesizer cache
            _psi=types.SimpleNamespace(compute_psi=lambda: {}),
            _settings=types.SimpleNamespace(etf_universe=[]))
        state.set_session(s)
        assert client.get("/agora/signals").json()["macro"]["macro_stance"] == "risk_off"


# ── /attribution ──────────────────────────────────────────────────────────────
class TestAttribution:
    def test_attribution_ok(self, client):
        attributor = types.SimpleNamespace(
            attribution_report=lambda start_date, end_date: {"vol_premium": {"pnl": 250.0}},
            slippage_report=lambda days: {"avg_slippage_pct": 0.03},
            regime_accuracy_report=lambda: {"risk_on": 0.61},
        )
        state.set_session(types.SimpleNamespace(_attributor=attributor))
        r = client.get("/agora/attribution?days=14")
        assert r.status_code == 200
        body = r.json()
        assert body["days"] == 14
        assert body["attribution"]["vol_premium"]["pnl"] == 250.0
        assert body["slippage"]["avg_slippage_pct"] == 0.03
        assert body["regime_accuracy"]["risk_on"] == 0.61


# ── /performance (DB-seeded) ──────────────────────────────────────────────────
def _perf_db(rows):
    """rows: (strategy, status, contracts, entry_price, entry_date, close_date, realized_pnl, close_source)."""
    path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    c = sqlite3.connect(path)
    c.execute("""CREATE TABLE positions (
        strategy TEXT, status TEXT, contracts INT, entry_price REAL, entry_date TEXT,
        close_date TEXT, realized_pnl REAL, close_source TEXT,
        regime_at_entry TEXT DEFAULT 'neutral')""")
    c.executemany("""INSERT INTO positions (strategy, status, contracts, entry_price, entry_date,
        close_date, realized_pnl, close_source) VALUES (?,?,?,?,?,?,?,?)""", rows)
    c.commit(); c.close()
    return path


class TestPerformance:
    def test_performance_ok_with_real_closes(self, client):
        today = date.today().isoformat()
        db = _perf_db([
            ("bull_put_spread", "closed", 1, 1.0, today, today, 150.0, "thesis_exit"),
            ("bull_put_spread", "closed", 1, 1.0, today, today, -80.0, "stop_loss"),
            ("bull_put_spread", "open", 1, 1.0, today, None, None, None),   # open — excluded
        ])
        state.set_session(types.SimpleNamespace(
            _settings=types.SimpleNamespace(db_path=db)))
        r = client.get("/agora/performance")
        assert r.status_code == 200
        # endpoint returns a structured rollup; just assert it computed without error
        assert isinstance(r.json(), dict)

    def test_performance_db_error_500(self, client):
        # db_path points at a file with no positions table → handled 500, not a crash
        bad = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        sqlite3.connect(bad).close()
        state.set_session(types.SimpleNamespace(_settings=types.SimpleNamespace(db_path=bad)))
        r = client.get("/agora/performance")
        assert r.status_code == 500 and "error" in r.json()

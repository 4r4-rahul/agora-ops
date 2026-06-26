"""
agora/tests/test_cro_risk_audit.py — the CRO's deterministic risk audit + self-heal. self_audit
raises threshold findings (kill switch, greek utilization, stop-loss proximity, daily-loss %,
correlation concentration, circuit breaker); self_heal turns the critical ones into autonomous
lateral actions (halt entries, cut size to half/none). No LLM — these must be exact, since a missed
band fails to throttle a blowup and a false band needlessly halts trading.
"""
from __future__ import annotations

import sqlite3
import tempfile
import types

import pytest

from agora.c_suite.cro import CROAgent


# ── self_audit harness ────────────────────────────────────────────────────────
def _audit_stub(greeks=None, positions=None, kill=False, cb_tripped=False,
                daily_rows=(), settings_over=None):
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    conn = sqlite3.connect(db)
    # Full schema so the CRO's daily-loss query can apply _REAL_CLOSE (it now excludes adopted/
    # fiction — see 7b3a2c9). daily_rows are REAL closes (status=closed, source=lifecycle).
    conn.execute("CREATE TABLE positions (realized_pnl REAL, close_date TEXT, status TEXT DEFAULT 'closed', "
                 "close_source TEXT DEFAULT 'lifecycle', regime_at_entry TEXT DEFAULT 'neutral')")
    for pnl in daily_rows:
        conn.execute(
            "INSERT INTO positions (realized_pnl, close_date, status, close_source, regime_at_entry) "
            "VALUES (?, date('now'), 'closed', 'lifecycle', 'neutral')", (pnl,))
    conn.commit()
    conn.close()

    s = types.SimpleNamespace(
        max_portfolio_delta=100.0, max_portfolio_vega=5000.0, stop_loss_multiplier=2.0,
        daily_loss_limit_dollars=2000.0, max_per_correlation_group=2, db_path=db,
    )
    for k, v in (settings_over or {}).items():
        setattr(s, k, v)

    return types.SimpleNamespace(
        _settings=s,
        _risk_council=types.SimpleNamespace(
            is_kill_switch_active=lambda: kill,
            get_kill_switch_state=lambda: {
                "reason": "daily loss", "tripped_at": "2026-01-01T09:30:00", "tripped_by": "auto"},
        ),
        _position_mgr=types.SimpleNamespace(
            get_portfolio_greeks=lambda: greeks or {"delta": 0, "vega": 0},
            get_open_positions=lambda: positions or [],
        ),
        _circuit_breaker=types.SimpleNamespace(get_state=lambda: {"tripped": cb_tripped, "reason": "x"}),
    )


def _keys(findings):
    return {k for k, _, _ in findings}


def _sev(findings, key):
    return next(sev for k, sev, _ in findings if k == key)


def _pos(ticker, pillar="vol_premium", entry_price=0.0, unrealized_pnl=0.0, contracts=1):
    return types.SimpleNamespace(ticker=ticker, pillar=pillar, entry_price=entry_price,
                                 unrealized_pnl=unrealized_pnl, contracts=contracts)


class TestSelfAudit:
    def test_clean_state_no_findings(self):
        assert CROAgent.self_audit(_audit_stub()) == []

    def test_kill_switch_active_is_critical(self):
        f = CROAgent.self_audit(_audit_stub(kill=True))
        assert "kill_switch_active" in _keys(f) and _sev(f, "kill_switch_active") == "critical"

    def test_delta_breach_vs_high_vs_ok(self):
        assert "delta_limit_breached" in _keys(CROAgent.self_audit(_audit_stub(greeks={"delta": 150, "vega": 0})))
        f_high = CROAgent.self_audit(_audit_stub(greeks={"delta": 85, "vega": 0}))
        assert "delta_limit_high" in _keys(f_high) and _sev(f_high, "delta_limit_high") == "warning"
        assert _keys(CROAgent.self_audit(_audit_stub(greeks={"delta": 50, "vega": 0}))) == set()

    def test_vega_breach_vs_high(self):
        assert "vega_limit_breached" in _keys(CROAgent.self_audit(_audit_stub(greeks={"delta": 0, "vega": 6000})))
        assert "vega_limit_high" in _keys(CROAgent.self_audit(_audit_stub(greeks={"delta": 0, "vega": 4500})))

    def test_daily_loss_critical_vs_high(self):
        # limit 2000; -1800 = 90% → critical
        assert "daily_loss_critical" in _keys(CROAgent.self_audit(_audit_stub(daily_rows=(-1800.0,))))
        # -1300 = 65% → high
        assert "daily_loss_high" in _keys(CROAgent.self_audit(_audit_stub(daily_rows=(-1300.0,))))
        # -500 = 25% → none
        assert _keys(CROAgent.self_audit(_audit_stub(daily_rows=(-500.0,)))) == set()

    def test_stop_loss_proximity_critical(self):
        # entry 3 ×100 ×1 ×2 ×-1 = -600 stop; upnl -500 < -450 (75%) → critical
        f = CROAgent.self_audit(_audit_stub(positions=[_pos("AAPL", entry_price=3.0, unrealized_pnl=-500.0)]))
        assert "stop_loss_proximity_AAPL" in _keys(f)

    def test_correlation_concentration_warning(self):
        positions = [_pos("A", pillar="vol_premium"), _pos("B", pillar="vol_premium"),
                     _pos("C", pillar="vol_premium")]   # 3 > limit 2
        f = CROAgent.self_audit(_audit_stub(positions=positions))
        assert any(k.startswith("concentration_") for k in _keys(f))

    def test_circuit_breaker_tripped(self):
        f = CROAgent.self_audit(_audit_stub(cb_tripped=True))
        assert "circuit_breaker_tripped" in _keys(f) and _sev(f, "circuit_breaker_tripped") == "critical"


# ── self_heal harness ─────────────────────────────────────────────────────────
def _heal_stub():
    calls: list[tuple] = []

    async def _notify(event, payload):
        calls.append((event, payload))

    stub = types.SimpleNamespace(_heal_attempts={}, notify_peers=_notify)
    return stub, calls


class TestSelfHeal:
    @pytest.mark.asyncio
    async def test_kill_switch_publishes_halt_once(self):
        stub, calls = _heal_stub()
        f = [("kill_switch_active", "critical", "x")]
        await CROAgent.self_heal(stub, f)
        await CROAgent.self_heal(stub, f)   # second call must NOT re-publish
        halts = [c for c in calls if c[0] == "kill_switch_tripped"]
        assert len(halts) == 1 and halts[0][1]["halt_new_entries"] is True

    @pytest.mark.asyncio
    async def test_daily_loss_critical_cuts_size_to_half(self):
        stub, calls = _heal_stub()
        await CROAgent.self_heal(stub, [("daily_loss_critical", "critical", "x")])
        size = [c for c in calls if c[0] == "size_bias_changed"]
        assert size and size[0][1]["new_bias"] == "half"
        assert any(c[0] == "daily_loss_warning" and c[1]["level"] == "critical" for c in calls)

    @pytest.mark.asyncio
    async def test_daily_loss_high_warns_only(self):
        stub, calls = _heal_stub()
        await CROAgent.self_heal(stub, [("daily_loss_high", "warning", "x")])
        assert [c for c in calls if c[0] == "daily_loss_warning"][0][1]["level"] == "warning"
        assert not [c for c in calls if c[0] == "size_bias_changed"]   # no size cut on 'high'

    @pytest.mark.asyncio
    async def test_greek_breach_blocks_directional(self):
        stub, calls = _heal_stub()
        await CROAgent.self_heal(stub, [("delta_limit_breached", "critical", "x")])
        size = [c for c in calls if c[0] == "size_bias_changed"]
        assert size and size[0][1]["new_bias"] == "none"

    @pytest.mark.asyncio
    async def test_no_findings_no_actions(self):
        stub, calls = _heal_stub()
        await CROAgent.self_heal(stub, [])
        assert calls == []

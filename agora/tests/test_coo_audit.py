"""
agora/tests/test_coo_audit.py — the COO's operational self_audit: the deterministic patrol that
surfaces ghost fills (broker trades with no shadow-book position), fill-rate / timeout collapse,
Error-201 storms, orphan GTC orders, unhealthy IVR feed, stale prices, IBKR disconnect, and system
health failures. No LLM — a missed finding leaves a broker/shadow-book divergence unflagged.
Pattern mirrors test_cro_risk_audit: bind the real method to a minimal stub.
"""
from __future__ import annotations

import sqlite3
import tempfile
import types

from agora.c_suite.coo import COOAgent


def _db(ghost_fills=(), positions=()):
    """ghost_fills: list of tickers with a confirmed fill today; positions: tickers in the book."""
    path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE execution_quality (ticker TEXT, fill_price REAL, attempt_date TEXT, outcome TEXT)")
    c.execute("CREATE TABLE positions (ticker TEXT, regime_at_entry TEXT DEFAULT 'neutral')")
    for t in ghost_fills:
        c.execute("INSERT INTO execution_quality VALUES (?,1.5,date('now'),'fill')", (t,))
    for t in positions:
        c.execute("INSERT INTO positions (ticker) VALUES (?)", (t,))
    c.commit(); c.close()
    return path


def _stub(*, ghost_fills=(), positions=(), eq_stats=None, session_stats=None,
          orphans_cancelled=0, ivr_healthy=True, stale=(), ibkr_healthy=True, health_ok=True):
    db = _db(ghost_fills, positions)
    eq = types.SimpleNamespace(
        get_today_db_stats=lambda: (eq_stats or {"total": 0, "fills": 0,
                                                 "fill_rate": None, "timeout_rate": None}),
        get_session_stats=lambda: (session_stats or {}),
    )
    di = types.SimpleNamespace(
        ivr_feed_healthy=ivr_healthy,
        get_stale_tickers=lambda: list(stale),
    )
    return types.SimpleNamespace(
        _settings=types.SimpleNamespace(db_path=db),
        _eq=eq,
        _reconciler=types.SimpleNamespace(last_orphans_cancelled=orphans_cancelled),
        _di=di,
        _ibkr_agent=types.SimpleNamespace(get_status=lambda: {"connection_healthy": ibkr_healthy}),
        _health=types.SimpleNamespace(is_healthy=lambda: health_ok),
    )


def _keys(findings):
    return {k for k, _, _ in findings}


def _sev(findings, key):
    return next(s for k, s, _ in findings if k == key)


class TestCOOSelfAudit:
    def test_clean_state_no_findings(self):
        assert COOAgent.self_audit(_stub()) == []

    def test_ghost_fill_detected(self):
        # AAPL filled today but no AAPL position → ghost
        f = COOAgent.self_audit(_stub(ghost_fills=["AAPL"], positions=["MSFT"]))
        assert "ghost_fills_detected" in _keys(f) and _sev(f, "ghost_fills_detected") == "critical"

    def test_no_ghost_when_position_exists(self):
        # filled AND tracked → not a ghost
        assert "ghost_fills_detected" not in _keys(
            COOAgent.self_audit(_stub(ghost_fills=["AAPL"], positions=["AAPL"])))

    def test_fill_rate_critical(self):
        f = COOAgent.self_audit(_stub(eq_stats={"total": 10, "fills": 1, "fill_rate": 0.10, "timeout_rate": 0.0}))
        assert "fill_rate_critical" in _keys(f)

    def test_fill_rate_ok_above_threshold(self):
        assert "fill_rate_critical" not in _keys(
            COOAgent.self_audit(_stub(eq_stats={"total": 10, "fills": 8, "fill_rate": 0.80, "timeout_rate": 0.0})))

    def test_low_volume_not_flagged(self):
        # total < 3 → no fill-rate verdict even at 0% fills
        assert _keys(COOAgent.self_audit(
            _stub(eq_stats={"total": 2, "fills": 0, "fill_rate": 0.0, "timeout_rate": 1.0}))) == set()

    def test_timeout_rate_critical(self):
        f = COOAgent.self_audit(_stub(eq_stats={"total": 5, "fills": 4, "fill_rate": 0.80, "timeout_rate": 0.90}))
        assert "timeout_rate_critical" in _keys(f)

    def test_error_201_storm(self):
        f = COOAgent.self_audit(_stub(
            eq_stats={"total": 5, "fills": 4, "fill_rate": 0.80, "timeout_rate": 0.0},
            session_stats={"error_201_storm": True}))
        assert "error_201_storm" in _keys(f)

    def test_orphan_gtc_cancelled_warning(self):
        f = COOAgent.self_audit(_stub(orphans_cancelled=3))
        assert "orphan_gtc_orders_cancelled" in _keys(f) and _sev(f, "orphan_gtc_orders_cancelled") == "warning"

    def test_ivr_feed_unhealthy(self):
        f = COOAgent.self_audit(_stub(ivr_healthy=False))
        assert "ivr_feed_unhealthy" in _keys(f) and _sev(f, "ivr_feed_unhealthy") == "critical"

    def test_stale_price_feeds_warning(self):
        f = COOAgent.self_audit(_stub(stale=["A", "B", "C", "D", "E"]))   # >3
        assert "stale_price_feeds" in _keys(f)

    def test_few_stale_not_flagged(self):
        assert "stale_price_feeds" not in _keys(COOAgent.self_audit(_stub(stale=["A", "B"])))

    def test_ibkr_connection_unhealthy(self):
        f = COOAgent.self_audit(_stub(ibkr_healthy=False))
        assert "ibkr_connection_unhealthy" in _keys(f)

    def test_ibkr_unhealthy_suppressed_by_fresh_portfolio_poll(self):
        # connection_healthy is stale-False (its 30-min scan ran during an outage) BUT the 90s
        # portfolio poller succeeded 30s ago → orders demonstrably flow → no false alarm.
        stub = _stub(ibkr_healthy=False)
        stub._ibkr_agent = types.SimpleNamespace(
            get_status=lambda: {"connection_healthy": False, "portfolio_poll_age_secs": 30.0})
        assert "ibkr_connection_unhealthy" not in _keys(COOAgent.self_audit(stub))

    def test_ibkr_unhealthy_fires_when_poll_also_stale(self):
        # No fresh poll evidence (poll age > 180s) → the alarm is real and must fire.
        stub = _stub(ibkr_healthy=False)
        stub._ibkr_agent = types.SimpleNamespace(
            get_status=lambda: {"connection_healthy": False, "portfolio_poll_age_secs": 600.0})
        assert "ibkr_connection_unhealthy" in _keys(COOAgent.self_audit(stub))

    def test_system_health_failing(self):
        f = COOAgent.self_audit(_stub(health_ok=False))
        assert "system_health_failing" in _keys(f)

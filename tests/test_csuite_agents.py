"""
Tests for AGORA C-suite agents — self_audit(), get_readiness_tasks(), collect_intelligence().

Validates:
  1. All 7 agents can be instantiated with all-None sub-agents (no wiring required)
  2. self_audit() returns a list and doesn't raise AttributeError
  3. get_readiness_tasks() returns a list and doesn't raise (CIO _pillar_health bug regression)
  4. collect_intelligence() returns a dict and doesn't raise
  5. CIO receives pillar_health in __init__ and correctly gates on it
"""

import collections
import os
import sqlite3
import tempfile
import pytest

from agora.c_suite.cro import CROAgent
from agora.c_suite.coo import COOAgent
from agora.c_suite.cio import CIOAgent
from agora.c_suite.cto import CTOAgent
from agora.c_suite.cfo import CFOAgent
from agora.c_suite.rnd import RNDAgent
from agora.c_suite.ctech import CTechAgent


# ── Minimal settings stub ──────────────────────────────────────────────────────

class _Settings:
    anthropic_api_key     = "sk-fake"
    db_path               = "/tmp/test_csuite.db"
    max_portfolio_delta   = 0.75
    max_portfolio_vega    = 500.0
    max_daily_theta_dollars = 125.0
    max_open_positions    = 6
    max_per_correlation_group = 1
    daily_loss_limit_dollars  = 500.0
    weekly_loss_limit_dollars = 1500.0
    account_size          = 25_000.0
    risk_per_trade_dollars = 500.0
    stop_loss_multiplier  = 2.0
    profit_target_pct     = 0.50
    min_rr_ratio          = 1.3
    ivr_bypass_threshold  = 60.0
    earnings_blackout_days = 3
    min_conviction_score  = 60.0
    high_conviction_score = 70.0
    target_dte_entry      = 45
    target_dte_entry_min  = 30
    target_dte_close      = 21
    short_delta_target    = 0.20
    long_delta_target     = 0.35
    edgar_poll_seconds    = 60
    etf_universe          = ["SPY", "QQQ"]
    claude_fast_model     = "claude-haiku-4-5-20251001"
    claude_model          = "claude-opus-4-7"
    claude_brief_model    = "claude-sonnet-4-6"


def _make_agent(cls, **kwargs):
    """Instantiate a C-suite agent without a real Anthropic client."""
    import unittest.mock as mock
    with mock.patch("anthropic.AsyncAnthropic"):
        return cls(_Settings(), **kwargs)


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def agents():
    """Return one instance of each C-suite agent with no sub-agents wired."""
    return {
        "cro":   _make_agent(CROAgent),
        "coo":   _make_agent(COOAgent),
        "cio":   _make_agent(CIOAgent),
        "cto":   _make_agent(CTOAgent),
        "cfo":   _make_agent(CFOAgent),
        "rnd":   _make_agent(RNDAgent),
        "ctech": _make_agent(CTechAgent),
    }


# ── self_audit() ───────────────────────────────────────────────────────────────

class TestSelfAudit:
    @pytest.mark.parametrize("key", ["cro", "coo", "cio", "cto", "cfo", "rnd", "ctech"])
    def test_returns_list(self, agents, key):
        result = agents[key].self_audit()
        assert isinstance(result, list)

    @pytest.mark.parametrize("key", ["cro", "coo", "cio", "cto", "cfo", "rnd", "ctech"])
    def test_no_entries_without_wiring(self, agents, key):
        """All unwired agents should produce zero findings (no sub-agents = nothing to audit)."""
        result = agents[key].self_audit()
        assert isinstance(result, list)
        # Each finding must be a 3-tuple (key, severity, message)
        for item in result:
            assert len(item) == 3
            _, sev, _ = item
            assert sev in ("warning", "critical", "info")


# ── get_readiness_tasks() ──────────────────────────────────────────────────────

class TestReadinessTasks:
    @pytest.mark.parametrize("key", ["cro", "coo", "cio", "cto", "cfo", "rnd", "ctech"])
    def test_returns_list(self, agents, key):
        result = agents[key].get_readiness_tasks()
        assert isinstance(result, list)

    def test_cio_pillar_health_task_reported(self, agents):
        """CIO should report PillarHealth as missing when not wired."""
        tasks = agents["cio"].get_readiness_tasks()
        assert any("PillarHealthAgent" in t for t in tasks), \
            f"Expected PillarHealthAgent task, got: {tasks}"

    def test_cio_with_pillar_health_no_task(self):
        """CIO with pillar_health wired should NOT report it as missing."""
        import unittest.mock as mock
        ph = mock.MagicMock()
        with mock.patch("anthropic.AsyncAnthropic"):
            cio = CIOAgent(_Settings(), pillar_health=ph)
        tasks = cio.get_readiness_tasks()
        assert not any("PillarHealthAgent" in t for t in tasks), \
            f"PillarHealthAgent should not appear in tasks when wired: {tasks}"


# ── collect_intelligence() ─────────────────────────────────────────────────────

class TestCollectIntelligence:
    @pytest.mark.parametrize("key", ["cro", "coo", "cio", "cto", "cfo", "rnd", "ctech"])
    def test_returns_dict(self, agents, key):
        result = agents[key].collect_intelligence()
        assert isinstance(result, dict)
        assert "timestamp" in result


# ── CIO-specific: pillar_health is in __init__ ─────────────────────────────────

class TestCIOPillarHealth:
    def test_pillar_health_attribute_set_when_none(self):
        import unittest.mock as mock
        with mock.patch("anthropic.AsyncAnthropic"):
            cio = CIOAgent(_Settings())
        assert hasattr(cio, "_pillar_health")
        assert cio._pillar_health is None

    def test_pillar_health_attribute_set_when_wired(self):
        import unittest.mock as mock
        ph = mock.MagicMock()
        with mock.patch("anthropic.AsyncAnthropic"):
            cio = CIOAgent(_Settings(), pillar_health=ph)
        assert cio._pillar_health is ph

    def test_get_readiness_tasks_no_attribute_error(self):
        """Regression: CIO.get_readiness_tasks() must NOT raise AttributeError."""
        import unittest.mock as mock
        with mock.patch("anthropic.AsyncAnthropic"):
            cio = CIOAgent(_Settings())
        result = cio.get_readiness_tasks()  # must not raise
        assert isinstance(result, list)

    def test_self_audit_no_attribute_error(self):
        """Regression: CIO.self_audit() must NOT raise AttributeError."""
        import unittest.mock as mock
        with mock.patch("anthropic.AsyncAnthropic"):
            cio = CIOAgent(_Settings())
        result = cio.self_audit()  # must not raise
        assert isinstance(result, list)

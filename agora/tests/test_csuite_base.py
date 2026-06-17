"""
agora/tests/test_csuite_base.py — the shared ExecutiveAgent base machinery every C-suite agent
inherits: the patrol recurrence/escalation engine (_run_patrol — critical escalates now, a warning
only after _RECURRENCE_THRESHOLD repeats, once), the CEO snapshot/audit-summary aggregations, alert
routing, and peer notification. A bug here mis-escalates (alert fatigue) or silently buries a
recurring operational problem across ALL executives.
"""
from __future__ import annotations

import types
from collections import deque
from datetime import UTC, datetime

import pytest

from agora.c_suite.base import _RECURRENCE_THRESHOLD, ExecutiveAgent


# ── _run_patrol harness ───────────────────────────────────────────────────────
def _patrol_stub(audit_returns):
    """audit_returns: a callable returning findings (so it can change across patrols)."""
    esc: list[tuple] = []
    healed: list = []

    async def _escalate(level, message):
        esc.append((level, message))

    async def _fmt(items, now):
        return "alert-body"

    async def _heal(findings):
        healed.append(findings)

    stub = types.SimpleNamespace(
        TITLE="TestExec",
        self_audit=audit_returns,
        _issue_counts={},
        _recurring_escalated=set(),
        _audit_history=deque(maxlen=48),
        _heal_attempts={},
        _escalate_to_ceo=_escalate,
        _format_patrol_alert=_fmt,
        self_heal=_heal,
    )
    return stub, esc, healed


async def _patrol(stub):
    await ExecutiveAgent._run_patrol(stub)


class TestRunPatrol:
    @pytest.mark.asyncio
    async def test_critical_escalates_immediately(self):
        stub, esc, _ = _patrol_stub(lambda: [("ghost_fills", "critical", "ghost detected")])
        await _patrol(stub)
        assert len(esc) == 1 and esc[0][0] == "critical"
        assert stub._issue_counts["ghost_fills"] == 1

    @pytest.mark.asyncio
    async def test_single_warning_not_escalated(self):
        stub, esc, _ = _patrol_stub(lambda: [("stale", "warning", "stale prices")])
        await _patrol(stub)
        assert esc == []
        assert stub._issue_counts["stale"] == 1

    @pytest.mark.asyncio
    async def test_recurring_warning_promotes_after_threshold(self):
        stub, esc, _ = _patrol_stub(lambda: [("stale", "warning", "stale prices")])
        for _ in range(_RECURRENCE_THRESHOLD):
            await _patrol(stub)
        # escalated exactly once, at the Nth patrol, promoted to critical
        assert len(esc) == 1 and esc[0][0] == "critical"
        assert "stale" in stub._recurring_escalated

    @pytest.mark.asyncio
    async def test_recurring_warning_not_re_escalated(self):
        stub, esc, _ = _patrol_stub(lambda: [("stale", "warning", "x")])
        for _ in range(_RECURRENCE_THRESHOLD + 3):
            await _patrol(stub)
        assert len(esc) == 1   # promoted once, never again while it persists

    @pytest.mark.asyncio
    async def test_resolved_issue_resets_counter(self):
        findings = [("stale", "warning", "x")]
        stub, esc, _ = _patrol_stub(lambda: findings)
        await _patrol(stub); await _patrol(stub)
        assert stub._issue_counts["stale"] == 2
        findings.clear()                      # issue resolved
        await _patrol(stub)
        assert "stale" not in stub._issue_counts   # counter reset
        assert "stale" not in stub._recurring_escalated

    @pytest.mark.asyncio
    async def test_history_appended_each_patrol(self):
        stub, _, _ = _patrol_stub(lambda: [("x", "warning", "y")])
        await _patrol(stub); await _patrol(stub)
        assert len(stub._audit_history) == 2
        assert stub._audit_history[-1]["findings"] == [("x", "warning", "y")]

    @pytest.mark.asyncio
    async def test_self_heal_called_with_findings(self):
        stub, _, healed = _patrol_stub(lambda: [("x", "warning", "y")])
        await _patrol(stub)
        assert healed == [[("x", "warning", "y")]]

    @pytest.mark.asyncio
    async def test_audit_exception_does_not_crash_patrol(self):
        def _boom():
            raise RuntimeError("audit blew up")
        stub, esc, _ = _patrol_stub(_boom)
        await _patrol(stub)   # must not raise
        assert esc == [] and len(stub._audit_history) == 1


# ── get_snapshot / get_audit_summary (CEO state collection) ───────────────────
class TestSnapshotAggregations:
    def _stub(self, issue_counts=None, history=None, latest_intel=None, brief="hi"):
        return types.SimpleNamespace(
            TITLE="TestExec",
            _issue_counts=issue_counts or {},
            _audit_history=deque(history or [], maxlen=48),
            _latest_intel=latest_intel or {},
            _latest_brief=brief,
            _last_brief_time=datetime(2026, 1, 5, tzinfo=UTC),
        )

    def test_snapshot_shape_and_recurring(self):
        s = self._stub(issue_counts={"a": 3, "b": 1}, latest_intel={"fill_rate": 0.9})
        snap = ExecutiveAgent.get_snapshot(s)
        assert snap["title"] == "TestExec"
        assert snap["open_issues"] == 2
        assert snap["recurring_issues"] == {"a": 3}   # only >=2
        assert snap["fill_rate"] == 0.9               # latest_intel merged in

    def test_snapshot_truncates_long_brief(self):
        snap = ExecutiveAgent.get_snapshot(self._stub(brief="x" * 1000))
        assert len(snap["latest_brief"]) == 400

    def test_snapshot_handles_no_brief(self):
        s = self._stub(brief="")
        snap = ExecutiveAgent.get_snapshot(s)
        assert snap["latest_brief"] is None

    def test_audit_summary(self):
        hist = [{"ts": "t1", "findings": [], "escalated": 0},
                {"ts": "t2", "findings": [("x", "critical", "m")], "escalated": 1}]
        s = self._stub(issue_counts={"x": _RECURRENCE_THRESHOLD, "y": 1}, history=hist)
        summ = ExecutiveAgent.get_audit_summary(s)
        assert summ["open_issue_count"] == 2
        assert summ["recurring"] == {"x": _RECURRENCE_THRESHOLD}   # >= threshold only
        assert summ["patrol_count"] == 2
        assert summ["last_patrol"]["escalated"] == 1


# ── receive_alert + notify_peers ──────────────────────────────────────────────
class TestAlertRouting:
    @pytest.mark.asyncio
    async def test_critical_alert_escalates(self):
        esc = []

        async def _escalate(level, message):
            esc.append((level, message))

        stub = types.SimpleNamespace(TITLE="X", _escalate_to_ceo=_escalate, _latest_intel={})
        await ExecutiveAgent.receive_alert(stub, "risk", "critical", "blowup")
        assert esc and esc[0][0] == "critical"

    @pytest.mark.asyncio
    async def test_warning_alert_buffered_not_escalated(self):
        esc = []

        async def _escalate(level, message):
            esc.append((level, message))

        stub = types.SimpleNamespace(TITLE="X", _escalate_to_ceo=_escalate, _latest_intel={})
        await ExecutiveAgent.receive_alert(stub, "ops", "warning", "minor")
        assert esc == []
        assert stub._latest_intel["pending_warnings"][0]["source"] == "ops"

    @pytest.mark.asyncio
    async def test_notify_peers_publishes_when_bus_wired(self):
        published = []

        class _Bus:
            async def publish(self, event_type, title, payload):
                published.append((event_type, title, payload))

        stub = types.SimpleNamespace(TITLE="X", _event_bus=_Bus())
        await ExecutiveAgent.notify_peers(stub, "size_bias_changed", {"new_bias": "half"})
        assert published == [("size_bias_changed", "X", {"new_bias": "half"})]

    @pytest.mark.asyncio
    async def test_notify_peers_no_bus_is_safe(self):
        stub = types.SimpleNamespace(TITLE="X", _event_bus=None)
        await ExecutiveAgent.notify_peers(stub, "evt", {})   # must not raise

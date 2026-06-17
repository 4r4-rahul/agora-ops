"""
agora/tests/test_live_readiness.py — the go-live readiness meter: per-pillar pass-ratio scoring,
weighted overall, and the two-part go-live gate (overall >= 80 AND every pillar >= 60). This is the
final guard before real capital flips on, so the gate math must be exact. Pure logic; the 8 pillar
scorers are stubbed to isolate the aggregation + approval rules, with one wired-agent pillar to
prove the check plumbing.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import agora.ops.live_readiness as lr
from agora.ops.live_readiness import (
    _GO_LIVE_MIN_OVERALL,
    _GO_LIVE_MIN_PILLAR,
    _PILLAR_WEIGHTS,
    LiveReadinessMeter,
)

_PILLARS = ("risk", "intelligence", "technology", "operations",
            "finance", "research", "execution", "compliance")


def _set_pillars(meter: LiveReadinessMeter, scores: dict[str, float]) -> None:
    """Stub each _score_<pillar>() to return a fixed score, isolating the aggregation logic."""
    for p in _PILLARS:
        s = scores.get(p, 100)
        setattr(meter, f"_score_{p}", (lambda sc=s: {"score": sc, "status": "x", "checks": {}}))


# ── weight sanity ─────────────────────────────────────────────────────────────
def test_pillar_weights_sum_to_one():
    assert abs(sum(_PILLAR_WEIGHTS.values()) - 1.0) < 1e-9
    assert set(_PILLAR_WEIGHTS) == set(_PILLARS)


# ── _pillar_result (pure pass-ratio scorer) ───────────────────────────────────
class TestPillarResult:
    def test_empty_is_no_data(self):
        r = LiveReadinessMeter()._pillar_result("x", [])
        assert r == {"score": 0, "checks": {}, "status": "no_data"}

    def test_all_pass_is_excellent(self):
        r = LiveReadinessMeter()._pillar_result("x", [("a", True), ("b", True)])
        assert r["score"] == 100 and r["status"] == "excellent"
        assert r["passed"] == 2 and r["total"] == 2

    @pytest.mark.parametrize("passed,total,score,status", [
        (9, 10, 90, "excellent"),
        (8, 10, 80, "good"),
        (6, 10, 60, "partial"),
        (5, 10, 50, "degraded"),
        (3, 10, 30, "critical"),
        (0, 4, 0, "critical"),
    ])
    def test_score_bands(self, passed, total, score, status):
        checks = [("c", True)] * passed + [("c", False)] * (total - passed)
        r = LiveReadinessMeter()._pillar_result("x", checks)
        assert r["score"] == score and r["status"] == status

    def test_checks_dict_preserved(self):
        r = LiveReadinessMeter()._pillar_result("x", [("alpha", True), ("beta", False)])
        assert r["checks"] == {"alpha": True, "beta": False}


# ── get_score aggregation ─────────────────────────────────────────────────────
class TestGetScore:
    def test_all_perfect_is_ready(self):
        m = LiveReadinessMeter()
        _set_pillars(m, {})   # all 100
        s = m.get_score()
        assert s["overall_score"] == 100.0
        assert s["ready_for_live"] is True
        assert s["critical_failures"] == []

    def test_all_50_not_ready_all_critical(self):
        m = LiveReadinessMeter()
        _set_pillars(m, {p: 50 for p in _PILLARS})
        s = m.get_score()
        assert s["overall_score"] == 50.0
        assert s["ready_for_live"] is False
        assert set(s["critical_failures"]) == set(_PILLARS)

    def test_one_weak_pillar_blocks_despite_high_overall(self):
        # risk=50 (weight 0.20), rest 100 → overall 90 (>=80) but risk<60 → NOT ready.
        m = LiveReadinessMeter()
        _set_pillars(m, {"risk": 50})
        s = m.get_score()
        assert s["overall_score"] == round(100 - _PILLAR_WEIGHTS["risk"] * 50, 1)
        assert s["overall_score"] >= _GO_LIVE_MIN_OVERALL
        assert "risk" in s["critical_failures"]
        assert s["ready_for_live"] is False   # per-pillar floor overrides high overall

    def test_weighting_is_applied(self):
        # compliance has the smallest weight (0.05); tanking only it barely moves overall.
        m = LiveReadinessMeter()
        _set_pillars(m, {"compliance": 0})
        s = m.get_score()
        assert s["overall_score"] == round(100 - _PILLAR_WEIGHTS["compliance"] * 100, 1)


# ── approve / revoke go-live ──────────────────────────────────────────────────
class TestGoLiveControl:
    def test_approve_when_all_green(self):
        m = LiveReadinessMeter()
        _set_pillars(m, {})
        res = m.approve_go_live(approved_by="CEO")
        assert res["approved"] is True
        assert m.is_live is True
        assert m.fireworks_active is True
        assert res["approved_by"] == "CEO"

    def test_reject_when_overall_below_threshold(self):
        m = LiveReadinessMeter()
        _set_pillars(m, {p: 70 for p in _PILLARS})   # overall 70 < 80, no pillar <60
        res = m.approve_go_live()
        assert res["approved"] is False
        assert "Overall score" in res["reason"]
        assert m.is_live is False

    def test_reject_when_pillar_below_minimum(self):
        m = LiveReadinessMeter()
        _set_pillars(m, {"risk": 50})   # overall 90 >= 80 but risk < 60
        res = m.approve_go_live()
        assert res["approved"] is False
        assert "Pillars below minimum" in res["reason"]
        assert m.is_live is False

    def test_revoke_clears_live_and_fireworks(self):
        m = LiveReadinessMeter()
        _set_pillars(m, {})
        m.approve_go_live()
        assert m.is_live
        m.revoke_go_live(revoked_by="ops")
        assert m.is_live is False
        assert m.fireworks_active is False

    def test_thresholds_are_the_documented_values(self):
        assert _GO_LIVE_MIN_OVERALL == 80.0
        assert _GO_LIVE_MIN_PILLAR == 60.0


# ── one pillar through the real check-plumbing (risk) ─────────────────────────
class TestRiskPillarWiring:
    def test_risk_checks_from_wired_agents(self):
        m = LiveReadinessMeter()
        risk = SimpleNamespace(
            get_kill_switch_state=lambda: {"active": False},
            _settings=SimpleNamespace(daily_loss_limit_dollars=2000,
                                      weekly_loss_limit_dollars=5000),
        )
        cb = SimpleNamespace(get_state=lambda: {"kill_switch_active": False})
        m.register_agents(risk_council=risk, circuit_breaker=cb)
        r = m._score_risk()
        # all 5 checks pass → excellent
        assert r["score"] == 100 and r["status"] == "excellent"
        assert r["checks"]["kill_switch_armed"] is True
        assert r["checks"]["daily_loss_limit_set"] is True

    def test_no_agents_is_no_data(self):
        # nothing registered → no checks → no_data (score 0), which is a critical failure
        assert LiveReadinessMeter()._score_risk() == {"score": 0, "checks": {}, "status": "no_data"}

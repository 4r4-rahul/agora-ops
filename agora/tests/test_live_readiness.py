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


# ── Performance-fitness gates (the 2026-06-23 rebuild: measure fitness, not uptime) ──
class TestPerformanceFitnessGates:
    """The meter used to read ~100/100 on uptime while the book lost money. These gates
    make Finance and Execution fail when win rate / P&L / exposure / fill rate are unfit —
    so the score tells the truth about live-readiness. All gates are sample-gated."""

    def _perf(self, win_rate, net_pnl, n=50):
        return SimpleNamespace(get_latest_snapshot=lambda: {
            "status": "ok", "total_trades": n, "win_rate": win_rate,
            "by_pillar": [{"pillar": "directional", "trades": n,
                           "win_rate": win_rate, "total_pnl": net_pnl}]})

    def _pm(self, total_max_loss):
        return SimpleNamespace(get_open_positions=lambda: [
            SimpleNamespace(max_loss_dollars=total_max_loss)])

    def test_finance_fails_on_poor_performance(self):
        m = LiveReadinessMeter()
        m.register_agents(agent_performance=self._perf(22.0, -5000.0),
                          position_mgr=self._pm(10**9),   # exposure ≫ any account → fail
                          risk_council=SimpleNamespace())
        r = m._score_finance()
        assert r["checks"]["win_rate_fit"] is False
        assert r["checks"]["net_pnl_positive"] is False
        assert r["checks"]["exposure_within_account"] is False
        assert r["score"] < _GO_LIVE_MIN_PILLAR    # drags the pillar below the go-live floor

    def test_finance_passes_on_healthy_performance(self):
        m = LiveReadinessMeter()
        m.register_agents(agent_performance=self._perf(55.0, 3000.0),
                          position_mgr=self._pm(0.0), risk_council=SimpleNamespace())
        r = m._score_finance()
        assert r["checks"]["win_rate_fit"] is True
        assert r["checks"]["net_pnl_positive"] is True
        assert r["checks"]["exposure_within_account"] is True

    def test_thin_sample_does_not_gate_performance(self):
        m = LiveReadinessMeter()
        m.register_agents(agent_performance=self._perf(10.0, -100.0, n=5),  # n<20
                          position_mgr=self._pm(0.0))
        r = m._score_finance()
        assert "win_rate_fit" not in r["checks"]
        assert "net_pnl_positive" not in r["checks"]

    def _eq(self, total7, fills7):
        return SimpleNamespace(
            get_today_db_stats=lambda: {"total": 0, "fills": 0, "timeouts": 0,
                                        "fill_rate": None, "timeout_rate": None},
            get_7day_stats=lambda: {"total": total7, "fills": fills7,
                                    "fill_rate": (fills7 / total7 if total7 else 0.0)},
            get_session_stats=lambda: {"error_201_storm": False})

    def test_execution_fails_on_low_fill_rate(self):
        m = LiveReadinessMeter()
        m.register_agents(exec_quality=self._eq(500, 10))   # 2% fill, real volume
        assert m._score_execution()["checks"]["fill_rate_fit"] is False

    def test_execution_passes_on_healthy_fill_rate(self):
        m = LiveReadinessMeter()
        m.register_agents(exec_quality=self._eq(200, 140))  # 70% fill
        assert m._score_execution()["checks"]["fill_rate_fit"] is True

    def test_execution_thin_volume_does_not_gate(self):
        m = LiveReadinessMeter()
        m.register_agents(exec_quality=self._eq(5, 0))      # <20 attempts
        assert "fill_rate_fit" not in m._score_execution()["checks"]


# ── Headline honesty: a confirmed fitness failure caps the overall (no false green) ──
def test_fitness_failure_caps_headline_below_ready():
    from agora.ops.live_readiness import _GO_LIVE_MIN_OVERALL
    m = LiveReadinessMeter()
    _set_pillars(m, {})                        # all infra pillars green (100)
    # finance scores high on uptime but a real-sample win-rate gate has FAILED
    m._score_finance = lambda: {"score": 90, "status": "good",
                                "checks": {"performance_monitor": True, "win_rate_fit": False}}
    s = m.get_score()
    assert s["fitness_failed"] is True
    assert s["overall_score"] <= _GO_LIVE_MIN_OVERALL - 1.0   # headline can't read "ready"
    assert s["ready_for_live"] is False


def test_no_fitness_failure_keeps_weighted_average():
    m = LiveReadinessMeter()
    _set_pillars(m, {})                        # all 100, no fitness checks present (cold start)
    s = m.get_score()
    assert s["fitness_failed"] is False
    assert s["overall_score"] == 100.0         # cold-start dip never caps falsely


# ── Research fitness: tie the score to the learning loop's real maturity, not just uptime ──
class TestResearchFitness:
    def _meter_with_research_agents(self):
        m = LiveReadinessMeter()
        m.register_agents(event_engine=SimpleNamespace(), earnings_cal=SimpleNamespace(),
                          earnings_transcript=SimpleNamespace(), analyst_rev=SimpleNamespace())
        return m

    def test_data_starved_is_not_excellent(self):
        m = self._meter_with_research_agents()
        m._ml_fleet_readiness = lambda: {"dataset": "EMERGING", "predictive_validated": False}
        r = m._score_research()
        assert r["checks"]["training_set_trainable"] is False
        assert r["checks"]["predictive_models_validated"] is False
        assert r["score"] < 100   # no longer a false "excellent"

    def test_trainable_fleet_passes(self):
        m = self._meter_with_research_agents()
        m._ml_fleet_readiness = lambda: {"dataset": "TRAINABLE", "predictive_validated": True}
        r = m._score_research()
        assert r["checks"]["training_set_trainable"] is True
        assert r["checks"]["predictive_models_validated"] is True

    def test_no_fleet_runs_skips_fitness_no_false_penalty(self):
        m = self._meter_with_research_agents()
        m._ml_fleet_readiness = lambda: None      # cold start — fleet hasn't run
        r = m._score_research()
        assert "training_set_trainable" not in r["checks"]
        assert "predictive_models_validated" not in r["checks"]
        assert r["score"] == 100   # presence-only, not falsely penalized

"""
agora/tests/test_execution_advisor.py — the shadow slippage-budget controller.

Pins the deterministic control law: widen on timeout-dominated low fills, hold on
reject-dominated low fills (slippage can't fix rejects), tighten when overpaying to
cross, hold in-band and on thin samples, clamp to [MIN,MAX], and mark combo
recommendations untrusted in paper mode (paper-sim can't fill spreads). Pure logic,
no I/O. The advisor never writes config — `applied` is always False.
"""
from __future__ import annotations

from agora.ops.execution_advisor import (
    MAX_BUDGET,
    MIN_BUDGET,
    MIN_SAMPLE,
    STEP,
    ExecutionAdvisor,
    is_combo,
)


def _rec(stats, current=0.20, mode="live"):
    return ExecutionAdvisor().recommend(stats, current_budget=current, trading_mode=mode)


def _strat(rec, name):
    return next(r for r in rec["per_strategy"] if r["strategy"] == name)


class TestControlLaw:
    def test_low_fill_timeout_dominated_widens(self):
        rec = _rec({"bull_put_spread": {"fills": 2, "timeouts": 50, "rejects": 1}})
        r = _strat(rec, "bull_put_spread")
        assert r["action"] == "widen"
        assert r["recommended"] == round(0.20 + STEP, 4)

    def test_low_fill_reject_dominated_holds(self):
        # Rejects dominate failures → slippage won't help → hold + flag.
        rec = _rec({"long_call": {"fills": 2, "timeouts": 1, "rejects": 40}})
        r = _strat(rec, "long_call")
        assert r["action"] == "hold"
        assert "reject-dominated" in r["reason"]

    def test_high_fill_tightens_to_recover_edge(self):
        rec = _rec({"long_put": {"fills": 18, "timeouts": 1, "rejects": 1}})
        r = _strat(rec, "long_put")
        assert r["action"] == "tighten"
        assert r["recommended"] == round(0.20 - STEP, 4)

    def test_in_band_holds(self):
        rec = _rec({"long_call": {"fills": 12, "timeouts": 8, "rejects": 0}})  # 60% fill
        assert _strat(rec, "long_call")["action"] == "hold"

    def test_thin_sample_holds(self):
        rec = _rec({"iron_condor": {"fills": 0, "timeouts": MIN_SAMPLE - 6, "rejects": 0}}, mode="live")
        r = _strat(rec, "iron_condor")
        assert r["action"] == "hold"
        assert "insufficient sample" in r["reason"]

    def test_widen_clamps_at_max(self):
        rec = _rec({"bull_put_spread": {"fills": 0, "timeouts": 99, "rejects": 0}},
                   current=MAX_BUDGET, mode="live")
        assert _strat(rec, "bull_put_spread")["recommended"] == MAX_BUDGET

    def test_tighten_clamps_at_min(self):
        rec = _rec({"long_call": {"fills": 99, "timeouts": 0, "rejects": 0}}, current=MIN_BUDGET)
        assert _strat(rec, "long_call")["recommended"] == MIN_BUDGET

    def test_policy_rejects_excluded_from_denominator(self):
        # 201 policy rejects are not failures — fill rate is fills/(fills+timeouts+rejects).
        rec = _rec({"bull_put_spread": {"fills": 9, "timeouts": 1, "rejects": 0,
                                        "policy_rejects": 500}})
        r = _strat(rec, "bull_put_spread")
        assert r["fill_rate"] == 0.9 and r["action"] == "tighten"


class TestPaperUntrusted:
    def test_combo_in_paper_is_untrusted(self):
        rec = _rec({"bull_put_spread": {"fills": 1, "timeouts": 99, "rejects": 0}}, mode="paper")
        r = _strat(rec, "bull_put_spread")
        assert r["action"] == "widen"          # law still computes
        assert r["trusted"] is False           # but flagged: paper-sim can't fill spreads
        assert "untrusted" in rec["note"]

    def test_single_leg_in_paper_is_trusted(self):
        rec = _rec({"long_call": {"fills": 1, "timeouts": 99, "rejects": 0}}, mode="paper")
        assert _strat(rec, "long_call")["trusted"] is True

    def test_portfolio_ignores_untrusted_widen(self):
        # Only an untrusted combo wants widening → portfolio holds (won't act on it).
        rec = _rec({"bull_put_spread": {"fills": 1, "timeouts": 99, "rejects": 0}}, mode="paper")
        assert rec["portfolio"]["action"] == "hold"

    def test_is_combo_classification(self):
        assert is_combo("bull_put_spread") and is_combo("iron_condor")
        assert not is_combo("long_call") and not is_combo("long_put")


def test_shadow_never_applies():
    rec = _rec({"bull_put_spread": {"fills": 1, "timeouts": 99, "rejects": 0}})
    assert rec["applied"] is False and rec["mode"] == "shadow"


# ── Promotion: apply_slippage_recommendation writes the budget, trusted+gated ──
import sqlite3
from datetime import date

import pytest

from agora.core.config import get_settings
from agora.ops.execution_quality import ExecutionQualityAgent


def _eq_agent(tmp_path, mode, autoapply, budget=0.20):
    s = get_settings().model_copy(update={
        "db_path": tmp_path / "t.db",
        "trading_mode": mode,
        "max_slippage_pct_of_width": budget,
        "exec_advisor_autoapply": autoapply,
    })
    return ExecutionQualityAgent(settings=s), s


def _seed(agent, strategy, fills, timeouts):
    today = date.today().isoformat()
    for _ in range(fills):
        agent._db.execute("INSERT INTO execution_quality (attempt_date,ticker,strategy,mid_price,outcome) "
                          "VALUES (?,?,?,1.0,'fill')", (today, "X", strategy))
    for _ in range(timeouts):
        agent._db.execute("INSERT INTO execution_quality (attempt_date,ticker,strategy,mid_price,outcome) "
                          "VALUES (?,?,?,1.0,'timeout')", (today, "X", strategy))
    agent._db.commit()


class TestApplyPromotion:
    def test_applies_trusted_widen(self, tmp_path):
        # LIVE + combo, low fill timeout-dominated → trusted widen → budget bumps 0.20→0.25.
        agent, s = _eq_agent(tmp_path, mode="live", autoapply=True)
        _seed(agent, "bull_put_spread", fills=2, timeouts=50)
        rec = agent.apply_slippage_recommendation()
        assert rec["portfolio"]["action"] == "widen"
        assert "applied" in rec["apply_status"]
        assert s.max_slippage_pct_of_width == pytest.approx(0.25)

    def test_paper_combo_holds_no_change(self, tmp_path):
        # PAPER + combo → untrusted → portfolio holds → budget untouched (no chasing the sim).
        agent, s = _eq_agent(tmp_path, mode="paper", autoapply=True)
        _seed(agent, "bull_put_spread", fills=2, timeouts=50)
        rec = agent.apply_slippage_recommendation()
        assert rec["portfolio"]["action"] == "hold"
        assert s.max_slippage_pct_of_width == pytest.approx(0.20)

    def test_disabled_flag_is_shadow(self, tmp_path):
        agent, s = _eq_agent(tmp_path, mode="live", autoapply=False)
        _seed(agent, "bull_put_spread", fills=2, timeouts=50)
        rec = agent.apply_slippage_recommendation()
        assert "shadow" in rec["apply_status"]
        assert s.max_slippage_pct_of_width == pytest.approx(0.20)   # never written in shadow

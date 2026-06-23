"""
agora/tests/test_ibkr_execution_advisor.py — the execution-param advisor merged into
IBKRKnowledgeAgent.evaluate_execution_params: the paper-untrusted guard + trusted-gated
auto-apply (the consolidation that retired the standalone ExecutionAdvisor).

Contract: a paper-mode widen is advisory only (paper-sim can't fill spreads, so a low paper
fill rate is an artifact) and is never auto-applied; a live widen / any tighten is trusted and
applied when exec_advisor_autoapply is on. Pure over a temp DB + stub stats.
"""
from __future__ import annotations

import pytest

from agora.core.config import get_settings
from agora.ops.ibkr_knowledge_agent import IBKRKnowledgeAgent


class _StubEQ:
    def __init__(self, fill_rate, avg_slippage=0.0, total=100):
        self._today = {"total": total, "fills": int(fill_rate * total),
                       "fill_rate": fill_rate, "timeout_rate": 0.9}
        self._week = {"total": total, "fill_rate": fill_rate,
                      "avg_slippage": avg_slippage, "reject_reasons": {}}

    def get_today_db_stats(self): return self._today
    def get_7day_stats(self): return self._week
    def get_session_stats(self): return {"error_201_storm": False}


def _agent(tmp_path, mode, autoapply, budget=0.20, fill=0.02, avg_slip=0.0):
    s = get_settings().model_copy(update={
        "db_path": tmp_path / "t.db", "trading_mode": mode,
        "max_slippage_pct_of_width": budget, "exec_advisor_autoapply": autoapply,
        "use_adaptive_algo": False, "ibkr_market_data_type": 1,
    })
    return IBKRKnowledgeAgent(settings=s, exec_quality=_StubEQ(fill, avg_slippage=avg_slip)), s


def _slip_rec(res):
    return next((r for r in res["recommendations"] if r["param"] == "max_slippage_pct_of_width"), None)


def test_paper_widen_is_untrusted_and_not_applied(tmp_path):
    a, s = _agent(tmp_path, mode="paper", autoapply=True, fill=0.02)
    res = a.evaluate_execution_params()
    rec = _slip_rec(res)
    assert rec is not None and rec["trusted"] is False        # paper-sim artifact
    assert res["applied"] is None and res["shadow_mode"] is True
    assert s.max_slippage_pct_of_width == pytest.approx(0.20)   # untouched


def test_live_widen_is_trusted_and_applied(tmp_path):
    a, s = _agent(tmp_path, mode="live", autoapply=True, budget=0.10, fill=0.02)
    res = a.evaluate_execution_params()
    rec = _slip_rec(res)
    assert rec["trusted"] is True
    assert res["applied"] == {"from": 0.10, "to": 0.15}
    assert s.max_slippage_pct_of_width == pytest.approx(0.15)


def test_autoapply_off_is_shadow(tmp_path):
    a, s = _agent(tmp_path, mode="live", autoapply=False, budget=0.10, fill=0.02)
    res = a.evaluate_execution_params()
    assert res["applied"] is None and res["shadow_mode"] is True
    assert s.max_slippage_pct_of_width == pytest.approx(0.10)


def test_tighten_is_trusted_and_applied_live(tmp_path):
    # Healthy fills (>=85%) + costly slippage → tighten; trusted regardless, applied when on.
    a, s = _agent(tmp_path, mode="live", autoapply=True, budget=0.20, fill=0.90, avg_slip=-0.30)
    res = a.evaluate_execution_params()
    rec = _slip_rec(res)
    assert rec is not None and rec["trusted"] is True
    assert res["applied"] == {"from": 0.20, "to": 0.18}
    assert s.max_slippage_pct_of_width == pytest.approx(0.18)

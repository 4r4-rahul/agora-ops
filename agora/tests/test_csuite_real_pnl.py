"""
agora/tests/test_csuite_real_pnl.py — REGRESSION GUARD: every C-suite agent's daily realized-P&L
read MUST use the canonical _REAL_CLOSE predicate.

On 2026-06-26 the CFO/CRO/CEO/COO computed today's realized P&L with a naïve
`WHERE close_date=date('now')` (no _REAL_CLOSE), so an adopted/fiction close corrupted their view:
they saw TODAY = −$14,964.89 when the real number was +$2,171 — a $17,136 fiction that drove
daily-loss assessments, the CRO's halt/alert logic, and executive reporting. This guard fails if
any of those daily-realized reads ever again omits _REAL_CLOSE.
"""
import inspect

import pytest


def _src(modpath, objname):
    mod = __import__(modpath, fromlist=[objname])
    return inspect.getsource(getattr(mod, objname))


@pytest.mark.parametrize("modpath,cls,method", [
    ("agora.c_suite.cfo", "CFOAgent", "self_audit"),
    ("agora.c_suite.cro", "CROAgent", "self_audit"),
    ("agora.c_suite.coo", "COOAgent", "collect_intelligence"),
])
def test_csuite_daily_realized_uses_real_close(modpath, cls, method):
    mod = __import__(modpath, fromlist=[cls])
    src = inspect.getsource(getattr(getattr(mod, cls), method))
    # any query that sums realized_pnl for "today" must be gated on _REAL_CLOSE
    if "close_date" in src and "realized_pnl" in src:
        assert "_REAL_CLOSE" in src, (
            f"{modpath}.{cls}.{method} reads today's realized_pnl without _REAL_CLOSE — "
            f"fiction (adopted/sync) would corrupt the daily-loss/intel number"
        )


def test_ceo_realized_today_uses_real_close():
    from agora.agents import ceo_agent
    src = inspect.getsource(ceo_agent)
    # the today-realized query that feeds realized_pnl_today must be gated on _REAL_CLOSE
    assert "{_REAL_CLOSE} AND close_date=date('now')" in src, \
        "CEO realized_pnl_today must read REAL fills only (_REAL_CLOSE)"

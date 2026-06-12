"""Unit tests for the proactive-reactivity build (Phases 1-3).

Each test binds the REAL method to a minimal stub `self`, so it exercises the actual
production logic (promotion path, exit sweep, shock guards) without standing up a full
session or the broker. Live confirmation at market open is separate.
"""
import types
from datetime import date, timedelta

import pytest

from agora.session import AgoraSession
from agora.lifecycle.position_manager import PositionManager


# ── Phase 3 — proactive catalyst calendar ────────────────────────────────────
@pytest.mark.asyncio
async def test_check_scheduled_catalysts_promotes_only_within_window(tmp_path):
    today = date.today()
    cals = [
        {"name": "NearIPO",   "date": today.isoformat(),                    "peers": ["AAA", "BBB"], "lead_days": 3},
        {"name": "FarEvent",  "date": (today + timedelta(days=30)).isoformat(), "peers": ["CCC"],   "lead_days": 3},
        {"name": "PastEvent", "date": (today - timedelta(days=5)).isoformat(),  "peers": ["DDD"],   "lead_days": 3},
        {"name": "BadDate",   "date": "not-a-date",                          "peers": ["EEE"],       "lead_days": 3},
    ]
    stub = types.SimpleNamespace(
        _settings=types.SimpleNamespace(scheduled_catalysts=cals, db_path=str(tmp_path / "agora.db")),
        _prestaged_catalysts=set(),
        _scan_engine=None,            # → fallback _priority_queue path
        _priority_queue=[],
        _priority_reasons={},
        _long_options_agent=None,
        _long_event_tickers={},
    )
    stub._promote_priority = AgoraSession._promote_priority.__get__(stub)

    await AgoraSession._check_scheduled_catalysts(stub)

    # Only the in-window event's peers are promoted.
    assert set(stub._priority_queue) == {"AAA", "BBB"}
    for excluded in ("CCC", "DDD", "EEE"):
        assert excluded not in stub._priority_queue

    # Dedup: a second call the same day promotes nothing new.
    await AgoraSession._check_scheduled_catalysts(stub)
    assert stub._priority_queue.count("AAA") == 1


@pytest.mark.asyncio
async def test_check_scheduled_catalysts_noop_when_empty(tmp_path):
    stub = types.SimpleNamespace(
        _settings=types.SimpleNamespace(scheduled_catalysts=[], db_path=str(tmp_path / "agora.db")),
        _prestaged_catalysts=set(), _scan_engine=None, _priority_queue=[],
        _priority_reasons={}, _long_options_agent=None, _long_event_tickers={},
    )
    stub._promote_priority = AgoraSession._promote_priority.__get__(stub)
    await AgoraSession._check_scheduled_catalysts(stub)
    assert stub._priority_queue == []


# ── Phase 1 — shock position protection ──────────────────────────────────────
@pytest.mark.asyncio
async def test_review_on_shock_remarks_all_and_reports_closed():
    state = {"positions": [types.SimpleNamespace(ticker="X"), types.SimpleNamespace(ticker="Y")],
             "marked": []}

    async def _refresh(p):
        state["marked"].append(p.ticker)

    async def _check(p):                      # simulate the shock closing X (stop hit)
        if p.ticker == "X":
            state["positions"] = [q for q in state["positions"] if q.ticker != "X"]

    stub = types.SimpleNamespace(
        _is_market_hours=lambda now: True,
        get_open_positions=lambda: list(state["positions"]),
        _refresh_position_price=_refresh,
        _check_position_targets=_check,
    )

    rv = await PositionManager.review_on_shock(stub, reason="SPY move 1.6%")

    assert rv["reviewed"] == 2
    assert rv["closed"] == 1                    # X was protected/closed
    assert set(state["marked"]) == {"X", "Y"}   # every position re-marked first


@pytest.mark.asyncio
async def test_review_on_shock_skips_after_hours():
    stub = types.SimpleNamespace(
        _is_market_hours=lambda now: False,
        get_open_positions=lambda: [types.SimpleNamespace(ticker="X")],
        _refresh_position_price=None, _check_position_targets=None,
    )
    rv = await PositionManager.review_on_shock(stub, reason="after-hours")
    assert rv["reviewed"] == 0 and rv.get("skipped") == "after_hours"


# ── Phase 2 — shock opportunity scan ─────────────────────────────────────────
def _shock_stub(evaluated):
    async def _eval(ticker, scan_priority=None, scan_reason=None):
        evaluated.append(ticker)
    return types.SimpleNamespace(
        _last_shock_scan_ts=0.0,
        _SHOCK_SCAN_DEBOUNCE_SECS=AgoraSession._SHOCK_SCAN_DEBOUNCE_SECS,
        _SHOCK_SCAN_MAX_NAMES=AgoraSession._SHOCK_SCAN_MAX_NAMES,
        _SHOCK_SCAN_MAX_MOVE_PCT=AgoraSession._SHOCK_SCAN_MAX_MOVE_PCT,
        _settings=types.SimpleNamespace(etf_universe=["SPY", "QQQ", "IWM", "DIA", "XLF"]),
        _long_event_tickers={"NVDA": "x"},
        _priority_queue=["AMD"],
        _evaluate_ticker=_eval,
    )


@pytest.mark.asyncio
async def test_shock_scan_chasing_top_guard_skips_extended_move():
    evaluated = []
    stub = _shock_stub(evaluated)
    await AgoraSession._shock_opportunity_scan(stub, "big crash", 0.06)  # ≥5% → skip
    assert evaluated == []


@pytest.mark.asyncio
async def test_shock_scan_evaluates_affected_and_caps():
    evaluated = []
    stub = _shock_stub(evaluated)
    await AgoraSession._shock_opportunity_scan(stub, "SPY move 1.6%", 0.016)
    assert "SPY" in evaluated and "NVDA" in evaluated      # ETF + event-promoted peer
    assert len(evaluated) <= stub._SHOCK_SCAN_MAX_NAMES     # capped


@pytest.mark.asyncio
async def test_shock_scan_debounces_repeat():
    evaluated = []
    stub = _shock_stub(evaluated)
    await AgoraSession._shock_opportunity_scan(stub, "first", 0.016)
    n = len(evaluated)
    assert n > 0
    await AgoraSession._shock_opportunity_scan(stub, "immediate repeat", 0.016)
    assert len(evaluated) == n                              # debounced — no new evals

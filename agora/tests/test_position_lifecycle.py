"""Unit tests for agora/lifecycle/position_manager.py — the position lifecycle /
exit logic of the single exit owner (PositionManager).

Pattern (mirrors test_reactivity_phases.py): bind the REAL method to a minimal
SimpleNamespace stub carrying only the attributes/methods the method touches, and
record side-effect calls via closures. No IBKR, no network, no DB writes (the close
path is stubbed; the one DB read in _close_position is short-circuited by routing
through a recorded _close_position stub instead). This exercises the production
control-flow without standing up a session or broker.

Covered behaviors:
  - review_on_shock: a hit stop closes the position; multiple positions all reviewed;
    an exception in one position does NOT abort the rest.
  - _check_position_targets: long options route to the long path; a spread at/below
    target_dte_close force-closes; a spread with ample DTE does NOT 21-DTE-close.
  - The 21-DTE rule EXCLUDES long_call / long_put (a fresh long is never force-closed
    by the spread DTE rule).

These complement (do NOT duplicate) the two review_on_shock tests already in
test_reactivity_phases.py.
"""
import types
from datetime import date, timedelta

import pytest

from agora.core.config import get_settings
from agora.core.models import (
    OpenPosition,
    PositionStatus,
    SpreadLeg,
    StrategyPillar,
    StrategyType,
)
from agora.lifecycle.position_manager import PositionManager


# ── Position builders ─────────────────────────────────────────────────────────

_SETTINGS = get_settings()


def _spread_position(
    pid: str = "spread-1",
    dte: int = 45,
    strategy: StrategyType = StrategyType.BULL_CALL_SPREAD,
    unrealized_pnl: float = 0.0,
    entry_price: float = 3.00,
    contracts: int = 1,
) -> OpenPosition:
    today = date.today()
    expiry = today + timedelta(days=dte)
    return OpenPosition(
        position_id=pid,
        ticker="TEST",
        strategy=strategy,
        pillar=StrategyPillar.DIRECTIONAL,
        status=PositionStatus.OPEN,
        legs=[
            SpreadLeg(option_type="call", strike=125.0, expiration=expiry,
                      action="buy", contracts=1, delta=0.40),
            SpreadLeg(option_type="call", strike=135.0, expiration=expiry,
                      action="sell", contracts=1, delta=0.22),
        ],
        contracts=contracts,
        entry_price=entry_price,
        current_price=entry_price,
        entry_date=today,
        expiry_date=expiry,
        target_close_date=today + timedelta(days=max(dte - 21, 0)),
        max_loss_dollars=300.0,
        max_gain_dollars=700.0,
        unrealized_pnl=unrealized_pnl,
    )


def _long_position(
    pid: str = "long-1",
    strategy: StrategyType = StrategyType.LONG_CALL,
    dte: int = 30,
) -> OpenPosition:
    today = date.today()
    expiry = today + timedelta(days=dte)
    opt = "call" if strategy == StrategyType.LONG_CALL else "put"
    return OpenPosition(
        position_id=pid,
        ticker="LONGX",
        strategy=strategy,
        pillar=StrategyPillar.DIRECTIONAL,
        status=PositionStatus.OPEN,
        legs=[SpreadLeg(option_type=opt, strike=100.0, expiration=expiry,
                        action="buy", contracts=1, delta=0.50)],
        contracts=1,
        entry_price=2.50,
        current_price=2.50,
        entry_date=today,
        expiry_date=expiry,
        target_close_date=today + timedelta(days=dte),
        max_loss_dollars=250.0,
        max_gain_dollars=1000.0,
        unrealized_pnl=0.0,
    )


def _targets_stub(closed: list, long_routed: list):
    """Stub `self` for _check_position_targets. The profit engine is a no-op that
    returns a HOLD-like decision; _close_position and _check_long_options_targets are
    recorders. The real-fill DB read in _close_position is never reached because we
    record at the _close_position boundary."""
    async def _close(position, reason, source="lifecycle"):
        closed.append((position.position_id, reason, source))

    async def _check_long(position):
        long_routed.append(position.position_id)

    async def _maybe_llm_exit(position, is_long):
        return False

    async def _check_tested(position):
        return None

    async def _attempt_roll(position):
        return False

    # Profit engine: HOLD, no close, profit_pct 0 so the debug branch is skipped.
    decision = types.SimpleNamespace(
        should_close=False, profit_pct=0.0, reason="hold", rule="HOLD",
        hwm_pct=0.0, effective_target=0.5, ratchet_stop_pct=0.0,
        velocity_1h=0.0, theta_excess=0.0,
    )
    profit_engine = types.SimpleNamespace(
        evaluate=lambda **kw: decision,
        clear_position=lambda pid: None,
    )

    return types.SimpleNamespace(
        _settings=_SETTINGS,
        _profit_engine=profit_engine,
        _close_position=_close,
        _check_long_options_targets=_check_long,
        _maybe_llm_exit=_maybe_llm_exit,
        _check_tested_status=_check_tested,
        _attempt_roll=_attempt_roll,
        get_realized_pnl_today=lambda: 0.0,
    )


# ── review_on_shock — deeper than the two existing tests ──────────────────────

@pytest.mark.asyncio
async def test_review_on_shock_closes_position_that_hits_stop():
    """A position whose target-check flattens it (stop hit) is counted as closed and is
    gone from the open set afterward — proving the re-mark → check → close path runs."""
    p = _spread_position(pid="STOP")
    state = {"open": [p], "marked": []}

    async def _refresh(pos):
        state["marked"].append(pos.position_id)

    async def _check(pos):  # the shock check flattens STOP
        state["open"] = [q for q in state["open"] if q.position_id != pos.position_id]

    stub = types.SimpleNamespace(
        _is_market_hours=lambda now: True,
        get_open_positions=lambda: list(state["open"]),
        _refresh_position_price=_refresh,
        _check_position_targets=_check,
    )

    rv = await PositionManager.review_on_shock(stub, reason="VIX spike")
    assert rv == {"reviewed": 1, "closed": 1}
    assert state["marked"] == ["STOP"]
    assert state["open"] == []


@pytest.mark.asyncio
async def test_review_on_shock_multiple_positions_partial_close():
    """Three positions: only one closes; all three are re-marked first; counts are exact."""
    ids = ["A", "B", "C"]
    state = {"open": [types.SimpleNamespace(ticker=i, position_id=i) for i in ids],
             "marked": []}

    async def _refresh(pos):
        state["marked"].append(pos.position_id)

    async def _check(pos):
        if pos.position_id == "B":
            state["open"] = [q for q in state["open"] if q.position_id != "B"]

    stub = types.SimpleNamespace(
        _is_market_hours=lambda now: True,
        get_open_positions=lambda: list(state["open"]),
        _refresh_position_price=_refresh,
        _check_position_targets=_check,
    )

    rv = await PositionManager.review_on_shock(stub, reason="news")
    assert rv["reviewed"] == 3
    assert rv["closed"] == 1
    assert set(state["marked"]) == {"A", "B", "C"}
    assert {q.position_id for q in state["open"]} == {"A", "C"}


@pytest.mark.asyncio
async def test_review_on_shock_exception_in_one_does_not_abort_rest():
    """If _check_position_targets raises on one position, the loop swallows it and still
    checks the others — a single bad mark must not strand the rest unreviewed."""
    ids = ["A", "B", "C"]
    state = {"open": [types.SimpleNamespace(ticker=i, position_id=i) for i in ids],
             "checked": []}

    async def _refresh(pos):
        return None

    async def _check(pos):
        state["checked"].append(pos.position_id)
        if pos.position_id == "B":
            raise RuntimeError("price feed glitch")
        # A and C both close cleanly
        state["open"] = [q for q in state["open"] if q.position_id != pos.position_id]

    stub = types.SimpleNamespace(
        _is_market_hours=lambda now: True,
        get_open_positions=lambda: list(state["open"]),
        _refresh_position_price=_refresh,
        _check_position_targets=_check,
    )

    rv = await PositionManager.review_on_shock(stub, reason="glitchy shock")
    # Every position was visited despite B raising.
    assert state["checked"] == ["A", "B", "C"]
    assert rv["reviewed"] == 3
    # A and C closed, B remained open (its close never ran).
    assert rv["closed"] == 2
    assert {q.position_id for q in state["open"]} == {"B"}


@pytest.mark.asyncio
async def test_review_on_shock_no_open_positions_returns_reviewed_zero():
    """Empty book → no re-marking, returns reviewed=0 with no 'closed' key (early return)."""
    stub = types.SimpleNamespace(
        _is_market_hours=lambda now: True,
        get_open_positions=lambda: [],
        _refresh_position_price=None,
        _check_position_targets=None,
    )
    rv = await PositionManager.review_on_shock(stub, reason="quiet")
    assert rv == {"reviewed": 0}


# ── _check_position_targets — routing & the 21-DTE rule ───────────────────────

@pytest.mark.asyncio
async def test_long_call_routes_to_long_path_not_spread_logic():
    closed, long_routed = [], []
    stub = _targets_stub(closed, long_routed)
    pos = _long_position(pid="LC", strategy=StrategyType.LONG_CALL, dte=30)

    await PositionManager._check_position_targets(stub, pos)

    assert long_routed == ["LC"]      # routed to the long-options rule set
    assert closed == []               # spread close path never invoked


@pytest.mark.asyncio
async def test_long_put_routes_to_long_path():
    closed, long_routed = [], []
    stub = _targets_stub(closed, long_routed)
    pos = _long_position(pid="LP", strategy=StrategyType.LONG_PUT, dte=30)

    await PositionManager._check_position_targets(stub, pos)

    assert long_routed == ["LP"]
    assert closed == []


@pytest.mark.asyncio
async def test_fresh_long_not_force_closed_by_21dte_rule():
    """The 21-DTE force-close must EXCLUDE longs. A long with dte BELOW target_dte_close
    (which WOULD trip the spread rule) must still route to the long path and NOT be closed
    by the 21-DTE branch — that branch is unreachable for long_call/long_put."""
    closed, long_routed = [], []
    stub = _targets_stub(closed, long_routed)
    # dte well under the 21-DTE threshold — would force-close a spread immediately.
    assert _SETTINGS.target_dte_close == 21
    pos = _long_position(pid="LC-LOWDTE", strategy=StrategyType.LONG_CALL, dte=10)

    await PositionManager._check_position_targets(stub, pos)

    assert long_routed == ["LC-LOWDTE"]   # went to the long path
    assert closed == []                   # NOT 21-DTE-closed


@pytest.mark.asyncio
async def test_spread_at_target_dte_force_closes():
    """A spread whose dte <= target_dte_close triggers the 21-DTE close via _close_position."""
    closed, long_routed = [], []
    stub = _targets_stub(closed, long_routed)
    dte = _SETTINGS.target_dte_close          # exactly at the threshold → dte <= target
    pos = _spread_position(pid="DTE-HIT", dte=dte)

    await PositionManager._check_position_targets(stub, pos)

    assert long_routed == []
    assert len(closed) == 1
    pid, reason, _ = closed[0]
    assert pid == "DTE-HIT"
    assert "21-DTE" in reason
    assert f"dte={dte}" in reason


@pytest.mark.asyncio
async def test_spread_below_target_dte_force_closes():
    """One day past the threshold (dte < target) also closes — boundary is inclusive."""
    closed, long_routed = [], []
    stub = _targets_stub(closed, long_routed)
    dte = _SETTINGS.target_dte_close - 5
    pos = _spread_position(pid="DTE-PAST", dte=dte)

    await PositionManager._check_position_targets(stub, pos)

    assert [c[0] for c in closed] == ["DTE-PAST"]


@pytest.mark.asyncio
async def test_spread_with_ample_dte_not_21dte_closed():
    """A spread far from expiry is NOT force-closed by the 21-DTE rule (and the no-op
    profit engine + stubbed LLM/roll leave it open) — it falls through to _check_tested."""
    closed, long_routed = [], []
    stub = _targets_stub(closed, long_routed)
    pos = _spread_position(pid="FRESH", dte=_SETTINGS.target_dte_close + 30)

    await PositionManager._check_position_targets(stub, pos)

    assert closed == []           # neither 21-DTE nor profit/stop fired
    assert long_routed == []


@pytest.mark.asyncio
async def test_spread_hard_stop_fires_when_no_roll():
    """When unrealized P&L breaches the 2x-entry hard floor and a roll is not available,
    the spread is closed with the hard-stop reason (deterministic backstop)."""
    closed, long_routed = [], []
    stub = _targets_stub(closed, long_routed)
    # hard_stop = -(entry_price * 100 * contracts * stop_loss_multiplier)
    entry, contracts = 3.00, 1
    floor = -(entry * 100 * contracts * _SETTINGS.stop_loss_multiplier)
    pos = _spread_position(
        pid="HARDSTOP", dte=_SETTINGS.target_dte_close + 30,
        entry_price=entry, contracts=contracts,
        unrealized_pnl=floor - 50.0,     # breach the floor
    )

    await PositionManager._check_position_targets(stub, pos)

    assert len(closed) == 1
    pid, reason, _ = closed[0]
    assert pid == "HARDSTOP"
    assert "Hard stop" in reason


@pytest.mark.asyncio
async def test_spread_profit_engine_close_is_honored():
    """When the profit engine says should_close, _check_position_targets closes via the
    engine's reason and clears engine state — the engine decision is authoritative over the
    later stop/LLM/tested checks (which must NOT also run)."""
    closed, long_routed = [], []
    stub = _targets_stub(closed, long_routed)

    cleared = []
    engine_decision = types.SimpleNamespace(
        should_close=True, profit_pct=0.60, reason="LOCK_IN +60%", rule="LOCK_IN",
        hwm_pct=0.70, effective_target=0.5, ratchet_stop_pct=0.45,
        velocity_1h=0.0, theta_excess=0.0,
    )
    stub._profit_engine = types.SimpleNamespace(
        evaluate=lambda **kw: engine_decision,
        clear_position=lambda pid: cleared.append(pid),
    )
    pos = _spread_position(pid="ENGINE", dte=_SETTINGS.target_dte_close + 30)

    await PositionManager._check_position_targets(stub, pos)

    assert [c[0] for c in closed] == ["ENGINE"]
    assert closed[0][1] == "LOCK_IN +60%"
    assert cleared == ["ENGINE"]

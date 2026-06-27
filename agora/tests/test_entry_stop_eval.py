"""
GAP-2 / HARDEN-3b — _evaluate_entry_stop_once closes the post-fill unprotected window.

Safety contract (proof-checked): it MARKS once then runs the exit owner, reusing the quote_ok HOLD
guard and surveil (which HOLDs on unrealized=0.0 across all combos — see below). So it protects on a
genuine adverse mark but can NEVER erroneously close a fresh position, and is fully fail-open.

Uses @pytest.mark.asyncio (repo asyncio_mode='strict') — not asyncio.run() (it nulls the current event
loop and pollutes later tests that call get_event_loop()).
"""
import types

import pytest

from agora.ops.position_surveillance import surveil
from agora.session import AgoraSession


class _PM:
    def __init__(self, positions):
        self._pos = positions
        self.refreshed: list[str] = []
        self.checked: list[str] = []

    def get_open_positions(self):
        return self._pos

    async def _refresh_position_price(self, p):
        self.refreshed.append(p.position_id)

    async def _check_position_targets(self, p):
        self.checked.append(p.position_id)


async def _eval(pm, pid):
    stub = types.SimpleNamespace(_position_mgr=pm)
    await AgoraSession._evaluate_entry_stop_once(stub, pid)


class TestEntryStopEval:
    @pytest.mark.asyncio
    async def test_marks_then_checks_in_order(self):
        pos = types.SimpleNamespace(position_id="P1")
        pm = _PM([pos])
        await _eval(pm, "P1")
        assert pm.refreshed == ["P1"]      # marked first (reuses quote_ok guard)
        assert pm.checked == ["P1"]        # then the single exit owner

    @pytest.mark.asyncio
    async def test_noop_on_none_position_id(self):
        pm = _PM([])
        await _eval(pm, None)
        assert pm.refreshed == [] and pm.checked == []

    @pytest.mark.asyncio
    async def test_noop_when_position_not_found(self):
        pm = _PM([])
        await _eval(pm, "GHOST")
        assert pm.checked == []

    @pytest.mark.asyncio
    async def test_fail_open_on_error(self):
        class _Boom:
            def get_open_positions(self):
                raise RuntimeError("db down")
        # must NOT raise — entry must never be blocked by a stop-eval error
        await _eval(_Boom(), "P1")


class TestFreshPositionHolds:
    def test_zero_unrealized_holds_every_combo(self):
        # the safety property _evaluate_entry_stop_once relies on: a just-filled position (unrealized=0)
        # never EXITs, regardless of structure/regime/credit.
        for is_credit in (True, False):
            for regime in ("neutral", "risk_off", "high_volatility"):
                for ml, mg, prem in [(500, 300, 200), (0, 0, 0), (1600, 200, 150)]:
                    v = surveil(is_credit=is_credit, unrealized=0.0, premium=prem,
                                max_loss=ml, max_gain=mg, regime=regime)
                    assert v.action == "HOLD"

    def test_real_adverse_mark_still_exits(self):
        v = surveil(is_credit=False, unrealized=-180.0, premium=200, max_loss=200, max_gain=300)
        assert v.action == "EXIT"   # protection still fires on a genuine blowout

"""
agora/tests/test_close_idempotency_guard.py — REGRESSION GUARD for the close-stacking runaway.

On 2026-06-26 the exit path re-fired a full-size MKT close every cycle while earlier closes were
still working (IBKR paper combos fill 2-4 min late, but the wait window is ~20s). ~17 live close
orders stacked and all filled, ballooning DIA from 59 → 631 contracts and NOW → 131 — a fictitious
~-$105k over-fill the engine never intended. Root cause: the close orderRef was SESSION-scoped, so
nothing could tell one position's working close from another's, and nothing checked the broker for
an already-working close before submitting a new one.

These tests fail loudly if the position-scoped, broker-checked idempotency guard is removed or
weakened.
"""
from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

from trading_platform.services import ibkr_client


def _fake_trade(order_ref: str, status: str, order_id: int = 1):
    return SimpleNamespace(
        order=SimpleNamespace(orderRef=order_ref, orderId=order_id),
        orderStatus=SimpleNamespace(status=status),
    )


class _FakeIB:
    def __init__(self, trades):
        self._trades = trades
        self.req_called = 0

    async def reqAllOpenOrdersAsync(self):
        self.req_called += 1
        return self._trades

    def openTrades(self):
        return self._trades


class TestRefBaseIsPositionScoped:
    def test_ref_is_keyed_on_position_id_not_session(self):
        # Two different positions in the SAME session must get DIFFERENT close refs — that is the
        # whole point: the runaway happened because both shared CLOSE_<session_id>.
        sess = "AGORA-20260626-0819"
        a = ibkr_client._close_ref_base("pos-aaaaaaaa-1111", sess)
        b = ibkr_client._close_ref_base("pos-bbbbbbbb-2222", sess)
        assert a != b
        assert a.startswith("CLOSE_")

    def test_falls_back_to_session_when_no_position_id(self):
        assert ibkr_client._close_ref_base("", "SESS").startswith("CLOSE_")


class TestWorkingCloseDetection:
    def test_detects_live_close_for_same_position(self):
        ref = ibkr_client._close_ref_base("pos-123", "sess")
        ib = _FakeIB([_fake_trade(ref, "Submitted", order_id=42)])
        working = asyncio.run(ibkr_client._working_close_orders(ib, ref))
        assert len(working) == 1 and working[0].order.orderId == 42

    def test_ignores_terminal_and_other_positions(self):
        ref = ibkr_client._close_ref_base("pos-123", "sess")
        other = ibkr_client._close_ref_base("pos-999", "sess")
        ib = _FakeIB([
            _fake_trade(ref, "Filled"),          # terminal — not working
            _fake_trade(ref, "Cancelled"),       # terminal — not working
            _fake_trade(other, "Submitted"),     # different position — not ours
            _fake_trade("", "Submitted"),        # an entry / unrelated order
        ])
        assert asyncio.run(ibkr_client._working_close_orders(ib, ref)) == []

    def test_matches_leg_suffixed_refs(self):
        # close_position_legs uses "<ref_base>-L0", "<ref_base>-L1" — startswith must still match.
        ref = ibkr_client._close_ref_base("pos-123", "sess")
        ib = _FakeIB([_fake_trade(f"{ref}-L0", "PreSubmitted"),
                      _fake_trade(f"{ref}-L1", "PreSubmitted")])
        assert len(asyncio.run(ibkr_client._working_close_orders(ib, ref))) == 2

    def test_fails_open_on_query_error(self):
        # If the broker query fails we proceed (return []) rather than refuse to ever close —
        # one extra order is recoverable; a stranded position is an unbounded loss.
        class _Boom(_FakeIB):
            async def reqAllOpenOrdersAsync(self):
                raise RuntimeError("connection reset")

        ref = ibkr_client._close_ref_base("pos-123", "sess")
        assert asyncio.run(ibkr_client._working_close_orders(_Boom([]), ref)) == []


class TestGuardWiredIntoClosePaths:
    def test_both_close_paths_call_the_guard(self):
        for fn in (ibkr_client.close_position, ibkr_client.close_position_legs):
            src = inspect.getsource(fn)
            assert "_working_close_orders" in src, f"{fn.__name__} lost the idempotency guard"
            assert "AlreadyWorking" in src, f"{fn.__name__} no longer returns AlreadyWorking"
            assert "_close_ref_base" in src, f"{fn.__name__} no longer uses a position-scoped ref"
            assert "position_id" in inspect.signature(fn).parameters

    def test_bridge_threads_position_id(self):
        from agora.execution import ibkr_bridge
        src = inspect.getsource(ibkr_bridge.close_trade)
        assert "position_id=" in src, "close_trade must pass position_id to the close fn"

    def test_session_handles_already_working(self):
        from agora import session
        src = inspect.getsource(session.AgoraSession._execute_close)
        assert "AlreadyWorking" in src, "_execute_close must not retry/escalate an already-working close"

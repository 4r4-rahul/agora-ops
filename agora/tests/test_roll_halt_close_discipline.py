"""
agora/tests/test_roll_halt_close_discipline.py — REGRESSION GUARD for two close-discipline gaps
found auditing the order paths after the 2026-06-26 close-stacking runaway.

The discipline _execute_close already follows: NEVER mutate book state as if an order succeeded
unless it ACTUALLY filled. Two paths violated it:

  • _execute_roll reopened regardless of whether the close filled → with paper fill lag (or an
    AlreadyWorking / Cancelled / partial close) the old position is still live AND a new one opens
    = DOUBLE exposure.
  • the news-halt path marked the position closed (booking the assumed mark as realized P&L) right
    after close_trade, regardless of fill → a GHOST: DB closed, broker still holding a live,
    now-unmanaged position.

These tests fail loudly if either regresses.
"""
from __future__ import annotations

import inspect
import types

import pytest

import agora.session as session


def _pos(ticker="AAPL"):
    return types.SimpleNamespace(
        ticker=ticker, position_id="p1", expiry_date="2026-07-17",
        strategy="bull_call_spread", pillar="directional", direction="bullish",
        contracts=3, unrealized_pnl=123.0,
    )


class TestRollOnlyReopensAfterFill:
    """The roll must NOT reopen unless the close actually flattened."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["Submitted", "AlreadyWorking", "Cancelled", "PartiallyClosed"])
    async def test_roll_aborts_when_close_did_not_fill(self, status, monkeypatch):
        reopened = {"n": 0}

        async def fake_close(position, settings, session_id):
            return {"status": status}

        async def fake_submit(*a, **k):
            reopened["n"] += 1
            return {"status": "Filled", "fills": []}

        monkeypatch.setattr(session, "close_trade", fake_close)
        monkeypatch.setattr(session, "submit_trade", fake_submit, raising=False)

        stub = types.SimpleNamespace(
            _settings=types.SimpleNamespace(), _session_id="S",
        )
        # Must return cleanly WITHOUT reopening (no submit_trade call).
        await session.AgoraSession._execute_roll(stub, _pos(), "2026-08-21")
        assert reopened["n"] == 0, f"roll reopened despite close status={status} — double exposure"

    @pytest.mark.asyncio
    async def test_roll_close_exception_aborts(self, monkeypatch):
        reopened = {"n": 0}

        async def boom(position, settings, session_id):
            raise RuntimeError("broker down")

        async def fake_submit(*a, **k):
            reopened["n"] += 1
            return {"status": "Filled"}

        monkeypatch.setattr(session, "close_trade", boom)
        monkeypatch.setattr(session, "submit_trade", fake_submit, raising=False)
        stub = types.SimpleNamespace(_settings=types.SimpleNamespace(), _session_id="S")
        await session.AgoraSession._execute_roll(stub, _pos(), "2026-08-21")
        assert reopened["n"] == 0


class TestSourceDisciplineGuards:
    def test_roll_gates_reopen_on_filled(self):
        src = inspect.getsource(session.AgoraSession._execute_roll)
        assert 'close_status != "Filled"' in src or 'status") != "Filled"' in src, \
            "_execute_roll must only reopen after a Filled close"
        assert "Roll ABORTED" in src

    def test_news_halt_routes_through_execute_close(self):
        src = inspect.getsource(session.AgoraSession._news_watch_loop)
        # must NOT unconditionally mark closed; must go through _execute_close and gate the alert
        assert "_execute_close" in src, "news-halt must close via _execute_close (fill-gated)"
        assert "mark_position_closed" not in src, \
            "news-halt must not mark closed directly — _execute_close owns that (only on fill)"
        assert "if await self._execute_close" in src


class TestOverfillAutoHalt:
    """2026-06-26: a 659-contract over-fill went undetected by any automation — a human tripped
    the kill switch. The heal cycle must now auto-trip it the instant an over-fill is detected."""

    def test_run_position_heal_auto_trips_on_overfill(self):
        src = inspect.getsource(session.AgoraSession._run_position_heal)
        assert 'res.get("overfill_plan")' in src, "heal must inspect the over-fill plan"
        assert "trip_kill_switch" in src, "heal must auto-trip the kill switch on over-fill"
        assert 'tripped_by="overfill_autoheal"' in src
        # trips once, not every cycle
        assert "is_kill_switch_active" in src, "must guard so it trips once, not every cycle"

"""
agora/tests/test_cto_gates.py — the CTO's deterministic production controls that the session reads
each cycle: entries_halted, effective_size_bias (tightest of plan vs lateral override), and the
pre-delegated auto-close authority (50% profit target, spreads-only 21-DTE, min-hold guard, CEO
close-targets). These run with no LLM, so they must be exact — a wrong size label mis-sizes every
trade; a 21-DTE rule misfiring on a long option force-closes a fresh winner.

Pattern: bind the REAL CTO methods to a minimal stub carrying only what each method touches.
"""
from __future__ import annotations

import types
from datetime import date, timedelta

import pytest

from agora.c_suite.cto import CTOAgent


def _plan(is_halted=False, size_multiplier=1.0, close_targets=()):
    return types.SimpleNamespace(
        is_halted=is_halted, size_multiplier=size_multiplier,
        close_targets=set(close_targets),
    )


# ── entries_halted ────────────────────────────────────────────────────────────
class TestEntriesHalted:
    def _stub(self, halted_flag, plan):
        return types.SimpleNamespace(_entries_halted=halted_flag, get_session_plan=lambda: plan)

    def test_lateral_halt_flag_true(self):
        s = self._stub(True, _plan(is_halted=False))
        assert CTOAgent.entries_halted.fget(s) is True

    def test_session_plan_halt_true(self):
        s = self._stub(False, _plan(is_halted=True))
        assert CTOAgent.entries_halted.fget(s) is True

    def test_both_clear_false(self):
        s = self._stub(False, _plan(is_halted=False))
        assert CTOAgent.entries_halted.fget(s) is False


# ── effective_size_bias (tightest of plan vs lateral override) ────────────────
class TestEffectiveSizeBias:
    def _bias(self, plan_mult, override):
        s = types.SimpleNamespace(
            get_session_plan=lambda: _plan(size_multiplier=plan_mult),
            _size_override=override,
        )
        return CTOAgent.effective_size_bias.fget(s)

    def test_full_when_no_constraint(self):
        assert self._bias(1.0, None) == "full"

    def test_lateral_override_tightens(self):
        assert self._bias(1.0, "half") == "half"
        assert self._bias(1.0, "quarter") == "quarter"
        assert self._bias(1.0, "none") == "none"

    def test_plan_tightens_below_override(self):
        # plan 0.25 with a 'full' lateral → quarter (the tighter of the two)
        assert self._bias(0.25, "full") == "quarter"

    def test_takes_minimum_of_both(self):
        # plan 0.5, override quarter(0.25) → quarter
        assert self._bias(0.5, "quarter") == "quarter"

    def test_intermediate_rounds_up_to_next_band(self):
        # effective 0.30 → first band it is <= is 0.5 → 'half'
        assert self._bias(0.30, None) == "half"

    def test_zero_is_none(self):
        assert self._bias(0.0, None) == "none"


# ── _check_auto_close_candidates (pre-delegated close authority) ──────────────
def _position(ticker="AAPL", strategy="bull_put_spread", dte=45, held_days=5,
              unrealized_pnl=0.0, max_profit=300.0):
    today = date.today()
    return types.SimpleNamespace(
        ticker=ticker,
        strategy=types.SimpleNamespace(value=strategy),
        entry_date=today - timedelta(days=held_days),
        expiry_date=today + timedelta(days=dte),
        unrealized_pnl=unrealized_pnl,
        max_profit=max_profit,
        conviction_at_entry=65.0,
        regime_at_entry="neutral",
    )


class _CTOStub:
    """Carries exactly what _check_auto_close_candidates touches; records close calls."""
    def __init__(self, positions, plan=None, min_hold=1):
        self.closed: list[tuple] = []
        self._pm = types.SimpleNamespace(get_open_positions=lambda: positions)
        self._settings = types.SimpleNamespace(csuite_close_min_hold_days=min_hold)
        self._plan = plan or _plan()

        async def _close_cb(p, reason):
            self.closed.append((p.ticker, reason))

        async def _notify(event, payload):
            return None

        self._close_cb = _close_cb
        self.notify_peers = _notify
        self.get_session_plan = lambda: self._plan


async def _run(stub):
    await CTOAgent._check_auto_close_candidates(stub)


class TestAutoCloseAuthority:
    @pytest.mark.asyncio
    async def test_profit_target_closes_at_50pct(self):
        pos = _position(unrealized_pnl=180.0, max_profit=300.0)   # 60% of max
        stub = _CTOStub([pos])
        await _run(stub)
        assert stub.closed == [("AAPL", "profit_target_60%")]

    @pytest.mark.asyncio
    async def test_below_profit_target_not_closed_by_profit(self):
        pos = _position(unrealized_pnl=120.0, max_profit=300.0, dte=45)  # 40% < 50%, ample DTE
        stub = _CTOStub([pos])
        await _run(stub)
        assert stub.closed == []

    @pytest.mark.asyncio
    async def test_spread_closes_at_21_dte(self):
        pos = _position(strategy="bull_put_spread", dte=15, held_days=5, unrealized_pnl=0.0)
        stub = _CTOStub([pos])
        await _run(stub)
        assert ("AAPL", "21_dte_15DTE") in stub.closed

    @pytest.mark.asyncio
    async def test_long_option_exempt_from_21_dte(self):
        # long_call at 15 DTE must NOT be force-closed by the spread 21-DTE rule
        pos = _position(strategy="long_call", dte=15, held_days=5, unrealized_pnl=0.0, max_profit=0.0)
        stub = _CTOStub([pos])
        await _run(stub)
        assert stub.closed == []

    @pytest.mark.asyncio
    async def test_min_hold_guard_blocks_fresh_21_dte_close(self):
        # spread at 15 DTE but held 0 days → discretionary 21-DTE close is gated off
        pos = _position(strategy="bull_put_spread", dte=15, held_days=0, unrealized_pnl=0.0)
        stub = _CTOStub([pos], min_hold=1)
        await _run(stub)
        assert stub.closed == []

    @pytest.mark.asyncio
    async def test_ceo_close_target_closes(self):
        pos = _position(ticker="NVDA", dte=45, held_days=3, unrealized_pnl=0.0)
        stub = _CTOStub([pos], plan=_plan(close_targets={"NVDA"}), min_hold=1)
        await _run(stub)
        assert ("NVDA", "ceo_session_plan_target") in stub.closed

    @pytest.mark.asyncio
    async def test_profit_target_takes_precedence_over_dte(self):
        # 60% profit AND 15 DTE → the profit-target branch fires first and `continue`s
        pos = _position(strategy="bull_put_spread", dte=15, held_days=5,
                        unrealized_pnl=180.0, max_profit=300.0)
        stub = _CTOStub([pos])
        await _run(stub)
        assert stub.closed == [("AAPL", "profit_target_60%")]   # only one close, profit reason

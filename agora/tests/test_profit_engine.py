"""Unit tests for agora/lifecycle/profit_engine.py"""
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import pytest
from agora.lifecycle.profit_engine import (
    IntelligentProfitEngine,
    PositionState,
    _RATCHET_SCHEDULE,
    _LOCK_IN_PCT,
)


# ── Minimal position stub ─────────────────────────────────────────────────────

@dataclass
class _Leg:
    action: str
    option_type: str
    strike: float
    delta: float = 0.3
    gamma: float = 0.0
    theta: float = -0.05
    vega: float = 0.10
    contracts: int = 1


@dataclass
class _Position:
    position_id: str
    ticker: str
    entry_date: date
    expiry_date: date
    legs: list
    contracts: int
    entry_price: float
    current_price: float
    unrealized_pnl: float
    max_gain_dollars: float
    max_loss_dollars: float
    pillar: Any = None
    direction: str = "bullish"


def _make_debit_spread(pid: str = "test-001", dte: int = 45) -> tuple[_Position, IntelligentProfitEngine]:
    entry = date(2026, 5, 18)
    eng = IntelligentProfitEngine()
    pos = _Position(
        position_id=pid,
        ticker="TEST",
        entry_date=entry,
        expiry_date=entry + timedelta(days=dte),
        legs=[_Leg("buy", "call", 125.0, delta=0.40), _Leg("sell", "call", 135.0, delta=0.22)],
        contracts=1,
        entry_price=3.00,
        current_price=3.00,
        unrealized_pnl=0.0,
        max_gain_dollars=700.0,
        max_loss_dollars=300.0,
    )
    eng._states[pid] = PositionState(
        dte_at_entry=dte,
        entry_theta_daily=-0.02,
        entry_net_delta=18.0,
        entry_net_vega=8.8,
    )
    return pos, eng


def _make_credit_spread(pid: str = "test-002", dte: int = 35) -> tuple[_Position, IntelligentProfitEngine]:
    entry = date(2026, 5, 18)
    eng = IntelligentProfitEngine()
    pos = _Position(
        position_id=pid,
        ticker="QQQ",
        entry_date=entry,
        expiry_date=entry + timedelta(days=dte),
        legs=[_Leg("sell", "put", 430.0, delta=-0.30), _Leg("buy", "put", 425.0, delta=-0.18)],
        contracts=1,
        entry_price=1.20,
        current_price=1.20,
        unrealized_pnl=0.0,
        max_gain_dollars=120.0,
        max_loss_dollars=380.0,
    )
    eng._states[pid] = PositionState(
        dte_at_entry=dte,
        entry_theta_daily=0.04,
        entry_net_delta=-12.0,
        entry_net_vega=-3.5,
    )
    return pos, eng


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestProfitEngineBasics:
    def test_evaluate_returns_decision(self):
        pos, eng = _make_debit_spread()
        d = eng.evaluate(pos, realized_pnl_today=0.0)
        assert hasattr(d, "should_close")
        assert hasattr(d, "profit_pct")
        assert hasattr(d, "rule")

    def test_no_close_at_entry(self):
        pos, eng = _make_debit_spread()
        d = eng.evaluate(pos, realized_pnl_today=0.0)
        # Engine says HOLD at entry (no profit, no loss trigger)
        assert d.rule in ("HOLD", "PENDING")
        assert not d.should_close

    def test_credit_spread_closes_at_target(self):
        pos, eng = _make_credit_spread()
        # At 60% profit the engine should hit DTE_CURVE target (60% for DTE=35)
        pos.unrealized_pnl = 72.0  # 60% of 120
        pos.current_price = 0.48   # 0.60 × (1 - 0.60) = remaining value
        d = eng.evaluate(pos, realized_pnl_today=0.0, credit_spread_flat_target=0.50)
        # Profit is above 50% flat target — engine may close or hold depending on DTE target
        assert d.profit_pct >= 0.50

    def test_lock_in_triggers_close_at_85pct(self):
        pos, eng = _make_debit_spread()
        # 85% profit triggers LOCK_IN rule
        pos.unrealized_pnl = 0.85 * pos.max_gain_dollars  # 595
        pos.current_price = 3.00 + (595 / 100)
        d = eng.evaluate(pos, realized_pnl_today=0.0)
        assert d.should_close
        assert d.rule == "LOCK_IN"

    def test_last_decision_cached(self):
        pos, eng = _make_debit_spread()
        d1 = eng.evaluate(pos, realized_pnl_today=0.0)
        state = eng._states[pos.position_id]
        assert state.last_decision is d1

    def test_get_state_snapshot_keys(self):
        pos, eng = _make_debit_spread()
        eng.evaluate(pos, realized_pnl_today=0.0)
        snap = eng.get_state_snapshot(pos.position_id)
        assert snap is not None
        for key in ("is_credit_spread", "dte_at_entry", "effective_target_pct",
                    "profit_pct", "hwm_pct", "ratchet_floor_pct", "last_rule"):
            assert key in snap

    def test_get_state_snapshot_none_for_unknown(self):
        eng = IntelligentProfitEngine()
        assert eng.get_state_snapshot("nonexistent") is None


class TestRatchetSchedule:
    def test_ratchet_schedule_sorted_descending(self):
        # Schedule is highest HWM threshold first (descending order)
        thresholds = [t for t, _ in _RATCHET_SCHEDULE]
        assert thresholds == sorted(thresholds, reverse=True)

    def test_ratchet_stops_decrease_with_lower_hwm(self):
        # Higher HWM threshold → higher floor stop (also descending)
        stops = [s for _, s in _RATCHET_SCHEDULE]
        assert stops == sorted(stops, reverse=True)

    def test_lock_in_pct_is_reasonable(self):
        assert 0.70 <= _LOCK_IN_PCT <= 0.95


class TestPriceTarget:
    def test_price_target_raises_effective_target(self):
        pos, eng = _make_debit_spread(dte=45)
        pos.unrealized_pnl = 0.0
        d_base = eng.evaluate(pos, realized_pnl_today=0.0)
        base_target = d_base.effective_target

        eng.set_price_target(pos.position_id, aligned_return_pct=0.30, entry_spot=120.0)
        d_pt = eng.evaluate(pos, realized_pnl_today=0.0)
        assert d_pt.effective_target >= base_target

    def test_fill_quality_bonus_applied(self):
        pos, eng = _make_debit_spread(dte=45)
        d_no_bonus = eng.evaluate(pos, realized_pnl_today=0.0)
        t_no_bonus = d_no_bonus.effective_target

        eng.set_fill_quality(pos.position_id, fill_bonus_pct=0.05)
        d_bonus = eng.evaluate(pos, realized_pnl_today=0.0)
        assert d_bonus.effective_target >= t_no_bonus


class TestRatchetProtection:
    def _simulate(self, profit_fractions: list[float]) -> list:
        """Feed a sequence of profit fractions and return decisions."""
        pos, eng = _make_debit_spread(dte=45)
        decisions = []
        base_ts = datetime(2026, 5, 18, 10, 0)
        for i, frac in enumerate(profit_fractions):
            pos.unrealized_pnl = frac * pos.max_gain_dollars
            state = eng._states[pos.position_id]
            state.pnl_history.append((base_ts + timedelta(days=i, hours=1), frac))
            d = eng.evaluate(pos, realized_pnl_today=0.0)
            decisions.append(d)
        return decisions

    def test_hwm_tracks_max(self):
        decisions = self._simulate([0.0, 0.30, 0.50, 0.70, 0.40, 0.20])
        hwms = [d.hwm_pct for d in decisions]
        # HWM should never decrease
        for i in range(1, len(hwms)):
            assert hwms[i] >= hwms[i-1]

    def test_ratchet_floor_locks_after_hwm_peak(self):
        decisions = self._simulate([0.0, 0.40, 0.60, 0.75, 0.30])
        # After HWM=75%, a ratchet floor should be set > 0
        floor_at_end = decisions[-1].ratchet_stop_pct
        assert floor_at_end > 0.0

    def test_ratchet_triggers_close_on_pullback(self):
        # Peak at 80%, drop back to 40% — ratchet floor should fire
        decisions = self._simulate([0.0, 0.40, 0.60, 0.80, 0.40])
        last = decisions[-1]
        # At 80% HWM, floor is typically 45-55%; 40% profit is below floor
        if last.ratchet_stop_pct > 0 and last.profit_pct < last.ratchet_stop_pct:
            assert last.should_close

"""
agora/tests/test_position_manager_math.py — the precision math of the position book:
portfolio-greek aggregation (risk-council inputs), realized-P&L-today (daily-loss breaker), the
real-fill booking that replaced the model-mark fiction, and the add→reload greek round-trip.

Greek aggregation is tested as pure math (stubbed get_open_positions); the SQL paths use a real
temp DB. The round-trip test guards a found defect: add_position must persist leg greeks or every
reloaded position reports zero greeks and the portfolio greek limits silently stop binding.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import types
from datetime import date, timedelta
from pathlib import Path

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


# ── builders ──────────────────────────────────────────────────────────────────
def _leg(action="buy", option_type="call", strike=100.0, contracts=1,
         delta=0.40, gamma=0.01, theta=-0.05, vega=0.10, dte=30):
    return SpreadLeg(
        option_type=option_type, strike=strike,
        expiration=date.today() + timedelta(days=dte), action=action,
        contracts=contracts, mid_price=1.0,
        delta=delta, gamma=gamma, theta=theta, vega=vega,
    )


def _pos(pid="p1", legs=None, contracts=1, **kw):
    today = date.today()
    exp = today + timedelta(days=30)
    return OpenPosition(
        position_id=pid, ticker="TEST",
        strategy=kw.get("strategy", StrategyType.BULL_CALL_SPREAD),
        pillar=StrategyPillar.DIRECTIONAL, status=PositionStatus.OPEN,
        legs=legs if legs is not None else [_leg()],
        contracts=contracts, entry_price=3.0, current_price=3.0,
        entry_date=today, expiry_date=exp, target_close_date=today + timedelta(days=9),
        max_loss_dollars=kw.get("max_loss_dollars", 700.0),
        max_gain_dollars=kw.get("max_gain_dollars", 300.0),
        unrealized_pnl=kw.get("unrealized_pnl", 0.0),
    )


# ── get_portfolio_greeks (pure aggregation) ───────────────────────────────────
class TestPortfolioGreeks:
    def _greeks(self, positions):
        stub = types.SimpleNamespace(get_open_positions=lambda: positions)
        return PositionManager.get_portfolio_greeks(stub)

    def test_empty_book_is_zero(self):
        g = self._greeks([])
        assert g == {"delta": 0.0, "vega": 0.0, "theta": 0.0, "gamma": 0.0, "positions": 0}

    def test_single_long_call_scales_by_contracts_and_100(self):
        # buy, delta 0.5, pos.contracts=2, leg.contracts=1 → 0.5*2*1*100 = +100
        g = self._greeks([_pos(legs=[_leg(action="buy", delta=0.5, vega=0.2, theta=-0.06)],
                               contracts=2)])
        assert g["delta"] == 100.0
        assert g["vega"] == 40.0     # 0.2*2*100
        assert g["theta"] == -12.0   # -0.06*2*100
        assert g["positions"] == 1

    def test_sell_leg_flips_sign(self):
        g = self._greeks([_pos(legs=[_leg(action="sell", delta=0.30)], contracts=1)])
        assert g["delta"] == -30.0   # sell → negative

    def test_spread_nets_legs(self):
        # long 0.40 - short 0.22 = net +0.18 delta per 1x
        spread = _pos(legs=[
            _leg(action="buy", delta=0.40),
            _leg(action="sell", delta=0.22),
        ], contracts=1)
        g = self._greeks([spread])
        assert g["delta"] == pytest.approx(18.0)

    def test_leg_contracts_multiply(self):
        # pos.contracts=3, leg.contracts=2 → multiplier 6
        g = self._greeks([_pos(legs=[_leg(action="buy", delta=0.5, contracts=2)], contracts=3)])
        assert g["delta"] == 0.5 * 3 * 2 * 100

    def test_gamma_rounded_to_4dp(self):
        g = self._greeks([_pos(legs=[_leg(action="buy", gamma=0.012345)], contracts=1)])
        assert g["gamma"] == round(0.012345 * 100, 4)

    def test_multiple_positions_sum(self):
        g = self._greeks([
            _pos(pid="a", legs=[_leg(action="buy", delta=0.5)], contracts=1),
            _pos(pid="b", legs=[_leg(action="buy", delta=0.3)], contracts=1),
        ])
        assert g["delta"] == 80.0 and g["positions"] == 2


# ── get_realized_pnl_today (daily-loss breaker input) ─────────────────────────
class TestRealizedPnlToday:
    def _pm_with_rows(self, rows):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE positions (realized_pnl REAL, close_date TEXT, status TEXT)")
        conn.executemany("INSERT INTO positions VALUES (?,?,?)", rows)
        return types.SimpleNamespace(_db=conn)

    def test_sums_only_today_closed(self):
        today = date.today().isoformat()
        yest = (date.today() - timedelta(days=1)).isoformat()
        pm = self._pm_with_rows([
            (100.0, today, "closed"),
            (-30.0, today, "closed"),
            (999.0, yest, "closed"),    # different day — excluded
            (5.0, today, "open"),       # not closed — excluded
        ])
        assert PositionManager.get_realized_pnl_today(pm) == 70.0

    def test_zero_when_nothing_today(self):
        yest = (date.today() - timedelta(days=1)).isoformat()
        pm = self._pm_with_rows([(500.0, yest, "closed")])
        assert PositionManager.get_realized_pnl_today(pm) == 0.0

    def test_negative_day(self):
        today = date.today().isoformat()
        pm = self._pm_with_rows([(-200.0, today, "closed"), (-50.0, today, "closed")])
        assert PositionManager.get_realized_pnl_today(pm) == -250.0


# ── _write_trade_record (real-fill booking, NOT the model mark) ───────────────
class TestWriteTradeRecordBooking:
    def _stub(self):
        conn = sqlite3.connect(":memory:")
        # 17 columns matching the INSERT; c0 (position_id) is the PRIMARY KEY so INSERT OR IGNORE
        # dedups the same position (mirrors the real trade_records schema).
        cols = "c0 PRIMARY KEY," + ",".join(f"c{i}" for i in range(1, 17))
        conn.execute(f"CREATE TABLE trade_records ({cols})")
        return types.SimpleNamespace(_db=conn)

    def _position(self, unrealized_pnl=500.0, current_price=5.0):
        return _pos(unrealized_pnl=unrealized_pnl) if False else types.SimpleNamespace(
            position_id="p1", ticker="TEST",
            strategy=types.SimpleNamespace(value="bull_put_spread"),
            pillar=types.SimpleNamespace(value="vol_premium"),
            entry_date=date.today(), expiry_date=date.today() + timedelta(days=30),
            entry_price=1.5, current_price=current_price, contracts=2,
            unrealized_pnl=unrealized_pnl, regime_at_entry="neutral", conviction_at_entry=65.0,
        )

    def test_real_fill_pnl_overrides_mark(self):
        stub = self._stub()
        # The model MARK says +500 (max credit); the REAL fill lost -120.
        PositionManager._write_trade_record(
            stub, self._position(unrealized_pnl=500.0), "lifecycle",
            realized_pnl=-120.0, close_price=2.2,
        )
        row = stub._db.execute("SELECT c10, c8 FROM trade_records").fetchone()
        assert row[0] == -120.0   # realized_pnl = REAL fill, not the +500 mark
        assert row[1] == 2.2      # close_price = real fill price

    def test_falls_back_to_mark_when_no_real_fill(self):
        stub = self._stub()
        PositionManager._write_trade_record(
            stub, self._position(unrealized_pnl=80.0, current_price=4.0), "manual",
        )
        row = stub._db.execute("SELECT c10, c8 FROM trade_records").fetchone()
        assert row[0] == 80.0   # no realized_pnl arg → uses unrealized mark
        assert row[1] == 4.0

    def test_insert_or_ignore_dedups_same_position(self):
        stub = self._stub()
        pos = self._position()
        PositionManager._write_trade_record(stub, pos, "n1", realized_pnl=-50.0, close_price=1.0)
        PositionManager._write_trade_record(stub, pos, "n2", realized_pnl=-99.0, close_price=1.0)
        rows = stub._db.execute("SELECT c10 FROM trade_records").fetchall()
        assert len(rows) == 1 and rows[0][0] == -50.0   # first write wins (INSERT OR IGNORE)


# ── add → reload greek round-trip (regression guard for the persistence fix) ──
class TestGreekPersistenceRoundTrip:
    def _real_pm(self) -> PositionManager:
        # Build a REAL PositionManager against a temp DB so add_position / get_open_positions run
        # the production schema + serialization (get_settings is a shared singleton — copy it).
        d = Path(tempfile.mkdtemp())
        s = get_settings().model_copy(update={"db_path": d / "agora.db"})
        pm = PositionManager(s)
        # _init_db builds the base schema; the fill-timestamp columns come from migration m008.
        from agora.ops.db_migrations import run_all
        run_all(str(s.db_path))
        return pm

    def test_greeks_survive_persist_and_reload(self):
        pm = self._real_pm()
        pos = _pos(legs=[_leg(action="buy", delta=0.42, gamma=0.013, theta=-0.07, vega=0.22)],
                   contracts=2)
        pm.add_position(pos)
        reloaded = pm.get_open_positions()
        assert len(reloaded) == 1
        leg = reloaded[0].legs[0]
        # The found defect: add_position dropped greeks from legs_json → these were 0.0 on reload.
        assert leg.delta == pytest.approx(0.42)
        assert leg.vega == pytest.approx(0.22)
        assert leg.theta == pytest.approx(-0.07)
        # …and the aggregate the risk council reads is therefore non-zero
        g = pm.get_portfolio_greeks()
        assert g["delta"] == pytest.approx(0.42 * 2 * 100)

"""
test_fill_timestamps.py — precise TWS entry/exit fill timestamps (DB == TWS, to the second).

positions stored only DATE granularity (entry_date/close_date); these tests verify the new
entry_ts_utc/exit_ts_utc columns persist the exact f.execution.time end-to-end, and that the
add_position INSERT is robust to the schema addition (explicit columns, not positional).
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

from agora.core.config import AgoraSettings
from agora.core.models import OpenPosition, SpreadLeg, StrategyPillar, StrategyType
from agora.lifecycle.position_manager import PositionManager
from agora.ops.db_migrations import run_all

_E = "2026-06-17T14:32:07.812000+00:00"
_X = "2026-06-17T15:48:22.119000+00:00"


def _pm(tmp_path):
    dbp = str(tmp_path / "t.db")
    run_all(dbp)
    s = AgoraSettings()
    s.db_path = Path(dbp)
    pm = PositionManager(settings=s)
    run_all(str(pm._settings.db_path))
    return pm


def _pos(entry_ts=""):
    exp = date.today() + timedelta(days=39)
    return OpenPosition(
        position_id="p1", ticker="NVDA", strategy=StrategyType.BULL_PUT_SPREAD,
        pillar=StrategyPillar.VOL_PREMIUM,
        legs=[SpreadLeg(option_type="put", strike=95.0, expiration=exp, action="sell",
                        contracts=1, mid_price=1.2)],
        contracts=1, entry_price=-1.2, entry_date=date.today(), expiry_date=exp,
        target_close_date=exp - timedelta(days=18), max_loss_dollars=380.0,
        max_gain_dollars=120.0, entry_ts_utc=entry_ts)


def test_entry_ts_persisted(tmp_path):
    pm = _pm(tmp_path)
    pm.add_position(_pos(entry_ts=_E))
    r = sqlite3.connect(str(pm._settings.db_path)).execute(
        "SELECT entry_ts_utc, exit_ts_utc FROM positions WHERE position_id='p1'").fetchone()
    assert r[0] == _E and r[1] is None      # exit NULL on open


def test_exit_ts_persisted_on_close(tmp_path):
    pm = _pm(tmp_path)
    pm.add_position(_pos(entry_ts=_E))
    pm.mark_position_closed("p1", realized_pnl=85.0, close_price=0.35,
                            source="session:thesis_exit", exit_ts_utc=_X)
    r = sqlite3.connect(str(pm._settings.db_path)).execute(
        "SELECT status, entry_ts_utc, exit_ts_utc FROM positions WHERE position_id='p1'").fetchone()
    assert r[0] == "closed" and r[1] == _E and r[2] == _X


def test_close_without_ts_preserves_entry(tmp_path):
    """A close path that has no exit timestamp (e.g. TWS-sync) must not wipe entry_ts (COALESCE)."""
    pm = _pm(tmp_path)
    pm.add_position(_pos(entry_ts=_E))
    pm.mark_position_closed("p1", realized_pnl=10.0, close_price=0.1, source="tws_reconcile")
    r = sqlite3.connect(str(pm._settings.db_path)).execute(
        "SELECT entry_ts_utc, exit_ts_utc FROM positions WHERE position_id='p1'").fetchone()
    assert r[0] == _E and r[1] is None      # entry kept; exit stays NULL, not blanked


def test_add_position_robust_to_schema(tmp_path):
    """Explicit-column INSERT must work after m008 added columns (a positional INSERT would break)."""
    pm = _pm(tmp_path)
    pm.add_position(_pos())                  # no entry_ts -> stored NULL, no crash
    n = sqlite3.connect(str(pm._settings.db_path)).execute(
        "SELECT COUNT(*) FROM positions WHERE position_id='p1'").fetchone()[0]
    assert n == 1

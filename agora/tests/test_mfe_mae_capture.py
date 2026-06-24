"""
agora/tests/test_mfe_mae_capture.py — the path-capture instrumentation fix (2026-06-24).

The old max_favorable_pct/max_adverse_pct were reconstructed from sparse DAILY lifecycle_snapshots
(MAX(captured_pct_gain)/MIN(unrealized_pct_risk)). On a ~2-day swing book that gave 1-2 daily points
per trade → true intraday peak/trough missed → ~27% zeros and impossible values (a winner showing
-153% favorable). Fix: track RUNNING peak/trough unrealized P&L (dollars) on the position at every
mark, and have the feature store convert those to pct (peak/max_gain, trough/max_loss), preferring
them over the snapshot reconstruction. These tests lock both halves + backward compatibility.
"""
from __future__ import annotations

import sqlite3
import tempfile

from agora.ops.feature_store import build_feature_store

# The exact running-update from PositionManager._update_position_price (MAX/MIN + COALESCE seed).
_MARK_SQL = (
    "UPDATE positions SET unrealized_pnl=?, "
    "peak_unrealized_pnl = MAX(COALESCE(peak_unrealized_pnl, ?), ?), "
    "trough_unrealized_pnl = MIN(COALESCE(trough_unrealized_pnl, ?), ?) "
    "WHERE position_id=?"
)


def _mark(c, pid, upnl):
    c.execute(_MARK_SQL, (upnl, upnl, upnl, upnl, upnl, pid))


def test_running_peak_trough_tracks_true_excursion():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE positions (position_id TEXT, unrealized_pnl REAL, "
              "peak_unrealized_pnl REAL, trough_unrealized_pnl REAL)")
    c.execute("INSERT INTO positions (position_id) VALUES ('x')")
    for upnl in (50.0, 120.0, -30.0, 80.0, -10.0):   # peak 120, trough -30
        _mark(c, "x", upnl)
    peak, trough = c.execute(
        "SELECT peak_unrealized_pnl, trough_unrealized_pnl FROM positions WHERE position_id='x'").fetchone()
    assert peak == 120.0 and trough == -30.0


def test_first_mark_seeds_both_from_current():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE positions (position_id TEXT, unrealized_pnl REAL, "
              "peak_unrealized_pnl REAL, trough_unrealized_pnl REAL)")
    c.execute("INSERT INTO positions (position_id) VALUES ('x')")
    _mark(c, "x", -40.0)   # only ever in the red → peak == trough == -40 (no positive excursion)
    peak, trough = c.execute(
        "SELECT peak_unrealized_pnl, trough_unrealized_pnl FROM positions WHERE position_id='x'").fetchone()
    assert peak == -40.0 and trough == -40.0


def _fs_db(*, with_running):
    """positions + (empty) deps for build_feature_store. with_running adds peak/trough cols+values."""
    p = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    c = sqlite3.connect(p)
    extra = ", peak_unrealized_pnl REAL, trough_unrealized_pnl REAL" if with_running else ""
    c.execute(f"""CREATE TABLE positions (position_id TEXT, ticker TEXT, strategy TEXT, pillar TEXT,
        direction TEXT, status TEXT, entry_price REAL, max_loss_dollars REAL, max_gain_dollars REAL,
        entry_date TEXT, expiry_date TEXT, conviction_at_entry REAL, regime_at_entry TEXT,
        realized_pnl REAL, close_date TEXT, close_source TEXT{extra})""")
    c.execute("CREATE TABLE decision_chains (chain_id TEXT, position_id TEXT, triggered_by TEXT, gates_passed TEXT, started_at TEXT)")
    c.execute("""CREATE TABLE lifecycle_snapshots (position_id TEXT, snapshot_date TEXT, days_held INTEGER,
        unrealized_pct_risk REAL, captured_pct_gain REAL, net_delta REAL, net_theta REAL)""")
    base = ("w1", "AAA", "bull_put_spread", "vol_premium", "bullish", "closed", -1.5, 300.0, 200.0,
            "2026-06-13", "2026-07-17", 55.0, "neutral", 100.0, "2026-06-20", "thesis_exit")
    if with_running:
        c.execute("INSERT INTO positions VALUES (" + ",".join("?" * 18) + ")", base + (150.0, -60.0))
    else:
        c.execute("INSERT INTO positions VALUES (" + ",".join("?" * 16) + ")", base)
    # DELIBERATELY WRONG snapshot values — to prove the running fields win when present.
    c.executemany("INSERT INTO lifecycle_snapshots VALUES (?,?,?,?,?,?,?)", [
        ("w1", "2026-06-18", 5, -0.99, -0.50, 0.0, 0.0)])   # garbage: -50% "favorable"
    c.commit(); c.close()
    return p


def test_feature_store_prefers_running_mfe_mae():
    db = _fs_db(with_running=True)
    build_feature_store(db, legacy_cutoff="2026-06-12")
    c = sqlite3.connect(db); c.row_factory = sqlite3.Row
    w1 = c.execute("SELECT * FROM trade_features WHERE position_id='w1'").fetchone()
    # peak 150 / max_gain 200 = 0.75 ; trough -60 / max_loss 300 = -0.20  (NOT the -0.50/-0.99 garbage)
    assert w1["max_favorable_pct"] == 0.75
    assert w1["max_adverse_pct"] == -0.20


def test_feature_store_falls_back_to_snapshots_when_no_running():
    db = _fs_db(with_running=False)   # legacy position, no peak/trough columns
    build_feature_store(db, legacy_cutoff="2026-06-12")
    c = sqlite3.connect(db); c.row_factory = sqlite3.Row
    w1 = c.execute("SELECT * FROM trade_features WHERE position_id='w1'").fetchone()
    assert w1["max_favorable_pct"] == -0.50 and w1["max_adverse_pct"] == -0.99   # snapshot values

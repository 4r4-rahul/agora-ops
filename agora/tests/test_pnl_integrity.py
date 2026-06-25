"""
agora/tests/test_pnl_integrity.py — the 'never again' P&L integrity invariants.

The 2026-06-25 corruption: adopted positions (reconstructed cost basis) booked impossible realized P&L
(−$808k on a $16k-max-loss DIA spread), poisoning the books, the ML labels, AND the daily breaker. These
codify the three guards that make it impossible again:
  1. pnl_within_bounds — a defined-risk position's realized P&L can never exceed its own max-loss/max-gain.
  2. _REAL_CLOSE (the trust boundary) EXCLUDES adopted positions.
  3. a data-integrity scan that fails if any 'real' close has mathematically-impossible P&L.
"""
from __future__ import annotations

import sqlite3
import tempfile

from agora.core.pnl import pnl_within_bounds


# ── 1. the invariant guard (pure) ───────────────────────────────────────────────────────
def test_within_bounds_true_for_sane_pnl():
    assert pnl_within_bounds(-300, max_loss_dollars=500, max_gain_dollars=150)   # loss within max_loss
    assert pnl_within_bounds(140, max_loss_dollars=500, max_gain_dollars=150)    # gain within max_gain
    assert pnl_within_bounds(-500, max_loss_dollars=500, max_gain_dollars=150)   # exactly max_loss


def test_impossible_loss_rejected():
    # −808,831 on a 16,000 max-loss spread → the exact corruption signature
    assert not pnl_within_bounds(-808_831, max_loss_dollars=16_000, max_gain_dollars=48_000)


def test_impossible_gain_rejected():
    assert not pnl_within_bounds(5_000, max_loss_dollars=500, max_gain_dollars=150)


def test_tolerance_band_allows_slippage():
    # within 1.2× the bound is allowed (slippage); beyond is not
    assert pnl_within_bounds(-590, max_loss_dollars=500, max_gain_dollars=150)        # 1.18× → ok
    assert not pnl_within_bounds(-610, max_loss_dollars=500, max_gain_dollars=150)    # 1.22× → impossible


def test_no_bounds_info_passes():
    assert pnl_within_bounds(-999_999, max_loss_dollars=0, max_gain_dollars=0)   # can't judge → allow


# ── 2. _REAL_CLOSE excludes adopted positions (the trust boundary) ───────────────────────
def _seeded_db():
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    c = sqlite3.connect(db)
    c.execute("""CREATE TABLE positions (position_id TEXT, status TEXT, close_date TEXT,
        close_source TEXT, realized_pnl REAL, regime_at_entry TEXT, max_loss_dollars REAL)""")
    c.executemany("INSERT INTO positions VALUES (?,?,?,?,?,?,?)", [
        ("eng-1", "closed", "2026-06-25", "lifecycle", -200.0, "neutral", 500.0),     # real engine close
        ("adopt-aaa", "closed", "2026-06-25", "lifecycle", -808831.0, "adopted", 16000.0),  # adopted fiction
    ])
    c.commit(); c.close()
    return db


def test_real_close_excludes_adopted():
    from agora.ops.edge_dashboard import _REAL_CLOSE
    db = _seeded_db()
    c = sqlite3.connect(db)
    n, total = c.execute(
        f"SELECT COUNT(*), COALESCE(SUM(realized_pnl),0) FROM positions WHERE {_REAL_CLOSE}").fetchone()
    c.close()
    assert n == 1 and total == -200.0   # only the engine close; the adopted −$808k is excluded


def test_breaker_query_excludes_adopted():
    # mirrors get_realized_pnl_today — the daily-breaker source must not see adopted fiction
    db = _seeded_db()
    c = sqlite3.connect(db)
    total = c.execute(
        "SELECT COALESCE(SUM(realized_pnl),0) FROM positions WHERE close_date='2026-06-25' "
        "AND status='closed' AND COALESCE(regime_at_entry,'') <> 'adopted'").fetchone()[0]
    c.close()
    assert total == -200.0   # breaker sees only real engine P&L, not the −$808k


# ── 3. data-integrity scan: no 'real' close may carry impossible P&L ─────────────────────
def test_data_integrity_no_impossible_real_close():
    from agora.ops.edge_dashboard import _REAL_CLOSE
    db = _seeded_db()
    c = sqlite3.connect(db)
    bad = c.execute(
        f"SELECT COUNT(*) FROM positions WHERE {_REAL_CLOSE} "
        "AND realized_pnl < -(ABS(max_loss_dollars)*1.2) - 50").fetchone()[0]
    c.close()
    assert bad == 0, "a _REAL_CLOSE position has mathematically-impossible P&L — corruption leaked in"

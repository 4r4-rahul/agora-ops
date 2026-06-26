"""
agora/tests/test_book_manager.py — the single-source-of-truth book manager.

Locks the three invariants that kill the recurring "DB vs UI mismatch":
  1. EVERY-PENNY-ACCOUNTED: real_strategy + Σexcluded == the naïve all-closed sum, to the penny
     (the excluded buckets are an exact, non-overlapping partition by cause).
  2. RECONCILIATION: rebuild_daily_pnl() always drives the daily_pnl ledger to match the book
     (drift → 0), and reconcile() proves it — derived projection, never an independent writer.
  3. REAL ≠ FICTION: adopted-legacy / reconcile-artifact / fabricated dollars are separated from
     real strategy P&L, so the headline number is honest.
"""
from __future__ import annotations

import sqlite3

from agora.ops.book_manager import (
    canonical_book,
    real_realized_by_day,
    rebuild_daily_pnl,
    reconcile,
)


def _db(tmp_path, positions, daily_pnl=()):
    p = str(tmp_path / "book.db")
    conn = sqlite3.connect(p)
    conn.execute(
        "CREATE TABLE positions (status TEXT, close_date TEXT, close_source TEXT, "
        "regime_at_entry TEXT DEFAULT '', realized_pnl REAL DEFAULT 0, unrealized_pnl REAL DEFAULT 0)"
    )
    conn.executemany(
        "INSERT INTO positions (status, close_date, close_source, regime_at_entry, realized_pnl, "
        "unrealized_pnl) VALUES (?,?,?,?,?,?)", positions,
    )
    conn.execute("CREATE TABLE daily_pnl (record_date TEXT PRIMARY KEY, realized_pnl REAL DEFAULT 0, "
                 "unrealized_pnl REAL DEFAULT 0, trades_count INTEGER DEFAULT 0)")
    conn.executemany("INSERT INTO daily_pnl VALUES (?,?,?,?)", daily_pnl)
    conn.commit(); conn.close()
    return p


# (status, close_date, close_source, regime, realized, unrealized)
_REAL_A = ("closed", "2026-06-20", "lifecycle", "neutral", 100.0, 0.0)
_REAL_B = ("closed", "2026-06-20", "thesis_exit", "risk_off", -50.0, 0.0)
_REAL_C = ("closed", "2026-06-21", "stop_loss", "neutral", -30.0, 0.0)
_ADOPTED = ("closed", "2026-06-19", "lifecycle", "adopted", -800.0, 0.0)
_RECON = ("closed", "2026-06-19", "reconcile_ghost", "neutral", -10.0, 0.0)
_FAB = ("closed", "2026-06-19", "fabricated_smoke", "neutral", 0.0, 0.0)
_OTHER = ("closed", "2026-06-19", "weird_unknown_source", "neutral", 5.0, 0.0)
_OPEN = ("open", None, "", "neutral", 0.0, 42.0)


class TestEveryPennyAccounted:
    def test_partition_is_exact_and_exhaustive(self, tmp_path):
        db = _db(tmp_path, [_REAL_A, _REAL_B, _REAL_C, _ADOPTED, _RECON, _FAB, _OTHER, _OPEN])
        b = canonical_book(db)
        assert b["partition_ok"] is True and b["partition_residual"] == 0.0
        # real strategy = 100 - 50 - 30 = 20
        assert b["real_strategy"]["net_realized"] == 20.0 and b["real_strategy"]["n_closed"] == 3
        # naïve all-closed = 20 - 800 - 10 + 0 + 5 = -785
        assert b["naive_all_closed"] == -785.0
        # real + excluded == naïve, to the penny
        assert round(b["real_strategy"]["net_realized"] + b["excluded"]["total"], 2) == b["naive_all_closed"]

    def test_excluded_buckets_are_attributed_by_cause(self, tmp_path):
        db = _db(tmp_path, [_REAL_A, _ADOPTED, _RECON, _FAB, _OTHER])
        ex = canonical_book(db)["excluded"]
        assert ex["adopted_legacy"]["pnl"] == -800.0 and ex["adopted_legacy"]["n"] == 1
        assert ex["reconcile_artifact"]["pnl"] == -10.0
        assert ex["fabricated_test"]["pnl"] == 0.0
        assert ex["other_excluded"]["pnl"] == 5.0   # nothing falls through the cracks

    def test_adopted_takes_priority_over_source(self, tmp_path):
        # an adopted row that ALSO has a reconcile source counts ONCE, as adopted (no double-count)
        dual = ("closed", "2026-06-19", "reconcile_ghost", "adopted", -100.0, 0.0)
        db = _db(tmp_path, [_REAL_A, dual])
        ex = canonical_book(db)["excluded"]
        assert ex["adopted_legacy"]["pnl"] == -100.0 and ex["reconcile_artifact"]["pnl"] == 0.0
        assert canonical_book(db)["partition_residual"] == 0.0


class TestReconciliation:
    def test_rebuild_drives_drift_to_zero(self, tmp_path):
        # daily_pnl ledger is WRONG (drifted) + has a fiction day with no real close behind it
        drifted = [
            ("2026-06-20", 999.0, 0.0, 0),   # should be 50 (100-50)
            ("2026-06-21", -30.0, 0.0, 0),   # correct
            ("2026-06-18", 314.0, 0.0, 0),   # FICTION: no real close on this day → must be zeroed
        ]
        db = _db(tmp_path, [_REAL_A, _REAL_B, _REAL_C, _ADOPTED], daily_pnl=drifted)
        assert reconcile(db)["reconciled"] is False         # drift present
        out = rebuild_daily_pnl(db)
        assert out["drift_after"] == 0.0 and out["reconciled_after"] is True
        assert out["fiction_days_zeroed"] >= 1              # 06-18 zeroed
        # ledger now equals the real book (20), not the fiction
        assert reconcile(db)["ledger_realized"] == 20.0
        assert reconcile(db)["reconciled"] is True

    def test_rebuild_is_idempotent(self, tmp_path):
        db = _db(tmp_path, [_REAL_A, _REAL_B], daily_pnl=[("2026-06-20", 0.0, 0.0, 0)])
        rebuild_daily_pnl(db)
        second = rebuild_daily_pnl(db)
        assert second["drift_after"] == 0.0 and reconcile(db)["reconciled"] is True

    def test_real_realized_by_day_excludes_fiction(self, tmp_path):
        db = _db(tmp_path, [_REAL_A, _REAL_B, _ADOPTED, _RECON])
        with sqlite3.connect(db) as conn:
            by_day = real_realized_by_day(conn)
        assert by_day == {"2026-06-20": 50.0}   # adopted/reconcile excluded; only the real day


class TestEmptyAndErrorSafety:
    def test_empty_book_is_clean(self, tmp_path):
        db = _db(tmp_path, [])
        b = canonical_book(db)
        assert b["real_strategy"]["net_realized"] == 0.0 and b["partition_ok"] is True
        assert reconcile(db)["reconciled"] is True

    def test_reads_never_raise_on_bad_path(self):
        assert reconcile("/nonexistent/x.db").get("status") == "ERROR"
        assert "error" in canonical_book("/nonexistent/x.db")

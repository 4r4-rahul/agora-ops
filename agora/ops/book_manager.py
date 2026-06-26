"""
agora/ops/book_manager.py — the SINGLE SOURCE OF TRUTH for the engine's money numbers.

Every surface (UI, daily-loss breaker, perf snapshot, reports) must read the SAME numbers from here,
derived ONE way from the authoritative `positions` table via the `_REAL_CLOSE` predicate. The
recurring "DB vs UI mismatch" had three roots, all addressed here:

  1. Two independent P&L writers. The circuit breaker froze a `daily_pnl` row each cycle, but after
     `clean_book_fiction` corrected the `positions` table the stale rows were never re-derived → a
     permanent reconciliation drift (−$425 across 8 days; 06-17/06-18 held P&L with NO real close
     behind them). FIX: `daily_pnl` is now a DERIVED PROJECTION of `positions` — `rebuild_daily_pnl`
     re-derives it at any time, and `reconcile()` proves they agree.

  2. Naïve `SUM(realized_pnl)` over all rows (−$22,876) vs the real `_REAL_CLOSE` total (−$5,359) —
     a $17.5k gap of fiction. FIX: `canonical_book` exposes ONLY the real number as headline, and
     buckets the excluded dollars by cause so nothing is hidden.

  3. No separation of execution-bug losses from strategy losses. FIX: `excluded_attribution` tags
     every non-real closed dollar to its cause (adopted legacy, reconciliation/over-fill artifact,
     fabricated/test) so "real strategy P&L" is cleanly separable from "bug/adoption impact".

This module is READ-MOSTLY. The one mutation, `rebuild_daily_pnl`, only re-derives the `daily_pnl`
ledger to match the book — it never touches `positions` (the source of truth) and never fabricates.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

from agora.ops.edge_dashboard import _REAL_CLOSE

# A closed row is EXCLUDED from real strategy P&L for one of these causes. Mirrors the inverse of
# _REAL_CLOSE so the two partitions are exhaustive: every closed row is either real or attributed.
_EXCLUDED_BUCKETS: dict[str, str] = {
    "adopted_legacy": "COALESCE(regime_at_entry,'') = 'adopted'",
    "reconcile_artifact": "(close_source LIKE '%tws_startup_sync%' OR close_source LIKE '%reconcile%' "
                          "OR close_source LIKE '%duplicate%')",
    "fabricated_test": "close_source LIKE '%fabricated%'",
}


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> float:
    row = conn.execute(sql, params).fetchone()
    return float(row[0]) if row and row[0] is not None else 0.0


def real_realized_by_day(conn: sqlite3.Connection) -> dict[str, float]:
    """{YYYY-MM-DD: realized} from REAL fills only (the authoritative per-day strategy P&L)."""
    rows = conn.execute(
        f"SELECT substr(close_date,1,10) d, ROUND(SUM(realized_pnl),2) "
        f"FROM positions WHERE {_REAL_CLOSE} GROUP BY d"
    ).fetchall()
    return {d: float(v or 0.0) for d, v in rows if d}


def reconcile(db_path: str) -> dict[str, Any]:
    """Prove the daily_pnl ledger's realized == the book's real fills. drift≈0 ⇒ reconciled.

    This is the canonical DB-internal reconciliation (the `recon_ok` the perf snapshot reports).
    It does NOT mutate; it only measures. `rebuild_daily_pnl` is what makes it pass."""
    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            ledger = _scalar(conn, "SELECT COALESCE(SUM(realized_pnl),0) FROM daily_pnl")
            book = _scalar(conn, f"SELECT COALESCE(SUM(realized_pnl),0) FROM positions WHERE {_REAL_CLOSE}")
        drift = round(ledger - book, 2)
        return {
            "ledger_realized": round(ledger, 2),
            "book_real_realized": round(book, 2),
            "drift": drift,
            "reconciled": abs(drift) < 1.0,
            "status": "OK" if abs(drift) < 1.0 else "DIVERGENCE",
        }
    except Exception as exc:  # never raise from a read path
        return {"error": str(exc), "reconciled": False, "status": "ERROR"}


def rebuild_daily_pnl(db_path: str) -> dict[str, Any]:
    """Re-derive `daily_pnl.realized_pnl` for every day from the authoritative `positions` (_REAL_CLOSE)
    so the ledger always reconciles to the book.

      • a day WITH real closes  → realized set to the real per-day sum (unrealized/trades preserved)
      • a day in the ledger with NO real close → realized zeroed (it held fiction)

    Idempotent. Never touches `positions`. Returns before/after drift + counts. Call this after any
    book correction (clean_book_fiction, restatement) and on a schedule so drift can never persist."""
    before = reconcile(db_path)
    updated = zeroed = 0
    try:
        with sqlite3.connect(db_path, timeout=20) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            real = real_realized_by_day(conn)
            existing = {r[0] for r in conn.execute("SELECT record_date FROM daily_pnl").fetchall()}
            # 1. Set realized for every real-close day (ON CONFLICT updates ONLY realized_pnl, so the
            #    transient intraday unrealized/trades columns are untouched).
            for d, v in real.items():
                conn.execute(
                    "INSERT INTO daily_pnl(record_date, realized_pnl, unrealized_pnl, trades_count) "
                    "VALUES (?,?,0,0) ON CONFLICT(record_date) DO UPDATE SET realized_pnl=excluded.realized_pnl",
                    (d, round(v, 2)),
                )
                updated += 1
            # 2. Zero any ledger day with no real close behind it (pure fiction rows).
            for d in existing - set(real):
                cur = conn.execute(
                    "UPDATE daily_pnl SET realized_pnl=0 WHERE record_date=? AND realized_pnl<>0", (d,)
                )
                zeroed += cur.rowcount
            conn.commit()
    except Exception as exc:
        return {"error": str(exc), "before": before}
    after = reconcile(db_path)
    return {
        "days_set": updated,
        "fiction_days_zeroed": zeroed,
        "drift_before": before.get("drift"),
        "drift_after": after.get("drift"),
        "reconciled_after": after.get("reconciled"),
    }


def excluded_attribution(conn: sqlite3.Connection) -> dict[str, Any]:
    """Partition every CLOSED dollar that is NOT real strategy P&L into EXACTLY ONE cause, by
    priority, so the partition is exhaustive and non-overlapping: real + Σexcluded == the naïve
    all-closed sum, to the penny. Causes: adopted legacy, reconciliation/over-fill artifact
    (= execution-bug cleanup), fabricated/test, and a catch-all `other_excluded` so no dollar is lost.
    """
    # One CASE assigns each non-real closed row to a single bucket (first match wins).
    case = (
        "CASE "
        f"  WHEN {_REAL_CLOSE} THEN '__real__' "
        f"  WHEN {_EXCLUDED_BUCKETS['adopted_legacy']} THEN 'adopted_legacy' "
        f"  WHEN {_EXCLUDED_BUCKETS['reconcile_artifact']} THEN 'reconcile_artifact' "
        f"  WHEN {_EXCLUDED_BUCKETS['fabricated_test']} THEN 'fabricated_test' "
        "  ELSE 'other_excluded' END"
    )
    rows = conn.execute(
        f"SELECT {case} bucket, ROUND(SUM(realized_pnl),2), COUNT(*) "
        f"FROM positions WHERE status='closed' GROUP BY bucket"
    ).fetchall()
    out: dict[str, Any] = {}
    for bucket, pnl, n in rows:
        if bucket == "__real__":
            continue   # the real partition is reported under real_strategy, not here
        out[bucket] = {"pnl": round(float(pnl or 0.0), 2), "n": int(n)}
    # ensure every bucket key is present even when empty, for a stable UI shape
    for name in (*_EXCLUDED_BUCKETS.keys(), "other_excluded"):
        out.setdefault(name, {"pnl": 0.0, "n": 0})
    return out


def canonical_book(db_path: str) -> dict[str, Any]:
    """THE authoritative money snapshot every surface must read. Real strategy P&L is the headline;
    excluded/fiction is shown separately and attributed; reconciliation status is always included."""
    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            real_net = _scalar(conn, f"SELECT COALESCE(SUM(realized_pnl),0) FROM positions WHERE {_REAL_CLOSE}")
            real_n = int(_scalar(conn, f"SELECT COUNT(*) FROM positions WHERE {_REAL_CLOSE}"))
            wins = int(_scalar(conn, f"SELECT COUNT(*) FROM positions WHERE {_REAL_CLOSE} AND realized_pnl>0"))
            losses = int(_scalar(conn, f"SELECT COUNT(*) FROM positions WHERE {_REAL_CLOSE} AND realized_pnl<0"))
            gross_win = _scalar(conn, f"SELECT COALESCE(SUM(realized_pnl),0) FROM positions WHERE {_REAL_CLOSE} AND realized_pnl>0")
            gross_loss = _scalar(conn, f"SELECT COALESCE(SUM(realized_pnl),0) FROM positions WHERE {_REAL_CLOSE} AND realized_pnl<0")
            naive_all = _scalar(conn, "SELECT COALESCE(SUM(realized_pnl),0) FROM positions WHERE status='closed'")
            open_n = int(_scalar(conn, "SELECT COUNT(*) FROM positions WHERE status IN ('open','tested','rolled')"))
            open_unreal = _scalar(conn, "SELECT COALESCE(SUM(unrealized_pnl),0) FROM positions WHERE status IN ('open','tested','rolled')")
            excluded = excluded_attribution(conn)
        win_rate = round(wins / real_n, 4) if real_n else 0.0
        expectancy = round(real_net / real_n, 2) if real_n else 0.0
        profit_factor = round(gross_win / abs(gross_loss), 2) if gross_loss else None
        excluded_total = round(sum(b["pnl"] for b in excluded.values()), 2)
        # INVARIANT (every-penny-accounted): real + Σexcluded must equal the naïve all-closed sum.
        partition_residual = round(naive_all - (real_net + excluded_total), 2)
        return {
            "real_strategy": {
                "net_realized": round(real_net, 2),
                "n_closed": real_n, "wins": wins, "losses": losses,
                "win_rate": win_rate, "expectancy": expectancy, "profit_factor": profit_factor,
                "open_positions": open_n, "open_unrealized": round(open_unreal, 2),
            },
            "excluded": {**excluded, "total": excluded_total},
            "naive_all_closed": round(naive_all, 2),   # what a fiction-blind query would show
            "partition_ok": abs(partition_residual) < 0.01,
            "partition_residual": partition_residual,   # must be 0.00 — every closed dollar attributed
            "reconciliation": reconcile(db_path),
            "computed_at_utc": datetime.now(tz=UTC).isoformat(),
        }
    except Exception as exc:
        return {"error": str(exc)}


if __name__ == "__main__":   # pragma: no cover — ad-hoc: python -m agora.ops.book_manager [db]
    import json
    import sys
    _db = sys.argv[1] if len(sys.argv) > 1 else ".agora/agora.db"
    print(json.dumps(canonical_book(_db), indent=2))

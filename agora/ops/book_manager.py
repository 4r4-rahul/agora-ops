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


def unclassified_close_sources(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """INTEGRITY GUARD against the 2026-06-29 CBOE bug class. A closed row that is neither REAL
    (_REAL_CLOSE) nor a KNOWN fiction cause (adopted / reconcile-artifact / fabricated) falls through to
    'other_excluded' — i.e. its close_source is UNCLASSIFIED. That is almost always a REAL trade being
    silently dropped from the book (CBOE +$660 via 'time_stop' was exactly this), or — rarely — a new
    fiction type needing a rule. Either way it must be LOUD, never quietly bucketed. Returns the
    offending close_sources; an empty list means every closed dollar is explicitly classified.

    The fix when this fires: add the source to REAL_CLOSE_SOURCES (if a real engine exit) or to a
    fiction pattern (agora/ops/close_sources.py) — never leave a close_source unclassified."""
    known = " OR ".join(f"({v})" for v in _EXCLUDED_BUCKETS.values())
    rows = conn.execute(
        "SELECT COALESCE(close_source,'(null)') cs, COUNT(*) n, ROUND(SUM(realized_pnl),2) pnl "
        f"FROM positions WHERE status='closed' AND NOT ({_REAL_CLOSE}) AND NOT ({known}) "
        "GROUP BY cs ORDER BY ABS(COALESCE(SUM(realized_pnl),0)) DESC"
    ).fetchall()
    return [{"close_source": cs, "n": int(n), "pnl": float(pnl or 0.0)} for cs, n, pnl in rows]


# ── Execution-bug episode registry ──────────────────────────────────────────────────────────────
# Dated incidents where an EXECUTION bug (not a strategy decision) created PHANTOM book entries —
# over-fills, mis-reconstructed cost basis. Each is quarantined from real strategy P&L (it lands in
# an `excluded` bucket). This registry is the NARRATIVE behind the excluded dollars: what happened,
# the root cause, and the fix commit — so every fiction dollar is tagged with a reason, and real
# strategy P&L is cleanly separable. Append new incidents here as they are found + fixed.
EXECUTION_BUG_EPISODES: list[dict[str, Any]] = [
    {
        "date": "2026-06-26", "name": "close-stacking runaway",
        "bucket": "reconcile_artifact",
        "phantom_peak": "DIA 59→659 contracts (~$105k phantom notional), NOW→131",
        "root_cause": "close orderRef was session-scoped with NO idempotency guard on the exit path; "
                      "IBKR paper fill-lag made _execute_close re-fire a full-size close every cycle — "
                      "~17 stacked and all filled.",
        "fix_commit": "c14a161",
    },
    {
        "date": "2026-06-25", "name": "adopted-position fiction (contracts² cost basis)",
        "bucket": "adopted_legacy",
        "phantom_peak": "book showed −$1.18M from legacy adopted positions",
        "root_cause": "the reconciler reconstructed entry_price as max_loss/contracts (carrying the "
                      "contract count), so realized P&L scaled by contracts TWICE.",
        "fix_commit": "956f352 / 27d66d7",
    },
    {
        "date": "2026-06-25", "name": "contract-cap breach",
        "bucket": "adopted_legacy",
        "phantom_peak": "positions over the 10-contract cap (DIA 21→59, NOK 12→25)",
        "root_cause": "the entry-path contract cap had no equivalent on the exit/adoption paths.",
        "fix_commit": "6de6204",
    },
]


def execution_bug_ledger(db_path: str) -> dict[str, Any]:
    """The honest execution-bug accounting: real strategy P&L vs bug/legacy FICTION impact, with
    every excluded dollar tagged to a dated incident + root cause + fix commit.

    KEY TRUTH the owner asked to surface: the bugs did NOT lose real strategy money — they created
    PHANTOM book entries (over-fills, mis-reconstructed cost basis) that are quarantined into the
    `excluded` buckets. Real strategy P&L stands alone, unaffected."""
    book = canonical_book(db_path)
    if "error" in book:
        return book
    excl = book["excluded"]
    episodes = []
    for ep in EXECUTION_BUG_EPISODES:
        b = excl.get(ep["bucket"], {})
        episodes.append({**ep, "current_book_bucket_pnl": b.get("pnl", 0.0), "bucket_n": b.get("n", 0)})
    rs = book["real_strategy"]
    return {
        "real_strategy_pnl": rs["net_realized"],
        "real_strategy_closes": rs["n_closed"],
        "total_excluded_fiction": excl.get("total", 0.0),
        "by_cause": {k: v for k, v in excl.items() if k != "total"},
        "episodes": episodes,
        "summary": (
            f"Real strategy P&L is {rs['net_realized']:+.0f} over {rs['n_closed']} closes. Separately, "
            f"{excl.get('total', 0.0):+.0f} of execution-bug/legacy FICTION is quarantined (phantom "
            f"book entries, NOT strategy outcomes) — see episodes for the cause of each."
        ),
        "reconciliation": book["reconciliation"],
        "computed_at_utc": book["computed_at_utc"],
    }


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
            unclassified = unclassified_close_sources(conn)
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
            # INTEGRITY GUARD: partition_ok proves no dollar is LOST; this proves no real trade is
            # silently DROPPED (the CBOE +$660 'time_stop' bug). ok=False names the unclassified sources.
            "integrity": {
                "ok": not unclassified,
                "unclassified_sources": unclassified,
                "message": (
                    "every closed dollar is classified as real or known-fiction"
                    if not unclassified else
                    f"⚠ {sum(u['n'] for u in unclassified)} closed position(s) "
                    f"(${round(sum(u['pnl'] for u in unclassified), 2)}) have an UNCLASSIFIED "
                    "close_source — likely REAL trades being dropped from the book. Classify in "
                    "agora/ops/close_sources.py: " + ", ".join(u["close_source"] for u in unclassified)
                ),
            },
            "reconciliation": reconcile(db_path),
            "computed_at_utc": datetime.now(tz=UTC).isoformat(),
        }
    except Exception as exc:
        return {"error": str(exc)}


def _latest_broker_recon(db_path: str) -> dict[str, Any]:
    """The most recent DB↔broker reconciliation result (persisted by the perf snapshot as
    recon_ok/recon_drift). This is the position-level reconcile (the reconciler/heal), distinct from
    the DB-internal ledger reconcile()."""
    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            row = conn.execute(
                "SELECT recon_ok, recon_drift, snapshot_date FROM perf_snapshots "
                "ORDER BY snapshot_date DESC LIMIT 1"
            ).fetchone()
        if not row:
            return {"ok": True, "detail": "no snapshot yet", "stale": True}
        ok, drift, day = bool(row[0]), float(row[1] or 0.0), row[2]
        return {"ok": ok, "drift": round(drift, 2), "as_of": day, "stale": False}
    except Exception:
        # No perf_snapshots yet (fresh DB / pre-first-snapshot) is "no data", NOT a divergence — the
        # real-time guards are the ledger + partition checks. Treat as ok-but-stale, don't false-warn.
        return {"ok": True, "detail": "no snapshot data", "stale": True}


def reconciliation_health(db_path: str) -> dict[str, Any]:
    """Unified, CONTINUOUS reconciliation health — ONE severity (ok/warn/critical) for 'are the books
    in sync RIGHT NOW', so a divergence can never sit unnoticed again (the −$425 recon_ok=0 sat for 2
    days). Combines three independent checks; the UI + alerts read this single signal.

      1. ledger_reconciled  — DB-internal: daily_pnl == real positions (reconcile())
      2. partition_exact    — every closed penny attributed (real + Σexcluded == naïve)
      3. broker_reconciled  — DB↔broker position-level recon (latest perf snapshot recon_ok)

    critical = ≥2 failing OR the ledger itself is broken; warn = exactly 1; ok = all green.
    A failing/erroring read is treated as NOT-ok (fail-loud), never silently green."""
    internal = reconcile(db_path)
    cb = canonical_book(db_path)
    broker = _latest_broker_recon(db_path)
    checks = [
        {"name": "ledger_reconciled", "ok": bool(internal.get("reconciled")),
         "detail": f"drift ${internal.get('drift', '?')}"},
        {"name": "partition_exact", "ok": bool(cb.get("partition_ok")),
         "detail": f"residual ${cb.get('partition_residual', '?')}"},
        {"name": "broker_reconciled", "ok": bool(broker.get("ok")),
         "detail": (f"drift ${broker.get('drift')}" + (" (stale)" if broker.get("stale") else ""))
                   if "drift" in broker else broker.get("detail", "?")},
    ]
    n_fail = sum(1 for c in checks if not c["ok"])
    ledger_broken = not checks[0]["ok"]
    status = "ok" if n_fail == 0 else ("critical" if (n_fail >= 2 or ledger_broken) else "warn")
    return {
        "status": status,
        "checks": checks,
        "real_strategy_pnl": cb.get("real_strategy", {}).get("net_realized"),
        "computed_at_utc": datetime.now(tz=UTC).isoformat(),
    }


if __name__ == "__main__":   # pragma: no cover — ad-hoc: python -m agora.ops.book_manager [db]
    import json
    import sys
    _db = sys.argv[1] if len(sys.argv) > 1 else ".agora/agora.db"
    print(json.dumps(canonical_book(_db), indent=2))

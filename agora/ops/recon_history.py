"""
agora/ops/recon_history.py — daily reconciliation-health history (HONESTY proof-gate backbone).

The HONESTY gate's milestone is "90 consecutive days reconciliation drift == $0.00." The reconciliation
INSTRUMENT is already live (book_manager.reconciliation_health), but proving 90 CONSECUTIVE clean days
needs a per-day history. This records one row per day (idempotent) and counts the consecutive clean
streak ending at the most recent snapshot. Read/write SQLite only; never raises out.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from typing import Any

_DDL = """
CREATE TABLE IF NOT EXISTS recon_history (
    snapshot_date TEXT PRIMARY KEY,   -- ISO date (one row/day)
    status        TEXT NOT NULL,      -- reconciliation_health status: ok | warn | critical | unknown
    real_pnl      REAL,
    recorded_at   TEXT NOT NULL
)
"""


def record_recon_snapshot(db_path: str, on_date: str | None = None) -> dict[str, Any]:
    """Record today's reconciliation status (idempotent per day — re-running overwrites today). Clean
    means status == 'ok'. Called from the daily snapshot job. Best-effort; swallows errors."""
    try:
        from agora.ops.book_manager import reconciliation_health
        rh = reconciliation_health(db_path)
        d = on_date or date.today().isoformat()
        with sqlite3.connect(db_path, timeout=10) as conn:
            conn.execute(_DDL)
            conn.execute(
                "INSERT INTO recon_history (snapshot_date, status, real_pnl, recorded_at) "
                "VALUES (?,?,?,?) ON CONFLICT(snapshot_date) DO UPDATE SET "
                "status=excluded.status, real_pnl=excluded.real_pnl, recorded_at=excluded.recorded_at",
                (d, str(rh.get("status", "unknown")), rh.get("real_strategy_pnl"),
                 datetime.now(UTC).isoformat()),
            )
            conn.commit()
        return {"date": d, "status": rh.get("status")}
    except Exception as exc:
        return {"error": str(exc)}


def consecutive_clean_days(db_path: str) -> int:
    """Consecutive days with status=='ok', counting back from the most recent snapshot. Breaks on the
    first non-'ok' day. (Snapshots are recorded on active days; calendar gaps are not penalized.)"""
    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            conn.execute(_DDL)
            rows = conn.execute(
                "SELECT status FROM recon_history ORDER BY snapshot_date DESC").fetchall()
    except Exception:
        return 0
    count = 0
    for (status,) in rows:
        if status != "ok":
            break
        count += 1
    return count

"""
agora/ops/incident_log.py — safety-incident history backing the DISASTER proof-gate.

The DISASTER milestone is "90 consecutive incident-free days + a daily adversarial chaos-suite proving
every guard fires." This records every AUTOMATIC safety trip (the kill-switch chokepoint: over-fill
auto-halt, daily-loss breaker, per-position 2x trip) and computes the incident-free streak from a
watch-start baseline. The chaos-suite half is proven continuously by smoke_runaway_defense in CI.
SQLite only; never raises out.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from typing import Any

_WATCH = "__watch_start__"

_DDL = """
CREATE TABLE IF NOT EXISTS safety_incidents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,   -- ISO datetime
    kind        TEXT NOT NULL,   -- e.g. kill_switch_auto | over_fill | contract_cap | __watch_start__
    detail      TEXT
)
"""


def ensure_watching(db_path: str) -> None:
    """Stamp the watch-start baseline ONCE (so 'incident-free days' has a clean origin even before any
    incident occurs). Idempotent; called from the daily snapshot."""
    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            conn.execute(_DDL)
            has = conn.execute("SELECT 1 FROM safety_incidents WHERE kind=? LIMIT 1", (_WATCH,)).fetchone()
            if not has:
                conn.execute("INSERT INTO safety_incidents (occurred_at, kind, detail) VALUES (?,?,?)",
                             (datetime.now(UTC).isoformat(), _WATCH, "incident tracking started"))
                conn.commit()
    except Exception:
        pass


def record_incident(db_path: str, kind: str, detail: str = "") -> None:
    """Record a real safety incident (an automatic protective trip). Best-effort, fail-open — a logging
    failure must NEVER interfere with the safety action itself."""
    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            conn.execute(_DDL)
            conn.execute("INSERT INTO safety_incidents (occurred_at, kind, detail) VALUES (?,?,?)",
                         (datetime.now(UTC).isoformat(), kind, detail[:500]))
            conn.commit()
    except Exception:
        pass


def incident_free_days(db_path: str) -> int:
    """Days since the most recent REAL incident, or since watch-start if none. 0 if not watching yet."""
    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            conn.execute(_DDL)
            real = conn.execute(
                "SELECT MAX(occurred_at) FROM safety_incidents WHERE kind<>?", (_WATCH,)).fetchone()[0]
            start = conn.execute(
                "SELECT MIN(occurred_at) FROM safety_incidents WHERE kind=?", (_WATCH,)).fetchone()[0]
    except Exception:
        return 0
    base = real or start
    if not base:
        return 0
    try:
        base_d = datetime.fromisoformat(base).date()
    except Exception:
        return 0
    return max(0, (date.today() - base_d).days)


def incident_count(db_path: str) -> int:
    """Number of real incidents recorded since watch-start (excludes the watch-start sentinel)."""
    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            conn.execute(_DDL)
            return int(conn.execute(
                "SELECT COUNT(*) FROM safety_incidents WHERE kind<>?", (_WATCH,)).fetchone()[0])
    except Exception:
        return 0


def status(db_path: str) -> dict[str, Any]:
    return {"incident_free_days": incident_free_days(db_path), "incident_count": incident_count(db_path)}

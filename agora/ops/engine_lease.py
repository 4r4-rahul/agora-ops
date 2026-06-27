"""
agora/ops/engine_lease.py — in-process single-engine lease (disaster-audit GAP-4, HARDEN-3b).

"One engine per IBKR account" was enforced ONLY by the launchd shell flock (start.sh/watchdog.sh). A
manual `uvicorn` launch or a launchd race outside the lock window could bind a SECOND engine to the same
IBKR paper account — two engines on one account = double-fire on orders (the kind of thing that produced
the close-stacking runaway). Nothing in Python refused it; it surfaced only as IBKR Error 326 churn.

This is DEFENSE-IN-DEPTH behind the shell flock: a DB-based lease (the SQLite DB is the shared resource
both engines would touch). On startup the engine refuses to run if a DIFFERENT, LIVE, FRESH-heartbeat
engine already holds the lease for the same IBKR slot. A crashed engine never locks us out — a stale
heartbeat OR a dead pid lets the next engine take over.

Keyed per-ENGINE-INSTANCE on host:port:client_id (the MAIN ibkr_client_id) — NOT per-clientId. The
news / portfolio / startup-sync / bridge pollers legitimately use OTHER clientIds; they belong to the
one engine that holds the lease and must never acquire their own.

stdlib only (sqlite3 + os + socket + datetime). Tests use a tmp DB; production calls live only in the
app lifespan, so the unit suite never trips a real lease.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime
from typing import Any

STALE_SECS = 90       # a heartbeat older than this ⇒ the holder is presumed gone (takeover allowed).
HEARTBEAT_SECS = 30   # refresh cadence (the caller drives this; must be < STALE_SECS with margin).


class LeaseHeldError(RuntimeError):
    """Raised when a different, live, fresh-heartbeat engine already holds the lease."""


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _pid_alive(pid: int) -> bool:
    """True if a process with this pid exists. os.kill(pid, 0) raises ProcessLookupError if dead,
    PermissionError if alive but not ours (still alive)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS engine_lease (
               lease_key    TEXT PRIMARY KEY,
               pid          INTEGER NOT NULL,
               host         TEXT NOT NULL,
               client_id    INTEGER NOT NULL,
               acquired_at  TEXT NOT NULL,
               heartbeat_at TEXT NOT NULL
           )"""
    )


def _heartbeat_age(hb: str) -> float:
    try:
        return (_now() - datetime.fromisoformat(hb)).total_seconds()
    except Exception:
        return float("inf")   # unparseable ⇒ treat as stale (safe: allows takeover)


def acquire_lease(db_path: str, lease_key: str, pid: int, host: str, client_id: int,
                  stale_secs: int = STALE_SECS) -> None:
    """Acquire the lease for lease_key, or raise LeaseHeldError if a DIFFERENT, LIVE, FRESH-heartbeat
    engine holds it. Takes over a stale-heartbeat OR dead-pid holder (so a crash never locks us out).
    The check-and-set is serialized with BEGIN IMMEDIATE."""
    now = _now().isoformat()
    with sqlite3.connect(db_path, timeout=10) as conn:
        _ensure_table(conn)
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT pid, heartbeat_at FROM engine_lease WHERE lease_key=?", (lease_key,)
        ).fetchone()
        if row is not None:
            held_pid, hb = int(row[0]), row[1]
            if held_pid != pid and _pid_alive(held_pid) and _heartbeat_age(hb) < stale_secs:
                conn.rollback()
                raise LeaseHeldError(
                    f"engine already running (pid={held_pid}, heartbeat {_heartbeat_age(hb):.0f}s ago) "
                    f"for lease '{lease_key}' — refusing to start a second engine on the same account")
        conn.execute(
            "INSERT OR REPLACE INTO engine_lease (lease_key, pid, host, client_id, acquired_at, "
            "heartbeat_at) VALUES (?,?,?,?,?,?)", (lease_key, pid, host, client_id, now, now))
        conn.commit()


def refresh_lease(db_path: str, lease_key: str, pid: int) -> None:
    """Refresh our heartbeat. Only updates the row if WE still hold it — so we never clobber a takeover
    that already decided we were gone. Best-effort (swallows errors; a missed beat just ages the lease)."""
    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            _ensure_table(conn)
            conn.execute("UPDATE engine_lease SET heartbeat_at=? WHERE lease_key=? AND pid=?",
                         (_now().isoformat(), lease_key, pid))
            conn.commit()
    except Exception:
        pass


def release_lease(db_path: str, lease_key: str, pid: int) -> None:
    """Release our lease on clean shutdown. Only deletes if WE hold it. Best-effort (a kill -9 leaves
    the row; the next engine takes over via stale/dead-pid detection)."""
    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            _ensure_table(conn)
            conn.execute("DELETE FROM engine_lease WHERE lease_key=? AND pid=?", (lease_key, pid))
            conn.commit()
    except Exception:
        pass


def lease_status(db_path: str, lease_key: str) -> dict[str, Any]:
    """Read-only snapshot for /agora/health. Never raises."""
    try:
        with sqlite3.connect(db_path, timeout=5) as conn:
            _ensure_table(conn)
            row = conn.execute(
                "SELECT pid, host, client_id, heartbeat_at FROM engine_lease WHERE lease_key=?",
                (lease_key,)).fetchone()
        if row is None:
            return {"held": False, "lease_key": lease_key}
        held_pid = int(row[0])
        age = _heartbeat_age(row[3])
        return {
            "held": True, "lease_key": lease_key, "holder_pid": held_pid,
            "my_pid": os.getpid(), "is_owner": held_pid == os.getpid(),
            "host": row[1], "client_id": int(row[2]),
            "heartbeat_age_s": round(age, 1), "stale": age >= STALE_SECS,
        }
    except Exception as exc:
        return {"status": "unknown", "error": str(exc)}

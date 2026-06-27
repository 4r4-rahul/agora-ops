"""
HARDEN-3b (GAP-4) — in-process single-engine lease.

Prevents two engines on one IBKR account (double-fire). Defense-in-depth behind the shell flock. These
lock the contract: acquire on empty, re-acquire by self, REFUSE a different live+fresh engine, TAKE OVER
a stale-heartbeat or dead-pid holder (so a crash never locks us out), and release only our own row.
"""
import os
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from agora.ops.engine_lease import (
    LeaseHeldError,
    _ensure_table,
    acquire_lease,
    lease_status,
    refresh_lease,
    release_lease,
)

KEY = "127.0.0.1:7497:10"


def _db(tmp_path):
    return str(tmp_path / "lease.db")


def _seed(db, pid, hb_age_s):
    with sqlite3.connect(db) as c:
        _ensure_table(c)
        now = datetime.now(UTC)
        hb = (now - timedelta(seconds=hb_age_s)).isoformat()
        c.execute("INSERT OR REPLACE INTO engine_lease VALUES (?,?,?,?,?,?)",
                  (KEY, pid, "host", 10, now.isoformat(), hb))
        c.commit()


class TestEngineLease:
    def test_acquire_on_empty(self, tmp_path):
        db = _db(tmp_path)
        acquire_lease(db, KEY, os.getpid(), "host", 10)
        assert lease_status(db, KEY)["held"] is True

    def test_same_pid_reacquire_ok(self, tmp_path):
        db = _db(tmp_path)
        acquire_lease(db, KEY, os.getpid(), "host", 10)
        acquire_lease(db, KEY, os.getpid(), "host", 10)   # must not raise

    def test_refuses_second_live_fresh(self, tmp_path):
        db = _db(tmp_path)
        _seed(db, os.getpid(), hb_age_s=1)                # held by a LIVE pid (me), fresh heartbeat
        with pytest.raises(LeaseHeldError):
            acquire_lease(db, KEY, 999_999, "host", 10)   # a different engine trying to start

    def test_takes_over_stale_heartbeat(self, tmp_path):
        db = _db(tmp_path)
        _seed(db, os.getpid(), hb_age_s=300)              # alive but STALE (> 90s)
        acquire_lease(db, KEY, 4242, "host", 10)          # takeover — no raise
        assert lease_status(db, KEY)["holder_pid"] == 4242

    def test_takes_over_dead_pid(self, tmp_path):
        db = _db(tmp_path)
        _seed(db, 2_000_000_000, hb_age_s=1)             # fresh heartbeat but DEAD pid
        acquire_lease(db, KEY, 4242, "host", 10)          # takeover — no raise
        assert lease_status(db, KEY)["holder_pid"] == 4242

    def test_release_removes_only_own_row(self, tmp_path):
        db = _db(tmp_path)
        acquire_lease(db, KEY, os.getpid(), "host", 10)
        release_lease(db, KEY, os.getpid())
        assert lease_status(db, KEY)["held"] is False

    def test_release_ignores_others(self, tmp_path):
        db = _db(tmp_path)
        _seed(db, 4242, hb_age_s=1)
        release_lease(db, KEY, os.getpid())               # not our row → no-op
        assert lease_status(db, KEY)["held"] is True

    def test_refresh_updates_heartbeat(self, tmp_path):
        db = _db(tmp_path)
        _seed(db, os.getpid(), hb_age_s=50)
        refresh_lease(db, KEY, os.getpid())
        assert lease_status(db, KEY)["heartbeat_age_s"] < 5

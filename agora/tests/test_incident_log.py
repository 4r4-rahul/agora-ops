"""
incident_log — safety-incident history backing the DISASTER proof-gate (incident-free streak).
Locks: watch-start baseline, an auto-trip resets the streak, and the gate greens only at 90+ free days.
"""
import sqlite3
from datetime import UTC, datetime, timedelta

from agora.ops.incident_log import (
    _DDL,
    ensure_watching,
    incident_count,
    incident_free_days,
    record_incident,
)


def _seed_at(db, kind, days_ago):
    ts = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    with sqlite3.connect(db) as c:
        c.execute(_DDL)
        c.execute("INSERT INTO safety_incidents (occurred_at, kind, detail) VALUES (?,?,?)", (ts, kind, ""))
        c.commit()


class TestIncidentLog:
    def test_no_watch_no_incidents_is_zero(self, tmp_path):
        assert incident_free_days(str(tmp_path / "e.db")) == 0

    def test_watch_start_gives_clean_streak(self, tmp_path):
        db = str(tmp_path / "w.db")
        _seed_at(db, "__watch_start__", days_ago=100)
        assert incident_free_days(db) == 100      # 100 clean days since watching began
        assert incident_count(db) == 0

    def test_real_incident_resets_streak(self, tmp_path):
        db = str(tmp_path / "r.db")
        _seed_at(db, "__watch_start__", days_ago=100)
        _seed_at(db, "kill_switch_auto", days_ago=3)   # an auto-trip 3 days ago
        assert incident_free_days(db) == 3             # streak now measured from the incident
        assert incident_count(db) == 1

    def test_ensure_watching_idempotent(self, tmp_path):
        db = str(tmp_path / "i.db")
        ensure_watching(db); ensure_watching(db)
        with sqlite3.connect(db) as c:
            n = c.execute("SELECT COUNT(*) FROM safety_incidents WHERE kind='__watch_start__'").fetchone()[0]
        assert n == 1

    def test_record_incident_counts(self, tmp_path):
        db = str(tmp_path / "c.db")
        ensure_watching(db)
        record_incident(db, "over_fill", "DIA 59->631")
        assert incident_count(db) == 1


class TestDisasterGateUsesStreak:
    def test_green_at_90_free_days(self, tmp_path):
        db = str(tmp_path / "g.db")
        _seed_at(db, "__watch_start__", days_ago=95)
        from agora.ops.proof_gates import GREEN, _disaster_gate
        g = _disaster_gate(db)
        assert g["current"]["incident_free_days"] >= 90
        assert g["status"] == GREEN

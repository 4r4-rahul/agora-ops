"""Unit tests for agora/ops/discord_commander.py (non-network parts)"""
import json
import sqlite3
import tempfile

from agora.ops.discord_commander import (
    _INBOX_FILE,
    _OUTBOX_FILE,
    _approve_lesson,
    _fmt_lessons,
    _reject_lesson,
    _write_inbox,
    read_inbox,
    write_outbox,
)

# ── DB helpers ────────────────────────────────────────────────────────────────

def _make_db_with_lessons(n: int = 3) -> str:
    tmp = tempfile.mktemp(suffix=".db")
    with sqlite3.connect(tmp) as conn:
        # Match the PRODUCTION agent_lessons schema that discord_commander.py and the API
        # query (lesson_id / confidence_in_lesson / human_approved / active / created_at_utc).
        conn.execute("""CREATE TABLE agent_lessons (
            lesson_id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_name TEXT,
            lesson_text TEXT,
            confidence_in_lesson REAL,
            human_approved INTEGER,
            approved_at_utc TEXT,
            rejected_at_utc TEXT,
            active INTEGER DEFAULT 1,
            created_at_utc TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        for i in range(n):
            conn.execute(
                "INSERT INTO agent_lessons "
                "(agent_name, lesson_text, confidence_in_lesson, human_approved, active) "
                "VALUES (?,?,?,NULL,1)",
                (f"agent_{i}", f"Lesson text number {i}.", 0.75),
            )
    return tmp


# ── Inbox / outbox ────────────────────────────────────────────────────────────

class TestInboxOutbox:
    def setup_method(self):
        _INBOX_FILE.unlink(missing_ok=True)
        _OUTBOX_FILE.unlink(missing_ok=True)

    def teardown_method(self):
        _INBOX_FILE.unlink(missing_ok=True)
        _OUTBOX_FILE.unlink(missing_ok=True)

    def test_write_then_read_inbox(self):
        _write_inbox("add cost widget to dashboard")
        result = read_inbox()
        assert result is not None
        assert result["instruction"] == "add cost widget to dashboard"
        assert result["from"] == "discord"

    def test_read_inbox_marks_as_read(self):
        _write_inbox("test instruction")
        read_inbox()
        # Second read should return None (already read)
        assert read_inbox() is None

    def test_read_inbox_none_when_missing(self):
        assert read_inbox() is None

    def test_write_outbox_then_read(self):
        write_outbox("I've added the cost widget. Commit abc123.")
        data = json.loads(_OUTBOX_FILE.read_text())
        assert data["reply"] == "I've added the cost widget. Commit abc123."
        assert not data["sent"]

    def test_outbox_marked_sent_idempotent(self):
        write_outbox("Done.")
        # Manually mark sent and verify second write_outbox resets it
        data = json.loads(_OUTBOX_FILE.read_text())
        data["sent"] = True
        _OUTBOX_FILE.write_text(json.dumps(data))
        # Overwrite with fresh
        write_outbox("Done v2.")
        data2 = json.loads(_OUTBOX_FILE.read_text())
        assert data2["reply"] == "Done v2."
        assert not data2["sent"]


# ── Lesson DB operations ──────────────────────────────────────────────────────

class TestLessonOperations:
    def test_fmt_lessons_no_pending(self):
        db = _make_db_with_lessons(0)
        result = _fmt_lessons(db)
        assert "No pending lessons" in result

    def test_fmt_lessons_shows_pending(self):
        db = _make_db_with_lessons(2)
        result = _fmt_lessons(db)
        assert "Pending Lessons" in result
        assert "Lesson text number 0" in result

    def test_approve_lesson_sets_approved(self):
        db = _make_db_with_lessons(1)
        result = _approve_lesson(db, 1)
        assert "approved" in result.lower()
        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT human_approved FROM agent_lessons WHERE lesson_id=1").fetchone()
        assert row[0] == 1

    def test_reject_lesson_sets_rejected(self):
        db = _make_db_with_lessons(1)
        result = _reject_lesson(db, 1)
        assert "rejected" in result.lower()
        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT human_approved FROM agent_lessons WHERE lesson_id=1").fetchone()
        assert row[0] == 0

    def test_approve_nonexistent_lesson_no_crash(self):
        db = _make_db_with_lessons(0)
        result = _approve_lesson(db, 9999)
        # Should not crash; returns some string
        assert isinstance(result, str)


# ── Text helpers ──────────────────────────────────────────────────────────────

class TestFormatHelpers:
    def test_fmt_lessons_limits_text(self):
        db = _make_db_with_lessons(3)
        result = _fmt_lessons(db)
        # Should not be absurdly long
        assert len(result) < 5000

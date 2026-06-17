"""
test_lesson_auto_approve.py — bounded §17 relaxation: auto-approve high-evidence pending lessons.

Unblocks the learning loop (451 pending, 10 approved). Only PENDING active lessons clearing the
strict bar are approved (conf>=min AND (reinforced>=min OR sample>=min)); rejected/low-conf/
one-shot-no-sample stay manual. Tagged approved_by='auto'.
"""
from __future__ import annotations

import sqlite3

from agora.core.config import AgoraSettings
from agora.ops.lessons_store import auto_approve_lessons

_SCHEMA = """CREATE TABLE agent_lessons (lesson_id INTEGER PRIMARY KEY AUTOINCREMENT, agent_name TEXT,
  lesson_text TEXT, derived_from_chain_ids TEXT, confidence_in_lesson REAL, sample_size INTEGER,
  created_at_utc TEXT, last_reinforced_at_utc TEXT, times_reinforced INTEGER, active INTEGER,
  human_approved INTEGER, approved_at_utc TEXT, approved_by TEXT, rejected_at_utc TEXT,
  rejected_reason TEXT)"""


def _db(tmp_path):
    p = str(tmp_path / "l.db")
    c = sqlite3.connect(p)
    c.execute(_SCHEMA)
    rows = [
        ("big_sample", 0.94, 1, 30, None),     # qualifies via sample>=25
        ("reinforced", 0.90, 3, 0, None),      # qualifies via reinforced>=2
        ("oneshot_nosample", 0.95, 1, 0, None),# one-shot, no sample -> manual
        ("low_conf", 0.60, 5, 50, None),       # conf<0.85 -> no
        ("rejected", 0.99, 9, 99, 0),          # not pending -> no
        ("already_approved", 0.99, 9, 99, 1),  # already approved -> unchanged
    ]
    for txt, conf, reinf, samp, appr in rows:
        c.execute("INSERT INTO agent_lessons (agent_name,lesson_text,confidence_in_lesson,sample_size,"
                  "created_at_utc,times_reinforced,active,human_approved) VALUES ('exit',?,?,?,'2026-06-17',?,1,?)",
                  (txt, conf, samp, reinf, appr))
    c.commit()
    return p, c


def test_auto_approves_only_high_evidence(tmp_path):
    p, c = _db(tmp_path)
    n = auto_approve_lessons(p, AgoraSettings())
    assert n == 2
    appr = {r[0] for r in c.execute(
        "SELECT lesson_text FROM agent_lessons WHERE human_approved=1 AND approved_by='auto'").fetchall()}
    assert appr == {"big_sample", "reinforced"}


def test_gate_off_approves_nothing(tmp_path):
    p, _ = _db(tmp_path)
    s = AgoraSettings(); s.lesson_auto_approve_enabled = False
    assert auto_approve_lessons(p, s) == 0


def test_idempotent(tmp_path):
    p, _ = _db(tmp_path)
    s = AgoraSettings()
    assert auto_approve_lessons(p, s) == 2
    assert auto_approve_lessons(p, s) == 0     # already approved -> nothing new


def test_never_touches_rejected(tmp_path):
    p, c = _db(tmp_path)
    auto_approve_lessons(p, AgoraSettings())
    rej = c.execute("SELECT human_approved FROM agent_lessons WHERE lesson_text='rejected'").fetchone()[0]
    assert rej == 0     # stays rejected

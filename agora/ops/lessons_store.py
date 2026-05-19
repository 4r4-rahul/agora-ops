"""
agora/ops/lessons_store.py — Approved lessons reader (§8.4 Sacred Rule §17).

Provides a single function that agents call at the start of each inference
request to retrieve the latest CEO-approved lessons. Lessons are injected
into the user payload so the LLM sees them as context — NOT into the system
prompt (which would require a cache-invalidating prefix change on every approval).

Sacred Rule §17: Only human-approved lessons (human_approved=1) are ever
returned. Unapproved, rejected, or inactive lessons are always excluded.

Design:
  - Pure SQL read — no network, no LLM.
  - Returns a capped list (max_lessons=5) ordered by times_reinforced DESC,
    confidence DESC so the most battle-tested lessons lead.
  - Returns [] if no approved lessons exist or DB is unavailable — agents
    work normally with an empty list.
  - Each call is fast (<1ms) — safe to call on every inference.
"""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)

_DEFAULT_MAX = 5   # cap to keep prompt token cost manageable


def load_approved_lessons(
    db_path: str,
    agent_name: str,
    max_lessons: int = _DEFAULT_MAX,
) -> list[str]:
    """
    Return up to max_lessons approved lesson texts for agent_name.
    agent_name: 'analyst' | 'strategy' | 'advocate' | 'exit'

    Returns [] on any error so callers always get a safe value.
    """
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                """SELECT lesson_text
                   FROM agent_lessons
                   WHERE agent_name = ?
                     AND human_approved = 1
                     AND active = 1
                   ORDER BY times_reinforced DESC, confidence_in_lesson DESC
                   LIMIT ?""",
                (agent_name, max_lessons),
            ).fetchall()
        return [r[0] for r in rows]
    except Exception as exc:
        logger.debug("lessons_store.load_approved_lessons[%s]: %s", agent_name, exc)
        return []

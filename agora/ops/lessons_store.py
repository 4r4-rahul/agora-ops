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
        lessons = [r[0] for r in rows]
        # INTO THE BLOOD: prepend the live system-performance line so EVERY agent that loads
        # lessons reasons toward positive expectancy (analyst/strategy/advocate/exit/long_options/
        # defender all call this). It leads the list so the LLM sees it first. Cheap aggregate over
        # real fills; failure-safe (omitted on any error).
        try:
            perf = _perf_context_cached(db_path)
            if perf:
                lessons = [perf] + lessons
        except Exception:
            pass
        return lessons
    except Exception as exc:
        logger.debug("lessons_store.load_approved_lessons[%s]: %s", agent_name, exc)
        return []


# Per-process cache so N agents in one cycle don't each recompute the metrics aggregate.
_PERF_CACHE: dict[str, tuple[float, str]] = {}
_PERF_TTL_SEC = 300.0


def _perf_context_cached(db_path: str) -> str:
    import time
    now = time.monotonic()
    hit = _PERF_CACHE.get(db_path)
    if hit and (now - hit[0]) < _PERF_TTL_SEC:
        return hit[1]
    from agora.ops.performance_metrics import performance_context_line
    line = performance_context_line(db_path)
    _PERF_CACHE[db_path] = (now, line)
    return line


def load_calibration_note(db_path: str, agent_name: str, min_gap: float = 0.15) -> str:
    """
    Return a one-line calibration self-awareness note for an agent's prompt when the
    outcome attributor has measured it as materially over- or under-confident
    (|predicted - actual| >= min_gap over >=5 samples). Empty string otherwise.

    This is the 'calibration haircut': rather than a crude numeric discount, we feed the
    agent its own measured accuracy so it recalibrates — an over-confident advocate
    (e.g. predicts 83% right, delivers 63%) is over-blocking and throttling entries.
    """
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                """SELECT predicted_win_rate, actual_win_rate, calibration_gap, sample_size
                   FROM calibration_log
                   WHERE agent_name = ? AND predicted_win_rate IS NOT NULL
                     AND actual_win_rate IS NOT NULL
                   ORDER BY measured_at_utc DESC LIMIT 1""",
                (agent_name,),
            ).fetchone()
        if not row:
            return ""
        pred, act, gap, n = row
        if n is None or n < 5 or gap is None or abs(gap) < min_gap:
            return ""
        if pred > act:  # over-confident — the case that throttles entries
            return (
                f"CALIBRATION FEEDBACK: over your last {int(n)} attributed decisions you were "
                f"correct {act*100:.0f}% of the time but expressed ~{pred*100:.0f}% confidence — "
                f"you have been OVER-CONFIDENT. Recalibrate: lower your stated confidence and "
                f"reserve the strongest negative verdict (BLOCK / CLOSE_NOW) for genuinely "
                f"high-severity, well-evidenced cases; when uncertain, prefer the softer call."
            )
        return (
            f"CALIBRATION FEEDBACK: over your last {int(n)} attributed decisions you were "
            f"correct {act*100:.0f}% vs ~{pred*100:.0f}% stated confidence — you have been "
            f"UNDER-CONFIDENT. You can trust strong, well-evidenced judgments more."
        )
    except Exception as exc:
        logger.debug("lessons_store.load_calibration_note[%s]: %s", agent_name, exc)
        return ""

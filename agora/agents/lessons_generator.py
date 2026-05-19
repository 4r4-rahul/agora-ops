"""
agora/agents/lessons_generator.py — LLM lesson synthesis (Phase 7, spec §8.4).

After LESSON_TRIGGER_NEW_ATTRIBUTIONS new outcome attributions, reads the
last N attributed rows from each agent's journal and asks Claude to synthesize
1-3 actionable, falsifiable lessons. Writes them to agent_lessons with
human_approved=0 — they never affect agent behavior until CEO approves them
via POST /agora/lessons/{id}/approve.

Sacred Rule §17: Lessons are advisory until explicitly approved by the CEO.
              No lesson is ever applied automatically.

Model: claude-haiku-4-5-20251001 — low-cost synthesis over structured data.
       Switches to claude-sonnet-4-6 if the sample window exceeds 25 rows
       (more nuanced reasoning needed for larger patterns).

Cost: ~$0.01–0.03 per synthesis run × 4 agents × every 10 attributions
      ≈ $0.04–0.12 per lesson generation event. Well under $1/day.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

import anthropic

from agora.ops.payload_compressor import compress_payload as _compress

logger = logging.getLogger(__name__)

_MODEL_FAST = "claude-haiku-4-5-20251001"
_MODEL_THOROUGH = "claude-sonnet-4-6"
_SAMPLE_SIZE = 20        # rows to feed per agent
_THOROUGH_THRESHOLD = 25  # use Sonnet when sample ≥ this

_SYSTEM = """You are a quantitative trading analyst reviewing a batch of options trade outcomes.
Your job: synthesize 1-3 lessons that are:
  - SPECIFIC: name a measurable trigger (e.g. "IV rank < 30", "DTE < 25")
  - ACTIONABLE: tell the agent what to do differently
  - FALSIFIABLE: future trades will prove or disprove it
  - NOVEL: not already obvious from the strategy rules

Do NOT write:
  - Vague lessons ("be more careful with volatile stocks")
  - Obvious rules already in the system ("avoid earnings risk")
  - Lessons that contradict hard-coded risk limits

Return exactly this JSON, no markdown, no prose:
{
  "lessons": [
    {
      "lesson_text": "...",
      "confidence_in_lesson": <float 0.5-0.95>,
      "pattern_observed": "brief data summary supporting this lesson"
    }
  ]
}

Return an empty array if no clear pattern emerges. Max 3 lessons."""


class LessonsGenerator:
    """
    Synthesizes agent lessons from attributed journal data.
    Called by ScheduledAttributor after LESSON_TRIGGER_NEW_ATTRIBUTIONS new rows.
    Writes pending lessons to agent_lessons — never approved automatically.
    """

    def __init__(self, db_path: str, api_key: str | None = None) -> None:
        self._db_path = db_path
        self._api_key = api_key or self._load_api_key()
        self._client  = anthropic.AsyncAnthropic(api_key=self._api_key)

    def _load_api_key(self) -> str:
        try:
            from agora.core.config import get_settings
            return get_settings().anthropic_api_key
        except Exception:
            import os
            key = os.getenv("ANTHROPIC_API_KEY", "")
            if not key:
                raise RuntimeError("ANTHROPIC_API_KEY not set and config unavailable")
            return key

    async def generate_all(self) -> dict[str, int]:
        """Generate lessons for all agents. Returns {agent: lessons_written}."""
        results: dict[str, int] = {}
        for agent in ("analyst", "strategy", "advocate", "exit"):
            try:
                n = await self._generate_for_agent(agent)
                results[agent] = n
            except Exception as exc:
                logger.warning("LessonsGenerator[%s] failed: %s", agent, exc)
                results[agent] = 0
        total = sum(results.values())
        if total > 0:
            logger.info("LessonsGenerator: %d lessons pending approval: %s", total, results)
        return results

    async def _generate_for_agent(self, agent: str) -> int:
        """Fetch recent attributed rows, call LLM, write lessons. Returns count written."""
        rows = self._fetch_sample(agent)
        if len(rows) < 5:
            logger.debug("LessonsGenerator[%s]: only %d rows — skipping", agent, len(rows))
            return 0

        model = _MODEL_THOROUGH if len(rows) >= _THOROUGH_THRESHOLD else _MODEL_FAST
        payload = self._build_payload(agent, rows)

        try:
            response = await self._client.messages.create(
                model=model,
                max_tokens=1024,
                system=_SYSTEM,
                messages=[{"role": "user", "content": _compress(payload)}],
                timeout=anthropic.Timeout(connect=30.0, read=60.0, write=30.0, pool=30.0),
            )
        except Exception as exc:
            logger.warning("LessonsGenerator[%s] API error: %s", agent, exc)
            return 0

        text_blocks = [b for b in response.content if b.type == "text"]
        raw_text = text_blocks[-1].text.strip() if text_blocks else "{}"
        if raw_text.startswith("```"):
            raw_text = raw_text.split("```")[1].lstrip("json").strip()

        try:
            raw = json.loads(raw_text)
        except json.JSONDecodeError:
            logger.warning("LessonsGenerator[%s]: invalid JSON: %s", agent, raw_text[:200])
            return 0

        lessons = raw.get("lessons", [])
        written = 0
        chain_ids = [r.get("decision_id", "") for r in rows if r.get("decision_id")]

        for lesson in lessons[:3]:   # hard cap at 3 per agent per run
            text = lesson.get("lesson_text", "").strip()
            confidence = float(lesson.get("confidence_in_lesson", 0.6))
            if not text or confidence < 0.5:
                continue
            # Dedup: skip if very similar lesson already pending/approved
            if self._is_duplicate(agent, text):
                continue
            self._write_lesson(agent, text, confidence, chain_ids, len(rows))
            written += 1
            logger.info("LessonsGenerator[%s] new lesson (conf=%.2f): %s",
                        agent, confidence, text[:100])

        return written

    def _fetch_sample(self, agent: str) -> list[dict]:
        """Fetch the most recent SAMPLE_SIZE attributed rows for one agent."""
        try:
            with sqlite3.connect(self._db_path) as conn:
                if agent == "analyst":
                    rows = conn.execute(
                        """SELECT decision_id, ticker, decided_at_utc, direction,
                                  magnitude_pct, confidence_pct, strategy_family,
                                  thesis_played_out, magnitude_realized_pct,
                                  confidence_was_calibrated, kill_conditions_json,
                                  scorecard_critique, shadow_mode
                           FROM analyst_journal
                           WHERE thesis_played_out IS NOT NULL
                           ORDER BY decided_at_utc DESC LIMIT ?""",
                        (_SAMPLE_SIZE,),
                    ).fetchall()
                    cols = ["decision_id", "ticker", "decided_at_utc", "direction",
                            "magnitude_pct", "confidence_pct", "strategy_family",
                            "thesis_played_out", "magnitude_realized_pct",
                            "confidence_was_calibrated", "kill_conditions",
                            "scorecard_critique", "shadow_mode"]
                elif agent == "strategy":
                    rows = conn.execute(
                        """SELECT decision_id, ticker, decided_at_utc, decision,
                                  strategy_type, realized_pnl, shadow_mode
                           FROM strategy_journal
                           WHERE structure_used = 1 AND realized_pnl IS NOT NULL
                           ORDER BY decided_at_utc DESC LIMIT ?""",
                        (_SAMPLE_SIZE,),
                    ).fetchall()
                    cols = ["decision_id", "ticker", "decided_at_utc", "decision",
                            "strategy_type", "realized_pnl", "shadow_mode"]
                elif agent == "advocate":
                    rows = conn.execute(
                        """SELECT decision_id, ticker, decided_at_utc, verdict,
                                  verdict_confidence, advocate_was_right, realized_pnl,
                                  failure_modes_json, shadow_mode
                           FROM advocate_journal
                           WHERE trade_taken IS NOT NULL
                           ORDER BY decided_at_utc DESC LIMIT ?""",
                        (_SAMPLE_SIZE,),
                    ).fetchall()
                    cols = ["decision_id", "ticker", "decided_at_utc", "verdict",
                            "verdict_confidence", "advocate_was_right", "realized_pnl",
                            "failure_modes", "shadow_mode"]
                elif agent == "exit":
                    rows = conn.execute(
                        """SELECT decision_id, position_id, ticker, decided_at_utc,
                                  thesis_validity, kill_condition_status, recommendation,
                                  confidence_pct, action_taken, action_quality,
                                  outcome_pnl, pnl_pct_of_max, shadow_mode
                           FROM exit_journal
                           WHERE action_quality IS NOT NULL
                           ORDER BY decided_at_utc DESC LIMIT ?""",
                        (_SAMPLE_SIZE,),
                    ).fetchall()
                    cols = ["decision_id", "position_id", "ticker", "decided_at_utc",
                            "thesis_validity", "kill_condition_status", "recommendation",
                            "confidence_pct", "action_taken", "action_quality",
                            "outcome_pnl", "pnl_pct_of_max", "shadow_mode"]
                else:
                    return []

                result = [dict(zip(cols, r)) for r in rows]
                # Parse any JSON string columns
                for row in result:
                    for key in ("kill_conditions", "failure_modes"):
                        if key in row and isinstance(row[key], str):
                            try:
                                row[key] = json.loads(row[key] or "[]")
                            except Exception:
                                pass
                return result
        except Exception as exc:
            logger.warning("LessonsGenerator._fetch_sample[%s]: %s", agent, exc)
            return []

    def _build_payload(self, agent: str, rows: list[dict]) -> dict:
        wins  = sum(1 for r in rows if (r.get("thesis_played_out") == 1
                                        or r.get("realized_pnl", 0) > 0
                                        or r.get("advocate_was_right") == 1))
        total = len(rows)
        return {
            "agent": agent,
            "sample_size": total,
            "win_rate": round(wins / total, 3) if total else None,
            "recent_outcomes": rows,
            "task": f"Synthesize lessons for the {agent} agent based on these {total} attributed outcomes.",
        }

    def _is_duplicate(self, agent: str, lesson_text: str) -> bool:
        """Rough dedup: skip if an active lesson with 50%+ word overlap exists."""
        try:
            with sqlite3.connect(self._db_path) as conn:
                existing = conn.execute(
                    "SELECT lesson_text FROM agent_lessons WHERE agent_name=? AND active=1",
                    (agent,),
                ).fetchall()
            words_new = set(lesson_text.lower().split())
            for (existing_text,) in existing:
                words_old = set(existing_text.lower().split())
                overlap = len(words_new & words_old) / max(len(words_new), 1)
                if overlap > 0.5:
                    return True
        except Exception:
            pass
        return False

    def _write_lesson(self, agent: str, text: str, confidence: float,
                      chain_ids: list[str], sample_size: int) -> None:
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute(
                    """INSERT INTO agent_lessons (
                        agent_name, lesson_text, derived_from_chain_ids,
                        confidence_in_lesson, sample_size, created_at_utc,
                        last_reinforced_at_utc, times_reinforced, active, human_approved
                    ) VALUES (?,?,?,?,?,?,?,1,1,0)""",
                    (
                        agent, text,
                        json.dumps(chain_ids[:20]),  # cap at 20 chain IDs
                        round(confidence, 3),
                        sample_size,
                        datetime.now(tz=timezone.utc).isoformat(),
                        datetime.now(tz=timezone.utc).isoformat(),
                    ),
                )
        except Exception as exc:
            logger.warning("LessonsGenerator._write_lesson[%s]: %s", agent, exc)

"""
agora/agents/performance_analyst.py — Weekly cross-agent performance analyst.

Runs every Sunday after market close (or on-demand via !analyze Discord command).
Reads ALL agent journals together and spots patterns that per-agent LessonsGenerator
cannot see: debate quality, feature value (chart/flow/memory), calibration drift.

Outputs two things:
  1. Proposed lessons written to agent_lessons (human_approved=NULL — await CEO review)
  2. A formatted Discord DM digest summarising findings and pending lesson count

Model: claude-sonnet-4-6 with effort=medium (analysis, not trading decisions).
Cost: ~$0.05–0.15 per weekly run across the full journal.

Sacred Rule §17: Lessons are advisory until explicitly approved via !approve <id>.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

import anthropic

from agora.ops.llm_cost_log import log_call as _log_llm, log_message as _log_msg
from agora.ops.payload_compressor import compress_payload as _compress

logger = logging.getLogger(__name__)

_MODEL = "claude-sonnet-4-6"
_SAMPLE = 30   # rows per journal

_SYSTEM = """\
You are a quantitative performance analyst for a multi-agent options trading system.
You receive a cross-agent data dump: swing decisions, advocate verdicts, defender verdicts,
analyst theses, strategy selections, and exit decisions — all linked by ticker and date.

Your job is to find CROSS-AGENT patterns invisible to each agent's own lesson synthesis.
Focus on:

1. DEBATE QUALITY — When advocate BLOCKed but defender overrode (CAUTION path):
   did those trades outperform or underperform straight PASSes?

2. FEATURE VALUE — Do trades with chart_b64/flow_signals/similar_trades inputs
   show higher prediction accuracy than trades without? (method="claude" vs "fallback")

3. CALIBRATION — Are high-confidence calls (≥70%) actually winning more than
   low-confidence calls (<50%)? Flag agents whose confidence is systematically miscalibrated.

4. THESIS ALIGNMENT — When analyst direction matches swing_judge direction AND
   advocate PASSes, what is the win rate vs when they disagree?

5. STRATEGY PATTERNS — Which strategy_type + regime combinations have the best outcomes?

OUTPUT: exactly this JSON, no markdown, no prose:
{
  "summary": "2-3 sentence executive summary of this week's findings",
  "lessons": [
    {
      "agent_name": "swing_judge" | "advocate" | "defender" | "analyst" | "strategy" | "exit" | "system",
      "lesson_text": "specific, measurable, actionable lesson",
      "confidence_in_lesson": <float 0.5-0.95>,
      "pattern_observed": "brief data evidence (win rates, counts, etc.)"
    }
  ],
  "feature_value": {
    "chart_vision_delta_pct": <float or null>,
    "flow_signals_delta_pct": <float or null>,
    "semantic_memory_delta_pct": <float or null>
  },
  "calibration_flags": [
    {"agent": "...", "issue": "...", "severity": "high|medium|low"}
  ]
}

Max 5 lessons. Return empty arrays if no clear pattern emerges (n < 10 is insufficient).
Be conservative — a lesson that hurts is worse than no lesson."""


class PerformanceAnalystAgent:
    """
    Weekly meta-agent that reads ALL journals together and proposes cross-agent lessons.

    Called by ScheduledAttributor.run_performance_analysis() — not in the hot path.
    All output goes to agent_lessons (pending) + Discord DM digest.
    """

    def __init__(self, db_path: str, api_key: str | None = None) -> None:
        self._db_path = db_path
        self._api_key = api_key or self._load_key()
        self._client  = anthropic.AsyncAnthropic(api_key=self._api_key)
        logger.info("PerformanceAnalystAgent ready: model=%s db=%s", _MODEL, db_path)

    def _load_key(self) -> str:
        try:
            from agora.core.config import get_settings
            return get_settings().anthropic_api_key
        except Exception:
            import os
            key = os.getenv("ANTHROPIC_API_KEY", "")
            if not key:
                raise RuntimeError("ANTHROPIC_API_KEY not set")
            return key

    async def analyze(self) -> dict:
        """
        Run full cross-agent analysis.
        Returns {"lessons_written": int, "summary": str, "digest": str}.
        Never raises — failures return an empty result so the scheduler stays alive.
        """
        try:
            payload = self._build_payload()
            if payload["total_rows"] < 10:
                logger.info("PerformanceAnalyst: too few rows (%d) — skipping", payload["total_rows"])
                return {"lessons_written": 0, "summary": "Insufficient data", "digest": ""}

            response = await self._client.messages.create(
                model=_MODEL,
                max_tokens=4096,
                thinking={"type": "adaptive"},
                output_config={"effort": "medium"},
                system=_SYSTEM,
                messages=[{"role": "user", "content": _compress(payload)}],
                timeout=anthropic.Timeout(connect=30.0, read=120.0, write=30.0, pool=30.0),
            )

            try:
                _log_msg(self._db_path, "PerformanceAnalystAgent", _MODEL, response.usage,
                         purpose="weekly_analysis")
            except Exception:
                pass

            text_blocks = [b for b in response.content if b.type == "text"]
            raw_text = text_blocks[-1].text.strip() if text_blocks else "{}"
            raw = _parse_json(raw_text)

            written = self._write_lessons(raw.get("lessons", []))
            digest  = self._build_digest(raw, written)

            logger.info("PerformanceAnalyst: %d lessons proposed | %s",
                        written, raw.get("summary", "")[:80])
            return {
                "lessons_written": written,
                "summary": raw.get("summary", ""),
                "digest": digest,
                "calibration_flags": raw.get("calibration_flags", []),
                "feature_value": raw.get("feature_value", {}),
            }

        except Exception as exc:
            logger.warning("PerformanceAnalystAgent.analyze failed: %s", exc)
            return {"lessons_written": 0, "summary": str(exc), "digest": ""}

    # ── Data collection ───────────────────────────────────────────────────────

    def _build_payload(self) -> dict:
        swing    = self._fetch_swing()
        advocate = self._fetch_advocate()
        defender = self._fetch_defender()
        analyst  = self._fetch_analyst()
        strategy = self._fetch_strategy()

        total = len(swing) + len(advocate) + len(defender) + len(analyst) + len(strategy)

        # Feature value: split swing rows by whether chart/flow was used
        with_features    = [r for r in swing if r.get("method") == "claude"]
        without_features = [r for r in swing if r.get("method") == "fallback"]

        return {
            "analysis_date": datetime.now(tz=timezone.utc).isoformat()[:10],
            "total_rows": total,
            "swing_decisions": swing,
            "advocate_verdicts": advocate,
            "defender_verdicts": defender,
            "analyst_theses": analyst,
            "strategy_selections": strategy,
            "feature_usage": {
                "claude_method_count": len(with_features),
                "fallback_method_count": len(without_features),
                "claude_win_rate": _win_rate(with_features, "pnl_pct"),
                "fallback_win_rate": _win_rate(without_features, "pnl_pct"),
            },
            "task": (
                "Analyze these cross-agent outcomes for this week. "
                "Find patterns that individual agents cannot see on their own. "
                "Propose specific, measurable lessons."
            ),
        }

    def _fetch_swing(self) -> list[dict]:
        try:
            with sqlite3.connect(self._db_path) as conn:
                rows = conn.execute(
                    """SELECT ticker, decision_ts, raw_score, direction, go, option_type,
                              confidence, outcome, pnl_pct, method,
                              key_thesis, what_kills_trade, post_trade_audit
                       FROM swing_journal
                       ORDER BY decision_ts DESC LIMIT ?""",
                    (_SAMPLE,),
                ).fetchall()
            cols = ["ticker", "decision_ts", "raw_score", "direction", "go", "option_type",
                    "confidence", "outcome", "pnl_pct", "method",
                    "key_thesis", "what_kills_trade", "post_trade_audit"]
            return [dict(zip(cols, r)) for r in rows]
        except Exception as exc:
            logger.debug("fetch_swing: %s", exc)
            return []

    def _fetch_advocate(self) -> list[dict]:
        try:
            with sqlite3.connect(self._db_path) as conn:
                rows = conn.execute(
                    """SELECT decision_id, ticker, decided_at_utc, verdict,
                              verdict_confidence, trade_taken, realized_pnl, advocate_was_right
                       FROM advocate_journal
                       ORDER BY decided_at_utc DESC LIMIT ?""",
                    (_SAMPLE,),
                ).fetchall()
            cols = ["decision_id", "ticker", "decided_at_utc", "verdict",
                    "verdict_confidence", "trade_taken", "realized_pnl", "advocate_was_right"]
            return [dict(zip(cols, r)) for r in rows]
        except Exception as exc:
            logger.debug("fetch_advocate: %s", exc)
            return []

    def _fetch_defender(self) -> list[dict]:
        try:
            with sqlite3.connect(self._db_path) as conn:
                # Table created on first ThesisDefender run
                rows = conn.execute(
                    """SELECT decision_id, ticker, decided_at_utc, thesis_strength,
                              confidence, go_recommendation, shadow_mode
                       FROM defender_journal
                       ORDER BY decided_at_utc DESC LIMIT ?""",
                    (_SAMPLE,),
                ).fetchall()
            cols = ["decision_id", "ticker", "decided_at_utc", "thesis_strength",
                    "confidence", "go_recommendation", "shadow_mode"]
            return [dict(zip(cols, r)) for r in rows]
        except Exception as exc:
            logger.debug("fetch_defender (table may not exist yet): %s", exc)
            return []

    def _fetch_analyst(self) -> list[dict]:
        try:
            with sqlite3.connect(self._db_path) as conn:
                rows = conn.execute(
                    """SELECT decision_id, ticker, decided_at_utc, direction,
                              confidence_pct, strategy_family, thesis_played_out,
                              magnitude_realized_pct
                       FROM analyst_journal
                       ORDER BY decided_at_utc DESC LIMIT ?""",
                    (_SAMPLE,),
                ).fetchall()
            cols = ["decision_id", "ticker", "decided_at_utc", "direction",
                    "confidence_pct", "strategy_family", "thesis_played_out",
                    "magnitude_realized_pct"]
            return [dict(zip(cols, r)) for r in rows]
        except Exception as exc:
            logger.debug("fetch_analyst: %s", exc)
            return []

    def _fetch_strategy(self) -> list[dict]:
        try:
            with sqlite3.connect(self._db_path) as conn:
                rows = conn.execute(
                    """SELECT decision_id, ticker, decided_at_utc, decision,
                              strategy_type, realized_pnl
                       FROM strategy_journal
                       ORDER BY decided_at_utc DESC LIMIT ?""",
                    (_SAMPLE,),
                ).fetchall()
            cols = ["decision_id", "ticker", "decided_at_utc", "decision",
                    "strategy_type", "realized_pnl"]
            return [dict(zip(cols, r)) for r in rows]
        except Exception as exc:
            logger.debug("fetch_strategy: %s", exc)
            return []

    # ── Output writing ────────────────────────────────────────────────────────

    def _write_lessons(self, lessons: list[dict]) -> int:
        written = 0
        for lesson in lessons[:5]:
            agent  = lesson.get("agent_name", "system")
            text   = lesson.get("lesson_text", "").strip()
            conf   = float(lesson.get("confidence_in_lesson", 0.6))
            pattern = lesson.get("pattern_observed", "")

            if not text or conf < 0.5:
                continue
            if self._is_duplicate(agent, text):
                continue

            try:
                with sqlite3.connect(self._db_path) as conn:
                    conn.execute(
                        """INSERT INTO agent_lessons (
                               agent_name, lesson_text, derived_from_chain_ids,
                               confidence_in_lesson, sample_size,
                               created_at_utc, last_reinforced_at_utc,
                               times_reinforced, active, human_approved
                           ) VALUES (?,?,?,?,?,?,?,1,1,NULL)""",
                        (
                            agent, text,
                            json.dumps([pattern[:200]]),
                            round(conf, 3), _SAMPLE,
                            datetime.now(tz=timezone.utc).isoformat(),
                            datetime.now(tz=timezone.utc).isoformat(),
                        ),
                    )
                written += 1
                logger.info("PerformanceAnalyst lesson [%s] conf=%.2f: %s",
                            agent, conf, text[:100])
            except Exception as exc:
                logger.debug("write_lesson error: %s", exc)

        return written

    def _is_duplicate(self, agent: str, text: str) -> bool:
        try:
            with sqlite3.connect(self._db_path) as conn:
                existing = conn.execute(
                    "SELECT lesson_text FROM agent_lessons WHERE agent_name=? AND active=1",
                    (agent,),
                ).fetchall()
            words_new = set(text.lower().split())
            for (ex,) in existing:
                words_old = set(ex.lower().split())
                if len(words_new & words_old) / max(len(words_new), 1) > 0.5:
                    return True
        except Exception:
            pass
        return False

    def _build_digest(self, raw: dict, written: int) -> str:
        summary  = raw.get("summary", "No summary.")
        fv       = raw.get("feature_value", {})
        cal_flags = raw.get("calibration_flags", [])

        lines = [
            "**Weekly Performance Analysis**",
            f"_{summary}_",
            "",
        ]

        if fv.get("chart_vision_delta_pct") is not None:
            lines.append(f"Chart vision lift: {fv['chart_vision_delta_pct']:+.1f}% win rate")
        if fv.get("flow_signals_delta_pct") is not None:
            lines.append(f"Flow signals lift: {fv['flow_signals_delta_pct']:+.1f}% win rate")
        if fv.get("semantic_memory_delta_pct") is not None:
            lines.append(f"Semantic memory lift: {fv['semantic_memory_delta_pct']:+.1f}% win rate")

        if cal_flags:
            lines.append("")
            lines.append("**Calibration flags:**")
            for f in cal_flags[:3]:
                lines.append(f"  [{f.get('severity','?').upper()}] {f.get('agent','?')}: {f.get('issue','?')}")

        lines += [
            "",
            f"**{written} lesson(s) pending your review.**",
            "Reply `!lessons` to see them, `!approve <id>` or `!reject <id>` to act.",
        ]
        return "\n".join(lines)


# ── Pure helpers ──────────────────────────────────────────────────────────────

def _win_rate(rows: list[dict], pnl_key: str) -> float | None:
    scored = [r for r in rows if r.get(pnl_key) is not None]
    if not scored:
        return None
    wins = sum(1 for r in scored if (r.get(pnl_key) or 0) > 0)
    return round(wins / len(scored), 3)


def _parse_json(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find('{')
    if start >= 0:
        try:
            return json.loads(text[start:])
        except json.JSONDecodeError:
            pass
        for end in range(len(text), start, -1):
            if text[end - 1] == '}':
                try:
                    return json.loads(text[start:end])
                except json.JSONDecodeError:
                    continue
    return {"lessons": [], "summary": "parse error", "calibration_flags": [], "feature_value": {}}

"""
ThesisDefenderAgent — generates the 3 strongest reasons a proposed trade will WORK.

Counterpart to AdvocateAgent (which generates failure modes). Both run in parallel;
the debate verdict moderates the BLOCK threshold: a strong defense raises the bar
the advocate needs to block.

Model: claude-sonnet-4-6 (same as AdvocateAgent)
Called in parallel with AdvocateAgent — same inputs, same timing.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import anthropic

from agora.core.json_extract import extract_json as _extract_json
from agora.ops.llm_cost_log import log_call as _log_llm, log_message as _log_msg
from agora.ops.payload_compressor import compress_payload as _compress
from agora.ops.lessons_store import load_approved_lessons as _load_lessons
from agora.mcp.sqlite_tools import SQLITE_TOOLS, sqlite_tool_handlers
from agora.mcp.search_tools import SEARCH_TOOLS, search_tool_handlers
from agora.mcp.tool_runner import run_with_tools

logger = logging.getLogger(__name__)

PROMPT_VERSION = "1.0.0"
_MODEL = "claude-sonnet-4-6"

# ── System prompt ─────────────────────────────────────────────────────────────

_SYSTEM = """You are a thesis defender for an options trading system. Your job is to find the strongest reasons a proposed trade will MAKE money. You are the defense, not a balanced analyst.

You will receive:
- A thesis from StockAnalystAgent
- A proposed options structure
- The full signal context
- Current portfolio positions

Your job: produce the 3 strongest, most specific reasons this trade will PROFIT.

WHAT YOU ARE LOOKING FOR:

1. THESIS STRENGTH
   - Is there a clear, specific catalyst with measurable probability?
   - Does the timing align with a known setup (earnings, breakout, regime)?
   - Is there confirmation from multiple independent signals?

2. STRUCTURE ADVANTAGE
   - Does the structure maximize leverage for the expected move?
   - Is the risk/reward appropriate for the conviction level?
   - Is IV positioning favorable (low IV for debit, high IV for credit)?

3. KNOWN SUCCESS PATTERNS
   - Low IV directional debit → IV expansion amplifies gains
   - Credit spread in high IV → time decay works in our favor
   - Post-earnings volatility compression → premium collapses as planned
   - Breakout with volume confirmation → trend continuation follow-through

4. RISK MANAGEMENT STRENGTH
   - Clear stop-loss level defined → max loss is bounded
   - Position size is appropriate for account → survivable if wrong
   - DTE gives adequate time for thesis to develop

5. EDGE SOURCES
   - Analyst consensus revision → institutional flow follows
   - Unusual options activity confirming direction → smart money signal
   - Sector momentum alignment → tide lifting all boats

SEVERITY RUBRIC (for success modes):
HIGH — historically a primary driver of profitable outcomes in this setup type.
MEDIUM — contributes meaningfully to the thesis but not decisive alone.
LOW — worth noting as supporting evidence.

OUTPUT — return exactly this JSON, no markdown, no prose:

BREVITY — go_recommendation and confidence are the decision; the text fields are for the
journal. Be terse and obey the per-field length caps below exactly. Specificity (numbers,
strikes, dates) over adjectives — never pad to fill space. Every wasted word is wasted cost.

{
  "go_recommendation": true | false,
  "confidence": <float 0.0-1.0>,
  "success_modes": [
    {
      "mode_name": "short descriptive name, <= 6 words",
      "probability_pct": <integer 5-95>,
      "mechanism": "exactly how this drives profit, <= 25 words",
      "catalyst_conditions": ["specific observable trigger, <= 10 words each — MAX 2 items"]
    },
    { "...mode 2..." },
    { "...mode 3..." }
  ],
  "most_likely_win_scenario": "narrative with specific price/time refs, <= 40 words",
  "conceded_risks": "the 1-2 real risks, <= 30 words total"
}

IMPORTANT: You MUST output exactly 3 success modes. Be specific and quantitative.
Do not be blindly optimistic — if the thesis is genuinely weak, reflect that in
lower probabilities and go_recommendation=false."""


# ── Output dataclass ──────────────────────────────────────────────────────────

@dataclass
class DefenderVerdict:
    thesis_strength: str            # "strong" | "moderate" | "weak"
    confidence: float               # 0.0-1.0 (defender's confidence the trade works)
    go_recommendation: bool         # True = defender recommends entering
    success_modes: list[dict]       # 3 strongest reasons it will work
    most_likely_win_scenario: str   # narrative: how this trade plays out profitably
    conceded_risks: str             # what the defender admits could go wrong
    raw: dict = field(default_factory=dict)

    @property
    def is_strong(self) -> bool:
        return self.thesis_strength == "strong"

    @property
    def is_weak(self) -> bool:
        return self.thesis_strength == "weak"


# ── Agent ─────────────────────────────────────────────────────────────────────

class ThesisDefenderAgent:
    """
    LLM thesis defense — runs in parallel with AdvocateAgent, before IBKR.

    In shadow mode: fires on every proposed trade, journals result, does not gate execution.
    In live mode: a strong defense raises the bar the advocate needs to block.

    Requires StockAnalystAgent to have run (for thesis context). If thesis is None
    (analyst disabled or failed), the agent reviews based on structure + signals only.
    """

    def __init__(self, settings: Any, shadow_mode: bool = True) -> None:
        self._settings    = settings
        self._shadow_mode = shadow_mode
        self._client      = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        self._ensure_table()
        logger.info("ThesisDefenderAgent ready: model=%s shadow=%s", _MODEL, shadow_mode)

    @property
    def shadow_mode(self) -> bool:
        return self._shadow_mode

    @shadow_mode.setter
    def shadow_mode(self, value: bool) -> None:
        if self._shadow_mode != value:
            logger.info("ThesisDefenderAgent shadow_mode %s → %s", self._shadow_mode, value)
        self._shadow_mode = value

    def _ensure_table(self) -> None:
        """Create defender_journal table if it does not exist."""
        try:
            with sqlite3.connect(str(self._settings.db_path)) as conn:
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS defender_journal (
                        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                        decision_id           TEXT NOT NULL DEFAULT '',
                        ticker                TEXT NOT NULL,
                        decided_at_utc        TEXT NOT NULL,
                        model                 TEXT NOT NULL,
                        thesis_strength       TEXT,
                        confidence            REAL,
                        go_recommendation     INTEGER,
                        success_modes_json    TEXT,
                        most_likely_win_scenario TEXT,
                        output_full_json      TEXT,
                        input_tokens          INTEGER DEFAULT 0,
                        output_tokens         INTEGER DEFAULT 0,
                        cost_usd              REAL    DEFAULT 0.0,
                        latency_ms            INTEGER DEFAULT 0,
                        shadow_mode           INTEGER DEFAULT 1
                    )"""
                )
        except Exception as exc:
            logger.debug("defender_journal table creation error: %s", exc)

    async def defend(
        self,
        ticker:          str,
        recommendation:  Any,              # TradeRecommendation
        thesis:          Any | None,       # AnalystThesis (may be None)
        positions:       list[Any],        # current open positions
        macro_context:   Any | None,
        decision_id:     str = "",
    ) -> DefenderVerdict | None:
        """
        Generate thesis defense.
        Returns DefenderVerdict in both shadow and live mode.
        Never raises — failures return None (treated as no defense by callers).
        """
        lessons = _load_lessons(str(self._settings.db_path), "defender")
        payload = self._build_payload(ticker, recommendation, thesis, positions, macro_context, lessons)
        t0 = time.monotonic()
        verdict: DefenderVerdict | None = None
        raw_output: dict = {}

        _db = str(self._settings.db_path)
        _tavily_key = getattr(self._settings, "tavily_api_key", None)
        # Drop web-search tools when no key — avoids a wasted tool-turn on a dead tool.
        _tools = SQLITE_TOOLS + (SEARCH_TOOLS if _tavily_key else [])
        _handlers = {
            **sqlite_tool_handlers(_db),
            **(search_tool_handlers(_tavily_key) if _tavily_key else {}),
        }

        _cached_system = [{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}]
        try:
            response = await run_with_tools(
                client=self._client,
                model=_MODEL,
                system=_cached_system,
                messages=[{"role": "user", "content": _compress(payload)}],
                tools=_tools,
                handlers=_handlers,
                max_turns=3,
                max_tokens=2000,
                thinking={"type": "disabled"},
                output_config={"effort": "medium"},
                timeout=anthropic.Timeout(connect=30.0, read=120.0, write=30.0, pool=30.0),
            )
            latency_ms = int((time.monotonic() - t0) * 1000)
            in_tok  = response.usage.input_tokens  if response.usage else 0
            out_tok = response.usage.output_tokens if response.usage else 0
            cost    = (in_tok * 3.0 + out_tok * 15.0) / 1_000_000  # Sonnet 4.6

            text_blocks = [b for b in response.content if b.type == "text"]
            raw_text = text_blocks[-1].text if text_blocks else "{}"
            raw_output = _extract_json(raw_text)
            verdict = _parse_verdict(raw_output)

            try:
                _log_msg(str(self._settings.db_path), "ThesisDefenderAgent", _MODEL,
                         response.usage, purpose=f"defender_{ticker}",
                         trace_id=decision_id)
            except Exception:
                pass

            self._write_journal(decision_id, ticker, verdict, raw_output,
                                in_tok, out_tok, cost, latency_ms)

            logger.info(
                "Defender [%s] strength=%s conf=%.2f go=%s | top_mode=%s%s",
                ticker,
                verdict.thesis_strength,
                verdict.confidence,
                verdict.go_recommendation,
                verdict.success_modes[0]["mode_name"] if verdict.success_modes else "n/a",
                " [SHADOW]" if self._shadow_mode else "",
            )

        except Exception as exc:
            latency_ms = int((time.monotonic() - t0) * 1000)
            logger.warning("ThesisDefenderAgent failed for %s: %s", ticker, exc)
            self._write_journal(decision_id, ticker, None, {"error": str(exc)},
                                0, 0, 0.0, latency_ms)

        return verdict

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _build_payload(
        self,
        ticker:         str,
        recommendation: Any,
        thesis:         Any | None,
        positions:      list[Any],
        macro_context:  Any | None,
        lessons:        list[str] | None = None,
    ) -> dict:
        """Build the user payload — same structure as AdvocateAgent._build_payload."""
        pos_summary = [
            {
                "ticker":    p.ticker,
                "direction": getattr(p, "direction", "neutral"),
                "strategy":  str(p.strategy),
                "pillar":    str(p.pillar),
            }
            for p in (positions or [])
        ]

        from datetime import date as _date
        _today = _date.today()
        rec_summary = {}
        if recommendation:
            try:
                rec_summary = {
                    "strategy_type":      str(getattr(recommendation.strategy, "value",
                                                       recommendation.strategy)),
                    "direction":          recommendation.direction,
                    "contracts":          recommendation.contracts,
                    "entry_debit_credit": recommendation.entry_debit_credit,
                    "max_profit":         recommendation.max_gain_dollars,
                    "max_loss":           recommendation.max_loss_dollars,
                    "rr_ratio":           recommendation.reward_risk_ratio,
                    "today_date":         _today.isoformat(),
                    "legs": [
                        {"action": lg.action, "type": lg.option_type,
                         "strike": lg.strike, "expiry": str(lg.expiration),
                         "dte": (lg.expiration - _today).days,
                         "mid": lg.mid_price, "delta": lg.delta}
                        for lg in recommendation.legs
                    ],
                }
            except Exception:
                pass

        thesis_summary = {}
        if thesis:
            thesis_summary = {
                "direction":          thesis.direction,
                "magnitude_pct":      thesis.magnitude_pct,
                "horizon_days":       thesis.horizon_days,
                "confidence_pct":     thesis.confidence_pct,
                "strategy_family":    thesis.strategy_family,
                "kill_conditions":    thesis.kill_conditions,
                "scorecard_critique": thesis.scorecard_critique,
                "reasoning_trace":    thesis.reasoning_trace,
            }

        return {
            "ticker":    ticker,
            "thesis":    thesis_summary,
            "structure": rec_summary,
            "portfolio": pos_summary,
            "macro": {
                "stance":         macro_context.macro_stance    if macro_context else "unknown",
                "confidence":     macro_context.confidence      if macro_context else None,
                "vol_selling_ok": macro_context.vol_selling_ok  if macro_context else None,
                "key_risk":       macro_context.key_risk        if macro_context else None,
            },
            "approved_lessons": lessons or [],
        }

    def _write_journal(
        self,
        decision_id: str,
        ticker:      str,
        verdict:     DefenderVerdict | None,
        raw_output:  dict,
        in_tok:      int,
        out_tok:     int,
        cost:        float,
        latency_ms:  int,
    ) -> None:
        try:
            with sqlite3.connect(str(self._settings.db_path)) as conn:
                conn.execute(
                    """INSERT INTO defender_journal (
                        decision_id, ticker, decided_at_utc, model,
                        thesis_strength, confidence, go_recommendation,
                        success_modes_json, most_likely_win_scenario,
                        output_full_json,
                        input_tokens, output_tokens, cost_usd, latency_ms, shadow_mode
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        decision_id or "",
                        ticker,
                        datetime.now(tz=timezone.utc).isoformat(),
                        _MODEL,
                        verdict.thesis_strength    if verdict else "error",
                        verdict.confidence         if verdict else None,
                        1 if (verdict and verdict.go_recommendation) else 0,
                        json.dumps(verdict.success_modes if verdict else [], default=str),
                        verdict.most_likely_win_scenario if verdict else None,
                        json.dumps(raw_output, default=str),
                        in_tok, out_tok, round(cost, 6), latency_ms,
                        1 if self._shadow_mode else 0,
                    ),
                )
        except Exception as exc:
            logger.debug("defender_journal write error: %s", exc)


# ── Pure helpers ──────────────────────────────────────────────────────────────

def _parse_json_robust(text: str) -> dict:
    """Parse JSON with best-effort recovery for leading/trailing prose and truncation."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Skip leading prose — find first '{' and parse from there
    start = text.find('{')
    if start > 0:
        try:
            return json.loads(text[start:])
        except json.JSONDecodeError:
            pass
    # Scan backwards for the longest valid prefix starting at '{'
    search_start = max(start, 0)
    for end in range(len(text), 0, -1):
        if text[end - 1] == '}':
            try:
                return json.loads(text[search_start:end])
            except json.JSONDecodeError:
                continue
    return {
        "go_recommendation": False,
        "confidence": 0.5,
        "success_modes": [],
        "most_likely_win_scenario": "JSON parse failed — defaulting to neutral",
        "conceded_risks": "unknown",
        "parse_error": True,
    }


def _compute_thesis_strength(verdict: DefenderVerdict) -> str:
    """
    Deterministic thesis_strength from confidence and success_mode probabilities.

    "strong"   if confidence >= 0.65 AND at least 1 success_mode has probability_pct >= 50
    "moderate" if confidence >= 0.45
    "weak"     otherwise
    """
    if verdict.confidence >= 0.65:
        top_prob = max(
            (m.get("probability_pct", 0) for m in verdict.success_modes),
            default=0,
        )
        if top_prob >= 50:
            return "strong"
    if verdict.confidence >= 0.45:
        return "moderate"
    return "weak"


def _parse_verdict(raw: dict) -> DefenderVerdict:
    # Build a partial verdict first so we can compute thesis_strength deterministically.
    partial = DefenderVerdict(
        thesis_strength="weak",  # placeholder; overwritten below
        confidence=float(raw.get("confidence", 0.5)),
        go_recommendation=bool(raw.get("go_recommendation", False)),
        success_modes=raw.get("success_modes", []),
        most_likely_win_scenario=raw.get("most_likely_win_scenario", ""),
        conceded_risks=raw.get("conceded_risks", ""),
        raw=raw,
    )
    partial.thesis_strength = _compute_thesis_strength(partial)
    return partial

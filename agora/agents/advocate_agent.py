"""
agora/agents/advocate_agent.py — LLM DevilsAdvocateAgent (Phase 5, spec §13.2).

Adversarial pre-IBKR review. Produces the 3 strongest arguments for why a
proposed trade will LOSE money. Verdict is deterministic from the failure modes:
  ANY HIGH + probability ≥ 20% + not in kill_conditions → BLOCK
  ANY HIGH + probability < 20%                          → CAUTION
  All MEDIUM or below                                   → PASS

Shadow mode (default):
  Fires on every proposed trade, journals to advocate_journal, does NOT block.

Live mode:
  BLOCK verdict stops submission. CAUTION is logged but execution continues.

Model: claude-sonnet-4-6 (structured adversarial review — cost-efficient for high-frequency calls)
Cost: ~$0.15/call × ≤5 trades/day ≈ $0.75/day (well under $5 budget)
Schema: advocate_journal — managed by migrations/2026_05_phase2_journals.sql

System prompt: §13.2 of AGORA Grand Specification v1.0
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

from agora.ops.llm_cost_log import log_call as _log_llm, log_message as _log_msg
from agora.ops.payload_compressor import compress_payload as _compress
from agora.ops.lessons_store import load_approved_lessons as _load_lessons
from agora.mcp.sqlite_tools import SQLITE_TOOLS, sqlite_tool_handlers
from agora.mcp.search_tools import SEARCH_TOOLS, search_tool_handlers
from agora.mcp.flow_tools import FLOW_TOOLS, flow_tool_handlers
from agora.mcp.tool_runner import run_with_tools

logger = logging.getLogger(__name__)

PROMPT_VERSION = "1.0.0"
_MODEL = "claude-sonnet-4-6"

# ── System prompt (spec §13.2) ────────────────────────────────────────────────

_SYSTEM = """You are an adversarial options trader hired specifically to find flaws in proposed trades. Your job is to argue against every trade that crosses your desk. You are not a balanced analyst. You are the prosecution.

You will receive:
- A thesis from StockAnalystAgent
- A proposed structure (strategy type, direction, key params)
- The full signal context
- Current portfolio positions (for correlation checks)

Your job: produce the 3 strongest, most specific arguments for why this trade will LOSE money. Rank them by severity and probability.

WHAT YOU ARE LOOKING FOR:

1. THESIS FRAGILITY
   - Built on one signal that, if wrong, kills the trade?
   - Assumes a catalyst that may have already played out?
   - Assumes macro continuation in a clearly regime-shifting moment?

2. STRUCTURE-THESIS MISMATCH
   - Does the structure actually express the thesis, or a different bet?
   - Wrong DTE for the horizon? Wrong delta for the magnitude?

3. KNOWN FAILURE PATTERNS
   - High IV + directional debit → IV crush kills the win even when right
   - Pre-earnings credit sell → vol expansion overwhelms premium collected
   - Selling premium in low-vol regime → insufficient edge
   - Directional debit in high-vol regime → overpaying for direction

4. MICROSTRUCTURE RISKS
   - Wide bid-ask eating edge (bid-ask > 8% of mid)
   - Short strike near high-OI level (pin risk at expiry)
   - Earnings or ex-div between entry and target exit

5. PORTFOLIO CORRELATION
   - Already long delta in same sector? Adding this concentrates risk.
   - Already short vega everywhere? Single vol event wipes portfolio.

6. CALIBRATION CONCERN
   - Analyst confidence seems inflated for this quality of thesis?
   - Scorecard critique ignored or poorly addressed?

SEVERITY RUBRIC:
HIGH — historically causes full-stop losses OR violates a hard methodology principle.
MEDIUM — reduces expected value materially but does not categorically kill the trade.
LOW — worth noting, does not change the trade.

HARD CONSTRAINTS:
C1. You MUST output exactly 3 failure modes. Not 1. Not 5. Three.
C2. Each failure mode MUST be specific (numeric triggers, not "market may fall").
C3. Each failure mode MUST estimate probability as a percentage (5–95%).
C4. Verdict logic is DETERMINISTIC (you do not decide it, the rules do):
    - ANY HIGH + probability ≥ 20% + NOT addressed by kill_conditions → BLOCK
    - ANY HIGH + probability < 20% → CAUTION
    - All MEDIUM or below → PASS
C5. Your verdict_confidence is YOUR confidence that your failure mode analysis is complete (not about the trade).

OUTPUT — return exactly this JSON, no markdown, no prose:

BREVITY — the verdict is computed deterministically from severity + probability_pct +
already_addressed_by_kill_condition (C4), so the text fields are for the journal, not the
decision. Be terse: every wasted word is wasted cost. Obey the per-field length caps below
exactly. Specificity (numbers, strikes, dates) over adjectives — never pad to fill space.

{
  "verdict": "PASS | CAUTION | BLOCK",
  "verdict_confidence_pct": <integer 40-90>,
  "verdict_reasoning_one_line": "string, <= 20 words",
  "failure_modes": [
    {
      "mode_name": "short descriptive name, <= 6 words",
      "severity": "HIGH | MEDIUM | LOW",
      "probability_pct": <integer 5-95>,
      "mechanism": "exactly how this causes a loss, <= 25 words",
      "trigger_conditions": ["specific observable trigger, <= 10 words each — MAX 2 items"],
      "already_addressed_by_kill_condition": <boolean>
    },
    { "...mode 2..." },
    { "...mode 3..." }
  ],
  "most_likely_loss_scenario": "narrative with specific price/time refs, <= 40 words",
  "recommendation_if_pass": "single suggested tweak, <= 20 words, or null"
}

CREDIT SPREAD / PREMIUM SELLING — MANDATORY CALIBRATION:
When strategy_type is BULL_PUT_SPREAD, BEAR_CALL_SPREAD, or IRON_CONDOR, the R/R ratio is INTENTIONALLY asymmetric (collecting $1 against $7-9 risk is NORMAL, not a failure). DO NOT flag "R/R asymmetry" or "high win rate required" as HIGH severity unless:
  • Short-leg delta > 0.30 (probability of profit at expiry < 70%), OR
  • Credit collected < 8% of spread width (degenerate structure, e.g. $0.40 on $10-wide spread)
For these structures, evaluate the trade on probability-of-profit, expected value (credit × POP - max_loss × (1-POP)), and DTE-theta match, not on raw risk:reward ratio.

DTE CALIBRATION — MANDATORY:
The payload includes "today_date" and each leg includes "dte" (days to expiry, pre-computed). USE THESE FIELDS — do not compute DTE yourself. Common target DTE for credit spreads is 30-60 days.

IMPORTANT: Do NOT let your role as adversary produce BLOCK verdicts that contradict C4 logic. If all three failure modes are MEDIUM, the verdict MUST be PASS even if you dislike the trade personally."""


# ── Output dataclass ──────────────────────────────────────────────────────────

@dataclass
class AdvocateVerdict:
    verdict:               str              # 'PASS' | 'CAUTION' | 'BLOCK'
    verdict_confidence:    int
    verdict_reasoning:     str
    failure_modes:         list[dict]
    most_likely_scenario:  str
    recommendation:        str | None
    raw:                   dict = field(default_factory=dict)

    @property
    def is_block(self) -> bool:
        return self.verdict == "BLOCK"

    @property
    def is_pass(self) -> bool:
        return self.verdict == "PASS"


# ── Agent ─────────────────────────────────────────────────────────────────────

class AdvocateAgent:
    """
    LLM adversarial review — runs after all deterministic gates, before IBKR.

    In shadow mode: fires on every proposed trade, journals result, never blocks.
    In live mode: BLOCK verdict stops submission and completes chain as 'risk_blocked'.

    Requires StockAnalystAgent to have run (for thesis context). If thesis is None
    (analyst disabled or failed), the agent reviews based on structure + signals only.
    """

    def __init__(self, settings: Any, shadow_mode: bool = True) -> None:
        self._settings    = settings
        self._shadow_mode = shadow_mode
        self._client      = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        logger.info("AdvocateAgent ready: model=%s shadow=%s", _MODEL, shadow_mode)

    @property
    def shadow_mode(self) -> bool:
        return self._shadow_mode

    @shadow_mode.setter
    def shadow_mode(self, value: bool) -> None:
        if self._shadow_mode != value:
            logger.info("AdvocateAgent shadow_mode %s → %s", self._shadow_mode, value)
        self._shadow_mode = value

    async def review(
        self,
        ticker:          str,
        recommendation:  Any,              # TradeRecommendation
        thesis:          Any | None,       # AnalystThesis (may be None)
        positions:       list[Any],        # current open positions
        macro_context:   Any | None,
        decision_id:     str = "",
    ) -> AdvocateVerdict | None:
        """
        Run adversarial review.
        Returns AdvocateVerdict in both shadow and live mode.
        Never raises — failures return None (treated as PASS by callers).
        """
        lessons = _load_lessons(str(self._settings.db_path), "advocate")
        payload = self._build_payload(ticker, recommendation, thesis, positions, macro_context, lessons)
        t0 = time.monotonic()
        verdict: AdvocateVerdict | None = None
        raw_output: dict = {}

        # MCP tools: advocate can verify earnings dates, query its own history,
        # check recent news — reduces hallucinated BLOCK verdicts
        _db = str(self._settings.db_path)
        _tavily_key = getattr(self._settings, "tavily_api_key", None)
        _tools = SQLITE_TOOLS + SEARCH_TOOLS + FLOW_TOOLS
        _handlers = {
            **sqlite_tool_handlers(_db),
            **search_tool_handlers(_tavily_key),
            **flow_tool_handlers(),
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
            raw_text = text_blocks[-1].text.strip() if text_blocks else "{}"
            if raw_text.startswith("```"):
                raw_text = raw_text.split("```")[1].lstrip("json").strip()

            raw_output = _parse_json_robust(raw_text)
            # Enforce deterministic verdict from failure mode analysis
            raw_output["verdict"] = _compute_verdict(
                raw_output.get("failure_modes", []),
                [c for c in (getattr(thesis, "kill_conditions", []) or [])],
            )
            verdict = _parse_verdict(raw_output)

            try:
                _log_msg(str(self._settings.db_path), "AdvocateAgent", _MODEL,
                         response.usage, purpose=f"advocate_{ticker}",
                         trace_id=decision_id)
            except Exception:
                pass

            self._write_journal(decision_id, ticker, verdict, raw_output,
                                in_tok, out_tok, cost, latency_ms)

            logger.info(
                "Advocate [%s] %s | conf=%d%% | top_failure=%s%s",
                ticker, verdict.verdict, verdict.verdict_confidence,
                verdict.failure_modes[0]["mode_name"] if verdict.failure_modes else "n/a",
                " [SHADOW]" if self._shadow_mode else "",
            )

        except Exception as exc:
            latency_ms = int((time.monotonic() - t0) * 1000)
            logger.warning("AdvocateAgent failed for %s: %s", ticker, exc)
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
        # Summarize open positions for correlation check
        pos_summary = [
            {
                "ticker":    p.ticker,
                "direction": getattr(p, "direction", "neutral"),
                "strategy":  str(p.strategy),
                "pillar":    str(p.pillar),
            }
            for p in (positions or [])
        ]

        # Summarize the recommendation
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
                "direction":       thesis.direction,
                "magnitude_pct":   thesis.magnitude_pct,
                "horizon_days":    thesis.horizon_days,
                "confidence_pct":  thesis.confidence_pct,
                "strategy_family": thesis.strategy_family,
                "kill_conditions": thesis.kill_conditions,
                "scorecard_critique": thesis.scorecard_critique,
                "reasoning_trace": thesis.reasoning_trace,
            }

        return {
            "ticker":         ticker,
            "thesis":         thesis_summary,
            "structure":      rec_summary,
            "portfolio":      pos_summary,
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
        verdict:     AdvocateVerdict | None,
        raw_output:  dict,
        in_tok:      int,
        out_tok:     int,
        cost:        float,
        latency_ms:  int,
    ) -> None:
        try:
            with sqlite3.connect(str(self._settings.db_path)) as conn:
                conn.execute(
                    """INSERT INTO advocate_journal (
                        decision_id, ticker, decided_at_utc, prompt_version, model,
                        payload_json, verdict, verdict_confidence, failure_modes_json,
                        most_likely_scenario, output_full_json,
                        input_tokens, output_tokens, cost_usd, latency_ms, shadow_mode
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        decision_id or "",
                        ticker,
                        datetime.now(tz=timezone.utc).isoformat(),
                        PROMPT_VERSION, _MODEL,
                        json.dumps({"ticker": ticker}, default=str),
                        verdict.verdict       if verdict else "error",
                        verdict.verdict_confidence if verdict else None,
                        json.dumps(verdict.failure_modes if verdict else [], default=str),
                        verdict.most_likely_scenario if verdict else None,
                        json.dumps(raw_output, default=str),
                        in_tok, out_tok, round(cost, 6), latency_ms,
                        1 if self._shadow_mode else 0,
                    ),
                )
        except Exception as exc:
            logger.debug("advocate_journal write error: %s", exc)


# ── Pure helpers ──────────────────────────────────────────────────────────────

def _parse_json_robust(text: str) -> dict:
    """Parse a valid advocate verdict object, recovering from truncated/malformed output.

    Claude sometimes embeds tool-result text (news snippets, filing excerpts) into
    string fields without escaping newlines or quotes, producing unterminated strings.
    Recovery strategy: find the longest prefix ending in '}' that parses into a dict
    containing 'failure_modes' (the field the deterministic verdict is computed from).

    Raises ValueError if no such object can be recovered. This is deliberate: a parse
    failure must NOT fabricate a PASS — review() catches the raise, returns None, and the
    session-level fail-closed gate blocks the un-reviewed trade. Returning a synthetic PASS
    here would silently bypass the adversarial gate.
    """
    def _valid(obj: object) -> bool:
        return isinstance(obj, dict) and "failure_modes" in obj

    try:
        obj = json.loads(text)
        if _valid(obj):
            return obj
    except json.JSONDecodeError:
        pass
    # Truncation recovery: longest prefix ending in '}' that is a valid verdict object.
    for end in range(len(text), 0, -1):
        if text[end - 1] == '}':
            try:
                obj = json.loads(text[:end])
            except json.JSONDecodeError:
                continue
            if _valid(obj):
                return obj
    raise ValueError("advocate output not parseable into a verdict object with failure_modes")


def _compute_verdict(failure_modes: list[dict], kill_conditions: list[str]) -> str:
    """Deterministic verdict per spec §13.2 C4 — not delegated to the LLM."""
    kill_text = " ".join(kill_conditions).lower()
    for fm in failure_modes:
        if fm.get("severity") == "HIGH":
            prob = int(fm.get("probability_pct", 0))
            addressed = fm.get("already_addressed_by_kill_condition", False)
            # Auto-mark as addressed if trigger_conditions text appears in kill_conditions
            if not addressed:
                triggers = fm.get("trigger_conditions", [])
                for t in triggers:
                    if any(word in kill_text for word in t.lower().split()[:3] if len(word) > 3):
                        addressed = True
                        break
            if not addressed and prob >= 20:
                return "BLOCK"
            if not addressed and prob < 20:
                return "CAUTION"
    return "PASS"


def _parse_verdict(raw: dict) -> AdvocateVerdict:
    return AdvocateVerdict(
        verdict=raw.get("verdict", "PASS"),
        verdict_confidence=raw.get("verdict_confidence_pct", 60),
        verdict_reasoning=raw.get("verdict_reasoning_one_line", ""),
        failure_modes=raw.get("failure_modes", []),
        most_likely_scenario=raw.get("most_likely_loss_scenario", ""),
        recommendation=raw.get("recommendation_if_pass"),
        raw=raw,
    )

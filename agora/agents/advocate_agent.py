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

import asyncio
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
from agora.ops.lessons_store import load_approved_lessons as _load_lessons, load_calibration_note as _load_cal_note
from agora.mcp.sqlite_tools import SQLITE_TOOLS, sqlite_tool_handlers
from agora.mcp.search_tools import SEARCH_TOOLS, search_tool_handlers
from agora.mcp.flow_tools import FLOW_TOOLS, flow_tool_handlers
from agora.mcp.tool_runner import run_with_tools

logger = logging.getLogger(__name__)

PROMPT_VERSION = "1.0.0"
_MODEL = "claude-sonnet-4-6"

# Transient API failures that should be RETRIED, not fail-closed. The advocate is the
# single gate every entry funnels through; treating a momentary timeout / 429 / 5xx as a
# hard BLOCK silently halts the entry engine (observed: 243 fail-closed blocks in a day,
# 0 fills). Retry-with-backoff recovers these before the fail-closed policy kicks in.
_RETRYABLE_ERRORS = tuple(
    e for e in (
        getattr(anthropic, "APITimeoutError", None),
        getattr(anthropic, "APIConnectionError", None),
        getattr(anthropic, "RateLimitError", None),
        getattr(anthropic, "InternalServerError", None),
    ) if e is not None
)


async def _with_retry(coro_factory, *, ticker: str, attempts: int = 3, base_backoff: float = 1.5):
    """Await coro_factory(), retrying transient Anthropic errors with exponential backoff.
    Re-raises the last error after `attempts` tries (caller then fail-closes). Non-transient
    errors (parse failures etc.) are NOT caught here — they propagate immediately."""
    last_exc = None
    for i in range(attempts):
        try:
            return await coro_factory()
        except _RETRYABLE_ERRORS as exc:
            last_exc = exc
            if i < attempts - 1:
                wait = base_backoff * (2 ** i)
                logger.warning(
                    "Advocate transient API error [%s] attempt %d/%d: %s — retrying in %.1fs",
                    ticker, i + 1, attempts, type(exc).__name__, wait,
                )
                await asyncio.sleep(wait)
    raise last_exc

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

ITM DIRECTIONAL DEBIT — MANDATORY CALIBRATION:
This applies ONLY to a single-leg long debit (LONG_CALL / LONG_PUT) whose leg is deep in-the-money: |delta| >= 0.70. Such premium is mostly INTRINSIC and largely vega-immune.
  • Do NOT flag "IV crush kills debit" or "overpaying for direction in high vol" at HIGH severity for these — an IV crush hits only the small extrinsic sleeve (an ~0.80-delta put loses ~5-7% to a vol crush vs ~35% for an ATM debit). Judge it on the DIRECTIONAL thesis and the underlying move required, not on the IV level.
  • INSTEAD scrutinize these ITM-specific risks (any can be HIGH): (1) REVERSAL/NOTIONAL — |delta| 0.70-0.85 moves ~1:1 with a large notional, so a counter-trend bounce loses fast; treat buying into an already-extended/oversold move (e.g. a put when RSI<35, or a call when RSI>65) as HIGH. (2) FILL QUALITY — deep-ITM strikes have wider bid-ask / lower OI; if bid-ask > 10% of mid or OI is thin, slippage erases the directional edge (HIGH). (3) DOLLARS-AT-RISK — max loss is the full (large) premium; size must be small.
  • An ATM/OTM debit (|delta| < 0.55) keeps the FULL normal IV-crush scrutiny — do NOT relax it there.

DTE CALIBRATION — MANDATORY:
The payload includes "today_date" and each leg includes "dte" (days to expiry, pre-computed). USE THESE FIELDS — do not compute DTE yourself. Common target DTE for credit spreads is 30-60 days.

MACRO-EVENT RISK — USE THE PROVIDED CONTEXT, DO NOT ASSUME:
The payload includes "event_risk". When it is not "none", it is the AUTHORITATIVE, quantified
macro-event assessment (the real event name, exact days-to-event, expected move, and this
structure's cushion/size-down) computed deterministically from the macro calendar. TRUST IT over
any assumption — do NOT invent or mis-identify an event (e.g. do not claim "FOMC tomorrow" when the
context says the event is NFP, or is days away). If event_risk says the structure is bounded,
cushioned, and size-reduced, the event risk is ALREADY ADDRESSED — do NOT raise a HIGH-severity
event failure mode on proximity alone. Only flag event risk as HIGH if the structure is genuinely
fragile to the stated expected move (e.g. short strike inside it) AND that isn't already mitigated.

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
        # Calibration haircut: prepend the agent's measured over/under-confidence so it
        # self-corrects (an over-confident advocate over-blocks and throttles entries).
        _cal_note = _load_cal_note(str(self._settings.db_path), "advocate")
        if _cal_note:
            lessons = [_cal_note] + lessons
        payload = self._build_payload(ticker, recommendation, thesis, positions, macro_context, lessons)
        t0 = time.monotonic()
        verdict: AdvocateVerdict | None = None
        raw_output: dict = {}

        # MCP tools: advocate can verify earnings dates, query its own history,
        # check recent news — reduces hallucinated BLOCK verdicts
        _db = str(self._settings.db_path)
        _tavily_key = getattr(self._settings, "tavily_api_key", None)
        # Drop web-search tools when no key — avoids a wasted tool-turn on a dead tool.
        _tools = SQLITE_TOOLS + FLOW_TOOLS + (SEARCH_TOOLS if _tavily_key else [])
        _handlers = {
            **sqlite_tool_handlers(_db),
            **flow_tool_handlers(),
            **(search_tool_handlers(_tavily_key) if _tavily_key else {}),
        }

        _cached_system = [{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}]
        try:
            # Retry transient API errors before the caller's fail-closed policy blocks the
            # trade — keeps the entry engine alive through momentary outages.
            response = await _with_retry(lambda: run_with_tools(
                client=self._client,
                model=_MODEL,
                system=_cached_system,
                messages=[{"role": "user", "content": _compress(payload)}],
                tools=_tools,
                handlers=_handlers,
                max_turns=3,
                max_tokens=4000,   # headroom: model often writes prose before the JSON
                thinking={"type": "disabled"},
                output_config={"effort": "medium"},
                timeout=anthropic.Timeout(connect=30.0, read=120.0, write=30.0, pool=30.0),
            ), ticker=ticker)
            latency_ms = int((time.monotonic() - t0) * 1000)
            in_tok  = response.usage.input_tokens  if response.usage else 0
            out_tok = response.usage.output_tokens if response.usage else 0
            cost    = (in_tok * 3.0 + out_tok * 15.0) / 1_000_000  # Sonnet 4.6

            text_blocks = [b for b in response.content if b.type == "text"]
            raw_text = text_blocks[-1].text.strip() if text_blocks else "{}"
            if raw_text.startswith("```"):
                raw_text = raw_text.split("```")[1].lstrip("json").strip()

            try:
                raw_output = _parse_json_robust(raw_text)
            except ValueError:
                # Parse-fail REGEN: the model occasionally ends its turn with prose analysis and
                # no JSON object (stop=end_turn, 7 cases on 2026-06-09), which fail-closes a valid
                # trade. A fresh generation almost always emits parseable JSON — retry ONCE; only
                # fail-close if the second attempt also can't be parsed.
                logger.warning(
                    "Advocate parse FAIL [%s] — regenerating | stop=%s blocks=%s text_len=%d raw=%r",
                    ticker, getattr(response, "stop_reason", "?"),
                    [b.type for b in response.content], len(raw_text), raw_text[:300],
                )
                response = await _with_retry(lambda: run_with_tools(
                    client=self._client,
                    model=_MODEL,
                    system=_cached_system,
                    messages=[{"role": "user", "content": _compress(payload)}],
                    tools=_tools,
                    handlers=_handlers,
                    max_turns=3,
                    max_tokens=4000,
                    thinking={"type": "disabled"},
                    output_config={"effort": "medium"},
                    timeout=anthropic.Timeout(connect=30.0, read=120.0, write=30.0, pool=30.0),
                ), ticker=ticker)
                _rtb = [b for b in response.content if b.type == "text"]
                raw_text = _rtb[-1].text.strip() if _rtb else "{}"
                if raw_text.startswith("```"):
                    raw_text = raw_text.split("```")[1].lstrip("json").strip()
                try:
                    raw_output = _parse_json_robust(raw_text)
                except ValueError:
                    logger.warning(
                        "Advocate parse FAIL [%s] AFTER regen — fail-closed | raw=%r",
                        ticker, raw_text[:600],
                    )
                    raise
            # Enforce deterministic verdict from failure mode analysis
            _kill_conditions = [c for c in (getattr(thesis, "kill_conditions", []) or [])]
            raw_output["verdict"] = _compute_verdict(
                raw_output.get("failure_modes", []), _kill_conditions,
            )
            verdict = _parse_verdict(raw_output)

            # Fact-grounding GATE — verify the advocate's stated event claims against the macro
            # calendar and, if it fabricated an imminent event (the "FOMC tomorrow" class), strip
            # the failure mode(s) built on that lie and recompute the verdict. A BLOCK manufactured
            # purely by a phantom event downgrades; genuine risks still block.
            verdict = self._apply_fact_gate(raw_output, verdict, _kill_conditions, ticker)

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
            # Cost-efficient degraded review (2026-06-09): a Sonnet-specific failure (overload,
            # timeout, rate limit) should NOT fail-close a trade when a cheap Haiku pass can still
            # review it. This keeps the gate alive (and far cheaper) through model-specific blips.
            # CAVEAT: an account-wide spend cap / zero-credit state blocks ALL models, so this
            # cannot recover that class (~94% of historical errors) — keep the account funded.
            try:
                _fb_model = getattr(self._settings, "claude_fast_model", "claude-haiku-4-5-20251001")
                response = await run_with_tools(
                    client=self._client,
                    model=_fb_model,
                    system=_cached_system,
                    messages=[{"role": "user", "content": _compress(payload)}],
                    tools=_tools,
                    handlers=_handlers,
                    max_turns=2,
                    max_tokens=3000,
                    thinking={"type": "disabled"},
                    output_config={"effort": "low"},
                    timeout=anthropic.Timeout(connect=20.0, read=90.0, write=20.0, pool=20.0),
                )
                _fb = [b for b in response.content if b.type == "text"]
                raw_text = _fb[-1].text.strip() if _fb else "{}"
                if raw_text.startswith("```"):
                    raw_text = raw_text.split("```")[1].lstrip("json").strip()
                raw_output = _parse_json_robust(raw_text)
                _kill_conditions = [c for c in (getattr(thesis, "kill_conditions", []) or [])]
                raw_output["verdict"] = _compute_verdict(
                    raw_output.get("failure_modes", []), _kill_conditions,
                )
                verdict = _parse_verdict(raw_output)
                verdict = self._apply_fact_gate(raw_output, verdict, _kill_conditions, ticker)
                self._write_journal(
                    decision_id, ticker, verdict, raw_output,
                    response.usage.input_tokens if response.usage else 0,
                    response.usage.output_tokens if response.usage else 0,
                    0.0, latency_ms,
                )
                logger.info("Advocate [%s] %s via HAIKU fallback (Sonnet failed: %s)",
                            ticker, verdict.verdict, str(exc)[:70])
            except Exception as fexc:
                logger.warning("AdvocateAgent failed for %s (Sonnet: %s | Haiku fb: %s)",
                               ticker, str(exc)[:60], str(fexc)[:60])
                self._write_journal(decision_id, ticker, None, {"error": str(exc)},
                                    0, 0, 0.0, latency_ms)

        return verdict

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _apply_fact_gate(self, raw_output: dict, verdict: "AdvocateVerdict",
                         kill_conditions: list, ticker: str) -> "AdvocateVerdict":
        """Run the fact-grounding scan; if the advocate fabricated an imminent macro event,
        neutralize the failure mode(s) built on it and recompute the deterministic verdict.
        Only ever relaxes a verdict (a lie can only manufacture risk, never hide it). Never
        raises — a guardrail must not break the path it guards."""
        try:
            from agora.ops.fact_grounding import scan as _fact_scan, neutralize_fabricated_modes
            modes = raw_output.get("failure_modes") or []
            _claim_text = (raw_output.get("most_likely_loss_scenario", "") or "") + " " + \
                (getattr(verdict, "verdict_reasoning", "") or "") + " " + " ".join(
                    f"{fm.get('mode_name','')} {fm.get('trigger_conditions','')}"
                    for fm in modes if isinstance(fm, dict))
            divs = _fact_scan(_claim_text, str(self._settings.db_path),
                              source="advocate", ticker=ticker)
            if not divs:
                return verdict
            clean_modes, removed = neutralize_fabricated_modes(modes, divs)
            if not removed:
                return verdict
            regrounded = _compute_verdict(clean_modes, kill_conditions)
            if regrounded != verdict.verdict:
                logger.critical(
                    "Advocate FACT-GATE [%s]: %s → %s — dropped fabricated-event failure mode(s) %s",
                    ticker, verdict.verdict, regrounded, removed,
                )
                raw_output["verdict"] = regrounded
                raw_output["fact_gate"] = {
                    "from": verdict.verdict, "to": regrounded,
                    "removed_modes": removed,
                    "reason": "; ".join(d.get("issue", "") for d in divs)[:300],
                }
                return _parse_verdict(raw_output)
            # Fabricated mode found but verdict unchanged (other genuine risks stand) — record it.
            raw_output["fact_gate"] = {"from": verdict.verdict, "to": verdict.verdict,
                                       "removed_modes": removed, "verdict_held": True}
        except Exception as exc:
            logger.debug("advocate fact-gate skipped [%s]: %s", ticker, exc)
        return verdict

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
            # Defensive getattr — _build_payload runs OUTSIDE review()'s try block, so any missing
            # attribute here raises straight out of review() → caller sees None → fail-closed BLOCK
            # with no journal row. The long-options path passes a lightweight SimpleNamespace thesis
            # that lacks scorecard_critique/reasoning_trace; accessing them directly silently blocked
            # every score-2 long-options entry (~110/day). Never hard-fail the gate on a thin thesis.
            thesis_summary = {
                "direction":       getattr(thesis, "direction", None),
                "magnitude_pct":   getattr(thesis, "magnitude_pct", None),
                "horizon_days":    getattr(thesis, "horizon_days", None),
                "confidence_pct":  getattr(thesis, "confidence_pct", None),
                "strategy_family": getattr(thesis, "strategy_family", None),
                "kill_conditions": getattr(thesis, "kill_conditions", []),
                "scorecard_critique": getattr(thesis, "scorecard_critique", None),
                "reasoning_trace": getattr(thesis, "reasoning_trace", None),
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
            # Accurate, quantified macro-event context from the surgical gate (real event,
            # days-to-event, expected move, structural cushion). Authoritative — overrides any
            # assumption about event dates.
            "event_risk": getattr(recommendation, "event_mitigation", "") or "none",
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

    # 1. Whole string is the object.
    try:
        obj = json.loads(text)
        if _valid(obj):
            return obj
    except json.JSONDecodeError:
        pass
    # 2. The model commonly wraps the verdict in prose and/or a ```json fence
    #    ("Here is my verdict:\n```json\n{...}\n```"). Extract the first balanced {...}
    #    object embedded ANYWHERE in the text (brace-counting, string-aware).
    obj = _extract_balanced_object(text, _valid)
    if obj is not None:
        return obj
    # 3. Truncation recovery: from the first '{', longest prefix ending in '}' that parses.
    start = text.find("{")
    if start != -1:
        for end in range(len(text), start, -1):
            if text[end - 1] == '}':
                try:
                    obj = json.loads(text[start:end])
                except json.JSONDecodeError:
                    continue
                if _valid(obj):
                    return obj
    raise ValueError("advocate output not parseable into a verdict object with failure_modes")


def _extract_balanced_object(text: str, validator=None) -> dict | None:
    """Return the first complete, balanced {...} JSON object embedded in text that satisfies
    `validator` (or any object if validator is None), else None. Brace counting ignores braces
    inside JSON strings. Scans ALL '{' positions — so a literal `{}` in the prose (e.g. the
    model writing "macro thesis object is empty `{}`") BEFORE the real verdict no longer traps
    the match on the wrong object (the 2026-06-09 advocate parse-FAIL class)."""
    i = 0
    while True:
        start = text.find("{", i)
        if start == -1:
            return None
        depth = 0
        in_str = False
        esc = False
        for j in range(start, len(text)):
            c = text[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:j + 1])
                        if validator is None or validator(obj):
                            return obj
                    except json.JSONDecodeError:
                        pass
                    break  # this balanced block is parsed-invalid/undecodable — try the next '{'
        i = start + 1


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

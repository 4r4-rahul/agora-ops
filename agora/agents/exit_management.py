"""
agora/agents/exit_management.py — ExitIntelligenceAgent (Phase 6, spec §13.4).

Hourly evaluation of each open position. Compares the original entry thesis
against current market state and recommends:
  HOLD | TIGHTEN_STOP | TAKE_PARTIAL | CLOSE_NOW | ROLL

HARD CONSTRAINTS (non-negotiable, §17 Sacred Rules):
  - Cannot override PositionManager floors (50% profit, 21 DTE, 2× stop).
  - Can recommend EARLIER exits. Never later.
  - ROLL triggers a full re-evaluation via StockAnalystAgent (not done here).

Shadow mode (default):
  Runs on every open position, journals recommendations, PositionManager ignores.
  After promoting to live, compare counterfactual P&L vs floors-only baseline.

Live mode:
  CLOSE_NOW triggers position close via the on_close callback.
  TIGHTEN_STOP and TAKE_PARTIAL are advisory (logged, no automated action yet).

Model: reads settings.claude_model (paper: sonnet-4-6 ~$0.024/call; production: opus-4-8 ~$0.04/call)
Cost: ~$0.024/call × 4 positions × 3 checks/day (2h interval) ≈ $0.29/day paper mode
Schema: exit_journal — managed by migrations/2026_05_phase2_journals.sql

System prompt: §13.4 of AGORA Grand Specification v1.0
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import anthropic

from agora.core.json_extract import extract_json as _extract_json
from agora.ops.lessons_store import load_approved_lessons as _load_lessons
from agora.ops.lessons_store import load_calibration_note as _load_cal_note
from agora.ops.llm_cost_log import log_message as _log_msg
from agora.ops.payload_compressor import compress_payload as _compress

logger = logging.getLogger(__name__)

PROMPT_VERSION = "1.0.0"
_MODEL_FALLBACK = "claude-sonnet-4-6"  # used only if settings not available

# Forced structured output: the model returns the decision as this tool's input, which the API
# validates against the schema — guaranteeing valid JSON. Replaces free-text + _extract_json, which
# was crashing ~80% of TSM/TXN evals on malformed/empty JSON ("Expecting ',' line 12" / "char 0").
_EXIT_DECISION_TOOL = {
    "name": "exit_decision",
    "description": "Return the structured exit decision for the open position.",
    "input_schema": {
        "type": "object",
        "properties": {
            "thesis_validity": {"type": "string", "enum": ["VALID", "WEAKENING", "INVALIDATED"]},
            "thesis_validity_reasoning": {"type": "string"},
            "kill_condition_status": {"type": "string",
                                      "enum": ["NONE_TRIGGERED", "APPROACHING", "TRIGGERED"]},
            "recommendation": {"type": "string",
                               "enum": ["HOLD", "TIGHTEN_STOP", "TAKE_PARTIAL", "CLOSE_NOW", "ROLL"]},
            "recommendation_reasoning": {"type": "string"},
            "specific_action": {"type": "object",
                                "description": "e.g. {\"close_reason\": \"...\"} when CLOSE_NOW"},
            "confidence_pct": {"type": "integer"},
            "key_risks_to_watch": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["thesis_validity", "kill_condition_status", "recommendation",
                     "recommendation_reasoning"],
    },
}

# ── System prompt (spec §13.4) ────────────────────────────────────────────────

_SYSTEM = """You are an options position manager. You receive the ORIGINAL THESIS from entry plus CURRENT STATE. Compare them. Recommend optimal action.

You are NOT trying to maximize P&L on this trade. You are optimizing ACROSS trades — sometimes closing a winner early frees capital for the next setup.

THE FUNDAMENTAL QUESTION: "Is the reason I opened this position still true?"

YOU ARE NOT ALONE. The payload gives you the ENTRY BRAIN'S FRESH READ on this exact ticker,
re-run RIGHT NOW (`entry_brain_now`: direction / confidence / reasoning), plus the live market
signals it used (`current_signals`: GEX, options flow, IV-rank, momentum, recent news). Use them
as GROUND TRUTH — do NOT guess whether the thesis still holds:
  - entry_brain_now AGREES with the original direction (and confident) → thesis VALID → bias HOLD.
  - entry_brain_now has FLIPPED or gone neutral/low-confidence → thesis WEAKENING/INVALIDATED.
  - current_signals confirm the original driver (e.g. flow/GEX still supportive) → VALID.
  - current_signals show the driver reversed (vol spike against you, flow flipped) → INVALIDATED.
This is the two brains talking. When entry_brain_now and the live signals BOTH still support the
trade, you must NOT close a winner on a soft "feels weak" — that is the over-confidence error you
are calibrated against. Only close when the FRESH evidence says the reason is genuinely gone.

- YES + not at target → HOLD
- YES + partial target hit → TAKE_PARTIAL or TIGHTEN_STOP
- NO (thesis invalidated) → CLOSE_NOW, even at a loss
- KILL_CONDITION TRIGGERED → CLOSE_NOW, mandatory
- YES + DTE approaching + profitable but not at target → ROLL (escalate)
- Thesis FULLY PLAYED OUT (target reached) → CLOSE_NOW

HARD CONSTRAINTS:
C1. You CANNOT override the deterministic exit floors. The actual auto-close rules are:
    - LOCK_IN: 85% of max profit → always closes, no exceptions (last 15% not worth gamma risk)
    - 21-DTE mandatory close (position_manager enforces before you are called)
    - Hard stop: 2× entry credit/debit (position_manager enforces)
    - Credit spread flat target: 50% of max profit (position_manager profit engine)
    - Short-DTE event pillars: 75% flat target (sector_momentum, event_fomc/cpi, catalyst)
    - Ratchet stop: dynamic floor that rises with HWM (protects locked-in gains)
C2. You CAN recommend EARLIER exits than the floors. Never later.
C3. You CANNOT recommend "hold past 21 DTE" or "ignore the stop."
C4. ROLL requires a new thesis review — if you recommend ROLL, state exactly what new thesis to verify.
C5. Read original kill_conditions verbatim. ANY triggered → CLOSE_NOW.

METHODOLOGY:
Step 1: KILL CONDITION CHECK — if any triggered, CLOSE_NOW immediately. State which one.
Step 2: THESIS VALIDITY — VALID / WEAKENING / INVALIDATED
  VALID: direction still intact, catalyst still relevant, regime unchanged
  WEAKENING: direction intact but one driver has faded
  INVALIDATED: price has moved against thesis, key driver reversed, regime shifted
Step 3: P&L STATE — % of max profit / max loss, time remaining, spread type
Step 4: NEW INFORMATION — significant macro shift? sector rotation? vol spike?
Step 5: TIME DECAY ECONOMICS — credit spread: theta works for us (target 50%); debit spread: we pay theta (take directional profit early on DTE-curve)
Step 6: RECOMMENDATION SELECTION

ANTI-PATTERNS:
A1. "Hope holding" — staying in a WEAKENING thesis because you don't want a loss
A2. "Premature profit taking" — closing at 25% when thesis is VALID and horizon remains
A3. "Roll for the sake of rolling" — never roll a loser to hide the loss
A4. "Ignoring kill conditions" — if they triggered, the trade thesis is over

OUTPUT — return exactly this JSON, no markdown, no prose:

{
  "thesis_validity": "VALID | WEAKENING | INVALIDATED",
  "thesis_validity_reasoning": "explicit original-vs-current comparison (2-3 sentences)",
  "kill_condition_status": "NONE_TRIGGERED | TRIGGERED:<which condition exactly>",
  "recommendation": "HOLD | TIGHTEN_STOP | TAKE_PARTIAL | CLOSE_NOW | ROLL",
  "recommendation_reasoning": "3-4 sentences explaining the recommendation",
  "specific_action": {
    "close_reason": "string (if CLOSE_NOW)",
    "partial_pct": 50,
    "new_stop_pct": null,
    "roll_note": "string (if ROLL — what new thesis to verify)"
  },
  "confidence_pct": <integer 40-90>,
  "key_risks_to_watch": ["string", "string"]
}"""

# Prompt-cache the ~4.4KB static system prompt (ephemeral): the exit agent runs on the top tier
# per open position every cycle, so the same large system text was re-billed at full input rate on
# every call. Mirrors the decision agents (strategy_selector/advocate). LLM-spend audit 2026-06-22.
_CACHED_SYSTEM = [{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}]


# ── Output dataclass ──────────────────────────────────────────────────────────

@dataclass
class ExitRecommendation:
    thesis_validity:          str              # 'VALID' | 'WEAKENING' | 'INVALIDATED'
    thesis_validity_reasoning: str
    kill_condition_status:    str
    recommendation:           str              # 'HOLD' | 'TIGHTEN_STOP' | 'TAKE_PARTIAL' | 'CLOSE_NOW' | 'ROLL'
    recommendation_reasoning: str
    specific_action:          dict
    confidence_pct:           int
    key_risks:                list[str]
    raw:                      dict = field(default_factory=dict)

    @property
    def should_close(self) -> bool:
        return self.recommendation == "CLOSE_NOW"

    @property
    def kill_triggered(self) -> bool:
        return self.kill_condition_status.startswith("TRIGGERED")


# ── Agent ─────────────────────────────────────────────────────────────────────

class ExitIntelligenceAgent:
    """
    Hourly per-position thesis validity checker.

    Usage in session.py: a background task calls evaluate() for each open position
    every hour during market hours. The result is journaled. In live mode, CLOSE_NOW
    triggers the on_close_callback to submit a market order via IBKR.

    Shadow mode: recommendations are journaled but on_close_callback is never invoked.
    """

    def __init__(
        self,
        settings:            Any,
        shadow_mode:         bool = True,
        on_close_callback:   Callable[[str, str], Awaitable[None]] | None = None,
    ) -> None:
        self._settings           = settings
        self._shadow_mode        = shadow_mode
        self._on_close           = on_close_callback
        self._client             = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        self._model              = getattr(settings, "claude_model", _MODEL_FALLBACK)
        self._last_evaluated:    dict[str, datetime] = {}  # position_id → last eval time
        # Signal provider (set by the session): async fn(position) -> dict with the CURRENT
        # market read at exit time — live GEX/flow/IV/momentum + the entry brain's FRESH thesis
        # ("if I analyzed this ticker now, what would I conclude?"). This is what lets the exit
        # brain judge against current reality + talk to the entry brain, instead of guessing
        # "new information" from only the stale entry thesis + P&L (the 79%-predicted/42%-
        # delivered calibration gap).
        self._signal_provider: Callable[[Any], Awaitable[dict]] | None = None
        logger.info("ExitIntelligenceAgent ready: model=%s shadow=%s", self._model, shadow_mode)

    def set_signal_provider(self, provider: Callable[[Any], Awaitable[dict]]) -> None:
        """Inject the session's current-signals + fresh-entry-brain-read provider."""
        self._signal_provider = provider

    @property
    def shadow_mode(self) -> bool:
        return self._shadow_mode

    @shadow_mode.setter
    def shadow_mode(self, value: bool) -> None:
        if self._shadow_mode != value:
            logger.info("ExitIntelligenceAgent shadow_mode %s → %s", self._shadow_mode, value)
        self._shadow_mode = value

    async def evaluate(
        self,
        position:      Any,          # OpenPosition
        macro_context: Any | None,
        thesis_override: dict | None = None,
        act:             bool = True,
    ) -> ExitRecommendation | None:
        """
        Evaluate one open position.
        Returns ExitRecommendation in both shadow and live modes.
        In live mode + CLOSE_NOW: invokes on_close_callback (unless act=False).
        Never raises — failures return None.

        thesis_override: supply the original thesis directly (e.g. long options, whose
            thesis lives in long_journal not analyst_journal) instead of the DB lookup.
        act: when False, the agent returns its recommendation WITHOUT invoking the close
            callback — the caller owns execution (used by the long-options loop so it can
            close via its own path and update the signal-stats learning loop).
        """
        position_id = position.position_id
        original_thesis = thesis_override if thesis_override is not None \
            else self._fetch_original_thesis(position_id)
        lessons = _load_lessons(str(self._settings.db_path), "exit")
        # Calibration haircut: feed the exit agent its measured over-confidence (it predicts
        # ~79% right, delivers ~42%) so it stops closing winners on weak thesis-break calls.
        _cal_note = _load_cal_note(str(self._settings.db_path), "exit")
        if _cal_note:
            lessons = [_cal_note] + lessons
        # Gather the CURRENT market read + the entry brain's FRESH thesis (fail-open — the exit
        # brain still works on the base payload if this errors, so it can never break exits).
        current_context: dict = {}
        if self._signal_provider is not None:
            try:
                current_context = await self._signal_provider(position) or {}
            except Exception as _sig_exc:
                logger.debug("Exit signal provider failed for %s: %s", position.ticker, _sig_exc)
        payload = self._build_payload(position, original_thesis, macro_context, lessons, current_context)
        decision_id = self._fetch_chain_id(position_id)

        t0 = time.monotonic()
        rec: ExitRecommendation | None = None
        raw_output: dict = {}

        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=2000,
                system=_CACHED_SYSTEM,
                messages=[{"role": "user", "content": _compress(payload)}],
                tools=[_EXIT_DECISION_TOOL],
                tool_choice={"type": "tool", "name": "exit_decision"},
                timeout=anthropic.Timeout(connect=30.0, read=60.0, write=30.0, pool=30.0),
            )
            latency_ms = int((time.monotonic() - t0) * 1000)
            in_tok  = response.usage.input_tokens  if response.usage else 0
            out_tok = response.usage.output_tokens if response.usage else 0
            cost    = (in_tok * 3.0 + out_tok * 15.0) / 1_000_000   # Sonnet 4.6

            # Forced-tool structured output: the decision IS the validated tool input — guaranteed
            # valid JSON (no more malformed/empty-JSON parse crashes). Fall back to text + robust
            # extraction only if no tool block is returned.
            tool_blocks = [b for b in response.content if b.type == "tool_use"]
            if tool_blocks:
                raw_output = tool_blocks[0].input or {}
            else:
                text_blocks = [b for b in response.content if b.type == "text"]
                raw_output = _extract_json(text_blocks[-1].text if text_blocks else "{}")
            rec = _parse_recommendation(raw_output)

            # Fact-grounding monitor — verify the exit agent's event claims vs the calendar.
            try:
                from agora.ops.fact_grounding import scan as _fact_scan
                _claim_text = " ".join([
                    rec.recommendation_reasoning or "", rec.thesis_validity_reasoning or "",
                    " ".join(rec.key_risks or []),
                ])
                _fact_scan(_claim_text, str(self._settings.db_path),
                           source="exit", ticker=position.ticker)
            except Exception:
                pass

            try:
                _log_msg(str(self._settings.db_path), "ExitIntelligenceAgent", self._model,
                         response.usage, purpose=f"exit_{position.ticker}",
                         trace_id=decision_id)
            except Exception:
                pass

            self._write_journal(decision_id, position, rec, raw_output,
                                in_tok, out_tok, cost, latency_ms)
            self._last_evaluated[position_id] = datetime.now(tz=UTC)

            logger.info(
                "ExitAgent [%s/%s] %s | validity=%s | kill=%s%s",
                position.ticker, position_id[:8],
                rec.recommendation, rec.thesis_validity,
                rec.kill_condition_status,
                " [SHADOW]" if self._shadow_mode else "",
            )

            # In live mode: act on CLOSE_NOW (skipped when act=False — caller owns close)
            if act and rec.should_close and not self._shadow_mode and self._on_close:
                close_reason = rec.specific_action.get("close_reason", rec.recommendation_reasoning)
                logger.warning(
                    "ExitAgent CLOSE_NOW: %s | reason=%s | kill=%s",
                    position.ticker, close_reason[:100], rec.kill_condition_status,
                )
                try:
                    await self._on_close(position_id, close_reason)
                except Exception as close_exc:
                    logger.error("ExitAgent close callback failed: %s", close_exc)

        except Exception as exc:
            latency_ms = int((time.monotonic() - t0) * 1000)
            logger.warning("ExitIntelligenceAgent failed for %s: %s", position.ticker, exc)
            self._write_journal(decision_id, position, None, {"error": str(exc)},
                                0, 0, 0.0, latency_ms)

        return rec

    def should_evaluate(self, position_id: str, interval_hours: float = 1.0) -> bool:
        """True if enough time has passed since last evaluation."""
        last = self._last_evaluated.get(position_id)
        if last is None:
            return True
        elapsed = (datetime.now(tz=UTC) - last).total_seconds()
        return elapsed >= interval_hours * 3600

    # ── DB helpers ────────────────────────────────────────────────────────────

    def _fetch_original_thesis(self, position_id: str) -> dict:
        """Fetch original analyst thesis via decision_chains → analyst_journal FK."""
        try:
            with sqlite3.connect(str(self._settings.db_path)) as conn:
                chain = conn.execute(
                    "SELECT chain_id FROM decision_chains WHERE position_id = ?",
                    (position_id,),
                ).fetchone()
                if not chain:
                    return {}
                row = conn.execute(
                    """SELECT direction, magnitude_pct, horizon_days, confidence_pct,
                              strategy_family, kill_conditions_json, reasoning_trace,
                              decided_at_utc
                       FROM analyst_journal WHERE decision_id = ?
                       ORDER BY journal_id DESC LIMIT 1""",
                    (chain[0],),
                ).fetchone()
                if not row:
                    return {}
                return {
                    "direction":       row[0],
                    "magnitude_pct":   row[1],
                    "horizon_days":    row[2],
                    "confidence_pct":  row[3],
                    "strategy_family": row[4],
                    "kill_conditions": json.loads(row[5] or "[]"),
                    "reasoning_trace": row[6],
                    "thesis_date":     row[7],
                }
        except Exception:
            return {}

    def _fetch_chain_id(self, position_id: str) -> str:
        """Get the decision chain_id linked to this position."""
        try:
            with sqlite3.connect(str(self._settings.db_path)) as conn:
                row = conn.execute(
                    "SELECT chain_id FROM decision_chains WHERE position_id = ?",
                    (position_id,),
                ).fetchone()
                return row[0] if row else ""
        except Exception:
            return ""

    # ── Payload builder ───────────────────────────────────────────────────────

    def _build_payload(
        self,
        position:        Any,
        original_thesis: dict,
        macro_context:   Any | None,
        lessons:         list[str] | None = None,
        current_context: dict | None = None,
    ) -> dict:
        today = date.today()
        dte_remaining = (position.expiry_date - today).days
        pnl_pct_of_max = 0.0
        if position.max_gain_dollars and position.max_gain_dollars > 0:
            pnl_pct_of_max = (position.unrealized_pnl / position.max_gain_dollars) * 100
        elif position.max_loss_dollars and position.max_loss_dollars > 0:
            pnl_pct_of_max = (position.unrealized_pnl / abs(position.max_loss_dollars)) * 100

        legs = [
            {"action": lg.action, "type": lg.option_type,
             "strike": lg.strike, "expiry": str(lg.expiration),
             "delta": lg.delta, "theta": lg.theta, "vega": lg.vega}
            for lg in position.legs
        ]

        return {
            "position_id":    position.position_id,
            "ticker":         position.ticker,
            "strategy":       str(getattr(position.strategy, "value", position.strategy)),
            "direction":      position.direction,
            "entry_date":     str(position.entry_date),
            "entry_price":    position.entry_price,
            "current_price":  position.current_price,
            "expiry_date":    str(position.expiry_date),
            "dte_remaining":  dte_remaining,
            "legs":           legs,
            "unrealized_pnl": round(position.unrealized_pnl, 2),
            "pnl_pct_of_max": round(pnl_pct_of_max, 1),
            "max_profit":     position.max_gain_dollars,
            "max_loss":       position.max_loss_dollars,
            "conviction_at_entry": position.conviction_at_entry,
            "regime_at_entry":    position.regime_at_entry,
            "original_thesis": original_thesis,
            "macro_current": {
                "stance":     macro_context.macro_stance    if macro_context else "unknown",
                "confidence": macro_context.confidence      if macro_context else None,
                "key_risk":   macro_context.key_risk        if macro_context else None,
            },
            "evaluation_time": datetime.now(tz=UTC).isoformat(),
            "approved_lessons": lessons or [],
            # CURRENT market read at exit time (vs the stale entry thesis above) — the live
            # signals + the entry brain's FRESH thesis. Empty if the provider isn't wired.
            "current_signals":  (current_context or {}).get("current_signals", {}),
            "entry_brain_now":  (current_context or {}).get("entry_brain_now", {}),
        }

    def _write_journal(
        self,
        decision_id: str,
        position:    Any,
        rec:         ExitRecommendation | None,
        raw_output:  dict,
        in_tok:      int,
        out_tok:     int,
        cost:        float,
        latency_ms:  int,
    ) -> None:
        pnl_pct = None
        if position.max_gain_dollars and position.max_gain_dollars > 0:
            pnl_pct = round(position.unrealized_pnl / position.max_gain_dollars, 4)
        try:
            with sqlite3.connect(str(self._settings.db_path)) as conn:
                conn.execute(
                    """INSERT INTO exit_journal (
                        decision_id, position_id, ticker, decided_at_utc,
                        prompt_version, model, payload_json,
                        thesis_validity, kill_condition_status, recommendation,
                        recommendation_reasoning, specific_action_json,
                        confidence_pct, output_full_json,
                        input_tokens, output_tokens, cost_usd, latency_ms, shadow_mode,
                        pnl_pct_of_max
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        decision_id or "",
                        position.position_id,
                        position.ticker,
                        datetime.now(tz=UTC).isoformat(),
                        PROMPT_VERSION, self._model,
                        json.dumps({"ticker": position.ticker}, default=str),
                        rec.thesis_validity           if rec else "error",
                        rec.kill_condition_status     if rec else "unknown",
                        rec.recommendation            if rec else "error",
                        rec.recommendation_reasoning  if rec else "",
                        json.dumps(rec.specific_action if rec else {}, default=str),
                        rec.confidence_pct            if rec else None,
                        json.dumps(raw_output, default=str),
                        in_tok, out_tok, round(cost, 6), latency_ms,
                        1 if self._shadow_mode else 0,
                        pnl_pct,
                    ),
                )
        except Exception as exc:
            logger.debug("exit_journal write error: %s", exc)


# ── Pure helpers ──────────────────────────────────────────────────────────────

def _parse_recommendation(raw: dict) -> ExitRecommendation:
    return ExitRecommendation(
        thesis_validity=raw.get("thesis_validity", "VALID"),
        thesis_validity_reasoning=raw.get("thesis_validity_reasoning", ""),
        kill_condition_status=raw.get("kill_condition_status", "NONE_TRIGGERED"),
        recommendation=raw.get("recommendation", "HOLD"),
        recommendation_reasoning=raw.get("recommendation_reasoning", ""),
        specific_action=raw.get("specific_action", {}),
        confidence_pct=raw.get("confidence_pct", 60),
        key_risks=raw.get("key_risks_to_watch", []),
        raw=raw,
    )

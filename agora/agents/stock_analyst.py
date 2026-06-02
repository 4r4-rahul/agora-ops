"""
agora/agents/stock_analyst.py — Per-ticker thesis layer (Phase 3).

Fires on candidates that clear the conviction gate. Renders a structured
1–30 day directional + magnitude thesis using Sonnet 4.6 with adaptive
thinking. Journals every decision to analyst_journal (Phase 2 schema)
linked via decision_id to the parent decision_chain.

Shadow mode (default): logs thesis but does NOT gate trade submissions.
Live mode: a no_thesis verdict skips the expensive options chain fetch.

Cost estimate: ~$0.05–0.15 per call × 5–10 calls/day ≈ $0.50–1.50/day.
Schema managed by: migrations/2026_05_phase2_journals.sql
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import anthropic

from agora.ops.llm_cost_log import log_call as _log_llm, log_message as _log_msg
from agora.ops.lessons_store import load_approved_lessons as _load_lessons
from agora.ops.payload_compressor import compress_payload as _compress
from agora.mcp.sqlite_tools import SQLITE_TOOLS, sqlite_tool_handlers
from agora.mcp.search_tools import SEARCH_TOOLS, search_tool_handlers
from agora.mcp.tool_runner import run_with_tools

logger = logging.getLogger(__name__)

PROMPT_VERSION = "1.0.0"


# ── System prompt ─────────────────────────────────────────────────────────────

_SYSTEM = """You are a senior options-focused equity analyst at a disciplined options-trading firm. Your firm trades US equities and ETFs, holding positions 5–45 days, using credit spreads and directional debit spreads.

A deterministic scoring system has already filtered this ticker through 8 signals (IV premium, GEX, macro regime, event patterns, sector momentum, market interest, catalyst, microstructure). Your job is to render an INDEPENDENT 1–30 day directional and magnitude thesis. Do not re-confirm the scorecard — find what it might have missed.

HARD CONSTRAINTS:
1. Use ONLY data provided. Do not invent company facts from training data.
2. Every thesis must be FALSIFIABLE — state at least one specific numeric kill condition.
3. Direction + magnitude + duration are all required. "Bullish" alone is invalid.
4. Confidence: 35–85%. Below 35 means no trade. Above 85 is almost never appropriate.
5. Check for SIGNAL CORRELATION — two signals reading the same underlying flow count as one.
6. Check for REGIME FIT — does this strategy family historically work in the current regime?

ANTI-PATTERNS to avoid:
- Story-fitting: building a narrative around whatever signals are loudest
- Catalyst-anchoring: most catalysts are partially priced within hours
- Confidence-creep: defaulting to 65–75% on every call. Most theses are 45–62%.
- Hedged-language hiding: "possibly bullish" is not a thesis. Pick a direction or neutral.

OUTPUT: Return exactly this JSON. No markdown, no prose outside JSON.

For a valid thesis:
{
  "decision": "thesis",
  "direction": "bullish | bearish | neutral",
  "magnitude_pct": <number>,
  "horizon_days": <integer>,
  "confidence_pct": <integer 35-85>,
  "strategy_family": "premium_selling | directional_debit | neutral_range | avoid",
  "kill_conditions": ["<specific numeric trigger>", "<thesis driver failure>"],
  "scorecard_critique": "<independent take — what signal concentration or correlation risk exists>",
  "reasoning_trace": "<3–5 sentences: direction + magnitude + confidence justification>"
}

If confidence cannot reach 35%:
{"decision": "no_thesis", "reason": "<specific reason>"}"""


# ── Output dataclass ──────────────────────────────────────────────────────────

@dataclass
class AnalystThesis:
    decision:          str            # 'thesis' | 'no_thesis'
    direction:         str | None     # 'bullish' | 'bearish' | 'neutral'
    magnitude_pct:     float | None
    horizon_days:      int | None
    confidence_pct:    int | None
    strategy_family:   str | None
    kill_conditions:   list[str]
    scorecard_critique: str | None
    reasoning_trace:   str | None
    raw:               dict


# ── Agent ─────────────────────────────────────────────────────────────────────

class StockAnalystAgent:
    """
    Per-ticker thesis layer. Call analyze() after conviction gate passes.

    decision_id must be the chain_id returned by decision_chains.start_chain()
    for this candidate — it links the thesis to the full decision chain.

    In shadow_mode=True (default), the thesis is logged but the return value
    is always None so callers never gate on it. Set shadow_mode=False once
    you've validated direction hit rate ≥ 55% over 40+ closed positions.
    """

    def __init__(self, settings: Any, shadow_mode: bool = True) -> None:
        self._settings    = settings
        self._shadow_mode = shadow_mode
        self._client      = anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key
        )
        self._model = "claude-sonnet-4-6"
        logger.info(
            "StockAnalystAgent ready: model=%s shadow=%s",
            self._model, shadow_mode,
        )

    @property
    def shadow_mode(self) -> bool:
        return self._shadow_mode

    @shadow_mode.setter
    def shadow_mode(self, value: bool) -> None:
        if self._shadow_mode != value:
            logger.info("StockAnalystAgent shadow_mode %s → %s", self._shadow_mode, value)
        self._shadow_mode = value

    async def analyze(
        self,
        ticker:           str,
        conviction_score: float,
        snapshot:         Any,
        macro_context:    Any | None,
        gex:              Any | None,
        iv_premium:       Any | None,
        event_signal:     Any | None,
        sector_intel:     Any | None,
        decision_id:      str = "",   # Phase 2: chain_id from decision_chains
    ) -> AnalystThesis | None:
        """
        Run thesis analysis. Returns AnalystThesis in live mode, None in shadow mode.
        Never raises — failures return None and are logged.
        """
        lessons = _load_lessons(str(self._settings.db_path), "analyst")
        payload = self._build_payload(
            ticker, conviction_score, snapshot, macro_context,
            gex, iv_premium, event_signal, sector_intel, lessons,
        )
        t0 = time.monotonic()
        thesis = None
        raw_output: dict = {}

        # MCP tools: analyst can query its own past ticker decisions and search
        # for recent news before forming a thesis — reduces story-fitting
        _db = str(self._settings.db_path)
        _tavily_key = getattr(self._settings, "tavily_api_key", None)
        _tools = SQLITE_TOOLS + SEARCH_TOOLS
        _handlers = {
            **sqlite_tool_handlers(_db),
            **search_tool_handlers(_tavily_key),
        }

        try:
            response = await run_with_tools(
                client=self._client,
                model=self._model,
                system=_SYSTEM,
                messages=[{"role": "user", "content": _compress(payload)}],
                tools=_tools,
                handlers=_handlers,
                max_turns=3,
                max_tokens=1024,
                thinking={"type": "adaptive"},
                timeout=anthropic.Timeout(connect=30.0, read=90.0, write=30.0, pool=30.0),
            )

            latency_ms = int((time.monotonic() - t0) * 1000)
            in_tok  = response.usage.input_tokens  if response.usage else 0
            out_tok = response.usage.output_tokens if response.usage else 0
            cost    = _estimate_cost(in_tok, out_tok)

            text_blocks = [b for b in response.content if b.type == "text"]
            raw_text = text_blocks[-1].text.strip() if text_blocks else "{}"
            if raw_text.startswith("```"):
                raw_text = raw_text.split("```")[1].lstrip("json").strip()

            raw_output = json.loads(raw_text)
            thesis = _parse_thesis(raw_output)

            try:
                _log_msg(
                    str(self._settings.db_path), "StockAnalystAgent",
                    self._model, response.usage, purpose=f"analyst_{ticker}",
                    trace_id=decision_id,
                )
            except Exception:
                pass

            self._write_journal(
                decision_id, ticker, conviction_score, payload, thesis, raw_output,
                in_tok, out_tok, cost, latency_ms, lessons,
            )

            logger.info(
                "Analyst [%s] %s | %s %.0f%% conf | %s | %.0fs%s",
                ticker,
                thesis.decision,
                f"{thesis.direction} {thesis.magnitude_pct:.1f}%" if thesis.direction else "",
                thesis.confidence_pct or 0,
                thesis.strategy_family or "",
                latency_ms / 1000,
                " [SHADOW]" if self._shadow_mode else "",
            )

        except Exception as exc:
            latency_ms = int((time.monotonic() - t0) * 1000)
            logger.warning("StockAnalystAgent failed for %s: %s", ticker, exc)
            thesis = AnalystThesis(
                decision="no_thesis", direction=None, magnitude_pct=None,
                horizon_days=None, confidence_pct=None, strategy_family=None,
                kill_conditions=[], scorecard_critique=None, reasoning_trace=None,
                raw={"error": str(exc)},
            )
            self._write_journal(
                decision_id, ticker, conviction_score, payload, thesis, raw_output,
                0, 0, 0.0, latency_ms, lessons,
            )

        # Always return thesis — gating (blocking execution) is caller's responsibility.
        # Shadow mode only means "do not gate"; the thesis object is always useful
        # to downstream agents (StrategySelectorAgent, AdvocateAgent).
        return thesis

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _build_payload(
        self,
        ticker: str,
        conviction_score: float,
        snapshot: Any,
        macro_context: Any | None,
        gex: Any | None,
        iv_premium: Any | None,
        event_signal: Any | None,
        sector_intel: Any | None,
        lessons: list[str] | None = None,
    ) -> dict:
        snap = snapshot
        return {
            "ticker": ticker,
            "conviction_score": round(conviction_score, 1),
            "market": {
                "price":       round(snap.price, 2) if snap and snap.price else None,
                "iv_rank":     round(snap.iv_rank, 1) if snap and snap.iv_rank else None,
                "rsi_14":      round(snap.rsi_14, 1) if snap and snap.rsi_14 else None,
                "hist_vol_30": round(snap.hist_vol_30 * 100, 1) if snap and snap.hist_vol_30 else None,
                "vix":         round(snap.vix, 1) if snap and snap.vix else None,
            },
            "macro": {
                "stance":         macro_context.macro_stance    if macro_context else "unknown",
                "confidence":     round(macro_context.confidence, 2) if macro_context else None,
                "vol_selling_ok": macro_context.vol_selling_ok  if macro_context else None,
                "key_risk":       macro_context.key_risk        if macro_context else None,
            },
            "gex": {
                "regime":          str(gex.regime.value) if gex and gex.regime else None,
                "dominant_strike": gex.dominant_strike   if gex else None,
                "flip_level":      gex.flip_level        if gex else None,
            },
            "iv_premium": {
                "signal_active":        iv_premium.signal_active      if iv_premium else None,
                "premium_ratio":        round(iv_premium.premium_ratio, 2) if iv_premium and iv_premium.premium_ratio else None,
                "days_above_threshold": iv_premium.days_above_threshold if iv_premium else None,
            },
            "event": {
                "type":          event_signal.event_type    if event_signal else None,
                "days_to_event": event_signal.days_to_event if event_signal else None,
                "direction":     event_signal.direction     if event_signal else None,
                "confidence":    event_signal.confidence    if event_signal else None,
            } if event_signal else None,
            "sector": {
                "direction":  sector_intel.read_through_direction                             if sector_intel else None,
                "confidence": round(sector_intel.read_through_confidence, 2) if sector_intel else None,
            } if sector_intel else None,
            "approved_lessons": lessons or [],
        }

    def _write_journal(
        self,
        decision_id: str,
        ticker: str,
        conviction_score: float,
        payload: dict,
        thesis: AnalystThesis,
        raw_output: dict,
        in_tok: int,
        out_tok: int,
        cost: float,
        latency_ms: int,
        lessons: list[str] | None = None,
    ) -> None:
        try:
            with sqlite3.connect(str(self._settings.db_path)) as conn:
                conn.execute(
                    """
                    INSERT INTO analyst_journal (
                        decision_id, ticker, decided_at_utc, prompt_version, model,
                        payload_json, history_used_json, lessons_applied_json,
                        decision, direction, magnitude_pct, horizon_days,
                        confidence_pct, strategy_family, kill_conditions_json,
                        scorecard_critique, reasoning_trace, output_full_json,
                        input_tokens, output_tokens, cost_usd, latency_ms, shadow_mode
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        decision_id or "",
                        ticker,
                        datetime.now(tz=timezone.utc).isoformat(),
                        PROMPT_VERSION,
                        self._model,
                        json.dumps(payload, default=str),
                        None,
                        json.dumps(lessons or [], default=str),
                        thesis.decision,
                        thesis.direction,
                        thesis.magnitude_pct,
                        thesis.horizon_days,
                        thesis.confidence_pct,
                        thesis.strategy_family,
                        json.dumps(thesis.kill_conditions),
                        thesis.scorecard_critique,
                        thesis.reasoning_trace,
                        json.dumps(raw_output, default=str),
                        in_tok, out_tok, round(cost, 6), latency_ms,
                        1 if self._shadow_mode else 0,
                    ),
                )
        except Exception as exc:
            logger.warning("analyst_journal write error: %s", exc)


# ── Pure helpers ──────────────────────────────────────────────────────────────

def _parse_thesis(raw: dict) -> AnalystThesis:
    return AnalystThesis(
        decision=raw.get("decision", "no_thesis"),
        direction=raw.get("direction"),
        magnitude_pct=raw.get("magnitude_pct"),
        horizon_days=raw.get("horizon_days"),
        confidence_pct=raw.get("confidence_pct"),
        strategy_family=raw.get("strategy_family"),
        kill_conditions=raw.get("kill_conditions") or [],
        scorecard_critique=raw.get("scorecard_critique"),
        reasoning_trace=raw.get("reasoning_trace"),
        raw=raw,
    )


def _estimate_cost(in_tok: int, out_tok: int) -> float:
    # Sonnet 4.6: $3.00/M input, $15.00/M output
    return (in_tok * 3.0 + out_tok * 15.0) / 1_000_000

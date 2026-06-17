"""
agora/agents/strategy_selector.py — StrategySelectorAgent (Phase 6, spec §13.3).

Validates and optionally overrides the StrategyRulesEngine's structure choice.
Receives the analyst thesis + a simplified options chain summary and outputs
whether to endorse the rules-engine selection or switch strategy type.

Shadow mode (default):
  Both rules engine (live) and selector (shadow) run. Selector output is
  journaled to strategy_journal but does NOT affect execution.

Live mode:
  Selector drives strategy type selection. On an 'override' the caller
  (AgoraSession._apply_selector_override) re-runs the rules engine forced to the selected type;
  buildable types execute, anything the engine cannot construct falls back to the engine's own
  structure so a trade is never lost. 'no_structure' vetoes the trade. The executed structure is
  written back to strategy_journal (override_honored + displaced baseline) so attribution measures
  what truly traded — see mark_override_execution.

Model: claude-sonnet-4-6 (fast structural selection — cost-efficient for per-ticker calls)
Cost: ~$0.05/call × ≤10 calls/day ≈ $0.50/day
Schema: strategy_journal — managed by migrations/2026_05_phase2_journals.sql

System prompt: §13.3 of AGORA Grand Specification v1.0
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import anthropic
from pydantic import BaseModel

from agora.ops.lessons_store import load_approved_lessons as _load_lessons
from agora.ops.llm_cost_log import log_message as _log_msg
from agora.ops.payload_compressor import compress_payload as _compress

logger = logging.getLogger(__name__)

PROMPT_VERSION = "1.0.0"
_MODEL = "claude-sonnet-4-6"

# ── System prompt (spec §13.3) ─────────────────────────────────────────────────

_SYSTEM = """Options structuring specialist. Receive analyst thesis + chain summary, validate or override the rules engine's structure choice.

HARD CONSTRAINTS:
C1. DTE = horizon_days × 1.5–2.5.
C2. Structure must match thesis direction and magnitude.
C3. Approved strategies only: bull_put_spread, bear_call_spread, iron_condor, iron_butterfly, cash_secured_put, bull_call_spread, bear_put_spread, long_call, long_put, calendar_spread.
C4. Reject if: Debit max_profit/max_loss < 1.3 OR Credit max_loss/max_profit > 4.0.
C5. Reject if bid-ask > 10% of mid for any leg.
C6. Default 1 contract. Scale to 2 only if size_multiplier ≥ 1.0 AND confidence ≥ 70%.

STRATEGY MAP:
  premium_selling + IVR > 50  → credit spread / iron
  directional_debit + IVR < 40 → debit spread / long single
  directional_debit + IVR > 60 → vertical debit (avoid IV crush)
  neutral_range + IVR > 50    → iron condor / butterfly
  avoid                        → no_structure

VALIDATION STEPS:
1. Does rules engine strategy match direction and IVR? Does expiry match DTE target?
   Yes → endorse. No → propose correct type.
2. Flag any legs with OI < 100 or bid-ask > 10%.

ANTI-PATTERNS: DTE creep, premium chasing, width compression, single-leg debit in high IV.

OUTPUT — exactly this JSON, no markdown:

Endorse: {"decision":"endorse","strategy_type":"<type>","expiry_preference":"YYYY-MM-DD","contracts":1,"endorses_rules":true,"rationale_one_line":"string","structure_reasoning":"2-3 sentences","concerns":[],"liquidity_score":1-10,"thesis_alignment_score":1-10}
Override: {"decision":"override","strategy_type":"<corrected>","expiry_preference":"YYYY-MM-DD","contracts":1,"endorses_rules":false,"rationale_one_line":"string","override_reason":"string","structure_reasoning":"2-3 sentences","concerns":["string"],"liquidity_score":1-10,"thesis_alignment_score":1-10}
No structure: {"decision":"no_structure","reason":"specific reason"}"""

_CACHED_SYSTEM = [{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}]


# ── Structured output schema ───────────────────────────────────────────────────
# All three decision shapes (endorse / override / no_structure) collapsed into
# one model with optional fields — SDK enforces this schema via output_config.format,
# so _parse_selection never sees malformed JSON.

class _SelectorOutput(BaseModel):
    decision: str                      # "endorse" | "override" | "no_structure"
    strategy_type: str | None = None
    expiry_preference: str | None = None
    contracts: int = 1
    endorses_rules: bool = True
    rationale_one_line: str = ""
    override_reason: str | None = None  # override only
    reason: str | None = None           # no_structure only
    structure_reasoning: str = ""
    concerns: list[str] = []
    liquidity_score: int = 5
    thesis_alignment_score: int = 5


# ── Output dataclass ──────────────────────────────────────────────────────────

@dataclass
class StrategySelection:
    decision:             str              # 'endorse' | 'override' | 'no_structure'
    strategy_type:        str | None       # strategy type to use (after endorsement or override)
    expiry_preference:    str | None       # preferred expiry date string
    contracts:            int
    endorses_rules:       bool
    rationale:            str
    structure_reasoning:  str
    concerns:             list[str]
    liquidity_score:      int
    thesis_alignment_score: int
    raw:                  dict = field(default_factory=dict)


# ── Agent ─────────────────────────────────────────────────────────────────────

class StrategySelectorAgent:
    """
    Phase 6 intelligence layer — validates/overrides the rules engine's structure choice.

    Call select() after rules engine builds its recommendation. In shadow mode the
    selection is journaled but the caller always uses the rules engine result.
    In live mode, an 'override' decision causes the caller to re-run the rules engine forced to
    the corrected strategy_type (honored only if the engine can construct it, else it falls back
    to the engine's own structure), while a 'no_structure' decision blocks submission. The wiring
    lives in AgoraSession._apply_selector_override; mark_override_execution records the outcome.
    """

    # Result cache TTL — reuse a selection for the same ticker / conviction-band /
    # pillar / direction. The chosen STRUCTURE for a given setup does not change within
    # a couple of hours, so a 30-min TTL just re-bought the same answer ~13x/ticker/day.
    # 2h aligns with the advocate/defender debate cooldown and is quality-neutral: the
    # key already buckets conviction (nearest 5), so a real setup change still re-runs.
    _CACHE_TTL_SECS = 7200

    def __init__(self, settings: Any, shadow_mode: bool = True) -> None:
        self._settings    = settings
        self._shadow_mode = shadow_mode
        self._client      = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        # cache: key → (cached_at, StrategySelection)
        self._cache: dict[str, tuple[datetime, StrategySelection]] = {}
        logger.info("StrategySelectorAgent ready: model=%s shadow=%s", _MODEL, shadow_mode)

    @property
    def shadow_mode(self) -> bool:
        return self._shadow_mode

    @shadow_mode.setter
    def shadow_mode(self, value: bool) -> None:
        if self._shadow_mode != value:
            logger.info("StrategySelectorAgent shadow_mode %s → %s", self._shadow_mode, value)
        self._shadow_mode = value

    async def select(
        self,
        ticker:               str,
        thesis:               Any,             # AnalystThesis
        conviction:           Any,             # ConvictionScore
        snapshot:             Any,             # MarketSnapshot
        options_chain:        dict,            # {expiry: {calls: df, puts: df}}
        rules_recommendation: Any | None,      # TradeRecommendation from rules engine
        decision_id:          str = "",
    ) -> StrategySelection | None:
        """
        Validate or override the rules engine selection.
        Returns StrategySelection in both shadow and live mode.
        Never raises — failures return None.
        """
        # ── Cache check ───────────────────────────────────────────────────────
        # Key: ticker + conviction band (nearest 5) + pillar + direction
        _conv_band = int(getattr(conviction, "total_score", 0) // 5) * 5
        _pillar    = str(getattr(conviction, "pillar", ""))
        _direction = str(getattr(thesis, "direction", "") if thesis else "")
        _cache_key = f"{ticker}|{_conv_band}|{_pillar}|{_direction}"
        _now = datetime.now(UTC)
        if _cache_key in self._cache:
            _cached_at, _cached_sel = self._cache[_cache_key]
            if (_now - _cached_at).total_seconds() < self._CACHE_TTL_SECS:
                logger.debug(
                    "StrategySelector cache hit [%s]: %s (age=%ds, saves ~$0.03)",
                    ticker, _cache_key,
                    int((_now - _cached_at).total_seconds()),
                )
                return _cached_sel

        lessons = _load_lessons(str(self._settings.db_path), "strategy")
        payload = self._build_payload(ticker, thesis, conviction, snapshot,
                                      options_chain, rules_recommendation, lessons)
        t0 = time.monotonic()
        selection: StrategySelection | None = None
        raw_output: dict = {}

        try:
            # messages.parse occasionally emits malformed structured output (e.g. a stray
            # trailing comma -> '{"decision": "override", }') and the validation rejects it.
            # This is an intermittent model glitch, so retry once before giving up.
            response = None
            _parse_err: Exception | None = None
            for _attempt in range(2):
                try:
                    response = await self._client.messages.parse(
                        model=_MODEL,
                        max_tokens=1024,
                        system=_CACHED_SYSTEM,
                        messages=[{"role": "user", "content": _compress(payload)}],
                        output_format=_SelectorOutput,
                        timeout=anthropic.Timeout(connect=30.0, read=45.0, write=30.0, pool=30.0),
                    )
                    if response.parsed_output is not None:
                        _parse_err = None
                        break
                    _parse_err = ValueError("messages.parse returned no structured output")
                except Exception as _e:  # malformed JSON / validation / transient
                    _parse_err = _e
                    logger.debug("StrategySelector parse retry for %s (attempt %d): %s",
                                 ticker, _attempt + 1, _e)
            if _parse_err is not None or response is None:
                raise _parse_err or ValueError("messages.parse failed")
            latency_ms = int((time.monotonic() - t0) * 1000)
            in_tok  = response.usage.input_tokens  if response.usage else 0
            out_tok = response.usage.output_tokens if response.usage else 0

            parsed = response.parsed_output
            raw_output = parsed.model_dump()
            selection  = _parse_selection(raw_output)

            try:
                _log_msg(str(self._settings.db_path), "StrategySelectorAgent", _MODEL,
                         response.usage, purpose=f"strategy_{ticker}",
                         trace_id=decision_id)
            except Exception:
                pass

            self._write_journal(decision_id, ticker, conviction, rules_recommendation,
                                selection, raw_output, in_tok, out_tok, latency_ms)

            if selection is not None:
                self._cache[_cache_key] = (_now, selection)

            logger.info(
                "StrategySelector [%s] %s | type=%s align=%d/10 liq=%d/10%s",
                ticker, selection.decision,
                selection.strategy_type or "n/a",
                selection.thesis_alignment_score, selection.liquidity_score,
                " [SHADOW]" if self._shadow_mode else "",
            )

        except Exception as exc:
            latency_ms = int((time.monotonic() - t0) * 1000)
            logger.warning("StrategySelectorAgent failed for %s: %s", ticker, exc)
            self._write_journal(decision_id, ticker, conviction, rules_recommendation,
                                None, {"error": str(exc)}, 0, 0, latency_ms)

        return selection

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _build_payload(
        self,
        ticker:               str,
        thesis:               Any,
        conviction:           Any,
        snapshot:             Any,
        options_chain:        dict,
        rules_recommendation: Any | None,
        lessons:              list[str] | None = None,
    ) -> dict:
        rules_summary = None
        if rules_recommendation is not None:
            try:
                legs = [
                    {"action": lg.action, "type": lg.option_type,
                     "strike": lg.strike, "expiry": str(lg.expiration),
                     "mid": lg.mid_price}
                    for lg in rules_recommendation.legs
                ]
                rules_summary = {
                    "strategy_type": str(getattr(rules_recommendation.strategy, "value",
                                                  rules_recommendation.strategy)),
                    "legs": legs,
                    "contracts": rules_recommendation.contracts,
                    "entry_debit_credit": rules_recommendation.entry_debit_credit,
                    "max_profit": rules_recommendation.max_gain_dollars,
                    "max_loss": rules_recommendation.max_loss_dollars,
                    "rr_ratio": rules_recommendation.reward_risk_ratio,
                }
            except Exception:
                pass

        return {
            "ticker": ticker,
            "thesis": {
                "direction":       getattr(thesis, "direction", None),
                "magnitude_pct":   getattr(thesis, "magnitude_pct", None),
                "horizon_days":    getattr(thesis, "horizon_days", None),
                "confidence_pct":  getattr(thesis, "confidence_pct", None),
                "strategy_family": getattr(thesis, "strategy_family", None),
                "kill_conditions": getattr(thesis, "kill_conditions", []),
            },
            "conviction": {
                "total_score":     round(getattr(conviction, "total_score", 0), 1),
                "pillar":          str(getattr(conviction, "pillar", "")),
                "gate":            str(getattr(conviction, "gate", "")),
                "size_multiplier": getattr(conviction, "size_multiplier", 1.0),
            },
            "market": {
                "spot":     round(snapshot.price, 2) if snapshot and snapshot.price else None,
                "iv_rank":  round(snapshot.iv_rank, 1) if snapshot and snapshot.iv_rank else None,
                "rsi_14":   round(snapshot.rsi_14, 1) if snapshot and snapshot.rsi_14 else None,
            },
            "chain_summary": _summarize_chain(
                options_chain,
                snapshot.price if snapshot and snapshot.price else 0,
                focus_strikes=(
                    [lg.strike for lg in rules_recommendation.legs]
                    if rules_recommendation is not None else None
                ),
            ),
            "rules_engine_proposal": rules_summary,
            "approved_lessons": lessons or [],
        }

    def _write_journal(
        self,
        decision_id:          str,
        ticker:               str,
        conviction:           Any,
        rules_recommendation: Any | None,
        selection:            StrategySelection | None,
        raw_output:           dict,
        in_tok:               int,
        out_tok:              int,
        latency_ms:           int,
    ) -> None:
        cost = (in_tok * 3.0 + out_tok * 15.0) / 1_000_000  # Sonnet 4.6 pricing
        try:
            with sqlite3.connect(str(self._settings.db_path)) as conn:
                conn.execute(
                    """INSERT INTO strategy_journal (
                        decision_id, ticker, decided_at_utc, prompt_version, model,
                        payload_json, decision, strategy_type, legs_json,
                        contracts, entry_debit_credit, max_profit, max_loss,
                        reward_risk_ratio, output_full_json,
                        input_tokens, output_tokens, cost_usd, latency_ms, shadow_mode
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        decision_id or "",
                        ticker,
                        datetime.now(tz=UTC).isoformat(),
                        PROMPT_VERSION, _MODEL,
                        json.dumps({"ticker": ticker}, default=str),
                        selection.decision if selection else "error",
                        selection.strategy_type if selection else None,
                        json.dumps(selection.concerns if selection else [], default=str),
                        selection.contracts if selection else 1,
                        # These come from the rules engine proposal, not selector
                        rules_recommendation.entry_debit_credit if rules_recommendation else None,
                        rules_recommendation.max_gain_dollars   if rules_recommendation else None,
                        rules_recommendation.max_loss_dollars   if rules_recommendation else None,
                        rules_recommendation.reward_risk_ratio  if rules_recommendation else None,
                        json.dumps(raw_output, default=str),
                        in_tok, out_tok, round(cost, 6), latency_ms,
                        1 if self._shadow_mode else 0,
                    ),
                )
        except Exception as exc:
            logger.warning("strategy_journal write error: %s", exc)


# ── Override-execution bookkeeping ──────────────────────────────────────────────

def _map_strategy_type(name: str | None):
    """Map a selector strategy_type string to the StrategyType enum, or None if unknown."""
    if not name:
        return None
    try:
        from agora.core.models import StrategyType
        return StrategyType(str(name).strip().lower())
    except Exception:
        return None


def mark_override_execution(
    db_path: str, decision_id: str, honored: bool,
    executed_recommendation: Any | None, displaced_strategy_type: str | None,
) -> None:
    """Record what ACTUALLY executed after a live override decision, so attribution credits the
    override row only when the override truly drove the fill.

    Before this existed the override was unwired: the journal stored the rules-engine economics
    regardless of decision, so every 'override' metric measured a structure that never traded.
    Now, when an override is honored, we overwrite the journal row with the EXECUTED structure and
    stash the displaced rules-engine structure as the counterfactual baseline; when it is not
    honored (unbuildable / rebuild failed), override_honored=0 marks it as having executed the
    rules engine's structure (endorse-equivalent for outcome analysis)."""
    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(strategy_journal)")}
            if "override_honored" not in cols:
                conn.execute("ALTER TABLE strategy_journal ADD COLUMN override_honored INTEGER")
            if not honored or executed_recommendation is None:
                conn.execute(
                    "UPDATE strategy_journal SET override_honored=0 WHERE decision_id=?",
                    (decision_id,),
                )
                return
            rec = executed_recommendation
            legs = [
                {"action": lg.action, "type": lg.option_type, "strike": lg.strike,
                 "expiry": str(lg.expiration), "mid": lg.mid_price}
                for lg in rec.legs
            ]
            conn.execute(
                """UPDATE strategy_journal
                   SET override_honored=1,
                       strategy_type=?, legs_json=?, contracts=?,
                       entry_debit_credit=?, max_profit=?, max_loss=?, reward_risk_ratio=?,
                       output_full_json=json_patch(
                           COALESCE(output_full_json,'{}'),
                           json_object('rules_engine_displaced', ?))
                   WHERE decision_id=?""",
                (
                    str(getattr(rec.strategy, "value", rec.strategy)),
                    json.dumps(legs, default=str), rec.contracts,
                    rec.entry_debit_credit, rec.max_gain_dollars, rec.max_loss_dollars,
                    rec.reward_risk_ratio,
                    displaced_strategy_type or "",
                    decision_id,
                ),
            )
    except Exception as exc:
        logger.warning("mark_override_execution failed [%s]: %s", decision_id, exc)


# ── Pure helpers ──────────────────────────────────────────────────────────────

def _parse_selection(raw: dict) -> StrategySelection:
    return StrategySelection(
        decision=raw.get("decision", "endorse"),
        strategy_type=raw.get("strategy_type"),
        expiry_preference=raw.get("expiry_preference"),
        contracts=raw.get("contracts", 1),
        endorses_rules=raw.get("endorses_rules", True),
        rationale=raw.get("rationale_one_line") or raw.get("reason", ""),
        structure_reasoning=raw.get("structure_reasoning", ""),
        concerns=raw.get("concerns", []),
        liquidity_score=raw.get("liquidity_score", 5),
        thesis_alignment_score=raw.get("thesis_alignment_score", 5),
        raw=raw,
    )


def _summarize_chain(chain_dict: dict, spot: float, focus_strikes=None) -> dict:
    """Compact options chain for LLM: ALL fetched expiries, 4 strikes near spot each side
    PLUS any focus_strikes (the strikes the rules engine actually proposed). Without the
    focus strikes the selector validates against a window that excludes the very strikes
    it's judging; and truncating to the nearest expiries (the old `[:2]`) hid the very
    expiry the rules engine proposed (it targets ~30 DTE, which is the 3rd/4th expiry, not
    the front two) — so the selector saw a proposal referencing an expiry 'not in the chain
    summary' and returned 'no_structure' on essentially every ticker. chain_dict is already
    capped at one expiry per DTE bracket (≤4) upstream, so including all of them is cheap."""
    focus = {round(float(s), 2) for s in (focus_strikes or [])}
    summary = {}
    for expiry, data in list(chain_dict.items()):
        try:
            calls_df = data.get("calls")
            puts_df  = data.get("puts")
            exp_date = date.fromisoformat(expiry)
            dte = (exp_date - date.today()).days

            def _fmt(df, lo_pct, hi_pct):
                if df is None or df.empty:
                    return []
                cols = ["strike", "bid", "ask", "openInterest", "impliedVolatility"]
                near = df[(df["strike"] >= spot * lo_pct) & (df["strike"] <= spot * hi_pct)]
                wanted = set(near["strike"].head(4).round(2).tolist()) | focus
                sub = df[df["strike"].round(2).isin(wanted)][cols].sort_values("strike")
                return json.loads(sub.round(4).to_json(orient="records"))

            summary[expiry] = {
                "dte":   dte,
                "calls": _fmt(calls_df, 0.97, 1.10),
                "puts":  _fmt(puts_df,  0.90, 1.03),
            }
        except Exception:
            continue
    return summary

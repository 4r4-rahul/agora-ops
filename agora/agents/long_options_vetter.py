"""
agora/agents/long_options_vetter.py — Opus 4.8 pre-trade quality gate.

Reviews each LongOptionsAgent "proceed" decision with Claude Opus 4.8 adaptive
thinking before the order is submitted. The deterministic engine scores signals
mechanically; the vetter asks whether those signals represent a genuinely good
trade in today's context.

Shadow mode (default, LONG_OPTIONS_VETTER_SHADOW_MODE=true):
  Runs on every proceed decision. Logs verdict vs. outcome for calibration.
  Does NOT block or modify trades. Use for at least 20 trades before going live.

Live mode (LONG_OPTIONS_VETTER_SHADOW_MODE=false):
  "reduce" → cuts contracts to verdict.adjusted_contracts.
  "skip"   → blocks the trade entirely.
  "proceed" → passes through unchanged.

Model: claude-opus-4-8 with adaptive thinking.
  Adaptive thinking lets the model reason through signal confluence, timing,
  and risk factors without a fixed budget — it thinks as much as the setup warrants.

Cost: ~$0.02–0.06/call depending on thinking depth. Only fires on proceed decisions.
      At 8 tickers × 15-min scan, ~2-4 proceeds/day → < $0.25/day in shadow mode.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import anthropic

logger = logging.getLogger(__name__)

_MODEL = "claude-opus-4-8"

# Transient Anthropic errors worth retrying before falling back to UNVETTED. The vetter
# fails open (vet() returns None → trade proceeds without LLM review), so a momentary
# 429/timeout silently drops the quality gate. This matters most under the parallel long
# loop, where concurrent Opus calls make rate-limit bursts more likely — retry-with-backoff
# recovers the gate instead of skipping it. Non-transient errors (parse, etc.) are NOT
# retried here; they propagate to vet()'s catch and fall back to unvetted as before.
_RETRYABLE_ERRORS = tuple(
    e for e in (
        getattr(anthropic, "APITimeoutError", None),
        getattr(anthropic, "APIConnectionError", None),
        getattr(anthropic, "RateLimitError", None),
        getattr(anthropic, "InternalServerError", None),
    ) if e is not None
)

_SYSTEM = """You are a senior options trader at a prop desk. Your role is quality control on proposed long option swing trades before capital is committed.

The deterministic scoring engine has already checked IVR gates, liquidity, bid-ask spreads, and signal counts. You are NOT re-checking those mechanical gates.

Your job: assess whether this specific signal combination, at this specific moment, represents a genuine trading opportunity — or a mechanical score that doesn't reflect real conviction.

THINK THROUGH:
1. Signal coherence — do the signals tell a consistent story, or are they coincidentally aligned?
   - flow sweep + momentum + macro alignment = coherent institutional play
   - news + volume surge without flow or momentum = potentially chasing headlines
2. Signal timing — is the opportunity still live, or has the move already happened?
   - RSI at 65 heading up with flow = early; RSI at 71 with news = likely late
3. Sizing sanity — does the quality score justify the contract count?
4. Key risk — what specific scenario would make this trade lose money within 5 days?

VERDICT OPTIONS:
- "proceed"  — setup is genuine, execute as proposed
- "reduce"   — setup has merit but contract size is too aggressive; halve it
- "skip"     — mechanical score does not reflect a tradeable setup; pass this cycle

Output ONLY valid JSON. No prose, no markdown, no code blocks. Just the JSON object."""

_PROMPT_TEMPLATE = """PROPOSED TRADE:
Ticker: {ticker}
Strategy: {strategy} ({direction})
Strike: {strike} ({delta:.2f}Δ)
Expiry: {dte} DTE
Premium: ${premium:.2f}/share × {contracts} contracts = ${total_cost:.0f} max loss
IVR: {ivr:.0f} (per-ticker proxy — options are in buy zone)
Signal quality score: {quality:.1f}/6.0

SIGNAL STACK (conviction score = {conviction}):
{signal_stack_lines}

MARKET CONTEXT:
- Regime: {regime}
- VIX: {vix:.1f}
- Flow direction: {flow_direction}
- RSI14: {rsi:.0f}
- 10-day return: {ret_10d:.1%}
- Volume surge: {vol_surge}

DTE REASONING: {dte_reason}

Respond with exactly this JSON structure (no other text):
{{
  "verdict": "proceed" or "reduce" or "skip",
  "confidence": <integer 0-100>,
  "adjusted_contracts": <integer or null>,
  "key_risk": "<one sentence: what specific event/condition causes this trade to lose within 5 days>",
  "reasoning": "<2-3 sentences: why this verdict>"
}}"""


@dataclass
class VetterVerdict:
    verdict:            str    # "proceed" | "reduce" | "skip"
    confidence:         int    # 0-100
    adjusted_contracts: int | None
    key_risk:           str
    reasoning:          str
    latency_ms:         float  = 0.0
    shadow_mode:        bool   = True


class LongOptionsVetterAgent:
    """
    Opus 4.8 pre-trade quality gate for long options.

    Call vet() after LongOptionsAgent.evaluate() returns a proceed decision.
    Runs asynchronously — does not block the scan loop if it times out.
    """

    def __init__(self, settings: Any) -> None:
        self._settings   = settings
        self._client     = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        self._shadow     = getattr(settings, "long_options_vetter_shadow_mode", True)
        self._min_conv   = getattr(settings, "long_options_vetter_min_conviction", 3)
        logger.info(
            "LongOptionsVetterAgent ready: model=%s shadow=%s min_conviction=%d",
            _MODEL, self._shadow, self._min_conv,
        )

    @property
    def shadow_mode(self) -> bool:
        return self._shadow

    async def vet(
        self,
        decision:      Any,       # LongDecision
        macro_context: Any | None,
        momentum:      dict | None = None,
    ) -> VetterVerdict | None:
        """
        Vet a proceed decision. Returns None if conviction below threshold (skip vetting).
        Never raises — failures return None so the trade proceeds without vetting.
        """
        conviction = getattr(decision, "conviction", 0)
        if conviction < self._min_conv:
            return None   # low-conviction trades don't need LLM vetting

        try:
            return await self._call_opus(decision, macro_context, momentum or {})
        except Exception as exc:
            logger.warning("LongOptionsVetter error [%s]: %s", decision.ticker, exc)
            return None

    async def _call_opus(
        self,
        decision:      Any,
        macro_context: Any | None,
        momentum:      dict,
    ) -> VetterVerdict:
        stack    = getattr(decision, "signal_stack", {})
        stack_lines = "\n".join(f"  {k}: {v}" for k, v in stack.items())

        prompt = _PROMPT_TEMPLATE.format(
            ticker        = decision.ticker,
            strategy      = decision.strategy,
            direction     = getattr(decision, "flow_direction", ""),
            strike        = decision.strike,
            delta         = abs(decision.delta_approx),
            dte           = decision.dte,
            premium       = decision.premium,
            contracts     = decision.contracts,
            total_cost    = decision.premium * 100 * decision.contracts,
            ivr           = decision.per_ticker_ivr or decision.ivr,
            quality       = getattr(decision, "signal_quality", 0.0),
            conviction    = decision.conviction,
            signal_stack_lines = stack_lines,
            regime        = getattr(decision, "regime", "normal"),
            vix           = getattr(decision, "vix", 18.0),
            flow_direction= getattr(decision, "flow_direction", "neutral"),
            rsi           = momentum.get("rsi", 50.0),
            ret_10d       = momentum.get("ret_10d", 0.0),
            vol_surge     = "yes" if momentum.get("vol_surge") else "no",
            dte_reason    = getattr(decision, "dte_reason", ""),
        )

        # ── CEO-approved lessons (gated learning loop) ────────────────────────
        # Lessons synthesized from long-options outcomes and approved by the CEO are
        # injected as decision context so the vetter applies what the book has learned.
        # §17: only human-approved lessons are ever used.
        try:
            from agora.ops.lessons_store import load_approved_lessons
            lessons = load_approved_lessons(str(self._settings.db_path), "long_options")
        except Exception:
            lessons = []
        if lessons:
            prompt += (
                "\n\nLESSONS LEARNED (CEO-approved, from this book's own closed trades — "
                "weigh these heavily):\n"
                + "\n".join(f"  - {l}" for l in lessons)
            )

        t0 = time.monotonic()
        response = await self._create_with_retry(prompt, decision.ticker)
        latency_ms = (time.monotonic() - t0) * 1000

        raw_text = next(
            (b.text for b in response.content if getattr(b, "type", "") == "text"),
            "{}",
        )

        try:
            data = json.loads(raw_text.strip())
        except json.JSONDecodeError:
            # Try to extract JSON from the text in case of extra prose
            import re
            match = re.search(r'\{.*\}', raw_text, re.DOTALL)
            data  = json.loads(match.group()) if match else {}

        # Validate the verdict against the allowed enum — an unknown/hallucinated value
        # (e.g. "hold") would otherwise be neither skip nor reduce and silently proceed.
        _raw_verdict = str(data.get("verdict", "proceed")).lower().strip()
        if _raw_verdict not in ("skip", "reduce", "proceed"):
            logger.warning("LongOptionsVetter: unknown verdict %r — defaulting to proceed", _raw_verdict)
            _raw_verdict = "proceed"
        verdict = VetterVerdict(
            verdict            = _raw_verdict,
            confidence         = int(data.get("confidence", 70)),
            adjusted_contracts = data.get("adjusted_contracts"),
            key_risk           = data.get("key_risk", ""),
            reasoning          = data.get("reasoning", ""),
            latency_ms         = latency_ms,
            shadow_mode        = self._shadow,
        )

        logger.info(
            "LongOptionsVetter [%s] → %s (conf=%d%%) | risk: %s | %.0fms%s",
            decision.ticker,
            verdict.verdict.upper(),
            verdict.confidence,
            verdict.key_risk,
            latency_ms,
            " [SHADOW]" if self._shadow else "",
        )

        return verdict

    async def _create_with_retry(self, prompt: str, ticker: str, attempts: int = 3,
                                 base_backoff: float = 1.5):
        """Call Opus, retrying transient Anthropic errors with exponential backoff.
        Re-raises the last error after `attempts` tries (vet() then falls back to unvetted)."""
        last_exc = None
        for i in range(attempts):
            try:
                return await self._client.messages.create(
                    model       = _MODEL,
                    max_tokens  = 512,
                    thinking    = {"type": "adaptive"},
                    system      = _SYSTEM,
                    messages    = [{"role": "user", "content": prompt}],
                    timeout     = anthropic.Timeout(connect=10.0, read=45.0, write=10.0, pool=10.0),
                )
            except _RETRYABLE_ERRORS as exc:
                last_exc = exc
                if i < attempts - 1:
                    wait = base_backoff * (2 ** i)
                    logger.warning(
                        "LongOptionsVetter transient API error [%s] attempt %d/%d: %s — "
                        "retrying in %.1fs", ticker, i + 1, attempts, type(exc).__name__, wait,
                    )
                    await asyncio.sleep(wait)
        raise last_exc

    def journal(
        self,
        decision: Any,
        verdict:  VetterVerdict,
        db_path:  str,
        outcome:  str = "",        # filled in on close: "win" | "loss"
        realized_pnl: float = 0.0,
    ) -> None:
        """Write vetter verdict to DB for calibration analysis."""
        try:
            with sqlite3.connect(db_path, timeout=10) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS long_vetter_log (
                        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts_utc              TEXT NOT NULL,
                        ticker              TEXT NOT NULL,
                        strategy            TEXT NOT NULL,
                        conviction          INTEGER,
                        signal_quality      REAL,
                        strike              REAL,
                        dte                 INTEGER,
                        contracts_proposed  INTEGER,
                        verdict             TEXT NOT NULL,
                        confidence          INTEGER,
                        adjusted_contracts  INTEGER,
                        key_risk            TEXT,
                        reasoning           TEXT,
                        latency_ms          REAL,
                        shadow_mode         INTEGER,
                        outcome             TEXT DEFAULT '',
                        realized_pnl        REAL DEFAULT 0.0
                    )
                """)
                conn.execute("""
                    INSERT INTO long_vetter_log (
                        ts_utc, ticker, strategy, conviction, signal_quality,
                        strike, dte, contracts_proposed, verdict, confidence,
                        adjusted_contracts, key_risk, reasoning, latency_ms, shadow_mode
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    datetime.now(timezone.utc).isoformat(),
                    decision.ticker,
                    decision.strategy,
                    decision.conviction,
                    getattr(decision, "signal_quality", 0.0),
                    decision.strike,
                    decision.dte,
                    decision.contracts,
                    verdict.verdict,
                    verdict.confidence,
                    verdict.adjusted_contracts,
                    verdict.key_risk,
                    verdict.reasoning,
                    verdict.latency_ms,
                    1 if verdict.shadow_mode else 0,
                ))
        except Exception as exc:
            logger.debug("long_vetter_log write error: %s", exc)

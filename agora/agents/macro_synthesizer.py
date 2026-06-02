"""
MacroSynthesizer — qualitative macro read using Claude extended thinking.

Inputs (all structured, no LLM needed to compute them):
  - Vol regime (from VolRegimeClassifier)
  - VIX level and VIX/VIX3M ratio
  - SPY RSI and trend
  - FOMC / CPI calendar proximity
  - Fed Funds rate trajectory (from FRED or cached)

Output: MacroContext dataclass with:
  - macro_stance: "risk_on" | "risk_off" | "neutral"
  - confidence: 0.0–1.0  (from Claude's calibrated reasoning)
  - vol_selling_ok: bool  (is IV elevated enough to justify selling premium?)
  - size_bias: "increase" | "maintain" | "reduce"
  - reasoning: str  (1–2 sentences, shown in dashboard)

Claude API features used:
  - Extended thinking: adaptive — synthesizes conflicting signals
  - Prompt caching: stable system prompt cached across daily calls
  - Called once per session open (not per ticker) — NOT in hot path
"""

from __future__ import annotations

import json
import logging
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import anthropic

from ..core.config import AgoraSettings, get_settings
from ..ops.llm_cost_log import log_call as _log_llm, log_message as _log_msg
from ..ops.payload_compressor import compress_text as _compress_text

logger = logging.getLogger(__name__)

_SYSTEM = """\
You are a macro market context synthesizer for an options trading system.
You receive structured quantitative market state and must produce a qualitative
synthesis to guide position sizing and strategy selection.

Think carefully about conflicting signals — e.g., high VIX (risk-off) but
VIX/VIX3M < 1.0 (term structure normal) suggests fear without panic.

Output JSON only:
{
  "macro_stance":    "risk_on" | "risk_off" | "neutral",
  "confidence":      <float 0.0-1.0>,
  "vol_selling_ok":  <bool>,        // IV elevated enough to sell premium profitably
  "size_bias":       "increase" | "maintain" | "reduce",
  "key_risk":        "<main tail risk in 1 phrase>",
  "reasoning":       "<1-2 sentences synthesizing the signals>"
}

vol_selling_ok = true when: IV rank > 40 OR vix > 18 AND regime != "crisis".
In crisis: only sell premium if VIX > 35 (ultra-elevated premium justifies risk).
size_bias = "reduce" when: macro_stance = "risk_off" AND confidence > 0.7.
size_bias = "increase" when: risk_on AND vol_selling_ok AND vix_vix3m < 1.0.
"""

_CACHED_SYSTEM = [
    {"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}
]


@dataclass
class MacroContext:
    macro_stance: str = "neutral"
    confidence: float = 0.5
    vol_selling_ok: bool = True
    size_bias: str = "maintain"
    key_risk: str = ""
    reasoning: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    method: str = "claude"


class MacroSynthesizer:
    """
    Called once per session open. Synthesizes regime + calendar + technicals
    into an actionable macro context that gates position sizing.
    """

    def __init__(self, settings: AgoraSettings | None = None) -> None:
        self._settings = settings or get_settings()
        self._client = anthropic.AsyncAnthropic(api_key=self._settings.anthropic_api_key)
        # Seed with a safe rule-based default so readiness never sees None
        # even before the first async scan completes.
        self._last_context: MacroContext = MacroContext(
            macro_stance="neutral", confidence=0.5,
            vol_selling_ok=True, size_bias="maintain",
            key_risk="startup", reasoning="Startup default — pending synthesis.",
            method="rules",
        )

    async def synthesize(
        self,
        regime: str,
        iv_rank: float | None,
        vix: float | None,
        vix_vix3m: float | None,
        spy_rsi: float | None,
        days_to_fomc: int | None,
        days_to_cpi: int | None,
        fed_rate: float | None = None,
    ) -> MacroContext:
        """
        Build macro context from quantitative inputs.
        Uses extended thinking for conflicting-signal resolution.
        """
        state_summary = self._format_state(
            regime, iv_rank, vix, vix_vix3m, spy_rsi,
            days_to_fomc, days_to_cpi, fed_rate,
        )

        try:
            # Non-streaming create() for macro synthesis: JSON output is tiny (512 tokens).
            # Explicit longer connect timeout — startup contention from 20+ agents opening
            # connections simultaneously can delay TCP handshake beyond the 5s default.
            _t0 = _time.monotonic()
            response = await self._client.messages.create(
                model=self._settings.claude_brief_model,
                max_tokens=512,
                system=_CACHED_SYSTEM,
                messages=[{"role": "user", "content": _compress_text(state_summary)}],
                timeout=anthropic.Timeout(connect=30.0, read=120.0, write=30.0, pool=30.0),
            )
            logger.info("MacroSynthesizer Claude call OK in %.1fs", _time.monotonic() - _t0)
            if hasattr(response, "usage"):
                _log_msg(
                    str(self._settings.db_path), "MacroSynthesizer",
                    self._settings.claude_brief_model,
                    response.usage,
                    purpose="macro_synthesis",
                )

            text_blocks = [b for b in response.content if b.type == "text"]
            if not text_blocks:
                ctx = self._fallback_context(regime, iv_rank, vix)
                self._last_context = ctx
                return ctx

            raw = text_blocks[-1].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1].lstrip("json").strip()

            data = json.loads(raw)
            ctx = MacroContext(
                macro_stance=data.get("macro_stance", "neutral"),
                confidence=float(data.get("confidence", 0.5)),
                vol_selling_ok=bool(data.get("vol_selling_ok", True)),
                size_bias=data.get("size_bias", "maintain"),
                key_risk=data.get("key_risk", ""),
                reasoning=data.get("reasoning", ""),
                method="claude",
            )
            self._last_context = ctx
            return ctx

        except Exception as exc:
            logger.warning(
                "MacroSynthesizer Claude call failed [%s]: %s — using rule fallback",
                type(exc).__name__, exc,
            )
            ctx = self._fallback_context(regime, iv_rank, vix)
            self._last_context = ctx  # always set so readiness meter sees non-None
            return ctx

    def _format_state(
        self,
        regime: str,
        iv_rank: float | None,
        vix: float | None,
        vix_vix3m: float | None,
        spy_rsi: float | None,
        days_to_fomc: int | None,
        days_to_cpi: int | None,
        fed_rate: float | None,
    ) -> str:
        lines = [
            f"Vol regime: {regime}",
            f"IV rank (0-100): {iv_rank:.1f}" if iv_rank is not None else "IV rank: unknown",
            f"VIX: {vix:.2f}" if vix is not None else "VIX: unknown",
            f"VIX/VIX3M ratio: {vix_vix3m:.3f}" if vix_vix3m is not None else "VIX/VIX3M: unknown",
            f"SPY RSI-14: {spy_rsi:.1f}" if spy_rsi is not None else "SPY RSI: unknown",
            f"Days to next FOMC: {days_to_fomc}" if days_to_fomc is not None else "FOMC: unknown",
            f"Days to next CPI: {days_to_cpi}" if days_to_cpi is not None else "CPI: unknown",
            f"Fed Funds rate: {fed_rate:.2f}%" if fed_rate is not None else "",
        ]
        return "\n".join(l for l in lines if l)

    def _fallback_context(
        self,
        regime: str,
        iv_rank: float | None,
        vix: float | None,
    ) -> MacroContext:
        """Deterministic fallback when Claude is unavailable."""
        vix = vix or 20.0
        iv_rank = iv_rank or 50.0

        if regime == "crisis":
            return MacroContext(
                macro_stance="risk_off", confidence=0.80,
                vol_selling_ok=(vix > 35),
                size_bias="reduce", key_risk="market panic",
                reasoning="Crisis regime: reduce size, only sell premium if VIX > 35.",
                method="rules",
            )
        elif regime == "high_volatility":
            return MacroContext(
                macro_stance="neutral", confidence=0.65,
                vol_selling_ok=True,
                size_bias="maintain", key_risk="vol spike",
                reasoning="High vol: elevated IV favors credit spreads, maintain standard size.",
                method="rules",
            )
        elif regime == "low_volatility":
            return MacroContext(
                macro_stance="risk_on", confidence=0.65,
                vol_selling_ok=(iv_rank > 40),
                size_bias="maintain", key_risk="vol expansion",
                reasoning="Low vol: prefer directional debit spreads; credit spreads need IV > 40.",
                method="rules",
            )
        else:
            return MacroContext(
                macro_stance="neutral", confidence=0.55,
                vol_selling_ok=(iv_rank > 35),
                size_bias="maintain", key_risk="regime shift",
                reasoning="Normal regime: balanced approach, standard sizing.",
                method="rules",
            )

    @property
    def last_context(self) -> MacroContext | None:
        return self._last_context

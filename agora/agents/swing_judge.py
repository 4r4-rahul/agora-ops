"""
SwingJudgeAgent — Claude Opus 4.7 go/no-go for swing trades.

Called only when SwingCandidateScorer returns ≥ 40.

Claude receives:
  - Structured market snapshot (price, technicals, IV)
  - Scored factor breakdown from SwingCandidateScorer
  - Last N journal entries for this ticker (learning context)
  - Sector context from SectorIntelligenceAgent

Claude outputs a SwingDecision with:
  - go/no-go
  - Instrument: long call or long put
  - Entry condition (market or limit at specific price)
  - Strike, DTE, price target, stop, max hold days
  - Journal entry (thesis + what kills it)

Features used:
  - Adaptive thinking: resolves conflicting signals (e.g. cheap IV but stale catalyst)
  - Prompt caching: stable system prompt cached across calls
  - Non-streaming: response is small JSON (< 512 tokens)
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import anthropic

from ..core.config import AgoraSettings, get_settings
from ..ops.llm_cost_log import log_call as _log_llm, log_message as _log_msg
from ..ops.payload_compressor import compress_text as _compress_text
from .swing_scorer import SwingFactors

logger = logging.getLogger(__name__)

_SYSTEM = """\
You are a swing trade judge for an options trading system ($10K paper account).

You receive structured quantitative data — raw signals, a factor score, and past
trade journal entries for this ticker. Your job is to decide whether to enter a
directional long option (call or put) as a swing play.

Think about:
1. Is there a CLEAR directional catalyst with a readable setup?
2. Is the entry price a genuine dip (for calls) or rip (for puts)?
3. Which strike/expiry gives the best risk-adjusted exposure?
4. What specific price invalidates this thesis (stop)?
5. What did past journal entries teach us about this ticker/setup?

Entry rules:
- Prefer 20-45 DTE (enough time without excessive theta decay)
- Strike: near ATM (0.35–0.50 delta) for cleaner exposure
- Size: always 1 contract (small account, risk management)
- "Dip entry": underlying must pull back ≥ 1.5% from intraday high OR sit at/below SMA20
- Only go if you have ≥ 60% confidence — if uncertain, say no

Output ONLY valid JSON (no prose, no markdown):
{
  "go": true | false,
  "option_type": "call" | "put",
  "direction": "bullish" | "bearish",
  "entry_condition": "market" | "limit {price}",
  "strike": <float or null>,
  "target_expiry_dte": <int or null>,
  "price_target": <float — underlying target>,
  "stop_price": <float — underlying stop>,
  "max_hold_days": <int>,
  "confidence": <float 0.0-1.0>,
  "key_thesis": "<1 sentence>",
  "what_kills_trade": "<1 sentence invalidation condition>",
  "journal_text": "<2-3 sentences for trade journal: setup, target, what to watch>"
}

If go=false, still fill: direction, confidence, key_thesis, what_kills_trade, journal_text.
journal_text for no-go should explain WHY you passed.
"""

_CACHED_SYSTEM = [
    {"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}
]


@dataclass
class SwingDecision:
    go: bool
    option_type: str                    # "call" | "put"
    direction: str                      # "bullish" | "bearish"
    entry_condition: str                # "market" | "limit 195.50"
    strike: float | None
    target_expiry_dte: int | None
    price_target: float | None
    stop_price: float | None
    max_hold_days: int | None
    confidence: float
    key_thesis: str
    what_kills_trade: str
    journal_text: str
    raw_score: float
    factor_breakdown: dict
    method: str = "claude"             # "claude" | "fallback"
    timestamp: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))


class SwingJudgeAgent:
    """
    Calls Claude Opus 4.7 with adaptive thinking to make go/no-go swing decisions.
    Called only when raw score ≥ threshold — NOT in the hot-path per-ticker.
    """

    def __init__(self, settings: AgoraSettings | None = None) -> None:
        self._settings = settings or get_settings()
        self._client = anthropic.AsyncAnthropic(api_key=self._settings.anthropic_api_key)

    @staticmethod
    def _compute_confidence_threshold(vix: float | None, regime: str) -> float:
        """Dynamic go/no-go threshold — lower in calm markets, higher in vol stress."""
        vix_val = vix if vix is not None else 20.0
        if vix_val < 15:
            base = 0.52   # low vol / trending — easier to read direction
        elif vix_val < 20:
            base = 0.55   # normal
        elif vix_val < 25:
            base = 0.60   # elevated
        else:
            base = 0.65   # high vol — markets are choppy, require more certainty

        # Regime adjustment
        if regime in ("risk_on", "low_volatility"):
            base -= 0.03
        elif regime in ("risk_off", "high_volatility"):
            base += 0.03

        return round(max(0.45, min(0.70, base)), 2)

    async def judge(
        self,
        ticker: str,
        price: float,
        factors: SwingFactors,
        snapshot_summary: dict[str, Any],          # price, rsi, sma20/50, atr, iv_rank, etc.
        sector_context: str,
        past_journal_entries: list[dict],          # last 5 decisions for this ticker
        options_chain_summary: dict | None = None, # available strikes near ATM
        vix: float | None = None,
        regime: str = "normal",
        chart_b64: str | None = None,             # base64 PNG candlestick chart for Vision
        similar_trades: list[dict] | None = None,  # semantic memory: similar past trades
        flow_signals: dict | None = None,          # options flow: volume/OI anomalies
        trace_id: str = "",                        # chain_id for Langfuse grouping
    ) -> SwingDecision:
        """Run Claude evaluation and return a SwingDecision."""
        confidence_threshold = self._compute_confidence_threshold(vix, regime)

        user_text = self._build_prompt(
            ticker, price, factors, snapshot_summary,
            sector_context, past_journal_entries, options_chain_summary,
            confidence_threshold=confidence_threshold,
            similar_trades=similar_trades,
            flow_signals=flow_signals,
        )

        # Build user content: prepend chart image when available
        if chart_b64:
            user_msg_content: list[dict] = [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": chart_b64},
                },
                {"type": "text", "text": "Chart above: 3-month daily OHLCV with SMA20/SMA50.\n\n"},
                {"type": "text", "text": _compress_text(user_text)},
            ]
        else:
            user_msg_content = [{"type": "text", "text": _compress_text(user_text)}]

        t0 = time.monotonic()
        try:
            response = await self._client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=4096,
                thinking={"type": "adaptive"},
                output_config={"effort": "high"},
                system=_CACHED_SYSTEM,
                messages=[{"role": "user", "content": user_msg_content}],
                timeout=anthropic.Timeout(connect=30.0, read=180.0, write=30.0, pool=30.0),
            )
            elapsed = time.monotonic() - t0
            logger.info("SwingJudge Claude call OK in %.1fs | %s", elapsed, ticker)
            if hasattr(response, "usage"):
                _log_msg(str(self._settings.db_path), "SwingJudge", "claude-sonnet-4-6",
                         response.usage,
                         purpose=f"swing_judgment_{ticker}", trace_id=trace_id)

            text_blocks = [b for b in response.content if b.type == "text"]
            if not text_blocks:
                return self._fallback_decision(ticker, factors, "no text block", confidence_threshold)

            raw = text_blocks[-1].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1].lstrip("json").strip()

            data = json.loads(raw)
            return SwingDecision(
                go=bool(data.get("go", False)),
                option_type=data.get("option_type") or "call",
                direction=data.get("direction") or factors.direction,
                entry_condition=data.get("entry_condition") or "market",
                strike=_float(data.get("strike")),
                target_expiry_dte=_int(data.get("target_expiry_dte")),
                price_target=_float(data.get("price_target")),
                stop_price=_float(data.get("stop_price")),
                max_hold_days=_int(data.get("max_hold_days")) or 5,
                confidence=float(data.get("confidence", 0.5)),
                key_thesis=data.get("key_thesis", ""),
                what_kills_trade=data.get("what_kills_trade", ""),
                journal_text=data.get("journal_text", ""),
                raw_score=factors.total,
                factor_breakdown=factors.as_dict(),
                method="claude",
            )

        except Exception as exc:
            elapsed = time.monotonic() - t0
            logger.warning(
                "SwingJudge Claude call failed after %.1fs for %s [%s]: %s",
                elapsed, ticker, type(exc).__name__, exc,
            )
            return self._fallback_decision(ticker, factors, str(exc), confidence_threshold)

    # ── Prompt builder ────────────────────────────────────────────

    def _build_prompt(
        self,
        ticker: str,
        price: float,
        factors: SwingFactors,
        snap: dict[str, Any],
        sector_context: str,
        past_journal: list[dict],
        chain: dict | None,
        confidence_threshold: float = 0.55,
        similar_trades: list[dict] | None = None,
        flow_signals: dict | None = None,
    ) -> str:
        lines = [
            f"TICKER: {ticker}  |  PRICE: ${price:.2f}",
            f"SWING SCORE: {factors.total:.0f}/100  |  DIRECTION LEAN: {factors.direction.upper()}",
            f"CONFIDENCE THRESHOLD: {confidence_threshold:.0%} (dynamic — based on VIX/regime)",
            "",
            "=== FACTOR BREAKDOWN ===",
            f"  Technical:     {factors.technical:.0f}/40   {_bar(factors.technical, 40)}",
            f"  Catalyst:      {factors.catalyst:.0f}/30   {_bar(factors.catalyst, 30)}",
            f"  Fundamental:   {factors.fundamental:.0f}/15   {_bar(factors.fundamental, 15)}",
            f"  Options Setup: {factors.options_setup:.0f}/15   {_bar(factors.options_setup, 15)}",
            "",
            "=== KEY SIGNALS ===",
        ]
        for note in factors.notes:
            lines.append(f"  • {note}")

        lines += [
            "",
            "=== MARKET SNAPSHOT ===",
            f"  RSI-14:      {snap.get('rsi_14', 'n/a')}",
            f"  SMA20:       {snap.get('sma_20', 'n/a')}",
            f"  SMA50:       {snap.get('sma_50', 'n/a')}",
            f"  ATR-14:      {snap.get('atr_14', 'n/a')}",
            f"  IV Rank:     {snap.get('iv_rank', 'n/a')}",
            f"  IV Pct:      {snap.get('iv_percentile', 'n/a')}",
            f"  HV-30:       {_pct(snap.get('hist_vol_30'))}",
            f"  VIX:         {snap.get('vix', 'n/a')}",
            f"  Volume:      {snap.get('volume', 'n/a')}",
            "",
            "=== SECTOR CONTEXT ===",
            sector_context or "  (none available)",
        ]

        if chain:
            lines += ["", "=== AVAILABLE OPTIONS (ATM ±2 strikes, nearest 2 expiries) ==="]
            for exp, strikes in list(chain.items())[:2]:
                lines.append(f"  Expiry {exp}:")
                for s in strikes[:5]:
                    lines.append(
                        f"    Strike {s['strike']:.0f}  "
                        f"C-bid:{s.get('call_bid','?')}/ask:{s.get('call_ask','?')}  "
                        f"P-bid:{s.get('put_bid','?')}/ask:{s.get('put_ask','?')}  "
                        f"IV:{s.get('iv','?')}"
                    )

        if past_journal:
            lines += ["", "=== PAST JOURNAL ENTRIES (most recent first) ==="]
            for entry in past_journal[:5]:
                ts = entry.get("decision_ts", "?")[:10]
                outcome = entry.get("outcome", "open")
                pnl = entry.get("pnl_pct")
                pnl_str = f" ({pnl:+.0f}%)" if pnl is not None else ""
                lines.append(
                    f"  [{ts}] go={entry.get('go')} | direction={entry.get('direction')} | "
                    f"outcome={outcome}{pnl_str}"
                )
                if entry.get("key_thesis"):
                    lines.append(f"    Thesis: {entry['key_thesis']}")
                if entry.get("post_trade_audit"):
                    lines.append(f"    Audit:  {entry['post_trade_audit'][:120]}")
        else:
            lines += ["", "=== PAST JOURNAL ENTRIES ===", "  (no prior decisions for this ticker)"]

        if flow_signals and not flow_signals.get("error"):
            lines += [
                "",
                "=== OPTIONS FLOW (volume/OI anomalies today) ===",
                f"  Direction:     {flow_signals.get('direction', 'n/a')}",
                f"  Strength:      {flow_signals.get('strength', 'n/a')}",
                f"  Volume/OI:     {flow_signals.get('volume_ratio', 'n/a')}",
                f"  Call/Put vol:  {flow_signals.get('call_put_vol_ratio', 'n/a')}",
                f"  Unusual:       {flow_signals.get('unusual', False)}",
                f"  Summary:       {flow_signals.get('summary', 'n/a')}",
            ]
            sweeps = flow_signals.get("sweeps", [])
            if sweeps:
                lines.append(f"  Large sweeps: {len(sweeps)} strike(s) with vol/OI > 3×")
                for s in sweeps[:3]:
                    lines.append(
                        f"    {s.get('option_type','?').upper()} {s.get('strike','?'):.0f} "
                        f"vol={s.get('volume','?')} OI={s.get('open_interest','?')} "
                        f"at_ask={s.get('at_ask',False)}"
                    )

        if similar_trades:
            lines += ["", "=== SEMANTIC MEMORY (most similar past trades) ==="]
            for t in similar_trades[:5]:
                outcome = t.get("outcome", "open")
                pnl = t.get("pnl_pct")
                pnl_str = f" ({pnl:+.0f}%)" if pnl is not None and pnl != 0.0 else ""
                sim = t.get("similarity_score", 0.0)
                lines.append(
                    f"  [{t.get('ticker','?')} | {t.get('direction','?')} | "
                    f"sim={sim:.0%}] go={t.get('go')} → {outcome}{pnl_str}"
                )
                if t.get("key_thesis"):
                    lines.append(f"    Thesis: {t['key_thesis'][:100]}")
        else:
            lines += ["", "=== SEMANTIC MEMORY ===", "  (no similar past trades found)"]

        lines += ["", "Make your go/no-go decision now."]
        return "\n".join(lines)

    def _fallback_decision(
        self,
        ticker: str,
        factors: SwingFactors,
        reason: str,
        confidence_threshold: float = 0.55,
    ) -> SwingDecision:
        """Rules-based fallback when Claude is unavailable."""
        go = factors.total >= (confidence_threshold * 100) and factors.direction != "neutral"
        return SwingDecision(
            go=go,
            option_type="call" if factors.direction == "bullish" else "put",
            direction=factors.direction,
            entry_condition="market",
            strike=None,
            target_expiry_dte=30,
            price_target=None,
            stop_price=None,
            max_hold_days=5,
            confidence=0.45,
            key_thesis=f"Fallback: score={factors.total:.0f}, direction={factors.direction}",
            what_kills_trade="Claude unavailable — rule-based fallback only",
            journal_text=f"Claude unavailable ({reason}). Score={factors.total:.0f}. Go={go}.",
            raw_score=factors.total,
            factor_breakdown=factors.as_dict(),
            method="fallback",
        )


# ── Utilities ─────────────────────────────────────────────────────

def _float(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _pct(v: float | None) -> str:
    return f"{v * 100:.1f}%" if v is not None else "n/a"


def _bar(val: float, max_val: float, width: int = 10) -> str:
    filled = round(val / max_val * width)
    return "[" + "█" * filled + "░" * (width - filled) + "]"

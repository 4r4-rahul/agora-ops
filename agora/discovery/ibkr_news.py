"""
IBKRNewsAgent — real-time Dow Jones / Briefing.com news via IBKR tick 292.

ib_insync manages its own internal event loop and cannot share the uvicorn
event loop. This agent runs on a dedicated background thread with its own
asyncio event loop. Catalysts are passed back to the session via
asyncio.run_coroutine_threadsafe() onto the main loop.

Subscribes to genericTickList="mdoff,292" for each universe ticker.
Incoming headlines are:
  1. Deduplicated by article base-ID (same story arrives through multiple providers)
  2. Pre-filtered by keyword before spending a Claude token
  3. Classified by Claude Haiku into a Catalyst object
  4. Emitted via on_catalyst() → event pillar
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine

import anthropic
from ib_insync import IB, Stock

from ..core.models import Catalyst, CatalystType

logger = logging.getLogger(__name__)

# ── Claude classification prompt ──────────────────────────────────────────────

_SYSTEM_PROMPT = """
You are a financial news classifier for an options trading system.

Given a news headline (and optional article body), return ONLY a JSON object:
{
  "ticker":      "<primary ticker, e.g. AAPL — or SPY for macro news>",
  "catalyst_type": "<one of: earnings_beat, earnings_miss, guidance_raise, guidance_cut,
                     ma_target, partnership, contract_win, funding_round, fda_approval,
                     fda_rejection, activist_13d, insider_cluster, macro_bullish, macro_bearish>",
  "direction":   "<bullish | bearish | neutral>",
  "strength":    "<strong | moderate | weak>",
  "reason":      "<one sentence summary>",
  "derivative_tickers": ["<other tickers affected — max 3>"]
}

Rules:
- Use macro_bullish / macro_bearish for Fed/CPI/tariff/GDP/yield news (set ticker=SPY)
- direction=neutral means no tradeable edge — still classify type and strength
- Only include derivative_tickers if genuinely materially affected
- Analyst upgrades/downgrades → direction = bullish/bearish, strength = moderate
""".strip()

_MACRO_KEYWORDS = {
    "FED", "FOMC", "CPI", "INFLATION", "RATE", "TARIFF",
    "RECESSION", "GDP", "JOBS", "PAYROLL", "POWELL", "YIELD",
    "TREASURY", "CHINA", "TRADE", "SANCTIONS", "OPEC",
}
_ANALYST_KEYWORDS = {
    "UPGRADE", "DOWNGRADE", "RAISE", "LOWER", "TARGET",
    "INITIATE", "OUTPERFORM", "UNDERPERFORM",
}
_NOISE_PATTERNS = [
    " ETF GAINS ", " ETF RISES ", " ETF DECLINES ", " ETF CLIMBS ",
    " ETF FALLS ", " ETF DROPS ", "CLIMBS 0.", "FALLS 0.", "GAINS 0.",
]


class IBKRNewsAgent:
    """
    Real-time IBKR news subscriber and catalyst classifier.

    Runs ib_insync on a dedicated daemon thread so it never conflicts
    with uvicorn's event loop. Callbacks are marshalled back to the
    session's main loop via run_coroutine_threadsafe.
    """

    _MIN_CALL_INTERVAL = 3.0   # seconds between Claude calls
    _EMIT_COOLDOWN_SECS = 1800  # 30 min: block same (ticker, catalyst_type) from re-emitting

    def __init__(
        self,
        settings: Any,
        on_catalyst: Callable[..., Coroutine],
    ) -> None:
        self._settings    = settings
        self._on_catalyst = on_catalyst
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._thread:    threading.Thread | None = None
        self._ib_loop:   asyncio.AbstractEventLoop | None = None
        self._running    = False
        self._seen:      set[str] = set()
        self._last_call  = 0.0
        # Cross-provider dedup: same story arrives from DJ-N, DJ-RTG, DJ-RTPRO with
        # distinct articleIds. Track (ticker, catalyst_type) → last emit timestamp.
        self._emit_seen:  dict[tuple, float] = {}  # key → monotonic time
        self._client     = anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key
        )
        self._csuite_manager: Any = None   # CIOAgent — set via register_csuite_manager()
        self._headlines_classified: int = 0
        self._strong_signals: int = 0

    def register_csuite_manager(self, manager: Any) -> None:
        """Wire the CIOAgent as supervising executive."""
        self._csuite_manager = manager

    def get_stats(self) -> dict:
        """Return headline classification stats for CIO briefing."""
        return {
            "headlines_classified": self._headlines_classified,
            "strong_signals": self._strong_signals,
            "connected": self._thread is not None and self._thread.is_alive(),
        }

    # ── Public lifecycle (called from main asyncio loop) ───────────────────────

    async def start(self) -> None:
        self._running   = True
        self._main_loop = asyncio.get_running_loop()
        self._thread    = threading.Thread(
            target=self._thread_main, daemon=True, name="ibkr-news"
        )
        self._thread.start()
        logger.info("IBKRNewsAgent thread started")

    async def stop(self) -> None:
        self._running = False
        # Do NOT call self._ib_loop.stop() here — it interrupts pending futures
        # (asyncio.ensure_future calls from _on_tick) and produces
        # "Event loop stopped before Future completed."
        # Instead, set _running=False and let the while loop in _ib_session
        # exit naturally, then the thread joins and the loop closes cleanly.
        if self._thread:
            self._thread.join(timeout=10)

    # ── Dedicated thread ───────────────────────────────────────────────────────

    def _thread_main(self) -> None:
        """Entry point for the dedicated ib_insync thread."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._ib_loop = loop
        try:
            loop.run_until_complete(self._ib_session())
        except Exception as exc:
            logger.warning("IBKRNewsAgent thread exited: %s", exc)
        finally:
            # Drain any remaining tasks before closing to avoid "Future destroyed" warnings
            try:
                pending = asyncio.all_tasks(loop)
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            loop.close()

    async def _ib_session(self) -> None:
        ib = IB()
        connected = False
        try:
            await ib.connectAsync(
                self._settings.ibkr_host,
                self._settings.ibkr_port,
                clientId=4,
                timeout=10,
            )
            connected = True
        except Exception as exc:
            logger.warning(
                "IBKRNewsAgent could not connect to IBKR — "
                "news feed disabled (session continues without it): %s", exc
            )
            return

        # Get subscribed providers
        try:
            providers = await ib.reqNewsProvidersAsync()
            prov_str  = ", ".join(p.code for p in providers)
            logger.info("IBKRNewsAgent providers: %s", prov_str)
        except Exception:
            prov_str = ""

        # Suppress non-actionable IBKR error codes from cluttering the log.
        # 10197: "No market data permissions" — fires when a competing live session
        #        holds a market data subscription. Harmless for news-tick-292 workflow.
        # 10090: "Part of requested market data is not subscribed" — same class.
        _SUPPRESS_CODES = {10197, 10090, 2104, 2106, 2158}

        def _on_ib_error(req_id, error_code, error_str, contract):
            if error_code in _SUPPRESS_CODES:
                logger.debug("IBKR [%d] %s", error_code, error_str[:80])
            else:
                logger.warning("IBKR error [%d]: %s", error_code, error_str[:120])

        ib.errorEvent += _on_ib_error

        # Subscribe to universe tickers
        universe  = self._settings.etf_universe or []
        contracts = [Stock(sym, "SMART", "USD") for sym in universe if sym]
        try:
            await ib.qualifyContractsAsync(*contracts)
        except Exception as exc:
            logger.warning("IBKRNewsAgent qualify failed: %s", exc)

        seen_local: set[str] = set()

        def _on_tick(news_tick) -> None:
            # Runs synchronously in ib_insync's event loop — schedule processing
            asyncio.ensure_future(self._handle_tick(news_tick, ib, seen_local))

        ib.tickNewsEvent += _on_tick

        for c in contracts:
            try:
                ib.reqMktData(c, genericTickList="mdoff,292", snapshot=False)
            except Exception:
                pass

        logger.info(
            "IBKRNewsAgent subscribed to %d tickers for real-time news", len(contracts)
        )

        # Keep the session alive until stop() is called
        while self._running and ib.isConnected():
            await asyncio.sleep(5)

        for c in contracts:
            try:
                ib.cancelMktData(c)
            except Exception:
                pass
        ib.disconnect()

    # ── Tick handler (runs on ib_insync's thread loop) ────────────────────────

    async def _handle_tick(self, tick, ib: IB, seen: set[str]) -> None:
        base_id = tick.articleId.split("$")[-1] if "$" in tick.articleId else tick.articleId
        if base_id in seen:
            return
        seen.add(base_id)
        if len(seen) > 5_000:
            # Rolling cleanup
            excess = list(seen)[:2_500]
            for k in excess:
                seen.discard(k)

        headline = tick.headline or ""
        provider = tick.providerCode or ""
        upper    = headline.upper()

        # Drop pure price-performance noise
        if any(pat in upper for pat in _NOISE_PATTERNS):
            return

        universe_set = {s.upper() for s in (self._settings.etf_universe or [])}
        ticker_hit = next((s for s in universe_set if s in upper), None)
        is_macro   = any(kw in upper for kw in _MACRO_KEYWORDS)
        is_analyst = provider == "BRFUPDN" and any(kw in upper for kw in _ANALYST_KEYWORDS)

        if not ticker_hit and not is_macro and not is_analyst:
            return

        logger.info("IBKR news [%s]: %s", provider, headline[:120])

        # Fetch article body if available
        article_text = ""
        try:
            article = await asyncio.wait_for(
                ib.reqNewsArticleAsync(provider, tick.articleId), timeout=5.0
            )
            if article and article.articleText:
                article_text = article.articleText[:3_000]
        except Exception:
            pass

        await self._classify_and_emit(
            headline=headline,
            article_text=article_text,
            provider=provider,
            hint_ticker=ticker_hit or ("SPY" if is_macro else None),
        )

    # ── Claude classification (runs on ib_insync's thread loop) ───────────────

    async def _classify_and_emit(
        self,
        headline: str,
        article_text: str,
        provider: str,
        hint_ticker: str | None,
    ) -> None:
        # Rate-limit
        now = asyncio.get_event_loop().time()
        gap = now - self._last_call
        if gap < self._MIN_CALL_INTERVAL:
            await asyncio.sleep(self._MIN_CALL_INTERVAL - gap)
        self._last_call = asyncio.get_event_loop().time()

        try:
            body = headline + (f"\n\n{article_text}" if article_text else "")
            response = await self._client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                system=[{
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": body}],
            )
            raw = response.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            data = json.loads(raw)
        except Exception as exc:
            logger.debug("IBKRNewsAgent Claude call failed: %s", exc)
            return

        direction = data.get("direction", "neutral")
        if direction == "neutral":
            return

        try:
            catalyst_type = CatalystType(data.get("catalyst_type", "partnership"))
        except ValueError:
            catalyst_type = CatalystType.PARTNERSHIP

        ticker = (data.get("ticker") or hint_ticker or "SPY").upper()

        catalyst = Catalyst(
            ticker=ticker,
            catalyst_type=catalyst_type,
            filing_time=datetime.now(tz=timezone.utc),
            headline=data.get("reason", headline[:200]),
            direction=direction,
            strength=data.get("strength", "moderate"),
            derivative_tickers=data.get("derivative_tickers", []),
            raw_text=headline,
            source=f"ibkr_{provider.lower().replace('-', '_')}",
        )

        logger.info(
            "IBKRNewsAgent catalyst: %s %s %s (%s) via %s",
            catalyst.ticker, catalyst.direction, catalyst.catalyst_type,
            catalyst.strength, provider,
        )

        self._headlines_classified += 1
        if catalyst.strength == "strong":
            self._strong_signals += 1

        # Cross-provider dedup: DJ-N, DJ-RTG, DJ-RTPRO deliver the same story with
        # distinct articleIds. Block re-emit of the same (ticker, catalyst_type) for 30 min.
        emit_key = (catalyst.ticker, catalyst.catalyst_type.value)
        now_mono = asyncio.get_event_loop().time()
        last_emit = self._emit_seen.get(emit_key, 0.0)
        if now_mono - last_emit < self._EMIT_COOLDOWN_SECS:
            logger.debug(
                "IBKRNews cross-provider dedup: %s/%s emitted %.0fs ago — suppressed",
                catalyst.ticker, catalyst.catalyst_type.value, now_mono - last_emit,
            )
            return
        self._emit_seen[emit_key] = now_mono
        # Evict stale entries
        cutoff = now_mono - self._EMIT_COOLDOWN_SECS
        self._emit_seen = {k: v for k, v in self._emit_seen.items() if v >= cutoff}

        # Marshal the async callback back onto the main event loop
        if self._main_loop and self._main_loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self._on_catalyst(catalyst), self._main_loop
            )
            if catalyst.strength == "strong" and self._csuite_manager:
                asyncio.run_coroutine_threadsafe(
                    self._csuite_manager.receive_alert(
                        "IBKRNews", "info",
                        f"IBKR news strong signal: {catalyst.direction} {catalyst.ticker} "
                        f"via {provider} — {catalyst.headline[:100]}",
                    ),
                    self._main_loop,
                )

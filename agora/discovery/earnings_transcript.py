"""
EarningsTranscriptAgent — deep post-earnings analysis using Claude.

Two activation modes:
  T+0 (same day as report):
    - Infer derivative tickers (sector peers, customers, suppliers)
    - Fire immediately into CatalystDiscoveryAgent callback

  T+1 to T+3 (post-earnings window):
    - Feed beat_quality + guidance_tone into EventPatternEngine
    - EventPatternEngine activates post_earnings_skew signal
    - Conviction scorer picks up the signal for bull_put_spread sizing

Data sources (all free):
  1. EDGAR EFTS: 8-K Item 2.02 (earnings press release text)
  2. SEC filing viewer: full text of earnings release
  3. yfinance: spot price, options chain for liquidity gate

Claude API features used:
  - Extended thinking (adaptive) for derivative inference — multi-hop reasoning
  - Prompt caching on extraction schema (stable system prompt)
  - Streaming: first token < 3s for T+0 urgency
  - Document blocks: feed filing text as structured doc
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import anthropic
import httpx

from ..core.config import AgoraSettings, get_settings
from ..core.json_extract import extract_json as _extract_json
from ..core.models import Catalyst, CatalystType

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

_EDGAR_EARNINGS_SEARCH = (
    "https://efts.sec.gov/LATEST/search-index"
    "?q=%22%22&forms=8-K&dateRange=custom&startdt={date}"
    "&hits.hits._source=period_of_report,entity_name,file_date,form_type,file_num,items"
)

_EXTRACT_SYSTEM = """\
You are a financial earnings analyst for an options trading system.
You receive an SEC 8-K earnings press release or earnings call transcript.

Extract the following fields as JSON:
{
  "beat_quality":    "strong" | "moderate" | "miss" | "in_line",
  "eps_surprise_pct": <float | null>,      // (actual - estimate) / |estimate| * 100
  "revenue_beat":    true | false | null,
  "guidance_tone":   "raised" | "flat" | "lowered" | "none",
  "guidance_pct":    <float | null>,       // % above/below prior guidance midpoint
  "one_time_items":  ["description", ...], // charges, gains that inflated/deflated EPS
  "management_tone": "confident" | "cautious" | "neutral",
  "derivative_tickers": ["TICK1", "TICK2", ...], // peers, customers, suppliers affected
  "sector_contagion": "positive" | "negative" | "neutral",
  "beat_type":       "clean" | "one_time_boosted" | "miss_with_guide_up" | "guide_cut",
  "headline":        "<1 sentence summary>"
}

Rules:
- beat_quality = "strong" only if EPS beat > 5% AND revenue beat AND guidance raised.
- beat_quality = "moderate" if EPS beat 0-5% OR revenue beat alone.
- beat_quality = "miss" if EPS miss, regardless of guidance.
- one_time_boosted = strong/moderate EPS but aided by tax benefit, asset sale, buyback.
- derivative_tickers: think 2 hops — direct competitors, major customers, component suppliers.
  E.g. NVDA earnings → AMD, INTC, TSM, ASML, QCOM, DELL, HPQ (data center customers).
- Output JSON only, no markdown.
"""

# Stable prefix cache breakpoint — this whole system prompt is cached
_SYSTEM_CACHE_BLOCK = [
    {
        "type": "text",
        "text": _EXTRACT_SYSTEM,
        "cache_control": {"type": "ephemeral"},
    }
]


class EarningsResult:
    """Parsed earnings analysis result."""
    __slots__ = (
        "ticker", "entity_name", "filing_date", "beat_quality",
        "eps_surprise_pct", "revenue_beat", "guidance_tone", "guidance_pct",
        "one_time_items", "management_tone", "derivative_tickers",
        "sector_contagion", "beat_type", "headline", "raw_text",
    )

    def __init__(self, **kw: Any) -> None:
        for k, v in kw.items():
            setattr(self, k, v)

    def is_tradeable(self) -> bool:
        """Strong or moderate beats/misses with clean beats only."""
        return (
            self.beat_quality in ("strong", "moderate", "miss")
            and self.beat_type != "one_time_boosted"
        )


class EarningsTranscriptAgent:
    """
    Monitors EDGAR for Item 2.02 earnings releases in real time.
    Classifies each with Claude extended thinking, fires catalysts and
    derivative signals via callbacks.
    """

    def __init__(
        self,
        settings: AgoraSettings | None = None,
        on_earnings: Any = None,    # async callback(result: EarningsResult)
        on_catalyst: Any = None,    # async callback(catalyst: Catalyst)
    ) -> None:
        self._settings = settings or get_settings()
        self._on_earnings = on_earnings
        self._on_catalyst = on_catalyst
        self._client = anthropic.AsyncAnthropic(api_key=self._settings.anthropic_api_key)
        self._seen_hashes: set[str] = set()
        self._running = False
        self._csuite_manager: Any = None   # RNDAgent — set via register_csuite_manager()
        self._recent_results: list[dict] = []   # rolling buffer for R&D briefing
        # Files API cache: text_hash → file_id (avoids re-uploading same filing)
        self._file_id_cache: dict[str, str] = {}

    def register_csuite_manager(self, manager: Any) -> None:
        """Wire the RNDAgent as supervising executive."""
        self._csuite_manager = manager

    def get_recent_results(self) -> list[dict]:
        """Return last 10 earnings analysis results for R&D briefing."""
        return self._recent_results[-10:]

    async def start(self) -> None:
        self._running = True
        logger.info("EarningsTranscriptAgent started — polling every %ds", self._settings.edgar_poll_seconds)
        while self._running:
            try:
                await self._poll_cycle()
            except Exception as exc:
                logger.error("Earnings poll cycle error: %s", exc)
            await asyncio.sleep(self._settings.edgar_poll_seconds)

    async def stop(self) -> None:
        self._running = False

    # ── Poll cycle ─────────────────────────────────────────────────

    async def _poll_cycle(self) -> None:
        now_et = datetime.now(tz=ET)
        # Earnings can drop pre-market (6 AM) and after-hours (up to 10 PM)
        if not (6 <= now_et.hour < 22):
            return

        today_str = now_et.date().isoformat()
        filings = await self._fetch_earnings_filings(today_str)
        tasks = [self._process_earnings_filing(f) for f in filings]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _fetch_earnings_filings(self, date_str: str) -> list[dict]:
        """Fetch 8-K Item 2.02 filings (earnings results) from EDGAR."""
        url = (
            "https://efts.sec.gov/LATEST/search-index"
            f"?q=%222.02%22&forms=8-K&dateRange=custom&startdt={date_str}"
            "&hits.hits._source=period_of_report,entity_name,file_date,form_type,file_num"
        )
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    url,
                    headers={"User-Agent": "AGORA/1.0 trading@agora.local"},
                )
                data = resp.json()
                return data.get("hits", {}).get("hits", [])
        except Exception as exc:
            logger.debug("EDGAR earnings fetch failed: %s", exc)
            return []

    async def _process_earnings_filing(self, filing: dict) -> None:
        source = filing.get("_source", {})
        entity = source.get("entity_name", "Unknown")
        file_num = source.get("file_num", "")

        h = hashlib.md5(file_num.encode()).hexdigest()
        if h in self._seen_hashes:
            return
        self._seen_hashes.add(h)

        text = await self._fetch_filing_text(file_num)
        if not text or len(text) < 200:
            return

        result = await self._analyze_earnings(entity, file_num, text)
        if not result:
            return

        if not result.is_tradeable():
            return

        logger.info(
            "EARNINGS: %s | beat=%s | guide=%s | type=%s",
            result.ticker, result.beat_quality, result.guidance_tone, result.beat_type,
        )

        self._recent_results.append({
            "ticker": result.ticker,
            "beat_quality": result.beat_quality,
            "guidance_tone": result.guidance_tone,
            "beat_type": result.beat_type,
            "sector_contagion": result.sector_contagion,
            "ts": datetime.now(tz=ET).isoformat(),
        })
        if len(self._recent_results) > 50:
            self._recent_results = self._recent_results[-50:]

        if self._on_earnings:
            await self._on_earnings(result)

        # Notify R&D of strong earnings results
        if result.beat_quality in ("strong", "miss") and self._csuite_manager:
            await self._csuite_manager.receive_alert(
                "EarningsTranscript", "info",
                f"Earnings: {result.ticker} | beat={result.beat_quality} "
                f"guide={result.guidance_tone} type={result.beat_type} "
                f"contagion={result.sector_contagion}",
            )

        # Fire derivative catalysts (T+0 sector contagion)
        await self._fire_derivative_catalysts(result)

    async def _fetch_filing_text(self, file_num: str) -> str:
        """Fetch filing text from EDGAR — try full text viewer, fall back to search highlight."""
        # Strategy 1: Search by file_num and get highlight
        search_url = (
            f"https://efts.sec.gov/LATEST/search-index"
            f"?q=%22%22&file_num={file_num}&forms=8-K"
        )
        try:
            async with httpx.AsyncClient(timeout=12) as client:
                resp = await client.get(
                    search_url,
                    headers={"User-Agent": "AGORA/1.0 trading@agora.local"},
                )
                data = resp.json()
                hits = data.get("hits", {}).get("hits", [])
                if hits:
                    # Get the filing document URL for full text
                    hit = hits[0]
                    src = hit.get("_source", {})

                    # Try to get actual filing text via EDGAR viewer
                    accession = self._file_num_to_accession(src, hit.get("_id", ""))
                    if accession:
                        full_text = await self._fetch_full_text(accession)
                        if full_text:
                            return full_text[:6000]

                    # Fall back to highlight
                    highlight = hit.get("highlight", {})
                    texts: list[str] = []
                    for v in highlight.values():
                        if isinstance(v, list):
                            texts.extend(v)
                    return " ".join(texts)[:6000]
        except Exception as exc:
            logger.debug("Filing text fetch failed: %s", exc)
        return ""

    def _file_num_to_accession(self, source: dict, hit_id: str) -> str | None:
        """Extract accession number from EDGAR hit."""
        # The _id field is typically the accession number
        if hit_id and re.match(r"\d{10}-\d{2}-\d{6}", hit_id):
            return hit_id
        return None

    async def _fetch_full_text(self, accession: str) -> str:
        """Attempt to fetch actual 8-K document text from EDGAR."""
        # Convert accession number format: 0001234567-24-012345 → 0001234567/24/012345
        acc_clean = accession.replace("-", "")
        cik_part = acc_clean[:10]
        url = f"https://www.sec.gov/Archives/edgar/data/{int(cik_part)}/{acc_clean}/{accession}-index.htm"
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                resp = await client.get(
                    url,
                    headers={"User-Agent": "AGORA/1.0 trading@agora.local"},
                )
                if resp.status_code == 200:
                    # Very rough text extraction from HTML
                    text = re.sub(r"<[^>]+>", " ", resp.text)
                    text = re.sub(r"\s+", " ", text)
                    return text[:6000]
        except Exception:
            pass
        return ""

    async def _get_or_upload_filing(self, text: str) -> str | None:
        """
        Upload filing text to Files API and return file_id.
        Caches by content hash so the same filing is never re-uploaded within a session.
        Falls back gracefully if Files API is unavailable.
        """
        import io as _io
        text_hash = hashlib.md5(text.encode()).hexdigest()
        if text_hash in self._file_id_cache:
            return self._file_id_cache[text_hash]
        try:
            file_bytes = _io.BytesIO(text.encode("utf-8"))
            resp = await self._client.beta.files.upload(
                file=("earnings_filing.txt", file_bytes, "text/plain"),
                extra_headers={"anthropic-beta": "files-api-2025-04-14"},
            )
            self._file_id_cache[text_hash] = resp.id
            logger.debug("Files API: uploaded filing %s → %s", text_hash[:8], resp.id)
            return resp.id
        except Exception as exc:
            logger.debug("Files API upload failed (will embed text): %s", exc)
            return None

    async def _analyze_earnings(
        self, entity: str, file_num: str, text: str
    ) -> EarningsResult | None:
        """
        Claude Opus with extended thinking for thorough derivative inference.
        Uses prompt-cached system prompt + streaming for fast first token.
        Filing text uploaded via Files API to avoid re-tokenizing on T+1 re-analysis.
        """
        try:
            filing_text = text[:5000]

            # Try Files API first — avoids re-tokenizing the same filing on T+1 re-analysis
            file_id = await self._get_or_upload_filing(filing_text)

            if file_id:
                user_content: list[Any] = [
                    {
                        "type": "text",
                        "text": f"Company: {entity}\nFiling number: {file_num}\n\nAnalyze the attached earnings filing:",
                    },
                    {
                        "type": "document",
                        "source": {"type": "file", "file_id": file_id},
                    },
                ]
            else:
                # Fallback: embed text directly
                user_content = (
                    f"Company: {entity}\n"
                    f"Filing number: {file_num}\n\n"
                    f"Earnings release text:\n{filing_text}"
                )

            # Stream with extended thinking — adaptive lets Claude self-calibrate depth
            full_text = ""
            stream_kwargs: dict[str, Any] = dict(
                model=self._settings.claude_model,
                max_tokens=2048,
                thinking={"type": "adaptive"},
                system=_SYSTEM_CACHE_BLOCK,
                messages=[{"role": "user", "content": user_content}],
            )
            if file_id:
                stream_kwargs["extra_headers"] = {"anthropic-beta": "files-api-2025-04-14"}

            async with self._client.messages.stream(**stream_kwargs) as stream:
                async for text_chunk in stream.text_stream:
                    full_text += text_chunk

            data = _extract_json(full_text)

            # Derive ticker from entity name (best effort — will be refined later)
            ticker = self._infer_ticker(entity, data)

            return EarningsResult(
                ticker=ticker,
                entity_name=entity,
                filing_date=date.today(),
                beat_quality=data.get("beat_quality", "in_line"),
                eps_surprise_pct=data.get("eps_surprise_pct"),
                revenue_beat=data.get("revenue_beat"),
                guidance_tone=data.get("guidance_tone", "none"),
                guidance_pct=data.get("guidance_pct"),
                one_time_items=data.get("one_time_items", []),
                management_tone=data.get("management_tone", "neutral"),
                derivative_tickers=data.get("derivative_tickers", []),
                sector_contagion=data.get("sector_contagion", "neutral"),
                beat_type=data.get("beat_type", "clean"),
                headline=data.get("headline", f"{entity} earnings"),
                raw_text=text[:500],
            )

        except json.JSONDecodeError as exc:
            logger.debug("Earnings JSON parse failed for %s: %s", entity, exc)
            return None
        except Exception as exc:
            logger.debug("Earnings analysis failed for %s: %s", entity, exc)
            return None

    def _infer_ticker(self, entity: str, data: dict) -> str:
        """
        Attempt ticker from Claude's output first, then strip common suffixes.
        CatalystDiscoveryAgent's liquidity gate will reject invalid tickers naturally.
        """
        # Claude sometimes includes the ticker in derivative_tickers[0] or headline
        headline = data.get("headline", "")
        ticker_match = re.search(r"\b([A-Z]{2,5})\b", headline)
        if ticker_match:
            candidate = ticker_match.group(1)
            # Filter out common financial abbreviations
            if candidate not in {"EPS", "YOY", "QOQ", "USD", "CEO", "CFO", "CTO", "IPO"}:
                return candidate

        # Strip common suffixes from entity name
        clean = re.sub(
            r"\b(Inc|Corp|Ltd|LLC|LP|Co|Group|Holdings|Technologies|Systems|International)\b\.?",
            "", entity, flags=re.IGNORECASE,
        ).strip()
        return clean[:5].upper().replace(" ", "")

    async def _fire_derivative_catalysts(self, result: EarningsResult) -> None:
        """
        T+0 sector contagion: for each derivative ticker, check liquidity and
        fire a catalyst event so the main pipeline can price the spread.
        """
        if not result.derivative_tickers or not self._on_catalyst:
            return
        if result.sector_contagion == "neutral":
            return

        direction = (
            "bullish" if result.sector_contagion == "positive"
            else "bearish"
        )
        # Only propagate to first 3 derivatives to control exposure
        for ticker in result.derivative_tickers[:3]:
            catalyst = Catalyst(
                ticker=ticker,
                catalyst_type=CatalystType.EARNINGS_BEAT
                if result.beat_quality in ("strong", "moderate")
                else CatalystType.EARNINGS_MISS,
                filing_time=datetime.now(tz=timezone.utc),
                headline=f"Derivative: {result.entity_name} {result.beat_quality} earnings — {result.sector_contagion} sector read-through",
                direction=direction,
                strength="moderate",   # derivatives are always moderate — primary ticker absorbed the move
                derivative_tickers=[],
                source="earnings_derivative",
            )
            try:
                await self._on_catalyst(catalyst)
            except Exception as exc:
                logger.debug("Derivative catalyst fire failed for %s: %s", ticker, exc)

    # ── Public convenience ──────────────────────────────────────────

    async def analyze_ticker(self, ticker: str, filing_text: str) -> EarningsResult | None:
        """
        One-shot analysis for a known ticker + filing text.
        Used by the conviction scorer when it needs fresh T+1 post-earnings data.
        """
        return await self._analyze_earnings(ticker, "", filing_text)

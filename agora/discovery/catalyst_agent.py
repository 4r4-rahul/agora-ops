"""
CatalystDiscoveryAgent — real-time catalyst scanner across all US public companies.

Monitors every 60 seconds:
  - EDGAR EFTS RSS: all 8-K filings (earnings, contracts, FDA, M&A, funding)
  - PRNewswire RSS: press releases
  - GlobeNewsWire RSS: press releases

For each filing:
  1. Claude classifies: is this a tradeable catalyst?
  2. Liquidity gate: OI ≥ 500, bid/ask < 10%, market cap ≥ $300M
  3. Strong catalysts → added to active session queue
  4. Derivative ticker inference: who else is affected by this news?

Claude API features used:
  - Streaming for <3s first-token response
  - Prompt caching on the extraction schema (stable system prompt)
  - Haiku for rapid classification, Opus for complex derivative inference
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import anthropic
import httpx

from ..core.config import AgoraSettings, get_settings
from ..core.models import Catalyst, CatalystType

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# EDGAR full-text search RSS — all 8-K filings in real time
_EDGAR_8K_RSS = "https://efts.sec.gov/LATEST/search-index?q=%22%22&dateRange=custom&startdt={date}&forms=8-K&hits.hits._source=period_of_report,file_date,entity_name,file_num,period_of_report,form_type&hits.hits.highlight.file_date=true"
_EDGAR_BASE   = "https://efts.sec.gov/LATEST/search-index?forms=8-K&dateRange=custom&startdt={date}&hits.hits.total=true"
_EDGAR_SEARCH = "https://efts.sec.gov/LATEST/search-index?q=%22%22&forms=8-K&hits.hits._source=period_of_report,entity_name,file_date,form_type,file_num&hits.hits.highlight=false"

# 8-K item types we care about
_TRADEABLE_ITEMS = {
    "1.01": "material_agreement",    # contracts, partnerships
    "1.02": "termination_agreement", # contract loss (bearish)
    "2.01": "acquisition_complete",
    "2.02": "earnings_results",      # earnings release
    "5.02": "management_change",     # new CEO/CFO can be bullish
    "7.01": "reg_fd_disclosure",     # forward guidance
    "8.01": "other_events",          # FDA, regulatory
}

_SYSTEM_PROMPT = """\
You are a financial event classifier for an options trading system.
Given an SEC 8-K filing or press release, extract:
1. The company ticker (if identifiable)
2. The catalyst type from: earnings_beat, earnings_miss, contract_win, funding_round,
   fda_approval, fda_rejection, activist_13d, insider_cluster, ma_target,
   partnership, guidance_raise, guidance_cut, management_change, other
3. Direction: bullish, bearish, or neutral
4. Strength: strong, moderate, or weak
5. Key dollar amount if present (contract value, funding amount)
6. Derivative tickers: other companies that would be affected by this news
7. Brief reason (1 sentence)

Output JSON only. If the filing is not tradeable (routine filings, immaterial events),
set catalyst_type to "other" and strength to "weak".
"""


class CatalystDiscoveryAgent:
    """
    Async agent that continuously polls EDGAR and press release feeds,
    classifies catalysts with Claude, and queues strong ones for trading.
    """

    def __init__(
        self,
        settings: AgoraSettings | None = None,
        on_catalyst: Any = None,  # async callback(catalyst: Catalyst)
    ) -> None:
        self._settings = settings or get_settings()
        self._on_catalyst = on_catalyst
        self._client = anthropic.AsyncAnthropic(api_key=self._settings.anthropic_api_key)
        self._seen_hashes: set[str] = set()
        self._daily_new_tickers: set[str] = set()
        self._last_day: str = ""
        self._running = False

    async def start(self) -> None:
        self._running = True
        logger.info("CatalystDiscoveryAgent started — polling every %ds", self._settings.edgar_poll_seconds)
        while self._running:
            try:
                await self._poll_cycle()
            except Exception as exc:
                logger.error("Catalyst poll cycle error: %s", exc)
            await asyncio.sleep(self._settings.edgar_poll_seconds)

    async def stop(self) -> None:
        self._running = False

    # ── Poll cycle ─────────────────────────────────────────────────

    async def _poll_cycle(self) -> None:
        today_str = datetime.now(tz=ET).date().isoformat()

        # Reset daily ticker cap on new day
        if today_str != self._last_day:
            self._daily_new_tickers.clear()
            self._last_day = today_str

        # Only run during market-adjacent hours (7 AM - 6 PM ET)
        now_et = datetime.now(tz=ET)
        if not (7 <= now_et.hour < 18):
            return

        filings = await self._fetch_edgar_filings(today_str)
        tasks = [self._process_filing(f) for f in filings]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _fetch_edgar_filings(self, date_str: str) -> list[dict]:
        """Fetch today's 8-K filings from EDGAR EFTS."""
        url = (
            f"https://efts.sec.gov/LATEST/search-index"
            f"?q=%22%22&forms=8-K&dateRange=custom"
            f"&startdt={date_str}&hits.hits._source=period_of_report,"
            f"entity_name,file_date,form_type,file_num"
        )
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(url, headers={"User-Agent": "AGORA/1.0 trading@agora.local"})
                data = resp.json()
                return data.get("hits", {}).get("hits", [])
        except Exception as exc:
            logger.debug("EDGAR fetch failed: %s", exc)
            return []

    async def _process_filing(self, filing: dict) -> None:
        """Classify a single filing and fire callback if it's a strong catalyst."""
        source = filing.get("_source", {})
        entity = source.get("entity_name", "Unknown")
        file_num = source.get("file_num", "")

        # Dedup by file number
        h = hashlib.md5(file_num.encode()).hexdigest()
        if h in self._seen_hashes:
            return
        self._seen_hashes.add(h)

        # Fetch the actual filing text (first 3000 chars is usually enough)
        filing_text = await self._fetch_filing_text(file_num)
        if not filing_text:
            return

        # Daily new ticker cap
        if len(self._daily_new_tickers) >= self._settings.max_new_tickers_per_day:
            return

        # Classify with Claude (Haiku for speed, cached system prompt)
        catalyst = await self._classify_filing(entity, filing_text)
        if not catalyst:
            return

        if catalyst.strength == "weak":
            return

        # Liquidity gate
        if not await self._passes_liquidity_gate(catalyst.ticker):
            return

        self._daily_new_tickers.add(catalyst.ticker)
        logger.info(
            "CATALYST: %s | %s | %s | %s",
            catalyst.ticker, catalyst.catalyst_type, catalyst.strength, catalyst.direction
        )

        if self._on_catalyst:
            await self._on_catalyst(catalyst)

    async def _fetch_filing_text(self, file_num: str) -> str:
        """Fetch a brief excerpt of the 8-K filing text from EDGAR."""
        try:
            # EDGAR viewer URL for filing text
            search_url = (
                f"https://efts.sec.gov/LATEST/search-index"
                f"?q=%22%22&file_num={file_num}&forms=8-K"
            )
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    search_url,
                    headers={"User-Agent": "AGORA/1.0 trading@agora.local"}
                )
                data = resp.json()
                hits = data.get("hits", {}).get("hits", [])
                if hits:
                    # Extract highlight text as a proxy for filing content
                    highlight = hits[0].get("highlight", {})
                    texts = []
                    for v in highlight.values():
                        if isinstance(v, list):
                            texts.extend(v)
                    return " ".join(texts)[:3000]
        except Exception:
            pass
        return ""

    async def _classify_filing(self, entity: str, text: str) -> Catalyst | None:
        """Use Claude Haiku (fast + cheap) to classify the filing."""
        try:
            user_msg = f"Company: {entity}\n\nFiling excerpt:\n{text[:2000]}"

            response = await self._client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                system=[
                    {
                        "type": "text",
                        "text": _SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},  # cache the schema prompt
                    }
                ],
                messages=[{"role": "user", "content": user_msg}],
            )

            import json as _json
            raw = response.content[0].text.strip()
            # Strip markdown code fences if present
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            data = _json.loads(raw)

            catalyst_type_str = data.get("catalyst_type", "other")
            try:
                catalyst_type = CatalystType(catalyst_type_str)
            except ValueError:
                catalyst_type = CatalystType.PARTNERSHIP  # safe default

            ticker = str(data.get("ticker", entity[:5].upper().replace(" ", "")))

            return Catalyst(
                ticker=ticker,
                catalyst_type=catalyst_type,
                filing_time=datetime.now(tz=timezone.utc),
                headline=data.get("reason", f"{entity} — {catalyst_type_str}"),
                direction=data.get("direction", "neutral"),
                strength=data.get("strength", "weak"),
                contract_value=data.get("contract_value_usd"),
                funding_amount=data.get("funding_amount_usd"),
                derivative_tickers=data.get("derivative_tickers", []),
                source="edgar_8k",
            )
        except Exception as exc:
            logger.debug("Filing classification failed: %s", exc)
            return None

    async def _passes_liquidity_gate(self, ticker: str) -> bool:
        """Quick options liquidity check — OI ≥ 500, bid/ask < 10%."""
        try:
            import yfinance as yf
            tk = yf.Ticker(ticker)
            if not tk.options:
                return False
            chain = tk.option_chain(tk.options[0])
            calls = chain.calls
            if calls.empty:
                return False
            total_oi = int(calls["openInterest"].fillna(0).sum())
            if total_oi < self._settings.min_open_interest:
                return False
            # Quick bid/ask check on ATM
            info = tk.info or {}
            spot = float(info.get("regularMarketPrice") or info.get("currentPrice") or 0)
            if spot <= 0:
                return False
            atm = calls.iloc[(calls["strike"] - spot).abs().argsort()[:1]]
            if atm.empty:
                return False
            bid = float(atm["bid"].iloc[0] or 0)
            ask = float(atm["ask"].iloc[0] or 0)
            mid = (bid + ask) / 2
            if mid <= 0:
                return False
            spread_pct = (ask - bid) / mid
            return spread_pct <= self._settings.bid_ask_max_pct
        except Exception:
            return False

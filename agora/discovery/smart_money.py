"""
SmartMoneyAgent — EDGAR 13D/13G/Form 4 cluster detection.

Monitors for institutional accumulation signals:
  - Schedule 13D: activist stake ≥ 5% (bullish catalyst — activist pressure)
  - Schedule 13G: passive stake ≥ 5% (moderate — institutional confidence)
  - Form 4: insider buy cluster (≥ 3 insiders buying within 10 days)

Filing-to-signal target: < 3 minutes.

Claude API features used:
  - Tool use loop: Claude calls read_filing_section() tool to fetch
    specific sections of the narrative (Item 4 "Purpose of Transaction")
    rather than brute-force feeding the whole SGML document.
  - Prompt caching: stable system + tool schema cached across calls.
  - Haiku for Form 4 batch triage (fast/cheap), Opus for 13D narrative intent.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import anthropic
import httpx

from ..core.config import AgoraSettings, get_settings
from ..ops.llm_cost_log import log_call as _log_llm, log_message as _log_msg
from ..core.models import Catalyst, CatalystType

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# ── Claude tool definitions ─────────────────────────────────────────────────

_TOOLS = [
    {
        "name": "read_filing_section",
        "description": (
            "Fetch a specific section of an SEC filing from EDGAR. "
            "Use this to read Item 4 (Purpose of Transaction) from 13D filings, "
            "or the transaction table from Form 4 filings. "
            "Returns the text of that section."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "accession_number": {
                    "type": "string",
                    "description": "EDGAR accession number, e.g. 0001234567-24-012345",
                },
                "section": {
                    "type": "string",
                    "enum": ["item4_purpose", "transaction_table", "full_text"],
                    "description": "Which section to retrieve",
                },
            },
            "required": ["accession_number", "section"],
        },
    }
]

_SYSTEM_13D = """\
You are a regulatory filing analyst specializing in activist investing signals.
You receive metadata about a Schedule 13D or 13G filing.

Use the read_filing_section tool to fetch Item 4 (Purpose of Transaction) before answering.

After reading the section, output JSON:
{
  "intent":      "activist" | "passive_accumulation" | "routine" | "sell_down",
  "stake_pct":   <float>,         // percentage of shares owned
  "dollar_value": <float | null>, // USD value of position
  "demands":     ["demand1", ...], // board seats, buyback, sale process, etc.
  "timeline":    "short" | "medium" | "long" | "unknown",
  "bullish_for_stock": true | false,
  "conviction":  "high" | "medium" | "low",
  "summary":     "<1 sentence>"
}
Output JSON only.
"""

_SYSTEM_FORM4 = """\
You are a regulatory filing analyst detecting insider trading clusters.
You receive a batch of recent Form 4 filings for one company.

Analyze transaction types, sizes, and timing to classify:
{
  "cluster_type": "buy_cluster" | "sell_cluster" | "mixed" | "routine_grant",
  "insider_count": <int>,
  "total_shares":  <int>,
  "total_value":   <float>,
  "is_open_market": true | false,   // false = option exercises, grants
  "signal_strength": "strong" | "moderate" | "weak",
  "summary": "<1 sentence>"
}

buy_cluster = ≥3 distinct insiders making open-market purchases within 10 days.
strong = ≥5 insiders OR total value > $1M.
Output JSON only.
"""

_CACHED_TOOLS_SYSTEM_13D = [
    {"type": "text", "text": _SYSTEM_13D, "cache_control": {"type": "ephemeral"}}
]

_CACHED_TOOLS_SYSTEM_FORM4 = [
    {"type": "text", "text": _SYSTEM_FORM4, "cache_control": {"type": "ephemeral"}}
]


class SmartMoneyAgent:
    """
    Real-time EDGAR watcher for activist and insider cluster signals.
    Fires Catalyst events for high-conviction smart money entries.
    """

    def __init__(
        self,
        settings: AgoraSettings | None = None,
        on_catalyst: Any = None,   # async callback(catalyst: Catalyst)
    ) -> None:
        self._settings = settings or get_settings()
        self._on_catalyst = on_catalyst
        self._client = anthropic.AsyncAnthropic(api_key=self._settings.anthropic_api_key)
        self._seen_hashes: set[str] = set()
        # Rolling window: ticker → list of Form 4 filings (date, name, shares, value)
        self._form4_buffer: dict[str, list[dict]] = {}
        self._running = False
        self._csuite_manager: Any = None   # CIOAgent — set via register_csuite_manager()
        self._recent_signals: list[dict] = []   # rolling buffer for CIO reporting

    def register_csuite_manager(self, manager: Any) -> None:
        """Wire the CIOAgent as supervising executive."""
        self._csuite_manager = manager

    def get_recent_count(self) -> int:
        """Return number of smart money signals fired this session."""
        return len(self._recent_signals)

    def get_recent_signals(self) -> list[dict]:
        """Return last 10 signal summaries for CIO briefing."""
        return self._recent_signals[-10:]

    async def start(self) -> None:
        self._running = True
        logger.info("SmartMoneyAgent started — polling every %ds", self._settings.edgar_poll_seconds)
        while self._running:
            try:
                await self._poll_cycle()
            except Exception as exc:
                logger.error("SmartMoney poll cycle error: %s", exc)
            await asyncio.sleep(self._settings.edgar_poll_seconds)

    async def stop(self) -> None:
        self._running = False

    # ── Poll cycle ─────────────────────────────────────────────────

    async def _poll_cycle(self) -> None:
        now_et = datetime.now(tz=ET)
        if not (7 <= now_et.hour < 20):
            return

        today_str = now_et.date().isoformat()
        await asyncio.gather(
            self._poll_13d_13g(today_str),
            self._poll_form4(today_str),
            return_exceptions=True,
        )

    # ── 13D / 13G ──────────────────────────────────────────────────

    async def _poll_13d_13g(self, date_str: str) -> None:
        url = (
            "https://efts.sec.gov/LATEST/search-index"
            f"?q=%22%22&forms=SC+13D,SC+13G&dateRange=custom&startdt={date_str}"
            "&hits.hits._source=entity_name,file_date,form_type,file_num,period_of_report"
        )
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    url,
                    headers={"User-Agent": "AGORA/1.0 trading@agora.local"},
                )
                hits = resp.json().get("hits", {}).get("hits", [])
        except Exception as exc:
            logger.debug("13D/13G fetch failed: %s", exc)
            return

        for hit in hits:
            try:
                await self._process_13d_filing(hit)
            except Exception as exc:
                logger.debug("13D process error: %s", exc)

    async def _process_13d_filing(self, hit: dict) -> None:
        source = hit.get("_source", {})
        entity = source.get("entity_name", "Unknown")
        file_num = source.get("file_num", "")
        form_type = source.get("form_type", "")
        accession = hit.get("_id", "")

        h = hashlib.md5(file_num.encode()).hexdigest()
        if h in self._seen_hashes:
            return
        self._seen_hashes.add(h)

        logger.debug("Processing %s filing for %s", form_type, entity)

        # Run Claude tool-use loop to read filing and classify intent
        analysis = await self._analyze_13d_with_tools(entity, accession, form_type)
        if not analysis:
            return

        if not analysis.get("bullish_for_stock"):
            return
        if analysis.get("conviction") == "low":
            return

        stake_pct = analysis.get("stake_pct", 0.0)
        if stake_pct < 5.0:   # only care about meaningful positions
            return

        catalyst_type = (
            CatalystType.ACTIVIST_13D if "13D" in form_type
            else CatalystType.INSIDER_CLUSTER
        )
        strength = "strong" if analysis.get("conviction") == "high" else "moderate"

        catalyst = Catalyst(
            ticker=self._entity_to_ticker(entity),
            catalyst_type=catalyst_type,
            filing_time=datetime.now(tz=timezone.utc),
            headline=analysis.get("summary", f"{form_type} filing for {entity}"),
            direction="bullish",
            strength=strength,
            source=f"edgar_{form_type.lower().replace(' ', '_')}",
        )

        logger.info(
            "SMART MONEY: %s | %s | stake=%.1f%% | %s",
            catalyst.ticker, form_type, stake_pct, analysis.get("intent"),
        )

        self._recent_signals.append({
            "ticker": catalyst.ticker,
            "type": form_type,
            "strength": strength,
            "intent": analysis.get("intent"),
            "ts": datetime.now(tz=ET).isoformat(),
        })
        if len(self._recent_signals) > 50:
            self._recent_signals = self._recent_signals[-50:]

        if self._on_catalyst:
            await self._on_catalyst(catalyst)

        if strength == "strong" and self._csuite_manager:
            await self._csuite_manager.receive_alert(
                "SmartMoney", "info",
                f"Smart money signal: {form_type} on {catalyst.ticker} "
                f"stake={stake_pct:.1f}% intent={analysis.get('intent')}",
            )

    async def _analyze_13d_with_tools(
        self, entity: str, accession: str, form_type: str
    ) -> dict | None:
        """
        Claude tool-use loop: Claude reads Item 4 via read_filing_section tool,
        then outputs structured JSON analysis.
        """
        messages: list[dict] = [
            {
                "role": "user",
                "content": (
                    f"Analyze this {form_type} filing.\n"
                    f"Company: {entity}\n"
                    f"Accession: {accession}\n\n"
                    f"Use read_filing_section to read Item 4 before classifying intent."
                ),
            }
        ]

        # Tool use loop — max 3 rounds
        for _round in range(3):
            try:
                response = await self._client.messages.create(
                    model=self._settings.claude_model,
                    max_tokens=1024,
                    system=_CACHED_TOOLS_SYSTEM_13D,
                    tools=_TOOLS,
                    messages=messages,
                )
            except Exception as exc:
                logger.debug("Claude 13D tool call failed: %s", exc)
                return None

            if hasattr(response, "usage"):
                _log_msg(str(self._settings.db_path), "SmartMoney13D", self._settings.claude_model,
                         response.usage, purpose="13d_tool_call")
            messages.append({"role": "assistant", "content": response.content})

            # Check if Claude wants to use a tool
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                # No more tool calls — extract JSON from last text block
                text_blocks = [b for b in response.content if b.type == "text"]
                if text_blocks:
                    try:
                        raw = text_blocks[-1].text.strip()
                        if raw.startswith("```"):
                            raw = raw.split("```")[1].lstrip("json").strip()
                        return json.loads(raw)
                    except json.JSONDecodeError:
                        return None
                break

            # Execute each tool call and append results
            tool_results = []
            for tool_use in tool_uses:
                result_text = await self._execute_tool(
                    tool_use.name, tool_use.input
                )
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": result_text,
                })
            messages.append({"role": "user", "content": tool_results})

            if response.stop_reason == "end_turn":
                break

        return None

    async def _execute_tool(self, name: str, inputs: dict) -> str:
        """Execute tool calls requested by Claude."""
        if name == "read_filing_section":
            return await self._fetch_filing_section(
                inputs.get("accession_number", ""),
                inputs.get("section", "full_text"),
            )
        return "Tool not found."

    async def _fetch_filing_section(self, accession: str, section: str) -> str:
        """Fetch the requested section from EDGAR."""
        if not accession:
            return "No accession number provided."

        # Search EDGAR for this accession
        search_url = (
            "https://efts.sec.gov/LATEST/search-index"
            f"?q=%22{accession}%22"
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
                    highlight = hits[0].get("highlight", {})
                    texts: list[str] = []
                    for v in highlight.values():
                        if isinstance(v, list):
                            texts.extend(v)
                    if texts:
                        return " ".join(texts)[:3000]
        except Exception as exc:
            logger.debug("Filing section fetch failed: %s", exc)

        return f"Could not retrieve {section} for {accession}. EDGAR may require direct document access."

    # ── Form 4 (insider transactions) ──────────────────────────────

    async def _poll_form4(self, date_str: str) -> None:
        url = (
            "https://efts.sec.gov/LATEST/search-index"
            f"?q=%22%22&forms=4&dateRange=custom&startdt={date_str}"
            "&hits.hits._source=entity_name,file_date,form_type,file_num,period_of_report"
        )
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    url,
                    headers={"User-Agent": "AGORA/1.0 trading@agora.local"},
                )
                hits = resp.json().get("hits", {}).get("hits", [])
        except Exception as exc:
            logger.debug("Form 4 fetch failed: %s", exc)
            return

        for hit in hits:
            try:
                await self._buffer_form4(hit)
            except Exception as exc:
                logger.debug("Form 4 buffer error: %s", exc)

        # Check all buffered tickers for clusters
        await self._check_form4_clusters()

    async def _buffer_form4(self, hit: dict) -> None:
        source = hit.get("_source", {})
        entity = source.get("entity_name", "Unknown")
        file_num = source.get("file_num", "")
        file_date_str = source.get("file_date", date.today().isoformat())

        h = hashlib.md5(file_num.encode()).hexdigest()
        if h in self._seen_hashes:
            return
        self._seen_hashes.add(h)

        ticker = self._entity_to_ticker(entity)

        if ticker not in self._form4_buffer:
            self._form4_buffer[ticker] = []

        self._form4_buffer[ticker].append({
            "entity": entity,
            "file_num": file_num,
            "file_date": file_date_str,
            "accession": hit.get("_id", ""),
        })

        # Trim buffer to last 10 days per ticker
        cutoff = (date.today() - timedelta(days=10)).isoformat()
        self._form4_buffer[ticker] = [
            f for f in self._form4_buffer[ticker]
            if f["file_date"] >= cutoff
        ]

    async def _check_form4_clusters(self) -> None:
        """For each ticker with ≥3 Form 4s in 10 days, run Claude cluster analysis."""
        for ticker, filings in list(self._form4_buffer.items()):
            if len(filings) < 3:
                continue

            # Don't re-fire if we already processed this cluster today
            cluster_key = f"cluster_{ticker}_{date.today().isoformat()}"
            h = hashlib.md5(cluster_key.encode()).hexdigest()
            if h in self._seen_hashes:
                continue
            self._seen_hashes.add(h)

            analysis = await self._analyze_form4_cluster(ticker, filings)
            if not analysis:
                continue

            if analysis.get("cluster_type") != "buy_cluster":
                continue
            if not analysis.get("is_open_market"):
                continue
            if analysis.get("signal_strength") == "weak":
                continue

            catalyst = Catalyst(
                ticker=ticker,
                catalyst_type=CatalystType.INSIDER_CLUSTER,
                filing_time=datetime.now(tz=timezone.utc),
                headline=analysis.get("summary", f"Insider buy cluster: {ticker}"),
                direction="bullish",
                strength=analysis.get("signal_strength", "moderate"),
                source="edgar_form4",
            )

            strength = analysis.get("signal_strength", "moderate")
            logger.info(
                "INSIDER CLUSTER: %s | insiders=%d | strength=%s",
                ticker, analysis.get("insider_count", len(filings)), strength,
            )

            self._recent_signals.append({
                "ticker": ticker,
                "type": "form4_cluster",
                "strength": strength,
                "insider_count": analysis.get("insider_count", len(filings)),
                "ts": datetime.now(tz=ET).isoformat(),
            })
            if len(self._recent_signals) > 50:
                self._recent_signals = self._recent_signals[-50:]

            if self._on_catalyst:
                await self._on_catalyst(catalyst)

            if strength in ("strong", "moderate") and self._csuite_manager:
                await self._csuite_manager.receive_alert(
                    "SmartMoney", "info",
                    f"Insider buy cluster: {ticker} | "
                    f"insiders={analysis.get('insider_count', len(filings))} strength={strength}",
                )

    async def _analyze_form4_cluster(
        self, ticker: str, filings: list[dict]
    ) -> dict | None:
        """Haiku-based cluster analysis — fast and cheap for triage."""
        filing_summary = "\n".join(
            f"- {f['entity']} filed {f['file_date']} (acc: {f['accession']})"
            for f in filings
        )
        user_msg = (
            f"Ticker: {ticker}\n"
            f"Recent Form 4 filings ({len(filings)} in last 10 days):\n"
            f"{filing_summary}\n\n"
            "Classify this as a buy cluster or not."
        )
        try:
            response = await self._client.messages.create(
                model=self._settings.claude_fast_model,
                max_tokens=512,
                system=_CACHED_TOOLS_SYSTEM_FORM4,
                messages=[{"role": "user", "content": user_msg}],
            )
            if hasattr(response, "usage"):
                _log_msg(str(self._settings.db_path), "SmartMoneyForm4", self._settings.claude_fast_model,
                         response.usage, purpose="form4_classify")
            raw = response.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1].lstrip("json").strip()
            return json.loads(raw)
        except Exception as exc:
            logger.debug("Form 4 cluster analysis failed for %s: %s", ticker, exc)
            return None

    # ── Utility ────────────────────────────────────────────────────

    def _entity_to_ticker(self, entity: str) -> str:
        """
        Best-effort ticker from entity name.
        Real resolution happens at the liquidity gate (yfinance lookup).
        """
        import re
        clean = re.sub(
            r"\b(Inc|Corp|Ltd|LLC|LP|Co|Group|Holdings|Technologies|Systems|International|Bancorp|Financial)\b\.?",
            "", entity, flags=re.IGNORECASE,
        ).strip()
        return clean[:5].upper().replace(" ", "").replace(".", "")

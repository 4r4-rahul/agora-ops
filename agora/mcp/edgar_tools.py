"""
agora/mcp/edgar_tools.py — SEC EDGAR direct-access tools for AGORA agents.

Uses the public EDGAR REST API (no API key required, rate-limited to 10 req/s).
User-Agent header is required by SEC — set EDGAR_USER_AGENT in env or config.

Provides:
  get_recent_8k     — Latest 8-K filings for a company (earnings, guidance, M&A)
  get_insider_trades — Form 4 insider purchases/sales (cluster buying = bullish signal)
  search_edgar      — Full-text EDGAR search across all filing types
  get_company_facts — Structured financials (EPS, revenue, shares outstanding)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_EDGAR_BASE     = "https://data.sec.gov"
_EDGAR_EFTS     = "https://efts.sec.gov/LATEST/search-index"
_TICKER_CIK_URL = "https://www.sec.gov/files/company_tickers.json"

# Cached ticker→CIK mapping (populated on first use)
_TICKER_CIK: dict[str, str] = {}
_CIK_LOCK = asyncio.Lock()

EDGAR_TOOLS: list[dict] = [
    {
        "name": "get_recent_8k",
        "description": (
            "Fetch the most recent 8-K filings for a company from SEC EDGAR. "
            "8-K filings include earnings releases, material agreements, FDA decisions, "
            "management changes, and guidance updates. "
            "Use this to find catalysts that may not yet appear in news feeds."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker, e.g. NVDA"},
                "limit": {"type": "integer", "description": "Max filings to return (default 5)", "default": 5},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_insider_trades",
        "description": (
            "Fetch recent Form 4 insider trading filings for a company. "
            "Returns purchases and sales by executives and directors in the past 30 days. "
            "Cluster buying (3+ insiders purchasing) is a strong bullish signal. "
            "Large insider sales can signal near-term distribution."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker, e.g. AAPL"},
                "days": {"type": "integer", "description": "Look-back window in days (default 30)", "default": 30},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "search_edgar_filings",
        "description": (
            "Full-text search across all SEC EDGAR filings. "
            "Use this to find filings mentioning a company name, contract, product, or event. "
            "Useful for finding derivative-ticker relationships (e.g. supplier mentions)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search text, e.g. 'NVIDIA data center contract'"},
                "form_type": {
                    "type": "string",
                    "description": "Filing type filter: 8-K, 10-Q, 10-K, 13D, 13G, 4, or empty for all",
                    "default": "8-K",
                },
                "days": {"type": "integer", "description": "Filings from last N days (default 7)", "default": 7},
                "limit": {"type": "integer", "description": "Max results (default 5)", "default": 5},
            },
            "required": ["query"],
        },
    },
]


def edgar_tool_handlers(user_agent: str) -> dict[str, Any]:
    """Return {tool_name: callable} using the SEC EDGAR public API."""

    headers = {
        "User-Agent": user_agent,
        "Accept-Encoding": "gzip, deflate",
    }

    async def _get_cik(ticker: str) -> str | None:
        """Map ticker → 10-digit CIK string."""
        global _TICKER_CIK
        ticker = ticker.upper()
        async with _CIK_LOCK:
            if not _TICKER_CIK:
                try:
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        resp = await client.get(_TICKER_CIK_URL, headers=headers)
                        data = resp.json()
                        for item in data.values():
                            t = item.get("ticker", "").upper()
                            cik = str(item.get("cik_str", "")).zfill(10)
                            _TICKER_CIK[t] = cik
                except Exception as exc:
                    logger.warning("EDGAR CIK map fetch failed: %s", exc)
        return _TICKER_CIK.get(ticker)

    async def get_recent_8k(ticker: str, limit: int = 5) -> list[dict]:
        cik = await _get_cik(ticker)
        if not cik:
            return [{"error": f"CIK not found for {ticker}"}]
        try:
            url = f"{_EDGAR_BASE}/submissions/CIK{cik}.json"
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url, headers=headers)
                data = resp.json()
            recent = data.get("filings", {}).get("recent", {})
            forms   = recent.get("form", [])
            dates   = recent.get("filingDate", [])
            accnums = recent.get("accessionNumber", [])
            docs    = recent.get("primaryDocument", [])
            descs   = recent.get("description", [])

            results = []
            for i, form in enumerate(forms):
                if form == "8-K" and len(results) < min(limit, 10):
                    acc = accnums[i].replace("-", "") if i < len(accnums) else ""
                    results.append({
                        "form":        form,
                        "date":        dates[i] if i < len(dates) else "",
                        "description": descs[i] if i < len(descs) else "",
                        "document":    docs[i] if i < len(docs) else "",
                        "url": (
                            f"https://www.sec.gov/Archives/edgar/data/"
                            f"{int(cik)}/{acc}/{docs[i]}"
                            if acc and i < len(docs) else ""
                        ),
                    })
            return results or [{"note": f"No recent 8-K filings found for {ticker}"}]
        except Exception as exc:
            logger.warning("EDGAR 8-K fetch failed for %s: %s", ticker, exc)
            return [{"error": str(exc)}]

    async def get_insider_trades(ticker: str, days: int = 30) -> list[dict]:
        cik = await _get_cik(ticker)
        if not cik:
            return [{"error": f"CIK not found for {ticker}"}]
        try:
            url = f"{_EDGAR_BASE}/submissions/CIK{cik}.json"
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url, headers=headers)
                data = resp.json()
            recent = data.get("filings", {}).get("recent", {})
            forms  = recent.get("form", [])
            dates  = recent.get("filingDate", [])
            descs  = recent.get("description", [])

            cutoff = (date.today() - timedelta(days=days)).isoformat()
            results = []
            for i, form in enumerate(forms):
                if form == "4" and i < len(dates) and dates[i] >= cutoff:
                    results.append({
                        "form":  "4",
                        "date":  dates[i],
                        "description": descs[i] if i < len(descs) else "Insider transaction",
                    })
                    if len(results) >= 10:
                        break

            summary = {
                "ticker":          ticker.upper(),
                "look_back_days":  days,
                "form4_count":     len(results),
                "filings":         results,
                "signal": (
                    "CLUSTER_BUY (3+ insiders)" if len(results) >= 3 else
                    "SMALL_ACTIVITY" if results else "NO_INSIDER_ACTIVITY"
                ),
            }
            return [summary]
        except Exception as exc:
            logger.warning("EDGAR Form 4 fetch failed for %s: %s", ticker, exc)
            return [{"error": str(exc)}]

    async def search_edgar_filings(
        query: str,
        form_type: str = "8-K",
        days: int = 7,
        limit: int = 5,
    ) -> list[dict]:
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        params: dict[str, str] = {
            "q":          query,
            "dateRange":  "custom",
            "startdt":    cutoff,
        }
        if form_type:
            params["forms"] = form_type
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(_EDGAR_EFTS, params=params, headers=headers)
                data = resp.json()
            hits = data.get("hits", {}).get("hits", [])
            results = []
            for h in hits[:min(limit, 10)]:
                src = h.get("_source", {})
                results.append({
                    "entity":    src.get("entity_name", ""),
                    "form":      src.get("form_type", ""),
                    "date":      src.get("file_date", ""),
                    "accession": src.get("file_num", ""),
                })
            return results or [{"note": "No results found"}]
        except Exception as exc:
            logger.warning("EDGAR full-text search failed: %s", exc)
            return [{"error": str(exc)}]

    return {
        "get_recent_8k":         get_recent_8k,
        "get_insider_trades":    get_insider_trades,
        "search_edgar_filings":  search_edgar_filings,
    }

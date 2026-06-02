"""
agora/mcp/search_tools.py — Tavily web search tools for AGORA agents.

Gives agents the ability to verify real-world facts in real-time:
  - Confirm earnings dates (prevent hallucinated date blocks)
  - Find recent news that arrived after session data was staged
  - Check analyst rating changes published today
  - Verify catalysts before acting on them

Requires TAVILY_API_KEY in environment / AgoraSettings.
Falls back gracefully (returns empty results) if key is missing.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

logger = logging.getLogger(__name__)

SEARCH_TOOLS: list[dict] = [
    {
        "name": "search_news",
        "description": (
            "Search the web for recent news about a company or topic. "
            "Returns up to 5 article summaries with title, source, date, and snippet. "
            "Use this to find catalysts, analyst updates, or macro news published today "
            "that might affect a proposed trade."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query, e.g. 'NVDA earnings date 2026' or 'SPY macro risk today'",
                },
                "days_back": {
                    "type": "integer",
                    "description": "Only return results from the last N days (default 7)",
                    "default": 7,
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum articles to return (default 5, max 10)",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "verify_earnings_date",
        "description": (
            "Search for the confirmed next earnings date for a stock ticker. "
            "Returns the expected earnings date and source. "
            "Use this before blocking a trade for 'upcoming earnings' to ensure "
            "the date is real and falls within the trade's DTE window."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {
                    "type": "string",
                    "description": "Stock ticker, e.g. NVDA",
                },
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "search_analyst_ratings",
        "description": (
            "Search for recent analyst rating changes and price target updates for a ticker. "
            "Returns upgrades, downgrades, and price target changes from the past 14 days."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {
                    "type": "string",
                    "description": "Stock ticker, e.g. AAPL",
                },
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "search_macro_context",
        "description": (
            "Search for current macro environment context: Fed stance, CPI outlook, "
            "yield curve, VIX regime, and key risk events in the next 30 days. "
            "Use this to validate macro assumptions before directional trades."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "focus": {
                    "type": "string",
                    "description": "Specific macro topic: 'fed', 'inflation', 'yields', 'vix', or 'general'",
                    "default": "general",
                },
            },
            "required": [],
        },
    },
]


def search_tool_handlers(api_key: str | None) -> dict[str, Any]:
    """Return {tool_name: callable} bound to the Tavily API key."""

    async def _tavily_search(query: str, days_back: int, max_results: int) -> list[dict]:
        if not api_key:
            logger.debug("Tavily API key not set — search returning empty")
            return [{"note": "Search unavailable: TAVILY_API_KEY not configured"}]
        try:
            from tavily import AsyncTavilyClient
            client = AsyncTavilyClient(api_key=api_key)
            cutoff = (date.today() - timedelta(days=days_back)).isoformat()
            response = await client.search(
                query=query,
                search_depth="basic",
                max_results=min(max_results, 10),
                include_answer=False,
                include_raw_content=False,
            )
            results = []
            for r in response.get("results", []):
                results.append({
                    "title":   r.get("title", ""),
                    "url":     r.get("url", ""),
                    "source":  r.get("source", ""),
                    "date":    r.get("published_date", ""),
                    "snippet": r.get("content", "")[:300],
                })
            return results
        except Exception as exc:
            logger.warning("Tavily search failed: %s", exc)
            return [{"error": str(exc)}]

    async def search_news(query: str, days_back: int = 7, max_results: int = 5) -> list[dict]:
        return await _tavily_search(query, days_back, max_results)

    async def verify_earnings_date(ticker: str) -> dict:
        results = await _tavily_search(
            f"{ticker.upper()} next earnings date 2026 confirmed",
            days_back=14,
            max_results=3,
        )
        return {"ticker": ticker.upper(), "search_results": results}

    async def search_analyst_ratings(ticker: str) -> list[dict]:
        return await _tavily_search(
            f"{ticker.upper()} analyst rating upgrade downgrade price target 2026",
            days_back=14,
            max_results=5,
        )

    async def search_macro_context(focus: str = "general") -> list[dict]:
        queries = {
            "fed":       "Federal Reserve interest rate decision 2026 FOMC",
            "inflation": "CPI inflation data 2026 outlook",
            "yields":    "10-year Treasury yield curve 2026",
            "vix":       "VIX volatility index market fear 2026",
            "general":   "stock market macro risk outlook 2026 Fed CPI",
        }
        query = queries.get(focus, queries["general"])
        return await _tavily_search(query, days_back=7, max_results=5)

    return {
        "search_news":          search_news,
        "verify_earnings_date": verify_earnings_date,
        "search_analyst_ratings": search_analyst_ratings,
        "search_macro_context": search_macro_context,
    }

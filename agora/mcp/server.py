"""
agora/mcp/server.py — FastMCP server exposing all AGORA tools.

Run standalone so Claude Code, Claude Desktop, or other MCP clients can
query AGORA's trading intelligence directly:

    python -m agora.mcp.server

Or via agora ops:
    uv run agora/mcp/server.py

Tools exposed:
  SQLite:  query_ticker_history, query_strategy_performance,
           query_advocate_history, query_approved_lessons,
           query_pillar_performance, query_recent_outcomes
  Search:  search_news, verify_earnings_date,
           search_analyst_ratings, search_macro_context
  EDGAR:   get_recent_8k, get_insider_trades, search_edgar_filings
  Market:  get_current_snapshot, get_options_liquidity,
           get_vol_term_structure, get_vix_and_regime
"""

from __future__ import annotations

import os
from pathlib import Path

from fastmcp import FastMCP

from .sqlite_tools import sqlite_tool_handlers
from .search_tools import search_tool_handlers
from .edgar_tools import edgar_tool_handlers
from .market_tools import market_tool_handlers

# ── Resolve config ────────────────────────────────────────────────────────────
_DB_PATH     = os.environ.get("AGORA_DB_PATH", ".agora/agora.db")
_TAVILY_KEY  = os.environ.get("TAVILY_API_KEY")
_EDGAR_AGENT = os.environ.get(
    "EDGAR_USER_AGENT",
    "AGORA Trading System rahulvari2021@gmail.com",
)

# ── Build handler maps ────────────────────────────────────────────────────────
_sqlite  = sqlite_tool_handlers(_DB_PATH)
_search  = search_tool_handlers(_TAVILY_KEY)
_edgar   = edgar_tool_handlers(_EDGAR_AGENT)
_market  = market_tool_handlers()

# ── FastMCP server ────────────────────────────────────────────────────────────
mcp = FastMCP(
    name="AGORA Trading Intelligence",
    instructions=(
        "You have access to AGORA's live trading system tools: "
        "trade journal queries, SEC EDGAR filings, web search, and real-time market data. "
        "Use these tools to verify claims, find catalysts, and analyze trading history."
    ),
)


# ── SQLite tools ──────────────────────────────────────────────────────────────

@mcp.tool()
def query_ticker_history(ticker: str, limit: int = 10) -> list[dict]:
    """Query analyst decision history for a ticker (last N decisions)."""
    return _sqlite["query_ticker_history"](ticker=ticker, limit=limit)


@mcp.tool()
def query_strategy_performance(strategy_type: str, days: int = 30) -> dict:
    """Query closed-trade win rate and outcomes for a strategy type."""
    return _sqlite["query_strategy_performance"](strategy_type=strategy_type, days=days)


@mcp.tool()
def query_advocate_history(ticker: str = "", days: int = 30) -> dict:
    """Query AdvocateAgent verdict history and top failure modes."""
    return _sqlite["query_advocate_history"](ticker=ticker, days=days)


@mcp.tool()
def query_approved_lessons(agent_type: str, limit: int = 20) -> list[dict]:
    """Retrieve approved lessons for an agent type (analyst/advocate/strategy/swing/exit)."""
    return _sqlite["query_approved_lessons"](agent_type=agent_type, limit=limit)


@mcp.tool()
def query_pillar_performance(days: int = 60) -> list[dict]:
    """Query rolling performance metrics per trading pillar."""
    return _sqlite["query_pillar_performance"](days=days)


@mcp.tool()
def query_recent_outcomes(limit: int = 20) -> list[dict]:
    """Query the most recent closed position outcomes with P&L."""
    return _sqlite["query_recent_outcomes"](limit=limit)


# ── Search tools ──────────────────────────────────────────────────────────────

@mcp.tool()
async def search_news(query: str, days_back: int = 7, max_results: int = 5) -> list[dict]:
    """Search the web for recent news about a company or macro topic."""
    return await _search["search_news"](query=query, days_back=days_back, max_results=max_results)


@mcp.tool()
async def verify_earnings_date(ticker: str) -> dict:
    """Search for the confirmed next earnings date for a stock ticker."""
    return await _search["verify_earnings_date"](ticker=ticker)


@mcp.tool()
async def search_analyst_ratings(ticker: str) -> list[dict]:
    """Search for recent analyst rating changes and price target updates."""
    return await _search["search_analyst_ratings"](ticker=ticker)


@mcp.tool()
async def search_macro_context(focus: str = "general") -> list[dict]:
    """Search for current macro context: fed/inflation/yields/vix/general."""
    return await _search["search_macro_context"](focus=focus)


# ── EDGAR tools ───────────────────────────────────────────────────────────────

@mcp.tool()
async def get_recent_8k(ticker: str, limit: int = 5) -> list[dict]:
    """Fetch the most recent 8-K filings for a company from SEC EDGAR."""
    return await _edgar["get_recent_8k"](ticker=ticker, limit=limit)


@mcp.tool()
async def get_insider_trades(ticker: str, days: int = 30) -> list[dict]:
    """Fetch recent Form 4 insider trading filings for a company."""
    return await _edgar["get_insider_trades"](ticker=ticker, days=days)


@mcp.tool()
async def search_edgar_filings(
    query: str, form_type: str = "8-K", days: int = 7, limit: int = 5
) -> list[dict]:
    """Full-text search across all SEC EDGAR filings."""
    return await _edgar["search_edgar_filings"](
        query=query, form_type=form_type, days=days, limit=limit
    )


# ── Market tools ──────────────────────────────────────────────────────────────

@mcp.tool()
async def get_current_snapshot(ticker: str) -> dict:
    """Get current price, RSI-14, SMA50, IV rank, and HV30 for a ticker."""
    return await _market["get_current_snapshot"](ticker=ticker)


@mcp.tool()
async def get_options_liquidity(
    ticker: str, expiry: str, strike: float, option_type: str
) -> dict:
    """Check current bid/ask/OI for a specific option strike."""
    return await _market["get_options_liquidity"](
        ticker=ticker, expiry=expiry, strike=strike, option_type=option_type
    )


@mcp.tool()
async def get_vol_term_structure(ticker: str) -> list[dict]:
    """Get the IV term structure across the nearest 4 expiries."""
    return await _market["get_vol_term_structure"](ticker=ticker)


@mcp.tool()
async def get_vix_and_regime() -> dict:
    """Get current VIX, 20-day average, and inferred vol regime."""
    return await _market["get_vix_and_regime"]()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()

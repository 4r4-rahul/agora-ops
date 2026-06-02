"""
agora/mcp/ — Model Context Protocol tools for AGORA agents.

Provides four categories of tools that agents can call during reasoning:

  sqlite_tools  — Query trade journals, decision chains, lessons, outcomes
  search_tools  — Tavily web search (news, earnings dates, analyst ratings)
  edgar_tools   — SEC EDGAR filings (8-K, Form 4 insider trades)
  market_tools  — Real-time IV rank, options liquidity, price/technicals
  tool_runner   — Agentic loop helper (tool_use / tool_result cycle)
  server        — FastMCP server exposing all tools (run standalone)
"""

from .sqlite_tools import SQLITE_TOOLS, sqlite_tool_handlers
from .search_tools import SEARCH_TOOLS, search_tool_handlers
from .edgar_tools import EDGAR_TOOLS, edgar_tool_handlers
from .market_tools import MARKET_TOOLS, market_tool_handlers
from .tool_runner import run_with_tools

__all__ = [
    "SQLITE_TOOLS", "sqlite_tool_handlers",
    "SEARCH_TOOLS", "search_tool_handlers",
    "EDGAR_TOOLS", "edgar_tool_handlers",
    "MARKET_TOOLS", "market_tool_handlers",
    "run_with_tools",
]

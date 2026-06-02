"""agora/mcp/memory_tools.py — Semantic memory MCP tools for SwingJudge."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..memory.semantic_store import SemanticTradeStore

logger = logging.getLogger(__name__)

# ── Tool definitions (Anthropic tool_use schema) ──────────────────────────────

MEMORY_TOOLS: list[dict] = [
    {
        "name": "find_similar_trades",
        "description": (
            "Search the semantic trade memory for past swing trades that are similar to "
            "the current setup. Returns up to k closed trades (wins or losses) ranked by "
            "similarity. Use this to understand how comparable setups have performed "
            "historically — are they winners or losers? Does confidence align with outcome? "
            "Prefer completed trades with concrete P&L over open positions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {
                    "type": "string",
                    "description": "Stock ticker symbol (e.g. AAPL)",
                },
                "direction": {
                    "type": "string",
                    "description": "Trade direction: 'bullish' or 'bearish'",
                    "enum": ["bullish", "bearish"],
                },
                "technical_score": {
                    "type": "number",
                    "description": "Technical factor score (0-35 range)",
                },
                "catalyst_score": {
                    "type": "number",
                    "description": "Catalyst factor score (0-25 range)",
                },
                "fundamental_score": {
                    "type": "number",
                    "description": "Fundamental factor score (0-20 range)",
                },
                "options_score": {
                    "type": "number",
                    "description": "Options setup factor score (0-20 range)",
                },
                "k": {
                    "type": "integer",
                    "description": "Number of similar trades to return (default 5)",
                    "default": 5,
                },
            },
            "required": [
                "ticker",
                "direction",
                "technical_score",
                "catalyst_score",
                "fundamental_score",
                "options_score",
            ],
        },
    },
    {
        "name": "get_trade_memory_stats",
        "description": (
            "Return aggregate statistics for the semantic trade memory: total trades indexed, "
            "win rate, average P&L%, and counts of open vs completed trades. "
            "Use this to calibrate how much weight to place on historical similarity results."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]


# ── Handler factory ────────────────────────────────────────────────────────────

def memory_tool_handlers(store: "SemanticTradeStore") -> dict[str, Any]:
    """
    Returns a dict mapping tool name -> callable handler.
    Each handler accepts keyword arguments matching the tool's input_schema.
    """

    def find_similar_trades(
        ticker: str,
        direction: str,
        technical_score: float,
        catalyst_score: float,
        fundamental_score: float,
        options_score: float,
        k: int = 5,
        **_: Any,
    ) -> list[dict]:
        try:
            factor_breakdown = {
                "technical": float(technical_score),
                "catalyst": float(catalyst_score),
                "fundamental": float(fundamental_score),
                "options_setup": float(options_score),
                "direction": direction,
            }
            return store.find_similar(
                ticker=ticker,
                direction=direction,
                factor_breakdown=factor_breakdown,
                k=int(k),
            )
        except Exception as exc:
            logger.warning("find_similar_trades handler failed: %s", exc)
            return []

    def get_trade_memory_stats(**_: Any) -> dict:
        try:
            return store.get_stats()
        except Exception as exc:
            logger.warning("get_trade_memory_stats handler failed: %s", exc)
            return {"error": str(exc), "count": 0}

    return {
        "find_similar_trades": find_similar_trades,
        "get_trade_memory_stats": get_trade_memory_stats,
    }

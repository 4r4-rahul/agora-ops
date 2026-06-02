"""agora/mcp/flow_tools.py — Options flow MCP tools for AdvocateAgent and SwingJudge."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from agora.services.flow_detector import FlowSignals, SweepData, get_flow_signals

logger = logging.getLogger(__name__)

FLOW_TOOLS: list[dict] = [
    {
        "name": "get_options_flow",
        "description": (
            "Analyse unusual options activity for a single ticker. "
            "Uses Unusual Whales API (real exchange sweep detection, dark pool prints, "
            "actual premium paid) when UNUSUAL_WHALES_API_KEY is set; "
            "falls back to yfinance chain analysis otherwise. "
            "Returns: volume/OI ratio, call/put volume ratio, net premium skew, "
            "sweep list (strike, side, at_ask), dark pool print count, "
            "net premium in USD, and directional bias (bullish/bearish/neutral) with strength. "
            "Use this to confirm whether institutional money is aggressively positioned "
            "before endorsing or vetoing a trade idea. "
            "'source' field tells you which data provider was used."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {
                    "type": "string",
                    "description": "Stock ticker symbol, e.g. NVDA or SPY",
                },
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_universe_flow_scan",
        "description": (
            "Scan options flow concurrently across a list of tickers (max 10) and "
            "return the top 5 by volume/OI ratio. Uses Unusual Whales API when available "
            "(real sweeps, dark pool), yfinance otherwise. Useful for identifying which "
            "names in a watchlist have the hottest institutional options activity today. "
            "Results are sorted by volume_ratio descending. Each result includes 'source' "
            "field indicating data provider, plus dark_pool_prints and net_premium_usd "
            "when Unusual Whales data is available."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tickers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of ticker symbols to scan (1–10 tickers)",
                    "minItems": 1,
                    "maxItems": 10,
                },
            },
            "required": ["tickers"],
        },
    },
]


def _serialise_sweep(s: SweepData) -> dict:
    return {
        "strike":        s.strike,
        "option_type":   s.option_type,
        "volume":        s.volume,
        "open_interest": s.open_interest,
        "vol_oi_ratio":  s.vol_oi_ratio,
        "bid":           s.bid,
        "ask":           s.ask,
        "at_ask":        s.at_ask,
    }


def _serialise_signals(sig: FlowSignals) -> dict:
    base = {
        "ticker":              sig.ticker,
        "volume_ratio":        sig.volume_ratio,
        "call_put_vol_ratio":  sig.call_put_vol_ratio,
        "net_premium_skew":    sig.net_premium_skew,
        "sweeps":              [_serialise_sweep(s) for s in sig.sweeps],
        "unusual":             sig.unusual,
        "direction":           sig.direction,
        "strength":            sig.strength,
        "summary":             sig.summary,
        "timestamp":           sig.timestamp.isoformat(),
        "source":              "yfinance",
    }
    # Merge Unusual Whales metadata when present (richer data: real premium, dark pool)
    uw_meta: dict = getattr(sig, "_uw_meta", {})
    if uw_meta:
        base.update(uw_meta)
    return base


def flow_tool_handlers() -> dict[str, Any]:
    """Return {tool_name: async callable} for flow analysis tools.

    No external dependencies — all data sourced from yfinance.
    """

    async def get_options_flow(ticker: str) -> dict:
        try:
            sig = await get_flow_signals(ticker.upper())
            if sig is None:
                return {"error": "no flow data", "ticker": ticker.upper()}
            return _serialise_signals(sig)
        except Exception as exc:
            logger.debug("get_options_flow handler error for %s: %s", ticker, exc)
            return {"error": "no flow data", "ticker": ticker.upper()}

    async def get_universe_flow_scan(tickers: list[str]) -> list[dict]:
        # Enforce cap defensively in case schema validation is bypassed
        capped = tickers[:10]

        tasks = [get_flow_signals(t.upper()) for t in capped]
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as exc:
            logger.debug("get_universe_flow_scan gather error: %s", exc)
            return []

        signals: list[FlowSignals] = []
        for item in results:
            if isinstance(item, FlowSignals):
                signals.append(item)
            # None and exceptions are silently skipped

        # Sort by volume_ratio descending, return top 5
        signals.sort(key=lambda s: s.volume_ratio, reverse=True)
        top5 = signals[:5]

        return [_serialise_signals(s) for s in top5]

    return {
        "get_options_flow":        get_options_flow,
        "get_universe_flow_scan":  get_universe_flow_scan,
    }

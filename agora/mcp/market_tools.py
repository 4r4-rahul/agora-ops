"""
agora/mcp/market_tools.py — Real-time market data tools for AGORA agents.

Lets agents verify market data in real-time during reasoning instead of
relying on potentially stale pre-staged snapshots.

Provides:
  get_current_snapshot  — Price, RSI, SMA50, IV rank, HV30 for a ticker
  get_options_liquidity — Bid/ask, OI for a specific strike+expiry
  get_vol_term_structure — IV across expirations (term structure / skew)
  get_vix_and_regime    — Current VIX and inferred vol regime
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Any

logger = logging.getLogger(__name__)

MARKET_TOOLS: list[dict] = [
    {
        "name": "get_current_snapshot",
        "description": (
            "Get the current market snapshot for a ticker: price, RSI-14, SMA50, "
            "IV rank (0-100), historical vol (30-day), and VIX. "
            "Use this to verify IV environment claims before endorsing a structure."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker, e.g. SPY"},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_options_liquidity",
        "description": (
            "Check current bid, ask, mid, and open interest for a specific option. "
            "Use this to verify that a proposed strike is actually liquid "
            "before endorsing a structure or flagging liquidity concerns."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker":     {"type": "string", "description": "Stock ticker"},
                "expiry":     {"type": "string", "description": "Expiry date YYYY-MM-DD"},
                "strike":     {"type": "number", "description": "Strike price"},
                "option_type":{"type": "string", "description": "call or put"},
            },
            "required": ["ticker", "expiry", "strike", "option_type"],
        },
    },
    {
        "name": "get_vol_term_structure",
        "description": (
            "Get the implied volatility term structure for a ticker: IV for the nearest "
            "3-4 expiries. A flat or inverted term structure (near-term IV > far-term) "
            "signals short-term fear and favors shorter DTE trades. "
            "Use this to validate DTE selection."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker"},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_vix_and_regime",
        "description": (
            "Get current VIX level, 20-day VIX average, and inferred vol regime "
            "(low_volatility / normal / high_volatility / crisis). "
            "Use this to calibrate confidence thresholds and strategy sizing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]


def market_tool_handlers() -> dict[str, Any]:
    """Return {tool_name: callable} using yfinance (already a project dependency)."""

    async def get_current_snapshot(ticker: str) -> dict:
        try:
            import yfinance as yf
            import pandas as pd
            tk = yf.Ticker(ticker.upper())
            hist = await asyncio.to_thread(lambda: tk.history(period="60d"))
            if hist.empty:
                return {"error": f"No data for {ticker}"}

            close = hist["Close"]
            price = float(close.iloc[-1])

            # RSI-14
            delta = close.diff()
            gain  = delta.clip(lower=0).rolling(14).mean()
            loss  = (-delta.clip(upper=0)).rolling(14).mean()
            rs    = gain / loss
            rsi   = float(100 - 100 / (1 + rs.iloc[-1]))

            sma50 = float(close.rolling(50).mean().iloc[-1]) if len(close) >= 50 else None
            hv30  = float(close.pct_change().rolling(30).std().iloc[-1] * (252**0.5) * 100) if len(close) >= 30 else None

            # IV rank — approximate from options chain ATM IV
            iv_rank = None
            try:
                opts = tk.options
                if opts:
                    chain = await asyncio.to_thread(lambda: tk.option_chain(opts[0]))
                    calls = chain.calls
                    atm_idx = (calls["strike"] - price).abs().idxmin()
                    iv_rank = round(float(calls.loc[atm_idx, "impliedVolatility"]) * 100, 1)
            except Exception:
                pass

            return {
                "ticker":   ticker.upper(),
                "price":    round(price, 2),
                "rsi_14":   round(rsi, 1),
                "sma_50":   round(sma50, 2) if sma50 else None,
                "hv_30pct": round(hv30, 1) if hv30 else None,
                "iv_rank_approx": iv_rank,
            }
        except Exception as exc:
            logger.warning("get_current_snapshot failed for %s: %s", ticker, exc)
            return {"error": str(exc)}

    async def get_options_liquidity(
        ticker: str, expiry: str, strike: float, option_type: str
    ) -> dict:
        try:
            import yfinance as yf
            tk = yf.Ticker(ticker.upper())
            chain = await asyncio.to_thread(lambda: tk.option_chain(expiry))
            df = chain.calls if option_type.lower() == "call" else chain.puts
            row = df[df["strike"] == strike]
            if row.empty:
                row = df.iloc[(df["strike"] - strike).abs().argsort()[:1]]
            r = row.iloc[0]
            bid = float(r.get("bid", 0) or 0)
            ask = float(r.get("ask", 0) or 0)
            mid = (bid + ask) / 2
            oi  = int(r.get("openInterest", 0) or 0)
            ba_pct = (ask - bid) / mid * 100 if mid > 0 else 0
            return {
                "ticker":   ticker.upper(),
                "expiry":   expiry,
                "strike":   float(r["strike"]),
                "type":     option_type,
                "bid":      round(bid, 2),
                "ask":      round(ask, 2),
                "mid":      round(mid, 2),
                "open_interest": oi,
                "bid_ask_pct":   round(ba_pct, 1),
                "liquid":   oi >= 500 and ba_pct <= 10.0,
            }
        except Exception as exc:
            logger.warning("get_options_liquidity failed: %s", exc)
            return {"error": str(exc)}

    async def get_vol_term_structure(ticker: str) -> list[dict]:
        try:
            import yfinance as yf
            tk = yf.Ticker(ticker.upper())
            exps = (await asyncio.to_thread(lambda: tk.options or []))[:4]
            today = date.today()
            result = []
            for exp in exps:
                try:
                    chain = await asyncio.to_thread(lambda e=exp: tk.option_chain(e))
                    calls = chain.calls
                    fi = tk.fast_info
                    spot = float(getattr(fi, "last_price", 0) or 0)
                    if spot > 0:
                        atm_idx = (calls["strike"] - spot).abs().idxmin()
                        iv = float(calls.loc[atm_idx, "impliedVolatility"])
                    else:
                        iv = float(calls["impliedVolatility"].median())
                    dte = (date.fromisoformat(exp) - today).days
                    result.append({"expiry": exp, "dte": dte, "atm_iv_pct": round(iv * 100, 1)})
                except Exception:
                    continue
            if len(result) >= 2:
                slope = result[-1]["atm_iv_pct"] - result[0]["atm_iv_pct"]
                structure = "inverted" if slope < -2 else "flat" if abs(slope) <= 2 else "normal"
                return [{"term_structure": structure, "slope_pct": round(slope, 1)}] + result
            return result
        except Exception as exc:
            logger.warning("get_vol_term_structure failed for %s: %s", ticker, exc)
            return [{"error": str(exc)}]

    async def get_vix_and_regime() -> dict:
        try:
            import yfinance as yf
            vix_tk = yf.Ticker("^VIX")
            hist = await asyncio.to_thread(lambda: vix_tk.history(period="30d"))
            if hist.empty:
                return {"error": "VIX data unavailable"}
            current_vix = float(hist["Close"].iloc[-1])
            avg_vix_20d = float(hist["Close"].rolling(20).mean().iloc[-1])
            if current_vix < 15:
                regime = "low_volatility"
            elif current_vix < 20:
                regime = "normal"
            elif current_vix < 28:
                regime = "high_volatility"
            else:
                regime = "crisis"
            return {
                "vix":         round(current_vix, 2),
                "vix_20d_avg": round(avg_vix_20d, 2),
                "regime":      regime,
                "vix_vs_avg":  round(current_vix - avg_vix_20d, 2),
            }
        except Exception as exc:
            logger.warning("get_vix_and_regime failed: %s", exc)
            return {"error": str(exc)}

    return {
        "get_current_snapshot":    get_current_snapshot,
        "get_options_liquidity":   get_options_liquidity,
        "get_vol_term_structure":  get_vol_term_structure,
        "get_vix_and_regime":      get_vix_and_regime,
    }

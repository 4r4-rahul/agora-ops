"""
agora/services/unusual_whales_client.py — Unusual Whales API client.

Replaces yfinance-based flow detection with real institutional-grade options flow:
  - Actual sweep orders identified by exchange sweep patterns (not volume/OI ratio proxies)
  - Dark pool block prints (separate category from lit-market sweeps)
  - Per-alert premium in dollars (not estimated from mid × volume)
  - UW sentiment tag (bullish / bearish / neutral) per trade

Docs: https://unusualwhales.com/api
Plan: $50/month covers API access; free tier has limited rate.

Design:
  - Zero-impact when UNUSUAL_WHALES_API_KEY is not set — returns None (caller falls back to yfinance)
  - 15-minute per-ticker cache: UW flow is meaningful over that window; also respects rate limits
  - Maps UW response → same FlowSignals dataclass used by the rest of the system
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from agora.services.flow_detector import FlowSignals, SweepData

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.unusualwhales.com"
_CACHE: dict[str, tuple[float, FlowSignals | None]] = {}
_CACHE_TTL = 900.0   # 15 minutes
_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)


def _api_key() -> str | None:
    return os.getenv("UNUSUAL_WHALES_API_KEY") or None


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(float(v)) if v is not None else default
    except (TypeError, ValueError):
        return default


async def get_flow_signals(
    ticker: str,
    account_size: float = 25_000.0,
) -> FlowSignals | None:
    """
    Get Unusual Whales flow signals for `ticker`.

    Priority:
      1. Discord alert store (UW webhook → Discord channel → listener parsed it)
      2. UW REST API (if UNUSUAL_WHALES_API_KEY is set)
      3. Returns None → caller falls back to yfinance

    Returns None if no UW data available.
    """
    ticker = ticker.upper()

    # 1. Discord alert store (no API cost, real-time)
    try:
        from agora.ops.uw_discord_listener import get_latest as _uw_discord_get
        discord_sig = _uw_discord_get(ticker)
        if discord_sig is not None:
            logger.debug("unusual_whales: Discord store hit for %s", ticker)
            return discord_sig
    except Exception:
        pass

    # 2. REST API (if key present)
    key = _api_key()
    if not key:
        return None

    now = time.monotonic()

    # Cache check
    if ticker in _CACHE:
        cached_at, cached_sig = _CACHE[ticker]
        if now - cached_at < _CACHE_TTL:
            logger.debug("unusual_whales: cache hit for %s (age=%.0fs)", ticker, now - cached_at)
            return cached_sig

    try:
        result = await _fetch_and_parse(ticker, key, account_size)
        _CACHE[ticker] = (now, result)
        return result
    except Exception as exc:
        logger.warning("unusual_whales: fetch failed for %s — %s", ticker, exc)
        _CACHE[ticker] = (now, None)
        return None


async def _fetch_and_parse(
    ticker: str,
    api_key: str,
    account_size: float,
) -> FlowSignals | None:
    """Fetch /api/stock/{ticker}/flow-recent and convert to FlowSignals."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json, text/plain",
        "User-Agent": "AGORA/1.0",
    }

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(
            f"{_BASE_URL}/api/stock/{ticker}/flow-recent",
            headers=headers,
        )

    if resp.status_code == 401:
        logger.error("unusual_whales: 401 Unauthorized — check UNUSUAL_WHALES_API_KEY")
        return None
    if resp.status_code == 429:
        logger.warning("unusual_whales: 429 rate-limited for %s", ticker)
        return None
    if resp.status_code == 404:
        logger.debug("unusual_whales: no flow data for %s (404)", ticker)
        return None

    resp.raise_for_status()
    body = resp.json()

    # UW returns {"data": [...]} or just a list
    records: list[dict] = body.get("data", body) if isinstance(body, dict) else body
    if not isinstance(records, list) or not records:
        logger.debug("unusual_whales: empty response for %s", ticker)
        return None

    return _build_flow_signals(ticker, records, account_size)


def _build_flow_signals(
    ticker: str,
    records: list[dict],
    account_size: float,
) -> FlowSignals | None:
    """Aggregate raw UW flow records into a FlowSignals."""
    total_call_vol = 0
    total_put_vol = 0
    total_call_oi = 0
    total_put_oi = 0
    call_premium_usd = 0.0
    put_premium_usd = 0.0
    sweeps: list[SweepData] = []
    dark_pool_count = 0

    for r in records:
        opt_type = str(r.get("option_type", "") or r.get("type", "")).lower()
        is_call = opt_type in ("call", "c")
        is_put  = opt_type in ("put", "p")
        if not (is_call or is_put):
            continue

        volume        = _safe_int(r.get("volume") or r.get("contracts"))
        oi            = _safe_int(r.get("open_interest"))
        premium_usd   = _safe_float(r.get("premium") or r.get("total_premium"))
        strike        = _safe_float(r.get("strike") or r.get("strike_price"))
        bid           = _safe_float(r.get("bid"))
        ask           = _safe_float(r.get("ask"))
        is_sweep      = bool(r.get("is_sweep") or r.get("sweep"))
        is_dark_pool  = bool(r.get("is_dark_pool") or r.get("dark_pool"))
        side          = str(r.get("side") or "").lower()
        expiry        = str(r.get("expiry") or r.get("expiration_date") or "")

        if is_call:
            total_call_vol += volume
            total_call_oi  = max(total_call_oi, oi)   # OI is stock-wide, take max across records
            call_premium_usd += premium_usd
        else:
            total_put_vol += volume
            total_put_oi  = max(total_put_oi, oi)
            put_premium_usd += premium_usd

        if is_dark_pool:
            dark_pool_count += 1

        # A sweep in UW is already a real exchange sweep — no vol/OI ratio proxy needed
        if is_sweep and strike > 0:
            sweeps.append(SweepData(
                strike=strike,
                option_type="call" if is_call else "put",
                volume=volume,
                open_interest=oi,
                vol_oi_ratio=round(volume / max(oi, 1), 2),
                bid=bid,
                ask=ask,
                at_ask=(side == "ask"),
            ))

    total_vol = total_call_vol + total_put_vol
    total_oi  = total_call_oi + total_put_oi

    if total_vol == 0:
        return None

    volume_ratio = round(total_vol / max(total_oi, 1), 4)

    if total_put_vol > 0:
        call_put_vol_ratio = round(total_call_vol / total_put_vol, 4)
    elif total_call_vol > 0:
        call_put_vol_ratio = 10.0
    else:
        call_put_vol_ratio = 1.0

    # Net premium skew: positive = more bullish premium than bearish
    net_premium_skew = round((call_premium_usd - put_premium_usd) / max(account_size, 1), 6)

    unusual = volume_ratio > 2.0 or len(sweeps) >= 2

    # Direction uses both vol ratio and premium dominance
    if call_put_vol_ratio > 1.4 and net_premium_skew > 0:
        direction = "bullish"
    elif call_put_vol_ratio < 0.7 and net_premium_skew < 0:
        direction = "bearish"
    else:
        direction = "neutral"

    if unusual and len(sweeps) >= 2 and abs(net_premium_skew) > 0.002:
        strength = "strong"
    elif unusual or len(sweeps) >= 1:
        strength = "moderate"
    else:
        strength = "weak"

    dark_note = f", {dark_pool_count} dark pool print(s)" if dark_pool_count else ""
    summary = (
        f"{ticker} shows {strength} {direction} flow [Unusual Whales]: "
        f"vol/OI {volume_ratio:.2f}, C/P ratio {call_put_vol_ratio:.2f}, "
        f"{len(sweeps)} sweep(s){dark_note}. "
        f"Net premium skew ${call_premium_usd - put_premium_usd:,.0f}."
    )

    sig = FlowSignals(
        ticker=ticker,
        volume_ratio=volume_ratio,
        call_put_vol_ratio=call_put_vol_ratio,
        net_premium_skew=net_premium_skew,
        sweeps=sweeps,
        unusual=unusual,
        direction=direction,
        strength=strength,
        summary=summary,
        timestamp=datetime.now(tz=timezone.utc),
    )

    # Attach extra UW-specific metadata so flow_tools.py can surface it
    sig._uw_meta = {  # type: ignore[attr-defined]
        "source":            "unusual_whales",
        "call_premium_usd":  round(call_premium_usd, 2),
        "put_premium_usd":   round(put_premium_usd, 2),
        "net_premium_usd":   round(call_premium_usd - put_premium_usd, 2),
        "dark_pool_prints":  dark_pool_count,
        "total_flow_alerts": len(records),
    }

    logger.info(
        "unusual_whales [%s]: direction=%s strength=%s sweeps=%d dark_pool=%d premium_net=$%.0f",
        ticker, direction, strength, len(sweeps), dark_pool_count,
        call_premium_usd - put_premium_usd,
    )
    return sig


async def scan_universe(
    tickers: list[str],
    account_size: float = 25_000.0,
) -> list[FlowSignals]:
    """Concurrent flow fetch for up to 10 tickers. Returns non-None results sorted by volume_ratio."""
    capped = tickers[:10]
    tasks  = [get_flow_signals(t, account_size) for t in capped]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    signals = [r for r in results if isinstance(r, FlowSignals)]
    signals.sort(key=lambda s: s.volume_ratio, reverse=True)
    return signals

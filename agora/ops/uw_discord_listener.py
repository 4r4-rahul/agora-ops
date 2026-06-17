"""
agora/ops/uw_discord_listener.py — Unusual Whales Discord alert listener.

Polls a Discord channel where UW posts flow alerts (via webhook) and converts
them into FlowSignals that get_flow_signals() in flow_detector.py can consume.

This bridges the UW web subscription (which includes Discord alert delivery)
to our programmatic flow detection — no API subscription required.

Architecture:
  UW dashboard → Discord webhook → #uw-alerts channel
  This poller → reads channel every 60s → parses UW embed/text
  → updates _STORE[ticker] → get_flow_signals() reads store (20-min TTL)

Setup (done once):
  1. Discord server: create #uw-alerts channel → create a webhook → copy URL
  2. UW dashboard → Alerts → select tickers → set Discord delivery → paste webhook URL
  3. Invite bot to server (see agora docs). Get channel ID → DISCORD_UW_CHANNEL_ID in .env
  4. Restart agora — listener starts automatically when channel ID is set

Message formats supported:
  - UW embed (fields: Ticker, Type, Strike, Expiry, Premium, Vol/OI, Side, Sentiment)
  - UW plain-text (regex extraction of $TICKER, Call/Put, Strike, Premium)
  - Unknown formats → logged at DEBUG level for parser tuning
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any

from agora.services.flow_detector import FlowSignals, SweepData

logger = logging.getLogger(__name__)

_BASE = "https://discord.com/api/v10"
_POLL_INTERVAL = 60       # seconds between channel polls
_SIGNAL_TTL    = 1200     # 20 minutes — discard stale UW signals

# ── In-memory store: ticker → (stored_at, FlowSignals) ──────────────────────
_STORE: dict[str, tuple[float, FlowSignals]] = {}


def get_latest(ticker: str) -> FlowSignals | None:
    """Return cached FlowSignals for ticker if within TTL, else None."""
    entry = _STORE.get(ticker.upper())
    if entry is None:
        return None
    stored_at, sig = entry
    if time.monotonic() - stored_at > _SIGNAL_TTL:
        return None
    return sig


def store_count() -> int:
    """Active (non-expired) signals in store."""
    now = time.monotonic()
    return sum(1 for ts, _ in _STORE.values() if now - ts <= _SIGNAL_TTL)


# ── Discord REST (minimal — mirrors discord_commander.py pattern) ─────────────

async def _api(method: str, path: str, token: str, **kwargs: Any) -> Any:
    try:
        import httpx
    except ImportError:
        return None
    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await getattr(client, method)(f"{_BASE}{path}", headers=headers, **kwargs)
    if resp.status_code in (200, 201):
        return resp.json()
    if resp.status_code == 204:
        return {}
    logger.debug("UW listener Discord %s %s → %d", method.upper(), path, resp.status_code)
    return None


async def _fetch_messages(token: str, channel_id: str, after_id: str) -> list[dict]:
    """Fetch messages after `after_id`, including webhook/bot messages (UW posts as webhook)."""
    r = await _api(
        "get", f"/channels/{channel_id}/messages", token,
        params={"after": after_id, "limit": 50},
    )
    return r if isinstance(r, list) else []


async def _get_latest_message_id(token: str, channel_id: str) -> str | None:
    """Get the ID of the most recent message to use as cursor."""
    r = await _api(
        "get", f"/channels/{channel_id}/messages", token,
        params={"limit": 1},
    )
    if isinstance(r, list) and r:
        return r[0].get("id")
    return None


# ── UW message parser ─────────────────────────────────────────────────────────

_TICKER_RE   = re.compile(r"\$([A-Z]{1,5})\b")
_STRIKE_RE   = re.compile(r"\$?([\d,]+\.?\d*)[CP]?\s*(?:Strike|strike|@)?", re.IGNORECASE)
_PREMIUM_RE  = re.compile(r"\$?([\d,]+\.?\d*)\s*([KMB]?)\s*(?:Premium|premium|prem)", re.IGNORECASE)
_VOLOI_RE    = re.compile(r"([\d.]+)\s*[xX]\s*(?:Vol/OI|vol/oi|Vol)", re.IGNORECASE)
_CONTRACTS_RE = re.compile(r"([\d,]+)\s+(?:contracts?|Contracts?)")
_EXPIRY_RE   = re.compile(r"(\d{4}-\d{2}-\d{2}|\w{3}\s+\d{1,2}[\s,]+'\d{2})")


def _parse_premium(value: str, suffix: str) -> float:
    """Convert '1.25' + 'M' → 1_250_000."""
    try:
        v = float(value.replace(",", ""))
        suffix = suffix.upper()
        if suffix == "M":
            return v * 1_000_000
        if suffix == "B":
            return v * 1_000_000_000
        if suffix == "K":
            return v * 1_000
        return v
    except (ValueError, TypeError):
        return 0.0


def _parse_embed(embed: dict) -> dict | None:
    """Parse a UW-style Discord embed into a structured dict."""
    fields: dict[str, str] = {}
    for f in embed.get("fields", []):
        name  = str(f.get("name",  "")).lower().strip()
        value = str(f.get("value", "")).strip()
        fields[name] = value

    # Required: ticker
    ticker = (
        fields.get("ticker") or
        fields.get("symbol") or
        fields.get("stock")
    )
    if ticker:
        ticker = ticker.lstrip("$").upper().split()[0]
    else:
        # Try title: "🔔 NVDA Call Sweep" or embed description
        title = embed.get("title", "") or embed.get("description", "")
        m = _TICKER_RE.search(title)
        ticker = m.group(1) if m else None

    if not ticker:
        return None

    # Option type
    type_str = (fields.get("type") or fields.get("option type") or
                fields.get("contract type") or "").lower()
    if "put" in type_str:
        opt_type = "put"
    else:
        opt_type = "call"  # default to call if ambiguous

    is_sweep     = "sweep" in type_str or "sweep" in str(embed.get("title", "")).lower()
    is_dark_pool = "dark" in type_str or "dark pool" in str(embed.get("title", "")).lower()

    # Strike
    strike_raw = fields.get("strike") or fields.get("strike price") or ""
    strike_m = re.search(r"[\d.]+", strike_raw.replace(",", ""))
    strike = float(strike_m.group()) if strike_m else 0.0

    # Premium — field value may be just "$1.25M" or "1,250,000" with no "premium" keyword
    premium_raw = fields.get("premium") or fields.get("total premium") or ""
    # Try with keyword regex first, then bare dollar+suffix
    pm = _PREMIUM_RE.search(premium_raw)
    if pm:
        premium_usd = _parse_premium(pm.group(1), pm.group(2))
    else:
        bare = re.search(r"\$?([\d,]+\.?\d*)\s*([KMBkmb]?)", premium_raw)
        premium_usd = _parse_premium(bare.group(1), bare.group(2)) if bare else 0.0

    # Vol/OI — field value may be "8.5x" with no "Vol/OI" keyword
    voloi_raw = fields.get("vol/oi") or fields.get("volume/oi") or fields.get("vol oi") or ""
    vm = _VOLOI_RE.search(voloi_raw)
    if vm:
        vol_oi_ratio = float(vm.group(1))
    else:
        bare_vm = re.search(r"([\d.]+)\s*[xX]?", voloi_raw)
        vol_oi_ratio = float(bare_vm.group(1)) if bare_vm else 0.0

    # Contracts / volume
    contract_raw = fields.get("contracts") or fields.get("size") or fields.get("volume") or "0"
    try:
        contracts = int(contract_raw.replace(",", "").split()[0])
    except (ValueError, IndexError):
        contracts = 0

    # Side (ask = aggressive buyer)
    side = (fields.get("side") or fields.get("order side") or "").lower()

    # Sentiment
    sentiment_raw = (fields.get("sentiment") or "").lower()
    if "bullish" in sentiment_raw:
        sentiment = "bullish"
    elif "bearish" in sentiment_raw:
        sentiment = "bearish"
    else:
        sentiment = "neutral"

    # Expiry
    expiry_raw = fields.get("expiry") or fields.get("expiration") or fields.get("exp") or ""
    expiry = expiry_raw.strip()

    return {
        "ticker":       ticker,
        "opt_type":     opt_type,
        "is_sweep":     is_sweep,
        "is_dark_pool": is_dark_pool,
        "strike":       strike,
        "premium_usd":  premium_usd,
        "vol_oi_ratio": vol_oi_ratio,
        "contracts":    contracts,
        "side":         side,
        "sentiment":    sentiment,
        "expiry":       expiry,
    }


def _parse_text(text: str) -> dict | None:
    """Fallback: regex parse plain-text UW alert."""
    m = _TICKER_RE.search(text)
    if not m:
        return None
    ticker = m.group(1)

    opt_type     = "put" if re.search(r"\bput\b", text, re.IGNORECASE) else "call"
    is_sweep     = bool(re.search(r"\bsweep\b", text, re.IGNORECASE))
    is_dark_pool = bool(re.search(r"\bdark.pool\b", text, re.IGNORECASE))

    pm = _PREMIUM_RE.search(text)
    premium_usd = _parse_premium(pm.group(1), pm.group(2)) if pm else 0.0

    vm = _VOLOI_RE.search(text)
    vol_oi_ratio = float(vm.group(1)) if vm else 0.0

    cm = _CONTRACTS_RE.search(text)
    contracts = int(cm.group(1).replace(",", "")) if cm else 0

    side = "ask" if re.search(r"\bask\b", text, re.IGNORECASE) else (
           "bid" if re.search(r"\bbid\b", text, re.IGNORECASE) else "")

    sentiment_raw = ""
    sm = re.search(r"Sentiment[:\s]+(\w+)", text, re.IGNORECASE)
    if sm:
        sentiment_raw = sm.group(1).lower()
    sentiment = (
        "bullish" if "bullish" in sentiment_raw else
        "bearish" if "bearish" in sentiment_raw else
        "neutral"
    )

    strike = 0.0
    sm2 = re.search(r"\$([\d.]+)[CP]", text)
    if sm2:
        try:
            strike = float(sm2.group(1))
        except ValueError:
            pass

    return {
        "ticker":       ticker,
        "opt_type":     opt_type,
        "is_sweep":     is_sweep,
        "is_dark_pool": is_dark_pool,
        "strike":       strike,
        "premium_usd":  premium_usd,
        "vol_oi_ratio": vol_oi_ratio,
        "contracts":    contracts,
        "side":         side,
        "sentiment":    sentiment,
        "expiry":       "",
    }


def _parse_message(msg: dict) -> dict | None:
    """Extract UW alert data from a Discord message (embed or plain text)."""
    # Try embeds first (richer data)
    for embed in msg.get("embeds", []):
        result = _parse_embed(embed)
        if result:
            return result

    # Plain text fallback
    content = msg.get("content", "")
    if content and _TICKER_RE.search(content):
        return _parse_text(content)

    logger.debug("uw_listener: unrecognised message format — raw: %.200s", str(msg))
    return None


# ── Signal builder ────────────────────────────────────────────────────────────

def _alerts_to_flow_signals(
    ticker: str,
    alerts: list[dict],
    account_size: float = 25_000.0,
) -> FlowSignals:
    """Aggregate multiple parsed UW alerts for one ticker into FlowSignals."""
    call_premium = sum(a["premium_usd"] for a in alerts if a["opt_type"] == "call")
    put_premium  = sum(a["premium_usd"] for a in alerts if a["opt_type"] == "put")
    call_vol = sum(a["contracts"] for a in alerts if a["opt_type"] == "call")
    put_vol  = sum(a["contracts"] for a in alerts if a["opt_type"] == "put")
    call_vol + put_vol

    sweeps = [
        SweepData(
            strike=a["strike"],
            option_type=a["opt_type"],
            volume=a["contracts"],
            open_interest=0,          # UW Discord alerts don't include OI
            vol_oi_ratio=a["vol_oi_ratio"],
            bid=0.0,
            ask=0.0,
            at_ask=(a["side"] == "ask"),
        )
        for a in alerts if a["is_sweep"] and a["strike"] > 0
    ]

    dark_pool_count = sum(1 for a in alerts if a["is_dark_pool"])

    # Direction from sentiment majority
    bullish_n = sum(1 for a in alerts if a["sentiment"] == "bullish")
    bearish_n = sum(1 for a in alerts if a["sentiment"] == "bearish")
    if bullish_n > bearish_n:
        direction = "bullish"
    elif bearish_n > bullish_n:
        direction = "bearish"
    else:
        direction = "neutral"

    net_premium_skew = round((call_premium - put_premium) / max(account_size, 1), 6)
    call_put_vol_ratio = round(call_vol / max(put_vol, 1), 4)
    volume_ratio = round(max(a["vol_oi_ratio"] for a in alerts), 2) if alerts else 0.0
    unusual = len(sweeps) >= 1 or volume_ratio > 2.0

    if unusual and len(sweeps) >= 2 and abs(net_premium_skew) > 0.002:
        strength = "strong"
    elif unusual or len(sweeps) >= 1:
        strength = "moderate"
    else:
        strength = "weak"

    dark_note = f", {dark_pool_count} dark pool" if dark_pool_count else ""
    summary = (
        f"{ticker} shows {strength} {direction} flow [UW Discord]: "
        f"{len(alerts)} alert(s), {len(sweeps)} sweep(s){dark_note}. "
        f"Net premium ${call_premium - put_premium:,.0f}."
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
        timestamp=datetime.now(tz=UTC),
    )
    sig._uw_meta = {  # type: ignore[attr-defined]
        "source":           "unusual_whales_discord",
        "call_premium_usd": round(call_premium, 2),
        "put_premium_usd":  round(put_premium, 2),
        "net_premium_usd":  round(call_premium - put_premium, 2),
        "dark_pool_prints": dark_pool_count,
        "total_flow_alerts": len(alerts),
    }
    return sig


# ── Listener ──────────────────────────────────────────────────────────────────

class UWDiscordListener:
    """
    Background task that polls a Discord channel for UW webhook alerts.

    Two consumers:
      1. FlowSignals store (_STORE) — used by flow_detector.get_flow_signals()
         for supplementary real sweep data alongside yfinance.
      2. uw_alerts SQLite table — consumed by UWMarketIntelAgent for
         classification, realtime forwarding, and scheduled summaries.

    Usage:
        listener = UWDiscordListener(token, channel_id, account_size, db_path)
        asyncio.create_task(listener.start())
        listener.stop()
    """

    def __init__(
        self,
        token:        str,
        channel_id:   str,
        account_size: float = 25_000.0,
        db_path:      str | None = None,
    ) -> None:
        self._token        = token
        self._channel_id   = channel_id
        self._account_size = account_size
        self._db_path      = db_path
        self._last_msg_id: str = "0"
        self._running      = False
        self._pending: dict[str, list[dict]] = {}

    async def start(self) -> None:
        self._running = True
        logger.info(
            "UWDiscordListener started — channel=%s poll_interval=%ds",
            self._channel_id, _POLL_INTERVAL,
        )
        # Seed cursor: start from most recent message so we don't replay history
        last = await _get_latest_message_id(self._token, self._channel_id)
        if last:
            self._last_msg_id = last
            logger.debug("UWDiscordListener: cursor seeded at message %s", last)

        while self._running:
            try:
                await self._poll()
            except Exception as exc:
                logger.warning("UWDiscordListener poll error: %s", exc)
            await asyncio.sleep(_POLL_INTERVAL)

    def stop(self) -> None:
        self._running = False

    def _persist_messages(self, msgs: list[dict]) -> None:
        """Write raw Discord messages to uw_alerts for UWMarketIntelAgent."""
        import sqlite3 as _sqlite3

        from agora.agents.uw_market_intel import _classify, _extract_tickers

        now_utc = datetime.now(UTC).isoformat()
        rows = []
        for msg in msgs:
            msg_id  = msg.get("id", "")
            if not msg_id:
                continue
            author   = (msg.get("author") or {}).get("username", "")
            content  = msg.get("content") or ""
            embeds   = msg.get("embeds") or []
            msg_dict = {"author": author, "content": content, "embeds": embeds}
            topic    = _classify(msg_dict)
            tickers  = _extract_tickers(msg_dict)
            rows.append((
                msg_id, now_utc, author, topic, tickers,
                content[:2000], json.dumps(embeds, default=str)[:4000],
            ))

        if not rows:
            return
        try:
            with _sqlite3.connect(self._db_path, timeout=10) as conn:
                conn.executemany(
                    """INSERT OR IGNORE INTO uw_alerts
                       (discord_msg_id, received_at_utc, author, topic_type, tickers,
                        content, embeds_json)
                       VALUES (?,?,?,?,?,?,?)""",
                    rows,
                )
            logger.debug("UWDiscordListener: persisted %d message(s) to uw_alerts", len(rows))
        except Exception as exc:
            logger.warning("UWDiscordListener: DB persist error: %s", exc)

    async def _poll(self) -> None:
        msgs = await _fetch_messages(self._token, self._channel_id, self._last_msg_id)
        if not msgs:
            return

        # Advance cursor (messages come newest-first from Discord)
        newest_id = max(m["id"] for m in msgs)
        if newest_id > self._last_msg_id:
            self._last_msg_id = newest_id

        # Persist ALL messages to uw_alerts for UWMarketIntelAgent
        if self._db_path:
            self._persist_messages(msgs)

        # Parse messages and group by ticker (flow signal extraction)
        batch: dict[str, list[dict]] = {}
        for msg in msgs:
            author = msg.get("author", {}).get("username", "")
            # Log raw UW messages to help tune the parser
            if msg.get("embeds") or (msg.get("content") and _TICKER_RE.search(msg.get("content", ""))):
                logger.info(
                    "UW raw message [%s]: content=%r embeds=%s",
                    author,
                    msg.get("content", "")[:120],
                    json.dumps(msg.get("embeds", []), default=str)[:400],
                )
            parsed = _parse_message(msg)
            if parsed is None:
                continue
            t = parsed["ticker"]
            batch.setdefault(t, []).append(parsed)
            logger.debug(
                "UW alert parsed: %s %s sweep=%s dp=%s premium=$%.0f",
                t, parsed["opt_type"], parsed["is_sweep"],
                parsed["is_dark_pool"], parsed["premium_usd"],
            )

        # Update store for each ticker that got alerts this poll
        now = time.monotonic()
        for ticker, alerts in batch.items():
            sig = _alerts_to_flow_signals(ticker, alerts, self._account_size)
            _STORE[ticker] = (now, sig)
            logger.info(
                "UWDiscordListener [%s]: stored %d alert(s) → direction=%s strength=%s",
                ticker, len(alerts), sig.direction, sig.strength,
            )

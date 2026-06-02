"""
agora/agents/uw_market_intel.py — UW Market Intelligence Agent.

Processes Unusual Whales Discord alerts from #uw-alerts (free tier) and
produces actionable summaries via Claude.

Modes:
  realtime   — forward flagged alerts immediately (halts, big flow, extreme skew)
  premarket  — 08:30 ET daily: overnight alerts summary
  midday     — 12:30 ET daily: market-hours summary so far
  postclose  — 16:30 ET daily: full-day summary

Data source: uw_alerts SQLite table (populated by UWDiscordListener).
Delivery: ALERT_WEBHOOK_URL (same Discord webhook as other agora alerts).

Model: claude-haiku-4-5 for realtime (latency), claude-sonnet-4-6 for scheduled.
Cost: ~$0.002 realtime / ~$0.05 scheduled → < $1/day.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import anthropic
import httpx

from agora.ops.llm_cost_log import log_call as _log_llm, log_message as _log_msg

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

_TICKER_RE = re.compile(r"\$([A-Z]{1,5})\b")
_PREMIUM_RE = re.compile(r"\$([\d,]+\.?\d*)\s*([KMBkmb]?)")
_PCT_RE = re.compile(r"([+-]?\d+\.?\d*)\s*%")

_MODEL_REALTIME  = "claude-haiku-4-5-20251001"
_MODEL_SCHEDULED = "claude-sonnet-4-6"

# ── Scheduled summary times (ET) ────────────────────────────────────────────
_SCHEDULES = {
    "premarket":  (8,  30),
    "midday":     (12, 30),
    "postclose":  (16, 30),
}
_SUMMARY_WINDOW_HOURS = {
    "premarket":  18,   # since ~2:30 PM yesterday
    "midday":      4,   # since market open (~8:30 AM)
    "postclose":   8,   # full trading day
}

# ── System prompt (cached across calls) ─────────────────────────────────────

_SYSTEM = """\
You are a market intelligence assistant processing Unusual Whales Discord alerts (free tier).

ALERT TYPES:
1. economic_news   → Fed/CPI/jobs/GDP. Extract: event, actual vs expected. Flag if surprise > 0.2%.
2. market_update   → Index moves, sector rotation. Flag moves > 1% intraday.
3. trading_state   → Halts, LULD, circuit breakers. ALWAYS flag (time-sensitive).
4. ticker_update   → Watchlist price/volume/P-C ratio. Flag P/C > 2 (bearish) or < 0.5 (bullish), volume > 2× avg.
5. highest_volume  → Top options contracts. Flag volume > 5× OI.
6. dividend        → Ex-dates, amounts. Log for calendar.
7. uw_tweet        → @unusual_whales tweets. Flag if 3+ tickers or flow keywords (sweep/block/whale).
8. snorlax_tweet   → @snorlax_uw tweets. Same flagging as uw_tweet.
9. flow_alert      → Delayed 5-12 min. Flag premium > $250K. NEVER call real-time.
10. announcement   → Low priority. Summarize only.

WATCHLIST: SPY, QQQ, AAPL, NVDA, MSFT, TSLA, AMZN, META

OUTPUT FORMAT:
- Discord-flavored markdown, < 2000 chars per message
- Emojis: ⚡ flow, 🐋 dark pool, 📊 data, 🏛️ congress, 🛑 halt, 📈 bullish, 📉 bearish, 🔔 econ
- Always cite source alert timestamp
- Skip empty sections, never pad
- Never invent data; omit missing fields
- Deduplicate: ≥3 similar alerts within 10 min → one summary line
- End every message: "Source: Unusual Whales free alerts (5-12 min delay on flow). Not financial advice."

HARD RULES:
- Free flow alerts are DELAYED 5-12 min — never call them "real-time"
- Never extrapolate beyond what alerts literally say
- Malformed/unparseable alerts → skip silently
- Watchlist tickers get priority placement in summaries
"""

_CACHED_SYSTEM = [{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}]


# ── Topic classifier ─────────────────────────────────────────────────────────

def _classify(msg: dict) -> str:
    author  = (msg.get("author") or "").lower()
    content = (msg.get("content") or "").lower()
    embeds  = msg.get("embeds") or []

    all_text = content
    for e in embeds:
        all_text += " " + (e.get("title") or "").lower()
        all_text += " " + (e.get("description") or "").lower()
        for f in (e.get("fields") or []):
            all_text += " " + (f.get("name") or "").lower()
            all_text += " " + (f.get("value") or "").lower()
        url = (e.get("url") or "").lower()
        all_text += " " + url

    if any(k in all_text for k in ("halt", "luld", "circuit breaker", "trading pause", "trading resumed")):
        return "trading_state"
    if any(k in all_text for k in ("cpi", "fomc", "fed rate", "federal reserve", "gdp", "nfp",
                                    "payroll", "inflation data", "economic calendar",
                                    "jobs report", "unemployment rate", "ppi", "retail sales")):
        return "economic_news"
    if any(k in all_text for k in ("unusual options", "flow alert", "sweep", "block trade", "dark pool")):
        return "flow_alert"
    if any(k in all_text for k in ("most active", "highest volume", "top options contracts")):
        return "highest_volume"
    if any(k in all_text for k in ("dividend", "ex-dividend", "ex-date")):
        return "dividend"
    if "snorlax" in author or "snorlax" in all_text:
        return "snorlax_tweet"
    if any(k in all_text for k in ("twitter.com", "x.com", "tweet", "t.co")):
        return "uw_tweet"
    if any(k in all_text for k in ("market update", "pre-market", "sector", "s&p", "nasdaq",
                                    "dow jones", "russell", "index move")):
        return "market_update"
    if _TICKER_RE.search(content) and any(k in all_text for k in
                                           ("volume", "put/call", "p/c", "premium", "price change")):
        return "ticker_update"
    return "announcement"


def _extract_tickers(msg: dict) -> str:
    text = (msg.get("content") or "") + " ".join(
        (e.get("title") or "") + (e.get("description") or "")
        for e in (msg.get("embeds") or [])
    )
    return ",".join(dict.fromkeys(_TICKER_RE.findall(text)))  # ordered dedup


# ── Realtime flag logic ──────────────────────────────────────────────────────

def _flag(msg: dict, topic: str) -> tuple[bool, str]:
    content  = (msg.get("content") or "").lower()
    embeds   = msg.get("embeds") or []
    all_text = content + " ".join(
        (e.get("title") or "") + (e.get("description") or "") +
        " ".join((f.get("value") or "") for f in (e.get("fields") or []))
        for e in embeds
    ).lower()

    if topic == "trading_state":
        return True, "🛑 Trading halt/state change"

    if topic == "economic_news":
        pcts = _PCT_RE.findall(all_text)
        nums = [abs(float(p)) for p in pcts if p]
        if any(n > 0.2 for n in nums):
            return True, f"🔔 Econ surprise > 0.2%"

    if topic == "ticker_update":
        # Flag extreme P/C ratios
        pc_m = re.search(r"p[/\s]?c\s*ratio[:\s]*([\d.]+)", all_text)
        if pc_m:
            pc = float(pc_m.group(1))
            if pc > 2.0:
                return True, f"📉 P/C ratio {pc:.1f} (bearish skew)"
            if pc < 0.5:
                return True, f"📈 P/C ratio {pc:.1f} (bullish skew)"
        # Flag extreme volume
        if re.search(r"\b[2-9]\d*x\b|\b[1-9]\d+x\b", all_text):  # "3x", "5x", etc.
            return True, "⚡ Volume spike vs avg"

    if topic == "highest_volume":
        # Flag volume > 5× OI
        nums = re.findall(r"[\d,]+", all_text)
        if len(nums) >= 2:
            try:
                vol = int(nums[0].replace(",", ""))
                oi  = int(nums[1].replace(",", ""))
                if oi > 0 and vol / oi > 5:
                    return True, f"⚡ Vol {vol:,} > 5× OI {oi:,}"
            except (ValueError, ZeroDivisionError):
                pass

    if topic == "flow_alert":
        pm = _PREMIUM_RE.search(all_text)
        if pm:
            val, suffix = pm.group(1).replace(",", ""), pm.group(2).upper()
            prem = float(val) * (1_000_000 if suffix == "M" else 1_000 if suffix == "K" else 1)
            if prem >= 250_000:
                return True, f"⚡ Premium ${prem:,.0f} (delayed)"

    if topic in ("uw_tweet", "snorlax_tweet"):
        tickers = _TICKER_RE.findall(content)
        flow_kw = any(k in all_text for k in ("sweep", "block", "whale", "institutional", "dark pool"))
        if len(tickers) >= 3 or flow_kw:
            return True, f"🐋 UW tweet: {len(tickers)} tickers / flow keyword"

    return False, ""


# ── Agent ────────────────────────────────────────────────────────────────────

class UWMarketIntelAgent:
    """
    Processes UW Discord alerts from uw_alerts table and delivers:
      - Realtime forwarding for flagged alerts
      - Scheduled premarket / midday / postclose summaries
    """

    def __init__(
        self,
        db_path:     str,
        settings:    Any,
        webhook_url: str | None = None,
    ) -> None:
        self._db       = db_path
        self._settings = settings
        self._webhook  = webhook_url or getattr(settings, "alert_webhook_url", None)
        self._client   = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        self._running  = False
        self._last_summary: dict[str, datetime] = {}   # mode → last run time
        logger.info("UWMarketIntelAgent ready — webhook=%s", bool(self._webhook))

    async def start(self) -> None:
        self._running = True
        logger.info("UWMarketIntelAgent started")
        while self._running:
            try:
                await self._tick()
            except Exception as exc:
                logger.warning("UWMarketIntelAgent tick error: %s", exc)
            await asyncio.sleep(30)

    def stop(self) -> None:
        self._running = False

    # ── Main tick ────────────────────────────────────────────────────

    async def _tick(self) -> None:
        now_et = datetime.now(tz=ET)

        # Realtime: forward any newly flagged alerts
        await self._process_realtime()

        # Scheduled summaries (fire once per window, not repeatedly)
        for mode, (h, m) in _SCHEDULES.items():
            target = now_et.replace(hour=h, minute=m, second=0, microsecond=0)
            last   = self._last_summary.get(mode)
            # Fire if we're within 2 min after target AND haven't run today
            within_window = abs((now_et - target).total_seconds()) < 120
            already_ran   = last is not None and last.date() == now_et.date()
            if within_window and not already_ran and now_et >= target:
                await self._run_scheduled(mode)
                self._last_summary[mode] = now_et

    # ── Realtime processing ──────────────────────────────────────────

    async def _process_realtime(self) -> None:
        rows = self._fetch_unflagged_pending()
        if not rows:
            return
        for row in rows:
            msg_id, content, embeds_json, topic = (
                row["discord_msg_id"], row["content"],
                row["embeds_json"], row["topic_type"],
            )
            msg = {
                "content": content or "",
                "embeds":  json.loads(embeds_json or "[]"),
                "author":  row["author"] or "",
            }
            flagged, reason = _flag(msg, topic or "announcement")
            self._update_flag(msg_id, flagged, reason)

            if flagged and self._webhook:
                summary = await self._realtime_summary(msg, topic or "announcement", reason)
                if summary:
                    await self._send(summary)
                    self._mark_realtime_sent(msg_id)

    async def _realtime_summary(self, msg: dict, topic: str, reason: str) -> str | None:
        user_msg = (
            f"topic_type: {topic}\n"
            f"flag_reason: {reason}\n"
            f"content: {msg.get('content', '')[:500]}\n"
            f"embeds: {json.dumps(msg.get('embeds', []))[:800]}\n\n"
            "Write a single short Discord alert message (max 280 chars) in realtime mode. "
            "Include the flag reason, key data points, and the delay disclaimer for flow alerts."
        )
        try:
            resp = await self._client.messages.create(
                model=_MODEL_REALTIME,
                max_tokens=256,
                system=_CACHED_SYSTEM,
                messages=[{"role": "user", "content": user_msg}],
                timeout=anthropic.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0),
            )
            _log_msg(self._db, "UWMarketIntelRT", _MODEL_REALTIME,
                     resp.usage, purpose=f"uw_realtime_{topic}")
            return resp.content[0].text.strip() if resp.content else None
        except Exception as exc:
            logger.warning("UWMarketIntelAgent realtime LLM error: %s", exc)
            return None

    # ── Scheduled summaries ──────────────────────────────────────────

    async def _run_scheduled(self, mode: str) -> None:
        hours = _SUMMARY_WINDOW_HOURS[mode]
        since = datetime.now(tz=timezone.utc) - timedelta(hours=hours)
        rows  = self._fetch_since(since.isoformat())

        if not rows:
            logger.info("UWMarketIntel %s: no alerts in window, skipping", mode)
            return

        # Group by topic_type
        by_topic: dict[str, list[dict]] = {}
        for r in rows:
            t = r["topic_type"] or "announcement"
            by_topic.setdefault(t, []).append({
                "ts":      r["received_at_utc"],
                "tickers": r["tickers"] or "",
                "content": (r["content"] or "")[:300],
                "embeds":  json.loads(r["embeds_json"] or "[]")[:2],
                "flagged": bool(r["flagged"]),
                "reason":  r["flag_reason"] or "",
            })

        user_msg = (
            f"mode: {mode}\n"
            f"window_hours: {hours}\n"
            f"alert_counts: {json.dumps({k: len(v) for k, v in by_topic.items()})}\n"
            f"alerts_by_topic: {json.dumps(by_topic, default=str)[:3000]}\n\n"
            f"Generate the {mode} summary following all output rules. "
            "Lead with halts > economic events > watchlist tickers > flow. "
            "Skip topics with zero alerts. "
            "Keep total under 1800 chars."
        )
        try:
            resp = await self._client.messages.create(
                model=_MODEL_SCHEDULED,
                max_tokens=1024,
                system=_CACHED_SYSTEM,
                messages=[{"role": "user", "content": user_msg}],
                timeout=anthropic.Timeout(connect=30.0, read=60.0, write=30.0, pool=30.0),
            )
            _log_msg(self._db, "UWMarketIntel", _MODEL_SCHEDULED,
                     resp.usage, purpose=f"uw_{mode}")
            text = resp.content[0].text.strip() if resp.content else ""
            if text and self._webhook:
                await self._send(text)
                self._mark_summary_sent(rows, mode)
            logger.info("UWMarketIntel %s summary sent (%d alerts → %d chars)",
                        mode, len(rows), len(text))
        except Exception as exc:
            logger.warning("UWMarketIntel scheduled %s LLM error: %s", mode, exc)

    # ── Delivery ─────────────────────────────────────────────────────

    async def _send(self, text: str) -> None:
        if not self._webhook:
            return
        # Split at 1900 chars if needed
        chunks = [text[i:i + 1900] for i in range(0, len(text), 1900)]
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                for chunk in chunks:
                    await c.post(self._webhook, json={"content": chunk})
                    if len(chunks) > 1:
                        await asyncio.sleep(0.5)
        except Exception as exc:
            logger.warning("UWMarketIntel Discord send error: %s", exc)

    # ── DB helpers ───────────────────────────────────────────────────

    def _fetch_unflagged_pending(self) -> list[sqlite3.Row]:
        try:
            with sqlite3.connect(self._db, timeout=10) as conn:
                conn.row_factory = sqlite3.Row
                return conn.execute(
                    "SELECT * FROM uw_alerts WHERE flagged=0 AND realtime_sent=0 "
                    "ORDER BY received_at_utc DESC LIMIT 50"
                ).fetchall()
        except Exception:
            return []

    def _fetch_since(self, since_utc: str) -> list[sqlite3.Row]:
        try:
            with sqlite3.connect(self._db, timeout=10) as conn:
                conn.row_factory = sqlite3.Row
                return conn.execute(
                    "SELECT * FROM uw_alerts WHERE received_at_utc >= ? "
                    "ORDER BY received_at_utc ASC",
                    (since_utc,),
                ).fetchall()
        except Exception:
            return []

    def _update_flag(self, msg_id: str, flagged: bool, reason: str) -> None:
        try:
            with sqlite3.connect(self._db, timeout=10) as conn:
                conn.execute(
                    "UPDATE uw_alerts SET flagged=?, flag_reason=? WHERE discord_msg_id=?",
                    (1 if flagged else -1, reason, msg_id),  # -1 = evaluated, not flagged
                )
        except Exception:
            pass

    def _mark_realtime_sent(self, msg_id: str) -> None:
        try:
            with sqlite3.connect(self._db, timeout=10) as conn:
                conn.execute(
                    "UPDATE uw_alerts SET realtime_sent=1 WHERE discord_msg_id=?",
                    (msg_id,),
                )
        except Exception:
            pass

    def _mark_summary_sent(self, rows: list, mode: str) -> None:
        ids = [r["discord_msg_id"] for r in rows]
        if not ids:
            return
        try:
            with sqlite3.connect(self._db, timeout=10) as conn:
                conn.executemany(
                    "UPDATE uw_alerts SET included_in_summary=? WHERE discord_msg_id=?",
                    [(mode, mid) for mid in ids],
                )
        except Exception:
            pass

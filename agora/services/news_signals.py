"""
agora/services/news_signals.py — News Signal Bus.

Reads uw_alerts DB (populated by UWDiscordListener) and extracts three tiers
of actionable signals from UW premium news and market commentary:

  Tier 1 — Halts (trading_state):
    Tickers with an active trading halt → close positions immediately.
    A subsequent "resumed" message removes the ticker from the halt set.

  Tier 2 — Macro events (economic_news):
    CPI/Fed/NFP surprises → override macro_stance to risk_off / risk_on / crisis.
    Most impactful event in the lookback window wins.

  Tier 3 — Ticker flags (ticker_update / uw_tweet / snorlax_tweet / flow_alert):
    Watchlist mentions with directional keywords → +1 bull/bear signal.
    Advisory only — never blocks entry, just nudges conviction score.

Public API:
    get_news_context(db_path, lookback_minutes=30) → NewsContext
    invalidate_cache()   # call when you know new data arrived

Results are cached 30s to avoid repeated SQLite hits from tight poll loops.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

logger = logging.getLogger(__name__)

_TICKER_RE = re.compile(r"\$([A-Z]{1,5})\b")
_CACHE_TTL  = 30.0   # seconds

_cache: tuple[float, NewsContext] | None = None


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class TickerFlag:
    ticker:    str
    direction: str    # "bullish" | "bearish"
    reason:    str
    ts:        datetime


@dataclass
class MacroEvent:
    stance:  str    # "risk_on" | "risk_off" | "crisis"
    reason:  str
    ts:      datetime


@dataclass
class NewsContext:
    halted_tickers: set[str]               = field(default_factory=set)
    macro_event:    MacroEvent | None      = None
    ticker_flags:   dict[str, TickerFlag]  = field(default_factory=dict)
    last_checked:   datetime               = field(default_factory=lambda: datetime.now(UTC))


# ── Public API ────────────────────────────────────────────────────────────────

def get_news_context(db_path: str, lookback_minutes: int = 30) -> NewsContext:
    """
    Return actionable NewsContext built from recent uw_alerts rows.
    Cached 30s; safe to call from any hot loop.
    """
    global _cache
    now = time.monotonic()
    if _cache is not None and now - _cache[0] < _CACHE_TTL:
        return _cache[1]
    try:
        ctx = _build_context(db_path, lookback_minutes)
    except Exception as exc:
        logger.debug("NewsSignalBus build error: %s", exc)
        ctx = NewsContext()
    _cache = (now, ctx)
    return ctx


def invalidate_cache() -> None:
    """Force next get_news_context() call to re-read the DB."""
    global _cache
    _cache = None


# ── Parser ────────────────────────────────────────────────────────────────────

def _build_context(db_path: str, lookback_minutes: int) -> NewsContext:
    since = (datetime.now(UTC) - timedelta(minutes=lookback_minutes)).isoformat()
    with sqlite3.connect(db_path, timeout=10) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        rows = conn.execute(
            "SELECT topic_type, content, embeds_json, tickers, received_at_utc "
            "FROM uw_alerts WHERE received_at_utc >= ? ORDER BY received_at_utc ASC",
            (since,),
        ).fetchall()

    ctx = NewsContext(last_checked=datetime.now(UTC))

    # Track halt/resume per ticker so a later "resumed" cancels an earlier "halt"
    halt_state: dict[str, bool] = {}   # ticker → True=halted, False=resumed

    for topic, content, embeds_json, tickers_csv, ts_str in rows:
        content = content or ""
        ts      = _parse_ts(ts_str)
        tickers = {t for t in (tickers_csv or "").upper().split(",") if t}

        try:
            embeds = json.loads(embeds_json or "[]")
        except Exception:
            embeds = []

        full_text = _flatten(content, embeds)

        if topic == "trading_state":
            _handle_halt(halt_state, content, full_text, tickers)
        elif topic == "economic_news":
            _handle_macro(ctx, full_text, ts)
        elif topic in ("ticker_update", "uw_tweet", "snorlax_tweet", "flow_alert"):
            _handle_ticker_flag(ctx, content, full_text, tickers, ts)

    # Resolve halt_state into ctx.halted_tickers (only those still halted)
    ctx.halted_tickers = {t for t, halted in halt_state.items() if halted}

    return ctx


def _flatten(content: str, embeds: list) -> str:
    parts = [content.lower()]
    for e in embeds:
        parts.append((e.get("title") or "").lower())
        parts.append((e.get("description") or "").lower())
        for f in e.get("fields", []):
            parts.append((f.get("name") or "").lower())
            parts.append((f.get("value") or "").lower())
    return " ".join(parts)


# ── Tier 1: Halts ─────────────────────────────────────────────────────────────

_HALT_KEYWORDS    = ("halt", "luld", "trading pause", "regulatory halt")
_RESUME_KEYWORDS  = ("resumed", "trading resumed", "halt lifted", "halt cleared")


def _handle_halt(
    halt_state: dict[str, bool],
    content: str,
    full_text: str,
    tickers: set[str],
) -> None:
    is_resume = any(k in full_text for k in _RESUME_KEYWORDS)
    is_halt   = any(k in full_text for k in _HALT_KEYWORDS)

    if not is_halt and not is_resume:
        return

    found = tickers | set(_TICKER_RE.findall(content.upper()))
    if not found:
        return

    for ticker in found:
        if is_resume:
            halt_state[ticker] = False
            logger.info("NewsSignalBus: halt CLEARED for %s", ticker)
        elif is_halt:
            halt_state[ticker] = True
            logger.info("NewsSignalBus: halt DETECTED for %s", ticker)


# ── Tier 2: Macro events ──────────────────────────────────────────────────────

_CRISIS_PATTERNS = [
    "circuit breaker", "market halt", "emergency fed", "systemic risk",
    "market crash", "black swan", "federal reserve emergency",
]
_RISK_OFF_PATTERNS = [
    "hotter than expected", "above expectations", "above forecast",
    "hawkish", "rate hike surprise", "recession fears", "misses estimates",
    "below expectations", "disappoints", "contraction", "stagflation",
    "worse than expected", "higher than expected inflation",
]
_RISK_ON_PATTERNS = [
    "cooler than expected", "below expectations", "dovish",
    "rate cut", "better than expected", "beats estimates", "beats expectations",
    "strong jobs", "strong gdp", "lower than expected inflation",
    "soft landing", "better than forecast",
]

# Priority order: crisis > risk_off > risk_on
_MACRO_TIERS: list[tuple[str, list[str]]] = [
    ("crisis",   _CRISIS_PATTERNS),
    ("risk_off", _RISK_OFF_PATTERNS),
    ("risk_on",  _RISK_ON_PATTERNS),
]


def _handle_macro(ctx: NewsContext, full_text: str, ts: datetime) -> None:
    # Only update when this event is more recent than the current one
    if ctx.macro_event and ts <= ctx.macro_event.ts:
        return
    for stance, patterns in _MACRO_TIERS:
        hit = _first_match(full_text, patterns)
        if hit:
            ctx.macro_event = MacroEvent(stance, hit, ts)
            logger.info("NewsSignalBus: macro event %s (%s)", stance, hit)
            return


# ── Tier 3: Ticker flags ──────────────────────────────────────────────────────

_BULL_KEYWORDS = [
    "bullish", "call sweep", "unusual call volume", "call buyer",
    "upgrade", "outperform", "buy rating", "raised guidance", "beats",
    "breakout", "strong earnings", "positive catalyst", "record high",
    "above sma", "golden cross",
]
_BEAR_KEYWORDS = [
    "bearish", "put sweep", "unusual put volume", "put buyer",
    "downgrade", "underperform", "sell rating", "cut guidance", "misses",
    "breakdown", "weak earnings", "negative catalyst", "52-week low",
    "death cross", "below sma",
]


def _handle_ticker_flag(
    ctx: NewsContext,
    content: str,
    full_text: str,
    tickers: set[str],
    ts: datetime,
) -> None:
    if not tickers:
        tickers = set(_TICKER_RE.findall(content.upper()))
    if not tickers:
        return

    bull_hits = sum(1 for k in _BULL_KEYWORDS if k in full_text)
    bear_hits = sum(1 for k in _BEAR_KEYWORDS if k in full_text)

    if bull_hits == bear_hits or (bull_hits == 0 and bear_hits == 0):
        return  # ambiguous — skip

    direction = "bullish" if bull_hits > bear_hits else "bearish"
    reason    = _first_match(full_text, _BULL_KEYWORDS if direction == "bullish" else _BEAR_KEYWORDS)

    for ticker in tickers:
        existing = ctx.ticker_flags.get(ticker)
        if existing is None or ts >= existing.ts:
            ctx.ticker_flags[ticker] = TickerFlag(ticker, direction, reason, ts)
            logger.debug("NewsSignalBus: ticker flag %s %s (%s)", ticker, direction, reason)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_ts(ts_str: str) -> datetime:
    try:
        dt = datetime.fromisoformat(ts_str)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except Exception:
        return datetime.now(UTC)


def _first_match(text: str, patterns: list[str]) -> str:
    for p in patterns:
        if p in text:
            return p
    return ""

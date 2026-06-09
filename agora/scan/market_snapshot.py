"""
agora/scan/market_snapshot.py — shared per-ticker market-data snapshot cache + rate limiter.

WHY (scanner-architecture review #1/#2/#6):
  The spread scan and the long-options loop each pulled the SAME yfinance data for the same
  tickers, independently — option chains, spot, daily history — with no shared cache. That
  doubled external calls (the engine was throwing yfinance 404/throttle errors) and created a
  DUAL SOURCE OF TRUTH: the two pipelines could evaluate one underlying on divergent snapshots
  (slightly different spot / bars) at the same instant.

  This module fetches each datum ONCE per short TTL and serves both pipelines the same value:
    • #1  cuts duplicate yfinance calls (overlapping expiries + spot + history are fetched once)
    • #2  both pipelines read identical prices for a ticker within the TTL window
    • #6  a global token-bucket throttle bounds yfinance network-call rate (replaces the blind
          210s scan stagger with real backpressure)

  Thread-safe: both pipelines call from asyncio.to_thread worker threads. Reads are O(1) under a
  fast lock; the actual yfinance calls remain serialized by the provider's _YF_OPTIONS_LOCK.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Reuse the provider's global options lock so yfinance option calls stay serialized exactly
# as before — this module changes WHO calls yfinance and how often, not the call's safety.
from trading_platform.services.market_data.yfinance_provider import _YF_OPTIONS_LOCK

# ── TTLs (seconds) ────────────────────────────────────────────────────────────
# Tuned to each datum's volatility: spot moves intraday (short), the expiry list and daily
# history barely move within a session (long). A scan cycle is ~minutes, so even a 30s spot
# TTL serves the second pipeline from cache while staying fresh for decisions.
_SPOT_TTL      = 30.0
_CHAIN_TTL     = 45.0
_EXPIRIES_TTL  = 600.0
_HIST_TTL      = 300.0

# ── #6 rate limiter: global token bucket bounding yfinance NETWORK calls/sec ──────
# Both pipelines + signal helpers share one budget so a burst can't trip free-tier throttling
# (the 404 storms). ~6 calls/sec sustained with a small burst allowance.
_RATE_PER_SEC  = 6.0
_BURST         = 8.0


@dataclass
class _Entry:
    value: Any
    expires_at: float


class _RateLimiter:
    """Simple monotonic token bucket. acquire() blocks the calling worker thread until a token
    is available — backpressure shared across every yfinance network call in the process."""

    def __init__(self, rate_per_sec: float, burst: float) -> None:
        self._rate = rate_per_sec
        self._capacity = burst
        self._tokens = burst
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            self._tokens = min(self._capacity, self._tokens + (now - self._last) * self._rate)
            self._last = now
            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) / self._rate
                time.sleep(wait)
                self._tokens = 0.0
                self._last = time.monotonic()
            else:
                self._tokens -= 1.0


class MarketSnapshot:
    """Process-wide cache of per-ticker yfinance data. Both scan pipelines read through it so a
    ticker's chain/spot/history is fetched once per TTL and shared, not duplicated."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._expiries: dict[str, _Entry] = {}
        self._chain: dict[tuple[str, str], _Entry] = {}
        self._spot: dict[str, _Entry] = {}
        self._hist: dict[tuple[str, str], _Entry] = {}
        self._limiter = _RateLimiter(_RATE_PER_SEC, _BURST)
        self._hits = 0
        self._misses = 0

    # ── internal helpers ──────────────────────────────────────────────────────
    def _cached(self, store: dict, key: Any) -> tuple[bool, Any]:
        now = time.monotonic()
        with self._lock:
            e = store.get(key)
            if e is not None and e.expires_at > now:
                self._hits += 1
                return True, e.value
        return False, None

    def _store(self, store: dict, key: Any, value: Any, ttl: float) -> None:
        with self._lock:
            store[key] = _Entry(value, time.monotonic() + ttl)
            self._misses += 1

    # ── public accessors (identical data for both pipelines) ──────────────────
    def expiries(self, ticker: str) -> list[str]:
        """Sorted option-expiry strings (YYYY-MM-DD) for a ticker. Cached ~10 min."""
        hit, val = self._cached(self._expiries, ticker)
        if hit:
            return val
        import yfinance as yf
        exps: list[str] = []
        for _attempt in range(2):
            try:
                self._limiter.acquire()
                with _YF_OPTIONS_LOCK:
                    exps = list(yf.Ticker(ticker).options or [])
                if exps or _attempt == 1:
                    break
                time.sleep(0.5)   # empty may mean a stale crumb — retry once
            except Exception:
                if _attempt == 0:
                    time.sleep(1.0)
        self._store(self._expiries, ticker, exps, _EXPIRIES_TTL)
        return exps

    def option_chain(self, ticker: str, expiry: str) -> dict | None:
        """{"calls": DataFrame, "puts": DataFrame} for one (ticker, expiry), or None. Cached."""
        key = (ticker, expiry)
        hit, val = self._cached(self._chain, key)
        if hit:
            return val
        import yfinance as yf
        result: dict | None = None
        try:
            self._limiter.acquire()
            with _YF_OPTIONS_LOCK:
                c = yf.Ticker(ticker).option_chain(expiry)
            result = {"calls": c.calls, "puts": c.puts}
        except Exception as exc:
            logger.debug("option_chain miss %s %s: %s", ticker, expiry, exc)
            result = None
        if result is not None:
            self._store(self._chain, key, result, _CHAIN_TTL)
        return result

    def spot(self, ticker: str) -> float:
        """Last/previous-close spot for a ticker, cached ~30s. 0.0 on failure."""
        hit, val = self._cached(self._spot, ticker)
        if hit:
            return val
        import yfinance as yf
        px = 0.0
        try:
            self._limiter.acquire()
            fi = yf.Ticker(ticker).fast_info
            px = float(fi.get("lastPrice", 0) or fi.get("previousClose", 0) or 0)
        except Exception:
            px = 0.0
        if px > 0:
            self._store(self._spot, ticker, px, _SPOT_TTL)
        return px

    def history(self, ticker: str, period: str = "3mo", interval: str = "1d") -> Any:
        """Daily-bar history DataFrame, cached ~5 min. None on failure."""
        key = (ticker, f"{period}:{interval}")
        hit, val = self._cached(self._hist, key)
        if hit:
            return val
        import yfinance as yf
        hist = None
        try:
            self._limiter.acquire()
            hist = yf.Ticker(ticker).history(period=period, interval=interval)
        except Exception:
            hist = None
        if hist is not None and not hist.empty:
            self._store(self._hist, key, hist, _HIST_TTL)
        return hist

    def stats(self) -> dict[str, Any]:
        total = self._hits + self._misses
        return {
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / total, 3) if total else 0.0,
        }


_SNAPSHOT: MarketSnapshot | None = None
_SNAPSHOT_LOCK = threading.Lock()


def get_market_snapshot() -> MarketSnapshot:
    """Process-wide singleton — both scan pipelines share one cache."""
    global _SNAPSHOT
    if _SNAPSHOT is None:
        with _SNAPSHOT_LOCK:
            if _SNAPSHOT is None:
                _SNAPSHOT = MarketSnapshot()
    return _SNAPSHOT

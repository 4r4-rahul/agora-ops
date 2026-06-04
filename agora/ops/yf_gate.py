"""
agora/ops/yf_gate.py — process-wide yfinance throttle + TTL cache.

Problem: ~139 call sites hit yfinance via raw `yf.Ticker(...)`, bypassing the central
provider — so the same ticker's heavy data (14-month history for momentum/IVR, option
chains for the 60s position refresh) is re-fetched by multiple loops with no shared cache
or rate limiting. Result: the free tier saturates (257 "Too Many Requests" in one day),
which fails evaluations and injects stale/NaN marks downstream.

Fix: install() monkeypatches the TWO heaviest, most-cacheable yfinance operations
— Ticker.history() and Ticker.option_chain() — to route through one global gate:
  • TTL cache (history rarely changes intraday; a chain is fine for ~90s on swing trades),
  • a concurrency semaphore + minimum call spacing (caps request volume),
  • backoff-and-retry on rate-limit errors.
This is a global interception (covers all call sites with zero migration) limited to two
well-defined methods, installed once, fully reversible (uninstall()), and DEFENSIVE — any
wrapper error falls back to the original yfinance call so a bug here can never break data.

Lighter ops (.info / .fast_info) are left direct; throttling the heavy ops cuts total
request volume enough to relieve rate-limit pressure across everything.
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

# ── Throttle ────────────────────────────────────────────────────────────────
_MAX_CONCURRENCY = 3            # max simultaneous yfinance HTTP fetches
_MIN_SPACING_SEC = 0.20         # minimum gap between fetch starts
_BACKOFF_BASE_SEC = 1.5         # exponential backoff on rate-limit
_BACKOFF_ATTEMPTS = 3

_sem = threading.Semaphore(_MAX_CONCURRENCY)
_spacing_lock = threading.Lock()
_last_start = [0.0]

# ── TTL cache ─────────────────────────────────────────────────────────────────
_TTL = {"history": 600.0, "option_chain": 90.0}   # seconds
_cache: dict = {}
_cache_lock = threading.Lock()

# ── Stats (for visibility) ──────────────────────────────────────────────────
_stats = {"history_hit": 0, "history_miss": 0, "chain_hit": 0, "chain_miss": 0,
          "rate_limit_retries": 0}

_installed = False
_orig_history = None
_orig_option_chain = None


def _is_rate_limit(exc: Exception) -> bool:
    s = str(exc).lower()
    return "too many requests" in s or "rate limit" in s or "yfratelimit" in s or "429" in s


def _gated_call(fn, *args, **kwargs):
    """Run a yfinance fetch under the global spacing + concurrency cap, with backoff."""
    for attempt in range(_BACKOFF_ATTEMPTS):
        # Minimum spacing between any two fetch starts (process-wide).
        with _spacing_lock:
            gap = _MIN_SPACING_SEC - (time.monotonic() - _last_start[0])
            if gap > 0:
                time.sleep(gap)
            _last_start[0] = time.monotonic()
        with _sem:
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                if _is_rate_limit(exc) and attempt < _BACKOFF_ATTEMPTS - 1:
                    _stats["rate_limit_retries"] += 1
                    time.sleep(_BACKOFF_BASE_SEC * (2 ** attempt))
                    continue
                raise
    # Should be unreachable, but keep a definite return path.
    return fn(*args, **kwargs)


def _cache_get(key):
    with _cache_lock:
        ent = _cache.get(key)
        if ent and time.monotonic() < ent[0]:
            return ent[1]
    return None


def _cache_put(key, val, ttl):
    with _cache_lock:
        _cache[key] = (time.monotonic() + ttl, val)


def install() -> None:
    """Idempotently monkeypatch yf.Ticker.history + option_chain with cache+throttle.
    Safe to call multiple times; safe if yfinance is missing (no-op)."""
    global _installed, _orig_history, _orig_option_chain
    if _installed:
        return
    try:
        import yfinance as yf
    except Exception as exc:
        logger.warning("yf_gate: yfinance unavailable, gate not installed: %s", exc)
        return

    _orig_history = yf.Ticker.history
    _orig_option_chain = yf.Ticker.option_chain

    def _history(self, *args, **kwargs):
        try:
            sym = getattr(self, "ticker", "?")
            key = ("hist", sym, str(args), str(sorted(kwargs.items())))
            hit = _cache_get(key)
            if hit is not None:
                _stats["history_hit"] += 1
                return hit
            _stats["history_miss"] += 1
            val = _gated_call(_orig_history, self, *args, **kwargs)
            # Only cache non-empty results (don't pin a transient empty/failed fetch).
            if val is not None and getattr(val, "empty", False) is False:
                _cache_put(key, val, _TTL["history"])
            return val
        except Exception:
            # Defensive: never let the gate break data — fall back to the raw call.
            return _orig_history(self, *args, **kwargs)

    def _option_chain(self, *args, **kwargs):
        try:
            sym = getattr(self, "ticker", "?")
            key = ("chain", sym, str(args), str(sorted(kwargs.items())))
            hit = _cache_get(key)
            if hit is not None:
                _stats["chain_hit"] += 1
                return hit
            _stats["chain_miss"] += 1
            val = _gated_call(_orig_option_chain, self, *args, **kwargs)
            if val is not None:
                _cache_put(key, val, _TTL["option_chain"])
            return val
        except Exception:
            return _orig_option_chain(self, *args, **kwargs)

    yf.Ticker.history = _history
    yf.Ticker.option_chain = _option_chain
    _installed = True
    logger.info("yf_gate installed: throttle(conc=%d, spacing=%.2fs) + TTL cache "
                "(history=%.0fs, chain=%.0fs) on Ticker.history/option_chain",
                _MAX_CONCURRENCY, _MIN_SPACING_SEC, _TTL["history"], _TTL["option_chain"])


def uninstall() -> None:
    """Restore original yfinance methods (reversibility)."""
    global _installed
    if not _installed:
        return
    try:
        import yfinance as yf
        if _orig_history:
            yf.Ticker.history = _orig_history
        if _orig_option_chain:
            yf.Ticker.option_chain = _orig_option_chain
    except Exception:
        pass
    _installed = False


def stats() -> dict:
    """Cache hit/miss + retry counters, plus derived hit-rates (for the dashboard)."""
    h_total = _stats["history_hit"] + _stats["history_miss"]
    c_total = _stats["chain_hit"] + _stats["chain_miss"]
    return {
        **_stats,
        "installed": _installed,
        "history_hit_rate": round(_stats["history_hit"] / h_total, 3) if h_total else 0.0,
        "chain_hit_rate": round(_stats["chain_hit"] / c_total, 3) if c_total else 0.0,
        "cached_keys": len(_cache),
    }

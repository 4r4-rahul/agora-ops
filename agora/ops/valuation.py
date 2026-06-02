"""
agora/ops/valuation.py — Fundamental Valuation + Earnings Revision Gate

Fetches fundamental metrics from yfinance and evaluates whether a stock
is cheap, fairly valued, expensive, or very expensive relative to its
GICS sector peers.  Also computes earnings revision momentum from analyst
estimate changes.

Valuation Score (0-10):
  Three metrics (trailingPE, EV/EBITDA, P/B) each scored:
    ≤ 0.75 × sector median → 2.5 pts  (cheap)
    ≤      sector median   → 1.5 pts  (fair)
    ≤ 1.50 × sector median → 0.5 pts  (slightly expensive)
    > 1.50 × sector median → 0.0 pts  (expensive)
  Max = 7.5 pts → scaled to 0-10.

Revision Momentum:
  net = (upLast30days_0q + upLast30days_+1q) - (downLast30days_0q + downLast30days_+1q)
  > 10  → strong_upgrade  (+5 pts conviction)
  > 3   → upgrade         (+2 pts)
  -3–3  → neutral         (  0 pts)
  < -3  → downgrade       (-3 pts)
  < -10 → strong_downgrade(-7 pts)

Cache TTL: 4 hours (fundamentals don't change intraday but refresh after earnings).
ETFs are skipped — return a neutral result immediately.

References:
  • Damodaran, "Investment Valuation" — EV/EBITDA and sector multiples
  • Lev & Sougiannis, "The Capitalization, Amortization, and Value-Relevance of R&D" — 1996
  • Chan, Jegadeesh & Lakonishok, "Earnings Quality and Stock Returns" — JBF 2004
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import yfinance as yf

logger = logging.getLogger(__name__)

# ── ETF exemption list ─────────────────────────────────────────────────────────
_ETF_TICKERS: frozenset[str] = frozenset({
    "SPY", "QQQ", "IWM", "GLD", "TLT", "GDX",
    "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLRE", "XLC", "XLB",
    "PPLT", "SLV",
})

# ── Sector reference medians (PE, EV/EBITDA, P/B) ────────────────────────────
# Source: Damodaran NYU sector multiples (2025 estimates; conservative)
_SECTOR_MEDIANS: dict[str, dict[str, float]] = {
    "Technology":                 {"pe": 30.0, "ev_ebitda": 25.0, "pb": 8.0},
    "Financials":                 {"pe": 15.0, "ev_ebitda": 12.0, "pb": 1.5},
    "Healthcare":                 {"pe": 20.0, "ev_ebitda": 15.0, "pb": 4.0},
    "Consumer Discretionary":     {"pe": 25.0, "ev_ebitda": 18.0, "pb": 5.0},
    "Consumer Staples":           {"pe": 22.0, "ev_ebitda": 16.0, "pb": 4.0},
    "Energy":                     {"pe": 12.0, "ev_ebitda":  8.0, "pb": 1.5},
    "Industrials":                {"pe": 20.0, "ev_ebitda": 14.0, "pb": 3.0},
    "Communication Services":     {"pe": 22.0, "ev_ebitda": 18.0, "pb": 4.0},
    "Materials":                  {"pe": 18.0, "ev_ebitda": 10.0, "pb": 2.0},
    # Default for unmapped or mixed sectors
    "_default":                   {"pe": 20.0, "ev_ebitda": 15.0, "pb": 3.0},
}

_CACHE_TTL_SECS = 4 * 3600.0   # 4-hour TTL


# ── Output dataclass ───────────────────────────────────────────────────────────
@dataclass
class ValuationResult:
    ticker: str
    pe_trailing: float | None
    pe_forward: float | None
    ev_ebitda: float | None
    price_to_book: float | None
    valuation_score: float          # 0-10
    valuation_tier: str             # "cheap" | "fair" | "expensive" | "very_expensive"
    revision_score: int             # -7 to +5
    revision_signal: str            # "strong_upgrade" | "upgrade" | "neutral" | "downgrade" | "strong_downgrade"
    conviction_adj: int             # capped [-15, +10]
    reason_str: str


# ── Module-level TTL cache ────────────────────────────────────────────────────
@dataclass
class _CacheEntry:
    result: ValuationResult
    fetched_at: float   # time.monotonic()


_cache_lock = threading.Lock()
_val_cache: dict[str, _CacheEntry] = {}


# ── Internal helpers ──────────────────────────────────────────────────────────

def _score_metric(value: float | None, median: float) -> float:
    """Return score (0 / 0.5 / 1.5 / 2.5) for one valuation metric."""
    if value is None or value <= 0 or median <= 0:
        return 0.0   # missing data → no credit (conservative)
    ratio = value / median
    if ratio <= 0.75:
        return 2.5
    elif ratio <= 1.00:
        return 1.5
    elif ratio <= 1.50:
        return 0.5
    else:
        return 0.0


def _get_medians(sector: str | None) -> dict[str, float]:
    if not sector:
        return _SECTOR_MEDIANS["_default"]
    for key in _SECTOR_MEDIANS:
        if key != "_default" and key.lower() in (sector or "").lower():
            return _SECTOR_MEDIANS[key]
    return _SECTOR_MEDIANS["_default"]


def _tier_from_score(score: float) -> tuple[str, int]:
    """Returns (valuation_tier, valuation_conviction_adj)."""
    if score >= 7.0:
        return "cheap", +5
    elif score >= 5.0:
        return "fair", 0
    elif score >= 3.0:
        return "expensive", -5
    else:
        return "very_expensive", -10


def _revision_classify(net: int) -> tuple[str, int]:
    """Returns (revision_signal, revision_conviction_adj)."""
    if net > 10:
        return "strong_upgrade", +5
    elif net > 3:
        return "upgrade", +2
    elif net >= -3:
        return "neutral", 0
    elif net >= -10:
        return "downgrade", -3
    else:
        return "strong_downgrade", -7


def _safe_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _neutral_result(ticker: str, reason: str) -> ValuationResult:
    return ValuationResult(
        ticker=ticker,
        pe_trailing=None,
        pe_forward=None,
        ev_ebitda=None,
        price_to_book=None,
        valuation_score=5.0,
        valuation_tier="fair",
        revision_score=0,
        revision_signal="neutral",
        conviction_adj=0,
        reason_str=reason,
    )


# ── Main public function ──────────────────────────────────────────────────────

def get_valuation(ticker: str) -> ValuationResult:
    """
    Fetch and score fundamental valuation + earnings revision for *ticker*.

    Returns a neutral (conviction_adj=0) result for ETFs or on data failure.
    Uses a 4-hour TTL cache so repeated intraday scans don't hammer yfinance.
    """
    ticker_upper = ticker.upper()

    # ETF exemption — no fundamental analysis for index/commodity funds
    if ticker_upper in _ETF_TICKERS:
        return _neutral_result(ticker, f"{ticker_upper} is an ETF — fundamental valuation skipped")

    # Check cache
    now = time.monotonic()
    with _cache_lock:
        entry = _val_cache.get(ticker_upper)
        if entry and (now - entry.fetched_at) < _CACHE_TTL_SECS:
            logger.debug("Valuation cache hit for %s", ticker_upper)
            return entry.result

    result = _fetch_and_score(ticker_upper)

    with _cache_lock:
        _val_cache[ticker_upper] = _CacheEntry(result=result, fetched_at=now)

    return result


def _fetch_and_score(ticker: str) -> ValuationResult:
    """Download fundamentals from yfinance and compute scores."""
    try:
        yf_obj = yf.Ticker(ticker)
        info   = yf_obj.info or {}
    except Exception as exc:
        logger.debug("Valuation: yfinance.Ticker(%s) failed: %s", ticker, exc)
        return _neutral_result(ticker, f"yfinance data unavailable: {exc}")

    # ── Fundamental metrics ───────────────────────────────────────────────────
    def _safe_float(key: str) -> float | None:
        val = info.get(key)
        try:
            f = float(val)
            return f if f > 0 else None
        except (TypeError, ValueError):
            return None

    pe_trailing   = _safe_float("trailingPE")
    pe_forward    = _safe_float("forwardPE")
    ev_ebitda     = _safe_float("enterpriseToEbitda")
    price_to_book = _safe_float("priceToBook")
    sector        = info.get("sector")

    # Use forward PE when trailing PE is missing (e.g. pre-earnings loss year)
    pe_for_score = pe_trailing if pe_trailing is not None else pe_forward

    medians = _get_medians(sector)

    raw_score = (
        _score_metric(pe_for_score, medians["pe"]) +
        _score_metric(ev_ebitda,    medians["ev_ebitda"]) +
        _score_metric(price_to_book, medians["pb"])
    )
    # Scale from [0, 7.5] to [0, 10]
    valuation_score = round(min(raw_score / 7.5 * 10.0, 10.0), 2)
    valuation_tier, val_conviction = _tier_from_score(valuation_score)

    # ── Earnings revision momentum ────────────────────────────────────────────
    revision_score: int = 0
    try:
        eps_revisions = yf_obj.eps_revisions
        if eps_revisions is not None and not eps_revisions.empty:
            # Rows indexed by period: "0q", "+1q", etc.
            for period in ["0q", "+1q"]:
                if period in eps_revisions.index:
                    row = eps_revisions.loc[period]
                    up30   = _safe_int(row.get("upLast30days",   0))
                    down30 = _safe_int(row.get("downLast30days", 0))
                    revision_score += up30 - down30
        else:
            logger.debug("Valuation: no eps_revisions for %s", ticker)
    except Exception as exc:
        logger.debug("Valuation: eps_revisions fetch failed for %s: %s", ticker, exc)
        revision_score = 0

    revision_signal, rev_conviction = _revision_classify(revision_score)

    # ── Composite conviction adjustment ───────────────────────────────────────
    raw_adj = val_conviction + rev_conviction
    conviction_adj = max(-15, min(10, raw_adj))

    # ── Reason string ─────────────────────────────────────────────────────────
    pe_str  = f"{pe_trailing:.1f}" if pe_trailing  is not None else "n/a"
    fpe_str = f"{pe_forward:.1f}"  if pe_forward   is not None else "n/a"
    ev_str  = f"{ev_ebitda:.1f}"   if ev_ebitda    is not None else "n/a"
    pb_str  = f"{price_to_book:.2f}" if price_to_book is not None else "n/a"

    reason_str = (
        f"Sector={sector or 'unknown'} | "
        f"PE={pe_str} fPE={fpe_str} EV/EBITDA={ev_str} P/B={pb_str} | "
        f"val_score={valuation_score:.1f}/10 ({valuation_tier}) | "
        f"revision_net={revision_score:+d} ({revision_signal}) | "
        f"conviction_adj={conviction_adj:+d}"
    )

    logger.debug("Valuation [%s] %s", ticker, reason_str)

    return ValuationResult(
        ticker=ticker,
        pe_trailing=pe_trailing,
        pe_forward=pe_forward,
        ev_ebitda=ev_ebitda,
        price_to_book=price_to_book,
        valuation_score=valuation_score,
        valuation_tier=valuation_tier,
        revision_score=revision_score,
        revision_signal=revision_signal,
        conviction_adj=conviction_adj,
        reason_str=reason_str,
    )


class ValuationGate:
    """Thin service wrapper for use in AgoraSession.__init__."""

    def evaluate(self, ticker: str) -> ValuationResult:
        return get_valuation(ticker)

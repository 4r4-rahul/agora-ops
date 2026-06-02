"""
agora/ops/correlation_monitor.py — Portfolio Correlation Monitor

Computes 60-day rolling Pearson correlation between a candidate ticker and
existing open positions (same direction only).  Prevents inadvertently adding
a near-duplicate position that masquerades as diversification.

Thresholds (avg correlation across same-direction positions):
  < 0.50  → low    /  0 conviction adj  (genuinely uncorrelated)
  0.50-0.69 → medium / -10 adj         (moderate overlap — reduce size)
  0.70-0.84 → high  / -20 adj          (highly correlated — flag for review)
  ≥ 0.85  → block                       (essentially the same stock)

Special cases:
  • ETFs (SPY, QQQ, IWM, GLD, TLT, GDX, XL*) → always "low" risk
    (they are diversified by construction; correlation is expected & benign)
  • Returns series cached per ticker for the current minute (avoid re-fetching
    the same 60d of daily close data on every scan cycle tick).

References:
  • Litterman, "Modern Investment Management" — correlation in portfolio construction
  • DeMiguel et al., "Optimal Versus Naive Diversification" — JFE 2009
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import yfinance as yf
import numpy as np

logger = logging.getLogger(__name__)

# ── ETFs that are exempt from correlation blocking ────────────────────────────
_EXEMPT_ETFS: frozenset[str] = frozenset({
    "SPY", "QQQ", "IWM", "GLD", "TLT", "GDX",
    "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLRE", "XLC", "XLB",
    "PPLT", "SLV",
})

_CORR_THRESHOLDS = {
    "block":  0.85,
    "high":   0.70,
    "medium": 0.50,
}


# ── Output dataclass ───────────────────────────────────────────────────────────
@dataclass
class CorrelationResult:
    ticker: str
    existing_tickers: list[str]
    avg_correlation: float | None
    max_correlation: float | None
    most_correlated_ticker: str | None
    risk_level: str                  # "low" | "medium" | "high" | "block"
    block_reason: str
    conviction_adj: int              # 0, -10, or -20


def _neutral_result(ticker: str, reason: str) -> CorrelationResult:
    return CorrelationResult(
        ticker=ticker,
        existing_tickers=[],
        avg_correlation=None,
        max_correlation=None,
        most_correlated_ticker=None,
        risk_level="low",
        block_reason=reason,
        conviction_adj=0,
    )


# ── Return-series cache (per minute) ─────────────────────────────────────────
@dataclass
class _CacheEntry:
    series: np.ndarray | None
    fetched_at: float   # time.monotonic()


_cache_lock = threading.Lock()
_returns_cache: dict[str, _CacheEntry] = {}
_CACHE_TTL_SECS = 60.0   # invalidate after 1 minute


def _get_returns(ticker: str) -> np.ndarray | None:
    """
    Fetch 60-day daily close returns for *ticker* from yfinance.
    Results cached for _CACHE_TTL_SECS seconds (per-minute session cache).
    Returns None on failure.
    """
    now = time.monotonic()
    with _cache_lock:
        entry = _returns_cache.get(ticker)
        if entry and (now - entry.fetched_at) < _CACHE_TTL_SECS:
            return entry.series

    series: np.ndarray | None = None
    try:
        hist = yf.download(
            ticker,
            period="90d",
            interval="1d",
            auto_adjust=True,
            progress=False,
        )
        if hist is not None and len(hist) >= 5:
            closes = hist["Close"].squeeze().dropna()
            if len(closes) >= 5:
                series = closes.pct_change().dropna().values[-60:]
    except Exception as exc:
        logger.debug("Correlation: failed to fetch %s returns: %s", ticker, exc)
        series = None

    with _cache_lock:
        _returns_cache[ticker] = _CacheEntry(series=series, fetched_at=now)

    return series


def _pearson_corr(a: np.ndarray, b: np.ndarray) -> float | None:
    """Compute Pearson correlation between two 1-D arrays. Returns None on error."""
    n = min(len(a), len(b))
    if n < 10:
        return None
    try:
        a_trim = a[-n:]
        b_trim = b[-n:]
        corr = float(np.corrcoef(a_trim, b_trim)[0, 1])
        if np.isnan(corr):
            return None
        return corr
    except Exception:
        return None


def check_correlation(
    new_ticker: str,
    existing_positions: list[Any],   # list of OpenPosition objects or dicts
    direction: str = "bullish",       # "bullish" | "bearish" | "neutral"
) -> CorrelationResult:
    """
    Compute correlation between *new_ticker* and same-direction existing positions.

    Parameters
    ----------
    new_ticker:
        The candidate ticker being evaluated for entry.
    existing_positions:
        List of open position objects (OpenPosition) or dicts with 'ticker'/'direction'.
    direction:
        Intended direction of the new trade ('bullish' or 'bearish').

    Returns
    -------
    CorrelationResult with risk_level and conviction_adj.
    """
    ticker_upper = new_ticker.upper()

    # ETF exemption — always low risk
    if ticker_upper in _EXEMPT_ETFS:
        return _neutral_result(
            new_ticker,
            f"{ticker_upper} is a diversified ETF — correlation check exempt",
        )

    # Filter same-direction existing positions
    same_dir: list[str] = []
    for pos in existing_positions:
        if isinstance(pos, dict):
            pos_ticker = str(pos.get("ticker", "")).upper()
            pos_dir    = str(pos.get("direction", "neutral")).lower()
        else:
            pos_ticker = str(getattr(pos, "ticker", "")).upper()
            pos_dir    = str(getattr(pos, "direction", "neutral")).lower()

        if not pos_ticker or pos_ticker == ticker_upper:
            continue

        # Match direction: bullish↔bullish, bearish↔bearish; neutral is excluded
        if direction.lower() in ("bullish", "bearish") and pos_dir != direction.lower():
            continue

        same_dir.append(pos_ticker)

    if not same_dir:
        return _neutral_result(
            new_ticker,
            "No existing same-direction positions — correlation check not applicable",
        )

    # Fetch returns for the new ticker
    new_returns = _get_returns(ticker_upper)
    if new_returns is None or len(new_returns) < 10:
        return _neutral_result(
            new_ticker,
            "Insufficient return history for correlation — defaulting to neutral",
        )

    correlations: dict[str, float] = {}
    for pos_ticker in same_dir:
        pos_returns = _get_returns(pos_ticker)
        if pos_returns is None:
            continue
        corr = _pearson_corr(new_returns, pos_returns)
        if corr is not None:
            correlations[pos_ticker] = abs(corr)   # use absolute correlation

    if not correlations:
        return _neutral_result(
            new_ticker,
            "Could not compute correlations (data unavailable) — defaulting to neutral",
        )

    avg_corr = float(np.mean(list(correlations.values())))
    max_corr = float(max(correlations.values()))
    most_corr = max(correlations, key=lambda k: correlations[k])

    # Classify by average correlation
    if avg_corr >= _CORR_THRESHOLDS["block"]:
        risk_level    = "block"
        conviction_adj = 0   # hard block — caller should return early
        block_reason  = (
            f"BLOCKED: {ticker_upper} avg_corr={avg_corr:.2f} ≥ 0.85 vs "
            f"{list(correlations.keys())} — essentially same exposure"
        )
    elif avg_corr >= _CORR_THRESHOLDS["high"]:
        risk_level    = "high"
        conviction_adj = -20
        block_reason  = (
            f"High correlation: {ticker_upper} avg_corr={avg_corr:.2f} "
            f"(most correlated: {most_corr}={correlations[most_corr]:.2f})"
        )
    elif avg_corr >= _CORR_THRESHOLDS["medium"]:
        risk_level    = "medium"
        conviction_adj = -10
        block_reason  = (
            f"Medium correlation: {ticker_upper} avg_corr={avg_corr:.2f} "
            f"(most correlated: {most_corr}={correlations[most_corr]:.2f})"
        )
    else:
        risk_level    = "low"
        conviction_adj = 0
        block_reason  = (
            f"Low correlation: {ticker_upper} avg_corr={avg_corr:.2f} — safe to add"
        )

    logger.debug(
        "Correlation [%s] dir=%s peers=%s avg=%.2f max=%.2f level=%s adj=%+d",
        ticker_upper, direction, list(correlations.keys()),
        avg_corr, max_corr, risk_level, conviction_adj,
    )

    return CorrelationResult(
        ticker=new_ticker,
        existing_tickers=list(correlations.keys()),
        avg_correlation=avg_corr,
        max_correlation=max_corr,
        most_correlated_ticker=most_corr,
        risk_level=risk_level,
        block_reason=block_reason,
        conviction_adj=conviction_adj,
    )


class CorrelationMonitor:
    """Thin service wrapper for use in AgoraSession.__init__."""

    def check(
        self,
        new_ticker: str,
        existing_positions: list[Any],
        direction: str = "bullish",
    ) -> CorrelationResult:
        return check_correlation(new_ticker, existing_positions, direction)

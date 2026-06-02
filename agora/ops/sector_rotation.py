"""
agora/ops/sector_rotation.py — GICS Sector Rotation Monitor

Computes 4-week and 13-week relative strength of each GICS sector ETF
versus SPY, blends into a composite rank (1=strongest, 11=weakest),
and maps individual stock tickers to their sector for conviction adjustment.

Cache: refreshed once per calendar day (first call after midnight).

References:
  • GICS — Global Industry Classification Standard (MSCI / S&P)
  • O'Shaughnessy, "What Works on Wall Street" — relative strength ranking ch.12
  • Zweig, "Winning on Wall Street" — sector rotation timing
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

import yfinance as yf

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# ── GICS sector ETF universe ───────────────────────────────────────────────────
_SECTOR_ETFS: dict[str, str] = {
    "XLK":  "Technology",
    "XLF":  "Financials",
    "XLE":  "Energy",
    "XLV":  "Healthcare",
    "XLI":  "Industrials",
    "XLY":  "Consumer Discretionary",
    "XLP":  "Consumer Staples",
    "XLU":  "Utilities",
    "XLRE": "Real Estate",
    "XLC":  "Communication Services",
    "XLB":  "Materials",
}

# ── Ticker → sector ETF mapping ───────────────────────────────────────────────
# None means: no sector adjustment (broad market ETFs / benchmarks)
_TICKER_TO_ETF: dict[str, str | None] = {
    # Technology (XLK)
    "AAPL": "XLK", "MSFT": "XLK", "NVDA": "XLK", "AMD": "XLK",
    "MU":   "XLK", "INTC": "XLK", "TSM":  "XLK", "AVGO": "XLK",
    "QCOM": "XLK", "LRCX": "XLK", "ASML": "XLK", "TXN":  "XLK",
    "SMTC": "XLK", "KEYS": "XLK", "ONTO": "XLK",
    # High-growth / tech-adjacent mapped to XLK
    "MSTR": "XLK", "IREN": "XLK", "CIFR": "XLK",
    "OKLO": "XLK", "ASTS": "XLK", "RKLB": "XLK",
    # Communication Services (XLC)
    "META":  "XLC", "GOOGL": "XLC", "GOOG": "XLC",
    "NFLX":  "XLC", "PLTR":  "XLC",
    # Consumer Discretionary (XLY)
    "AMZN": "XLY", "TSLA": "XLY", "HD": "XLY", "NKE": "XLY", "BJ": "XLY",
    # Consumer Staples (XLP)
    "WMT": "XLP", "PG": "XLP", "KO": "XLP", "PEP": "XLP",
    # Financials (XLF)
    "JPM": "XLF", "BAC": "XLF", "GS": "XLF", "MS": "XLF",
    "SCHW": "XLF", "V": "XLF", "MA": "XLF", "BRK.B": "XLF",
    # Healthcare (XLV)
    "UNH": "XLV", "JNJ": "XLV", "PFE": "XLV",
    "ABBV": "XLV", "LLY": "XLV", "MRNA": "XLV",
    # Energy (XLE)
    "XOM": "XLE", "CVX": "XLE", "SLB": "XLE",
    # Industrials (XLI)
    "CAT":  "XLI", "HON": "XLI", "BA": "XLI",
    "GE":   "XLI", "POWL": "XLI", "KTOS": "XLI",
    # Materials (XLB)
    "PPLT": "XLB", "SLV": "XLB",
    # Broad market / benchmarks — no sector adjustment
    "SPY": None, "QQQ": None, "IWM": None,
    "GLD": None, "TLT": None, "GDX": None,
}

# ── Output dataclass ───────────────────────────────────────────────────────────
@dataclass
class SectorSignal:
    ticker: str
    sector_etf: str | None          # e.g. "XLK", None for broad market
    sector_name: str                 # human-readable sector name
    rs_4w: float | None             # 4-week relative return vs SPY
    rs_13w: float | None            # 13-week relative return vs SPY
    sector_rank: int | None         # 1 (best) to 11 (worst); None if unknown
    conviction_adj: int             # +5 / 0 / -5
    reason_str: str


# ── Module-level cache ─────────────────────────────────────────────────────────
_lock = threading.Lock()
_cache_date: date | None = None
_cache: dict[str, dict] = {}   # etf → {"rs_4w": float, "rs_13w": float, "rank": int}


def _fetch_returns() -> dict[str, dict]:
    """
    Download 65 trading-day price history for SPY + all sector ETFs.
    Returns mapping etf → {"rs_4w": float, "rs_13w": float} or empty dict on error.
    """
    tickers_to_fetch = ["SPY"] + list(_SECTOR_ETFS.keys())
    result: dict[str, dict] = {}

    try:
        raw = yf.download(
            tickers_to_fetch,
            period="100d",
            interval="1d",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
        # yfinance returns MultiIndex columns when multiple tickers
        if hasattr(raw.columns, "levels"):
            close = raw["Close"]
        else:
            close = raw[["Close"]]

        if "SPY" not in close.columns:
            logger.debug("Sector rotation: SPY data unavailable — skipping")
            return {}

        spy_close = close["SPY"].dropna()
        if len(spy_close) < 22:
            logger.debug("Sector rotation: insufficient SPY history (%d rows)", len(spy_close))
            return {}

        spy_ret_4w  = spy_close.iloc[-1] / spy_close.iloc[-21] - 1.0 if len(spy_close) >= 21 else None
        spy_ret_13w = spy_close.iloc[-1] / spy_close.iloc[-66] - 1.0 if len(spy_close) >= 66 else None

        scores: dict[str, float] = {}
        for etf in _SECTOR_ETFS:
            if etf not in close.columns:
                logger.debug("Sector rotation: %s data unavailable", etf)
                continue
            etf_close = close[etf].dropna()
            if len(etf_close) < 22:
                logger.debug("Sector rotation: %s insufficient history (%d rows)", etf, len(etf_close))
                continue

            etf_ret_4w  = etf_close.iloc[-1] / etf_close.iloc[-21] - 1.0 if len(etf_close) >= 21 else None
            etf_ret_13w = etf_close.iloc[-1] / etf_close.iloc[-66] - 1.0 if len(etf_close) >= 66 else None

            rs_4w  = (etf_ret_4w  - spy_ret_4w)  if (etf_ret_4w  is not None and spy_ret_4w  is not None) else None
            rs_13w = (etf_ret_13w - spy_ret_13w) if (etf_ret_13w is not None and spy_ret_13w is not None) else None

            result[etf] = {"rs_4w": rs_4w, "rs_13w": rs_13w}

            # Composite score for ranking: 4w×0.4 + 13w×0.6 (favour medium-term trend)
            if rs_4w is not None and rs_13w is not None:
                scores[etf] = rs_4w * 0.4 + rs_13w * 0.6
            elif rs_4w is not None:
                scores[etf] = rs_4w
            elif rs_13w is not None:
                scores[etf] = rs_13w

        # Rank — higher composite score = better RS = lower rank number
        ranked = sorted(scores.keys(), key=lambda e: scores[e], reverse=True)
        for rank, etf in enumerate(ranked, start=1):
            result[etf]["rank"] = rank

    except Exception as exc:
        logger.debug("Sector rotation fetch error: %s", exc)

    return result


def _ensure_cache() -> None:
    """Refresh cache if it is from a prior calendar day."""
    today = datetime.now(tz=ET).date()
    with _lock:
        global _cache_date, _cache
        if _cache_date == today and _cache:
            return
        logger.info("Sector rotation: refreshing daily RS cache (date=%s)", today)
        new_data = _fetch_returns()
        if new_data:
            _cache = new_data
            _cache_date = today
        elif not _cache:
            # First load failed: keep empty cache, will retry next call
            logger.warning("Sector rotation: initial data fetch failed — using neutral defaults")


def get_sector_signal(ticker: str) -> SectorSignal:
    """
    Return sector relative-strength signal for *ticker*.

    conviction_adj:
      top-3 sectors     → +5
      mid-tier (4-8)    →  0
      bottom-3 sectors  → -5
      unknown / ETF     →  0
    """
    _ensure_cache()

    sector_etf = _TICKER_TO_ETF.get(ticker.upper())

    # Ticker not in map — try looking up sector ETFs themselves
    if ticker.upper() in _SECTOR_ETFS:
        sector_etf = ticker.upper()

    if sector_etf is None:
        # Broad market ETF or unmapped ticker — no adjustment
        return SectorSignal(
            ticker=ticker,
            sector_etf=None,
            sector_name="Broad Market",
            rs_4w=None,
            rs_13w=None,
            sector_rank=None,
            conviction_adj=0,
            reason_str="Broad market ETF or unmapped ticker — no sector adjustment",
        )

    with _lock:
        data = _cache.get(sector_etf)

    sector_name = _SECTOR_ETFS.get(sector_etf, sector_etf)

    if not data:
        return SectorSignal(
            ticker=ticker,
            sector_etf=sector_etf,
            sector_name=sector_name,
            rs_4w=None,
            rs_13w=None,
            sector_rank=None,
            conviction_adj=0,
            reason_str=f"{sector_etf} data unavailable — neutral adjustment",
        )

    rs_4w  = data.get("rs_4w")
    rs_13w = data.get("rs_13w")
    rank   = data.get("rank")

    if rank is None:
        conviction_adj = 0
        qualifier = "no rank data"
    elif rank <= 3:
        conviction_adj = +5
        qualifier = f"top-tier rank #{rank}"
    elif rank <= 8:
        conviction_adj = 0
        qualifier = f"mid-tier rank #{rank}"
    else:
        conviction_adj = -5
        qualifier = f"lagging rank #{rank}"

    rs_4w_str  = f"{rs_4w:+.1%}"  if rs_4w  is not None else "n/a"
    rs_13w_str = f"{rs_13w:+.1%}" if rs_13w is not None else "n/a"

    reason_str = (
        f"{sector_name} ({sector_etf}) {qualifier} | "
        f"RS 4w={rs_4w_str} 13w={rs_13w_str}"
    )

    return SectorSignal(
        ticker=ticker,
        sector_etf=sector_etf,
        sector_name=sector_name,
        rs_4w=rs_4w,
        rs_13w=rs_13w,
        sector_rank=rank,
        conviction_adj=conviction_adj,
        reason_str=reason_str,
    )


class SectorRotationMonitor:
    """Thin service wrapper for use in AgoraSession.__init__."""

    def get_signal(self, ticker: str) -> SectorSignal:
        return get_sector_signal(ticker)

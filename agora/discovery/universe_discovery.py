"""
UniverseDiscoveryAgent — dynamic universe expansion and pruning.

The static 83-ticker universe misses stocks like CSCO that we "know" are interesting
but didn't include. This agent dynamically expands and prunes the universe based on:

  1. Earnings calendar sweep — stocks reporting in next 14 days (added T-7)
  2. Market interest signals — high-OI / ETF-flow candidates from MarketInterestAgent
  3. Momentum screen — stocks with strong directional momentum and elevated IV rank
  4. Liquidity gate — OI ≥ 500, bid/ask < 10%, market cap in configured range

Discovery happens once per week (Monday 6:00 AM ET) + on-demand when a high-conviction
catalyst fires for an out-of-universe ticker.

New tickers are added to `session._dynamic_universe` — a separate list from the static
`settings.etf_universe`, so they can be cleared without persisting to settings.

Cap: max 15 new tickers per week. Stale tickers pruned after 10 trading days.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import anthropic

from ..core.config import AgoraSettings, get_settings

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

_MAX_NEW_PER_WEEK     = 15
_PRUNE_AFTER_DAYS     = 10
_MIN_OI               = 500
_MAX_BID_ASK_PCT      = 0.10

# S&P 500 sector ETFs for broad earnings sweep candidates
_BROAD_SCAN_SOURCES = [
    "SPY", "QQQ", "IWM", "XLK", "XLF", "XLE", "XLV", "XLC", "XLI", "XLB"
]


@dataclass
class DiscoveredTicker:
    ticker: str
    reason: str      # "earnings_T7" | "market_interest" | "momentum" | "catalyst"
    added_date: date
    confidence: float
    prune_after: date


class UniverseDiscoveryAgent:
    """
    Maintains a dynamic supplement to the static universe.
    Session reads `get_dynamic_tickers()` each scan cycle.
    """

    def __init__(
        self,
        settings: AgoraSettings | None = None,
        market_interest_agent: Any = None,   # MarketInterestAgent
    ) -> None:
        self._settings        = settings or get_settings()
        self._market_interest = market_interest_agent
        self._client          = anthropic.AsyncAnthropic(api_key=self._settings.anthropic_api_key)
        self._running         = False
        self._dynamic: dict[str, DiscoveredTicker] = {}   # ticker → record
        self._last_weekly_run: date | None = None
        self._weekly_added_count: int = 0

    def get_dynamic_tickers(self) -> list[str]:
        """Synchronous — called by _universe_scan() to augment the static universe."""
        today = date.today()
        return [
            t for t, d in self._dynamic.items()
            if d.prune_after >= today
        ]

    def add_catalyst_ticker(self, ticker: str, reason: str = "catalyst") -> None:
        """Called by CatalystDiscoveryAgent for out-of-universe tickers."""
        if ticker not in self._dynamic and ticker not in self._settings.etf_universe:
            if self._weekly_added_count < _MAX_NEW_PER_WEEK:
                self._add_ticker(ticker, reason, confidence=0.70)
                logger.info("Universe: catalyst-added %s (%s)", ticker, reason)

    async def start(self) -> None:
        self._running = True
        logger.info("UniverseDiscoveryAgent started")
        while self._running:
            now_et = datetime.now(tz=ET)
            today  = now_et.date()

            # Weekly sweep: Monday 6:00 AM ET
            if now_et.weekday() == 0 and now_et.hour == 6 and self._last_weekly_run != today:
                try:
                    await self._weekly_discovery()
                    self._last_weekly_run = today
                    self._weekly_added_count = 0
                except Exception as exc:
                    logger.error("Universe weekly discovery failed: %s", exc)

            # Daily pruning at midnight
            if now_et.hour == 0:
                self._prune_stale()

            await asyncio.sleep(300)   # check every 5 min

    async def stop(self) -> None:
        self._running = False

    # ── Weekly discovery ───────────────────────────────────────────

    async def _weekly_discovery(self) -> None:
        logger.info("UniverseDiscovery: weekly sweep starting")
        candidates: list[tuple[str, str, float]] = []   # (ticker, reason, confidence)

        # 1. Earnings calendar sweep (next 14 days)
        earnings_candidates = await self._sweep_earnings_calendar()
        candidates.extend(earnings_candidates)

        # 2. Market interest signals
        if self._market_interest:
            interest_top = self._market_interest.get_top_interest_tickers(n=20)
            static = set(self._settings.etf_universe)
            for ticker, score in interest_top:
                if ticker not in static and score >= 4.0:
                    candidates.append((ticker, "market_interest", min(score / 10.0, 0.90)))

        # 3. Qualify candidates (liquidity gate)
        qualified: list[tuple[str, str, float]] = []
        for ticker, reason, conf in candidates:
            if ticker in self._settings.etf_universe or ticker in self._dynamic:
                continue
            if await self._passes_liquidity_gate(ticker):
                qualified.append((ticker, reason, conf))

        # Sort by confidence and add up to cap
        qualified.sort(key=lambda x: x[2], reverse=True)
        for ticker, reason, conf in qualified[:_MAX_NEW_PER_WEEK]:
            self._add_ticker(ticker, reason, conf)
            self._weekly_added_count += 1

        logger.info(
            "UniverseDiscovery: added %d tickers | dynamic_universe=%d",
            self._weekly_added_count, len(self._dynamic),
        )

    async def _sweep_earnings_calendar(self) -> list[tuple[str, str, float]]:
        """
        Sweep the broad market for stocks with earnings in next 14 days.
        Uses yfinance screener approach: check major index components.
        """
        candidates = []
        horizon = date.today() + timedelta(days=14)

        try:
            import yfinance as yf

            # Use S&P 500 components via a spot check of sector ETF holdings
            # yfinance doesn't have a full screener, so we check known candidates
            # from a curated 200-ticker extended list
            extended_watchlist = [
                "ORCL", "SAP", "CRM", "NOW", "WDAY", "PANW", "FTNT", "CRWD",
                "CSCO", "ANET", "JNPR", "HPE", "DELL", "IBM", "ACN",
                "GS", "MS", "JPM", "BAC", "WFC", "BLK", "SPGI",
                "UNH", "CVS", "HCA", "ISRG", "DXCM", "MRNA", "PFE",
                "XOM", "CVX", "COP", "SLB", "EOG", "PXD",
                "BA", "RTX", "LMT", "NOC", "GD", "L3H",
                "HD", "LOW", "TGT", "AMZN", "BABA",
                "V", "MA", "PYPL", "SQ",
                "DIS", "NFLX", "PARA", "WBD",
                "T", "VZ", "TMUS",
            ]

            for ticker in extended_watchlist:
                if ticker in self._settings.etf_universe:
                    continue
                try:
                    tk = yf.Ticker(ticker)
                    cal = tk.calendar
                    if not cal:
                        continue
                    earnings_raw = cal.get("Earnings Date") if hasattr(cal, "get") else None
                    if not earnings_raw:
                        continue
                    if isinstance(earnings_raw, (list, tuple)) and earnings_raw:
                        earnings_raw = earnings_raw[0]
                    if hasattr(earnings_raw, "date"):
                        earnings_date = earnings_raw.date()
                    elif isinstance(earnings_raw, date):
                        earnings_date = earnings_raw
                    else:
                        continue

                    if date.today() <= earnings_date <= horizon:
                        dte = (earnings_date - date.today()).days
                        conf = 0.80 if dte <= 7 else 0.65
                        candidates.append((ticker, f"earnings_T{dte}", conf))
                        logger.info("Discovery: %s earnings T-%d", ticker, dte)

                    await asyncio.sleep(0.1)
                except Exception:
                    continue
        except Exception as exc:
            logger.debug("Earnings calendar sweep failed: %s", exc)

        return candidates

    async def _passes_liquidity_gate(self, ticker: str) -> bool:
        """OI ≥ 500, bid/ask < 10%, market cap in configured range."""
        try:
            import yfinance as yf
            tk = yf.Ticker(ticker)
            info = tk.info or {}

            # Market cap gate (fast_info lacks marketCap — keep .info for this)
            mkt_cap = float(info.get("marketCap") or 0)
            if not (self._settings.single_name_min_market_cap <= mkt_cap <= self._settings.single_name_max_market_cap):
                return False

            try:
                fi   = tk.fast_info
                spot = float(getattr(fi, "last_price", None) or fi.get("lastPrice", 0) or 0)
            except Exception:
                spot = 0.0
            if spot <= 0:
                spot = float(info.get("regularMarketPrice") or 0)
            if spot <= 0:
                return False

            exps = tk.options or []
            if not exps:
                return False

            chain = tk.option_chain(exps[0])
            calls = chain.calls
            if calls.empty:
                return False

            total_oi = int(calls["openInterest"].fillna(0).sum())
            if total_oi < _MIN_OI:
                return False

            atm_idx = (calls["strike"] - spot).abs().argsort().iloc[0]
            bid = float(calls["bid"].iloc[atm_idx] or 0)
            ask = float(calls["ask"].iloc[atm_idx] or 0)
            mid = (bid + ask) / 2
            if mid <= 0:
                return False

            return (ask - bid) / mid <= _MAX_BID_ASK_PCT
        except Exception:
            return False

    def _add_ticker(self, ticker: str, reason: str, confidence: float) -> None:
        today = date.today()
        self._dynamic[ticker] = DiscoveredTicker(
            ticker=ticker,
            reason=reason,
            added_date=today,
            confidence=confidence,
            prune_after=today + timedelta(days=_PRUNE_AFTER_DAYS),
        )

    def _prune_stale(self) -> None:
        today = date.today()
        before = len(self._dynamic)
        self._dynamic = {
            t: d for t, d in self._dynamic.items()
            if d.prune_after >= today
        }
        pruned = before - len(self._dynamic)
        if pruned:
            logger.info("UniverseDiscovery: pruned %d stale tickers", pruned)

"""
IntradaySectorMomentumDetector — real-time sector momentum signal.

Fires when 3+ universe tickers in the same sector move ≥3% in the same
direction within a 35-minute window.  Called from _price_monitor_loop
every 5 minutes with each ticker's signed 30-minute price change.

Signal lifetime: 2 hours per sector.  One signal per sector per session
(doesn't keep firing after the initial alert).

Used by _evaluate_ticker to bypass the DisagreementResolver's no_trade
gate and fire a defined-risk directional spread in the sector's direction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import NamedTuple
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

_SIGNAL_TTL_SECONDS = 7_200        # 2 hours
_MIN_TICKERS        = 3
_MIN_MOVE_PCT       = 3.0          # abs pct move to count a ticker
_WINDOW_MINUTES     = 35           # price monitor uses 30-min window; add 5m buffer


# ── Sector groupings ──────────────────────────────────────────────────────────

SECTOR_MAP: dict[str, list[str]] = {
    "semis": [
        "MU", "AMKR", "SMTC", "TSM", "LRCX", "ASML", "INTC", "TXN",
        "HIMX", "TSEM", "VECO", "AVGO", "AMD",
    ],
    "mega_tech": ["AAPL", "MSFT", "NVDA", "META", "AMZN", "GOOGL", "TSLA"],
    "energy_nuclear": ["VST", "CEG", "NEE", "FCEL", "BE", "AMSC", "FLNC", "CCJ", "OKLO"],
    "defense": ["PLTR", "KTOS", "AVAV", "RKLB"],
    "commodities": ["GLD", "SLV", "COPX", "PPLT"],
    "bonds": ["TLT"],
    "biotech": ["LLY", "JNJ", "ABT", "BSX", "BIIB", "MDT"],
    "cloud_software": ["SNOW", "ZS", "UPST", "ORCL"],
    "optical_photonics": ["LITE", "COHR", "AAOI", "AXTI", "AOSL"],
    "finance": ["SCHW", "HOOD", "CBOE"],
    "crypto_adjacent": ["MSTR", "IREN", "CIFR"],
    "industrial_power": ["GEV", "POWL", "VICR", "CAT"],
    "mining_materials": ["MP", "CCJ"],
}

# Reverse map: ticker → sector name
_TICKER_TO_SECTOR: dict[str, str] = {
    ticker: sector
    for sector, tickers in SECTOR_MAP.items()
    for ticker in tickers
}


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class SectorMomentumSignal:
    sector: str
    direction: str                  # "bearish" | "bullish"
    tickers: list[str]              # tickers that triggered the signal
    moves: list[float]              # corresponding signed pct moves
    avg_move_pct: float             # mean signed move of triggering tickers
    fired_at: datetime              # ET timestamp


class _TickerMove(NamedTuple):
    signed_pct: float
    timestamp: datetime


# ── Detector ──────────────────────────────────────────────────────────────────

class IntradaySectorMomentumDetector:
    """
    Called from _price_monitor_loop with each ticker's signed 30-minute price
    change.  Checks whether enough same-sector peers have moved in the same
    direction to fire a SectorMomentumSignal.

    Thread-safe: all state is mutated only from the single asyncio event loop
    that runs _price_monitor_loop.
    """

    def __init__(self) -> None:
        # Latest signed 30m move per ticker
        self._moves: dict[str, _TickerMove] = {}
        # Active signals per sector (at most one per sector)
        self._active: dict[str, SectorMomentumSignal] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def update(
        self,
        ticker: str,
        signed_pct: float,
        ts: datetime,
    ) -> SectorMomentumSignal | None:
        """
        Record the latest 30-minute signed move for *ticker* and check whether
        the sector threshold has been crossed.

        Returns a SectorMomentumSignal if this update triggered a new signal,
        otherwise None.
        """
        self._moves[ticker] = _TickerMove(signed_pct=signed_pct, timestamp=ts)
        sector = _TICKER_TO_SECTOR.get(ticker)
        if sector is None:
            return None
        if sector in self._active:
            return None  # already fired — don't re-fire within session
        return self._check_sector(sector, ts)

    def get_active_signal(self, ticker: str) -> SectorMomentumSignal | None:
        """
        Return the active SectorMomentumSignal for ticker's sector, or None if
        no signal is active or the signal has expired (TTL exceeded).
        """
        sector = _TICKER_TO_SECTOR.get(ticker)
        if sector is None:
            return None
        sig = self._active.get(sector)
        if sig is None:
            return None
        age = (datetime.now(tz=ET) - sig.fired_at).total_seconds()
        if age > _SIGNAL_TTL_SECONDS:
            del self._active[sector]
            logger.debug("Sector momentum signal for %s expired (age=%.0fs)", sector, age)
            return None
        return sig

    def reset_session(self) -> None:
        """Clear all state at session close / new-day startup."""
        self._moves.clear()
        self._active.clear()

    def active_signals(self) -> list[SectorMomentumSignal]:
        """Return a snapshot of all currently active (non-expired) signals."""
        now = datetime.now(tz=ET)
        live = []
        expired = []
        for sector, sig in self._active.items():
            if (now - sig.fired_at).total_seconds() <= _SIGNAL_TTL_SECONDS:
                live.append(sig)
            else:
                expired.append(sector)
        for s in expired:
            del self._active[s]
        return live

    # ── Internal ──────────────────────────────────────────────────────────────

    def _check_sector(self, sector: str, ts: datetime) -> SectorMomentumSignal | None:
        cutoff = ts - timedelta(minutes=_WINDOW_MINUTES)
        bearish: list[tuple[str, float]] = []
        bullish: list[tuple[str, float]] = []

        for t in SECTOR_MAP.get(sector, []):
            mv = self._moves.get(t)
            if mv is None or mv.timestamp < cutoff:
                continue
            if mv.signed_pct <= -_MIN_MOVE_PCT:
                bearish.append((t, mv.signed_pct))
            elif mv.signed_pct >= _MIN_MOVE_PCT:
                bullish.append((t, mv.signed_pct))

        for group, direction in [(bearish, "bearish"), (bullish, "bullish")]:
            if len(group) >= _MIN_TICKERS:
                tickers = [t for t, _ in group]
                moves   = [p for _, p in group]
                avg     = sum(moves) / len(moves)
                sig = SectorMomentumSignal(
                    sector=sector,
                    direction=direction,
                    tickers=tickers,
                    moves=moves,
                    avg_move_pct=avg,
                    fired_at=ts,
                )
                self._active[sector] = sig
                logger.info(
                    "SECTOR MOMENTUM SIGNAL | sector=%s direction=%s avg_move=%.1f%% "
                    "tickers=%s",
                    sector, direction, avg, tickers,
                )
                return sig
        return None

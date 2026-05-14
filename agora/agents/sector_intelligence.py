"""
SectorIntelligenceAgent — peer earnings read-through regression.

The CSCO lesson: sector peers that already reported give us a quantitative
prediction for the current ticker's earnings magnitude AND direction.

This agent maintains a live sector intelligence map updated every morning:
  - Which peers in each sector have already reported this quarter?
  - What was their beat/miss magnitude (actual vs estimate)?
  - What was their post-earnings stock move?
  - Regression: given peer beat patterns, predict current ticker's EPS beat prob.

Provides `get_intelligence(ticker)` → dict used by:
  - EarningsCalendarAgent (pre-earnings positioning)
  - OptionsEdgeAgent (magnitude prediction → strike selection)
  - CEOAgent (sector health in reports)

Data sources: yfinance earnings history, analyst estimates.
Claude: Opus for regression synthesis, Haiku for data extraction.
"""

from __future__ import annotations

import asyncio
import logging
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import anthropic

from ..core.config import AgoraSettings, get_settings

logger = logging.getLogger(__name__)

# Sector groupings: ticker → sector
_TICKER_SECTOR: dict[str, str] = {
    # AI Infrastructure / Networking
    "CSCO": "ai_networking", "AVGO": "ai_networking", "ANET": "ai_networking",
    "JNPR": "ai_networking", "NOK": "ai_networking",
    # Mega-cap tech
    "AAPL": "mega_tech", "MSFT": "mega_tech", "GOOGL": "mega_tech",
    "META": "mega_tech", "AMZN": "mega_tech",
    # Semiconductors
    "NVDA": "semiconductors", "AMD": "semiconductors", "INTC": "semiconductors",
    "TSM": "semiconductors", "MU": "semiconductors", "TXN": "semiconductors",
    "LRCX": "semiconductors", "ASML": "semiconductors", "AVGO": "semiconductors",
    # Cloud / software
    "SNOW": "cloud_software", "ZS": "cloud_software", "ORCL": "cloud_software",
    "MSFT": "cloud_software",
    # Defense
    "PLTR": "defense", "KTOS": "defense", "AVAV": "defense", "RKLB": "defense",
    # Energy / power
    "VST": "power", "CEG": "power", "NEE": "power", "CCJ": "power",
    # Finance
    "SCHW": "finance", "HOOD": "finance", "CBOE": "finance",
    # Healthcare
    "LLY": "healthcare", "JNJ": "healthcare", "ABT": "healthcare",
    "BSX": "healthcare", "BIIB": "healthcare", "MDT": "healthcare",
}


@dataclass
class PeerEarningsRecord:
    ticker: str
    report_date: date
    eps_actual: float | None
    eps_estimate: float | None
    beat_pct: float | None        # (actual - estimate) / |estimate|
    stock_move_pct: float | None  # next-day price move


@dataclass
class SectorIntelligence:
    sector: str
    ticker: str
    peers_reported_this_quarter: list[PeerEarningsRecord] = field(default_factory=list)
    avg_peer_beat_pct: float | None = None       # avg EPS beat % across reported peers
    avg_peer_stock_move: float | None = None     # avg stock move after peer earnings
    beat_rate: float | None = None               # fraction of peers that beat estimates
    read_through_direction: str = "neutral"      # "bullish" | "bearish" | "neutral"
    read_through_confidence: float = 0.5
    regression_note: str = ""                    # Claude's narrative synthesis
    updated_at: datetime = field(default_factory=datetime.utcnow)


class SectorIntelligenceAgent:
    """
    Maintains a live sector intelligence map.
    Updated once per morning; provides synchronous `get_intelligence()` for intraday use.
    """

    def __init__(self, settings: AgoraSettings | None = None) -> None:
        self._settings = settings or get_settings()
        self._client   = anthropic.AsyncAnthropic(api_key=self._settings.anthropic_api_key)
        self._running  = False
        self._cache: dict[str, SectorIntelligence] = {}   # ticker → intelligence
        self._last_update_date: date | None = None

    def get_intelligence(self, ticker: str) -> SectorIntelligence | None:
        """Synchronous accessor for use in _evaluate_ticker() path."""
        return self._cache.get(ticker)

    def get_cached_count(self) -> int:
        """Return number of tickers with cached sector intelligence."""
        return len(self._cache)

    def get_top_sectors(self, n: int = 5) -> list[dict]:
        """Return top n sectors by average sentiment score for CIO briefing."""
        by_sector: dict[str, list[float]] = {}
        for intel in self._cache.values():
            sector = getattr(intel, "sector", "unknown")
            score = getattr(intel, "sentiment_score", 0.0)
            by_sector.setdefault(sector, []).append(score)
        ranked = sorted(
            [{"sector": s, "avg_score": round(sum(v)/len(v), 2), "ticker_count": len(v)}
             for s, v in by_sector.items()],
            key=lambda x: x["avg_score"],
            reverse=True,
        )
        return ranked[:n]

    async def start(self) -> None:
        self._running = True
        logger.info("SectorIntelligenceAgent started")
        from zoneinfo import ZoneInfo
        # Warm up immediately on startup if cache is empty — covers restarts mid-day.
        # Without this, sector intelligence sits empty until the 6 AM ET window fires.
        if not self._cache:
            logger.info("SectorIntelligenceAgent: cold cache — running startup warm-up")
            try:
                await self._update_all_sectors()
                from datetime import timezone as _tz
                self._last_update_date = datetime.now(tz=ZoneInfo("America/New_York")).date()
            except Exception as exc:
                logger.error("Sector intelligence startup warm-up failed: %s", exc)
        while self._running:
            now = datetime.utcnow()
            today = now.date()
            # Re-fetch every morning at 6 AM ET to catch new peer earnings reports
            et_now = datetime.now(tz=ZoneInfo("America/New_York"))
            if et_now.hour == 6 and self._last_update_date != today:
                try:
                    await self._update_all_sectors()
                    self._last_update_date = today
                except Exception as exc:
                    logger.error("Sector intelligence update failed: %s", exc)
            await asyncio.sleep(300)   # check every 5 min

    async def stop(self) -> None:
        self._running = False

    # ── Update cycle ───────────────────────────────────────────────

    async def _update_all_sectors(self) -> None:
        """Refresh sector intelligence for all tracked tickers."""
        sectors_seen: set[str] = set()
        sector_tickers: dict[str, list[str]] = {}

        for ticker, sector in _TICKER_SECTOR.items():
            sector_tickers.setdefault(sector, []).append(ticker)

        logger.info("Sector intelligence update: %d sectors", len(sector_tickers))

        tasks = [
            self._update_sector(sector, tickers)
            for sector, tickers in sector_tickers.items()
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _update_sector(self, sector: str, tickers: list[str]) -> None:
        """Fetch peer earnings records for all tickers in a sector."""
        quarter_start = self._current_quarter_start()
        peer_records: list[PeerEarningsRecord] = []

        for ticker in tickers:
            try:
                records = await self._fetch_earnings_records(ticker, since=quarter_start)
                peer_records.extend(records)
            except Exception:
                continue

        if not peer_records:
            return

        # Compute aggregates
        beat_pcts  = [r.beat_pct for r in peer_records if r.beat_pct is not None]
        stock_moves = [r.stock_move_pct for r in peer_records if r.stock_move_pct is not None]

        avg_beat    = statistics.mean(beat_pcts)  if beat_pcts  else None
        avg_move    = statistics.mean(stock_moves) if stock_moves else None
        beat_rate   = (sum(1 for b in beat_pcts if b > 0) / len(beat_pcts)) if beat_pcts else None

        # Direction from peer moves
        direction = "neutral"
        confidence = 0.5
        if avg_move is not None and len(stock_moves) >= 2:
            if avg_move > 0.03:
                direction  = "bullish"
                confidence = min(0.5 + avg_move * 3, 0.85)
            elif avg_move < -0.03:
                direction  = "bearish"
                confidence = min(0.5 + abs(avg_move) * 3, 0.85)

        # Claude synthesis for non-obvious sectors
        note = ""
        if len(peer_records) >= 2:
            note = await self._synthesize_read_through(sector, peer_records, avg_beat, avg_move)

        # Write intelligence for each ticker in the sector
        for ticker in tickers:
            # Peer records = all other tickers in this sector
            peers_for_ticker = [r for r in peer_records if r.ticker != ticker]
            intel = SectorIntelligence(
                sector=sector,
                ticker=ticker,
                peers_reported_this_quarter=peers_for_ticker,
                avg_peer_beat_pct=avg_beat,
                avg_peer_stock_move=avg_move,
                beat_rate=beat_rate,
                read_through_direction=direction,
                read_through_confidence=confidence,
                regression_note=note,
            )
            self._cache[ticker] = intel

        logger.info(
            "Sector %s: %d peers | avg_beat=%.1f%% | avg_move=%.1f%% | dir=%s",
            sector,
            len(peer_records),
            (avg_beat or 0) * 100,
            (avg_move or 0) * 100,
            direction,
        )

    async def _fetch_earnings_records(
        self, ticker: str, since: date
    ) -> list[PeerEarningsRecord]:
        """Fetch this quarter's earnings record for one ticker."""
        try:
            import yfinance as yf
            import pandas as pd

            tk = yf.Ticker(ticker)
            earnings_dates = tk.earnings_dates
            if earnings_dates is None or earnings_dates.empty:
                return []

            records = []
            hist = tk.history(period="1y", interval="1d", auto_adjust=True)

            for ts, row in earnings_dates.iterrows():
                report_date = ts.date() if hasattr(ts, "date") else ts
                if report_date < since or report_date > date.today():
                    continue

                eps_actual   = float(row.get("Reported EPS") or 0) or None
                eps_estimate = float(row.get("EPS Estimate") or 0) or None

                beat_pct = None
                if eps_actual and eps_estimate and eps_estimate != 0:
                    beat_pct = (eps_actual - eps_estimate) / abs(eps_estimate)

                # Stock move: earnings day close vs prev day close
                stock_move = None
                if not hist.empty:
                    hist_dates = [d.date() for d in hist.index]
                    if report_date in hist_dates:
                        idx = hist_dates.index(report_date)
                        if idx > 0:
                            c1 = float(hist["Close"].iloc[idx])
                            c0 = float(hist["Close"].iloc[idx - 1])
                            stock_move = (c1 - c0) / c0

                records.append(PeerEarningsRecord(
                    ticker=ticker,
                    report_date=report_date,
                    eps_actual=eps_actual,
                    eps_estimate=eps_estimate,
                    beat_pct=beat_pct,
                    stock_move_pct=stock_move,
                ))

            return records
        except Exception:
            return []

    async def _synthesize_read_through(
        self,
        sector: str,
        records: list[PeerEarningsRecord],
        avg_beat: float | None,
        avg_move: float | None,
    ) -> str:
        """One-sentence read-through narrative from Claude Haiku."""
        try:
            peer_str = "\n".join(
                f"  {r.ticker}: beat={r.beat_pct*100:+.1f}% | stock move={r.stock_move_pct*100:+.1f}%"
                for r in records
                if r.beat_pct is not None and r.stock_move_pct is not None
            )
            if not peer_str:
                return ""

            resp = await self._client.messages.create(
                model=self._settings.claude_fast_model,
                max_tokens=120,
                messages=[{
                    "role": "user",
                    "content": (
                        f"Sector: {sector}. Peers that already reported this quarter:\n{peer_str}\n\n"
                        f"In one sentence: what does this peer earnings pattern read-through suggest "
                        f"for remaining {sector} companies yet to report? Be specific about direction."
                    ),
                }],
            )
            return resp.content[0].text.strip()
        except Exception:
            return f"avg beat {(avg_beat or 0)*100:+.1f}% | avg move {(avg_move or 0)*100:+.1f}%"

    @staticmethod
    def _current_quarter_start() -> date:
        """Return the start of the current fiscal quarter (calendar-based)."""
        today = date.today()
        quarter_month_starts = {1: 1, 2: 1, 3: 1, 4: 4, 5: 4, 6: 4,
                                 7: 7, 8: 7, 9: 7, 10: 10, 11: 10, 12: 10}
        start_month = quarter_month_starts[today.month]
        return date(today.year, start_month, 1)

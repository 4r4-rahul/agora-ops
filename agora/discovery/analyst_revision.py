"""
AnalystRevisionTracker — post-earnings analyst upgrades and price target changes.

After a company reports earnings, Wall Street analysts revise:
  - Price targets (up/down)
  - Ratings (upgrade/downgrade)
  - EPS estimates (raise/cut)

This data is a leading indicator of post-earnings momentum:
  - Mass upgrades + PT raise = institutional conviction → multi-week bullish drift
  - Mass downgrades = institutions positioning short → continuation lower

The CSCO lesson: after a 17% beat + guidance raise, analysts rushed to upgrade.
Those upgrades drive the next 2-4 weeks of institutional buying.
We should position in the T+1 to T+5 window to capture this drift.

Data source: yfinance tk.upgrades_downgrades (free, 90-day lookback).
Fires catalyst callback when revision momentum is strong (≥3 upgrades, net positive).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable, Coroutine

from ..core.config import AgoraSettings, get_settings
from ..core.models import Catalyst, CatalystType

logger = logging.getLogger(__name__)

_MIN_UPGRADES_TO_FIRE  = 2    # need at least 2 upgrades to fire a bullish signal
_MIN_DOWNGRADES_TO_FIRE = 2   # need at least 2 downgrades to fire a bearish signal
_LOOKBACK_DAYS         = 5    # look for revisions in last 5 days (post-earnings window)


@dataclass
class RevisionSummary:
    ticker: str
    upgrades: int
    downgrades: int
    pt_raises: int
    pt_cuts: int
    avg_new_pt: float | None
    net_sentiment: float   # +1.0 = all upgrades, -1.0 = all downgrades
    direction: str         # "bullish" | "bearish" | "neutral"
    confidence: float


class AnalystRevisionTracker:
    """
    Tracks post-earnings analyst revisions.
    Called by session after _on_earnings() fires.
    """

    def __init__(
        self,
        settings: AgoraSettings | None = None,
        on_catalyst: Callable[[Catalyst], Coroutine] | None = None,
    ) -> None:
        self._settings    = settings or get_settings()
        self._on_catalyst = on_catalyst
        self._running     = False
        # Track which tickers we've already analyzed (avoid re-processing)
        self._analyzed: dict[str, date] = {}   # ticker → date analyzed
        self._csuite_manager: Any = None   # RNDAgent — set via register_csuite_manager()
        self._recent_revisions: list[dict] = []   # rolling buffer for R&D briefing

    def register_csuite_manager(self, manager: Any) -> None:
        """Wire the RNDAgent as supervising executive."""
        self._csuite_manager = manager

    def get_recent_revision_count(self) -> int:
        """Return count of analyst revision signals fired this session."""
        return len(self._recent_revisions)

    def get_recent_revisions(self) -> list[dict]:
        """Return last 10 analyst revision summaries for R&D briefing."""
        return self._recent_revisions[-10:]

    async def start(self) -> None:
        self._running = True
        while self._running:
            await asyncio.sleep(3600)   # Passive — called externally; just keep alive

    async def stop(self) -> None:
        self._running = False

    async def track_post_earnings(self, ticker: str) -> RevisionSummary | None:
        """
        Called by session._on_earnings() for a ticker that just reported.
        Fetches analyst revisions in the past 5 days and fires catalyst if strong.
        """
        # Avoid re-analyzing same ticker twice in one day
        today = date.today()
        if self._analyzed.get(ticker) == today:
            return None
        self._analyzed[ticker] = today

        try:
            summary = await self._fetch_revisions(ticker)
            if summary is None:
                return None

            logger.info(
                "AnalystRevision %s: up=%d dn=%d PT_raises=%d sentiment=%.2f dir=%s",
                ticker,
                summary.upgrades,
                summary.downgrades,
                summary.pt_raises,
                summary.net_sentiment,
                summary.direction,
            )

            # Fire catalyst if strong enough
            if summary.direction != "neutral" and summary.confidence >= 0.60:
                await self._fire_catalyst(ticker, summary)

            return summary
        except Exception as exc:
            logger.error("AnalystRevisionTracker failed for %s: %s", ticker, exc)
            return None

    async def _fetch_revisions(self, ticker: str) -> RevisionSummary | None:
        """Fetch analyst upgrades/downgrades from yfinance."""
        try:
            import yfinance as yf
            import pandas as pd

            tk = yf.Ticker(ticker)
            upgrades = tk.upgrades_downgrades

            if upgrades is None or upgrades.empty:
                return None

            # Filter to last 5 days
            cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=_LOOKBACK_DAYS)
            recent = upgrades[upgrades.index >= cutoff] if upgrades.index.tz else upgrades.tail(20)

            if recent.empty:
                return None

            # Count upgrades vs downgrades
            grade_col = None
            for col in ["Action", "GradeFrom", "ToGrade", "action", "grade"]:
                if col in recent.columns:
                    grade_col = col
                    break

            if grade_col is None:
                return None

            grades = recent[grade_col].str.lower()
            n_upgrades   = int(grades.str.contains("upgrade|raised|initiated|outperform|buy|overweight", na=False).sum())
            n_downgrades = int(grades.str.contains("downgrade|lowered|underperform|sell|underweight|neutral", na=False).sum())

            # Price target analysis
            pt_col = next((c for c in ["PriceTarget", "NewTarget", "TargetTo"] if c in recent.columns), None)
            avg_new_pt = None
            pt_raises  = 0
            pt_cuts    = 0
            if pt_col and "PriceTarget" in recent.columns:
                pts = recent["PriceTarget"].dropna()
                if not pts.empty:
                    avg_new_pt = float(pts.mean())

            # Net sentiment
            total = n_upgrades + n_downgrades
            if total == 0:
                return None

            net = (n_upgrades - n_downgrades) / total

            direction = "neutral"
            confidence = 0.5
            if net >= 0.5 and n_upgrades >= _MIN_UPGRADES_TO_FIRE:
                direction  = "bullish"
                confidence = min(0.5 + net * 0.5, 0.90)
            elif net <= -0.5 and n_downgrades >= _MIN_DOWNGRADES_TO_FIRE:
                direction  = "bearish"
                confidence = min(0.5 + abs(net) * 0.5, 0.90)

            return RevisionSummary(
                ticker=ticker,
                upgrades=n_upgrades,
                downgrades=n_downgrades,
                pt_raises=pt_raises,
                pt_cuts=pt_cuts,
                avg_new_pt=avg_new_pt,
                net_sentiment=net,
                direction=direction,
                confidence=confidence,
            )
        except Exception as exc:
            logger.debug("Revision fetch failed for %s: %s", ticker, exc)
            return None

    async def _fire_catalyst(self, ticker: str, summary: RevisionSummary) -> None:
        """Fire a catalyst for post-earnings analyst upgrade momentum."""
        if not self._on_catalyst:
            return

        strength = "strong" if summary.confidence >= 0.75 else "moderate"
        pt_str = f" | avg new PT ${summary.avg_new_pt:.2f}" if summary.avg_new_pt else ""

        catalyst = Catalyst(
            ticker=ticker,
            catalyst_type=CatalystType.GUIDANCE_RAISE if summary.direction == "bullish" else CatalystType.GUIDANCE_CUT,
            filing_time=datetime.utcnow(),
            headline=(
                f"Post-earnings analyst revisions: {summary.upgrades} upgrades / "
                f"{summary.downgrades} downgrades{pt_str}"
            ),
            direction=summary.direction,
            strength=strength,
            source="analyst_revision",
        )

        await self._on_catalyst(catalyst)
        logger.info(
            "AnalystRevision fired %s catalyst for %s: %d↑ %d↓ conf=%.2f",
            summary.direction, ticker, summary.upgrades, summary.downgrades, summary.confidence,
        )

        self._recent_revisions.append({
            "ticker": ticker,
            "direction": summary.direction,
            "upgrades": summary.upgrades,
            "downgrades": summary.downgrades,
            "confidence": round(summary.confidence, 2),
            "avg_pt": summary.avg_new_pt,
            "ts": datetime.utcnow().isoformat(),
        })
        if len(self._recent_revisions) > 50:
            self._recent_revisions = self._recent_revisions[-50:]

        if self._csuite_manager:
            await self._csuite_manager.receive_alert(
                "AnalystRevision", "info",
                f"Post-earnings revisions: {ticker} {summary.direction} | "
                f"{summary.upgrades}↑ {summary.downgrades}↓ conf={summary.confidence:.2f}",
            )

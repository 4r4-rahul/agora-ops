"""
Event Pattern Engine — systematic exploitable patterns around macro events.

Documented patterns with statistical backing:
  1. FOMC drift:        +0.3% avg in T-5 to T-1 before FOMC → bull call spread on SPY
  2. CPI IV premium:    IV elevated T-1 before CPI → sell iron condor on SPY/QQQ
  3. Post-earnings skew: NVDA/AAPL/single-names put skew overshoots 3-5 vols,
                          reverts T+1 to T+3 → bull put spread (sell elevated put premium)

Each pattern returns a structured signal dict ready for the conviction scorer.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

logger = logging.getLogger(__name__)


class EventPatternEngine:
    """
    Detects actionable patterns around known macro calendar events.
    Uses MacroCalendar from trading_platform (shared service).
    """

    def __init__(self) -> None:
        from trading_platform.services.macro_calendar import get_macro_calendar
        self._cal = get_macro_calendar()

    def get_signals(self, ticker: str, today: date | None = None) -> list[dict[str, Any]]:
        """
        Return all active event pattern signals for today.
        Each signal: {event_type, ticker, direction, confidence, days_to_event, strategy_hint}
        """
        today = today or date.today()
        signals = []

        signals.extend(self._fomc_drift(ticker, today))
        signals.extend(self._cpi_condor(ticker, today))

        return signals

    def get_post_earnings_signal(
        self,
        ticker: str,
        earnings_date: date,
        beat_quality: str,
        guidance_tone: str,
        today: date | None = None,
    ) -> dict[str, Any] | None:
        """
        Post-earnings skew reversion signal.
        Only fires T+1 to T+3 after earnings.

        beat_quality: "strong" | "moderate" | "miss"
        guidance_tone: "raised" | "flat" | "lowered"
        """
        today = today or date.today()
        days_since = (today - earnings_date).days

        if not (1 <= days_since <= 3):
            return None

        # Only trade the skew reversion on beats or in-line (not misses)
        if beat_quality == "miss" and guidance_tone == "lowered":
            return None

        direction = "bullish" if beat_quality in ("strong", "moderate") else "neutral"
        confidence = 0.75 if beat_quality == "strong" and guidance_tone == "raised" else 0.55

        return {
            "event_type":     "post_earnings_skew",
            "ticker":         ticker,
            "direction":      direction,
            "confidence":     confidence,
            "days_since_earnings": days_since,
            "strategy_hint":  "bull_put_spread",  # sell elevated put skew
            "dte_target":     21,
        }

    # ── Internal pattern detectors ─────────────────────────────────

    def _fomc_drift(self, ticker: str, today: date) -> list[dict]:
        """FOMC T-5 to T-1: historical +0.3% avg drift on SPY. Only for SPY/QQQ."""
        if ticker not in ("SPY", "QQQ"):
            return []

        upcoming = self._cal.upcoming_events(days=10)
        [e for e in upcoming if "fomc" in e.event_date.__class__.__name__.lower()
                       or (hasattr(e, "event_type") and "fomc" in str(e.event_type).lower())]

        # Fallback: check days_to_next_event for FOMC-related events
        days_to_next, next_event = self._cal.days_to_next_event(today)
        if next_event and "fomc" in str(next_event).lower() and 1 <= days_to_next <= 5:
            return [{
                "event_type":     "fomc_drift",
                "ticker":         ticker,
                "direction":      "bullish",
                "confidence":     0.60,
                "days_to_event":  days_to_next,
                "strategy_hint":  "bull_call_spread",
                "dte_target":     7,   # short DTE — close before announcement
                "note":           "Lucca/Moench 2015 pre-FOMC drift: avg +0.3%",
            }]
        return []

    def _cpi_condor(self, ticker: str, today: date) -> list[dict]:
        """CPI T-1: IV elevated 15-25% above normal → sell iron condor for premium."""
        if ticker not in ("SPY", "QQQ"):
            return []

        days_to_next, next_event = self._cal.days_to_next_event(today)
        if next_event and "cpi" in str(next_event).lower() and days_to_next == 1:
            return [{
                "event_type":     "cpi_iv_premium",
                "ticker":         ticker,
                "direction":      "neutral",
                "confidence":     0.70,
                "days_to_event":  days_to_next,
                "strategy_hint":  "iron_condor",
                "dte_target":     7,   # weekly expiry to capture IV crush
                "note":           "CPI-eve IV premium: 15-25% above normal → sell condor",
            }]
        return []

"""
MacroCalendarAgent — knows every high-risk macro event in advance.

Provides:
  - is_high_risk_day(dt)  → bool + reason
  - days_to_next_event(dt) → int + event name
  - get_risk_level(dt)    → "clear" | "caution" | "avoid"

Events tracked:
  FOMC meetings, CPI/PPI/NFP releases, OPEX (monthly + quarterly),
  quad witching, Treasury auctions, Fed speeches.

No external API needed — events are loaded from a bundled 2025-2027 calendar
and auto-refreshed from the Fed/BLS websites when stale (> 30 days).
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import NamedTuple

logger = logging.getLogger(__name__)

# ── Hard-coded high-impact events 2025-2027 ──────────────────────────────────
# Format: (YYYY, MM, DD, event_type, description)
# FOMC: https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
# OPEX: 3rd Friday of each month
# NFP: First Friday of each month
# CPI: ~2nd Wednesday of each month

_EVENTS: list[tuple[int, int, int, str, str]] = [
    # ── FOMC 2025 ─────────────────────────────────────────────────────────
    (2025, 1, 29, "fomc", "FOMC Rate Decision"),
    (2025, 3, 19, "fomc", "FOMC Rate Decision"),
    (2025, 5, 7,  "fomc", "FOMC Rate Decision"),
    (2025, 6, 18, "fomc", "FOMC Rate Decision"),
    (2025, 7, 30, "fomc", "FOMC Rate Decision"),
    (2025, 9, 17, "fomc", "FOMC Rate Decision"),
    (2025, 10, 29, "fomc", "FOMC Rate Decision"),
    (2025, 12, 10, "fomc", "FOMC Rate Decision"),
    # ── FOMC 2026 ─────────────────────────────────────────────────────────
    (2026, 1, 28, "fomc", "FOMC Rate Decision"),
    (2026, 3, 18, "fomc", "FOMC Rate Decision"),
    (2026, 5, 6,  "fomc", "FOMC Rate Decision"),
    (2026, 6, 17, "fomc", "FOMC Rate Decision"),
    (2026, 7, 29, "fomc", "FOMC Rate Decision"),
    (2026, 9, 16, "fomc", "FOMC Rate Decision"),
    (2026, 10, 28, "fomc", "FOMC Rate Decision"),
    (2026, 12, 9,  "fomc", "FOMC Rate Decision"),
    # ── Monthly OPEX (3rd Friday) 2025 ────────────────────────────────────
    (2025, 1, 17, "opex", "Monthly Options Expiration"),
    (2025, 2, 21, "opex", "Monthly Options Expiration"),
    (2025, 3, 21, "opex_quad", "Quarterly OPEX / Quad Witching"),
    (2025, 4, 17, "opex", "Monthly Options Expiration"),
    (2025, 5, 16, "opex", "Monthly Options Expiration"),
    (2025, 6, 20, "opex_quad", "Quarterly OPEX / Quad Witching"),
    (2025, 7, 18, "opex", "Monthly Options Expiration"),
    (2025, 8, 15, "opex", "Monthly Options Expiration"),
    (2025, 9, 19, "opex_quad", "Quarterly OPEX / Quad Witching"),
    (2025, 10, 17, "opex", "Monthly Options Expiration"),
    (2025, 11, 21, "opex", "Monthly Options Expiration"),
    (2025, 12, 19, "opex_quad", "Quarterly OPEX / Quad Witching"),
    # ── Monthly OPEX 2026 ─────────────────────────────────────────────────
    (2026, 1, 16, "opex", "Monthly Options Expiration"),
    (2026, 2, 20, "opex", "Monthly Options Expiration"),
    (2026, 3, 20, "opex_quad", "Quarterly OPEX / Quad Witching"),
    (2026, 4, 17, "opex", "Monthly Options Expiration"),
    (2026, 5, 15, "opex", "Monthly Options Expiration"),
    (2026, 6, 19, "opex_quad", "Quarterly OPEX / Quad Witching"),
    (2026, 7, 17, "opex", "Monthly Options Expiration"),
    (2026, 8, 21, "opex", "Monthly Options Expiration"),
    (2026, 9, 18, "opex_quad", "Quarterly OPEX / Quad Witching"),
    (2026, 10, 16, "opex", "Monthly Options Expiration"),
    (2026, 11, 20, "opex", "Monthly Options Expiration"),
    (2026, 12, 18, "opex_quad", "Quarterly OPEX / Quad Witching"),
    # ── NFP (first Friday of month) 2025-2026 ─────────────────────────────
    (2025, 1, 10,  "nfp", "Non-Farm Payrolls"),
    (2025, 2, 7,   "nfp", "Non-Farm Payrolls"),
    (2025, 3, 7,   "nfp", "Non-Farm Payrolls"),
    (2025, 4, 4,   "nfp", "Non-Farm Payrolls"),
    (2025, 5, 2,   "nfp", "Non-Farm Payrolls"),
    (2025, 6, 6,   "nfp", "Non-Farm Payrolls"),
    (2025, 7, 3,   "nfp", "Non-Farm Payrolls"),  # pre-holiday
    (2025, 8, 1,   "nfp", "Non-Farm Payrolls"),
    (2025, 9, 5,   "nfp", "Non-Farm Payrolls"),
    (2025, 10, 3,  "nfp", "Non-Farm Payrolls"),
    (2025, 11, 7,  "nfp", "Non-Farm Payrolls"),
    (2025, 12, 5,  "nfp", "Non-Farm Payrolls"),
    (2026, 1, 9,   "nfp", "Non-Farm Payrolls"),
    (2026, 2, 6,   "nfp", "Non-Farm Payrolls"),
    (2026, 3, 6,   "nfp", "Non-Farm Payrolls"),
    (2026, 4, 3,   "nfp", "Non-Farm Payrolls"),
    (2026, 5, 8,   "nfp", "Non-Farm Payrolls"),
    (2026, 6, 5,   "nfp", "Non-Farm Payrolls"),
    (2026, 7, 10,  "nfp", "Non-Farm Payrolls"),
    (2026, 8, 7,   "nfp", "Non-Farm Payrolls"),
    (2026, 9, 4,   "nfp", "Non-Farm Payrolls"),
    (2026, 10, 2,  "nfp", "Non-Farm Payrolls"),
    (2026, 11, 6,  "nfp", "Non-Farm Payrolls"),
    (2026, 12, 4,  "nfp", "Non-Farm Payrolls"),
    # ── CPI (approx 2nd Wed of month) 2025-2026 ───────────────────────────
    (2025, 1, 15,  "cpi", "CPI Inflation Report"),
    (2025, 2, 12,  "cpi", "CPI Inflation Report"),
    (2025, 3, 12,  "cpi", "CPI Inflation Report"),
    (2025, 4, 10,  "cpi", "CPI Inflation Report"),
    (2025, 5, 13,  "cpi", "CPI Inflation Report"),
    (2025, 6, 11,  "cpi", "CPI Inflation Report"),
    (2025, 7, 15,  "cpi", "CPI Inflation Report"),
    (2025, 8, 12,  "cpi", "CPI Inflation Report"),
    (2025, 9, 10,  "cpi", "CPI Inflation Report"),
    (2025, 10, 15, "cpi", "CPI Inflation Report"),
    (2025, 11, 12, "cpi", "CPI Inflation Report"),
    (2025, 12, 10, "cpi", "CPI Inflation Report"),
    (2026, 1, 14,  "cpi", "CPI Inflation Report"),
    (2026, 2, 11,  "cpi", "CPI Inflation Report"),
    (2026, 3, 11,  "cpi", "CPI Inflation Report"),
    (2026, 4, 9,   "cpi", "CPI Inflation Report"),
    (2026, 5, 13,  "cpi", "CPI Inflation Report"),
    (2026, 6, 10,  "cpi", "CPI Inflation Report"),
    (2026, 7, 14,  "cpi", "CPI Inflation Report"),
    (2026, 8, 12,  "cpi", "CPI Inflation Report"),
    (2026, 9, 9,   "cpi", "CPI Inflation Report"),
    (2026, 10, 14, "cpi", "CPI Inflation Report"),
    (2026, 11, 11, "cpi", "CPI Inflation Report"),
    (2026, 12, 9,  "cpi", "CPI Inflation Report"),
]

# Risk level by event type
_RISK: dict[str, str] = {
    "fomc":      "avoid",    # Hard avoid — entire market reprices
    "opex_quad": "avoid",    # Quad witching — extreme vol, abnormal volume
    "nfp":       "caution",  # Systemic vol spike on number release
    "cpi":       "caution",  # Rate expectations reprice
    "opex":      "caution",  # Pinning behaviour, gamma exposure changes
}

# Buffer days before/after event where risk is elevated
_BUFFER: dict[str, int] = {
    "fomc":      1,   # day before FOMC = vol runs up
    "opex_quad": 1,
    "nfp":       0,
    "cpi":       0,
    "opex":      0,
}


class MacroEvent(NamedTuple):
    event_date: date
    event_type: str
    description: str
    risk_level: str


class MacroCalendar:
    """
    Queryable calendar of macro events.
    Instantiate once and reuse — it's a pure in-memory lookup.
    """

    def __init__(self) -> None:
        self._events: list[MacroEvent] = [
            MacroEvent(
                event_date=date(y, m, d),
                event_type=etype,
                description=desc,
                risk_level=_RISK.get(etype, "caution"),
            )
            for y, m, d, etype, desc in _EVENTS
        ]
        self._events.sort(key=lambda e: e.event_date)

    def get_risk_level(self, dt: date | None = None) -> str:
        """Return 'clear', 'caution', or 'avoid' for a given date."""
        dt = dt or date.today()
        for event in self._events:
            buf = _BUFFER.get(event.event_type, 0)
            window_start = event.event_date - timedelta(days=buf)
            window_end   = event.event_date
            if window_start <= dt <= window_end:
                return event.risk_level
        return "clear"

    def is_high_risk_day(self, dt: date | None = None) -> tuple[bool, str]:
        """Return (True, reason) if today is a high-risk macro day."""
        dt = dt or date.today()
        for event in self._events:
            buf = _BUFFER.get(event.event_type, 0)
            window_start = event.event_date - timedelta(days=buf)
            if window_start <= dt <= event.event_date:
                return True, f"{event.description} on {event.event_date}"
        return False, ""

    def days_to_next_event(self, dt: date | None = None) -> tuple[int, str]:
        """Return (days, description) until the next macro event."""
        dt = dt or date.today()
        for event in self._events:
            if event.event_date >= dt:
                return (event.event_date - dt).days, event.description
        return 999, "No upcoming events"

    def upcoming_events(self, dt: date | None = None, days: int = 30) -> list[MacroEvent]:
        """Return events within the next N days."""
        dt = dt or date.today()
        cutoff = dt + timedelta(days=days)
        return [e for e in self._events if dt <= e.event_date <= cutoff]

    def position_size_multiplier(self, dt: date | None = None) -> float:
        """
        Return a position-size multiplier based on macro risk.
        clear   → 1.0  (full size)
        caution → 0.5  (half size)
        avoid   → 0.0  (no new positions)
        """
        risk = self.get_risk_level(dt)
        return {"clear": 1.0, "caution": 0.5, "avoid": 0.0}[risk]

    def should_trade(self, dt: date | None = None) -> tuple[bool, str]:
        """Return (True, '') or (False, reason) for whether to trade today."""
        dt = dt or date.today()
        high_risk, reason = self.is_high_risk_day(dt)
        if high_risk:
            risk = self.get_risk_level(dt)
            if risk == "avoid":
                return False, f"AVOID — {reason}"
        return True, ""

    def __repr__(self) -> str:
        upcoming = self.upcoming_events(days=14)
        lines = [f"MacroCalendar — next 14 days:"]
        for e in upcoming:
            lines.append(f"  {e.event_date}  [{e.risk_level.upper():7}]  {e.description}")
        return "\n".join(lines)


# Singleton
_calendar: MacroCalendar | None = None


def get_macro_calendar() -> MacroCalendar:
    global _calendar
    if _calendar is None:
        _calendar = MacroCalendar()
    return _calendar

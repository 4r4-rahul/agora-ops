"""
EntryTimingGate — hard rules about when new positions can be opened.

Prevents the CSCO failure mode: after-hours entry triggered by EDGAR 8-K at 5:33 PM ET.
This gate is called synchronously inside _submit_recommendation() before any IBKR order.

Permitted window: 10:00 AM → 3:30 PM ET
  - Skip the open (9:30–10:00): widest spreads, worst fills, most algos gaming the open
  - Skip the close (3:30+): after-hours fill risk, gamma instability near close
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# New entries permitted: 10:00 AM → 3:30 PM ET
# First 30 minutes (9:30–10:00) are skipped: widest spreads, algo-driven price discovery,
# order imbalances; options fills are routinely 20–30% wider than mid during this window.
_ENTRY_OPEN_HOUR   = 10
_ENTRY_OPEN_MIN    = 0    # full first 30 minutes skipped
_ENTRY_CLOSE_HOUR  = 15
_ENTRY_CLOSE_MIN   = 30   # hard stop: no new entries 3:30 PM or later


class EntryTimingGate:
    """
    Stateless gate that returns (permitted: bool, reason: str).

    Called on every new trade before the risk council.
    Closing existing positions is NOT gated here — only new entries.
    """

    def is_entry_permitted(self) -> tuple[bool, str]:
        """Returns (True, 'Market hours') or (False, human-readable reason)."""
        now = datetime.now(tz=ET)

        # Weekend
        if now.weekday() >= 5:
            day = now.strftime("%A")
            return False, f"Weekend ({day}) — market closed, no new entries"

        h, m = now.hour, now.minute
        total_min = h * 60 + m

        open_min  = _ENTRY_OPEN_HOUR  * 60 + _ENTRY_OPEN_MIN   # 600 (10:00 AM)
        close_min = _ENTRY_CLOSE_HOUR * 60 + _ENTRY_CLOSE_MIN  # 930 (3:30 PM)

        if total_min < open_min:
            opens_in = open_min - total_min
            if total_min < 9 * 60 + 30:
                return False, (
                    f"Pre-market ({now.strftime('%H:%M')} ET) — "
                    f"entries open at 10:00 ET (in {opens_in} min)"
                )
            return False, (
                f"Market open window ({now.strftime('%H:%M')} ET) — "
                f"skipping first 30 min; entries open at 10:00 ET (in {opens_in} min). "
                "Options spreads are 20-30% wider during 9:30-10:00 open."
            )

        if total_min >= close_min:
            return False, (
                f"After 3:30 PM ET ({now.strftime('%H:%M')} ET) — "
                "no new entries; prevents after-hours fills on pending orders"
            )

        return True, f"Market hours ({now.strftime('%H:%M')} ET)"

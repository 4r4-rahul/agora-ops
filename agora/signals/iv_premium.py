"""
IV Premium Screen — signals when implied vol is systematically overpriced
relative to realized vol, creating a structural edge for selling premium.

Signal: (ATM_IV_30d - HV_21d) / HV_21d > threshold for min_days consecutive days.

This is the vol risk premium — the most documented persistent edge in options.
When IV_implied consistently exceeds IV_realized, option sellers earn positive EV.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)


class IvPremiumScreen:
    """
    Tracks the IV_implied_vs_realized ratio for a ticker over time.
    Signals when premium has been elevated for min_days consecutive trading days.
    """

    def __init__(
        self,
        threshold: float = 0.25,
        min_days: int = 15,
        cache_dir: Path = Path(".agora/iv_premium"),
    ) -> None:
        self._threshold = threshold
        self._min_days = min_days
        self._cache_dir = cache_dir
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    def check(self, ticker: str, atm_iv: float | None, hv_21d: float | None) -> dict:
        """
        Update the daily cache and return the current signal state.

        Parameters
        ----------
        ticker   : stock symbol
        atm_iv   : today's ATM implied vol (as decimal, e.g. 0.25 = 25%)
        hv_21d   : 21-day historical/realized vol (as decimal)

        Returns
        -------
        dict with: signal_active, premium_ratio, days_above_threshold,
                   atm_iv_pct, hv_21d_pct
        """
        result: dict = {
            "ticker":               ticker,
            "signal_active":        False,
            "premium_ratio":        None,
            "days_above_threshold": 0,
            "atm_iv_pct":           round(atm_iv * 100, 1) if atm_iv else None,
            "hv_21d_pct":           round(hv_21d * 100, 1) if hv_21d else None,
        }

        if not atm_iv or not hv_21d or hv_21d <= 0:
            return result

        premium_ratio = (atm_iv - hv_21d) / hv_21d
        result["premium_ratio"] = round(premium_ratio, 4)

        # Load cache
        cache_path = self._cache_dir / f"{ticker.upper()}.json"
        cache: dict = {"dates": [], "ratios": []}
        if cache_path.exists():
            try:
                cache = json.loads(cache_path.read_text())
            except Exception:
                cache = {"dates": [], "ratios": []}

        today_str = date.today().isoformat()
        if not cache["dates"] or cache["dates"][-1] != today_str:
            cache["dates"].append(today_str)
            cache["ratios"].append(round(premium_ratio, 6))

        # Keep 90 days
        cache["dates"]  = cache["dates"][-90:]
        cache["ratios"] = cache["ratios"][-90:]
        cache_path.write_text(json.dumps(cache))

        # Count consecutive days at or above threshold (from most recent going back)
        consecutive = 0
        for r in reversed(cache["ratios"]):
            if r >= self._threshold:
                consecutive += 1
            else:
                break

        result["days_above_threshold"] = consecutive
        result["signal_active"] = consecutive >= self._min_days

        return result

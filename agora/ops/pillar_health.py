"""
PillarHealthAgent — per-pillar liveness and contribution telemetry.

Problem it solves:
  DisagreementResolver logs "No consensus direction (bull=0, bear=0)" on most
  tickers. This means the directional pillars (SectorMomentum, Catalyst,
  SmartMoney) are not generating signed votes. Without this agent, the silent-
  pillar condition is invisible — no alert fires, no CEO notification, no
  automatic correction.

What we track per pillar:
  last_signal_time    — when it last contributed a bullish or bearish vote
  contribution_count  — bullish/bearish/neutral counts (session rolling)
  silent_minutes      — minutes since last non-neutral signal
  health_status       — healthy | silent | degraded

Alert thresholds:
  > 60 min without signal   → "silent" warning
  > 180 min without signal  → "degraded" critical alert to CEO
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from ..core.config import AgoraSettings, get_settings

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

_SILENT_WARN_MIN   = 60    # minutes without a directional signal → warning
_SILENT_CRIT_MIN   = 180   # minutes without a directional signal → critical
_CHECK_INTERVAL    = 600   # check every 10 min


class PillarHealthAgent:
    """
    Synchronous recording API + async monitoring loop.
    ConvictionScorer calls record_pillar_signal() on every evaluation.
    This agent monitors the cadence and escalates when pillars go dark.
    """

    def __init__(
        self,
        settings: AgoraSettings | None = None,
        ceo_agent: Any = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._ceo = ceo_agent
        self._csuite_manager: Any = None   # RNDAgent — set via register_csuite_manager()
        self._running = False

        # Per-pillar state
        self._last_signal:     dict[str, datetime | None] = defaultdict(lambda: None)  # directional
        self._last_any_signal: dict[str, datetime | None] = defaultdict(lambda: None)  # any direction
        self._contrib:      dict[str, dict[str, int]] = defaultdict(
            lambda: {"bullish": 0, "bearish": 0, "neutral": 0, "total": 0}
        )
        self._alerted_silent:    set[str] = set()
        self._alerted_degraded:  set[str] = set()

    def register_csuite_manager(self, manager: Any) -> None:
        """Wire the RNDAgent as supervising executive for alert escalation."""
        self._csuite_manager = manager

    # ── Recording API (called by ConvictionScorer / session) ─────────

    def record_pillar_signal(
        self, pillar: str, direction: str, ticker: str = "", score: float = 0.0
    ) -> None:
        """
        Call on every ConvictionScorer evaluation.
        direction: 'bullish' | 'bearish' | 'neutral'
        """
        now = datetime.now(tz=ET)
        self._contrib[pillar]["total"] += 1
        direction_key = direction if direction in ("bullish", "bearish", "neutral") else "neutral"
        self._contrib[pillar][direction_key] += 1

        # Track any-signal time (proves the pillar is alive even in neutral markets)
        self._last_any_signal[pillar] = now

        if direction_key in ("bullish", "bearish"):
            self._last_signal[pillar] = now
            # Reset alert flags when pillar recovers
            self._alerted_silent.discard(pillar)
            self._alerted_degraded.discard(pillar)
            logger.debug(
                "PillarHealth: %s → %s | %s | score=%.1f",
                pillar, direction_key, ticker, score,
            )

    # ── Status queries ───────────────────────────────────────────────

    def get_pillar_health(self) -> dict[str, dict]:
        now = datetime.now(tz=ET)
        result = {}
        for pillar, stats in self._contrib.items():
            last_dir = self._last_signal.get(pillar)
            # Use last-any-signal for liveness: a neutral macro in a calm market
            # is healthy, not silent. Only alert when the pillar stops reporting entirely.
            last_any = self._last_any_signal.get(pillar)
            silent_min = (
                (now - last_any).total_seconds() / 60
                if last_any is not None
                else float("inf")
            )
            total = stats["total"]
            dir_rate = (
                (stats["bullish"] + stats["bearish"]) / total
                if total > 0 else 0.0
            )
            if silent_min < _SILENT_WARN_MIN:
                status = "healthy"
            elif silent_min < _SILENT_CRIT_MIN:
                status = "silent"
            else:
                status = "degraded"

            result[pillar] = {
                "status":           status,
                "last_signal":      last_dir.isoformat() if last_dir else None,
                "last_any_signal":  last_any.isoformat() if last_any else None,
                "silent_minutes":   round(silent_min, 1) if silent_min != float("inf") else None,
                "directional_rate": round(dir_rate, 2),
                "bullish":          stats["bullish"],
                "bearish":          stats["bearish"],
                "neutral":          stats["neutral"],
                "total":            total,
            }
        return result

    def is_pillar_healthy(self, pillar: str) -> bool:
        last = self._last_any_signal.get(pillar)
        if last is None:
            return False
        return (datetime.now(tz=ET) - last).total_seconds() / 60 < _SILENT_WARN_MIN

    def get_silent_pillars(self) -> list[str]:
        """Return pillars that have been silent beyond the warning threshold."""
        return [
            p for p, h in self.get_pillar_health().items()
            if h["status"] in ("silent", "degraded")
        ]

    # ── Async monitoring loop ─────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        while self._running:
            await asyncio.sleep(_CHECK_INTERVAL)
            now_et = datetime.now(tz=ET)
            if 9 <= now_et.hour < 16:
                await self._check_pillar_health()

    async def stop(self) -> None:
        self._running = False

    async def _check_pillar_health(self) -> None:
        health = self.get_pillar_health()
        for pillar, h in health.items():
            status = h["status"]
            silent_min = h["silent_minutes"]

            if status == "silent" and pillar not in self._alerted_silent:
                self._alerted_silent.add(pillar)
                msg = (
                    f"Pillar '{pillar}' has been SILENT for {silent_min:.0f} min "
                    f"(directional rate: {h['directional_rate']:.0%}). "
                    f"Conviction scores will be structurally low until it contributes."
                )
                logger.warning("PillarHealth: %s", msg)
                await self._notify("warning", f"⚠️ {msg}")

            elif status == "degraded" and pillar not in self._alerted_degraded:
                self._alerted_degraded.add(pillar)
                msg = (
                    f"Pillar '{pillar}' DEGRADED — {silent_min:.0f} min without a "
                    f"directional signal. Total evaluations: {h['total']}. "
                    f"Investigate the pillar→scorer wiring."
                )
                logger.error("PillarHealth: %s", msg)
                await self._notify("critical", f"🚨 {msg}")

        # Summary log every check
        silent = [p for p, h in health.items() if h["status"] != "healthy"]
        if silent:
            logger.info(
                "PillarHealth summary: %d silent/degraded pillars — %s",
                len(silent), silent,
            )

    async def _notify(self, level: str, message: str) -> None:
        """Route alert: PillarHealthAgent → R&D → CEO."""
        if self._csuite_manager:
            await self._csuite_manager.receive_alert("PillarHealthAgent", level, message)
        elif self._ceo:
            await self._ceo.dispatch_alert(level, message)

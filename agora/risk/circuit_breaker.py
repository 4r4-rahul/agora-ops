"""
CircuitBreakerAgent — real-time continuous P&L monitor.

Distinct from RiskCouncil (which gates each trade before entry),
this agent monitors the live portfolio every 60 seconds and trips
the kill switch automatically on:

  1. Daily loss breach    — unrealized + realized > daily_loss_limit_dollars
  2. Single position blow — position unrealized P&L > 2× its max_loss_dollars
  3. VIX spike > 40      — enters size-reduction mode (does NOT trip kill switch)
  4. Market halt         — circuit breaker detected (SPY move > 7% in a session)

Fires CEOAgent critical alerts on every trip.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from ..agents.ceo_agent import CEOAgent
    from ..lifecycle.position_manager import PositionManager
    from .risk_council import RiskCouncil

from ..core.config import AgoraSettings, get_settings

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

_VIX_STRESS_THRESHOLD  = 40.0    # above this: size reduction mode
_SPY_HALT_THRESHOLD    = 0.07    # 7% single-session drop: market circuit breaker


class CircuitBreakerAgent:
    """
    Runs every 60 seconds during market hours.
    Designed to never raise — any exception is caught and logged.
    """

    def __init__(
        self,
        settings: AgoraSettings | None = None,
        position_mgr: Any = None,
        risk_council: Any = None,
        ceo_agent: Any = None,
    ) -> None:
        self._settings   = settings or get_settings()
        self._position_mgr = position_mgr
        self._risk       = risk_council
        self._ceo        = ceo_agent
        self._csuite_manager: Any = None   # CROAgent — set via register_csuite_manager()
        self._running    = False

        # Stress mode state — not a kill switch, just a flag for session to read
        self._vix_stress_mode: bool = False
        self._vix_stress_level: float = 0.0

        # Track which positions we've already alerted on (avoid repeated alerts)
        self._alerted_positions: set[str] = set()
        # SPY open price for halt detection
        self._spy_open: float | None = None
        # Trip log for reporting
        self._trip_log: list[dict] = []

        # Daily mark-to-market baseline — set on first check of each trading day.
        # Daily loss = (total_unrealized_now - _daily_unrealized_baseline) + realized_today.
        # Persisted to disk so mid-day restarts don't forgive morning losses.
        self._daily_unrealized_baseline: float | None = None
        self._baseline_date: str = ""
        # Most recent unrealized mark, persisted every cycle. On a new day it becomes
        # the baseline (a proxy for the prior session's close) so overnight gaps on
        # held positions count toward today's loss instead of being forgiven.
        self._last_unrealized: float | None = None
        self._last_unrealized_date: str = ""
        self._baseline_path = (
            self._settings.db_path.parent / "cb_baseline.json"
        )
        self._load_baseline()

    # ── Baseline persistence ───────────────────────────────────────

    def _load_baseline(self) -> None:
        """Load persisted state. The intraday baseline is reused only within the same
        calendar day (so a mid-day restart honours morning losses); the last mark is
        always loaded so a fresh start on a new day can baseline against the prior
        session's close — capturing overnight gaps rather than forgiving them."""
        try:
            import json
            from datetime import date
            if not self._baseline_path.exists():
                return
            data = json.loads(self._baseline_path.read_text())
            if data.get("last_unrealized") is not None:
                self._last_unrealized = float(data["last_unrealized"])
                self._last_unrealized_date = data.get("last_date", "")
            if data.get("date") == date.today().isoformat() and data.get("baseline") is not None:
                self._daily_unrealized_baseline = float(data["baseline"])
                self._baseline_date = data["date"]
                logger.info(
                    "CircuitBreaker: loaded persisted baseline $%.0f for %s",
                    self._daily_unrealized_baseline, self._baseline_date,
                )
        except Exception as exc:
            logger.debug("CircuitBreaker: could not load baseline: %s", exc)

    def _save_baseline(self) -> None:
        """Persist baseline + most-recent mark so restarts honour today's losses and
        the next session can baseline against this session's close."""
        try:
            import json
            self._baseline_path.write_text(
                json.dumps({
                    "date": self._baseline_date,
                    "baseline": self._daily_unrealized_baseline,
                    "last_unrealized": self._last_unrealized,
                    "last_date": self._last_unrealized_date,
                })
            )
        except Exception as exc:
            logger.debug("CircuitBreaker: could not save baseline: %s", exc)

    def register_csuite_manager(self, manager: Any) -> None:
        """Wire the CROAgent as the supervising executive for alert escalation."""
        self._csuite_manager = manager

    @property
    def vix_stress_mode(self) -> bool:
        return self._vix_stress_mode

    @property
    def size_multiplier_override(self) -> float:
        """0.5 when VIX stress mode, 1.0 normally."""
        return 0.5 if self._vix_stress_mode else 1.0

    def get_state(self) -> dict:
        """Public snapshot for CRO intelligence collection."""
        ks = self._risk.get_kill_switch_state() if self._risk else {}
        return {
            "kill_switch_active":   ks.get("active", False),
            "kill_switch_reason":   ks.get("reason", ""),
            "vix_stress_mode":      self._vix_stress_mode,
            "vix_level":            self._vix_stress_level,
            "size_multiplier":      self.size_multiplier_override,
            "alerted_positions":    len(self._alerted_positions),
            "recent_trips":         self._trip_log[-3:],
        }

    async def start(self) -> None:
        self._running = True
        logger.info("CircuitBreakerAgent started")
        while self._running:
            now_et = datetime.now(tz=ET)
            # Only run from 9:30 AM (market open) to 4:30 PM ET.
            # Pre-market prices are stale/wide — running before 9:30 would set
            # a garbage baseline and absorb real morning losses into it.
            from datetime import time as _t
            if now_et.weekday() < 5 and _t(9, 30) <= now_et.time() < _t(16, 30):
                try:
                    await self._check_cycle()
                except Exception as exc:
                    logger.error("CircuitBreaker check failed: %s", exc)
            await asyncio.sleep(60)

    async def stop(self) -> None:
        self._running = False

    # ── Main check cycle ───────────────────────────────────────────

    async def _check_cycle(self) -> None:
        from datetime import date
        positions = self._position_mgr.get_open_positions() if self._position_mgr else []

        # 1. Individual position loss limit
        for pos in positions:
            if pos.position_id in self._alerted_positions:
                continue
            if pos.unrealized_pnl < -2.0 * pos.max_loss_dollars:
                self._alerted_positions.add(pos.position_id)
                msg = (
                    f"Position {pos.ticker} ({pos.strategy}) exceeded 2× max loss: "
                    f"P&L=${pos.unrealized_pnl:.0f}, max=${pos.max_loss_dollars:.0f}"
                )
                logger.critical("CIRCUIT BREAKER (position loss): %s", msg)
                if self._risk:
                    self._risk.trip_kill_switch(msg, tripped_by="circuit_breaker")
                await self._alert("critical", f"🚨 Position loss limit: {msg}")

        # 2. Daily portfolio loss — mark-to-market change since session open + realized today.
        # We track a baseline (set once per calendar day on first check) so that losses
        # carried over from prior sessions do not re-trip the switch on restart.
        total_unrealized = sum(p.unrealized_pnl for p in positions)
        realized_today   = self._get_todays_realized_pnl()
        today_str        = date.today().isoformat()
        limit            = self._settings.daily_loss_limit_dollars

        # Reset baseline each new calendar day, or on first check ever.
        if self._baseline_date != today_str or self._daily_unrealized_baseline is None:
            # Baseline against the PRIOR session's last mark (its close) when available,
            # so an overnight gap on held positions counts toward today's loss. Only fall
            # back to the current mark on the very first run with no prior history.
            if self._last_unrealized is not None and self._last_unrealized_date != today_str:
                self._daily_unrealized_baseline = self._last_unrealized
            else:
                self._daily_unrealized_baseline = total_unrealized
            self._baseline_date = today_str
            logger.info(
                "CircuitBreaker: daily unrealized baseline set to $%.0f for %s",
                self._daily_unrealized_baseline, today_str,
            )

        # Record this cycle's mark as the most recent (prior-close proxy for next day).
        self._last_unrealized      = total_unrealized
        self._last_unrealized_date = today_str
        self._save_baseline()

        # Daily P&L = today's movement in unrealized + today's realized closes.
        daily_pnl = (total_unrealized - self._daily_unrealized_baseline) + realized_today

        # Push live snapshot to RiskCouncil for dashboard/readiness reporting.
        if self._risk:
            self._risk.record_daily_pnl(
                realized=realized_today,
                unrealized=total_unrealized,
                trades=len([p for p in positions if p.unrealized_pnl != 0]),
            )

        if daily_pnl < -limit:
            msg = (
                f"Daily loss limit breached: ${daily_pnl:.0f} today "
                f"(Δunrealized=${total_unrealized - self._daily_unrealized_baseline:.0f} "
                f"+ realized=${realized_today:.0f}) vs limit=${-limit:.0f}"
            )
            logger.critical("CIRCUIT BREAKER (daily loss): %s", msg)
            if self._risk and not self._risk.is_kill_switch_active():
                self._risk.trip_kill_switch(msg, tripped_by="circuit_breaker")
                await self._alert("critical", f"🚨 Daily loss circuit breaker tripped — {msg}")

        # 3. VIX stress check and market halt detection
        try:
            import yfinance as yf

            def _fast_price(sym: str) -> float:
                fi = yf.Ticker(sym).fast_info
                try:
                    p = float(fi.last_price or 0)
                except AttributeError:
                    p = float(fi.get("lastPrice", 0) or 0)
                return p

            vix = _fast_price("^VIX")
            prev_stress = self._vix_stress_mode
            self._vix_stress_mode  = vix >= _VIX_STRESS_THRESHOLD
            self._vix_stress_level = vix

            if self._vix_stress_mode and not prev_stress:
                msg = f"VIX stress mode ACTIVATED: VIX={vix:.1f} ≥ {_VIX_STRESS_THRESHOLD}"
                logger.warning("CIRCUIT BREAKER: %s", msg)
                await self._alert("warning", f"⚠️ {msg} — size halved")
            elif not self._vix_stress_mode and prev_stress:
                logger.info("VIX stress mode CLEARED: VIX=%.1f", vix)

            # Market halt: SPY session drop > 7%
            spy_now = _fast_price("SPY")
            try:
                fi_spy = yf.Ticker("SPY").fast_info
                spy_open_today = float(getattr(fi_spy, "open", None) or spy_now)
            except Exception:
                spy_open_today = spy_now
            if spy_open_today > 0 and spy_now > 0:
                session_drop = (spy_now - spy_open_today) / spy_open_today
                if session_drop < -_SPY_HALT_THRESHOLD:
                    msg = f"Market circuit breaker: SPY down {session_drop*100:.1f}% today"
                    logger.critical("CIRCUIT BREAKER (market halt): %s", msg)
                    if self._risk and not self._risk.is_kill_switch_active():
                        self._risk.trip_kill_switch(msg, tripped_by="market_halt")
                        await self._alert("critical", f"🚨 Market circuit breaker: {msg}")
        except Exception as exc:
            logger.debug("CircuitBreaker VIX/SPY check failed: %s", exc)

    # ── Alert routing ──────────────────────────────────────────────

    async def _alert(self, level: str, message: str, details: dict | None = None) -> None:
        """Route alert up the chain: sub-agent → CRO → CEO."""
        if level in ("critical", "warning"):
            self._trip_log.append({"level": level, "message": message[:120],
                                   "ts": datetime.now(tz=ET).isoformat()})
            self._trip_log = self._trip_log[-10:]   # keep last 10
        if self._csuite_manager:
            await self._csuite_manager.receive_alert("CircuitBreaker", level, message)
        elif self._ceo:
            if details:
                await self._ceo.dispatch_alert(level, message, details)
            else:
                await self._ceo.dispatch_alert(level, message)

    # ── Helpers ────────────────────────────────────────────────────

    def _get_todays_realized_pnl(self) -> float:
        """Query trade_records for today's realized P&L from closed positions."""
        try:
            import sqlite3
            from datetime import date
            conn = sqlite3.connect(str(self._settings.db_path), check_same_thread=False, timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
            row = conn.execute(
                "SELECT SUM(realized_pnl) FROM trade_records WHERE close_date=?",
                (date.today().isoformat(),),
            ).fetchone()
            conn.close()
            return float(row[0] or 0) if row else 0.0
        except Exception:
            return 0.0

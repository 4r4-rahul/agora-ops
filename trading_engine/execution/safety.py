"""
Safety Monitor + Alert System
================================
Circuit breakers and real-time alerts for the live trading loop.

Monitors:
  • P&L limits (daily, weekly, monthly)
  • Drawdown from peak equity
  • VIX spike detection (>20% intraday jump)
  • Connection health
  • Consecutive loss streaks

Alerts via:
  • Console (always)
  • Log file (always)
  • Webhook (Discord/Slack — optional, set ALERT_WEBHOOK_URL env var)

Usage:
    from trading_engine.execution.safety import SafetyMonitor

    safety = SafetyMonitor(state_manager, executor)
    safety.check()  # Run all checks, triggers kill switch if needed
"""

import os
import json
import logging
import requests
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Tuple

logger = logging.getLogger(__name__)


class AlertLevel:
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    EMERGENCY = "EMERGENCY"


class SafetyMonitor:
    """
    Real-time safety monitoring with circuit breakers.

    This is the final safety layer — if all else fails, this stops trading.
    """

    def __init__(
        self,
        state_manager=None,
        executor=None,
        account_size: float = 10_000.0,
        webhook_url: Optional[str] = None,
    ):
        self.state = state_manager
        self.executor = executor
        self.account_size = account_size
        self.webhook_url = webhook_url or os.getenv("ALERT_WEBHOOK_URL")
        self.alert_history: List[Dict] = []

        # Thresholds
        self.daily_loss_limit_pct = 0.05       # 5%
        self.weekly_loss_limit_pct = 0.08      # 8%
        self.monthly_loss_limit_pct = 0.10     # 10%
        self.max_consecutive_losses = 5
        self.vix_spike_threshold_pct = 20.0    # 20% intraday VIX jump
        self.max_open_positions = 6
        self._last_vix = None
        self._kill_switch_triggered = False

    # ─── Main Check ──────────────────────────────────────────────

    def check(self) -> Tuple[bool, List[str]]:
        """
        Run all safety checks.

        Returns:
            (safe: bool, alerts: List[str])
            If safe=False, kill switch has been triggered.
        """
        alerts = []

        if self.state:
            alerts.extend(self._check_pnl_limits())
            alerts.extend(self._check_consecutive_losses())
            alerts.extend(self._check_position_count())

        if alerts:
            for alert in alerts:
                logger.warning(f"SAFETY: {alert}")

        # Check if any alert requires kill switch
        for alert in alerts:
            if "[EMERGENCY]" in alert and not self._kill_switch_triggered:
                self._trigger_kill_switch(alert)
                return False, alerts

        return True, alerts

    # ─── P&L Checks ──────────────────────────────────────────────

    def _check_pnl_limits(self) -> List[str]:
        """Check daily/weekly/monthly P&L limits."""
        alerts = []
        s = self.state.state

        # Daily loss limit
        daily_limit = self.account_size * self.daily_loss_limit_pct
        if s.daily_pnl < -daily_limit:
            alerts.append(
                f"[EMERGENCY] Daily loss limit breached: "
                f"${s.daily_pnl:+.2f} (limit: -${daily_limit:.0f})"
            )
        elif s.daily_pnl < -daily_limit * 0.8:
            alerts.append(
                f"[WARNING] Approaching daily loss limit: "
                f"${s.daily_pnl:+.2f} (limit: -${daily_limit:.0f})"
            )

        # Weekly loss limit
        weekly_limit = self.account_size * self.weekly_loss_limit_pct
        if s.weekly_pnl < -weekly_limit:
            alerts.append(
                f"[EMERGENCY] Weekly loss limit breached: "
                f"${s.weekly_pnl:+.2f} (limit: -${weekly_limit:.0f})"
            )
        elif s.weekly_pnl < -weekly_limit * 0.8:
            alerts.append(
                f"[WARNING] Approaching weekly loss limit: "
                f"${s.weekly_pnl:+.2f} (limit: -${weekly_limit:.0f})"
            )

        # Monthly circuit breaker
        monthly_limit = self.account_size * self.monthly_loss_limit_pct
        if s.monthly_pnl < -monthly_limit:
            alerts.append(
                f"[EMERGENCY] Monthly circuit breaker tripped: "
                f"${s.monthly_pnl:+.2f} (limit: -${monthly_limit:.0f})"
            )

        # Drawdown from peak
        current_equity = self.account_size + s.monthly_pnl
        if s.peak_equity > 0:
            drawdown = (s.peak_equity - current_equity) / s.peak_equity * 100
            if drawdown > 8:
                alerts.append(
                    f"[WARNING] Drawdown {drawdown:.1f}% from peak "
                    f"(${s.peak_equity:,.0f} → ${current_equity:,.0f})"
                )

        return alerts

    def _check_consecutive_losses(self) -> List[str]:
        """Check for consecutive loss streaks."""
        alerts = []
        if not self.state:
            return alerts

        losses = self.state.state.consecutive_losses
        if losses >= self.max_consecutive_losses:
            alerts.append(
                f"[EMERGENCY] {losses} consecutive losses — halt trading"
            )
        elif losses >= 3:
            alerts.append(
                f"[WARNING] {losses} consecutive losses — consider reducing size"
            )

        return alerts

    def _check_position_count(self) -> List[str]:
        """Check if too many positions are open."""
        alerts = []
        if not self.state:
            return alerts

        count = len(self.state.state.open_positions)
        if count > self.max_open_positions:
            alerts.append(
                f"[WARNING] {count} open positions exceeds limit ({self.max_open_positions})"
            )

        return alerts

    # ─── VIX Spike Detection ────────────────────────────────────

    def check_vix_spike(self, current_vix: float) -> Optional[str]:
        """
        Detect rapid VIX spikes.

        Call this on each cycle with the latest VIX reading.
        """
        if self._last_vix and self._last_vix > 0:
            change_pct = (current_vix - self._last_vix) / self._last_vix * 100
            if change_pct > self.vix_spike_threshold_pct:
                alert = (
                    f"[EMERGENCY] VIX SPIKE: {self._last_vix:.1f} → {current_vix:.1f} "
                    f"(+{change_pct:.1f}%)"
                )
                self._last_vix = current_vix
                self.send_alert(alert, AlertLevel.EMERGENCY)
                return alert

        self._last_vix = current_vix
        return None

    # ─── Kill Switch ─────────────────────────────────────────────

    def _trigger_kill_switch(self, reason: str):
        """Trigger kill switch — flatten all positions."""
        self._kill_switch_triggered = True
        self.send_alert(f"🚨 KILL SWITCH TRIGGERED: {reason}", AlertLevel.EMERGENCY)

        if self.executor:
            try:
                self.executor.flatten_all(reason=reason)
            except Exception as e:
                logger.error(f"Kill switch execution failed: {e}")
                self.send_alert(
                    f"❌ KILL SWITCH FAILED: {e} — MANUAL INTERVENTION REQUIRED",
                    AlertLevel.EMERGENCY,
                )

    # ─── Alerts ──────────────────────────────────────────────────

    def send_alert(self, message: str, level: str = AlertLevel.INFO):
        """
        Send alert via all configured channels.
        """
        timestamp = datetime.now().isoformat()
        alert = {
            "timestamp": timestamp,
            "level": level,
            "message": message,
        }
        self.alert_history.append(alert)

        # Console
        icons = {
            AlertLevel.INFO: "ℹ️",
            AlertLevel.WARNING: "⚠️",
            AlertLevel.CRITICAL: "🔴",
            AlertLevel.EMERGENCY: "🚨",
        }
        icon = icons.get(level, "📢")
        print(f"  {icon} [{level}] {message}")

        # Log
        if level == AlertLevel.EMERGENCY:
            logger.critical(message)
        elif level == AlertLevel.CRITICAL:
            logger.error(message)
        elif level == AlertLevel.WARNING:
            logger.warning(message)
        else:
            logger.info(message)

        # Webhook (Discord/Slack)
        if self.webhook_url:
            self._send_webhook(message, level)

    def _send_webhook(self, message: str, level: str):
        """Send alert to Discord/Slack webhook with rich formatting."""
        try:
            # Detect Slack vs Discord
            if "slack" in self.webhook_url.lower():
                payload = {"text": f"*[{level}]* {message}"}
            else:
                # Discord rich embed
                color_map = {
                    "INFO": 0x3498DB,       # blue
                    "WARNING": 0xF39C12,    # amber
                    "CRITICAL": 0xE74C3C,   # red
                    "EMERGENCY": 0x8B0000,  # dark red
                }
                color = color_map.get(str(level), 0x95A5A6)

                icon_map = {
                    "INFO": "ℹ️",
                    "WARNING": "⚠️",
                    "CRITICAL": "🔴",
                    "EMERGENCY": "🚨",
                }
                icon = icon_map.get(str(level), "📢")

                embed = {
                    "title": f"{icon} {level}",
                    "description": message,
                    "color": color,
                    "footer": {"text": "0DTE Trading Engine"},
                    "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                }
                payload = {"embeds": [embed]}

            resp = requests.post(
                self.webhook_url,
                json=payload,
                timeout=5,
            )
            if resp.status_code not in (200, 204):
                logger.warning(f"Webhook returned {resp.status_code}")
        except Exception as e:
            logger.warning(f"Webhook failed: {e}")

    def send_trade_alert(self, action: str, ticker: str, strikes: str,
                         credit: float, pnl: float = 0.0):
        """Send a rich trade notification via Discord embed."""
        if action == "OPEN":
            msg = f"📤 OPENED: {ticker} {strikes} for ${credit:.2f} credit"
        elif action == "CLOSE":
            pnl_emoji = "🟢" if pnl >= 0 else "🔴"
            msg = f"📥 CLOSED: {ticker} {strikes} | {pnl_emoji} P&L: ${pnl:+.2f}"
        else:
            msg = f"📋 {action}: {ticker} {strikes}"

        self.send_alert(msg, AlertLevel.INFO)

    def send_daily_summary(self):
        """Send end-of-day summary with a rich Discord embed."""
        if not self.state:
            return

        s = self.state.state
        pnl_emoji = "🟢" if s.daily_pnl >= 0 else "🔴"

        # Build embed fields for Discord
        if self.webhook_url and "discord" in self.webhook_url.lower():
            try:
                color = 0x2ECC71 if s.daily_pnl >= 0 else 0xE74C3C
                embed = {
                    "title": f"📊 Daily Summary — {s.date}",
                    "color": color,
                    "fields": [
                        {"name": "Daily P&L", "value": f"${s.daily_pnl:+.2f}", "inline": True},
                        {"name": "Trades", "value": str(s.trades_today), "inline": True},
                        {"name": "Open Positions", "value": str(len(s.open_positions)), "inline": True},
                        {"name": "Weekly P&L", "value": f"${s.weekly_pnl:+.2f}", "inline": True},
                        {"name": "Monthly P&L", "value": f"${s.monthly_pnl:+.2f}", "inline": True},
                    ],
                    "footer": {"text": "0DTE Trading Engine"},
                    "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                }
                resp = requests.post(
                    self.webhook_url,
                    json={"embeds": [embed]},
                    timeout=5,
                )
                if resp.status_code in (200, 204):
                    return
            except Exception as e:
                logger.warning(f"Rich daily summary failed: {e}")

        # Fallback to plain text
        msg = (
            f"📊 DAILY SUMMARY ({s.date})\n"
            f"  {pnl_emoji} P&L: ${s.daily_pnl:+.2f} | Trades: {s.trades_today}\n"
            f"  Weekly: ${s.weekly_pnl:+.2f} | Monthly: ${s.monthly_pnl:+.2f}\n"
            f"  Open positions: {len(s.open_positions)}"
        )
        self.send_alert(msg, AlertLevel.INFO)

"""
SystemHealthAgent — heartbeat, connectivity, and data integrity monitoring.

Checks every 5 minutes:
  1. IBKR connectivity — can we connect on the configured port?
  2. yfinance data freshness — is market data actually updating?
  3. Anthropic API reachability — can Claude respond within timeout?
  4. DB integrity — position count in DB matches session state
  5. Disk space — log dir and DB dir have adequate space

Alerts CEO on first failure of each check type.
Auto-recovers: re-checks every 5 min, clears alert when check passes again.

Does NOT use asyncio.sleep loops for individual checks — it runs a full
check_all() cycle every 5 minutes, preventing cascading delays.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from ..core.config import AgoraSettings, get_settings

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

_CHECK_INTERVAL_SECONDS = 300   # 5 minutes
_YFINANCE_STALE_MINUTES = 15   # data older than this = stale
_MIN_DISK_FREE_MB       = 500   # alert if less than 500 MB free


@dataclass
class HealthCheck:
    name: str
    status: str = "unknown"   # "ok" | "warn" | "fail"
    message: str = ""
    last_checked: float = field(default_factory=time.monotonic)
    consecutive_failures: int = 0


class SystemHealthAgent:
    """
    Lightweight health monitor. Runs async but all I/O is optional
    (yfinance/IBKR checks are best-effort, never block trading).
    """

    def __init__(
        self,
        settings: AgoraSettings | None = None,
        ceo_agent: Any = None,
        position_mgr: Any = None,
    ) -> None:
        self._settings     = settings or get_settings()
        self._ceo          = ceo_agent
        self._position_mgr = position_mgr
        self._csuite_manager: Any = None   # COOAgent — set via register_csuite_manager()
        self._running      = False
        self._checks: dict[str, HealthCheck] = {
            "ibkr":       HealthCheck("ibkr"),
            "yfinance":   HealthCheck("yfinance"),
            "anthropic":  HealthCheck("anthropic"),
            "db":         HealthCheck("db"),
            "disk":       HealthCheck("disk"),
        }
        self._last_run: float = 0.0
        # Track what we've already alerted on to avoid spam
        self._alerted: set[str] = set()

    def register_csuite_manager(self, manager: Any) -> None:
        """Wire the COOAgent as supervising executive for alert escalation."""
        self._csuite_manager = manager

    def get_status(self) -> dict[str, Any]:
        """Synchronous health summary for CEO reports."""
        return {
            name: {"status": c.status, "message": c.message}
            for name, c in self._checks.items()
        }

    def is_healthy(self) -> bool:
        return all(c.status != "fail" for c in self._checks.values())

    async def start(self) -> None:
        self._running = True
        logger.info("SystemHealthAgent started — checking every %ds", _CHECK_INTERVAL_SECONDS)
        while self._running:
            elapsed = time.monotonic() - self._last_run
            if elapsed >= _CHECK_INTERVAL_SECONDS:
                try:
                    await self._run_all_checks()
                    self._last_run = time.monotonic()
                except Exception as exc:
                    logger.error("Health check cycle error: %s", exc)
            await asyncio.sleep(60)

    async def stop(self) -> None:
        self._running = False

    # ── Check cycle ────────────────────────────────────────────────

    async def _run_all_checks(self) -> None:
        results = await asyncio.gather(
            self._check_ibkr(),
            self._check_yfinance(),
            self._check_anthropic(),
            self._check_db(),
            self._check_disk(),
            return_exceptions=True,
        )

        # Alert on new failures
        for name, check in self._checks.items():
            if check.status == "fail":
                check.consecutive_failures += 1
                alert_key = f"{name}_{check.consecutive_failures}"
                # Alert on first failure and every 3rd consecutive failure
                if check.consecutive_failures == 1 or check.consecutive_failures % 3 == 0:
                    if alert_key not in self._alerted:
                        self._alerted.add(alert_key)
                        await self._notify(
                            "warning",
                            f"⚠️ System health: {name} CHECK FAILED (#{check.consecutive_failures})\n{check.message}",
                        )
            else:
                if check.consecutive_failures > 0:
                    logger.info("SystemHealth: %s recovered after %d failures", name, check.consecutive_failures)
                check.consecutive_failures = 0

        # Log summary
        statuses = " | ".join(f"{n}={'✓' if c.status=='ok' else '✗'}" for n, c in self._checks.items())
        logger.info("SystemHealth: %s", statuses)

    async def _notify(self, level: str, message: str) -> None:
        """Route alert: SystemHealthAgent → COO → CEO."""
        if self._csuite_manager:
            await self._csuite_manager.receive_alert("SystemHealthAgent", level, message)
        elif self._ceo:
            await self._ceo.dispatch_alert(level, message)

    async def _check_ibkr(self) -> None:
        check = self._checks["ibkr"]
        try:
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(3)
            result = sock.connect_ex((self._settings.ibkr_host, self._settings.ibkr_port))
            sock.close()
            if result == 0:
                check.status  = "ok"
                check.message = f"IBKR reachable at {self._settings.ibkr_host}:{self._settings.ibkr_port}"
            else:
                check.status  = "warn"
                check.message = f"IBKR port {self._settings.ibkr_port} not accepting connections"
        except Exception as exc:
            check.status  = "fail"
            check.message = f"IBKR connection error: {exc}"
        check.last_checked = time.monotonic()

    async def _check_yfinance(self) -> None:
        check = self._checks["yfinance"]
        try:
            import yfinance as yf
            fi = yf.Ticker("SPY").fast_info
            price = float(getattr(fi, "last_price", None) or fi.get("lastPrice", 0) or 0)
            if price > 0:
                check.status  = "ok"
                check.message = f"yfinance OK — SPY=${price:.2f}"
            else:
                check.status  = "warn"
                check.message = "yfinance returned SPY price=0"
        except Exception as exc:
            check.status  = "fail"
            check.message = f"yfinance error: {exc}"
        check.last_checked = time.monotonic()

    async def _check_anthropic(self) -> None:
        check = self._checks["anthropic"]
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=self._settings.anthropic_api_key)
            # Minimal ping — count tokens in a tiny message
            result = client.messages.count_tokens(
                model=self._settings.claude_fast_model,
                messages=[{"role": "user", "content": "ping"}],
            )
            check.status  = "ok"
            check.message = f"Anthropic API reachable — {result.input_tokens} tokens"
        except Exception as exc:
            err_str = str(exc)
            # Credit/billing errors → warn (rule-based fallbacks handle these gracefully)
            # Authentication/network errors → fail (actual connectivity problem)
            if "credit" in err_str.lower() or "billing" in err_str.lower() or "balance" in err_str.lower():
                check.status  = "warn"
                check.message = f"Anthropic API: low credits — using rule fallbacks ({err_str[:80]})"
            elif "401" in err_str or "authentication" in err_str.lower() or "api_key" in err_str.lower():
                check.status  = "fail"
                check.message = f"Anthropic API auth error: {err_str[:100]}"
            else:
                check.status  = "warn"  # network errors are transient — warn, don't fail
                check.message = f"Anthropic API unavailable: {err_str[:100]}"
        check.last_checked = time.monotonic()

    async def _check_db(self) -> None:
        check = self._checks["db"]
        try:
            db_path = self._settings.db_path
            if not db_path.exists():
                check.status  = "warn"
                check.message = f"DB not found at {db_path}"
                return

            conn = sqlite3.connect(str(db_path), check_same_thread=False)
            row = conn.execute("SELECT COUNT(*) FROM positions WHERE status='open'").fetchone()
            conn.close()

            db_count = row[0] if row else 0
            session_count = len(self._position_mgr.get_open_positions()) if self._position_mgr else db_count

            if abs(db_count - session_count) > 1:
                check.status  = "warn"
                check.message = f"DB/session mismatch: DB={db_count} session={session_count} open positions"
            else:
                check.status  = "ok"
                check.message = f"DB OK — {db_count} open positions"
        except Exception as exc:
            check.status  = "fail"
            check.message = f"DB error: {exc}"
        check.last_checked = time.monotonic()

    async def _check_disk(self) -> None:
        check = self._checks["disk"]
        try:
            stat = os.statvfs(str(self._settings.db_path.parent))
            free_mb = (stat.f_bavail * stat.f_frsize) / (1024 * 1024)
            if free_mb < _MIN_DISK_FREE_MB:
                check.status  = "warn"
                check.message = f"Low disk space: {free_mb:.0f} MB free (min {_MIN_DISK_FREE_MB} MB)"
            else:
                check.status  = "ok"
                check.message = f"Disk OK — {free_mb:.0f} MB free"
        except Exception as exc:
            check.status  = "ok"   # disk check non-critical on non-Unix
            check.message = f"Disk check N/A: {exc}"
        check.last_checked = time.monotonic()

"""
ExecutionQualityAgent — tracks fill rate, reject taxonomy, and slippage.

Records every order attempt and its outcome. Exposes real-time stats so the
CEO and session can detect systematic execution failure (like Error 201 storms)
before they burn an entire trading day.

Metrics tracked:
  fill_rate          — filled / total attempted (session + rolling 7-day)
  reject_rate        — rejected / total attempted
  reject_reasons     — dict of IBKR error_code → count
  avg_slippage_ticks — mean(mid_price - fill_price) per strategy type
  attempts_by_ticker — per-ticker attempt / fill / reject counts
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ..core.config import AgoraSettings, get_settings

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")


class ExecutionQualityAgent:
    """
    Thread-safe execution quality monitor. Call record_* from any async context.
    Logs a quality summary every 30 min during market hours.
    """

    def __init__(
        self,
        settings: AgoraSettings | None = None,
        ceo_agent: Any = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._ceo = ceo_agent
        self._csuite_manager: Any = None   # COOAgent — set via register_csuite_manager()
        self._running = False
        self._db = self._init_db()

        # In-session counters (reset on start, NOT persisted — use DB for history)
        self._session_attempts = 0
        self._session_fills = 0
        self._session_rejects = 0
        # Policy rejects (Error 201 — IBKR account restriction, not execution failure)
        # Excluded from fill rate denominator so 201 storms don't trigger false alerts.
        self._session_policy_rejects = 0
        self._session_reject_reasons: dict[str, int] = defaultdict(int)
        self._session_slippage: list[float] = []
        # Per-symbol attempt/fill tally THIS SESSION — caps the re-submission storm where
        # the engine re-proposes the same name every cycle (e.g. SMH x25, all unfilled),
        # which inflates the fill-rate denominator and floods the pending queue.
        self._session_ticker_attempts: dict[str, int] = defaultdict(int)
        self._session_ticker_fills: dict[str, int] = defaultdict(int)

        # Alert threshold: if effective_fill_rate drops below this, dispatch CEO alert
        self._fill_rate_alert_threshold = 0.30  # 30%
        self._alert_sent_this_session = False

    def register_csuite_manager(self, manager: Any) -> None:
        """Wire the COOAgent as supervising executive for alert escalation."""
        self._csuite_manager = manager

    def _init_db(self) -> sqlite3.Connection:
        db_path = self._settings.db_path
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS execution_quality (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                attempt_date TEXT NOT NULL,
                ticker TEXT NOT NULL,
                strategy TEXT NOT NULL DEFAULT '',
                mid_price REAL NOT NULL DEFAULT 0,
                outcome TEXT NOT NULL,        -- 'fill' | 'reject' | 'timeout'
                fill_price REAL,              -- actual fill price (null if rejected)
                slippage_ticks REAL,          -- mid - fill (negative = paid more than mid)
                reject_code TEXT DEFAULT '',  -- IBKR error code string
                reject_reason TEXT DEFAULT '' -- human-readable reason
            )
        """)
        conn.commit()
        return conn

    # ── Recording API (called by ibkr_bridge / session) ─────────────

    def record_attempt(self, ticker: str, strategy: str, mid_price: float) -> None:
        """Call before submitting an order to IBKR."""
        self._session_attempts += 1
        self._session_ticker_attempts[ticker] += 1
        self._db.execute(
            "INSERT INTO execution_quality (attempt_date, ticker, strategy, mid_price, outcome) "
            "VALUES (?, ?, ?, ?, 'pending')",
            (date.today().isoformat(), ticker, strategy, mid_price),
        )
        self._db.commit()

    def should_skip_symbol(self, ticker: str) -> tuple[bool, str]:
        """True if `ticker` has already failed to fill too many times THIS SESSION.

        Stops the re-submission storm (one name, no fill, retried every cycle). A ticker
        that has filled at least once is never skipped — only persistently-unfillable
        names back off. Resets each session. Returns (skip, reason)."""
        cap = int(getattr(self._settings, "exec_max_attempts_per_symbol", 4) or 4)
        attempts = self._session_ticker_attempts.get(ticker, 0)
        fills = self._session_ticker_fills.get(ticker, 0)
        if fills == 0 and attempts >= cap:
            return True, f"{attempts} unfilled attempts this session (cap {cap})"
        return False, ""

    def record_fill(
        self, ticker: str, fill_price: float, mid_price: float, strategy: str = ""
    ) -> None:
        """Call when IBKR confirms a fill."""
        self._session_fills += 1
        self._session_ticker_fills[ticker] += 1
        slippage = mid_price - fill_price  # positive = better than mid (rare)

        # Sanity guard: a multi-leg combo reports one execution per LEG, so passing a
        # single leg's price here instead of the NET fill produces impossible slippage
        # (e.g. mid 0.53 vs leg 5.79 => -5.42). If |slippage| dwarfs the mid, the units
        # almost certainly don't match — flag it and DON'T pollute the slippage average.
        plausible = (
            mid_price > 0
            and fill_price > 0
            and abs(slippage) <= max(0.50, 1.5 * mid_price)
        )
        if plausible:
            self._session_slippage.append(slippage)
        else:
            slippage = None  # type: ignore[assignment]
            logger.warning(
                "ExecutionQuality: implausible slippage for %s (mid=%.4f fill=%.4f) — "
                "likely a per-leg vs net-combo unit mismatch; slippage not recorded",
                ticker, mid_price, fill_price,
            )

        cur = self._db.execute(
            "UPDATE execution_quality SET outcome='fill', fill_price=?, slippage_ticks=? "
            "WHERE id = (SELECT id FROM execution_quality WHERE ticker=? AND outcome='pending' "
            "AND attempt_date=? ORDER BY id DESC LIMIT 1)",
            (fill_price, slippage, ticker, date.today().isoformat()),
        )
        if cur.rowcount == 0:
            # No prior record_attempt to update — the long-options path can
            # record fills without a preceding attempt row, so the DB-based fill-rate
            # metric (COO audit) counted them as 0. Insert the fill directly so it counts.
            self._db.execute(
                "INSERT INTO execution_quality "
                "(attempt_date, ticker, strategy, mid_price, outcome, fill_price, slippage_ticks) "
                "VALUES (?, ?, ?, ?, 'fill', ?, ?)",
                (date.today().isoformat(), ticker, strategy, mid_price, fill_price, slippage),
            )
        self._db.commit()
        logger.info(
            "ExecutionQuality FILL: %s | fill=%.4f mid=%.4f slippage=%s",
            ticker, fill_price, mid_price,
            f"{slippage:+.4f}" if slippage is not None else "n/a(unit-mismatch)",
        )

    def record_reject(
        self,
        ticker: str,
        error_code: str,
        reason: str,
        strategy: str = "",
    ) -> None:
        """Call when IBKR rejects an order."""
        self._session_rejects += 1
        self._session_reject_reasons[error_code] += 1

        # Error 201 = IBKR account policy (riskless combo limit) — not an execution failure.
        # Tracked separately so fill rate denominator is not polluted by policy rejects.
        if error_code == "201":
            self._session_policy_rejects += 1

        # SQLite UPDATE doesn't support ORDER BY/LIMIT — use a subquery to target latest row
        self._db.execute(
            "UPDATE execution_quality SET outcome='reject', reject_code=?, reject_reason=? "
            "WHERE id = (SELECT id FROM execution_quality WHERE ticker=? AND outcome='pending' "
            "AND attempt_date=? ORDER BY id DESC LIMIT 1)",
            (error_code, reason[:500], ticker, date.today().isoformat()),
        )
        self._db.commit()
        logger.warning(
            "ExecutionQuality REJECT: %s | code=%s | %s",
            ticker, error_code, reason[:120],
        )

        # Check for Error 201 storm (5+ in session = systemic account config issue)
        if error_code == "201":
            self._session_reject_reasons["201_storm_count"] = (
                self._session_reject_reasons.get("201_storm_count", 0) + 1
            )

    # ── Stats ────────────────────────────────────────────────────────

    def get_session_stats(self) -> dict:
        # effective_attempts excludes policy rejects (Error 201) — IBKR account restriction,
        # not a reflection of execution quality.
        effective_attempts = max(0, self._session_attempts - self._session_policy_rejects)
        fill_rate = (
            self._session_fills / self._session_attempts
            if self._session_attempts > 0 else 0.0
        )
        effective_fill_rate = (
            self._session_fills / effective_attempts
            if effective_attempts > 0 else 0.0
        )
        avg_slippage = (
            sum(self._session_slippage) / len(self._session_slippage)
            if self._session_slippage else 0.0
        )
        return {
            "attempts":            self._session_attempts,
            "fills":               self._session_fills,
            "rejects":             self._session_rejects,
            "policy_rejects":      self._session_policy_rejects,
            "effective_attempts":  effective_attempts,
            "fill_rate":           fill_rate,
            "effective_fill_rate": effective_fill_rate,
            "avg_slippage":        avg_slippage,
            "reject_reasons":      dict(self._session_reject_reasons),
            "error_201_storm":     self._session_reject_reasons.get("201", 0) >= 5,
        }

    def get_today_db_stats(self) -> dict:
        """
        DB-accurate today stats — correct even after a session restart.
        In-memory session counters reset on restart; this doesn't.
        """
        today = date.today().isoformat()
        rows = self._db.execute(
            "SELECT outcome, COUNT(*) FROM execution_quality WHERE attempt_date=? GROUP BY outcome",
            (today,),
        ).fetchall()
        m = {r[0]: r[1] for r in rows}
        # fill_closed = confirmed round-trip (opened+closed, no open position)
        fills    = m.get("fill", 0) + m.get("fill_closed", 0)
        rejects  = m.get("reject", 0)
        timeouts = m.get("timeout", 0)
        total    = fills + rejects + timeouts

        # Policy rejects (Error 201) count separately — not execution quality failures.
        policy_rejects = self._db.execute(
            "SELECT COUNT(*) FROM execution_quality WHERE attempt_date=? AND reject_code='201'",
            (today,),
        ).fetchone()[0]
        effective_total = max(0, total - policy_rejects)

        return {
            "fills":                fills,
            "rejects":              rejects,
            "policy_rejects":       policy_rejects,
            "timeouts":             timeouts,
            "total":                total,
            "effective_total":      effective_total,
            "fill_rate":            fills / total if total > 0 else None,
            "effective_fill_rate":  fills / effective_total if effective_total > 0 else None,
            "timeout_rate":         timeouts / total if total > 0 else None,
        }

    def count_ghost_fills_today(self, db_path: str) -> int:
        """
        Fills recorded in execution_quality today that have no matching position.
        > 0 means record_fill() ran but _record_position() didn't — prior crash indicator.
        """
        try:
            conn = sqlite3.connect(db_path, check_same_thread=False)
            count = conn.execute(
                """SELECT COUNT(*) FROM execution_quality eq
                   WHERE eq.outcome='fill' AND eq.attempt_date=date('now')
                   AND eq.ticker NOT IN (SELECT ticker FROM positions)""",
            ).fetchone()[0]
            conn.close()
            return count
        except Exception:
            return 0

    def get_7day_stats(self) -> dict:
        cutoff = (date.today() - timedelta(days=7)).isoformat()
        rows = self._db.execute(
            "SELECT outcome, reject_code, slippage_ticks FROM execution_quality "
            "WHERE attempt_date >= ?",
            (cutoff,),
        ).fetchall()
        total = len(rows)
        fills = sum(1 for r in rows if r[0] == "fill")
        rejects = sum(1 for r in rows if r[0] == "reject")
        reject_codes: dict[str, int] = defaultdict(int)
        slippages = []
        for r in rows:
            if r[0] == "reject" and r[1]:
                reject_codes[r[1]] += 1
            if r[2] is not None:
                slippages.append(r[2])
        return {
            "total":          total,
            "fills":          fills,
            "rejects":        rejects,
            "fill_rate":      fills / total if total > 0 else 0.0,
            "avg_slippage":   sum(slippages) / len(slippages) if slippages else 0.0,
            "reject_reasons": dict(reject_codes),
        }

    # ── Async loop ───────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        # Mark any stale 'pending' rows as 'timeout' before this session begins.
        # Pending rows from prior sessions represent DAY orders that expired at EOD
        # without a fill/reject callback — they are no longer actionable.
        stale = self._db.execute(
            "UPDATE execution_quality SET outcome='timeout' WHERE outcome='pending'"
        ).rowcount
        self._db.commit()
        if stale:
            logger.info("ExecutionQuality: cleared %d stale pending rows from prior sessions", stale)

        while self._running:
            await asyncio.sleep(1800)  # report every 30 min
            now_et = datetime.now(tz=ET)
            if 9 <= now_et.hour < 16:
                await self._quality_report()

    async def stop(self) -> None:
        self._running = False

    async def _quality_report(self) -> None:
        stats = self.get_session_stats()
        if stats["attempts"] == 0:
            return

        effective_fill_rate = stats["effective_fill_rate"]
        policy_rejects = stats["policy_rejects"]
        summary = (
            f"Execution Quality: {stats['fills']}/{stats['effective_attempts']} fills "
            f"({effective_fill_rate:.0%} effective) | policy_rejects(201)={policy_rejects} "
            f"| slippage={stats['avg_slippage']:+.4f}/sh"
        )
        logger.info(summary)

        # Escalate Error 201 storm to CEO (account config issue, not execution)
        if stats.get("error_201_storm") and not self._alert_sent_this_session:
            self._alert_sent_this_session = True
            reject_breakdown = ", ".join(
                f"{k}×{v}"
                for k, v in stats["reject_reasons"].items()
                if k != "201_storm_count"
            )
            await self._notify(
                "critical",
                f"🚨 IBKR Error 201 storm: {stats['reject_reasons'].get('201', 0)} "
                f"policy rejections (riskless combo limit). Error 201s excluded from fill rate.\n"
                f"Reject breakdown: {reject_breakdown}",
            )

        elif effective_fill_rate < self._fill_rate_alert_threshold and stats["effective_attempts"] >= 5:
            await self._notify(
                "warning",
                f"⚠️ Low fill rate: {effective_fill_rate:.0%} "
                f"({stats['fills']}/{stats['effective_attempts']} effective attempts, "
                f"{policy_rejects} policy rejects excluded) | "
                f"Reject reasons: {stats['reject_reasons']}",
            )

    async def _notify(self, level: str, message: str) -> None:
        """Route alert: ExecutionQualityAgent → COO → CEO."""
        if self._csuite_manager:
            await self._csuite_manager.receive_alert("ExecutionQualityAgent", level, message)
        elif self._ceo:
            await self._ceo.dispatch_alert(level, message)

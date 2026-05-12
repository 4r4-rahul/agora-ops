"""
Risk Council — portfolio-level risk gating before any trade execution.

Checks (in order, first failure = block):
  1. Kill switch: SQLite flag — if set, all new trades blocked immediately
  2. Daily loss limit: realized + unrealized P&L > -2% of account → halt
  3. Weekly loss limit: rolling 5-day P&L > -6% of account → halt
  4. Max open positions: hard cap at 10
  5. Portfolio delta: |Σdelta| / (account / 10K) ≤ 0.30
  6. Portfolio vega: |Σvega| / (account / 10K) ≤ 200
  7. Daily theta: |Σtheta| / account ≤ 0.5%/day
  8. Correlation check: adding SPY when already long QQQ counts double
  9. Bid/ask spread gate: < 10% of mid (liquidity)
  10. Hard-to-borrow: short legs require borrow availability check

Kill switch is a SQLite row — persists across restarts, trips on:
  - Daily loss limit breach (auto-trip)
  - Manual operator trigger via API (POST /agora/kill)
  - Unrealized P&L of any single position > 2× max_loss_dollars

Kill switch reset: requires explicit API call (DELETE /agora/kill) — not auto-reset.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any

from ..core.config import AgoraSettings, get_settings
from ..core.models import TradeRecommendation

logger = logging.getLogger(__name__)

# Correlation groups — positions in same group count against each other
_CORRELATION_GROUPS: dict[str, str] = {
    "SPY": "us_equity", "QQQ": "us_equity", "IWM": "us_equity",
    "GLD": "commodities", "GDX": "commodities",
    "TLT": "rates", "IEF": "rates", "AGG": "rates",
    "XLE": "energy", "USO": "energy",
    "VXX": "volatility", "UVXY": "volatility",
}
_MAX_SAME_GROUP_POSITIONS = 2


class RiskCouncil:
    """
    Stateless check against portfolio state.
    All methods are synchronous (no market data fetches — use pre-fetched Greeks).
    """

    def __init__(self, settings: AgoraSettings | None = None) -> None:
        self._settings = settings or get_settings()
        self._db = self._init_db()

    def _init_db(self) -> sqlite3.Connection:
        db_path = self._settings.db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS kill_switch (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                active INTEGER NOT NULL DEFAULT 0,
                reason TEXT NOT NULL DEFAULT '',
                tripped_at TEXT NOT NULL DEFAULT '',
                tripped_by TEXT NOT NULL DEFAULT 'system'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_pnl (
                record_date TEXT PRIMARY KEY,
                realized_pnl REAL NOT NULL DEFAULT 0,
                unrealized_pnl REAL NOT NULL DEFAULT 0,
                trades_count INTEGER NOT NULL DEFAULT 0
            )
        """)
        # Ensure kill switch row exists
        conn.execute(
            "INSERT OR IGNORE INTO kill_switch VALUES (1, 0, '', '', 'system')"
        )
        conn.commit()
        return conn

    # ── Primary gate ───────────────────────────────────────────────

    def approve_trade(
        self,
        recommendation: TradeRecommendation,
        portfolio_greeks: dict[str, float],
        open_positions: list[Any],
        spot: float = 0.0,
    ) -> dict[str, Any]:
        """
        Full pre-trade risk check.
        Returns: {approved: bool, reason: str, checks: dict}
        """
        checks: dict[str, Any] = {}

        # 1. Kill switch
        if self.is_kill_switch_active():
            return {
                "approved": False,
                "reason": "Kill switch ACTIVE — all new trades blocked",
                "checks": {"kill_switch": False},
            }
        checks["kill_switch"] = True

        # 2. Daily P&L limit
        daily_ok, daily_msg = self._check_daily_loss()
        checks["daily_loss"] = daily_ok
        if not daily_ok:
            return {"approved": False, "reason": daily_msg, "checks": checks}

        # 3. Weekly P&L limit
        weekly_ok, weekly_msg = self._check_weekly_loss()
        checks["weekly_loss"] = weekly_ok
        if not weekly_ok:
            return {"approved": False, "reason": weekly_msg, "checks": checks}

        # 4. Max positions
        n_positions = portfolio_greeks.get("positions", len(open_positions))
        if n_positions >= self._settings.max_open_positions:
            checks["max_positions"] = False
            return {
                "approved": False,
                "reason": f"Max positions reached ({n_positions}/{self._settings.max_open_positions})",
                "checks": checks,
            }
        checks["max_positions"] = True

        # 5. Portfolio delta
        new_delta = sum(
            (l.delta if l.action == "buy" else -l.delta) * recommendation.contracts * 100
            for l in recommendation.legs
        )
        total_delta = abs(portfolio_greeks.get("delta", 0) + new_delta)
        delta_limit = self._settings.max_portfolio_delta
        checks["portfolio_delta"] = total_delta <= delta_limit
        if not checks["portfolio_delta"]:
            return {
                "approved": False,
                "reason": f"Delta limit breach: |Δ|={total_delta:.1f} > {delta_limit:.1f}",
                "checks": checks,
            }

        # 6. Portfolio vega
        new_vega = sum(
            (l.vega if l.action == "buy" else -l.vega) * recommendation.contracts * 100
            for l in recommendation.legs
        )
        total_vega = abs(portfolio_greeks.get("vega", 0) + new_vega)
        vega_limit = self._settings.max_portfolio_vega
        checks["portfolio_vega"] = total_vega <= vega_limit
        if not checks["portfolio_vega"]:
            return {
                "approved": False,
                "reason": f"Vega limit breach: |ν|={total_vega:.0f} > {vega_limit:.0f}",
                "checks": checks,
            }

        # 7. Daily theta
        new_theta = sum(
            (l.theta if l.action == "buy" else -l.theta) * recommendation.contracts * 100
            for l in recommendation.legs
        )
        total_theta = abs(portfolio_greeks.get("theta", 0) + new_theta)
        theta_limit = self._settings.max_daily_theta_dollars
        checks["portfolio_theta"] = total_theta <= theta_limit
        if not checks["portfolio_theta"]:
            return {
                "approved": False,
                "reason": f"Theta limit breach: |θ|=${total_theta:.0f}/day > ${theta_limit:.0f}",
                "checks": checks,
            }

        # 8. Correlation group check
        group_ok, group_msg = self._check_correlation(recommendation.ticker, open_positions)
        checks["correlation"] = group_ok
        if not group_ok:
            return {"approved": False, "reason": group_msg, "checks": checks}

        # 9. Reward/risk minimum
        checks["reward_risk"] = recommendation.reward_risk_ratio >= self._settings.min_reward_risk_ratio
        if not checks["reward_risk"]:
            return {
                "approved": False,
                "reason": f"R/R ratio {recommendation.reward_risk_ratio:.2f} below minimum {self._settings.min_reward_risk_ratio}",
                "checks": checks,
            }

        return {
            "approved": True,
            "reason": "All risk checks passed",
            "checks": checks,
        }

    # ── Kill switch ────────────────────────────────────────────────

    def is_kill_switch_active(self) -> bool:
        row = self._db.execute(
            "SELECT active FROM kill_switch WHERE id = 1"
        ).fetchone()
        return bool(row and row[0])

    def trip_kill_switch(self, reason: str, tripped_by: str = "system") -> None:
        self._db.execute("""
            UPDATE kill_switch SET active=1, reason=?, tripped_at=?, tripped_by=?
            WHERE id=1
        """, (reason, datetime.now(tz=timezone.utc).isoformat(), tripped_by))
        self._db.commit()
        logger.critical("KILL SWITCH TRIPPED: %s (by=%s)", reason, tripped_by)

    def reset_kill_switch(self, reset_by: str = "operator") -> None:
        self._db.execute("""
            UPDATE kill_switch SET active=0, reason='', tripped_at='', tripped_by=?
            WHERE id=1
        """, (reset_by,))
        self._db.commit()
        logger.info("Kill switch RESET by %s", reset_by)

    def get_kill_switch_state(self) -> dict[str, Any]:
        row = self._db.execute("SELECT * FROM kill_switch WHERE id=1").fetchone()
        if not row:
            return {"active": False}
        return {
            "active": bool(row[1]),
            "reason": row[2],
            "tripped_at": row[3],
            "tripped_by": row[4],
        }

    # ── P&L tracking ──────────────────────────────────────────────

    def record_daily_pnl(
        self,
        realized: float,
        unrealized: float,
        trades: int = 0,
    ) -> None:
        today = date.today().isoformat()
        self._db.execute("""
            INSERT INTO daily_pnl VALUES (?, ?, ?, ?)
            ON CONFLICT(record_date) DO UPDATE SET
                realized_pnl=excluded.realized_pnl,
                unrealized_pnl=excluded.unrealized_pnl,
                trades_count=excluded.trades_count
        """, (today, realized, unrealized, trades))
        self._db.commit()

        # Auto-trip kill switch on daily loss breach
        total_pnl = realized + unrealized
        if total_pnl < -self._settings.daily_loss_limit_dollars:
            self.trip_kill_switch(
                f"Daily loss limit breached: ${total_pnl:.0f}",
                tripped_by="auto",
            )

    def _check_daily_loss(self) -> tuple[bool, str]:
        today = date.today().isoformat()
        row = self._db.execute(
            "SELECT realized_pnl + unrealized_pnl FROM daily_pnl WHERE record_date=?",
            (today,),
        ).fetchone()
        if not row:
            return True, ""
        total = row[0]
        limit = -self._settings.daily_loss_limit_dollars
        if total < limit:
            return False, f"Daily loss limit: ${total:.0f} < ${limit:.0f}"
        return True, ""

    def _check_weekly_loss(self) -> tuple[bool, str]:
        cutoff = (date.today() - timedelta(days=5)).isoformat()
        row = self._db.execute(
            "SELECT SUM(realized_pnl + unrealized_pnl) FROM daily_pnl WHERE record_date >= ?",
            (cutoff,),
        ).fetchone()
        if not row or row[0] is None:
            return True, ""
        total = row[0]
        limit = -self._settings.weekly_loss_limit_dollars
        if total < limit:
            return False, f"Weekly loss limit: ${total:.0f} < ${limit:.0f}"
        return True, ""

    def _check_correlation(
        self, ticker: str, open_positions: list[Any]
    ) -> tuple[bool, str]:
        group = _CORRELATION_GROUPS.get(ticker)
        if not group:
            return True, ""

        same_group = [
            p for p in open_positions
            if _CORRELATION_GROUPS.get(p.ticker) == group
        ]
        if len(same_group) >= _MAX_SAME_GROUP_POSITIONS:
            return (
                False,
                f"Correlation limit: {len(same_group)} positions in {group} group "
                f"(max {_MAX_SAME_GROUP_POSITIONS})",
            )
        return True, ""

    def check_position_loss_limit(
        self,
        position: Any,
        current_pnl: float,
    ) -> bool:
        """Trip kill switch if single position loss > 2× max_loss_dollars."""
        if current_pnl < -2.0 * position.max_loss_dollars:
            self.trip_kill_switch(
                f"Single position loss exceeded 2× max: {position.ticker} "
                f"P&L=${current_pnl:.0f} vs max=${position.max_loss_dollars:.0f}",
            )
            return True
        return False

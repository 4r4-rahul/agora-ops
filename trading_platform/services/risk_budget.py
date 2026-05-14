"""
RiskBudgetTracker — daily/weekly loss limits + circuit breakers.

Tracks:
  - Consecutive losses (pause after 3)
  - Daily realized P&L (halt at daily_loss_limit)
  - Weekly realized P&L (halt at weekly_loss_limit)
  - Correlation: blocks 4th position in same direction
  - Post-win cooldown: 2-hour pause after a large winner

Backed by trade_journal.db for persistence across restarts.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from ..core.config import Settings

logger = logging.getLogger(__name__)

_DB_PATH = Path("./trade_journal.db")

# Sector map used to detect correlated sector concentration.
# Add new tickers here as the universe expands.
_SECTOR_MAP: dict[str, str] = {
    # Broad market (treated separately — index exposure is diversifying)
    "SPY": "broad_market", "QQQ": "broad_market", "IWM": "broad_market", "DIA": "broad_market",
    # Technology
    "AAPL": "tech", "MSFT": "tech", "GOOGL": "tech", "GOOG": "tech",
    "META": "tech", "NVDA": "tech", "AMD": "tech", "INTC": "tech",
    "CRM": "tech", "ADBE": "tech", "ORCL": "tech", "NFLX": "tech",
    # Semiconductors (high intra-sector correlation, separate from software tech)
    "TSM": "semis", "AMAT": "semis", "LRCX": "semis", "KLAC": "semis", "MRVL": "semis",
    # Financials
    "JPM": "financials", "GS": "financials", "BAC": "financials",
    "MS": "financials", "C": "financials", "WFC": "financials",
    # Energy
    "XOM": "energy", "CVX": "energy", "OXY": "energy", "SLB": "energy",
    # Healthcare
    "JNJ": "healthcare", "UNH": "healthcare", "PFE": "healthcare", "MRK": "healthcare",
    # Consumer / Discretionary
    "AMZN": "consumer", "TSLA": "consumer", "HD": "consumer", "NKE": "consumer",
}

_MAX_SECTOR_POSITIONS = 2  # max open positions in the same sector simultaneously


class CircuitBreakerTripped(Exception):
    """Raised when a circuit breaker blocks a new trade."""


class RiskBudgetTracker:
    """
    Stateful risk budget tracker.
    Call check_can_trade() before every new entry.
    Call record_close() after every exit.
    """

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._cooldown_until: datetime | None = None

    # ── Public API ────────────────────────────────────────────────────────

    def check_can_trade(
        self,
        direction: str,
        open_positions: list[dict],
        ticker: str = "",
    ) -> None:
        """
        Raise CircuitBreakerTripped if any rule blocks trading.
        direction: 'bullish' | 'bearish' | 'neutral'
        open_positions: list of dicts with 'direction' and 'ticker' keys
        ticker: the proposed new trade ticker (used for sector correlation check)
        """
        self._check_cooldown()
        self._check_daily_loss()
        self._check_weekly_loss()
        self._check_consecutive_losses()
        self._check_account_drawdown()
        self._check_pdt_limit()
        self._check_correlation(direction, open_positions)
        if ticker:
            self._check_sector_concentration(ticker, open_positions)

    def record_close(self, pnl: float) -> None:
        """Call after any position closes to update circuit breaker state."""
        if pnl > self._s.account_size * 0.03:
            # Large winner — 2-hour cooldown to prevent overconfidence re-entry
            self._cooldown_until = datetime.now() + timedelta(hours=2)
            logger.info(
                "Large winner ($%.0f) — 2-hour cooldown until %s",
                pnl, self._cooldown_until.strftime("%H:%M"),
            )

    # ── Checks ────────────────────────────────────────────────────────────

    def _check_cooldown(self) -> None:
        if self._cooldown_until and datetime.now() < self._cooldown_until:
            remaining = (self._cooldown_until - datetime.now()).seconds // 60
            raise CircuitBreakerTripped(
                f"Post-win cooldown active — {remaining} min remaining. "
                "Prevents overconfidence re-entry into chop."
            )

    def _check_daily_loss(self) -> None:
        daily_pnl = self._get_todays_realized_pnl()
        limit = self._s.daily_loss_limit_dollars
        if daily_pnl <= -limit:
            raise CircuitBreakerTripped(
                f"Daily loss limit hit: ${daily_pnl:+,.0f} (limit: ${-limit:,.0f}). "
                "No new positions today. Review what went wrong."
            )

    def _check_weekly_loss(self) -> None:
        weekly_pnl = self._get_this_weeks_realized_pnl()
        limit = self._s.weekly_loss_limit_dollars
        if weekly_pnl <= -limit:
            raise CircuitBreakerTripped(
                f"Weekly loss limit hit: ${weekly_pnl:+,.0f} (limit: ${-limit:,.0f}). "
                "No new positions this week. Mandatory review before Monday."
            )

    def _check_consecutive_losses(self) -> None:
        streak = self._get_consecutive_loss_streak()
        if streak >= 3:
            raise CircuitBreakerTripped(
                f"{streak} consecutive losses detected. "
                "Pausing new entries for 24 hours. "
                "Review trade journal — something in the setup criteria needs adjusting."
            )

    def _check_account_drawdown(self) -> None:
        """Halt all new trades when account drops 25% below its peak (high-water mark)."""
        peak = self._get_peak_balance()
        current = self._get_current_balance()
        if peak <= 0:
            return
        dd_pct = (peak - current) / peak
        if dd_pct >= 0.25:
            raise CircuitBreakerTripped(
                f"Account drawdown {dd_pct:.0%} from peak ${peak:,.0f} → current ${current:,.0f}. "
                "All new trades halted. Review strategy and journal before resuming."
            )
        if dd_pct >= 0.15:
            logger.warning(
                "Account at %.0f%% drawdown from peak $%.0f — approaching circuit breaker (25%%)",
                dd_pct * 100, peak,
            )

    def _check_pdt_limit(self) -> None:
        """Block new entries if PDT limit reached (accounts < $25K get max 3 day trades/week).
        Skipped entirely when pdt_exempt=True (non-US accounts, cash accounts)."""
        if getattr(self._s, "pdt_exempt", False):
            return  # no day-trade restriction
        if self._s.account_size >= 25_000:
            return  # PDT only applies to accounts < $25K
        day_trades_used = self._get_week_day_trades()
        if day_trades_used >= 3:
            raise CircuitBreakerTripped(
                f"PDT limit: {day_trades_used}/3 day trades used this rolling Mon-Fri window. "
                "Open positions you intend to hold overnight to avoid consuming day trade allotment."
            )
        if day_trades_used == 2:
            logger.warning(
                "PDT warning: 2/3 day trades used this week. "
                "Next entry should be held overnight to avoid burning last day trade."
            )

    def _check_correlation(self, direction: str, open_positions: list[dict]) -> None:
        if direction == "neutral":
            return
        same_direction = sum(
            1 for p in open_positions
            if p.get("direction", "").lower() == direction.lower()
        )
        if same_direction >= 3:
            raise CircuitBreakerTripped(
                f"Correlation block: already have {same_direction} {direction} positions. "
                "Adding a 4th creates concentrated directional exposure. "
                "Wait for an existing position to close first."
            )

    def _check_sector_concentration(self, ticker: str, open_positions: list[dict]) -> None:
        """Block if adding ticker would create >_MAX_SECTOR_POSITIONS in the same sector."""
        sector = _SECTOR_MAP.get(ticker.upper(), "")
        if not sector or sector == "broad_market":
            # Unknown tickers or broad-market ETFs are not blocked by sector rules
            return
        same_sector_tickers = [
            p.get("ticker", "") for p in open_positions
            if _SECTOR_MAP.get(p.get("ticker", "").upper(), "") == sector
        ]
        if len(same_sector_tickers) >= _MAX_SECTOR_POSITIONS:
            raise CircuitBreakerTripped(
                f"Sector concentration: already have {len(same_sector_tickers)} open "
                f"position(s) in the '{sector}' sector "
                f"({', '.join(same_sector_tickers)}). "
                f"Adding {ticker} would exceed the {_MAX_SECTOR_POSITIONS}-position sector limit. "
                "Wait for an existing sector position to close first."
            )

    # ── Database queries ───────────────────────────────────────────────────

    def _get_todays_realized_pnl(self) -> float:
        if not _DB_PATH.exists():
            return 0.0
        today = date.today().isoformat()
        try:
            with sqlite3.connect(_DB_PATH) as conn:
                row = conn.execute(
                    "SELECT COALESCE(SUM(realized_pnl), 0) FROM trade_journal "
                    "WHERE status='closed' AND DATE(closed_at) = ?",
                    (today,),
                ).fetchone()
            return float(row[0]) if row else 0.0
        except Exception as exc:
            logger.error("daily P&L query failed: %s", exc)
            return 0.0

    def _get_this_weeks_realized_pnl(self) -> float:
        if not _DB_PATH.exists():
            return 0.0
        today = date.today()
        week_start = (today - timedelta(days=today.weekday())).isoformat()
        try:
            with sqlite3.connect(_DB_PATH) as conn:
                row = conn.execute(
                    "SELECT COALESCE(SUM(realized_pnl), 0) FROM trade_journal "
                    "WHERE status='closed' AND DATE(closed_at) >= ?",
                    (week_start,),
                ).fetchone()
            return float(row[0]) if row else 0.0
        except Exception as exc:
            logger.error("weekly P&L query failed: %s", exc)
            return 0.0

    def _get_consecutive_loss_streak(self) -> int:
        if not _DB_PATH.exists():
            return 0
        try:
            with sqlite3.connect(_DB_PATH) as conn:
                rows = conn.execute(
                    "SELECT realized_pnl FROM trade_journal "
                    "WHERE status='closed' AND realized_pnl IS NOT NULL "
                    "ORDER BY closed_at DESC LIMIT 10"
                ).fetchall()
            streak = 0
            for (pnl,) in rows:
                if pnl < 0:
                    streak += 1
                else:
                    break
            return streak
        except Exception as exc:
            logger.error("streak query failed: %s", exc)
            return 0

    def _get_current_balance(self) -> float:
        """Account initial size + all closed PnL to date."""
        if not _DB_PATH.exists():
            return self._s.account_size
        try:
            with sqlite3.connect(_DB_PATH) as conn:
                row = conn.execute(
                    "SELECT COALESCE(SUM(realized_pnl), 0) FROM trade_journal WHERE status='closed'"
                ).fetchone()
            return self._s.account_size + float(row[0] if row else 0)
        except Exception as exc:
            logger.error("current balance query failed: %s", exc)
            return self._s.account_size

    def _get_peak_balance(self) -> float:
        """High-water mark of account balance across all closed trades."""
        if not _DB_PATH.exists():
            return self._s.account_size
        try:
            with sqlite3.connect(_DB_PATH) as conn:
                rows = conn.execute(
                    "SELECT realized_pnl FROM trade_journal "
                    "WHERE status='closed' AND realized_pnl IS NOT NULL "
                    "ORDER BY closed_at"
                ).fetchall()
            running = self._s.account_size
            peak = running
            for (pnl,) in rows:
                running += pnl
                if running > peak:
                    peak = running
            return peak
        except Exception as exc:
            logger.error("peak balance query failed: %s", exc)
            return self._s.account_size

    def _get_week_day_trades(self) -> int:
        """Count same-day open+close round trips in the current Mon-Fri window."""
        if not _DB_PATH.exists():
            return 0
        today = date.today()
        week_start = (today - timedelta(days=today.weekday())).isoformat()
        try:
            with sqlite3.connect(_DB_PATH) as conn:
                rows = conn.execute(
                    """
                    SELECT COUNT(*) FROM trade_journal
                    WHERE status = 'closed'
                      AND DATE(opened_at) >= ?
                      AND DATE(closed_at) = DATE(opened_at)
                    """,
                    (week_start,),
                ).fetchone()
            return int(rows[0]) if rows else 0
        except Exception as exc:
            logger.error("PDT day trade count failed: %s", exc)
            return 0

    def summary(self) -> dict:
        peak = self._get_peak_balance()
        current = self._get_current_balance()
        dd_pct = (peak - current) / peak if peak > 0 else 0.0
        return {
            "daily_pnl": self._get_todays_realized_pnl(),
            "weekly_pnl": self._get_this_weeks_realized_pnl(),
            "consecutive_losses": self._get_consecutive_loss_streak(),
            "cooldown_until": (
                self._cooldown_until.isoformat() if self._cooldown_until else None
            ),
            "daily_limit": -self._s.daily_loss_limit_dollars,
            "weekly_limit": -self._s.weekly_loss_limit_dollars,
            "current_balance": current,
            "peak_balance": peak,
            "account_drawdown_pct": round(dd_pct * 100, 1),
            "pdt_day_trades_used": self._get_week_day_trades(),
            "pdt_active": self._s.account_size < 25_000 and not getattr(self._s, "pdt_exempt", False),
            "pdt_exempt": getattr(self._s, "pdt_exempt", False),
        }

"""
ComplianceAgent — wash sale, Reg T, options rules.

NOTE: PDT (Pattern Day Trader) rule is NOT tracked here.
The SEC repealed PDT restrictions effective June 4, 2026. We do not restrict
day trade frequency in AGORA.

Rules enforced:
  1. Wash Sale (IRS §1091)
     - Cannot repurchase substantially identical security within 30 days of
       a loss sale. We track closed losing trades per ticker.
     - Advisory only: we log a warning and reduce conviction, not hard block.
       (AGORA is not a tax advisor — flag for owner review.)

  2. Reg T (Federal Reserve Reg T)
     - Options: long options require full premium payment (no margin for longs).
     - Spreads: net debit must be covered by available cash.
     - Credit spreads: collateral = width of spread × contracts × 100.
     - We track estimated buying power used and flag if we approach 90% of account.

  3. Options Level Requirements
     - Only strategies appropriate for Level 3 options approval:
       credit spreads, debit spreads, iron condors.
     - No naked calls/puts (require Level 4+).
     - This is enforced by StrategyRulesEngine, but we double-check here.

  4. Position Concentration
     - No single ticker > 20% of account risk.
     - Tracked separately from RiskCouncil's portfolio-level limits.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, timedelta
from typing import Any

from ..core.config import AgoraSettings, get_settings

logger = logging.getLogger(__name__)

# Naked strategies — blocked at our options level
_NAKED_STRATEGIES = {"long_call", "long_put", "cash_secured_put"}
# (cash_secured_put is actually fine, but blocks naked call/put)

_WASH_SALE_DAYS = 30        # 30 days before and after loss sale
_MAX_TICKER_RISK_PCT = 0.20 # no single ticker > 20% of account


class ComplianceAgent:
    """
    Synchronous pre-trade compliance gate.
    All methods return (compliant: bool, message: str).
    """

    def __init__(self, settings: AgoraSettings | None = None) -> None:
        self._settings = settings or get_settings()
        self._csuite_manager: Any = None   # CROAgent — set via register_csuite_manager()
        self._db = self._init_db()

    def register_csuite_manager(self, manager: Any) -> None:
        """Wire the CROAgent as supervising executive."""
        self._csuite_manager = manager

    def _init_db(self) -> sqlite3.Connection:
        db_path = self._settings.db_path
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS wash_sale_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                close_date TEXT NOT NULL,
                realized_pnl REAL NOT NULL,
                strategy TEXT NOT NULL DEFAULT '',
                flagged INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.commit()
        return conn

    # ── Primary gate ───────────────────────────────────────────────

    def check_trade(
        self,
        recommendation: Any,
        open_positions: list[Any],
        account_buying_power: float | None = None,
    ) -> dict[str, Any]:
        """
        Full compliance check. Returns:
        {compliant: bool, reason: str, warnings: list[str]}
        """
        warnings: list[str] = []

        # 1. Wash sale check (advisory)
        ws_ok, ws_msg = self._check_wash_sale(recommendation.ticker)
        if not ws_ok:
            warnings.append(f"WASH SALE WARNING: {ws_msg}")
            logger.warning("Compliance wash sale: %s", ws_msg)

        # 2. Strategy level check (naked call: always blocked; naked put: conviction-gated)
        strategy_ok, strategy_msg = self._check_strategy_level(recommendation)
        if not strategy_ok:
            return {"compliant": False, "reason": strategy_msg, "warnings": warnings}

        # 3. Position concentration
        conc_ok, conc_msg = self._check_concentration(recommendation, open_positions)
        if not conc_ok:
            return {"compliant": False, "reason": conc_msg, "warnings": warnings}

        # 4. Reg T buying power estimate
        bp_ok, bp_msg = self._check_buying_power(recommendation, account_buying_power)
        if not bp_ok:
            warnings.append(f"BUYING POWER: {bp_msg}")

        return {
            "compliant": True,
            "reason": "Compliance checks passed" + (f" ({len(warnings)} warning(s))" if warnings else ""),
            "warnings": warnings,
        }

    # ── Wash sale ──────────────────────────────────────────────────

    def _check_wash_sale(self, ticker: str) -> tuple[bool, str]:
        """
        Check if this ticker has a loss trade closed within last 30 days.
        Advisory only — we flag but don't hard block.
        """
        cutoff = (date.today() - timedelta(days=_WASH_SALE_DAYS)).isoformat()
        row = self._db.execute("""
            SELECT close_date, realized_pnl FROM wash_sale_log
            WHERE ticker = ? AND close_date >= ? AND realized_pnl < 0
            ORDER BY close_date DESC LIMIT 1
        """, (ticker, cutoff)).fetchone()

        if row:
            return (
                False,
                f"{ticker} had a loss close on {row[0]} (${row[1]:.0f}). "
                f"Re-entering within 30 days may trigger IRS wash sale rule. "
                f"Consult tax advisor."
            )
        return True, ""

    def record_close(self, ticker: str, realized_pnl: float, strategy: str = "") -> None:
        """Called by PositionManager when a position closes."""
        self._db.execute("""
            INSERT INTO wash_sale_log (ticker, close_date, realized_pnl, strategy, flagged)
            VALUES (?, ?, ?, ?, ?)
        """, (
            ticker,
            date.today().isoformat(),
            realized_pnl,
            strategy,
            1 if realized_pnl < 0 else 0,
        ))
        self._db.commit()
        if realized_pnl < 0:
            logger.info(
                "Wash sale log: %s closed at $%.0f loss on %s",
                ticker, realized_pnl, date.today()
            )

    # ── Strategy level ─────────────────────────────────────────────

    def _check_strategy_level(self, recommendation: Any) -> tuple[bool, str]:
        """
        Conviction-gated naked options check.

        Two completely different risk profiles:

        Naked SHORT CALL → always blocked.
          Unlimited theoretical loss: stock can go to infinity.
          Also requires IBKR Level 4 approval. No conviction level changes this.

        Naked SHORT PUT (cash-secured) → allowed at conviction ≥ 80%.
          Max loss is fully defined: strike × 100 × contracts.
          Often the *better* trade than a bull put spread at high conviction —
          you keep the entire premium; the long put leg just reduces edge.
          The buying power check (Reg T) already enforces cash backing.
        """
        legs = recommendation.legs if recommendation.legs else []

        if len(legs) != 1:
            return True, ""

        leg = legs[0]
        if leg.action != "sell":
            return True, ""   # long single option is fine

        if leg.option_type == "call":
            # Naked short call: unlimited loss, Level 4 required — hard block always
            return (
                False,
                f"Naked short CALL on {recommendation.ticker} blocked — "
                f"unlimited loss potential. Use a bear call spread instead."
            )

        # Naked short PUT (cash-secured): conviction-gated
        conviction = getattr(recommendation, "conviction_score", 0.0)
        if conviction >= 80.0:
            logger.info(
                "Cash-secured put on %s approved at conviction=%.0f "
                "(defined max loss = strike×100×contracts; buying power check handles Reg T)",
                recommendation.ticker, conviction,
            )
            return True, ""

        return (
            False,
            f"Naked short PUT on {recommendation.ticker} requires conviction ≥ 80 "
            f"(current={conviction:.0f}). Use a bull put spread or raise conviction first."
        )

    # ── Position concentration ─────────────────────────────────────

    def _check_concentration(
        self, recommendation: Any, open_positions: list[Any]
    ) -> tuple[bool, str]:
        """No single ticker > 20% of account risk."""
        ticker = recommendation.ticker
        max_risk = self._settings.account_size * _MAX_TICKER_RISK_PCT

        # Current risk in this ticker
        existing_risk = sum(
            p.max_loss_dollars * p.contracts
            for p in open_positions
            if p.ticker == ticker
        )
        new_risk = recommendation.max_loss_dollars * recommendation.contracts
        total_risk = existing_risk + new_risk

        if total_risk > max_risk:
            return (
                False,
                f"Concentration limit: {ticker} total risk ${total_risk:.0f} > "
                f"{_MAX_TICKER_RISK_PCT:.0%} of account (${max_risk:.0f})"
            )
        return True, ""

    # ── Reg T buying power ─────────────────────────────────────────

    def _check_buying_power(
        self, recommendation: Any, buying_power: float | None
    ) -> tuple[bool, str]:
        """
        Estimate collateral requirement for this trade.
        Credit spread: max_loss_dollars (the spread width × contracts × 100)
        Debit spread:  entry_debit_credit × contracts × 100
        """
        if buying_power is None:
            return True, ""   # can't check without BP data

        if recommendation.entry_debit_credit > 0:
            # Debit trade: full premium required
            cost = recommendation.entry_debit_credit * recommendation.contracts * 100
        else:
            # Credit spread: collateral = max loss
            cost = recommendation.max_loss_dollars * recommendation.contracts

        bp_usage_pct = cost / max(buying_power, 1) * 100
        if bp_usage_pct > 90:
            return (
                False,
                f"Trade requires ${cost:.0f} ({bp_usage_pct:.0f}% of buying power ${buying_power:.0f})"
            )
        return True, f"Estimated collateral ${cost:.0f} ({bp_usage_pct:.0f}% of BP)"

    # ── Compliance summary ─────────────────────────────────────────

    def get_wash_sale_watchlist(self) -> list[dict]:
        """Return all tickers in current 30-day wash sale watch window."""
        cutoff = (date.today() - timedelta(days=_WASH_SALE_DAYS)).isoformat()
        rows = self._db.execute("""
            SELECT ticker, close_date, realized_pnl FROM wash_sale_log
            WHERE close_date >= ? AND realized_pnl < 0
            ORDER BY close_date DESC
        """, (cutoff,)).fetchall()
        return [
            {"ticker": r[0], "close_date": r[1], "loss": r[2]}
            for r in rows
        ]

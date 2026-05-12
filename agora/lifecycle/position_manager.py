"""
PositionLifecycleAgent — state machine for every open AGORA position.

State transitions:
  OPEN → TESTED     : underlying through short strike (intraday monitor)
  OPEN → CLOSED     : 50% profit target hit OR 21 DTE reached
  OPEN → EXPIRED    : held to expiry (should be rare — auto-close at 3:30 PM)
  TESTED → ROLLED   : roll to same delta +30 DTE for net credit
  TESTED → CLOSED   : stop-loss hit (2× initial credit/debit)
  ROLLED → OPEN     : new rolled position enters fresh OPEN state
  ANY   → ASSIGNED  : detected via IBKR position reconciliation

Key rules (from architecture):
  1. Close at 50% profit (never let winner become loser)
  2. Close at 21 DTE regardless (theta decay accelerates, gamma risk spikes)
  3. Stop-loss at 2× initial credit/debit (sell iron condor for $2 → stop at -$4)
  4. Roll when TESTED: same delta, +30 DTE from tested expiry, MUST collect net credit
  5. Assignment scanner: T-2 ITM check at 3:45 PM ET
  6. OCA bracket orders at IBKR (stop + profit target orders in place at entry)

Position storage: SQLite (.agora/agora.db) — all positions persisted to disk.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..core.config import AgoraSettings, get_settings
from ..core.models import (
    OpenPosition,
    PositionStatus,
    SpreadLeg,
    StrategyPillar,
    StrategyType,
    TradeRecord,
)

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

_CLOSE_TIME = time(15, 30)       # 3:30 PM ET — auto-close before expiry
_ASSIGNMENT_SCAN_TIME = time(15, 45)  # 3:45 PM ET — T-2 ITM check


class PositionManager:
    """
    Runs the position lifecycle loop every 60 seconds during market hours.
    Stores all state in SQLite for crash recovery.
    """

    def __init__(
        self,
        settings: AgoraSettings | None = None,
        on_close_order: Any = None,    # async callback(position, reason) → ibkr order
        on_roll_order: Any = None,     # async callback(position, new_expiry) → ibkr order
    ) -> None:
        self._settings = settings or get_settings()
        self._on_close = on_close_order
        self._on_roll = on_roll_order
        self._db = self._init_db()
        self._running = False

    def _init_db(self) -> sqlite3.Connection:
        db_path = self._settings.db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                position_id TEXT PRIMARY KEY,
                ticker TEXT NOT NULL,
                strategy TEXT NOT NULL,
                pillar TEXT NOT NULL,
                status TEXT NOT NULL,
                legs_json TEXT NOT NULL,
                contracts INTEGER NOT NULL,
                entry_price REAL NOT NULL,
                current_price REAL NOT NULL DEFAULT 0,
                entry_date TEXT NOT NULL,
                expiry_date TEXT NOT NULL,
                target_close_date TEXT NOT NULL,
                max_loss_dollars REAL NOT NULL,
                max_gain_dollars REAL NOT NULL,
                unrealized_pnl REAL NOT NULL DEFAULT 0,
                realized_pnl REAL NOT NULL DEFAULT 0,
                rolled_count INTEGER NOT NULL DEFAULT 0,
                last_reviewed TEXT NOT NULL,
                ibkr_order_ids TEXT NOT NULL DEFAULT '[]',
                notes TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trade_records (
                trade_id TEXT PRIMARY KEY,
                ticker TEXT NOT NULL,
                strategy TEXT NOT NULL,
                pillar TEXT NOT NULL,
                entry_date TEXT NOT NULL,
                close_date TEXT,
                expiry_date TEXT NOT NULL,
                entry_price REAL NOT NULL,
                close_price REAL,
                contracts INTEGER NOT NULL,
                realized_pnl REAL,
                commission REAL NOT NULL DEFAULT 0,
                slippage REAL NOT NULL DEFAULT 0,
                regime_at_entry TEXT,
                conviction_at_entry REAL,
                signal_hash TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.commit()
        return conn

    async def start(self) -> None:
        self._running = True
        logger.info("PositionManager started")
        while self._running:
            try:
                await self._lifecycle_cycle()
            except Exception as exc:
                logger.error("Lifecycle cycle error: %s", exc)
            await asyncio.sleep(60)

    async def stop(self) -> None:
        self._running = False

    # ── Lifecycle cycle ────────────────────────────────────────────

    async def _lifecycle_cycle(self) -> None:
        now_et = datetime.now(tz=ET)
        if not self._is_market_hours(now_et):
            return

        positions = self.get_open_positions()
        if not positions:
            return

        # Update current prices
        tasks = [self._refresh_position_price(p) for p in positions]
        await asyncio.gather(*tasks, return_exceptions=True)

        # Re-fetch with updated prices
        positions = self.get_open_positions()

        # Assignment scanner at 3:45 PM ET
        now_time = now_et.time()
        if _ASSIGNMENT_SCAN_TIME <= now_time <= time(16, 0):
            await self._scan_for_assignment_risk(positions)

        # Auto-close expiring positions at 3:30 PM ET
        if _CLOSE_TIME <= now_time <= time(15, 45):
            await self._auto_close_expiring(positions, now_et.date())

        # Check all P&L targets and stop-losses
        for pos in positions:
            await self._check_position_targets(pos)

    def _is_market_hours(self, now_et: datetime) -> bool:
        """9:30 AM – 4:00 PM ET weekdays."""
        if now_et.weekday() >= 5:
            return False
        t = now_et.time()
        return time(9, 30) <= t <= time(16, 0)

    # ── Price refresh ──────────────────────────────────────────────

    async def _refresh_position_price(self, position: OpenPosition) -> None:
        """Fetch current spread mid price from yfinance options chain."""
        try:
            import yfinance as yf
            tk = yf.Ticker(position.ticker)
            if not tk.options:
                return

            current_mid = 0.0
            for leg in position.legs:
                exp_str = leg.expiration.isoformat()
                if exp_str not in tk.options:
                    continue
                chain = tk.option_chain(exp_str)
                df = chain.calls if leg.option_type == "call" else chain.puts
                row = df[df["strike"] == leg.strike]
                if row.empty:
                    continue
                bid = float(row["bid"].iloc[0] or 0)
                ask = float(row["ask"].iloc[0] or 0)
                mid = (bid + ask) / 2
                if leg.action == "buy":
                    current_mid += mid
                else:
                    current_mid -= mid

            unrealized = (current_mid - position.entry_price) * 100 * position.contracts
            self._update_position_price(position.position_id, current_mid, unrealized)
        except Exception as exc:
            logger.debug("Price refresh failed for %s: %s", position.ticker, exc)

    # ── Target checks ──────────────────────────────────────────────

    async def _check_position_targets(self, position: OpenPosition) -> None:
        today = date.today()

        # 21-DTE close
        dte = (position.expiry_date - today).days
        if dte <= self._settings.target_dte_close:
            await self._close_position(position, f"21-DTE reached (dte={dte})")
            return

        # 50% profit target
        profit_target = position.max_gain_dollars * self._settings.profit_target_pct
        if position.unrealized_pnl >= profit_target:
            await self._close_position(
                position,
                f"50% profit target hit (unrealized=${position.unrealized_pnl:.0f})",
            )
            return

        # Stop-loss: 2× initial debit/credit
        stop_threshold = -abs(position.entry_price * 100 * position.contracts * self._settings.stop_loss_multiplier)
        if position.unrealized_pnl <= stop_threshold:
            # Check if rolling is viable before stopping out
            rolled = await self._attempt_roll(position)
            if not rolled:
                await self._close_position(
                    position,
                    f"Stop-loss hit (unrealized=${position.unrealized_pnl:.0f})",
                )
            return

        # Mark as TESTED if underlying through short strike
        await self._check_tested_status(position)

    async def _check_tested_status(self, position: OpenPosition) -> None:
        """Mark TESTED if spot has crossed through the short strike."""
        try:
            import yfinance as yf
            info = yf.Ticker(position.ticker).info or {}
            spot = float(info.get("regularMarketPrice") or info.get("currentPrice") or 0)
            if spot <= 0:
                return

            # Find short legs
            short_legs = [l for l in position.legs if l.action == "sell"]
            for leg in short_legs:
                if leg.option_type == "put" and spot < leg.strike:
                    self._update_status(position.position_id, PositionStatus.TESTED)
                    logger.warning("TESTED: %s spot=%.2f below short put %.2f",
                                   position.ticker, spot, leg.strike)
                    break
                elif leg.option_type == "call" and spot > leg.strike:
                    self._update_status(position.position_id, PositionStatus.TESTED)
                    logger.warning("TESTED: %s spot=%.2f above short call %.2f",
                                   position.ticker, spot, leg.strike)
                    break
        except Exception as exc:
            logger.debug("Tested check failed for %s: %s", position.ticker, exc)

    async def _attempt_roll(self, position: OpenPosition) -> bool:
        """
        Attempt to roll: close current position + open new at same delta +30 DTE.
        Only proceeds if roll collects NET CREDIT.
        Returns True if roll order placed.
        """
        if position.rolled_count >= 2:
            logger.info("ROLL SKIPPED: %s already rolled %d times", position.ticker, position.rolled_count)
            return False

        if self._on_roll:
            new_expiry = position.expiry_date + timedelta(days=30)
            try:
                await self._on_roll(position, new_expiry)
                self._update_status(position.position_id, PositionStatus.ROLLED)
                logger.info("ROLLED: %s to %s", position.ticker, new_expiry)
                return True
            except Exception as exc:
                logger.warning("Roll failed for %s: %s", position.ticker, exc)
        return False

    async def _close_position(self, position: OpenPosition, reason: str) -> None:
        if self._on_close:
            try:
                await self._on_close(position, reason)
            except Exception as exc:
                logger.error("Close order failed for %s: %s", position.ticker, exc)

        self._update_status(position.position_id, PositionStatus.CLOSED)
        logger.info("CLOSED: %s | reason: %s | PnL: $%.0f",
                    position.ticker, reason, position.unrealized_pnl)

        # Write to trade_records for attribution
        self._write_trade_record(position, reason)

    async def _auto_close_expiring(
        self, positions: list[OpenPosition], today: date
    ) -> None:
        """Close all positions expiring today by 3:30 PM."""
        for pos in positions:
            if pos.expiry_date == today and pos.status == PositionStatus.OPEN:
                await self._close_position(pos, "Expiry-day auto-close 3:30 PM ET")

    async def _scan_for_assignment_risk(self, positions: list[OpenPosition]) -> None:
        """T-2 check: warn if short options are ITM within 2 days of expiry."""
        today = date.today()
        try:
            import yfinance as yf
            for pos in positions:
                dte = (pos.expiry_date - today).days
                if dte > 2:
                    continue
                info = yf.Ticker(pos.ticker).info or {}
                spot = float(info.get("regularMarketPrice") or 0)
                if spot <= 0:
                    continue
                for leg in pos.legs:
                    if leg.action != "sell":
                        continue
                    if leg.option_type == "put" and spot < leg.strike:
                        logger.warning(
                            "ASSIGNMENT RISK: %s short put %.2f ITM (spot=%.2f, dte=%d)",
                            pos.ticker, leg.strike, spot, dte,
                        )
                    elif leg.option_type == "call" and spot > leg.strike:
                        logger.warning(
                            "ASSIGNMENT RISK: %s short call %.2f ITM (spot=%.2f, dte=%d)",
                            pos.ticker, leg.strike, spot, dte,
                        )
        except Exception as exc:
            logger.debug("Assignment scan failed: %s", exc)

    # ── Storage ────────────────────────────────────────────────────

    def add_position(self, position: OpenPosition) -> None:
        legs_json = json.dumps([
            {
                "option_type": l.option_type,
                "strike": l.strike,
                "expiration": l.expiration.isoformat(),
                "action": l.action,
                "contracts": l.contracts,
                "mid_price": l.mid_price,
            }
            for l in position.legs
        ])
        self._db.execute("""
            INSERT OR REPLACE INTO positions VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
        """, (
            position.position_id,
            position.ticker,
            position.strategy.value,
            position.pillar.value,
            position.status.value,
            legs_json,
            position.contracts,
            position.entry_price,
            position.current_price,
            position.entry_date.isoformat(),
            position.expiry_date.isoformat(),
            position.target_close_date.isoformat(),
            position.max_loss_dollars,
            position.max_gain_dollars,
            position.unrealized_pnl,
            position.realized_pnl,
            position.rolled_count,
            datetime.now(tz=timezone.utc).isoformat(),
            json.dumps(position.ibkr_order_ids),
            position.notes,
        ))
        self._db.commit()

    def get_open_positions(self) -> list[OpenPosition]:
        rows = self._db.execute(
            "SELECT * FROM positions WHERE status IN ('open','tested','rolled')"
        ).fetchall()
        positions = []
        for row in rows:
            try:
                legs_data = json.loads(row[5])
                legs = [
                    SpreadLeg(
                        option_type=l["option_type"],
                        strike=l["strike"],
                        expiration=date.fromisoformat(l["expiration"]),
                        action=l["action"],
                        contracts=l.get("contracts", 1),
                        mid_price=l.get("mid_price", 0.0),
                    )
                    for l in legs_data
                ]
                pos = OpenPosition(
                    position_id=row[0],
                    ticker=row[1],
                    strategy=StrategyType(row[2]),
                    pillar=StrategyPillar(row[3]),
                    status=PositionStatus(row[4]),
                    legs=legs,
                    contracts=row[6],
                    entry_price=row[7],
                    current_price=row[8],
                    entry_date=date.fromisoformat(row[9]),
                    expiry_date=date.fromisoformat(row[10]),
                    target_close_date=date.fromisoformat(row[11]),
                    max_loss_dollars=row[12],
                    max_gain_dollars=row[13],
                    unrealized_pnl=row[14],
                    realized_pnl=row[15],
                    rolled_count=row[16],
                    ibkr_order_ids=json.loads(row[18]),
                    notes=row[19],
                )
                positions.append(pos)
            except Exception as exc:
                logger.debug("Position deserialize failed: %s", exc)
        return positions

    def _update_position_price(
        self, position_id: str, current_price: float, unrealized_pnl: float
    ) -> None:
        self._db.execute(
            "UPDATE positions SET current_price=?, unrealized_pnl=?, last_reviewed=? WHERE position_id=?",
            (current_price, unrealized_pnl, datetime.now(tz=timezone.utc).isoformat(), position_id),
        )
        self._db.commit()

    def _update_status(self, position_id: str, status: PositionStatus) -> None:
        self._db.execute(
            "UPDATE positions SET status=? WHERE position_id=?",
            (status.value, position_id),
        )
        self._db.commit()

    def _write_trade_record(self, position: OpenPosition, notes: str) -> None:
        self._db.execute("""
            INSERT OR IGNORE INTO trade_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            position.position_id,
            position.ticker,
            position.strategy.value,
            position.pillar.value,
            position.entry_date.isoformat(),
            date.today().isoformat(),
            position.expiry_date.isoformat(),
            position.entry_price,
            position.current_price,
            position.contracts,
            position.unrealized_pnl,
            0.0,  # commission tracked separately by IBKR callback
            0.0,  # slippage filled in by execution layer
            None,
            None,
            "",
            notes,
        ))
        self._db.commit()

    def get_portfolio_greeks(self) -> dict[str, float]:
        """Aggregate Greeks across all open positions for risk council."""
        positions = self.get_open_positions()
        total_delta = 0.0
        total_vega = 0.0
        total_theta = 0.0
        total_gamma = 0.0
        for pos in positions:
            for leg in pos.legs:
                sign = 1.0 if leg.action == "buy" else -1.0
                contracts = pos.contracts * leg.contracts
                total_delta += sign * leg.delta * contracts * 100
                total_vega  += sign * leg.vega  * contracts * 100
                total_theta += sign * leg.theta * contracts * 100
                total_gamma += sign * leg.gamma * contracts * 100
        return {
            "delta": round(total_delta, 2),
            "vega":  round(total_vega, 2),
            "theta": round(total_theta, 2),
            "gamma": round(total_gamma, 4),
            "positions": len(positions),
        }

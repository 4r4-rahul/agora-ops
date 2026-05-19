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
from ..ops.llm_cost_log import ensure_table as _ensure_llm_cost_table
from ..ops.decision_chains import update_close as _decision_chain_close
from ..core.models import (
    OpenPosition,
    PositionStatus,
    SpreadLeg,
    StrategyPillar,
    StrategyType,
    TradeRecord,
)
from .profit_engine import IntelligentProfitEngine

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
        self._profit_engine = IntelligentProfitEngine()

    def set_macro_context(self, ctx: Any) -> None:
        """Called by session after every macro synthesis — keeps engine regime-aware."""
        self._profit_engine.set_macro_context(ctx)

    def set_price_target_for_position(
        self, position_id: str, aligned_return_pct: float, entry_spot: float
    ) -> None:
        """Delegate to profit engine — called once per fill when PriceTargetAgent cache hit."""
        self._profit_engine.set_price_target(position_id, aligned_return_pct, entry_spot)

    def set_fill_quality_for_position(self, position_id: str, fill_bonus_pct: float) -> None:
        """Delegate to profit engine — called once per confirmed IBKR fill."""
        self._profit_engine.set_fill_quality(position_id, fill_bonus_pct)

    def get_profit_engine_state(self, position_id: str) -> dict | None:
        """Return serialisable profit engine snapshot for the dashboard."""
        return self._profit_engine.get_state_snapshot(position_id)

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
                notes TEXT NOT NULL DEFAULT '',
                direction TEXT NOT NULL DEFAULT 'neutral'
            )
        """)
        # Incremental migrations — add columns that didn't exist in earlier versions
        existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(positions)").fetchall()}
        if "direction" not in existing_cols:
            conn.execute("ALTER TABLE positions ADD COLUMN direction TEXT NOT NULL DEFAULT 'neutral'")
        if "conviction_at_entry" not in existing_cols:
            conn.execute("ALTER TABLE positions ADD COLUMN conviction_at_entry REAL NOT NULL DEFAULT 0")
        if "regime_at_entry" not in existing_cols:
            conn.execute("ALTER TABLE positions ADD COLUMN regime_at_entry TEXT NOT NULL DEFAULT ''")
        if "earnings_date" not in existing_cols:
            conn.execute("ALTER TABLE positions ADD COLUMN earnings_date TEXT")
        if "is_pre_earnings" not in existing_cols:
            conn.execute("ALTER TABLE positions ADD COLUMN is_pre_earnings INTEGER NOT NULL DEFAULT 0")
        if "close_date" not in existing_cols:
            conn.execute("ALTER TABLE positions ADD COLUMN close_date TEXT")
        if "close_price" not in existing_cols:
            conn.execute("ALTER TABLE positions ADD COLUMN close_price REAL")
        if "close_source" not in existing_cols:
            conn.execute("ALTER TABLE positions ADD COLUMN close_source TEXT NOT NULL DEFAULT ''")
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trade_journal (
                journal_id   TEXT PRIMARY KEY,
                position_id  TEXT NOT NULL,
                ticker       TEXT NOT NULL,
                strategy     TEXT NOT NULL,
                direction    TEXT NOT NULL,
                pillar       TEXT NOT NULL,
                entry_date   TEXT NOT NULL,
                spot_at_entry REAL NOT NULL DEFAULT 0,
                entry_price  REAL NOT NULL,
                max_loss     REAL NOT NULL DEFAULT 0,
                max_gain     REAL NOT NULL DEFAULT 0,
                rr_ratio     REAL NOT NULL DEFAULT 0,
                conviction   REAL NOT NULL DEFAULT 0,
                gate         TEXT NOT NULL DEFAULT '',
                legs_summary TEXT NOT NULL DEFAULT '',
                why_traded   TEXT NOT NULL DEFAULT '',
                macro_at_entry TEXT NOT NULL DEFAULT '',
                regime_at_entry TEXT NOT NULL DEFAULT '',
                ibkr_order_id INTEGER NOT NULL DEFAULT -1,
                FOREIGN KEY (position_id) REFERENCES positions(position_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS decision_chains (
                chain_id       TEXT PRIMARY KEY,
                ticker         TEXT NOT NULL,
                triggered_by   TEXT NOT NULL DEFAULT 'universe_scan',
                started_at     TEXT NOT NULL,
                outcome        TEXT NOT NULL DEFAULT 'pending',
                position_id    TEXT,
                realized_pnl   REAL,
                session_id     TEXT NOT NULL DEFAULT '',
                conviction     REAL NOT NULL DEFAULT 0,
                strategy       TEXT NOT NULL DEFAULT '',
                gates_passed   TEXT NOT NULL DEFAULT '[]'
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_dc_ticker ON decision_chains(ticker)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_dc_session ON decision_chains(session_id)")
        conn.commit()
        _ensure_llm_cost_table(str(db_path))
        return conn

    async def start(self) -> None:
        self._running = True
        # Register existing positions with the profit engine on startup.
        # Entry-time Greeks will be approximated from current leg state
        # (greeks will have drifted, but DTE-curve and ratchet logic still apply).
        existing = self.get_open_positions()
        if existing:
            for pos in existing:
                self._profit_engine.register_position(pos, is_existing=True)
            logger.info(
                "ProfitEngine: registered %d existing position(s) on startup: %s",
                len(existing), [p.ticker for p in existing],
            )
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

        # Log upcoming 21-DTE closes (warn the day before so the user knows)
        today = now_et.date()
        tomorrow_closures = [p for p in positions if (p.expiry_date - today).days == 22]
        if tomorrow_closures and now_et.time() >= time(9, 30):
            tickers = ", ".join(p.ticker for p in tomorrow_closures)
            logger.warning(
                "TOMORROW 21-DTE CLOSE: %d position(s) will auto-close at market open — %s",
                len(tomorrow_closures), tickers,
            )

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

    @staticmethod
    def _fetch_price_data_sync(
        ticker: str, legs: list[Any], today: date
    ) -> dict:
        """Synchronous yfinance work — runs in a thread pool via asyncio.to_thread()."""
        import yfinance as yf
        from trading_platform.services.options_flow import compute_bs_greeks

        tk = yf.Ticker(ticker)
        avail_exps = set(tk.options or [])
        if not avail_exps:
            return {}

        try:
            fi   = tk.fast_info
            spot = float(getattr(fi, "last_price", None) or fi.get("lastPrice", 0) or 0)
        except Exception:
            spot = 0.0
        if spot <= 0:
            info = tk.info or {}
            spot = float(info.get("regularMarketPrice") or info.get("currentPrice") or 0)

        current_mid = 0.0
        updated_legs: list[dict] = []

        for leg in legs:
            exp_str = leg.expiration.isoformat()
            leg_dict = {
                "option_type": leg.option_type,
                "strike":      leg.strike,
                "expiration":  exp_str,
                "action":      leg.action,
                "contracts":   leg.contracts,
                "mid_price":   leg.mid_price,
                "delta":       leg.delta,
                "gamma":       leg.gamma,
                "theta":       leg.theta,
                "vega":        leg.vega,
            }

            if exp_str in avail_exps:
                chain = tk.option_chain(exp_str)
                df    = chain.calls if leg.option_type == "call" else chain.puts
                row   = df[df["strike"] == leg.strike]
                if not row.empty:
                    # Convert DataFrame slice to Series so .get() works correctly
                    r   = row.iloc[0]
                    bid = float(r.get("bid", 0) or 0)
                    ask = float(r.get("ask", 0) or 0)
                    mid = (bid + ask) / 2

                    delta = float(r.get("delta", 0) or 0)
                    gamma = float(r.get("gamma", 0) or 0)
                    theta = float(r.get("theta", 0) or 0)
                    vega  = float(r.get("vega",  0) or 0)

                    if delta == 0 and spot > 0:
                        iv  = float(r.get("impliedVolatility", 0) or 0)
                        dte = max((leg.expiration - today).days, 0.5)
                        if iv <= 0:
                            iv = mid / (spot * 0.04 * (dte / 365) ** 0.5) if mid > 0 else 0.30
                            iv = max(0.10, min(iv, 2.0))
                        bs  = compute_bs_greeks(spot, leg.strike, dte, iv, leg.option_type)
                        delta, gamma, theta, vega = bs["delta"], bs["gamma"], bs["theta"], bs["vega"]

                    leg_dict.update({
                        "mid_price": round(mid, 2),
                        "delta": delta, "gamma": gamma,
                        "theta": theta, "vega":  vega,
                    })

                    if leg.action == "buy":
                        current_mid += mid
                    else:
                        current_mid -= mid

            updated_legs.append(leg_dict)

        return {"spot": spot, "current_mid": current_mid, "updated_legs": updated_legs}

    async def _refresh_position_price(self, position: OpenPosition) -> None:
        """
        Fetch current spread mid price and live greeks from yfinance options chain.

        Runs the synchronous yfinance calls in a thread pool so the event loop
        stays unblocked. Updates unrealized P&L and per-leg greeks.
        """
        try:
            result = await asyncio.to_thread(
                self._fetch_price_data_sync,
                position.ticker, position.legs, date.today(),
            )
            if not result:
                return

            current_mid   = round(result["current_mid"], 4)
            updated_legs  = result["updated_legs"]
            unrealized    = round((current_mid - position.entry_price) * 100 * position.contracts, 2)
            self._update_position_price(position.position_id, current_mid, unrealized)

            self._db.execute(
                "UPDATE positions SET legs_json=? WHERE position_id=?",
                (json.dumps(updated_legs), position.position_id),
            )
            self._db.commit()

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

        # ── Intelligent profit engine ────────────────────────────────
        decision = self._profit_engine.evaluate(
            position=position,
            realized_pnl_today=self.get_realized_pnl_today(),
            short_dte_flat_target=self._settings.profit_target_pct_short_dte,
            credit_spread_flat_target=self._settings.profit_target_pct,
            portfolio_daily_loss_limit=-self._settings.daily_loss_limit_dollars,
        )
        if decision.profit_pct != 0:
            logger.debug(
                "PROFIT ENGINE | %s | rule=%-14s pct=%+.0f%%  hwm=%.0f%%  "
                "target=%.0f%%  ratchet=%+.0f%%  vel=%+.2f%%/h  θ-excess=%.1fx  close=%s",
                position.ticker, decision.rule,
                decision.profit_pct * 100, decision.hwm_pct * 100,
                decision.effective_target * 100, decision.ratchet_stop_pct * 100,
                decision.velocity_1h, decision.theta_excess, decision.should_close,
            )
        if decision.should_close:
            self._profit_engine.clear_position(position.position_id)
            await self._close_position(position, decision.reason)
            return

        # ── Stop-loss ────────────────────────────────────────────────
        # Hard floor: 2× entry credit (absolute backstop, never negotiable).
        # Ratchet stop is already handled inside the engine above — if ratchet fired
        # we returned above. This block only fires when ratchet hasn't activated yet
        # and loss breaches the hard floor.
        hard_stop = -abs(position.entry_price * 100 * position.contracts * self._settings.stop_loss_multiplier)
        if position.unrealized_pnl <= hard_stop:
            rolled = await self._attempt_roll(position)
            if not rolled:
                self._profit_engine.clear_position(position.position_id)
                await self._close_position(
                    position,
                    f"Hard stop: 2× entry hit (unrealized=${position.unrealized_pnl:.0f} ≤ ${hard_stop:.0f})",
                )
            return

        # Mark as TESTED if underlying through short strike
        await self._check_tested_status(position)

    @staticmethod
    def _fetch_spot_sync(ticker: str) -> float:
        import yfinance as yf
        tk = yf.Ticker(ticker)
        try:
            fi   = tk.fast_info
            spot = float(getattr(fi, "last_price", None) or fi.get("lastPrice", 0) or 0)
        except Exception:
            spot = 0.0
        if spot <= 0:
            info = tk.info or {}
            spot = float(info.get("regularMarketPrice") or info.get("currentPrice") or 0)
        return spot

    async def _check_tested_status(self, position: OpenPosition) -> None:
        """Mark TESTED if spot has crossed through the short strike."""
        try:
            spot = await asyncio.to_thread(self._fetch_spot_sync, position.ticker)
            if spot <= 0:
                return

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

        self._db.execute(
            "UPDATE positions SET status=?, close_date=?, close_price=?, close_source=?, "
            "realized_pnl=?, last_reviewed=? WHERE position_id=?",
            (
                PositionStatus.CLOSED.value,
                date.today().isoformat(),
                round(position.current_price, 4),
                "lifecycle",
                round(position.unrealized_pnl, 2),
                datetime.now(tz=timezone.utc).isoformat(),
                position.position_id,
            ),
        )
        self._db.commit()
        logger.info("CLOSED: %s | reason: %s | PnL: $%.0f",
                    position.ticker, reason, position.unrealized_pnl)

        # Link realized P&L back to the decision chain that opened this position
        _decision_chain_close(
            str(self._settings.db_path),
            position.position_id,
            round(position.unrealized_pnl, 2),
        )

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
            near_expiry = [pos for pos in positions if (pos.expiry_date - today).days <= 2]
            spots = await asyncio.gather(
                *[asyncio.to_thread(self._fetch_spot_sync, pos.ticker) for pos in near_expiry],
                return_exceptions=True,
            )
            for pos, spot_or_exc in zip(near_expiry, spots):
                dte  = (pos.expiry_date - today).days
                spot = float(spot_or_exc) if isinstance(spot_or_exc, (int, float)) else 0.0
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
        earnings_date = getattr(position, "earnings_date", None)
        self._db.execute("""
            INSERT OR REPLACE INTO positions VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?
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
            getattr(position, "direction", "neutral"),
            getattr(position, "conviction_at_entry", 0.0),
            getattr(position, "regime_at_entry", ""),
            earnings_date.isoformat() if earnings_date else None,
            1 if getattr(position, "is_pre_earnings", False) else 0,
            # close columns — NULL on open
            None, None, "",
        ))
        self._db.commit()
        # Register with profit engine so entry-time Greeks are captured
        self._profit_engine.register_position(position)

    def add_journal_entry(
        self,
        position: "OpenPosition",
        spot_at_entry: float = 0.0,
        why_traded: str = "",
        macro_at_entry: str = "",
        ibkr_order_id: int = -1,
    ) -> None:
        """Write a human-readable trade journal entry explaining WHY this trade was taken."""
        import uuid as _uuid
        legs_summary = " / ".join(
            f"{l.action.upper()} {l.option_type[0].upper()} {l.strike:.0f} Δ{l.delta:.2f}"
            for l in position.legs
        )
        self._db.execute("""
            INSERT OR IGNORE INTO trade_journal VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            str(_uuid.uuid4()),
            position.position_id,
            position.ticker,
            position.strategy.value,
            getattr(position, "direction", ""),
            position.pillar.value,
            position.entry_date.isoformat(),
            round(spot_at_entry, 2),
            round(position.entry_price, 4),
            round(position.max_loss_dollars, 2),
            round(position.max_gain_dollars, 2),
            round(getattr(position, "reward_risk_ratio", position.max_gain_dollars / max(position.max_loss_dollars, 1)), 2),
            round(getattr(position, "conviction_at_entry", 0.0), 1),
            getattr(position, "gate_at_entry", "standard"),
            legs_summary,
            why_traded[:2000],
            macro_at_entry[:500],
            getattr(position, "regime_at_entry", ""),
            ibkr_order_id,
        ))
        self._db.commit()
        logger.info("JOURNAL: %s | %s | conv=%.0f | why=%s",
                    position.ticker, legs_summary,
                    getattr(position, "conviction_at_entry", 0), why_traded[:120])

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
                        delta=l.get("delta", 0.0),
                        gamma=l.get("gamma", 0.0),
                        theta=l.get("theta", 0.0),
                        vega=l.get("vega", 0.0),
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
                    direction=row[20] if len(row) > 20 else "neutral",
                    conviction_at_entry=float(row[21]) if len(row) > 21 and row[21] is not None else 0.0,
                    regime_at_entry=row[22] if len(row) > 22 and row[22] else "",
                    earnings_date=date.fromisoformat(row[23]) if len(row) > 23 and row[23] else None,
                    is_pre_earnings=bool(row[24]) if len(row) > 24 and row[24] is not None else False,
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

    def mark_position_closed(
        self,
        position_id: str,
        realized_pnl: float = 0.0,
        close_price: float = 0.0,
        source: str = "tws_reconcile",
    ) -> bool:
        """
        Mark a position closed from an external source (TWS fill detector, startup sync).
        Called when IBKR executes a GTC profit-target or stop-loss that our session
        didn't receive a live callback for. Returns True if a row was actually updated.
        """
        cursor = self._db.execute(
            "UPDATE positions SET status='closed', realized_pnl=?, close_price=?, "
            "close_date=?, close_source=?, last_reviewed=? "
            "WHERE position_id=? AND status IN ('open','tested','rolled')",
            (
                round(realized_pnl, 2),
                round(close_price, 4),
                date.today().isoformat(),
                source,
                datetime.now(tz=timezone.utc).isoformat(),
                position_id,
            ),
        )
        self._db.commit()
        updated = cursor.rowcount > 0
        if updated:
            logger.info(
                "POSITION CLOSED (external): id=%s pnl=$%.2f source=%s",
                position_id[:12], realized_pnl, source,
            )
            self._write_trade_record_from_id(position_id, realized_pnl, close_price, source)
        return updated

    def _write_trade_record_from_id(
        self, position_id: str, realized_pnl: float, close_price: float, notes: str
    ) -> None:
        """Write to trade_records using the already-closed positions row. INSERT OR IGNORE is safe."""
        try:
            row = self._db.execute(
                """SELECT ticker, strategy, pillar, entry_date, expiry_date,
                          entry_price, contracts, regime_at_entry, conviction_at_entry
                   FROM positions WHERE position_id = ?""",
                (position_id,),
            ).fetchone()
            if not row:
                return
            ticker, strategy, pillar, entry_date, expiry_date, entry_price, contracts, regime, conviction = row
            self._db.execute(
                "INSERT OR IGNORE INTO trade_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    position_id, ticker, strategy, pillar,
                    entry_date, date.today().isoformat(), expiry_date,
                    entry_price, round(close_price, 4), contracts,
                    round(realized_pnl, 2),
                    0.0, 0.0,  # commission / slippage
                    regime, conviction, "", notes,
                ),
            )
            self._db.commit()
        except Exception as exc:
            logger.debug("trade_record write error (mark_closed path): %s", exc)

    def get_open_position_by_ticker(self, ticker: str) -> "OpenPosition | None":
        """Return the first active position for a ticker, or None."""
        for p in self.get_open_positions():
            if p.ticker == ticker:
                return p
        return None

    def get_realized_pnl_today(self) -> float:
        """Sum of realized_pnl for positions closed today. Used by kill switch reset logic."""
        today = date.today().isoformat()
        row = self._db.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) FROM positions "
            "WHERE close_date=? AND status='closed'",
            (today,),
        ).fetchone()
        return float(row[0]) if row else 0.0

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
            position.regime_at_entry or None,
            position.conviction_at_entry or None,
            "",
            notes,
        ))
        self._db.commit()

    def get_performance_summary(self, lookback_days: int = 30) -> dict:
        """
        Aggregate closed trade outcomes for the last `lookback_days` days.

        Returns win rates, average P&L, and trade counts broken down by:
          - strategy type
          - regime at entry
          - conviction band (0-40, 40-60, 60-80, 80-100)

        Used by the afterhours attribution report and future feedback scoring.
        """
        cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()
        rows = self._db.execute("""
            SELECT strategy, regime_at_entry, conviction_at_entry,
                   realized_pnl, close_date
            FROM trade_records
            WHERE close_date IS NOT NULL AND close_date >= ?
        """, (cutoff,)).fetchall()

        if not rows:
            return {"period_days": lookback_days, "total_trades": 0}

        total = len(rows)
        wins = sum(1 for r in rows if (r[3] or 0) > 0)
        total_pnl = sum((r[3] or 0) for r in rows)

        # By strategy
        by_strategy: dict[str, dict] = {}
        for strategy, _, _, pnl, _ in rows:
            s = by_strategy.setdefault(strategy, {"trades": 0, "wins": 0, "pnl": 0.0})
            s["trades"] += 1
            s["wins"]   += 1 if (pnl or 0) > 0 else 0
            s["pnl"]    += pnl or 0
        for s in by_strategy.values():
            s["win_rate"] = round(s["wins"] / s["trades"], 3) if s["trades"] else 0.0
            s["avg_pnl"]  = round(s["pnl"] / s["trades"], 2) if s["trades"] else 0.0

        # By regime at entry
        by_regime: dict[str, dict] = {}
        for _, regime, _, pnl, _ in rows:
            key = regime or "unknown"
            r = by_regime.setdefault(key, {"trades": 0, "wins": 0, "pnl": 0.0})
            r["trades"] += 1
            r["wins"]   += 1 if (pnl or 0) > 0 else 0
            r["pnl"]    += pnl or 0
        for r in by_regime.values():
            r["win_rate"] = round(r["wins"] / r["trades"], 3) if r["trades"] else 0.0
            r["avg_pnl"]  = round(r["pnl"] / r["trades"], 2) if r["trades"] else 0.0

        # By conviction band
        def _band(score: float | None) -> str:
            if score is None: return "unknown"
            if score < 40:    return "0-40"
            if score < 60:    return "40-60"
            if score < 80:    return "60-80"
            return "80-100"

        by_conviction: dict[str, dict] = {}
        for _, _, conviction, pnl, _ in rows:
            key = _band(conviction)
            c = by_conviction.setdefault(key, {"trades": 0, "wins": 0, "pnl": 0.0})
            c["trades"] += 1
            c["wins"]   += 1 if (pnl or 0) > 0 else 0
            c["pnl"]    += pnl or 0
        for c in by_conviction.values():
            c["win_rate"] = round(c["wins"] / c["trades"], 3) if c["trades"] else 0.0
            c["avg_pnl"]  = round(c["pnl"] / c["trades"], 2) if c["trades"] else 0.0

        return {
            "period_days":    lookback_days,
            "total_trades":   total,
            "wins":           wins,
            "win_rate":       round(wins / total, 3),
            "total_pnl":      round(total_pnl, 2),
            "avg_pnl":        round(total_pnl / total, 2),
            "by_strategy":    by_strategy,
            "by_regime":      by_regime,
            "by_conviction":  by_conviction,
        }

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

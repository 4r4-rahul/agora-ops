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
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ..core.config import AgoraSettings, get_settings
from ..core.models import (
    OpenPosition,
    PositionStatus,
    SpreadLeg,
    StrategyPillar,
    StrategyType,
)
from ..ops.decision_chains import update_close as _decision_chain_close
from ..ops.llm_cost_log import ensure_table as _ensure_llm_cost_table
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
        # LLM exit intelligence (thesis re-validation) — injected by the session via
        # set_exit_agent(). PositionManager is the single exit owner, so it runs this
        # itself for EVERY strategy rather than a separate patrol loop. None → skipped.
        self._exit_agent = None
        self._macro_ctx = None
        # Long-options trailing-stop high-water marks (position_id → peak P&L fraction).
        # Persisted so a restart does not reset the trail and convert a locked winner
        # back into a runner. This is exit state, so it lives with the exit owner.
        self._long_peak_path = self._settings.db_path.parent / "long_peak_pnl.json"
        self._long_peak_pnl: dict[str, float] = self._load_long_peaks()
        # position_id → conviction-dynamic profit target (cached; derived from long_journal
        # conviction at first check). Fixes the prior gap where the dynamic target was
        # computed at entry but never applied at exit (OpenPosition carries no metadata).
        self._long_profit_tgt: dict[str, float] = {}
        # position_id → stop-loss multiplier, derived from entry IVR (cached). Hypothesis:
        # when we overpaid for vol (high entry IVR) the option bleeds faster on vega/theta,
        # so cut losers a touch sooner; when vol was cheap, give a touch more room. Bounded
        # ±15% — intentionally small until the exit-quality report validates the direction.
        self._long_stop_mult: dict[str, float] = {}
        # Partial scale-out: session handler that closes a SUBSET of contracts, and the set
        # of positions already scaled (one-time per position). Handler injected at startup.
        self._on_partial_close = None
        self._long_scaled: set[str] = set()
        # Stage 2: exit decisions mark off TWS/IBKR's exact per-leg unrealizedPNL (not a yfinance
        # estimate). Injected by the session → returns the live portfolio items from the TWS poller.
        # None / no-match → fall back to the existing yfinance mark (with its no-data HOLD guard).
        self._tws_pnl_getter: Any = None
        # Stale-mark safety backstop: count consecutive no-quote refresh cycles per position and
        # cache the last good underlying spot, so a position blowing out DURING a chronic quote
        # outage can still be stopped on a conservative intrinsic mark (the no-data HOLD guard
        # would otherwise freeze its mark and suppress the hard stop to the 21-DTE date close).
        self._stale_cycles: dict[str, int] = {}
        self._last_spot: dict[str, float] = {}

    def set_macro_context(self, ctx: Any) -> None:
        """Called by session after every macro synthesis — keeps engine regime-aware."""
        self._profit_engine.set_macro_context(ctx)
        self._macro_ctx = ctx

    def set_exit_agent(self, agent: Any) -> None:
        """Inject the ExitIntelligenceAgent so PositionManager can run the LLM thesis
        re-validation itself (single exit owner). Called once by the session at startup."""
        self._exit_agent = agent

    def set_partial_close_handler(self, fn: Any) -> None:
        """Inject the session's partial-close executor (closes a subset of contracts)."""
        self._on_partial_close = fn

    def apply_partial_close(self, position_id: str, qty: int, close_price: float,
                            realized_pnl: float) -> None:
        """Resize an open position after a scale-out: reduce contracts (scaling max
        loss/gain proportionally) and record the closed slice as its own trade_record for
        attribution. Daily-P&L (breaker) reads closed positions, so the locked gain is
        credited only at full close — conservative and safe."""
        try:
            row = self._db.execute(
                "SELECT contracts, max_loss_dollars, max_gain_dollars, ticker, strategy, "
                "pillar, entry_price, entry_date, expiry_date, regime_at_entry, "
                "conviction_at_entry FROM positions WHERE position_id=?",
                (position_id,),
            ).fetchone()
            if not row:
                return
            old_ct = int(row[0])
            new_ct = max(0, old_ct - qty)
            if new_ct <= 0:
                return
            ratio = new_ct / old_ct
            self._db.execute(
                "UPDATE positions SET contracts=?, max_loss_dollars=?, max_gain_dollars=?, "
                "last_reviewed=? WHERE position_id=?",
                (new_ct, row[1] * ratio, row[2] * ratio,
                 datetime.now(tz=UTC).isoformat(), position_id),
            )
            self._db.execute(
                "INSERT OR IGNORE INTO trade_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"{position_id}__scale", row[3], row[4], row[5], row[7],
                 date.today().isoformat(), row[8], row[6], close_price, qty,
                 realized_pnl, 0.0, 0.0, row[9] or None, row[10] or None, "", "scale_out_half"),
            )
            self._db.commit()
            logger.info("PARTIAL SCALE-OUT %s: closed %d/%d @ $%.2f realized=$%.0f, %d remain",
                        row[3], qty, old_ct, close_price, realized_pnl, new_ct)
        except Exception as exc:
            logger.error("apply_partial_close failed [%s]: %s", position_id, exc, exc_info=True)

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
        # Running MFE/MAE in DOLLARS — updated at every mark (every exit-eval cycle, ~minutes), so
        # they capture the true peak/trough excursion instead of being reconstructed from the sparse
        # DAILY lifecycle_snapshots (positions hold ~2 days → 1-2 daily points → MFE/MAE missed).
        # The feature store converts these to max_favorable_pct / max_adverse_pct at build time.
        if "peak_unrealized_pnl" not in existing_cols:
            conn.execute("ALTER TABLE positions ADD COLUMN peak_unrealized_pnl REAL")
        if "trough_unrealized_pnl" not in existing_cols:
            conn.execute("ALTER TABLE positions ADD COLUMN trough_unrealized_pnl REAL")
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

    async def review_on_shock(self, reason: str = "shock") -> dict:
        """On-demand position review triggered by a market SHOCK (sharp SPY/VIX move or
        breaking news). Re-marks every open position to CURRENT prices and runs the full
        exit / stop-loss / profit-target check IMMEDIATELY, rather than waiting for the next
        periodic lifecycle cycle. This is what lets the system PROTECT positions the news hit
        — cut a stop or lock a profit within seconds of the move instead of minutes later."""
        now_et = datetime.now(tz=ET)
        if not self._is_market_hours(now_et):
            return {"reviewed": 0, "skipped": "after_hours"}
        positions = self.get_open_positions()
        if not positions:
            return {"reviewed": 0}
        logger.info("SHOCK POSITION REVIEW (%s) — re-marking + checking %d open position(s)",
                    reason, len(positions))
        await asyncio.gather(*[self._refresh_position_price(p) for p in positions],
                             return_exceptions=True)
        positions = self.get_open_positions()
        n_before = len(positions)
        for pos in positions:
            try:
                await self._check_position_targets(pos)
            except Exception as exc:
                logger.warning("Shock review error on %s: %s", pos.ticker, exc)
        closed = n_before - len(self.get_open_positions())
        logger.info("SHOCK POSITION REVIEW (%s) complete — %d reviewed, %d closed/protected",
                    reason, n_before, closed)
        return {"reviewed": n_before, "closed": closed}

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
        n_quoted    = 0          # legs with a REAL bid/ask — used to reject a no-data 0.00 mark
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
                    if bid > 0 or ask > 0:
                        n_quoted += 1   # a real one/two-sided quote exists for this leg

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

        return {"spot": spot, "current_mid": current_mid, "updated_legs": updated_legs,
                "legs_quoted": n_quoted, "legs_total": len(legs),
                "quote_ok": (spot > 0 and n_quoted == len(legs) and len(legs) > 0)}

    def set_tws_pnl_getter(self, getter: Any) -> None:
        """Inject the live TWS portfolio-items source (the IBKR poller). Exit P&L marks off this."""
        self._tws_pnl_getter = getter

    def _tws_unrealized(self, position: OpenPosition) -> float | None:
        """Exact unrealized P&L from TWS for THIS position, matched leg-by-leg (symbol+strike+right+
        expiry) against IBKR's portfolio items. Returns None if the feed is absent or any leg is
        unmatched (→ caller uses the yfinance fallback) — never a partial/guessed number."""
        getter = self._tws_pnl_getter
        if getter is None:
            return None
        try:
            items = getter() or []
            if not items:
                return None
            book: dict[tuple, float] = {}
            for it in items:
                key = (str(it.get("symbol")), float(it.get("strike") or 0),
                       str(it.get("right") or "").upper(), str(it.get("expiry") or ""))
                book[key] = book.get(key, 0.0) + float(it.get("unrealized_pnl") or 0)
            total = 0.0
            for lg in getattr(position, "legs", []) or []:
                right = "C" if str(getattr(lg, "option_type", "")).lower().startswith("c") else "P"
                exp = getattr(lg, "expiration", None)
                exp_str = exp.strftime("%Y%m%d") if isinstance(exp, date) else str(exp or "")
                key = (position.ticker, float(getattr(lg, "strike", 0) or 0), right, exp_str)
                if key not in book:
                    return None   # a leg isn't in the TWS book → don't trust a partial match
                total += book[key]
            return round(total, 2)
        except Exception:
            return None

    async def _refresh_position_price(self, position: OpenPosition) -> None:
        """
        Mark the position for exit decisions. PRIMARY source = TWS/IBKR's exact per-leg unrealizedPNL
        (Stage 2); the yfinance options-chain mid is the fallback + supplies per-leg greeks.

        Runs the synchronous yfinance calls in a thread pool so the event loop stays unblocked.
        """
        # ── TWS-FIRST: use IBKR's exact unrealizedPNL when the live poller has this position ──
        _tws_u = self._tws_unrealized(position)
        if _tws_u is not None:
            _ctr = max(1, int(getattr(position, "contracts", 1) or 1))
            _tws_mid = round(position.entry_price + _tws_u / (100 * _ctr), 4)
            self._stale_cycles[position.position_id] = 0
            self._update_position_price(position.position_id, _tws_mid, _tws_u)
            # still refresh per-leg greeks from yfinance (best-effort, display only)
            try:
                _r = await asyncio.to_thread(self._fetch_price_data_sync,
                                             position.ticker, position.legs, date.today())
                if _r and _r.get("quote_ok") and _r.get("updated_legs"):
                    self._db.execute("UPDATE positions SET legs_json=? WHERE position_id=?",
                                     (json.dumps(_r["updated_legs"]), position.position_id))
                    self._db.commit()
            except Exception:
                pass
            return
        # ── FALLBACK: TWS feed has no value for this position → yfinance mid (with HOLD guard) ──
        try:
            result = await asyncio.to_thread(
                self._fetch_price_data_sync,
                position.ticker, position.legs, date.today(),
            )
            if not result:
                return

            _pid  = position.position_id
            _spot = float(result.get("spot", 0.0) or 0.0)
            if _spot > 0:
                self._last_spot[_pid] = _spot   # underlying is usually quoted even when options aren't

            # No-data guard: when the paper account has no live option quote, every leg's
            # bid/ask is 0 and current_mid sums to 0.0. Writing that 0.00 mark fabricates a
            # P&L — for a CREDIT spread (entry_price<0) unrealized=(0-(-credit))*100 = +max_gain,
            # which the profit engine reads as 100% profit and LOCK-IN-closes a day-1 spread (the
            # -$1,374 bleed). Treat a missing/partial quote as a DATA OUTAGE: keep the last good
            # mark and HOLD. Only write a mark backed by a real quote on every leg.
            if not result.get("quote_ok", False):
                self._stale_cycles[_pid] = self._stale_cycles.get(_pid, 0) + 1
                logger.debug("Stale/missing mark for %s — %d/%d legs quoted (stale x%d); HOLD",
                             position.ticker, result.get("legs_quoted", 0),
                             result.get("legs_total", 0), self._stale_cycles[_pid])
                return

            self._stale_cycles[_pid] = 0   # fresh quote — clear the stale counter
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

    def _intrinsic_unrealized(self, position: OpenPosition, spot: float) -> float | None:
        """Conservative intrinsic (no time value) mark for a spread — used ONLY as a stale-quote
        HARD-STOP backstop, never for profit-taking. Intrinsic ≤ true option value, so it can
        never over-stop; near a genuine blowout (deep ITM) intrinsic ≈ true value, so it still
        catches the tail. Needs only the (still-quoted) underlying spot + strikes. None on error."""
        try:
            legs = getattr(position, "legs", None)
            if not legs or spot <= 0:
                return None
            spread_mid = 0.0
            for lg in legs:
                otype  = getattr(lg, "option_type", None)
                strike = float(getattr(lg, "strike", 0.0) or 0.0)
                action = getattr(lg, "action", None)
                if strike <= 0 or otype not in ("call", "put"):
                    return None
                intrinsic = max(0.0, spot - strike) if otype == "call" else max(0.0, strike - spot)
                spread_mid += intrinsic if action == "buy" else -intrinsic
            return round((spread_mid - position.entry_price) * 100 * position.contracts, 2)
        except Exception:
            return None

    # ── Target checks ──────────────────────────────────────────────

    async def _check_position_targets(self, position: OpenPosition) -> None:
        today = date.today()

        # Long options use their OWN exit rules (5-day time stop, conviction-dynamic
        # profit target, trailing + flat stops) — NOT the spread-calibrated logic below
        # (the 21-DTE rule would force-close them on entry day, and the profit-engine /
        # 2x-credit stop math is wrong for long premium). PositionManager is the single
        # exit owner for ALL strategies; it just branches on the rule set here.
        _strat = str(getattr(position.strategy, "value", position.strategy))
        if _strat in ("long_call", "long_put"):
            await self._check_long_options_targets(position)
            return

        # Pre-earnings IV-crush protection (T-1). Earnings report after-hours and IV
        # collapses ~80%→~30% on the print regardless of direction; close the day before
        # to capture elevated IV. Highest priority — fires before the 21-DTE rule.
        if getattr(position, "is_pre_earnings", False) and position.earnings_date is not None:
            days_to_earnings = (position.earnings_date - today).days
            if days_to_earnings <= 1:
                await self._close_position(
                    position,
                    f"Pre-earnings IV-crush close (earnings {position.earnings_date}, "
                    f"T-{days_to_earnings})",
                    source="pre_earnings",
                )
                return

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
            # Credit-spread stop grace (THE expectancy lever): a credit spread's day-1 unrealized
            # P&L is dominated by bid-ask/natural MARK NOISE, not real loss, so the 2×-credit floor
            # routinely trips on day 1 and kills the position before theta works — the cause of the
            # 6% credit-spread win rate (vs ~70% norm; 16/18 closed at 1.0d). Credit spreads are
            # theta trades AND defined-risk, so within the grace window we suppress the 2×-credit
            # stop UNLESS the loss is a genuine blowout (near max_loss — the only real risk).
            _is_credit = (position.entry_price or 0) < 0
            _grace = int(getattr(self._settings, "spread_stop_min_hold_days", 0))
            try:
                _held = (date.today() - position.entry_date).days
            except Exception:
                _held = 99
            _ml = abs(getattr(position, "max_loss_dollars", 0.0) or 0.0)
            _blowout_frac = float(getattr(self._settings, "spread_stop_blowout_max_loss_frac", 0.85))
            _genuine_blowout = _ml > 0 and position.unrealized_pnl <= -_blowout_frac * _ml
            if _is_credit and _held < _grace and not _genuine_blowout:
                logger.info(
                    "Credit-spread stop GRACE [%s]: day %d < %d, unrealized=$%.0f not near max_loss "
                    "$%.0f — holding for theta (defined risk capped)",
                    position.ticker, _held, _grace, position.unrealized_pnl, _ml)
            else:
                rolled = await self._attempt_roll(position)
                if not rolled:
                    self._profit_engine.clear_position(position.position_id)
                    await self._close_position(
                        position,
                        f"Hard stop: 2× entry hit (unrealized=${position.unrealized_pnl:.0f} ≤ ${hard_stop:.0f})",
                    )
                return

        # ── Stale-quote safety backstop ──────────────────────────────
        # The no-data HOLD guard freezes a position's mark when quotes go missing, so a blowout
        # that happens DURING a chronic outage is invisible to the mark-based hard stop above.
        # After several consecutive no-quote cycles, re-derive a CONSERVATIVE intrinsic mark from
        # the still-quoted underlying and apply the hard stop on that (never profit-taking). This
        # closes the "no-stop tail" the C-suite flagged as the biggest risk of the mark guard.
        _pid = position.position_id
        _stale = getattr(self, "_stale_cycles", {})
        if _stale.get(_pid, 0) >= getattr(self._settings, "spread_stale_stop_cycles", 5):
            _intr = self._intrinsic_unrealized(position, getattr(self, "_last_spot", {}).get(_pid, 0.0))
            # Defined-risk spreads cap at max_loss, so the 2×-credit hard stop is often unreachable;
            # the meaningful backstop is "near max loss during an outage" — close to dodge short-leg
            # assignment/pin and free capital. Trigger = whichever is REACHABLE: the 2× hard stop OR
            # 95% of max loss.
            _ml = abs(getattr(position, "max_loss_dollars", 0.0) or 0.0)
            _stop_thr = max(hard_stop, -0.95 * _ml) if _ml > 0 else hard_stop
            if _intr is not None and _intr <= _stop_thr:
                self._profit_engine.clear_position(_pid)
                await self._close_position(
                    position,
                    f"Stale-quote intrinsic stop: {_stale.get(_pid, 0)} stale cycles, "
                    f"intrinsic=${_intr:.0f} ≤ ${_stop_thr:.0f} (max_loss=${_ml:.0f})",
                    source="stale_model_stop",
                )
                return

        # ── LLM thesis re-validation (intelligence layer, single owner) ──────────────
        # Spreads use the analyst_journal thesis (agent's default lookup). Closes early
        # only on a genuine thesis break, after the deterministic floors above.
        if await self._maybe_llm_exit(position, is_long=False):
            return

        # Mark as TESTED if underlying through short strike
        await self._check_tested_status(position)

    # ── Long-options exit rules (single owner; distinct from spread logic) ─────

    def _load_long_peaks(self) -> dict:
        try:
            if self._long_peak_path.exists():
                return {k: float(v) for k, v in json.loads(self._long_peak_path.read_text()).items()}
        except Exception as exc:
            logger.debug("long_peak load failed: %s", exc)
        return {}

    def _save_long_peaks(self) -> None:
        try:
            self._long_peak_path.write_text(json.dumps(self._long_peak_pnl))
        except Exception as exc:
            logger.debug("long_peak save failed: %s", exc)

    def _long_thesis_for(self, position: OpenPosition) -> dict:
        """Build an ExitIntelligenceAgent thesis dict from the long_journal entry, so the agent
        can re-validate a long with real entry context (its thesis lives in long_journal). Resolved
        by position_id, else by the position's ticker+strike+expiry structure — the entry's thesis
        is in long_journal but its position_id is unlinked for ~99% of longs (incl. healer-adopted
        positions like the unmanaged TSM -$479). {} → agent evaluates on position state alone."""
        try:
            from agora.agents.long_options_agent import LongOptionsAgent as _LOA
            leg = (getattr(position, "legs", None) or [None])[0]
            strike = float(getattr(leg, "strike", 0) or 0) if leg is not None else None
            expiry = str(getattr(position, "expiry_date", "") or "")
            row = _LOA.find_entry_journal(
                str(self._settings.db_path), position.position_id,
                position.ticker, strike, expiry)
            if not row:
                return {}
            try:
                stack = json.loads(row.get("signal_stack") or "{}")
            except Exception:
                stack = {}
            return {
                "direction":       row.get("direction"),
                "magnitude_pct":   None,
                "horizon_days":    self._settings.long_options_max_hold_days,
                "confidence_pct":  row.get("conviction_score"),
                "strategy_family": "long_directional",
                "kill_conditions": [
                    "directional thesis broken — trend or institutional flow reversed",
                    "underlying RSI hit an extreme against the position",
                    "expected catalyst passed with no follow-through",
                ],
                "reasoning_trace": (f"entry signals={stack} | regime={row.get('regime')} "
                                    f"| flow={row.get('flow_direction')}"),
                "thesis_date":     row.get("decided_at_utc"),
            }
        except Exception as exc:
            logger.debug("_long_thesis_for failed [%s]: %s",
                         getattr(position, "position_id", "?"), exc)
            return {}

    async def _maybe_llm_exit(self, position: OpenPosition, is_long: bool) -> bool:
        """
        Throttled LLM thesis re-validation — the intelligence layer of the single exit
        owner, run for EVERY strategy after the deterministic checks. Returns True if it
        closed the position.

        Division of labor (avoids the over-aggressive churn seen when any CLOSE_NOW was
        honored): the deterministic floors own profit-taking / stops / time; the LLM owns
        THESIS BREAK only. So we act on its close ONLY when the thesis is genuinely
        INVALIDATED or a kill condition TRIGGERED — not on a soft "WEAKENING" read.
        """
        agent = self._exit_agent
        if agent is None:
            return False
        # Min-hold guard: the thesis needs room to play out. For LONGS this was effectively
        # day-0-only, which let the LLM close 100% of longs on day 1 (cutting winners). Longs
        # now hold long_exit_llm_min_hold_days (default 2); the deterministic stops own the
        # downside until then. Shorts/spreads keep the day-0 guard.
        _min_hold = (getattr(self._settings, "long_exit_llm_min_hold_days", 2) if is_long
                     else getattr(self._settings, "spread_exit_llm_min_hold_days", 3))
        if (date.today() - position.entry_date).days < _min_hold:
            return False
        # Real-mark precondition: never let the exit brain act on a 0.00/missing mark. The payload
        # would carry a fabricated P&L ("max loss"/"worthless") and the LLM closes a good position
        # on bad data. Treat a 0.00 mark as a data outage and HOLD (the source guard normally
        # prevents this; this is defense-in-depth for cold-start / never-quoted positions).
        if position.current_price == 0.0:
            return False
        # Winner-lock (longs): a GREEN long belongs to the conviction-scaled trailing stop, which
        # was built to let winners run. Don't even evaluate it — never spend a token to (and never
        # let the LLM) cut a winner. The LLM only adjudicates RED longs (genuine thesis-break vs
        # intraday noise). This is the fix for the day-1 churn that closed META +$1,435 / ORCL
        # +$1,325 on their entry day. Toggle via long_exit_llm_winner_lock.
        _winner_lock = is_long and getattr(self._settings, "long_exit_llm_winner_lock", True)
        if _winner_lock and (position.unrealized_pnl or 0) >= 0:
            return False
        try:
            interval = getattr(self._settings, "exit_intelligence_interval_hours", 1.0)
            if not agent.should_evaluate(position.position_id, interval):
                return False
            thesis = self._long_thesis_for(position) if is_long else None
            rec = await agent.evaluate(position, self._macro_ctx, thesis_override=thesis, act=False)
            if rec is None or agent.shadow_mode:
                return False
            # RED longs under winner-lock require a HARD kill-condition trigger — not a soft
            # "INVALIDATED" read, which the LLM fires too readily on intraday noise. Shorts/spreads
            # keep the original kill-or-invalidated rule.
            if _winner_lock:
                strong = rec.kill_triggered
            elif (not is_long) and getattr(self._settings, "spread_exit_require_kill", True):
                # Credit spreads are theta trades: a bare "INVALIDATED" read (often off an empty/
                # undocumented thesis) closed 39-DTE spreads on day 1. Require a HARD kill-condition
                # trigger — the deterministic stops/DTE own everything else.
                strong = rec.kill_triggered
            else:
                strong = rec.kill_triggered or rec.thesis_validity == "INVALIDATED"
            if rec.should_close and strong:
                reason = f"LLM thesis-exit: {rec.thesis_validity}/{rec.kill_condition_status} | " \
                         f"{(rec.recommendation_reasoning or '')[:60]}"
                if is_long:
                    await self._close_long(position, reason, "thesis_exit")
                else:
                    await self._close_position(position, reason, source="thesis_exit")
                return True
        except Exception as exc:
            logger.warning("LLM exit check failed [%s]: %s", position.ticker, exc)
        return False

    async def _close_long(self, position: OpenPosition, reason: str, source: str) -> None:
        """Close a long-options position via the canonical path, then fire the
        signal-stats learning update and clear its trailing peak."""
        await self._close_position(position, reason, source=source)
        try:
            from agora.agents.long_options_agent import LongOptionsAgent as _LOA
            leg = (getattr(position, "legs", None) or [None])[0]
            _strike = float(getattr(leg, "strike", 0) or 0) if leg is not None else None
            _LOA.update_signal_stats(
                str(self._settings.db_path), position.position_id, position.unrealized_pnl,
                ticker=position.ticker, strike=_strike,
                expiry=str(getattr(position, "expiry_date", "") or ""))
        except Exception as exc:
            logger.debug("update_signal_stats failed [%s]: %s", position.ticker, exc)
        self._long_peak_pnl.pop(position.position_id, None)
        self._long_scaled.discard(position.position_id)
        self._long_profit_tgt.pop(position.position_id, None)
        self._long_stop_mult.pop(position.position_id, None)
        self._save_long_peaks()

    def _long_profit_target_for(self, position_id: str) -> float:
        """Conviction-dynamic profit target for a long position (40/50/75/100% by
        conviction), read once from long_journal and cached. Falls back to the config
        default if the journal row is missing."""
        if position_id in self._long_profit_tgt:
            return self._long_profit_tgt[position_id]
        tgt = self._settings.long_options_profit_target_pct
        try:
            with sqlite3.connect(str(self._settings.db_path), timeout=5) as conn:
                row = conn.execute(
                    "SELECT conviction_score FROM long_journal WHERE position_id=? "
                    "AND outcome='proceed' ORDER BY journal_id DESC LIMIT 1",
                    (position_id,),
                ).fetchone()
            if row and row[0] is not None:
                from agora.agents.long_options_agent import LongOptionsAgent as _LOA
                tgt = _LOA._conviction_profit_target(int(row[0]))
        except Exception as exc:
            logger.debug("_long_profit_target_for [%s]: %s", position_id, exc)
        self._long_profit_tgt[position_id] = tgt
        return tgt

    def _long_stop_for(self, position_id: str, base_stop: float) -> float:
        """Entry-IVR-scaled stop (bounded ±15%). High IVR → tighter (overpaid for vol,
        faster bleed); low IVR → looser. Cached. Falls back to base_stop. This is a
        deliberately small effect pending validation from the exit-quality report."""
        if position_id in self._long_stop_mult:
            return base_stop * self._long_stop_mult[position_id]
        mult = 1.0
        try:
            with sqlite3.connect(str(self._settings.db_path), timeout=5) as conn:
                row = conn.execute(
                    "SELECT ivr FROM long_journal WHERE position_id=? AND outcome='proceed' "
                    "ORDER BY journal_id DESC LIMIT 1",
                    (position_id,),
                ).fetchone()
            if row and row[0] is not None:
                ivr = float(row[0])
                if ivr >= 50:
                    mult = 0.85
                elif ivr <= 25:
                    mult = 1.15
        except Exception as exc:
            logger.debug("_long_stop_for [%s]: %s", position_id, exc)
        self._long_stop_mult[position_id] = mult
        return base_stop * mult

    async def _check_long_options_targets(self, position: OpenPosition) -> None:
        """
        Long-call / long-put exit rules. Priority: time stop → profit target →
        trailing stop → flat stop. Mirrors the directional-swing playbook; the
        deterministic floors here are layered with the LLM thesis check (added in a
        later step). update_signal_stats is fired on every close (learning loop).
        """
        s = self._settings
        max_hold      = s.long_options_max_hold_days
        stop_tgt      = s.long_options_stop_loss_pct
        trail_trigger = s.long_options_trailing_stop_trigger
        trail_floor   = s.long_options_trailing_stop_floor
        today         = date.today()
        pid           = position.position_id

        # A. Hard 5-day time stop — close regardless of P&L.
        age_days = (today - position.entry_date).days
        if age_days >= max_hold:
            logger.info("LongOptions time-stop [%s] held %dd ≥ %dd", position.ticker, age_days, max_hold)
            await self._close_long(position, f"5-day time stop (held {age_days}d)", "time_stop")
            return

        if position.entry_price <= 0 or position.contracts <= 0:
            return

        # No-data mark guard: a 0.00 long mark (missing quote) makes unrealized=(0-entry)*… read
        # as pnl_pct=-100%, tripping a FALSE stop_loss below. Treat a 0.00 mark as a data outage
        # and HOLD the mark-based checks (the date-based time stop above already had its turn).
        if position.current_price == 0.0:
            logger.debug("LongOptions stale mark [%s] (price=0.00) — HOLD mark-based checks", position.ticker)
            return

        pnl_pct = position.unrealized_pnl / (position.entry_price * position.contracts * 100)

        # Update trailing high-water mark (persisted across restarts).
        peak = self._long_peak_pnl.get(pid, 0.0)
        if pnl_pct > peak:
            self._long_peak_pnl[pid] = pnl_pct
            peak = pnl_pct
            self._save_long_peaks()

        # Conviction-dynamic profit target (derived from long_journal conviction, cached).
        pos_profit_tgt = self._long_profit_target_for(pid)

        # Urgency taper: within 3 days of target close, halve the profit target.
        days_to_close = (position.target_close_date - today).days if position.target_close_date else 999
        urgent = days_to_close <= 3
        eff_profit_tgt  = pos_profit_tgt * 0.5 if urgent else pos_profit_tgt

        # DTE-aware trailing floor: tighten as EXPIRY nears (theta accelerates → lock gains
        # rather than give them back to decay). Combined with the target-close urgency taper;
        # the tighter of the two wins.
        dte = (position.expiry_date - today).days
        if dte <= 7:
            dte_mult = 0.5
        elif dte <= 14:
            dte_mult = 0.75
        else:
            dte_mult = 1.0
        # L1 "let winners run": scale the trailing floor by conviction (via its profit target)
        # so a high-conviction CONVEX winner gets room to run instead of being stopped on
        # intraday noise at peak-15%, while a weak (scalp) signal stays tight and locks fast.
        # Long options pay off through the fat right tail — a too-tight uniform trail clips it.
        #   conviction 2/3/4/5  →  base floor ≈ 0.12 / 0.15 / 0.22 / 0.30  (pre DTE/urgency).
        conv_floor = trail_floor * (pos_profit_tgt / 0.50)
        eff_trail_floor = conv_floor * min(dte_mult, 0.5 if urgent else 1.0)

        # A2. Scale-out: lock HALF of a high-conviction winner (target > 50%) once it
        # reaches +50%, and let the rest run to its higher target under the trailing stop.
        # One-time per position. Captures gains the all-or-nothing target would risk giving
        # back, while keeping upside on the runner.
        if (pos_profit_tgt > 0.50 and pnl_pct >= 0.50 and position.contracts >= 2
                and pid not in self._long_scaled and self._on_partial_close is not None):
            half = position.contracts // 2
            logger.info("LongOptions scale-out [%s] +%.0f%% — locking %d/%d, running rest to %.0f%%",
                        position.ticker, pnl_pct * 100, half, position.contracts, pos_profit_tgt * 100)
            self._long_scaled.add(pid)   # set before await so a slow fill can't double-trigger
            await self._on_partial_close(position, half, f"scale-out at +{pnl_pct*100:.0f}%")
            return   # re-evaluate next cycle with the reduced size

        # B. Hard profit target (before trail activates).
        if pnl_pct >= eff_profit_tgt and peak < trail_trigger:
            logger.info("LongOptions profit-target [%s] +%.0f%% — closing", position.ticker, pnl_pct * 100)
            await self._close_long(position, f"profit target +{pnl_pct*100:.0f}%", "profit_target")
            return

        # C. Trailing stop (active once peak ≥ trigger).
        if peak >= trail_trigger:
            trail_stop = peak - eff_trail_floor
            if pnl_pct <= trail_stop:
                logger.info("LongOptions trail-stop [%s] pnl=+%.0f%% peak=+%.0f%% — closing",
                            position.ticker, pnl_pct * 100, peak * 100)
                await self._close_long(position, f"trailing stop (peak +{peak*100:.0f}%)", "trailing_stop")
                return

        # D. Flat stop loss (pre-trail), entry-IVR-scaled.
        eff_stop = self._long_stop_for(pid, stop_tgt)
        if pnl_pct <= -eff_stop:
            logger.info("LongOptions stop-loss [%s] %.0f%% ≤ -%.0f%% — closing",
                        position.ticker, pnl_pct * 100, eff_stop * 100)
            await self._close_long(position, f"stop loss {pnl_pct*100:.0f}%", "stop_loss")
            return

        # E. LLM thesis re-validation (intelligence layer) — closes early only on a
        # genuine thesis break, after the deterministic floors above had their say.
        await self._maybe_llm_exit(position, is_long=True)

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

    async def _close_position(self, position: OpenPosition, reason: str,
                              source: str = "lifecycle") -> None:
        if self._on_close:
            try:
                _ok = await self._on_close(position, reason)
            except Exception as exc:
                logger.error("Close order failed for %s: %s", position.ticker, exc)
                _ok = False
            # C3: only record the close if it actually flattened. A failed close (the callback
            # returns False, or raised) leaves the position OPEN in IBKR — marking it closed here
            # would strand it unmanaged (an unbounded loss). Keep it OPEN to retry next cycle;
            # the session has already escalated to the CRO.
            if _ok is False:
                logger.error("Close NOT recorded for %s — order failed; position kept OPEN for retry",
                             position.ticker)
                return

            # KEYSTONE FIX: the real-close callback (_execute_close) has ALREADY recorded the
            # close_price + realized_pnl from the ACTUAL broker fill (mark_position_closed). The old
            # code then OVERWROTE that here with close_price=current_price and
            # realized_pnl=unrealized_pnl — the model MARK — which booked losing credit spreads as
            # full-credit max wins (close_price=0.00, +full credit) off a fabricated mark. NEVER
            # overwrite the real numbers. Only re-stamp the structured close_source label, then link
            # downstream off the REAL realized P&L.
            try:
                _row = self._db.execute(
                    "SELECT realized_pnl, close_price FROM positions WHERE position_id=?",
                    (position.position_id,),
                ).fetchone()
            except Exception:
                _row = None
            _real_pnl = float(_row[0]) if _row and _row[0] is not None else round(position.unrealized_pnl, 2)
            _real_cp  = float(_row[1]) if _row and _row[1] is not None else round(position.current_price, 4)
            _now_ts = datetime.now(tz=UTC).isoformat()
            self._db.execute(
                # keep the precise broker exit-fill time if mark_position_closed already set it,
                # else stamp now() — exit_ts_utc is never left null on a real close.
                "UPDATE positions SET close_source=?, last_reviewed=?, "
                "exit_ts_utc=COALESCE(exit_ts_utc, ?) WHERE position_id=?",
                (source, _now_ts, _now_ts, position.position_id),
            )
            self._db.commit()
            logger.info("CLOSED (real fill): %s | reason: %s | realized=$%.0f close=%.2f",
                        position.ticker, reason, _real_pnl, _real_cp)
            _decision_chain_close(str(self._settings.db_path), position.position_id, round(_real_pnl, 2))
            self._write_trade_record(position, reason, realized_pnl=_real_pnl, close_price=_real_cp)
            # observe-only post-close counterfactual hook — fire-if-present, never break a close
            getattr(self, "_watch_after_close", lambda *a: None)(position, source)
            return

        # No real-close callback wired (pure paper SIMULATION) — book from the in-memory mark.
        # This is the only path where the unrealized mark is the realized result, by design.
        _sim_now = datetime.now(tz=UTC).isoformat()
        self._db.execute(
            "UPDATE positions SET status=?, close_date=?, close_price=?, close_source=?, "
            "realized_pnl=?, last_reviewed=?, exit_ts_utc=? WHERE position_id=?",
            (
                PositionStatus.CLOSED.value,
                date.today().isoformat(),
                round(position.current_price, 4),
                source,
                round(position.unrealized_pnl, 2),
                _sim_now,
                _sim_now,   # simulation close: record moment is the exit time (seconds precision)
                position.position_id,
            ),
        )
        self._db.commit()
        logger.info("CLOSED (sim mark): %s | reason: %s | PnL: $%.0f",
                    position.ticker, reason, position.unrealized_pnl)

        # Link realized P&L back to the decision chain that opened this position
        _decision_chain_close(
            str(self._settings.db_path),
            position.position_id,
            round(position.unrealized_pnl, 2),
        )

        # Write to trade_records for attribution
        self._write_trade_record(position, reason)
        getattr(self, "_watch_after_close", lambda *a: None)(position, source)

    def _watch_after_close(self, position: OpenPosition, source: str) -> None:
        """S0.2: register the just-closed position for post-close counterfactual scoring (did the
        move continue in our favor → we exited early, or against → exit was correct). Observe-only,
        best-effort, never disrupts the close."""
        try:
            from agora.ops.post_close_watch import record_close
            record_close(str(self._settings.db_path), position, source)
        except Exception as exc:
            logger.debug("post_close_watch.record_close wiring [%s]: %s", position.ticker, exc)

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
            for pos, spot_or_exc in zip(near_expiry, spots, strict=False):
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
                # Persist per-leg greeks too: get_open_positions reads delta/gamma/theta/vega back
                # (with 0.0 defaults), and get_portfolio_greeks aggregates them for the risk
                # council's delta/vega/theta limits. Omitting them here reloaded every position with
                # ZERO greeks, silently disabling those limits after any DB round-trip.
                "delta": l.delta,
                "gamma": l.gamma,
                "theta": l.theta,
                "vega": l.vega,
            }
            for l in position.legs
        ])
        earnings_date = getattr(position, "earnings_date", None)
        # Explicit column list (robust to schema additions — a positional VALUES(...) breaks the
        # moment a migration appends a column, as m008 did for the fill timestamps).
        self._db.execute("""
            INSERT OR REPLACE INTO positions (
                position_id, ticker, strategy, pillar, status, legs_json, contracts, entry_price,
                current_price, entry_date, expiry_date, target_close_date, max_loss_dollars,
                max_gain_dollars, unrealized_pnl, realized_pnl, rolled_count, last_reviewed,
                ibkr_order_ids, notes, direction, conviction_at_entry, regime_at_entry,
                earnings_date, is_pre_earnings, close_date, close_price, close_source,
                entry_ts_utc, exit_ts_utc
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
            datetime.now(tz=UTC).isoformat(),
            json.dumps(position.ibkr_order_ids),
            position.notes,
            getattr(position, "direction", "neutral"),
            getattr(position, "conviction_at_entry", 0.0),
            getattr(position, "regime_at_entry", ""),
            earnings_date.isoformat() if earnings_date else None,
            1 if getattr(position, "is_pre_earnings", False) else 0,
            # close columns — NULL on open
            None, None, "",
            # precise TWS fill timestamps — prefer the broker fill time; fall back to the record
            # moment (full UTC, seconds) so entry_ts_utc is NEVER null going forward. exit NULL on open.
            getattr(position, "entry_ts_utc", "") or datetime.now(tz=UTC).isoformat(), None,
        ))
        self._db.commit()
        # Register with profit engine so entry-time Greeks are captured
        self._profit_engine.register_position(position)

    def add_journal_entry(
        self,
        position: OpenPosition,
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
        # Also advance the running MFE/MAE (peak/trough unrealized P&L in $) so the feature store
        # gets the TRUE excursion, not a daily-snapshot reconstruction. MAX/MIN over the existing
        # value (COALESCE → first mark seeds both to the current mark). Dollars avoid the sign/
        # division pitfalls that corrupted the old pct snapshots.
        self._db.execute(
            "UPDATE positions SET current_price=?, unrealized_pnl=?, last_reviewed=?, "
            "peak_unrealized_pnl = MAX(COALESCE(peak_unrealized_pnl, ?), ?), "
            "trough_unrealized_pnl = MIN(COALESCE(trough_unrealized_pnl, ?), ?) "
            "WHERE position_id=?",
            (current_price, unrealized_pnl, datetime.now(tz=UTC).isoformat(),
             unrealized_pnl, unrealized_pnl, unrealized_pnl, unrealized_pnl, position_id),
        )
        self._db.commit()

    def mark_position_closed(
        self,
        position_id: str,
        realized_pnl: float = 0.0,
        close_price: float = 0.0,
        source: str = "tws_reconcile",
        exit_ts_utc: str | None = None,
    ) -> bool:
        """
        Mark a position closed from an external source (TWS fill detector, startup sync).
        Called when IBKR executes a GTC profit-target or stop-loss that our session
        didn't receive a live callback for. Returns True if a row was actually updated.
        """
        cursor = self._db.execute(
            "UPDATE positions SET status='closed', realized_pnl=?, close_price=?, "
            "close_date=?, close_source=?, last_reviewed=?, "
            # exact TWS exit-fill time when supplied; else keep any prior value; else stamp now() —
            # never leave exit_ts_utc null on a close.
            "exit_ts_utc=COALESCE(?, exit_ts_utc, ?) "
            "WHERE position_id=? AND status IN ('open','tested','rolled')",
            (
                round(realized_pnl, 2),
                round(close_price, 4),
                date.today().isoformat(),
                source,
                datetime.now(tz=UTC).isoformat(),
                (exit_ts_utc or None),
                datetime.now(tz=UTC).isoformat(),
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

    def get_open_position_by_ticker(self, ticker: str) -> OpenPosition | None:
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

    def _write_trade_record(self, position: OpenPosition, notes: str,
                            realized_pnl: float | None = None,
                            close_price: float | None = None) -> None:
        # Record the REAL fill-based close P&L/price when provided (a genuine broker close), NOT
        # position.unrealized_pnl / current_price — those are model MARKS and booking them as the
        # realized result was the defect that recorded losing spreads as max-credit wins.
        _pnl = realized_pnl if realized_pnl is not None else position.unrealized_pnl
        _cp  = close_price  if close_price  is not None else position.current_price
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
            _cp,
            position.contracts,
            _pnl,
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
        from agora.ops.edge_dashboard import _REAL_CLOSE
        rows = self._db.execute(f"""
            SELECT strategy, regime_at_entry, conviction_at_entry,
                   realized_pnl, close_date
            FROM positions
            WHERE {_REAL_CLOSE} AND close_date >= ?
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

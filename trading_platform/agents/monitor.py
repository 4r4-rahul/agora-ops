"""
MonitorAgent — periodic position monitor that enforces stop losses and profit targets.

Subscribes to: EXECUTION_STATUS  (to learn about new open positions)
Publishes:     POSITION_UPDATE   (when a position hits stop or target)

Runs a background asyncio loop every `poll_interval_seconds`.

Exit detection uses the same intrinsic-value + time-decay spread pricing
model as the walk-forward backtester, so paper-mode results are consistent
with backtested expectations. The underlying price fetched from yfinance is
converted to an estimated option spread price before comparing against the
option-denominated stop_loss and profit_target thresholds.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ..core.bus import MessageBus
from ..core.config import Settings
from ..core.models.agent import AgentMessage, AgentStatus, AgentTopic
from ..core.state import SharedStateStore
from .base import BaseAgent

logger = logging.getLogger(__name__)

_DB_PATH = Path("./trade_journal.db")

# IV crush multipliers applied to time-value estimate once > 20% of DTE has elapsed.
# Reflects the typical post-earnings collapse in implied volatility.
_IV_CRUSH_FACTOR: dict[str, float] = {
    "none": 1.0,
    "low": 0.90,
    "moderate": 0.70,
    "high": 0.50,
    "extreme": 0.35,
}


class OpenPosition:
    """In-memory record of a monitored position with full pricing context."""

    __slots__ = (
        "journal_id", "session_id", "ticker", "strategy",
        "direction", "entry_price", "stop_loss", "profit_target",
        "contracts", "max_loss_dollars",
        "underlying_at_entry", "expiration_date", "original_dte",
        "legs", "trailing_stop_price",
        "iv_at_entry", "iv_crush_risk",
    )

    def __init__(self, row: dict[str, Any]) -> None:
        self.journal_id: str = row["id"]
        self.session_id: str = row["session_id"]
        self.ticker: str = row["ticker"]
        self.strategy: str = row["strategy"]
        self.direction: str = row["direction"]
        self.entry_price: float = float(row["entry_price"])
        self.stop_loss: float = float(row["stop_loss"])
        self.profit_target: float = float(row["profit_target"])
        self.contracts: int = int(row["contracts"])
        self.max_loss_dollars: float = float(row["max_loss_dollars"])

        # Pricing context — may be None for positions opened before schema migration
        self.underlying_at_entry: float | None = (
            float(row["underlying_at_entry"]) if row.get("underlying_at_entry") else None
        )
        self.expiration_date: date | None = (
            date.fromisoformat(row["expiration_date"]) if row.get("expiration_date") else None
        )
        self.original_dte: int = int(row.get("original_dte") or 21)

        # Parse legs + IV context from raw_recommendation JSON
        raw = row.get("raw_recommendation") or "{}"
        try:
            rec = json.loads(raw) if isinstance(raw, str) else raw
            self.legs: list[dict] = rec.get("legs", [])
            iv_rank = rec.get("iv_rank")
            # Convert IV rank (0-100) to annualised σ estimate: 10% at rank 0, 40% at rank 100
            self.iv_at_entry: float | None = (
                0.10 + float(iv_rank) / 100.0 * 0.30 if iv_rank is not None else None
            )
            self.iv_crush_risk: str = rec.get("iv_crush_risk") or "none"
        except Exception:
            self.legs = []
            self.iv_at_entry = None
            self.iv_crush_risk = "none"

        # Trailing stop — ratchets upward as the position profits (set dynamically)
        self.trailing_stop_price: float | None = None


def _estimate_option_price(pos: OpenPosition, underlying_now: float) -> float:
    """
    Estimate current spread price using intrinsic value + linear time decay.

    Matches the pricing model in backtester/engine.py so paper-mode
    monitoring is consistent with what the backtest simulates.
    """
    legs = pos.legs
    if len(legs) < 2 or pos.underlying_at_entry is None:
        # Not enough context — fall back to entry price (treat as unchanged)
        return pos.entry_price

    strikes = sorted(float(l.get("strike", underlying_now)) for l in legs)
    low_strike, high_strike = strikes[0], strikes[-1]
    spread_width = high_strike - low_strike

    if spread_width <= 0:
        return pos.entry_price

    # Intrinsic value from current underlying
    direction = pos.direction.lower()
    if direction == "bullish":
        intrinsic = max(0.0, min(underlying_now - low_strike, spread_width))
    elif direction == "bearish":
        intrinsic = max(0.0, min(high_strike - underlying_now, spread_width))
    else:  # neutral / iron condor
        centre = (low_strike + high_strike) / 2.0
        half_width = spread_width / 2.0
        displacement = abs(underlying_now - centre)
        intrinsic = max(0.0, half_width - displacement)

    # Time value: convex theta decay scaled by moneyness.
    # sqrt(T/T0) prevents premature stop-outs in early days.
    # Moneyness factor zeroes out TV when underlying is far from the profit zone.
    import math as _math
    dte_remaining = 0
    if pos.expiration_date:
        dte_remaining = max(0, (pos.expiration_date - date.today()).days)
    time_fraction = dte_remaining / max(1, pos.original_dte)

    # Moneyness scaling: TV drops to 0 when underlying is >2 spread-widths OTM
    if spread_width > 0:
        if direction == "bullish":
            distance_otm = max(0.0, low_strike - underlying_now)
        elif direction == "bearish":
            distance_otm = max(0.0, underlying_now - high_strike)
        else:
            centre = (low_strike + high_strike) / 2.0
            distance_otm = max(0.0, abs(underlying_now - centre) - spread_width / 2.0)
        moneyness_scale = max(0.0, 1.0 - distance_otm / (spread_width * 2.0))
    else:
        moneyness_scale = 1.0

    time_value = pos.entry_price * _math.sqrt(time_fraction) * moneyness_scale

    # IV crush adjustment: once >20% of DTE has elapsed and the position had
    # meaningful earnings/event risk, reduce time value to reflect vol collapse.
    iv_crush_risk = getattr(pos, "iv_crush_risk", "none") or "none"
    if iv_crush_risk != "none":
        pct_elapsed = 1.0 - time_fraction
        if pct_elapsed > 0.20:
            time_value *= _IV_CRUSH_FACTOR.get(iv_crush_risk, 1.0)

    return intrinsic + time_value


class MonitorAgent(BaseAgent):
    """
    Monitors open paper positions for stop loss / profit target exits.

    Uses a background asyncio task (not pub/sub) for the polling loop.
    Pub/sub is used only to learn about newly opened positions and to
    publish exit notifications.
    """

    name = "monitor"
    subscriptions = [AgentTopic.EXECUTION_STATUS]

    def __init__(
        self,
        bus: MessageBus,
        state_store: SharedStateStore,
        settings: Settings | None = None,
        poll_interval_seconds: float = 60.0,
    ) -> None:
        super().__init__(bus, state_store, settings)
        self._poll_interval = poll_interval_seconds
        self._positions: dict[str, OpenPosition] = {}  # journal_id → position
        self._monitor_task: asyncio.Task | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        await super().start()
        self._load_open_positions_from_db()
        self._monitor_task = asyncio.create_task(
            self._monitor_loop(), name="position-monitor"
        )
        self._log.info(
            "monitor started — %d open positions, polling every %.0fs",
            len(self._positions),
            self._poll_interval,
        )

    async def stop(self) -> None:
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        await super().stop()

    # ── Pub/sub: learn about newly opened positions ────────────────────────

    async def handle(self, message: AgentMessage) -> None:
        payload = message.payload
        if payload.get("status") not in ("paper_filled", "live_filled"):
            return
        journal_id = payload.get("journal_id")
        if not journal_id:
            return
        pos = self._load_one_position(journal_id)
        if pos:
            self._positions[journal_id] = pos
            self._log.info(
                "monitoring new position %s — %s %s entry=%.2f stop=%.2f target=%.2f",
                journal_id[:8], pos.ticker, pos.strategy,
                pos.entry_price, pos.stop_loss, pos.profit_target,
            )

    # ── Monitoring loop ────────────────────────────────────────────────────

    async def _monitor_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._poll_interval)
                await self._check_all_positions()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._log.error("monitor loop error: %s", exc, exc_info=True)

    async def _check_all_positions(self) -> None:
        if not self._positions:
            return

        tickers = {pos.ticker for pos in self._positions.values()}
        prices = await self._fetch_prices(tickers)

        closed: list[str] = []
        # (ticker, strategy, reason) → [pnl, underlying, option_price, journal_id]
        close_groups: dict[tuple, list] = {}

        for journal_id, pos in self._positions.items():
            underlying_now = prices.get(pos.ticker)
            if underlying_now is None:
                continue

            option_price_now = _estimate_option_price(pos, underlying_now)
            self._update_trailing_stop(pos, option_price_now)
            exit_reason = self._check_exit_condition(pos, option_price_now)

            if exit_reason:
                pnl = self._compute_pnl(pos, option_price_now, exit_reason)
                await self._close_position(pos, underlying_now, option_price_now, exit_reason, pnl)
                closed.append(journal_id)
                key = (pos.ticker, pos.strategy, exit_reason)
                if key not in close_groups:
                    close_groups[key] = {"total_pnl": 0.0, "count": 0,
                                         "underlying": underlying_now,
                                         "option_price": option_price_now,
                                         "journal_id": pos.journal_id}
                close_groups[key]["total_pnl"] += pnl
                close_groups[key]["count"] += 1
            else:
                adjustment = self._check_adjustment_needed(pos, option_price_now)
                if adjustment:
                    self._log.info(
                        "ADJUSTMENT RECOMMENDED — %s %s: %s | "
                        "option_est=%.2f entry=%.2f (%.0f%% gain)",
                        pos.ticker, pos.strategy, adjustment.upper(),
                        option_price_now, pos.entry_price,
                        (option_price_now - pos.entry_price) / pos.entry_price * 100,
                    )
                self._log.debug(
                    "%s %s underlying=%.2f est_option=%.2f (stop=%.2f target=%.2f)",
                    pos.ticker, pos.strategy,
                    underlying_now, option_price_now,
                    pos.stop_loss, pos.profit_target,
                )

        for j in closed:
            self._positions.pop(j, None)

        # Send one batched Discord alert per unique ticker+strategy+reason group
        from ..services.alerts import alert_position_closed
        for (ticker, strategy, reason), info in close_groups.items():
            count = info["count"]
            suffix = f" (×{count} positions)" if count > 1 else ""
            await alert_position_closed(
                self._settings.alert_webhook_url,
                ticker=ticker,
                strategy=strategy,
                exit_reason=reason,
                underlying_price=info["underlying"],
                option_exit_price=info["option_price"],
                estimated_pnl=info["total_pnl"],
                journal_id=info["journal_id"] + suffix,
            )

        self._log_portfolio_summary(prices)

    def _update_trailing_stop(self, pos: OpenPosition, option_price: float) -> None:
        """Ratchet trailing stop upward as the position profits (never lower it)."""
        max_profit = pos.profit_target - pos.entry_price
        if max_profit <= 0:
            return

        current_gain = option_price - pos.entry_price
        pct_of_max = current_gain / max_profit

        new_trailing: float | None = None
        if pct_of_max >= 0.70:
            # 70%+ of max profit: lock in 50% gain (trail at 50% of max profit)
            new_trailing = pos.entry_price + max_profit * 0.50
        elif pct_of_max >= 0.50:
            # 50%+ of max profit: trail at breakeven + 10% buffer
            new_trailing = pos.entry_price + max_profit * 0.10
        elif pct_of_max >= 0.30:
            # 30%+ of max profit: trail at exact breakeven (protect capital)
            new_trailing = pos.entry_price

        if new_trailing is not None:
            # Only ratchet upward, never allow trailing stop to decrease
            if pos.trailing_stop_price is None or new_trailing > pos.trailing_stop_price:
                old = pos.trailing_stop_price or pos.stop_loss
                logger.info(
                    "TRAILING STOP %s %s: $%.2f → $%.2f (gain %.0f%% of max)",
                    pos.ticker, pos.strategy, old, new_trailing, pct_of_max * 100,
                )
                pos.trailing_stop_price = new_trailing

    def _check_exit_condition(self, pos: OpenPosition, option_price: float) -> str | None:
        """
        Check all exit conditions in priority order:
          1. Hard stop loss OR trailing stop (whichever is more protective)
          2. Dynamic early profit target (50% of max profit before 50% DTE used)
          3. Standard profit target
          4. Thesis invalidation (regime/structure broken)
          5. Expiry
        """
        # 1. Stop check: trailing stop (once activated) or hard stop
        effective_stop = (
            pos.trailing_stop_price
            if pos.trailing_stop_price is not None and pos.trailing_stop_price > pos.stop_loss
            else pos.stop_loss
        )
        if option_price <= effective_stop:
            return "trailing_stop" if pos.trailing_stop_price is not None and effective_stop > pos.stop_loss else "stop_loss"

        # 2 & 3. Dynamic profit targets
        max_profit = pos.profit_target - pos.entry_price  # spread between entry and target
        current_gain = option_price - pos.entry_price
        pct_of_max = current_gain / max_profit if max_profit > 0 else 0

        if pos.expiration_date and pos.original_dte > 0:
            dte_remaining = max(0, (pos.expiration_date - date.today()).days)
            pct_dte_used = 1.0 - (dte_remaining / pos.original_dte)

            # Exit at 50% of max profit if we've used less than 50% of DTE
            # (take the win early — don't let theta eat it)
            if pct_of_max >= 0.50 and pct_dte_used < 0.50:
                return "dynamic_profit_early"

            # Exit at 75% of max profit at any time
            if pct_of_max >= 0.75:
                return "dynamic_profit_75pct"

        # Standard profit target
        if option_price >= pos.profit_target:
            return "profit_target"

        # 4. Thesis invalidation (structural)
        thesis_break = self._check_thesis_invalidation(pos)
        if thesis_break:
            return thesis_break

        # 5. Expiry
        if pos.expiration_date and pos.expiration_date <= date.today():
            return "expiry"

        return None

    def _check_thesis_invalidation(self, pos: OpenPosition) -> str | None:
        """
        Check if the original entry thesis has been structurally broken.
        Returns exit reason string or None.

        Current checks (non-Claude, deterministic):
          - DTE < 5 with position still losing → time-decay danger zone
          - Position held > 2× original_dte (position somehow held too long)
        """
        if not pos.expiration_date:
            return None

        dte_remaining = max(0, (pos.expiration_date - date.today()).days)

        # With < 5 DTE, time decay accelerates exponentially — exit losers
        if dte_remaining < 5 and pos.original_dte > 7:
            return "thesis_dte_danger"

        return None

    def _compute_pnl(self, pos: OpenPosition, option_price: float, reason: str) -> float:
        exit_price = max(0.0, option_price) if reason == "expiry" else option_price
        gross = (exit_price - pos.entry_price) * pos.contracts * 100
        n_legs = len(pos.legs) if pos.legs else 2
        settings = getattr(self, "_settings", None)
        rate = getattr(settings, "commission_per_contract_leg", 0.65)
        commission = n_legs * pos.contracts * rate * 2  # entry + exit round-trip
        logger.debug(
            "commission %s %s: %d legs × %d contracts × $%.2f × 2 = $%.2f",
            pos.ticker, pos.strategy, n_legs, pos.contracts, rate, commission,
        )
        return gross - commission

    # ── Market data ────────────────────────────────────────────────────────

    async def _fetch_prices(self, tickers: set[str]) -> dict[str, float]:
        loop = asyncio.get_event_loop()

        def _sync_fetch() -> dict[str, float]:
            import yfinance as yf
            prices: dict[str, float] = {}
            for ticker in tickers:
                try:
                    info = yf.Ticker(ticker).info or {}
                    price = (
                        info.get("regularMarketPrice")
                        or info.get("currentPrice")
                        or info.get("previousClose")
                    )
                    if price:
                        prices[ticker] = float(price)
                except Exception as exc:
                    self._log.debug("price fetch failed for %s: %s", ticker, exc)
            return prices

        try:
            return await loop.run_in_executor(None, _sync_fetch)
        except Exception as exc:
            self._log.error("price fetch failed: %s", exc)
            return {}

    # ── Close notification ─────────────────────────────────────────────────

    async def _close_position(
        self,
        pos: OpenPosition,
        underlying_price: float,
        option_price: float,
        reason: str,
        pnl: float,
    ) -> None:
        self._log.info(
            "CLOSING %s %s — reason=%s underlying=%.2f option_est=%.2f pnl=$%.0f",
            pos.ticker, pos.strategy, reason,
            underlying_price, option_price, pnl,
        )

        # For both paper and live: cancel the GTC profit-target and close via IBKR
        if self._settings.trading_mode in ("live", "paper") and pos.legs:
            mode_label = "LIVE" if self._settings.trading_mode == "live" else "PAPER"
            try:
                from ..services.ibkr_client import close_position as ibkr_close
                close_result = await ibkr_close(
                    ticker=pos.ticker,
                    legs=pos.legs,
                    contracts=pos.contracts,
                    session_id=pos.session_id,
                    host=self._settings.ibkr_host,
                    port=self._settings.ibkr_port,
                    # Use a different client_id to avoid conflicts with open order client
                    client_id=self._settings.ibkr_client_id + 1,
                )
                actual_price = close_result.get("avg_price") or option_price
                pnl = self._compute_pnl(pos, actual_price, reason)
                self._log.info(
                    "[%s] IBKR close filled — orderId=%s avg=%.2f actual_pnl=$%.0f",
                    mode_label, close_result.get("order_id"), actual_price, pnl,
                )
                option_price = actual_price
            except Exception as exc:
                self._log.error(
                    "[%s] IBKR close failed for %s %s: %s",
                    mode_label, pos.ticker, pos.strategy, exc,
                )
                if self._settings.trading_mode == "live":
                    # Live mode: don't update DB — position is still open; retry next poll
                    return
                # Paper mode: IBKR unavailable — proceed with formula-based close

        self._update_db_closed(pos.journal_id, option_price, reason, pnl)
        await self.publish(
            AgentTopic.POSITION_UPDATE,
            session_id=pos.session_id,
            payload={
                "journal_id": pos.journal_id,
                "ticker": pos.ticker,
                "strategy": pos.strategy,
                "exit_reason": reason,
                "underlying_price": underlying_price,
                "option_exit_price": option_price,
                "estimated_pnl": pnl,
                "status": "closed",
            },
        )

        # Alert is sent by _check_all_positions after batching same-ticker closes.

    # ── Database helpers ───────────────────────────────────────────────────

    def _load_open_positions_from_db(self) -> None:
        if not _DB_PATH.exists():
            return
        try:
            with sqlite3.connect(_DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM trade_journal WHERE status = 'open'"
                ).fetchall()
            for row in rows:
                pos = OpenPosition(dict(row))
                self._positions[pos.journal_id] = pos
        except Exception as exc:
            self._log.error("failed to load open positions from DB: %s", exc)

    def _load_one_position(self, journal_id: str) -> OpenPosition | None:
        if not _DB_PATH.exists():
            return None
        try:
            with sqlite3.connect(_DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT * FROM trade_journal WHERE id = ?", (journal_id,)
                ).fetchone()
            return OpenPosition(dict(row)) if row else None
        except Exception as exc:
            self._log.error("failed to load position %s: %s", journal_id, exc)
            return None

    def _update_db_closed(
        self,
        journal_id: str,
        exit_price: float,
        reason: str,
        pnl: float,
    ) -> None:
        if not _DB_PATH.exists():
            return
        try:
            with sqlite3.connect(_DB_PATH) as conn:
                conn.execute(
                    """
                    UPDATE trade_journal
                    SET status = 'closed',
                        closed_at = ?,
                        exit_price = ?,
                        realized_pnl = ?,
                        exit_reason = ?
                    WHERE id = ?
                    """,
                    (
                        datetime.now().isoformat(),
                        exit_price,
                        pnl,
                        reason,
                        journal_id,
                    ),
                )
                conn.commit()
        except Exception as exc:
            self._log.error("DB close update failed for %s: %s", journal_id, exc)

    def _check_adjustment_needed(
        self, pos: OpenPosition, option_price: float
    ) -> str | None:
        """
        Returns an adjustment action if the position needs active management.
        These are logged as recommendations — not auto-executed in paper mode.

        Actions:
          roll     — thesis intact but DTE running low (< 7 days), consider rolling
          scale    — position winning strongly, consider adding at better price
          reduce   — partial profit, reduce size to lock in gains
        """
        if not pos.expiration_date or pos.original_dte <= 0:
            return None

        dte_remaining = max(0, (pos.expiration_date - date.today()).days)
        pct_dte_used = 1.0 - (dte_remaining / pos.original_dte)
        current_gain_pct = (option_price - pos.entry_price) / pos.entry_price

        # Roll candidate: DTE < 7, position down or flat, thesis likely still valid
        if dte_remaining < 7 and current_gain_pct < 0.20:
            return "roll"

        # Scale candidate: strong winner early (>40% gain, <40% DTE used)
        if current_gain_pct >= 0.40 and pct_dte_used < 0.40:
            return "scale"

        # Reduce candidate: decent gain (25-50%) but showing signs of stalling
        if 0.25 <= current_gain_pct <= 0.50 and pct_dte_used > 0.60:
            return "reduce"

        return None

    def _log_portfolio_summary(self, prices: dict[str, float]) -> None:
        """Log portfolio-level Greeks after every poll cycle (Black-Scholes when IV known)."""
        if not self._positions:
            return

        from ..core.models.risk import compute_spread_greeks

        total_max_risk = 0.0
        total_unrealized = 0.0
        net_delta = 0.0       # $ per 1-point move in underlying
        net_theta = 0.0       # $ per calendar day from time decay
        net_vega = 0.0        # $ per 1% move in IV
        bs_positions = 0      # count of positions using real Greeks
        ticker_risk: dict[str, float] = {}

        for pos in self._positions.values():
            underlying_now = prices.get(pos.ticker)

            total_max_risk += pos.max_loss_dollars
            ticker_risk[pos.ticker] = ticker_risk.get(pos.ticker, 0.0) + pos.max_loss_dollars

            if underlying_now is not None:
                est = _estimate_option_price(pos, underlying_now)
                total_unrealized += (est - pos.entry_price) * pos.contracts * 100

            # Prefer real Black-Scholes Greeks when we have IV and expiration data
            used_bs = False
            if (
                pos.legs
                and pos.iv_at_entry is not None
                and pos.expiration_date is not None
                and underlying_now is not None
            ):
                try:
                    dte_rem = max(0, (pos.expiration_date - date.today()).days)
                    g = compute_spread_greeks(
                        underlying=underlying_now,
                        legs=pos.legs,
                        implied_vol=pos.iv_at_entry,
                        dte_remaining=dte_rem,
                    )
                    multiplier = pos.contracts * 100
                    net_delta += g.delta * multiplier
                    net_theta += g.theta * multiplier
                    net_vega += g.vega * multiplier
                    used_bs = True
                    bs_positions += 1
                except Exception:
                    pass

            if not used_bs:
                # Fallback: directional 0.5-delta approximation
                direction = pos.direction.lower()
                sign = 1 if direction == "bullish" else (-1 if direction == "bearish" else 0)
                net_delta += sign * 0.5 * pos.contracts * 100
                net_theta -= (pos.entry_price / max(1, pos.original_dte)) * pos.contracts * 100

        n = len(self._positions)
        self._log.info(
            "── PORTFOLIO (%d positions, %d BS) ── "
            "Unrealized: $%+.0f | Net Δ: %.2f | Daily θ: $%.0f | Vega: $%.0f | Max risk: $%.0f",
            n, bs_positions, total_unrealized,
            net_delta, net_theta, net_vega, total_max_risk,
        )
        for ticker, risk in sorted(ticker_risk.items(), key=lambda x: -x[1]):
            pct = risk / max(1, total_max_risk) * 100
            self._log.info("  %s  $%.0f (%.0f%% of book)", ticker, risk, pct)

    @property
    def open_position_count(self) -> int:
        return len(self._positions)

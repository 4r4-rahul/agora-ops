"""
Walk-forward backtester for the trading_platform agent pipeline.

Design:
  • Fetches full history once, then slices it day-by-day — no lookahead.
  • Each trading day: builds MarketSnapshot from past bars only, runs the
    full 10-agent pipeline with a deterministic mock Claude (fast, free).
  • Simulates fills using a conservative mid-price approximation.
  • Manages open positions: checks stop-loss and profit-target each day.
  • Outputs: BacktestResult with equity curve, trade log, and stats.

Usage:
    engine = BacktestEngine(ticker="SPY", start="2024-01-01", end="2024-12-31")
    result = await engine.run()
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import date, datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

from ..core.bus import MessageBus
from ..core.config import Settings
from ..core.models.market import Bar, MarketSnapshot
from ..core.models.trade import TradeDecision
from ..core.state import SharedStateStore
from ..agents.conviction import ConvictionAgent
from ..agents.market_data import MarketDataAgent
from ..agents.regime import RegimeAgent
from ..agents.technical import TechnicalAnalysisAgent
from ..agents.news import NewsCatalystAgent
from ..agents.options_strategy import OptionsStrategyAgent
from ..agents.risk_manager import RiskManagerAgent
from ..agents.reviewer import ReviewerAgent
from ..agents.orchestrator import OrchestratorAgent
from .mock_claude import make_mock_client
from .models import BacktestResult, BacktestTrade, BacktestTradeStatus

logger = logging.getLogger(__name__)


class BacktestEngine:
    """
    Walk-forward backtester.

    Args:
        ticker:          Ticker to backtest (e.g. "SPY")
        start:           Start date string "YYYY-MM-DD"
        end:             End date string "YYYY-MM-DD"
        starting_balance: Initial account balance
        trade_every_n_days: Only attempt a trade every N trading days (default 5 = weekly)
        max_open_positions: Max concurrent open positions
        settings:        Optional Settings override (uses paper defaults if None)
    """

    def __init__(
        self,
        ticker: str,
        start: str,
        end: str,
        starting_balance: float = 10_000.0,
        trade_every_n_days: int = 5,
        max_open_positions: int = 3,
        settings: Settings | None = None,
    ) -> None:
        self.ticker = ticker.upper()
        self.start_date = date.fromisoformat(start)
        self.end_date = date.fromisoformat(end)
        self.starting_balance = starting_balance
        self.trade_every_n_days = trade_every_n_days
        self.max_open_positions = max_open_positions
        self._settings = settings or Settings(
            anthropic_api_key="backtest-mock",
            trading_mode="paper",
            require_human_approval=False,
            account_size=starting_balance,
            min_reward_risk_ratio=1.5,
            log_level="WARNING",
        )

    async def run(self) -> BacktestResult:
        """Execute the walk-forward backtest. Returns BacktestResult."""
        logger.info("Fetching %s history for backtest %s → %s", self.ticker, self.start_date, self.end_date)
        all_bars = await self._fetch_history()
        if not all_bars:
            raise RuntimeError(f"No historical data for {self.ticker}")

        trading_days = [b.ts.date() for b in all_bars if self.start_date <= b.ts.date() <= self.end_date]
        trading_days = sorted(set(trading_days))

        result = BacktestResult(
            ticker=self.ticker,
            start_date=self.start_date,
            end_date=self.end_date,
            starting_balance=self.starting_balance,
        )

        balance = self.starting_balance
        open_positions: list[BacktestTrade] = []
        days_since_last_trade = self.trade_every_n_days  # allow trade on day 1

        logger.info("Backtesting %d trading days for %s", len(trading_days), self.ticker)

        for idx, today in enumerate(trading_days):
            # ── Build snapshot using only bars up to (not including) today ───
            past_bars = [b for b in all_bars if b.ts.date() < today]
            if len(past_bars) < 20:
                result.equity_curve.append(balance)
                result.daily_dates.append(today)
                continue

            today_bar = next((b for b in all_bars if b.ts.date() == today), None)
            if today_bar is None:
                result.equity_curve.append(balance)
                result.daily_dates.append(today)
                continue

            snapshot = self._build_snapshot(past_bars, today_bar)

            # ── Check open positions for exit ─────────────────────────────
            # Accounting: open deducted position_size_dollars from balance.
            # On close, return that investment then apply net P&L:
            #   balance += position_size_dollars + net_pnl
            # This equals adding the exit proceeds back to cash.
            for pos in list(open_positions):
                net_pnl = self._check_exit(pos, snapshot, today)
                if net_pnl is not None:
                    pos.pnl_dollars = net_pnl
                    pos.date_closed = today
                    pos.exit_price = self._compute_exit_price(pos, net_pnl)
                    balance += pos.position_size_dollars + net_pnl
                    open_positions.remove(pos)
                    logger.debug(
                        "  CLOSED %s %s PnL=$%+.0f [%s] balance=$%.0f",
                        today, pos.strategy, net_pnl, pos.status.value, balance,
                    )

                # Force-close at expiry — spread expired OTM, proceeds = $0
                elif pos.expiration_date <= today:
                    net_pnl = -pos.position_size_dollars  # full debit is lost
                    pos.pnl_dollars = net_pnl
                    pos.date_closed = today
                    pos.status = BacktestTradeStatus.CLOSED_EXPIRY
                    pos.exit_price = 0.0
                    # balance += pos.position_size_dollars + net_pnl = 0 → no change
                    # (investment was already deducted on open and is now confirmed lost)
                    open_positions.remove(pos)
                    logger.debug(
                        "  EXPIRED %s %s PnL=$%+.0f balance=$%.0f",
                        today, pos.strategy, net_pnl, balance,
                    )

            # ── Record daily equity ───────────────────────────────────────
            result.equity_curve.append(balance)
            result.daily_dates.append(today)

            # ── Attempt new trade (throttled + position limit) ────────────
            days_since_last_trade += 1
            can_trade = (
                days_since_last_trade >= self.trade_every_n_days
                and len(open_positions) < self.max_open_positions
                and balance > self._settings.account_size * 0.5  # stop if down >50%
            )

            if can_trade:
                trade = await self._run_pipeline_day(snapshot, today, balance)
                if trade is not None:
                    open_positions.append(trade)
                    result.trades.append(trade)
                    balance -= trade.position_size_dollars  # reserve capital
                    days_since_last_trade = 0
                    logger.debug(
                        "  OPENED %s %s entry=$%.2f maxloss=$%.0f balance=$%.0f",
                        today, trade.strategy, trade.entry_price,
                        trade.max_loss_dollars, balance,
                    )

        # Force-close any positions still open at end of backtest (full debit lost)
        for pos in open_positions:
            pos.pnl_dollars = -pos.position_size_dollars
            pos.date_closed = self.end_date
            pos.status = BacktestTradeStatus.CLOSED_EXPIRY
            pos.exit_price = 0.0
            # balance already deducted position_size on open; no further change needed
            logger.debug("  FORCE-CLOSED at end: %s PnL=$%+.0f", pos.strategy, pos.pnl_dollars)

        result.trades.extend(open_positions)
        logger.info(
            "Backtest complete: %d trades, PnL=$%+.0f, WR=%.1f%%",
            result.total_trades,
            result.total_pnl,
            result.win_rate * 100,
        )
        return result

    # ── Pipeline integration ──────────────────────────────────────────────

    async def _run_pipeline_day(
        self, snapshot: MarketSnapshot, today: date, balance: float
    ) -> BacktestTrade | None:
        """Run the full agent pipeline for one day. Returns a trade or None."""
        snapshot_ref: dict[str, Any] = {"snap": snapshot, "account_size": balance}
        mock_client = make_mock_client(snapshot_ref)

        bus = MessageBus()
        state_store = SharedStateStore()
        # Temporarily override account size to reflect current balance
        settings = Settings(
            anthropic_api_key="backtest-mock",
            trading_mode="paper",
            require_human_approval=False,
            account_size=balance,
            min_reward_risk_ratio=self._settings.min_reward_risk_ratio,
            log_level="WARNING",
        )

        with patch(
            "trading_platform.agents.base.anthropic.AsyncAnthropic",
            return_value=mock_client,
        ):
            # Execution and Journal agents excluded: backtester manages fills
            # directly and must not write to the live trade_journal.db
            agents = [
                MarketDataAgent(bus=bus, state_store=state_store, settings=settings),
                RegimeAgent(bus=bus, state_store=state_store, settings=settings),
                TechnicalAnalysisAgent(bus=bus, state_store=state_store, settings=settings),
                NewsCatalystAgent(bus=bus, state_store=state_store, settings=settings),
                ConvictionAgent(bus=bus, state_store=state_store, settings=settings),
                OptionsStrategyAgent(bus=bus, state_store=state_store, settings=settings),
                RiskManagerAgent(bus=bus, state_store=state_store, settings=settings),
                ReviewerAgent(bus=bus, state_store=state_store, settings=settings),
            ]

            # Patch market data fetch to return our pre-built snapshot
            from unittest.mock import AsyncMock
            with patch(
                "trading_platform.agents.market_data.YFinanceProvider.get_snapshot",
                new_callable=AsyncMock,
                return_value=snapshot,
            ), patch(
                "trading_platform.agents.news.NewsCatalystAgent._fetch_headlines",
                new_callable=AsyncMock,
                return_value=[],  # no headlines in backtest
            ):
                for agent in agents:
                    await agent.start()

                orchestrator = OrchestratorAgent(
                    bus=bus, state_store=state_store, settings=settings
                )
                try:
                    result = await orchestrator.analyze(self.ticker, timeout=10.0)
                except Exception as exc:
                    logger.warning("Pipeline failed on %s: %s", today, exc)
                    result = {}
                finally:
                    for agent in agents:
                        await agent.stop()

        return self._result_to_trade(result, today, snapshot)

    def _result_to_trade(
        self,
        result: dict[str, Any],
        today: date,
        snapshot: MarketSnapshot,
    ) -> BacktestTrade | None:
        """Convert pipeline result dict into a BacktestTrade, or None if not actionable."""
        if not result or result.get("error"):
            return None

        decision = result.get("final_decision")
        # PENDING_APPROVAL = reviewer approved, waiting for human sign-off.
        # In backtest we auto-approve; WATCHLIST and REJECTED are skipped.
        actionable = {TradeDecision.APPROVED, TradeDecision.PENDING_APPROVAL, "APPROVED", "PENDING_APPROVAL"}
        if decision not in actionable:
            return None

        entry = result.get("entry_price", 0)
        max_loss = result.get("max_loss_dollars", 0)
        dte = result.get("expiration_dte", 21)

        if entry <= 0 or max_loss <= 0:
            return None

        return BacktestTrade(
            session_id=result.get("session_id", ""),
            date_opened=today,
            ticker=self.ticker,
            strategy=result.get("strategy", "unknown"),
            direction=result.get("direction", "neutral"),
            entry_price=entry,
            contracts=result.get("contracts", 1),
            position_size_dollars=result.get("position_size_dollars", entry * 100),
            max_loss_dollars=max_loss,
            profit_target=result.get("profit_target", entry * 2),
            stop_loss=result.get("stop_loss", entry * 0.5),
            expiration_dte=dte,
            expiration_date=today + timedelta(days=dte),
            thesis=result.get("thesis", "")[:200],
            reward_risk_ratio=result.get("reward_risk_ratio", 0),
            underlying_at_entry=snapshot.price,
            recommendation=result,
        )

    # ── Position management ───────────────────────────────────────────────

    def _check_exit(
        self, pos: BacktestTrade, snapshot: MarketSnapshot, today: date
    ) -> float | None:
        """
        Estimate current option spread value and check stop/target.

        Uses a spread-intrinsic + time-value model keyed off cumulative
        underlying move from the actual entry day (not just today's daily move).
        """
        if not pos.is_open:
            return None

        days_held = (today - pos.date_opened).days
        if days_held < 1:
            return None

        dte_original = pos.expiration_dte
        dte_remaining = max(0, dte_original - days_held)
        time_fraction = dte_remaining / dte_original  # 1.0 at entry → 0.0 at expiry

        price_now = snapshot.price
        S0 = pos.underlying_at_entry if pos.underlying_at_entry > 0 else price_now

        # Strike geometry implied from entry_price and recommendation
        legs = pos.recommendation.get("legs", [])
        if len(legs) >= 2:
            strikes = sorted(float(leg.get("strike", S0)) for leg in legs)
            low_strike, high_strike = strikes[0], strikes[-1]
            spread_width = high_strike - low_strike
        else:
            # Fallback: estimate spread width from max_loss + entry_price
            spread_width = pos.max_loss_dollars / 100 + pos.entry_price

        # Intrinsic value of the spread at current underlying price
        if pos.direction == "bullish":
            # Bull call spread: value = max(0, min(S-low_strike, spread_width))
            intrinsic = max(0.0, min(price_now - low_strike, spread_width))
        elif pos.direction == "bearish":
            # Bear put spread: value = max(0, min(high_strike-S, spread_width))
            intrinsic = max(0.0, min(high_strike - price_now, spread_width))
        else:
            # Iron condor: decreases as underlying moves away from centre
            centre = (low_strike + high_strike) / 2
            condor_width = (high_strike - low_strike) / 2
            displacement = abs(price_now - centre)
            intrinsic = max(0.0, condor_width - displacement)

        # Time value: convex theta decay scaled by moneyness.
        # sqrt(T/T0) avoids premature stop-outs on fresh positions.
        # Moneyness factor zeroes TV when underlying is far from profit zone.
        if spread_width > 0:
            if pos.direction == "bullish":
                distance_otm = max(0.0, low_strike - price_now)
            elif pos.direction == "bearish":
                distance_otm = max(0.0, price_now - high_strike)
            else:
                centre = (low_strike + high_strike) / 2.0
                distance_otm = max(0.0, abs(price_now - centre) - spread_width / 2.0)
            moneyness_scale = max(0.0, 1.0 - distance_otm / (spread_width * 2.0))
        else:
            moneyness_scale = 1.0

        time_value = pos.entry_price * math.sqrt(time_fraction) * moneyness_scale

        current_price = intrinsic + time_value
        current_price = max(0.01, current_price)

        # Round-trip commission: entry + exit, $0.65/contract/leg each side
        n_legs = len(legs) if legs else 2
        commission_rate = getattr(self._settings, "commission_per_contract_leg", 0.65)
        commission = n_legs * pos.contracts * commission_rate * 2
        pos.commission_dollars = commission

        # Profit target check
        if current_price >= pos.profit_target:
            gross = (pos.profit_target - pos.entry_price) * pos.contracts * 100
            pos.status = BacktestTradeStatus.CLOSED_PROFIT_TARGET
            return gross - commission

        # Stop loss check
        if current_price <= pos.stop_loss:
            gross = (pos.stop_loss - pos.entry_price) * pos.contracts * 100
            pos.status = BacktestTradeStatus.CLOSED_STOP_LOSS
            return gross - commission

        return None

    def _compute_exit_price(self, pos: BacktestTrade, pnl: float) -> float:
        return pos.entry_price + pnl / (pos.contracts * 100)

    # ── Market data ───────────────────────────────────────────────────────

    async def _fetch_history(self) -> list[Bar]:
        """Download full OHLCV history from yfinance (run once at startup)."""
        import asyncio
        from functools import partial
        import yfinance as yf

        loop = asyncio.get_event_loop()

        def _download():
            # Fetch 3 months before start_date for indicator warmup
            warmup_start = self.start_date - timedelta(days=90)
            df = yf.download(
                self.ticker,
                start=warmup_start.isoformat(),
                end=(self.end_date + timedelta(days=1)).isoformat(),
                progress=False,
                auto_adjust=True,
            )
            # Flatten multi-level columns (yfinance ≥0.2.31)
            if hasattr(df, "columns") and hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
                df.columns = df.columns.get_level_values(0)
            return df

        df = await loop.run_in_executor(None, _download)
        if df is None or (hasattr(df, "empty") and df.empty):
            return []

        bars = []
        for ts, row in df.iterrows():
            def _get(col: str) -> float:
                try:
                    v = row[col] if col in row.index else row.get(col, 0)
                    return float(v) if v is not None else 0.0
                except Exception:
                    return 0.0

            bars.append(Bar(
                ts=ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else datetime.now(timezone.utc).replace(tzinfo=None),
                open=_get("Open"),
                high=_get("High"),
                low=_get("Low"),
                close=_get("Close"),
                volume=int(_get("Volume")),
            ))
        return bars

    def _build_snapshot(self, past_bars: list[Bar], today_bar: Bar) -> MarketSnapshot:
        """Build a MarketSnapshot using only past data — no lookahead."""
        closes = [b.close for b in past_bars]
        price = today_bar.open  # use today's open as "current price" (no future data)

        def _sma(n: int) -> float | None:
            if len(closes) < n:
                return None
            return sum(closes[-n:]) / n

        def _rsi(n: int = 14) -> float | None:
            if len(closes) < n + 1:
                return None
            changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
            gains = [max(c, 0) for c in changes[-n:]]
            losses = [abs(min(c, 0)) for c in changes[-n:]]
            avg_g = sum(gains) / n
            avg_l = sum(losses) / n
            if avg_l == 0:
                return 100.0
            return 100 - 100 / (1 + avg_g / avg_l)

        def _atr(n: int = 14) -> float | None:
            bars = past_bars
            if len(bars) < n + 1:
                return None
            trs = []
            for i in range(1, len(bars)):
                pc = bars[i - 1].close
                tr = max(bars[i].high - bars[i].low, abs(bars[i].high - pc), abs(bars[i].low - pc))
                trs.append(tr)
            return sum(trs[-n:]) / n

        def _hv(n: int = 30) -> float | None:
            if len(closes) < n + 1:
                return None
            returns = [
                math.log(closes[i] / closes[i - 1])
                for i in range(1, len(closes))
                if closes[i - 1] > 0 and closes[i] > 0
            ]
            if len(returns) < n:
                return None
            w = returns[-n:]
            mean = sum(w) / len(w)
            var = sum((r - mean) ** 2 for r in w) / (len(w) - 1)
            return math.sqrt(var) * math.sqrt(252)

        hv = _hv()
        # IV rank proxy: HV relative to its 1-year range (simplified)
        iv_rank: float | None = None
        if hv and len(closes) >= 252:
            hvs = []
            for i in range(30, min(len(closes), 252)):
                window = closes[i - 30:i]
                if all(c > 0 for c in window):
                    r = [math.log(window[j] / window[j - 1]) for j in range(1, len(window))]
                    mean_r = sum(r) / len(r)
                    var_r = sum((x - mean_r) ** 2 for x in r) / max(1, len(r) - 1)
                    hvs.append(math.sqrt(var_r) * math.sqrt(252))
            if hvs:
                min_hv, max_hv = min(hvs), max(hvs)
                iv_rank = (hv - min_hv) / (max_hv - min_hv) * 100 if max_hv > min_hv else 50.0

        prev_close = past_bars[-1].close if past_bars else None

        return MarketSnapshot(
            ticker=self.ticker,
            timestamp=datetime.combine(today_bar.ts.date(), datetime.min.time()),
            price=price,
            volume=int(today_bar.volume),
            day_open=today_bar.open,
            day_high=today_bar.high,
            day_low=today_bar.low,
            prev_close=prev_close,
            atr_14=_atr(),
            rsi_14=_rsi(),
            sma_20=_sma(20),
            sma_50=_sma(50),
            sma_200=_sma(200),
            vix=None,        # VIX not in single-ticker history; regime uses None-safe _fmt
            iv_rank=iv_rank,
            iv_percentile=iv_rank,  # same proxy
            hist_vol_30=hv,
            bars_daily=past_bars[-30:],
        )

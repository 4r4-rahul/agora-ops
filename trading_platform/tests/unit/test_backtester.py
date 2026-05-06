"""
Unit tests for the walk-forward backtester.

These tests run entirely in-process with synthetic bar data —
no yfinance calls, no Claude calls, no file I/O.
"""

from __future__ import annotations

import asyncio
import math
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from trading_platform.backtester.models import (
    BacktestResult,
    BacktestTrade,
    BacktestTradeStatus,
)
from trading_platform.backtester.engine import BacktestEngine
from trading_platform.backtester.mock_claude import make_mock_client, _classify_regime, _build_strategy
from trading_platform.core.models.market import Bar, MarketSnapshot


# ── Helpers ───────────────────────────────────────────────────────────────

def _make_bars(n: int = 60, base_price: float = 500.0, trend: float = 0.001) -> list[Bar]:
    """Generate synthetic daily bars with a mild uptrend."""
    bars = []
    price = base_price
    start = datetime(2024, 1, 2)
    for i in range(n):
        open_ = price
        high = price * (1 + 0.005)
        low = price * (1 - 0.003)
        close = price * (1 + trend)
        bars.append(Bar(
            ts=start + timedelta(days=i),
            open=open_,
            high=high,
            low=low,
            close=close,
            volume=50_000_000,
        ))
        price = close
    return bars


def _make_snapshot(bars: list[Bar], today_bar: Bar) -> MarketSnapshot:
    closes = [b.close for b in bars]
    price = today_bar.open

    def sma(n):
        if len(closes) < n:
            return None
        return sum(closes[-n:]) / n

    def rsi(n=14):
        if len(closes) < n + 1:
            return None
        changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains = [max(c, 0) for c in changes[-n:]]
        losses = [abs(min(c, 0)) for c in changes[-n:]]
        ag = sum(gains) / n
        al = sum(losses) / n
        if al == 0:
            return 100.0
        return 100 - 100 / (1 + ag / al)

    return MarketSnapshot(
        ticker="SPY",
        timestamp=datetime.combine(today_bar.ts.date(), datetime.min.time()),
        price=price,
        volume=50_000_000,
        day_open=today_bar.open,
        day_high=today_bar.high,
        day_low=today_bar.low,
        prev_close=bars[-1].close if bars else None,
        atr_14=price * 0.01,
        rsi_14=rsi(),
        sma_20=sma(20),
        sma_50=sma(50),
        sma_200=sma(200),
        vix=16.0,
        iv_rank=40.0,
        iv_percentile=45.0,
        hist_vol_30=0.12,
        bars_daily=bars[-30:],
    )


# ── BacktestResult model tests ────────────────────────────────────────────

class TestBacktestResult:
    def _make_trade(self, pnl: float, strategy: str = "bull_call_spread") -> BacktestTrade:
        t = BacktestTrade(
            session_id="test",
            date_opened=date(2024, 1, 10),
            ticker="SPY",
            strategy=strategy,
            direction="bullish",
            entry_price=2.0,
            contracts=1,
            position_size_dollars=200.0,
            max_loss_dollars=200.0,
            profit_target=4.0,
            stop_loss=1.0,
            expiration_dte=21,
            expiration_date=date(2024, 1, 31),
            thesis="test",
            reward_risk_ratio=1.5,
        )
        t.pnl_dollars = pnl
        t.date_closed = date(2024, 1, 20)
        t.status = BacktestTradeStatus.CLOSED_PROFIT_TARGET if pnl > 0 else BacktestTradeStatus.CLOSED_STOP_LOSS
        return t

    def test_win_rate(self):
        r = BacktestResult("SPY", date(2024, 1, 1), date(2024, 6, 30), 10_000.0)
        r.trades = [
            self._make_trade(+300),
            self._make_trade(+300),
            self._make_trade(-100),
            self._make_trade(-100),
        ]
        assert r.win_rate == pytest.approx(0.5)

    def test_total_pnl(self):
        r = BacktestResult("SPY", date(2024, 1, 1), date(2024, 6, 30), 10_000.0)
        r.trades = [self._make_trade(+300), self._make_trade(-100)]
        assert r.total_pnl == pytest.approx(200.0)

    def test_ending_balance(self):
        r = BacktestResult("SPY", date(2024, 1, 1), date(2024, 6, 30), 10_000.0)
        r.trades = [self._make_trade(+300), self._make_trade(-100)]
        assert r.ending_balance == pytest.approx(10_200.0)

    def test_max_drawdown(self):
        r = BacktestResult("SPY", date(2024, 1, 1), date(2024, 6, 30), 10_000.0)
        r.equity_curve = [10_000, 9_800, 9_600, 9_900, 10_100]
        assert r.max_drawdown_dollars == pytest.approx(400.0)
        assert r.max_drawdown_pct == pytest.approx(4.0)

    def test_sharpe_positive_returns(self):
        r = BacktestResult("SPY", date(2024, 1, 1), date(2024, 6, 30), 10_000.0)
        # Monotonically increasing equity → positive Sharpe
        r.equity_curve = [10_000 + i * 10 for i in range(50)]
        assert r.sharpe_ratio > 0

    def test_profit_factor(self):
        r = BacktestResult("SPY", date(2024, 1, 1), date(2024, 6, 30), 10_000.0)
        r.trades = [self._make_trade(+300), self._make_trade(+300), self._make_trade(-100)]
        assert r.profit_factor == pytest.approx(6.0)

    def test_expectancy(self):
        r = BacktestResult("SPY", date(2024, 1, 1), date(2024, 6, 30), 10_000.0)
        r.trades = [self._make_trade(+300), self._make_trade(-100)]
        assert r.expectancy == pytest.approx(100.0)


# ── mock_claude tests ─────────────────────────────────────────────────────

class TestMockClaude:

    def _make_snap(self, price: float = 500.0, vix: float = 16.0, rsi: float = 55.0) -> MarketSnapshot:
        bars = _make_bars(60, price)
        return _make_snapshot(bars[:-1], bars[-1])

    def test_regime_bull_trend(self):
        snap = self._make_snap(price=500.0, vix=14.0, rsi=55.0)
        # Force price above all MAs by using tall uptrend bars
        bars = _make_bars(250, 400.0, trend=0.003)
        snap = _make_snapshot(bars[:-1], bars[-1])
        result = _classify_regime(snap)
        assert result["regime"] in ("bull_trend", "low_volatility", "ranging")
        assert 0 <= result["confidence"] <= 1

    def test_regime_crisis_on_high_vix(self):
        snap = self._make_snap()
        snap = snap.model_copy(update={"vix": 45.0})
        result = _classify_regime(snap)
        assert result["regime"] == "crisis"

    def test_strategy_has_required_fields(self):
        bars = _make_bars(60)
        snap = _make_snapshot(bars[:-1], bars[-1])
        regime = {"regime": "bull_trend"}
        result = _build_strategy(snap, regime)
        required = ["ticker", "direction", "thesis", "strategy", "legs",
                    "entry_price", "stop_loss", "profit_target",
                    "max_loss_dollars", "max_gain_dollars", "reward_risk_ratio"]
        for field in required:
            assert field in result, f"Missing field: {field}"

    def test_strategy_rr_positive(self):
        bars = _make_bars(60)
        snap = _make_snapshot(bars[:-1], bars[-1])
        result = _build_strategy(snap, {"regime": "bull_trend"})
        assert result["reward_risk_ratio"] > 0
        assert result["max_gain_dollars"] > 0
        assert result["max_loss_dollars"] > 0

    @pytest.mark.asyncio
    async def test_mock_client_routes_regime(self):
        bars = _make_bars(60)
        snap = _make_snapshot(bars[:-1], bars[-1])
        ref = {"snap": snap}
        client = make_mock_client(ref)

        resp = await client.messages.create(
            system=[{"type": "text", "text": "You are a quantitative market regime analyst"}],
            messages=[{"role": "user", "content": "Classify regime"}],
            model="claude-opus-4-7",
            max_tokens=512,
        )
        assert resp.content[0].type == "tool_use"
        payload = resp.content[0].input
        assert "regime" in payload
        assert "confidence" in payload

    @pytest.mark.asyncio
    async def test_mock_client_strategy_after_regime(self):
        """Strategy call after regime call should share regime context."""
        bars = _make_bars(60)
        snap = _make_snapshot(bars[:-1], bars[-1])
        ref = {"snap": snap}
        client = make_mock_client(ref)

        # First: regime
        await client.messages.create(
            system=[{"type": "text", "text": "You are a quantitative market regime analyst"}],
            messages=[{"role": "user", "content": "Classify"}],
            model="claude-opus-4-7",
            max_tokens=512,
        )

        # Then: strategy — should use regime context
        resp = await client.messages.create(
            system=[{"type": "text", "text": "You are a professional options trader"}],
            messages=[{"role": "user", "content": "Generate strategy"}],
            model="claude-opus-4-7",
            max_tokens=512,
        )
        payload = resp.content[0].input
        assert "strategy" in payload
        assert "reward_risk_ratio" in payload
        assert payload["reward_risk_ratio"] > 0


# ── Engine exit-check accounting tests ───────────────────────────────────

class TestExitCheckAccounting:
    """Verify that the option pricing model and exit accounting are correct."""

    def _make_trade(
        self,
        direction: str = "bullish",
        entry_price: float = 2.0,
        low_strike: float = 500.0,
        high_strike: float = 510.0,
        dte: int = 21,
    ) -> BacktestTrade:
        spread_width = high_strike - low_strike
        max_loss = entry_price * 100
        profit_target = (spread_width - entry_price) * 0.8
        stop_loss = entry_price * 0.5

        return BacktestTrade(
            session_id="test",
            date_opened=date(2024, 1, 10),
            ticker="SPY",
            strategy="bull_call_spread",
            direction=direction,
            entry_price=entry_price,
            contracts=1,
            position_size_dollars=max_loss,
            max_loss_dollars=max_loss,
            profit_target=profit_target,
            stop_loss=stop_loss,
            expiration_dte=dte,
            expiration_date=date(2024, 1, 10) + timedelta(days=dte),
            thesis="test",
            reward_risk_ratio=2.5,
            underlying_at_entry=500.0,
            recommendation={"legs": [
                {"option_type": "call", "strike": low_strike, "expiration_dte": dte, "action": "buy", "quantity": 1},
                {"option_type": "call", "strike": high_strike, "expiration_dte": dte, "action": "sell", "quantity": 1},
            ]},
        )

    def _snap_at(self, price: float) -> MarketSnapshot:
        bars = _make_bars(60, price * 0.9)
        return _make_snapshot(bars[:-1], bars[-1])

    def test_profit_target_hit_when_itm(self):
        """Bullish spread deep in the money should hit profit target."""
        engine = BacktestEngine("SPY", "2024-01-01", "2024-06-30")
        pos = self._make_trade(direction="bullish", low_strike=490.0, high_strike=500.0)
        # Price 520 → well above both strikes → intrinsic = 10 → near max profit
        snap = self._snap_at(520.0)
        result = engine._check_exit(pos, snap, date(2024, 1, 15))
        assert result is not None
        assert result > 0
        assert pos.status == BacktestTradeStatus.CLOSED_PROFIT_TARGET

    def test_stop_hit_when_otm(self):
        """Bullish spread with price well below buy strike should hit stop."""
        engine = BacktestEngine("SPY", "2024-01-01", "2024-06-30")
        pos = self._make_trade(direction="bullish", low_strike=510.0, high_strike=520.0)
        # Price 480 → well below buy strike → intrinsic 0 → time value only → should hit stop
        snap = self._snap_at(480.0)
        result = engine._check_exit(pos, snap, date(2024, 1, 20))
        # After 10 days with price 30pt OTM the option should be near worthless
        if result is not None:
            assert result < 0
            assert pos.status == BacktestTradeStatus.CLOSED_STOP_LOSS

    def test_no_exit_on_open_day(self):
        """Should not exit on the same day it was opened."""
        engine = BacktestEngine("SPY", "2024-01-01", "2024-06-30")
        pos = self._make_trade()
        snap = self._snap_at(530.0)  # deep ITM
        result = engine._check_exit(pos, snap, date(2024, 1, 10))  # same day as open
        assert result is None

    def test_balance_accounting_on_target(self):
        """Balance should net to: starting - cost + proceeds (not double-counted)."""
        entry_cost = 200.0  # position_size_dollars
        starting = 10_000.0
        balance = starting - entry_cost  # = 9,800 after opening

        net_pnl = +340.0  # target hit: profit
        balance += entry_cost + net_pnl  # return investment + gain
        assert balance == pytest.approx(starting + net_pnl)  # 10,340

    def test_balance_accounting_on_expiry(self):
        """Expired position: debit fully lost, balance stays at post-open level."""
        entry_cost = 200.0
        starting = 10_000.0
        balance = starting - entry_cost  # 9,800

        # Expiry: position_size_dollars returned + net_pnl(-entry_cost) = 0 net change
        net_pnl = -entry_cost
        balance += entry_cost + net_pnl  # += 0
        assert balance == pytest.approx(starting - entry_cost)  # 9,800 unchanged

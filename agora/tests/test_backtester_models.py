"""
agora/tests/test_backtester_models.py — the backtester's result/stat dataclasses (pure, 0 I/O).
Pins the headline metrics every backtest is judged on: win rate, profit factor, avg win/loss,
max drawdown, Sharpe, ending balance, and per-pillar attribution. A wrong aggregation here makes a
losing strategy look profitable (or vice-versa).
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from agora.backtester.models import (
    AgoraBacktestResult,
    AgoraBacktestTrade,
    AgoraTradeStatus,
    PillarStats,
)

_D = date(2026, 1, 5)


def _trade(pnl=None, status=AgoraTradeStatus.OPEN, pillar="vol_premium", commission=0.0,
           short=95.0, long=90.0):
    return AgoraBacktestTrade(
        trade_id="t", date_opened=_D, ticker="SPY", pillar=pillar,
        strategy="bull_put_spread", direction="bullish",
        short_strike=short, long_strike=long, expiration_date=_D + timedelta(days=30),
        dte_at_entry=30, entry_credit_debit=-1.0, contracts=1,
        max_loss_dollars=400.0, max_gain_dollars=100.0, profit_target_price=0.5,
        stop_loss_price=2.0, underlying_at_entry=100.0, iv_at_entry=0.25,
        conviction_score=65.0, size_multiplier=1.0,
        pnl_dollars=pnl, status=status, commission_dollars=commission,
    )


def _result(trades=(), equity=(), starting=10000.0):
    return AgoraBacktestResult(
        start_date=_D, end_date=_D + timedelta(days=90), starting_balance=starting,
        trades=list(trades), equity_curve=list(equity),
    )


# ── AgoraBacktestTrade ────────────────────────────────────────────────────────
class TestTrade:
    def test_is_open(self):
        assert _trade(status=AgoraTradeStatus.OPEN).is_open is True
        assert _trade(status=AgoraTradeStatus.CLOSED_PROFIT_TARGET).is_open is False

    def test_spread_width(self):
        assert _trade(short=95, long=90).spread_width == 5.0
        assert _trade(short=90, long=95).spread_width == 5.0   # abs


# ── PillarStats ───────────────────────────────────────────────────────────────
class TestPillarStats:
    def test_win_rate(self):
        assert PillarStats("x", trades=4, wins=3).win_rate == 0.75
        assert PillarStats("x", trades=0).win_rate == 0.0   # no div-by-zero

    def test_avg_pnl(self):
        assert PillarStats("x", trades=4, total_pnl=400.0).avg_pnl == 100.0
        assert PillarStats("x", trades=0).avg_pnl == 0.0

    def test_net_pnl_subtracts_commission(self):
        assert PillarStats("x", total_pnl=500.0, total_commission=40.0).net_pnl == 460.0


# ── AgoraBacktestResult aggregations ──────────────────────────────────────────
class TestResult:
    def _mixed(self):
        # 2 winners (+150, +50), 2 losers (-100, -80), 1 still open
        return _result(trades=[
            _trade(pnl=150.0, status=AgoraTradeStatus.CLOSED_PROFIT_TARGET, commission=2.0),
            _trade(pnl=50.0, status=AgoraTradeStatus.CLOSED_EXPIRY, commission=2.0),
            _trade(pnl=-100.0, status=AgoraTradeStatus.CLOSED_STOP_LOSS, commission=2.0),
            _trade(pnl=-80.0, status=AgoraTradeStatus.CLOSED_STOP_LOSS, commission=2.0),
            _trade(pnl=None, status=AgoraTradeStatus.OPEN),
        ])

    def test_closed_and_open_split(self):
        r = self._mixed()
        assert len(r.closed_trades) == 4
        assert r.total_trades == 5

    def test_winning_trades(self):
        assert len(self._mixed().winning_trades) == 2

    def test_total_pnl_excludes_open(self):
        assert self._mixed().total_pnl == 150 + 50 - 100 - 80   # 20

    def test_total_commissions(self):
        assert self._mixed().total_commissions == 8.0   # 4 closed × 2

    def test_win_rate(self):
        assert self._mixed().win_rate == 0.5   # 2 of 4 closed

    def test_win_rate_empty_is_zero(self):
        assert _result().win_rate == 0.0

    def test_ending_balance(self):
        # 10000 + 20 pnl - 8 commissions
        assert self._mixed().ending_balance == 10000 + 20 - 8

    def test_avg_win_and_loss(self):
        r = self._mixed()
        assert r.avg_win == (150 + 50) / 2          # 100
        assert r.avg_loss == (-100 + -80) / 2       # -90

    def test_profit_factor(self):
        # gross win 200 / gross loss 180
        assert self._mixed().profit_factor == pytest.approx(200 / 180)

    def test_profit_factor_inf_when_no_losses(self):
        r = _result(trades=[_trade(pnl=100.0, status=AgoraTradeStatus.CLOSED_PROFIT_TARGET)])
        assert r.profit_factor == float("inf")

    def test_max_drawdown_pct(self):
        # peak 11000, trough 9900 → dd = (11000-9900)/11000*100
        r = _result(equity=[10000, 11000, 10500, 9900, 10200])
        assert r.max_drawdown_pct == pytest.approx((11000 - 9900) / 11000 * 100)

    def test_max_drawdown_empty_is_zero(self):
        assert _result().max_drawdown_pct == 0.0

    def test_sharpe_zero_when_too_short(self):
        assert _result(equity=[10000]).sharpe_ratio == 0.0

    def test_sharpe_positive_for_rising_curve(self):
        r = _result(equity=[10000, 10100, 10200, 10350, 10500])
        assert r.sharpe_ratio > 0

    def test_pillar_attribution(self):
        r = _result(trades=[
            _trade(pnl=100.0, status=AgoraTradeStatus.CLOSED_PROFIT_TARGET, pillar="vol_premium", commission=2.0),
            _trade(pnl=-50.0, status=AgoraTradeStatus.CLOSED_STOP_LOSS, pillar="vol_premium", commission=2.0),
            _trade(pnl=200.0, status=AgoraTradeStatus.CLOSED_PROFIT_TARGET, pillar="directional", commission=2.0),
        ])
        attr = r.pillar_attribution()
        assert set(attr) == {"vol_premium", "directional"}
        vp = attr["vol_premium"]
        assert vp.trades == 2 and vp.wins == 1
        assert vp.total_pnl == 50.0 and vp.win_rate == 0.5
        assert vp.best_trade == 100.0 and vp.worst_trade == -50.0
        assert attr["directional"].win_rate == 1.0

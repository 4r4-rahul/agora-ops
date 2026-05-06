"""Unit tests for core Pydantic models."""

from __future__ import annotations

import pytest
from datetime import date, datetime
from uuid import UUID

from trading_platform.core.models.market import Bar, MarketSnapshot, OptionContract, OptionsChain
from trading_platform.core.models.trade import (
    Direction,
    OptionsStrategyType,
    SpreadLeg,
    TradeDecision,
    TradeRecommendation,
    TradeStatus,
)
from trading_platform.core.models.risk import Greeks, LiquidityCheck, PositionSizing, RiskAssessment


class TestBar:
    def test_valid_bar(self):
        bar = Bar(ts=datetime.utcnow(), open=100, high=105, low=99, close=103, volume=1000)
        assert bar.high >= bar.low

    def test_invalid_bar_high_lt_low(self):
        with pytest.raises(ValueError):
            Bar(ts=datetime.utcnow(), open=100, high=95, low=99, close=103, volume=1000)


class TestOptionContract:
    def _make(self, **kwargs) -> OptionContract:
        defaults = dict(
            ticker="SPY",
            expiration=date.today(),
            strike=450.0,
            option_type="call",
            bid=2.00,
            ask=2.10,
        )
        defaults.update(kwargs)
        return OptionContract(**defaults)

    def test_mid(self):
        c = self._make(bid=2.00, ask=2.10)
        assert abs(c.mid - 2.05) < 0.001

    def test_spread_pct(self):
        c = self._make(bid=2.00, ask=2.10)
        assert abs(c.spread_pct - 0.10 / 2.05) < 0.001

    def test_dte(self):
        from datetime import timedelta
        c = self._make(expiration=date.today() + timedelta(days=7))
        assert c.dte == 7

    def test_liquidity_check_pass(self):
        c = self._make(bid=2.00, ask=2.10, open_interest=500, volume=200)
        assert c.is_liquid

    def test_liquidity_check_fail_oi(self):
        c = self._make(bid=2.00, ask=2.10, open_interest=50, volume=200)
        assert not c.is_liquid


class TestLiquidityCheck:
    def test_pass(self):
        check = LiquidityCheck.evaluate("SPY", 500, 200, 2.00, 2.10)
        assert check.passed

    def test_fail_oi(self):
        check = LiquidityCheck.evaluate("SPY", 50, 200, 2.00, 2.10)
        assert not check.passed
        assert "OI" in check.notes

    def test_fail_spread(self):
        check = LiquidityCheck.evaluate("SPY", 500, 200, 1.00, 2.00)
        assert not check.passed
        assert "spread" in check.notes


class TestPositionSizing:
    def test_basic_sizing(self):
        sizing = PositionSizing.calculate(
            ticker="SPY",
            max_loss_per_contract=200.0,
            account_size=25_000,
            max_position_pct=0.05,
            max_risk_pct=0.02,
        )
        assert sizing.recommended_contracts >= 1
        assert sizing.total_max_loss <= 25_000 * 0.05
        assert sizing.account_risk_pct <= 5.0


class TestTradeRecommendation:
    def _make(self) -> TradeRecommendation:
        return TradeRecommendation(
            session_id="test-session",
            ticker="SPY",
            direction=Direction.BULLISH,
            thesis="SPY is in a bull trend above all key SMAs with strong breadth.",
            strategy=OptionsStrategyType.BULL_CALL_SPREAD,
            expiration=date(2026, 5, 16),
            legs=[
                SpreadLeg(
                    option_type="call",
                    strike=520.0,
                    expiration=date(2026, 5, 16),
                    action="buy",
                ),
                SpreadLeg(
                    option_type="call",
                    strike=525.0,
                    expiration=date(2026, 5, 16),
                    action="sell",
                ),
            ],
            strike_selection_logic="Buy ATM call, sell 5 points OTM to cap cost.",
            entry_trigger="SPY breaks above 520.50 on volume > 1.5x avg",
            entry_price=2.50,
            stop_loss=1.25,
            stop_loss_logic="Exit at 50% of debit — hard rule",
            profit_target=4.00,
            profit_target_logic="Exit at 1.6:1 R/R target",
            max_loss_dollars=250.0,
            max_gain_dollars=250.0,
            reward_risk_ratio=1.6,
            contracts=1,
            position_size_dollars=250.0,
            liquidity_ok=True,
            iv_rank=45.0,
        )

    def test_creation(self):
        rec = self._make()
        assert isinstance(rec.id, UUID)
        assert rec.final_decision == TradeDecision.PENDING_APPROVAL

    def test_approve(self):
        rec = self._make()
        rec.approve("trader_1")
        assert rec.final_decision == TradeDecision.APPROVED
        assert rec.approved_by == "trader_1"
        assert rec.approved_at is not None

    def test_reject(self):
        rec = self._make()
        rec.reject(["IV too high", "poor liquidity"])
        assert rec.final_decision == TradeDecision.REJECTED
        assert len(rec.rejection_reasons) == 2

    def test_thesis_min_length(self):
        with pytest.raises(ValueError):
            base = self._make().model_dump()
            base["thesis"] = "short"
            TradeRecommendation.model_validate(base)


class TestGreeks:
    def test_addition(self):
        g1 = Greeks(delta=0.5, gamma=0.02, theta=-0.05, vega=0.3)
        g2 = Greeks(delta=-0.3, gamma=0.01, theta=-0.03, vega=0.2)
        combined = g1 + g2
        assert abs(combined.delta - 0.2) < 1e-10
        assert abs(combined.gamma - 0.03) < 1e-10

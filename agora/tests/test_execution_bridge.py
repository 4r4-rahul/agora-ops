"""
agora/tests/test_execution_bridge.py — the IBKR execution bridge with a FAKE broker (the order
placement functions are mocked, so no ib_insync, no socket). Covers the precision pieces that
decide what order actually goes to the broker:
  • _rec_to_legs / _pos_to_close_legs — leg-dict construction (action casing, exact expiry, DTE
    floor) and the verified rule that close legs keep the ORIGINAL entry action (reversing here
    double-reverses and re-opens the position — the 'exits not executing' bug).
  • submit_trade — per-share pricing (divide totals by contracts×100), the debit-vs-credit
    profit-target formula, the 2× stop, and spread-type-aware routing (single-leg → native leg
    order; paper spread → leg-by-leg; live spread → atomic BAG).
"""
from __future__ import annotations

import types
from datetime import date, timedelta
from unittest.mock import AsyncMock

import pytest

import trading_platform.services.ibkr_client as ibkr_client
from agora.execution.ibkr_bridge import _pos_to_close_legs, _rec_to_legs, close_trade, submit_trade


# ── builders ──────────────────────────────────────────────────────────────────
def _leg(action="sell", option_type="put", strike=100.0, dte=30):
    return types.SimpleNamespace(
        action=action, option_type=option_type, strike=strike,
        expiration=date.today() + timedelta(days=dte),
    )


def _rec(legs=None, contracts=1, entry_debit_credit=-150.0, max_gain=150.0, strategy="bull_put_spread"):
    return types.SimpleNamespace(
        ticker="AAPL", strategy=types.SimpleNamespace(value=strategy),
        contracts=contracts, entry_debit_credit=entry_debit_credit,
        max_gain_dollars=max_gain, conviction_score=70.0,
        legs=legs if legs is not None else [_leg("sell", "put", 100), _leg("buy", "put", 95)],
    )


def _settings(trading_mode="paper"):
    return types.SimpleNamespace(
        trading_mode=trading_mode, ibkr_host="127.0.0.1", ibkr_port=7497, ibkr_client_id=2,
    )


# ── _rec_to_legs ──────────────────────────────────────────────────────────────
class TestRecToLegs:
    def test_action_uppercased(self):
        legs = _rec_to_legs(_rec(legs=[_leg("sell"), _leg("buy")]))
        assert [l["action"] for l in legs] == ["SELL", "BUY"]

    def test_exact_expiry_string(self):
        exp = date.today() + timedelta(days=30)
        legs = _rec_to_legs(_rec(legs=[_leg("buy", dte=30)]))
        assert legs[0]["expiration_date"] == exp.strftime("%Y%m%d")

    def test_dte_floored_at_one(self):
        # an expiry in the past must not produce a 0/negative DTE
        legs = _rec_to_legs(_rec(legs=[_leg("buy", dte=-5)]))
        assert legs[0]["expiration_dte"] == 1

    def test_quantity_is_one_per_leg(self):
        legs = _rec_to_legs(_rec(legs=[_leg(), _leg()]))
        assert all(l["quantity"] == 1 for l in legs)

    def test_strike_and_type_carried(self):
        legs = _rec_to_legs(_rec(legs=[_leg("buy", "call", 110)]))
        assert legs[0]["strike"] == 110 and legs[0]["option_type"] == "call"


# ── _pos_to_close_legs (must NOT reverse actions) ─────────────────────────────
class TestPosToCloseLegs:
    def test_keeps_original_entry_actions(self):
        pos = types.SimpleNamespace(legs=[_leg("sell", "put", 100), _leg("buy", "put", 95)])
        legs = _pos_to_close_legs(pos)
        # original actions preserved — the close fn reverses them itself; reversing here would
        # double-reverse and re-open the position.
        assert [l["action"] for l in legs] == ["SELL", "BUY"]

    def test_exact_expiry_preserved(self):
        exp = date.today() + timedelta(days=10)
        pos = types.SimpleNamespace(legs=[_leg("buy", dte=10)])
        assert _pos_to_close_legs(pos)[0]["expiration_date"] == exp.strftime("%Y%m%d")


# ── submit_trade pricing + routing (fake broker) ──────────────────────────────
@pytest.fixture
def fake_broker(monkeypatch):
    bracket = AsyncMock(return_value={"status": "Filled", "order_id": 1, "fills": []})
    legs = AsyncMock(return_value={"status": "Filled", "order_id": 2, "fills": []})
    close_bag = AsyncMock(return_value={"status": "Filled", "order_id": 3, "fills": []})
    close_legs = AsyncMock(return_value={"status": "Filled", "order_id": 4, "fills": []})
    monkeypatch.setattr(ibkr_client, "place_bracket_order", bracket)
    monkeypatch.setattr(ibkr_client, "place_legs_individually", legs)
    monkeypatch.setattr(ibkr_client, "close_position", close_bag)
    monkeypatch.setattr(ibkr_client, "close_position_legs", close_legs)
    return types.SimpleNamespace(bracket=bracket, legs=legs,
                                 close_bag=close_bag, close_legs=close_legs)


def _position(strategy="bull_put_spread", legs=None, contracts=1, unrealized_pnl=50.0):
    return types.SimpleNamespace(
        ticker="AAPL", strategy=types.SimpleNamespace(value=strategy),
        contracts=contracts, unrealized_pnl=unrealized_pnl,
        legs=legs if legs is not None else [_leg("sell", "put", 100), _leg("buy", "put", 95)],
    )


class TestCloseTradeRouting:
    @pytest.mark.asyncio
    async def test_paper_credit_closes_leg_by_leg(self, fake_broker):
        await close_trade(_position("bull_put_spread"), _settings("paper"), "C1")
        fake_broker.close_legs.assert_awaited_once()
        fake_broker.close_bag.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_single_leg_long_closes_native(self, fake_broker):
        pos = _position("long_call", legs=[_leg("buy", "call", 100)])
        await close_trade(pos, _settings("paper"), "C2")
        fake_broker.close_legs.assert_awaited_once()
        fake_broker.close_bag.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_paper_debit_closes_atomic_bag(self, fake_broker):
        # debit spread is NOT a credit strategy → BAG even on paper
        pos = _position("bull_call_spread", legs=[_leg("buy", "call", 100), _leg("sell", "call", 110)])
        await close_trade(pos, _settings("paper"), "C3")
        fake_broker.close_bag.assert_awaited_once()
        fake_broker.close_legs.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_live_credit_closes_atomic_bag(self, fake_broker):
        await close_trade(_position("bull_put_spread"), _settings("live"), "C4")
        fake_broker.close_bag.assert_awaited_once()
        fake_broker.close_legs.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_close_uses_dedicated_client_id(self, fake_broker):
        # close uses ibkr_client_id + 1 so it never collides with the entry connection
        await close_trade(_position("bull_put_spread"), _settings("paper"), "C5")
        assert fake_broker.close_legs.call_args.kwargs["client_id"] == _settings().ibkr_client_id + 1

    @pytest.mark.asyncio
    async def test_close_legs_keep_original_actions(self, fake_broker):
        await close_trade(_position("bull_put_spread"), _settings("paper"), "C6")
        legs = fake_broker.close_legs.call_args.kwargs["legs"]
        assert [l["action"] for l in legs] == ["SELL", "BUY"]   # NOT reversed


class TestSubmitTradePricing:
    @pytest.mark.asyncio
    async def test_credit_spread_per_share_pricing(self, fake_broker):
        # entry -150 total, 1 contract → -1.50/sh; profit target = 50% of credit = 0.75; stop = 3.0
        rec = _rec(entry_debit_credit=-150.0, max_gain=150.0, contracts=1)
        await submit_trade(rec, _settings("paper"), "S1")
        kw = fake_broker.legs.call_args.kwargs
        assert kw["entry_price"] == pytest.approx(-1.50)
        assert kw["profit_target"] == pytest.approx(0.75)
        assert kw["stop_loss"] == pytest.approx(3.00)

    @pytest.mark.asyncio
    async def test_debit_spread_profit_target_formula(self, fake_broker):
        # entry +300 total → +3.0/sh; max_gain 700 → 7.0/sh; target = 3.0 + 0.5*7.0 = 6.5; stop 6.0
        rec = _rec(entry_debit_credit=300.0, max_gain=700.0, contracts=1, strategy="bull_call_spread",
                   legs=[_leg("buy", "call", 100), _leg("sell", "call", 110)])
        await submit_trade(rec, _settings("live"), "S2")
        kw = fake_broker.bracket.call_args.kwargs
        assert kw["entry_price"] == pytest.approx(3.0)
        assert kw["profit_target"] == pytest.approx(6.5)
        assert kw["stop_loss"] == pytest.approx(6.0)

    @pytest.mark.asyncio
    async def test_contracts_scale_per_share_divisor(self, fake_broker):
        # entry -300 total, 2 contracts → divisor 200 → -1.50/sh
        rec = _rec(entry_debit_credit=-300.0, max_gain=300.0, contracts=2)
        await submit_trade(rec, _settings("paper"), "S3")
        assert fake_broker.legs.call_args.kwargs["entry_price"] == pytest.approx(-1.50)


class TestSubmitTradeRouting:
    @pytest.mark.asyncio
    async def test_single_leg_uses_native_leg_order(self, fake_broker):
        rec = _rec(legs=[_leg("buy", "call", 100)], entry_debit_credit=250.0,
                   max_gain=0.0, strategy="long_call")
        await submit_trade(rec, _settings("live"), "S4")
        fake_broker.legs.assert_awaited_once()
        fake_broker.bracket.assert_not_awaited()
        assert fake_broker.legs.call_args.kwargs["adaptive_single_leg"] is True

    @pytest.mark.asyncio
    async def test_paper_spread_legs_by_leg(self, fake_broker):
        await submit_trade(_rec(), _settings("paper"), "S5")
        fake_broker.legs.assert_awaited_once()
        fake_broker.bracket.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_live_spread_uses_atomic_bag(self, fake_broker):
        rec = _rec(entry_debit_credit=300.0, max_gain=700.0, strategy="bull_call_spread",
                   legs=[_leg("buy", "call", 100), _leg("sell", "call", 110)])
        await submit_trade(rec, _settings("live"), "S6")
        fake_broker.bracket.assert_awaited_once()
        fake_broker.legs.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_legs_passed_to_broker(self, fake_broker):
        rec = _rec(legs=[_leg("sell", "put", 100), _leg("buy", "put", 95)])
        await submit_trade(rec, _settings("paper"), "S7")
        legs = fake_broker.legs.call_args.kwargs["legs"]
        assert [l["strike"] for l in legs] == [100, 95]
        assert [l["action"] for l in legs] == ["SELL", "BUY"]

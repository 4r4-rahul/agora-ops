"""
test_execution_routing.py — submit_trade routing + marketable-start wiring.

Locks in the 2026-06-16 execution fix:
  • PAPER: ALL spreads (credit AND debit) route leg-by-leg (debit no longer hits the BAG/Error-103
    path that froze it at 0% fill). Single legs already did.
  • entry_marketable_start is threaded ONLY to place_legs_individually (the BAG path can't accept it).
  • LIVE spreads still use the atomic BAG (place_bracket_order).
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

import agora.execution.ibkr_bridge as bridge
import trading_platform.services.ibkr_client as ic
from agora.core.config import AgoraSettings

_EXP = date.today() + timedelta(days=31)


def _rec(strategy: str, legs, entry_dc: float):
    leg_objs = [SimpleNamespace(option_type=ot, strike=k, expiration=_EXP,
                                action=act, contracts=1) for ot, k, act in legs]
    return SimpleNamespace(ticker="NVDA", strategy=SimpleNamespace(value=strategy), contracts=1,
                           entry_debit_credit=entry_dc, max_gain_dollars=100.0,
                           conviction_score=70.0, legs=leg_objs)


def _route(monkeypatch, rec, mode):
    s = AgoraSettings()
    s.trading_mode = mode
    cap: dict = {}

    async def stub_legs(**kw):
        cap["fn"], cap["kw"] = "legs", kw
        return {"status": "ok"}

    async def stub_bag(**kw):
        cap["fn"], cap["kw"] = "bag", kw
        return {"status": "ok"}

    monkeypatch.setattr(ic, "place_legs_individually", stub_legs)
    monkeypatch.setattr(ic, "place_bracket_order", stub_bag)
    asyncio.new_event_loop().run_until_complete(bridge.submit_trade(rec, s, "sess-1"))
    return cap


# credit spread = negative entry_debit_credit; debit = positive
_BULL_PUT = ("bull_put_spread", [("put", 95, "sell"), ("put", 90, "buy")], -120.0)
_BULL_CALL = ("bull_call_spread", [("call", 100, "buy"), ("call", 105, "sell")], +200.0)
_LONG_CALL = ("long_call", [("call", 100, "buy")], +250.0)


def test_paper_debit_spread_routes_leg_by_leg(monkeypatch):
    """THE FIX: a paper debit spread (bull_call) must leg-in, not hit the BAG path."""
    cap = _route(monkeypatch, _rec(*_BULL_CALL), "paper")
    assert cap["fn"] == "legs"
    assert cap["kw"]["entry_marketable_start"] is True


def test_paper_credit_spread_routes_leg_by_leg(monkeypatch):
    cap = _route(monkeypatch, _rec(*_BULL_PUT), "paper")
    assert cap["fn"] == "legs" and cap["kw"]["entry_marketable_start"] is True


def test_single_leg_routes_leg_by_leg_with_marketable(monkeypatch):
    cap = _route(monkeypatch, _rec(*_LONG_CALL), "paper")
    assert cap["fn"] == "legs"
    assert cap["kw"]["entry_marketable_start"] is True
    assert cap["kw"]["adaptive_single_leg"] is True


def test_live_spread_uses_bag_without_marketable_kwarg(monkeypatch):
    """LIVE spread -> atomic BAG; entry_marketable_start must NOT be passed (BAG rejects it)."""
    cap = _route(monkeypatch, _rec(*_BULL_CALL), "live")
    assert cap["fn"] == "bag"
    assert "entry_marketable_start" not in cap["kw"]


def test_flag_off_still_routes_but_marketable_false(monkeypatch):
    s = AgoraSettings(); s.trading_mode = "paper"; s.entry_marketable_start = False
    cap: dict = {}
    async def stub_legs(**kw):
        cap["fn"], cap["kw"] = "legs", kw; return {"status": "ok"}
    monkeypatch.setattr(ic, "place_legs_individually", stub_legs)
    asyncio.new_event_loop().run_until_complete(bridge.submit_trade(_rec(*_BULL_PUT), s, "s"))
    assert cap["kw"]["entry_marketable_start"] is False

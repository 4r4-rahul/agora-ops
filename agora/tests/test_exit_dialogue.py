"""Tests for the entry<->exit brain dialogue — the exit brain's payload now carries the live
market read (current_signals) and the entry brain's FRESH thesis (entry_brain_now), so it
judges against current reality instead of guessing from a stale entry thesis + P&L.
"""
import datetime
import types

from agora.agents.exit_management import ExitIntelligenceAgent


def _bare_agent():
    # Bypass __init__ (which builds an anthropic client) — we only test pure methods.
    a = ExitIntelligenceAgent.__new__(ExitIntelligenceAgent)
    a._signal_provider = None
    return a


def _pos():
    return types.SimpleNamespace(
        position_id="p1", ticker="QQQ", strategy="iron_condor", direction="neutral",
        entry_date="2026-06-10", entry_price=-1.5, current_price=0.8,
        expiry_date=datetime.date(2026, 7, 10), legs=[],
        unrealized_pnl=40.0, max_gain_dollars=150.0, max_loss_dollars=-350.0,
        conviction_at_entry=64, regime_at_entry="neutral",
    )


def test_payload_includes_current_signals_and_fresh_read():
    a = _bare_agent()
    ctx = {
        "current_signals": {"gex_regime": "positive", "iv_rank": 42,
                            "flow": {"direction": "bullish", "strength": "moderate"}, "spot": 753.8},
        "entry_brain_now": {"direction": "neutral", "confidence_pct": 44,
                            "magnitude_pct": 0.8, "reasoning": "range-bound"},
    }
    p = a._build_payload(_pos(), {"direction": "neutral"}, None, [], ctx)
    assert p["current_signals"]["gex_regime"] == "positive"
    assert p["current_signals"]["flow"]["direction"] == "bullish"
    assert p["entry_brain_now"]["direction"] == "neutral"
    assert p["entry_brain_now"]["confidence_pct"] == 44
    # The stale entry context is still present alongside the fresh read (the dialogue).
    assert p["original_thesis"] == {"direction": "neutral"}
    assert p["conviction_at_entry"] == 64


def test_payload_handles_missing_context_gracefully():
    a = _bare_agent()
    p = a._build_payload(_pos(), {"direction": "neutral"}, None, [], None)
    assert p["current_signals"] == {}      # empty, not crashing
    assert p["entry_brain_now"] == {}


def test_set_signal_provider():
    a = _bare_agent()
    async def provider(position):
        return {"current_signals": {}, "entry_brain_now": {}}
    a.set_signal_provider(provider)
    assert a._signal_provider is provider

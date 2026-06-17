"""
agora/tests/test_event_patterns.py — EventPatternEngine: the deterministic macro-event pattern
detectors (pre-FOMC drift, CPI-eve IV condor, post-earnings skew reversion) that feed the
conviction scorer. Pure logic over a mocked macro calendar — a regression here injects a phantom
catalyst signal or drops a real one, biasing entries.
"""
from __future__ import annotations

import types
from datetime import date, timedelta

import pytest

from agora.signals.event_patterns import EventPatternEngine


def _engine(days_to_next=None, next_event=None):
    e = EventPatternEngine.__new__(EventPatternEngine)
    e._cal = types.SimpleNamespace(
        upcoming_events=lambda days=10: [],
        days_to_next_event=lambda today=None: (days_to_next, next_event),
    )
    return e


_TODAY = date(2026, 1, 15)


# ── post-earnings skew reversion (pure date + classification) ─────────────────
class TestPostEarningsSignal:
    def _sig(self, days_since, beat="strong", guidance="raised"):
        earnings = _TODAY - timedelta(days=days_since)
        return EventPatternEngine.get_post_earnings_signal(
            _engine(), "NVDA", earnings, beat, guidance, today=_TODAY)

    def test_fires_t1_to_t3(self):
        assert self._sig(1) is not None
        assert self._sig(2) is not None
        assert self._sig(3) is not None

    def test_silent_on_earnings_day(self):
        assert self._sig(0) is None

    def test_silent_after_t3(self):
        assert self._sig(4) is None

    def test_miss_and_lowered_is_skipped(self):
        assert self._sig(2, beat="miss", guidance="lowered") is None

    def test_strong_raised_high_confidence_bullish(self):
        s = self._sig(2, beat="strong", guidance="raised")
        assert s["direction"] == "bullish"
        assert s["confidence"] == 0.75
        assert s["strategy_hint"] == "bull_put_spread"
        assert s["dte_target"] == 21

    def test_moderate_is_bullish_lower_confidence(self):
        s = self._sig(2, beat="moderate", guidance="flat")
        assert s["direction"] == "bullish" and s["confidence"] == 0.55

    def test_miss_with_flat_guidance_is_neutral_but_present(self):
        # miss (not miss+lowered) still returns a signal, but neutral direction
        s = self._sig(2, beat="miss", guidance="flat")
        assert s is not None and s["direction"] == "neutral"

    def test_days_since_recorded(self):
        assert self._sig(2)["days_since_earnings"] == 2


# ── FOMC drift ────────────────────────────────────────────────────────────────
class TestFomcDrift:
    def test_fires_for_spy_within_window(self):
        e = _engine(days_to_next=3, next_event="FOMC meeting")
        sigs = e._fomc_drift("SPY", _TODAY)
        assert len(sigs) == 1
        s = sigs[0]
        assert s["event_type"] == "fomc_drift"
        assert s["direction"] == "bullish"
        assert s["strategy_hint"] == "bull_call_spread"
        assert s["days_to_event"] == 3

    def test_only_spy_and_qqq(self):
        e = _engine(days_to_next=3, next_event="FOMC")
        assert e._fomc_drift("AAPL", _TODAY) == []
        assert len(e._fomc_drift("QQQ", _TODAY)) == 1

    def test_outside_t5_window_silent(self):
        assert _engine(days_to_next=6, next_event="FOMC")._fomc_drift("SPY", _TODAY) == []
        assert _engine(days_to_next=0, next_event="FOMC")._fomc_drift("SPY", _TODAY) == []

    def test_non_fomc_event_silent(self):
        assert _engine(days_to_next=3, next_event="CPI")._fomc_drift("SPY", _TODAY) == []


# ── CPI condor ──────────────────────────────────────────────────────────────--
class TestCpiCondor:
    def test_fires_t1_before_cpi(self):
        sigs = _engine(days_to_next=1, next_event="CPI release")._cpi_condor("QQQ", _TODAY)
        assert len(sigs) == 1
        s = sigs[0]
        assert s["event_type"] == "cpi_iv_premium"
        assert s["direction"] == "neutral"
        assert s["strategy_hint"] == "iron_condor"

    def test_only_t1(self):
        assert _engine(days_to_next=2, next_event="CPI")._cpi_condor("SPY", _TODAY) == []

    def test_only_spy_qqq(self):
        assert _engine(days_to_next=1, next_event="CPI")._cpi_condor("TSLA", _TODAY) == []

    def test_non_cpi_event_silent(self):
        assert _engine(days_to_next=1, next_event="FOMC")._cpi_condor("SPY", _TODAY) == []


# ── get_signals aggregation ───────────────────────────────────────────────────
class TestGetSignals:
    def test_returns_fomc_when_next(self):
        sigs = _engine(days_to_next=2, next_event="FOMC")._fomc_drift("SPY", _TODAY)
        full = _engine(days_to_next=2, next_event="FOMC").get_signals("SPY", today=_TODAY)
        assert [s["event_type"] for s in full] == ["fomc_drift"]

    def test_returns_cpi_when_next(self):
        full = _engine(days_to_next=1, next_event="CPI").get_signals("QQQ", today=_TODAY)
        assert [s["event_type"] for s in full] == ["cpi_iv_premium"]

    def test_empty_for_non_index_ticker(self):
        assert _engine(days_to_next=1, next_event="CPI").get_signals("AAPL", today=_TODAY) == []

    def test_empty_when_no_event(self):
        assert _engine(days_to_next=None, next_event=None).get_signals("SPY", today=_TODAY) == []

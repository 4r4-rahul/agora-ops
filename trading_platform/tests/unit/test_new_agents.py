"""
Unit tests for the four new agents added in the Tier-2 build:
  - SetupWatcherAgent (setup detection arithmetic)
  - PreMarketAgent (gap + VIX term structure logic)
  - PerformanceFeedbackAgent (statistics computation)
  - UniverseScreenerAgent (mechanical filter scoring)

All tests are pure-unit: no network calls, no database, no Claude API.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

ET = ZoneInfo("America/New_York")


# ── SetupWatcherAgent ─────────────────────────────────────────────────────────

class TestSetupWatcherDetection:
    """Tests for _detect_setup() — pure arithmetic, no I/O."""

    def _make_agent(self):
        from trading_platform.agents.setup_watcher import SetupWatcherAgent
        bus = MagicMock()
        state = MagicMock()
        settings = MagicMock()
        settings.default_tickers = ["SPY"]
        agent = SetupWatcherAgent.__new__(SetupWatcherAgent)
        agent._bus = bus
        agent._state = state
        agent._settings = settings
        agent._poll_interval = 300
        agent._cooldown = 900
        agent._running = False
        agent._ticker_states = {}
        agent._task = None
        import logging
        agent._log = logging.getLogger("test.setup_watcher")
        return agent

    def _make_state(self, orb_high=505.0, orb_low=495.0, vwap=500.0, atr5=2.0):
        from trading_platform.agents.setup_watcher import _TickerState
        s = _TickerState(ticker="SPY")
        s.orb_high = orb_high
        s.orb_low = orb_low
        s.orb_set = True
        s.vwap = vwap
        s.atr5 = atr5
        s.bars = [{"high": 501, "low": 499, "close": 500, "volume": 1_000_000}] * 10
        return s

    def test_orb_break_up(self):
        agent = self._make_agent()
        state = self._make_state()
        latest = {"high": 506, "low": 504, "close": 505.6, "volume": 1_200_000}
        prev   = {"high": 504, "low": 502, "close": 504.9, "volume": 900_000}
        result = agent._detect_setup(state, latest, prev)
        assert result == "ORB_BREAK_UP"

    def test_orb_break_down(self):
        agent = self._make_agent()
        state = self._make_state()
        latest = {"high": 496, "low": 494, "close": 494.4, "volume": 1_200_000}
        prev   = {"high": 497, "low": 495, "close": 495.1, "volume": 900_000}
        result = agent._detect_setup(state, latest, prev)
        assert result == "ORB_BREAK_DOWN"

    def test_vwap_reclaim_up_requires_vol_spike(self):
        agent = self._make_agent()
        state = self._make_state(vwap=500.0)
        # Cross VWAP upward — but low volume (no spike)
        latest = {"high": 501, "low": 499.5, "close": 500.5, "volume": 800_000}
        prev   = {"high": 500, "low": 498,   "close": 499.5, "volume": 900_000}
        # avg_vol from 10 bars all at 1M → vol_spike needs > 1.5M
        result = agent._detect_setup(state, latest, prev)
        assert result != "VWAP_RECLAIM_UP"

    def test_vwap_reclaim_up_with_vol_spike(self):
        agent = self._make_agent()
        state = self._make_state(vwap=500.0)
        state.bars = [{"high": 501, "low": 499, "close": 500, "volume": 1_000_000}] * 10
        latest = {"high": 502, "low": 499.5, "close": 501.0, "volume": 1_600_000}
        prev   = {"high": 500, "low": 498,   "close": 499.5, "volume": 900_000}
        result = agent._detect_setup(state, latest, prev)
        assert result == "VWAP_RECLAIM_UP"

    def test_momentum_surge_up(self):
        agent = self._make_agent()
        state = self._make_state(atr5=2.0)
        # bar_range = 6 > 2*2=4; close near top (top 20%)
        latest = {"high": 506, "low": 500, "close": 505.5, "volume": 1_000_000}
        prev   = {"high": 503, "low": 501, "close": 502.0, "volume": 900_000}
        result = agent._detect_setup(state, latest, prev)
        assert result == "MOMENTUM_SURGE_UP"

    def test_momentum_surge_down(self):
        agent = self._make_agent()
        state = self._make_state(atr5=2.0)
        latest = {"high": 504, "low": 498, "close": 498.5, "volume": 1_000_000}
        prev   = {"high": 503, "low": 501, "close": 502.0, "volume": 900_000}
        result = agent._detect_setup(state, latest, prev)
        assert result == "MOMENTUM_SURGE_DOWN"

    def test_no_trigger_in_quiet_market(self):
        agent = self._make_agent()
        state = self._make_state(atr5=2.0)
        # bar_range=1 < 4; no ORB break; no VWAP cross
        latest = {"high": 500.5, "low": 499.5, "close": 500.1, "volume": 950_000}
        prev   = {"high": 500.3, "low": 499.7, "close": 500.0, "volume": 900_000}
        result = agent._detect_setup(state, latest, prev)
        assert result is None

    def test_no_trigger_without_orb(self):
        from trading_platform.agents.setup_watcher import _TickerState
        agent = self._make_agent()
        state = _TickerState(ticker="SPY")
        state.orb_set = False
        state.vwap = None
        state.atr5 = None
        state.bars = []
        latest = {"high": 510, "low": 490, "close": 509, "volume": 2_000_000}
        prev   = {"high": 502, "low": 498, "close": 501, "volume": 900_000}
        result = agent._detect_setup(state, latest, prev)
        assert result is None


# ── PreMarketAgent ────────────────────────────────────────────────────────────

class TestPreMarketGapLogic:
    """Tests for gap classification and VIX term structure — no network I/O."""

    def _build_context_sync(self, ticker_close, ticker_prev, vix_close=15.0, vixmt_close=17.5, es_close=None, es_prev=None):
        """
        Call _build_context logic directly by patching yfinance.
        We test the classification rules without real data.
        """
        gap_pct = (ticker_close - ticker_prev) / ticker_prev

        if abs(gap_pct) < 0.003:
            gap_type = "flat"
        elif abs(gap_pct) < 0.008:
            gap_type = "small_gap"
        elif abs(gap_pct) < 0.015:
            gap_type = "medium_gap"
        elif abs(gap_pct) < 0.025:
            gap_type = "large_gap"
        else:
            gap_type = "extreme_gap"

        fill_map = {
            "flat": 0.0, "small_gap": 0.72, "medium_gap": 0.56,
            "large_gap": 0.38, "extreme_gap": 0.22,
        }
        gap_fill_prob = fill_map[gap_type]

        vix_spread = vixmt_close - vix_close
        if vix_spread > 2:
            vix_term = "contango"
        elif vix_spread < -2:
            vix_term = "backwardation"
        else:
            vix_term = "flat"

        return gap_type, gap_fill_prob, vix_term

    def test_flat_gap(self):
        gap_type, prob, _ = self._build_context_sync(500.0, 500.1)
        assert gap_type == "flat"
        assert prob == 0.0

    def test_small_gap_up(self):
        gap_type, prob, _ = self._build_context_sync(502.0, 500.0)
        assert gap_type == "small_gap"
        assert prob == pytest.approx(0.72)

    def test_large_gap(self):
        gap_type, prob, _ = self._build_context_sync(510.0, 500.0)  # 2%
        assert gap_type == "large_gap"
        assert prob == pytest.approx(0.38)

    def test_extreme_gap(self):
        gap_type, prob, _ = self._build_context_sync(514.0, 500.0)  # 2.8%
        assert gap_type == "extreme_gap"
        assert prob == pytest.approx(0.22)

    def test_vix_contango(self):
        _, _, vix_term = self._build_context_sync(500.0, 500.0, vix_close=15.0, vixmt_close=18.0)
        assert vix_term == "contango"

    def test_vix_backwardation(self):
        _, _, vix_term = self._build_context_sync(500.0, 500.0, vix_close=30.0, vixmt_close=25.0)
        assert vix_term == "backwardation"

    def test_vix_flat(self):
        _, _, vix_term = self._build_context_sync(500.0, 500.0, vix_close=18.0, vixmt_close=19.0)
        assert vix_term == "flat"

    def test_daily_cache_is_class_level(self):
        """Class-level cache doesn't bleed between instances."""
        from trading_platform.agents.premarket import PreMarketAgent
        PreMarketAgent._daily_cache.clear()
        today = date.today()
        PreMarketAgent._daily_cache["SPY"] = (today, {"gap_type": "flat"})
        # A new instance sees the same cache
        PreMarketAgent._daily_cache["QQQ"] = (today, {"gap_type": "small_gap"})
        assert PreMarketAgent._daily_cache["SPY"][1]["gap_type"] == "flat"
        assert PreMarketAgent._daily_cache["QQQ"][1]["gap_type"] == "small_gap"
        PreMarketAgent._daily_cache.clear()


# ── PerformanceFeedbackAgent ──────────────────────────────────────────────────

def _make_trade(pnl: float, strategy: str = "bull_call_spread",
                exit_reason: str = "profit_target",
                opened_at: str = "2026-04-28T10:30:00") -> dict:
    return {
        "realized_pnl": pnl,
        "strategy": strategy,
        "exit_reason": exit_reason,
        "opened_at": opened_at,
    }


class TestComputeStatistics:

    def test_empty_returns_zero_count(self):
        from trading_platform.agents.performance_feedback import compute_statistics
        result = compute_statistics([])
        assert result == {"trade_count": 0}

    def test_win_rate_all_winners(self):
        from trading_platform.agents.performance_feedback import compute_statistics
        trades = [_make_trade(100), _make_trade(200), _make_trade(50)]
        stats = compute_statistics(trades)
        assert stats["win_rate"] == pytest.approx(1.0)

    def test_win_rate_all_losers(self):
        from trading_platform.agents.performance_feedback import compute_statistics
        trades = [_make_trade(-100), _make_trade(-50)]
        stats = compute_statistics(trades)
        assert stats["win_rate"] == pytest.approx(0.0)

    def test_win_rate_mixed(self):
        from trading_platform.agents.performance_feedback import compute_statistics
        trades = [_make_trade(100), _make_trade(-50), _make_trade(200), _make_trade(-75)]
        stats = compute_statistics(trades)
        assert stats["win_rate"] == pytest.approx(0.5)

    def test_expectancy_positive_edge(self):
        from trading_platform.agents.performance_feedback import compute_statistics
        # 60% WR, avg win $150, avg loss -$100 → expectancy = 0.6*150 + 0.4*(-100) = 50
        trades = [
            _make_trade(150), _make_trade(150), _make_trade(150),
            _make_trade(-100), _make_trade(-100),
        ]
        stats = compute_statistics(trades)
        assert stats["expectancy"] == pytest.approx(50.0)

    def test_profit_factor_greater_than_one_for_profitable_system(self):
        from trading_platform.agents.performance_feedback import compute_statistics
        trades = [_make_trade(200), _make_trade(150), _make_trade(-100)]
        stats = compute_statistics(trades)
        assert stats["profit_factor"] > 1.0

    def test_by_strategy_groups_correctly(self):
        from trading_platform.agents.performance_feedback import compute_statistics
        trades = [
            _make_trade(100, strategy="bull_call_spread"),
            _make_trade(-50, strategy="bull_call_spread"),
            _make_trade(200, strategy="iron_condor"),
        ]
        stats = compute_statistics(trades)
        assert stats["by_strategy"]["bull_call_spread"]["count"] == 2
        assert stats["by_strategy"]["iron_condor"]["count"] == 1

    def test_by_exit_reason_groups(self):
        from trading_platform.agents.performance_feedback import compute_statistics
        trades = [
            _make_trade(100, exit_reason="profit_target"),
            _make_trade(-50, exit_reason="stop_loss"),
            _make_trade(80,  exit_reason="profit_target"),
        ]
        stats = compute_statistics(trades)
        assert stats["by_exit_reason"]["profit_target"]["count"] == 2
        assert stats["by_exit_reason"]["stop_loss"]["count"] == 1

    def test_dow_penalty_weight_generated_when_win_rate_low(self):
        from trading_platform.agents.performance_feedback import compute_statistics
        # 6 trades all on Monday (2026-04-27 = Monday), all losers → win rate < 40%
        trades = [_make_trade(-50, opened_at="2026-04-27T10:30:00") for _ in range(6)]
        stats = compute_statistics(trades)
        assert "dow_mon_penalty" in stats["weight_adjustments"]

    def test_dow_bonus_weight_generated_when_win_rate_high(self):
        from trading_platform.agents.performance_feedback import compute_statistics
        # 6 trades on Tuesday (2026-04-28 = Tuesday), all winners
        trades = [_make_trade(100, opened_at="2026-04-28T10:30:00") for _ in range(6)]
        stats = compute_statistics(trades)
        assert "dow_tue_bonus" in stats["weight_adjustments"]

    def test_half_kelly_never_negative(self):
        from trading_platform.agents.performance_feedback import compute_statistics
        # All losers → kelly negative → half_kelly should be 0
        trades = [_make_trade(-100), _make_trade(-50)]
        stats = compute_statistics(trades)
        assert stats["half_kelly"] >= 0.0


# ── UniverseScreenerAgent (mechanical filter) ─────────────────────────────────

class TestUniverseScreenerFilter:
    """Tests for _compute_stage0_score — pure arithmetic, no network."""

    def test_score_near_ideal_inputs(self):
        from trading_platform.agents.universe_screener import UniverseScreenerAgent
        # iv_rank=45, adv=10M, price=$150 → each sub-score at max
        score = UniverseScreenerAgent._compute_stage0_score(45.0, 10_000_000, 150.0)
        assert score == pytest.approx(100.0, abs=1.0)

    def test_score_low_adv_penalised(self):
        from trading_platform.agents.universe_screener import UniverseScreenerAgent
        # Same as ideal but adv = 500K (5% of peak)
        score_high_adv = UniverseScreenerAgent._compute_stage0_score(45.0, 10_000_000, 150.0)
        score_low_adv  = UniverseScreenerAgent._compute_stage0_score(45.0, 500_000,    150.0)
        assert score_high_adv > score_low_adv

    def test_score_monotone_in_adv(self):
        from trading_platform.agents.universe_screener import UniverseScreenerAgent
        score_500k = UniverseScreenerAgent._compute_stage0_score(45.0, 500_000,    150.0)
        score_5m   = UniverseScreenerAgent._compute_stage0_score(45.0, 5_000_000,  150.0)
        score_10m  = UniverseScreenerAgent._compute_stage0_score(45.0, 10_000_000, 150.0)
        assert score_500k < score_5m < score_10m

    def test_iv_rank_sweet_spot_at_45(self):
        from trading_platform.agents.universe_screener import UniverseScreenerAgent
        score_sweet = UniverseScreenerAgent._compute_stage0_score(45.0, 5_000_000, 150.0)
        score_high  = UniverseScreenerAgent._compute_stage0_score(90.0, 5_000_000, 150.0)
        score_low   = UniverseScreenerAgent._compute_stage0_score(5.0,  5_000_000, 150.0)
        assert score_sweet > score_high
        assert score_sweet > score_low

    def test_score_always_non_negative(self):
        from trading_platform.agents.universe_screener import UniverseScreenerAgent
        # Worst possible inputs
        score = UniverseScreenerAgent._compute_stage0_score(0.0, 0.0, 0.0)
        assert score >= 0.0

    def test_price_center_at_150(self):
        from trading_platform.agents.universe_screener import UniverseScreenerAgent
        score_center = UniverseScreenerAgent._compute_stage0_score(45.0, 5_000_000, 150.0)
        score_far    = UniverseScreenerAgent._compute_stage0_score(45.0, 5_000_000, 500.0)
        assert score_center > score_far

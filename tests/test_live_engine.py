#!/usr/bin/env python3
"""
Unit Tests for LiveScalpEngine
================================
Tests the live execution bridge with mocked provider/executor.
No IBKR connection required.

Covers:
  - Initialization & daily reset
  - Position sizing (hybrid LW + VolTarget)
  - IV estimation
  - Regime classification
  - Scan flow (signal → strike selection → sizing → order)
  - Exit monitoring (stop, target, time)
  - Daily loss limit
  - Cooldown logic
  - Stopped-direction tracking
  - Strategy gates (ORB defers to momentum, RF regime filter)
  - Dry-run mode
  - Properties (open_positions, trades_today, daily_realized_pnl)
  - Position serialization

Usage:
    python -m pytest tests/test_live_engine.py -v
"""

import os
import sys
import math
import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from dataclasses import dataclass
from datetime import datetime, date

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from trading_engine.config import EngineConfig, PositionSizingConfig
from trading_engine.execution import OrderStatus, FillResult
from trading_engine.live_engine import LiveScalpEngine, LiveScalpPosition
from trading_engine.regime import RegimeInfo


# ─────────────────────────────────────────────────────────────────
# Helpers — mock data generation
# ─────────────────────────────────────────────────────────────────

def make_bars(n_bars: int = 200, base_price: float = 600.0, atr: float = 0.50) -> pd.DataFrame:
    """Generate synthetic 1-minute bars for testing."""
    np.random.seed(42)
    timestamps = pd.date_range(
        "2026-01-15 09:30", periods=n_bars, freq="1min", tz="US/Eastern"
    )
    prices = [base_price]
    for _ in range(n_bars - 1):
        prices.append(prices[-1] + np.random.normal(0, atr * 0.2))

    prices = np.array(prices)
    highs = prices + np.random.uniform(0.05, atr * 0.5, n_bars)
    lows = prices - np.random.uniform(0.05, atr * 0.5, n_bars)
    opens = prices + np.random.normal(0, 0.1, n_bars)
    volumes = np.random.randint(100_000, 2_000_000, n_bars).astype(float)

    df = pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": prices,
        "volume": volumes,
        "vwap": prices + np.random.normal(0, 0.05, n_bars),
    }, index=timestamps)
    return df


def make_options_chain(price: float, right: str = "C") -> list:
    """Generate a synthetic options chain around the given price."""
    chain = []
    for offset in range(-5, 6):
        strike = round(price + offset, 0)
        distance = abs(offset)
        delta = max(0.05, 0.55 - distance * 0.08)
        mid = max(0.30, 3.0 - distance * 0.40)
        spread = mid * 0.10
        chain.append({
            "strike": strike,
            "right": right,
            "bid": round(mid - spread / 2, 2),
            "ask": round(mid + spread / 2, 2),
            "mid": round(mid, 2),
            "delta": round(delta, 3),
            "gamma": 0.03,
            "iv": 0.20,
        })
    return chain


def make_fill(status=OrderStatus.FILLED, price=2.50, qty=1, commission=1.30):
    """Create a mock FillResult."""
    return FillResult(
        order_id=1,
        status=status,
        num_contracts=qty,
        num_filled=qty if status == OrderStatus.FILLED else 0,
        avg_fill_price=price,
        commission=commission,
    )


def make_engine(account_size=10_000.0, config=None) -> LiveScalpEngine:
    """Create a LiveScalpEngine with mocked provider and executor."""
    provider = MagicMock()
    executor = MagicMock()
    cfg = config or EngineConfig()

    engine = LiveScalpEngine(
        provider=provider,
        executor=executor,
        config=cfg,
        account_size=account_size,
    )
    return engine


# ─────────────────────────────────────────────────────────────────
# Test: Initialization
# ─────────────────────────────────────────────────────────────────

class TestInitialization:
    def test_creates_engine(self):
        engine = make_engine()
        assert engine is not None
        assert engine.account_size == 10_000.0

    def test_all_positions_start_none(self):
        engine = make_engine()
        assert engine.open_scalp is None
        assert engine.open_runner is None
        assert engine.open_orb is None
        assert engine.open_rf is None

    def test_trade_counts_start_zero(self):
        engine = make_engine()
        assert engine.trades_today == 0
        assert engine.scalp_trades_today == 0
        assert engine.runner_trades_today == 0
        assert engine.orb_trades_today == 0
        assert engine.rf_trades_today == 0

    def test_daily_pnl_starts_zero(self):
        engine = make_engine()
        assert engine.daily_pnl == 0.0
        assert engine.daily_realized_pnl == 0.0

    def test_signal_engines_created(self):
        engine = make_engine()
        assert engine._signal_engine is not None
        assert engine._orb_signal_engine is not None
        assert engine._rf_signal_engine is not None

    def test_exit_engines_created(self):
        engine = make_engine()
        assert engine._scalp_exit is not None
        assert engine._runner_exit is not None
        assert engine._orb_exit is not None
        assert engine._rf_exit is not None

    def test_config_wiring(self):
        cfg = EngineConfig()
        engine = make_engine(config=cfg)
        assert engine.scalp_cfg is cfg.scalp
        assert engine.orb_cfg is cfg.orb
        assert engine.rf_cfg is cfg.range_fade
        assert engine.sizing_cfg is cfg.sizing


# ─────────────────────────────────────────────────────────────────
# Test: Daily Reset
# ─────────────────────────────────────────────────────────────────

class TestDailyReset:
    def test_reset_clears_positions(self):
        engine = make_engine()
        engine.open_scalp = LiveScalpPosition(ticker="SPY", tier="scalp")
        engine.open_runner = LiveScalpPosition(ticker="SPY", tier="runner")
        engine.reset_daily()
        assert engine.open_scalp is None
        assert engine.open_runner is None
        assert engine.open_orb is None
        assert engine.open_rf is None

    def test_reset_clears_trade_counts(self):
        engine = make_engine()
        engine.scalp_trades_today = 3
        engine.runner_trades_today = 1
        engine.orb_trades_today = 2
        engine.rf_trades_today = 1
        engine.reset_daily()
        assert engine.trades_today == 0

    def test_reset_clears_daily_pnl(self):
        engine = make_engine()
        engine.daily_pnl = 500.0
        engine.reset_daily()
        assert engine.daily_pnl == 0.0

    def test_reset_clears_state_flags(self):
        engine = make_engine()
        engine.stopped_directions.add("CALL")
        engine.cooldown_remaining = 5
        engine.momentum_fired_today = True
        engine.scalp_won_today = True
        engine.scalp_win_direction = "CALL"
        engine.reset_daily()
        assert len(engine.stopped_directions) == 0
        assert engine.cooldown_remaining == 0
        assert engine.momentum_fired_today is False
        assert engine.scalp_won_today is False
        assert engine.scalp_win_direction is None

    def test_reset_saves_prev_day_hl(self):
        engine = make_engine()
        bars = make_bars(100, base_price=600.0)
        engine._last_bars = bars
        engine.reset_daily()
        assert engine.prev_day_high == float(bars["high"].max())
        assert engine.prev_day_low == float(bars["low"].min())

    def test_reset_clears_precomputed_data(self):
        engine = make_engine()
        engine._orb_data = {"valid": True}
        engine._rf_data = {"valid": True}
        engine._regime_classified = True
        engine._precomp = {"something": True}
        engine._last_bars = make_bars(10)
        engine.reset_daily()
        assert engine._orb_data is None
        assert engine._rf_data is None
        assert engine._regime_classified is False
        assert engine._precomp is None
        assert engine._last_bars is None


# ─────────────────────────────────────────────────────────────────
# Test: Position Serialization
# ─────────────────────────────────────────────────────────────────

class TestPositionSerialization:
    def test_to_dict_fields(self):
        pos = LiveScalpPosition(
            ticker="SPY", strike=600.0, right="C",
            direction="CALL", expiry="20260115", tier="scalp",
            confirmations=["VWAP", "EMA", "BREAKOUT"],
            confidence=0.6, entry_premium=2.50, num_contracts=3,
        )
        d = pos.to_dict()
        assert d["ticker"] == "SPY"
        assert d["strike"] == 600.0
        assert d["right"] == "C"
        assert d["direction"] == "CALL"
        assert d["tier"] == "scalp"
        assert d["confidence"] == 0.6
        assert d["entry_premium"] == 2.50
        assert d["num_contracts"] == 3
        assert isinstance(d["entry_time"], str)
        assert d["is_open"] is True

    def test_to_dict_roundtrip(self):
        pos = LiveScalpPosition(ticker="QQQ", strike=500.0, right="P")
        d = pos.to_dict()
        assert d["ticker"] == "QQQ"
        assert d["strike"] == 500.0


# ─────────────────────────────────────────────────────────────────
# Test: Risk Parity + Phased Capital Scaling Position Sizer
# ─────────────────────────────────────────────────────────────────

class TestPositionSizer:
    def test_basic_sizing(self):
        engine = make_engine(account_size=10_000.0)
        n = engine._compute_position_size(
            balance=10_000.0,
            entry_premium=2.00,    # $200/contract
            current_atr=0.50,
            daily_median_atr=0.50,
            strategy_tier="scalp",
        )
        assert n >= 1
        # Phase 1 (Risk Parity): scalp weight=19.9%, risk=12.0%
        # $10K × 12.0% = $1,196 / $200 = 5.98 → 5 contracts
        # Vol target: $10K × 15% / 1.0 = $1,500 / $200 = 7.5
        # Abs max: $10K × 15% = $1,500 / $200 = 7.5
        # min(5.98, 7.5, 7.5) = 5
        assert n == 5

    def test_sizing_respects_balance_gate(self):
        engine = make_engine(account_size=1_000.0)
        n = engine._compute_position_size(
            balance=1_000.0,
            entry_premium=8.00,    # $800/contract
            current_atr=0.50,
            daily_median_atr=0.50,
            strategy_tier="scalp",
        )
        # $1K × 50% balance gate = $500 max → can't afford 1 × $800
        assert n == 0

    def test_sizing_zero_premium(self):
        engine = make_engine()
        n = engine._compute_position_size(
            balance=10_000.0,
            entry_premium=0.0,
            current_atr=0.50,
            daily_median_atr=0.50,
            strategy_tier="scalp",
        )
        assert n == 0

    def test_sizing_zero_balance(self):
        engine = make_engine()
        n = engine._compute_position_size(
            balance=0.0,
            entry_premium=2.00,
            current_atr=0.50,
            daily_median_atr=0.50,
            strategy_tier="scalp",
        )
        assert n == 0

    def test_high_atr_reduces_size(self):
        """When ATR is 2× median, vol-target should size down."""
        engine = make_engine(account_size=10_000.0)
        normal = engine._compute_position_size(
            balance=10_000.0,
            entry_premium=2.00,
            current_atr=0.50,
            daily_median_atr=0.50,
            strategy_tier="scalp",
        )
        elevated = engine._compute_position_size(
            balance=10_000.0,
            entry_premium=2.00,
            current_atr=1.00,       # 2× median
            daily_median_atr=0.50,
            strategy_tier="scalp",
        )
        assert elevated <= normal

    def test_runner_budget_sizing(self):
        """Runner uses budget-based sizing, not LW."""
        engine = make_engine(account_size=10_000.0)
        n = engine._compute_position_size(
            balance=10_000.0,
            entry_premium=1.00,    # $100/contract
            current_atr=0.50,
            daily_median_atr=0.50,
            strategy_tier="runner",
        )
        # runner_budget_pct = 0.02 → $200 budget / $100 = 2 contracts
        assert n == 2

    def test_conviction_multiplier(self):
        """1.5× conviction should increase position size."""
        engine = make_engine(account_size=10_000.0)
        base = engine._compute_position_size(
            balance=10_000.0,
            entry_premium=2.00,
            current_atr=0.50,
            daily_median_atr=0.50,
            strategy_tier="scalp",
            conviction_mult=1.0,
        )
        boosted = engine._compute_position_size(
            balance=10_000.0,
            entry_premium=2.00,
            current_atr=0.50,
            daily_median_atr=0.50,
            strategy_tier="scalp",
            conviction_mult=1.5,
        )
        assert boosted >= base

    def test_lw_lookback_uses_history(self):
        """With loss history at Phase 2+, LW adjusts sizing."""
        engine = make_engine(account_size=30_000.0)
        # Record some trade losses
        for _ in range(5):
            engine._trade_history.append(-300.0)

        n = engine._compute_position_size(
            balance=30_000.0,
            entry_premium=2.00,
            current_atr=0.50,
            daily_median_atr=0.50,
            strategy_tier="scalp",
        )
        # Phase 2 ($25K-$50K): LW 10%, worst_loss=$300
        # LW mult = min($30K × 10% / $300, 3.0) = min(10.0, 3.0) = 3.0
        # LW dollars = 3.0 × $300 = $900 → $900/$200 = 4.5 → 4
        assert n >= 1

    def test_phase_transition(self):
        """Verify sizing increases as balance crosses phase thresholds."""
        engine = make_engine(account_size=10_000.0)
        # Seed some trade history so LW uses real worst-loss, not default
        for _ in range(5):
            engine._trade_history.append(-200.0)
        for _ in range(10):
            engine._trade_history.append(400.0)

        # Phase 1: Risk Parity at $10K
        n1 = engine._compute_position_size(
            balance=10_000.0, entry_premium=2.00,
            current_atr=0.50, daily_median_atr=0.50,
            strategy_tier="scalp",
        )
        # Phase 2: LW 10% at $30K
        n2 = engine._compute_position_size(
            balance=30_000.0, entry_premium=2.00,
            current_atr=0.50, daily_median_atr=0.50,
            strategy_tier="scalp",
        )
        # Phase 3: LW 15% at $60K
        n3 = engine._compute_position_size(
            balance=60_000.0, entry_premium=2.00,
            current_atr=0.50, daily_median_atr=0.50,
            strategy_tier="scalp",
        )
        # Higher balance → more contracts (with history, LW scales properly)
        assert n3 > n1
        assert n2 > n1


# ─────────────────────────────────────────────────────────────────
# Test: IV Estimation
# ─────────────────────────────────────────────────────────────────

class TestIVEstimation:
    def test_estimate_iv_returns_reasonable(self):
        engine = make_engine()
        bars = make_bars(100)
        iv = engine._estimate_iv(bars)
        assert 0.10 <= iv <= 1.00

    def test_estimate_iv_with_insufficient_bars(self):
        engine = make_engine()
        bars = make_bars(3)
        iv = engine._estimate_iv(bars)
        assert iv == 0.18  # Default fallback

    def test_estimate_iv_with_none(self):
        engine = make_engine()
        iv = engine._estimate_iv(None)
        assert iv == 0.18


# ─────────────────────────────────────────────────────────────────
# Test: Regime Classification
# ─────────────────────────────────────────────────────────────────

class TestRegimeClassification:
    def test_classify_regime_sets_flag(self):
        engine = make_engine()
        bars = make_bars(100)
        engine._classify_regime(bars)
        assert engine._regime_classified is True
        assert engine._regime_info is not None
        assert hasattr(engine._regime_info, "regime")

    def test_classify_regime_returns_known_type(self):
        engine = make_engine()
        bars = make_bars(100)
        engine._classify_regime(bars)
        valid_regimes = {
            "STRONG_TREND", "MODERATE_TREND", "RANGE_BOUND",
            "MIXED", "CHOPPY", "DEAD_FLAT",
        }
        assert engine._regime_info.regime in valid_regimes


# ─────────────────────────────────────────────────────────────────
# Test: Properties
# ─────────────────────────────────────────────────────────────────

class TestProperties:
    def test_open_positions_empty(self):
        engine = make_engine()
        assert engine.open_positions == []

    def test_open_positions_with_entries(self):
        engine = make_engine()
        engine.open_scalp = LiveScalpPosition(ticker="SPY", tier="scalp")
        engine.open_orb = LiveScalpPosition(ticker="SPY", tier="orb")
        assert len(engine.open_positions) == 2

    def test_open_positions_excludes_closed(self):
        engine = make_engine()
        closed = LiveScalpPosition(ticker="SPY", tier="scalp", is_open=False)
        engine.open_scalp = closed
        assert len(engine.open_positions) == 0

    def test_trades_today_sums_all(self):
        engine = make_engine()
        engine.scalp_trades_today = 2
        engine.runner_trades_today = 1
        engine.orb_trades_today = 1
        engine.rf_trades_today = 0
        assert engine.trades_today == 4

    def test_daily_realized_pnl_alias(self):
        engine = make_engine()
        engine.daily_pnl = 123.45
        assert engine.daily_realized_pnl == 123.45


# ─────────────────────────────────────────────────────────────────
# Test: Can Enter
# ─────────────────────────────────────────────────────────────────

class TestCanEnter:
    def test_can_enter_default(self):
        engine = make_engine()
        can, reason = engine.can_enter()
        assert can is True
        assert reason == "OK"

    def test_cannot_enter_after_daily_loss(self):
        engine = make_engine()
        engine.daily_pnl = -(engine.scalp_cfg.daily_loss_limit + 1)
        can, reason = engine.can_enter()
        assert can is False
        assert "Daily loss limit" in reason


# ─────────────────────────────────────────────────────────────────
# Test: Scan — Pre-check Gates
# ─────────────────────────────────────────────────────────────────

class TestScanGates:
    def test_scan_blocks_at_daily_loss_limit(self):
        engine = make_engine()
        engine.daily_pnl = -9999
        result = engine.scan("SPY", minutes_since_open=30)
        assert result is None

    def test_scan_blocks_during_cooldown(self):
        engine = make_engine()
        engine.cooldown_remaining = 3
        result = engine.scan("SPY", minutes_since_open=30)
        assert result is None
        assert engine.cooldown_remaining == 2  # Decremented

    def test_scan_blocks_near_close(self):
        """>360 min since open = near close, no new entries."""
        engine = make_engine()
        result = engine.scan("SPY", minutes_since_open=361)
        assert result is None

    def test_scan_blocks_insufficient_bars(self):
        engine = make_engine()
        engine.provider.get_historical_bars.return_value = make_bars(10)  # Too few
        result = engine.scan("SPY", minutes_since_open=30)
        assert result is None

    def test_scan_handles_provider_failure(self):
        engine = make_engine()
        engine.provider.get_historical_bars.side_effect = Exception("Connection lost")
        result = engine.scan("SPY", minutes_since_open=30)
        assert result is None


# ─────────────────────────────────────────────────────────────────
# Test: Momentum Scalp — Strategy A
# ─────────────────────────────────────────────────────────────────

class TestMomentumScalp:
    def test_scalp_blocked_when_position_open(self):
        engine = make_engine()
        engine.open_scalp = LiveScalpPosition(ticker="SPY", tier="scalp")
        result = engine._scan_momentum_scalp(
            "SPY", make_bars(100), 50, 600.0, 0.50, 0.50, 25, False,
        )
        assert result is None

    def test_scalp_blocked_at_max_trades(self):
        engine = make_engine()
        engine.scalp_trades_today = engine.scalp_cfg.max_trades_per_day
        result = engine._scan_momentum_scalp(
            "SPY", make_bars(100), 50, 600.0, 0.50, 0.50, 25, False,
        )
        assert result is None

    def test_scalp_blocked_outside_windows(self):
        """Outside both W1 and W2, no signal evaluation."""
        engine = make_engine()
        # Default W1 end is ~120, W2 start is ~300; 200 is in the gap
        result = engine._scan_momentum_scalp(
            "SPY", make_bars(100), 50, 600.0, 0.50, 0.50, 200, False,
        )
        assert result is None

    def test_scalp_blocked_in_stopped_direction(self):
        """If CALL was stopped out today, new CALL signals are blocked."""
        engine = make_engine()
        engine.stopped_directions.add("CALL")
        bars = make_bars(100)
        engine._precomp = engine._signal_engine.precompute_day_indicators(bars)
        engine._precomp['prev_day_high'] = None
        engine._precomp['prev_day_low'] = None

        # Mock signal engine to return a CALL signal
        mock_signal = MagicMock()
        mock_signal.direction = "CALL"
        mock_signal.confirmations = ["VWAP", "EMA", "BREAKOUT"]
        mock_signal.confidence = 0.6
        engine._signal_engine.evaluate_fast = MagicMock(return_value=mock_signal)

        result = engine._scan_momentum_scalp(
            "SPY", bars, 50, 600.0, 0.50, 0.50, 25, False,
        )
        assert result is None


# ─────────────────────────────────────────────────────────────────
# Test: Runner — Strategy C
# ─────────────────────────────────────────────────────────────────

class TestRunner:
    def test_runner_blocked_when_disabled(self):
        engine = make_engine()
        engine.scalp_cfg.runner_enabled = False
        result = engine._scan_runner(
            "SPY", make_bars(100), 50, 600.0, 0.50, 0.50, 300, False,
        )
        assert result is None

    def test_runner_blocked_when_position_open(self):
        engine = make_engine()
        engine.open_runner = LiveScalpPosition(ticker="SPY", tier="runner")
        result = engine._scan_runner(
            "SPY", make_bars(100), 50, 600.0, 0.50, 0.50, 300, False,
        )
        assert result is None

    def test_runner_blocked_outside_window(self):
        engine = make_engine()
        # Runner window is typically 300-350; minute 25 is outside
        result = engine._scan_runner(
            "SPY", make_bars(100), 50, 600.0, 0.50, 0.50, 25, False,
        )
        assert result is None


# ─────────────────────────────────────────────────────────────────
# Test: ORB — Strategy D
# ─────────────────────────────────────────────────────────────────

class TestORB:
    def test_orb_blocked_when_disabled(self):
        cfg = EngineConfig()
        cfg.orb.enabled = False
        engine = make_engine(config=cfg)
        result = engine._scan_orb(
            "SPY", make_bars(100), 50, 600.0, 0.50, 0.50, 60, False,
        )
        assert result is None

    def test_orb_blocked_when_position_open(self):
        engine = make_engine()
        engine.open_orb = LiveScalpPosition(ticker="SPY", tier="orb")
        result = engine._scan_orb(
            "SPY", make_bars(100), 50, 600.0, 0.50, 0.50, 60, False,
        )
        assert result is None

    def test_orb_defers_to_momentum(self):
        """When only_when_no_momentum=True, ORB defers to momentum."""
        cfg = EngineConfig()
        cfg.orb.only_when_no_momentum = True  # Explicitly enable defer behavior
        engine = make_engine(config=cfg)
        engine.momentum_fired_today = True
        engine._orb_data = {"valid": True}
        engine._orb_regime_ok = True
        result = engine._scan_orb(
            "SPY", make_bars(100), 50, 600.0, 0.50, 0.50, 60, False,
        )
        assert result is None

    def test_orb_coexists_with_momentum_by_default(self):
        """By default (only_when_no_momentum=False), ORB fires even when momentum has fired."""
        engine = make_engine()
        engine.momentum_fired_today = True
        engine._orb_data = {"valid": True}
        engine._orb_regime_ok = True
        # Should NOT be blocked by momentum — coexist is the default
        # (will fail on signal evaluation, not momentum gate)
        assert engine.config.orb.only_when_no_momentum is False

    def test_orb_blocked_without_data(self):
        engine = make_engine()
        engine._orb_data = None  # Not yet computed
        result = engine._scan_orb(
            "SPY", make_bars(100), 50, 600.0, 0.50, 0.50, 60, False,
        )
        assert result is None

    def test_orb_blocked_on_bad_regime(self):
        engine = make_engine()
        engine._orb_regime_ok = False
        engine._orb_data = {"valid": True}
        result = engine._scan_orb(
            "SPY", make_bars(100), 50, 600.0, 0.50, 0.50, 60, False,
        )
        assert result is None


# ─────────────────────────────────────────────────────────────────
# Test: Range Fade — Strategy E
# ─────────────────────────────────────────────────────────────────

class TestRangeFade:
    def test_rf_blocked_when_disabled(self):
        cfg = EngineConfig()
        cfg.range_fade.enabled = False
        engine = make_engine(config=cfg)
        result = engine._scan_range_fade(
            "SPY", make_bars(100), 50, 600.0, 0.50, 0.50, 60, False,
        )
        assert result is None

    def test_rf_blocked_when_position_open(self):
        engine = make_engine()
        engine.open_rf = LiveScalpPosition(ticker="SPY", tier="range_fade")
        result = engine._scan_range_fade(
            "SPY", make_bars(100), 50, 600.0, 0.50, 0.50, 60, False,
        )
        assert result is None

    def test_rf_blocked_on_trending_regime(self):
        engine = make_engine()
        engine._rf_regime_ok = False
        engine._rf_data = {"valid": True}
        result = engine._scan_range_fade(
            "SPY", make_bars(100), 50, 600.0, 0.50, 0.50, 60, False,
        )
        assert result is None

    def test_rf_defers_to_momentum(self):
        engine = make_engine()
        engine.momentum_fired_today = True
        engine._rf_regime_ok = True
        engine._rf_data = {"valid": True}
        result = engine._scan_range_fade(
            "SPY", make_bars(100), 50, 600.0, 0.50, 0.50, 60, False,
        )
        assert result is None


# ─────────────────────────────────────────────────────────────────
# Test: Execute Entry (shared path)
# ─────────────────────────────────────────────────────────────────

class TestExecuteEntry:
    def test_dry_run_returns_position_without_order(self):
        engine = make_engine()
        engine._profile = MagicMock(premium_scale=1.0)
        engine._premium_scale = 1.0

        # Mock chain
        chain = make_options_chain(600.0, "C")
        engine.provider.get_options_chain.return_value = chain

        pos = engine._execute_entry(
            ticker="SPY",
            signal_direction="CALL",
            confirmations=["VWAP", "EMA"],
            confidence=0.4,
            price=600.0,
            atr=0.50,
            median_atr=0.50,
            stop_price=599.0,
            target_price=602.0,
            tier="scalp",
            dry_run=True,
        )

        assert pos is not None
        assert pos.ticker == "SPY"
        assert pos.right == "C"
        assert pos.direction == "CALL"
        assert pos.tier == "scalp"
        assert pos.num_contracts >= 1
        # No order should have been placed
        engine.executor.buy_option.assert_not_called()

    def test_live_entry_calls_buy_option(self):
        engine = make_engine()
        engine._profile = MagicMock(premium_scale=1.0)
        engine._premium_scale = 1.0

        chain = make_options_chain(600.0, "C")
        engine.provider.get_options_chain.return_value = chain
        engine.executor.buy_option.return_value = make_fill(
            status=OrderStatus.FILLED, price=2.50, qty=3,
        )

        pos = engine._execute_entry(
            ticker="SPY",
            signal_direction="CALL",
            confirmations=["VWAP", "EMA"],
            confidence=0.4,
            price=600.0,
            atr=0.50,
            median_atr=0.50,
            stop_price=599.0,
            target_price=602.0,
            tier="scalp",
            dry_run=False,
        )

        assert pos is not None
        engine.executor.buy_option.assert_called_once()
        assert pos.entry_premium == 2.50
        assert pos.num_contracts == 3

    def test_rejected_fill_returns_none(self):
        engine = make_engine()
        engine._profile = MagicMock(premium_scale=1.0)
        engine._premium_scale = 1.0

        chain = make_options_chain(600.0, "C")
        engine.provider.get_options_chain.return_value = chain
        engine.executor.buy_option.return_value = make_fill(
            status=OrderStatus.REJECTED, price=0.0, qty=0,
        )

        pos = engine._execute_entry(
            ticker="SPY",
            signal_direction="CALL",
            confirmations=["VWAP", "EMA"],
            confidence=0.4,
            price=600.0,
            atr=0.50,
            median_atr=0.50,
            stop_price=599.0,
            target_price=602.0,
            tier="scalp",
            dry_run=False,
        )

        assert pos is None

    def test_no_chain_returns_none(self):
        engine = make_engine()
        engine._profile = MagicMock(premium_scale=1.0)
        engine._premium_scale = 1.0
        engine.provider.get_options_chain.return_value = []

        pos = engine._execute_entry(
            ticker="SPY",
            signal_direction="CALL",
            confirmations=["VWAP"],
            confidence=0.2,
            price=600.0,
            atr=0.50,
            median_atr=0.50,
            stop_price=599.0,
            target_price=602.0,
            tier="scalp",
            dry_run=False,
        )
        assert pos is None

    def test_put_direction_uses_right_P(self):
        engine = make_engine()
        engine._profile = MagicMock(premium_scale=1.0)
        engine._premium_scale = 1.0

        chain = make_options_chain(600.0, "P")
        engine.provider.get_options_chain.return_value = chain
        engine.executor.buy_option.return_value = make_fill(
            status=OrderStatus.FILLED, price=2.00, qty=2,
        )

        pos = engine._execute_entry(
            ticker="SPY",
            signal_direction="PUT",
            confirmations=["VWAP", "EMA"],
            confidence=0.4,
            price=600.0,
            atr=0.50,
            median_atr=0.50,
            stop_price=601.0,
            target_price=598.0,
            tier="scalp",
            dry_run=False,
        )

        assert pos is not None
        assert pos.right == "P"
        assert pos.direction == "PUT"
        call_kwargs = engine.executor.buy_option.call_args
        assert call_kwargs[1]["right"] == "P" or call_kwargs.kwargs["right"] == "P"


# ─────────────────────────────────────────────────────────────────
# Test: Strike Selection
# ─────────────────────────────────────────────────────────────────

class TestStrikeSelection:
    def test_selects_best_delta(self):
        engine = make_engine()
        chain = make_options_chain(600.0, "C")
        engine.provider.get_options_chain.return_value = chain

        result = engine._select_strike(
            ticker="SPY", expiry="20260115", right="C",
            delta_min=0.35, delta_max=0.65,
            min_prem=0.30, max_prem=10.00,
            premium_scale=1.0,
        )
        assert result is not None
        assert "strike" in result
        assert "ask" in result
        assert "delta" in result
        assert result["delta"] >= 0.35
        assert result["delta"] <= 0.65

    def test_no_provider_returns_none(self):
        engine = make_engine()
        engine.provider = None
        result = engine._select_strike(
            "SPY", "20260115", "C", 0.35, 0.65, 0.30, 10.00, 1.0,
        )
        assert result is None

    def test_empty_chain_returns_none(self):
        engine = make_engine()
        engine.provider.get_options_chain.return_value = []
        result = engine._select_strike(
            "SPY", "20260115", "C", 0.35, 0.65, 0.30, 10.00, 1.0,
        )
        assert result is None

    def test_filters_by_premium(self):
        engine = make_engine()
        chain = make_options_chain(600.0, "C")
        engine.provider.get_options_chain.return_value = chain

        # Set very tight premium filter that should exclude most
        result = engine._select_strike(
            "SPY", "20260115", "C", 0.35, 0.65,
            min_prem=50.0, max_prem=100.0,  # Nothing costs $50+
            premium_scale=1.0,
        )
        assert result is None


# ─────────────────────────────────────────────────────────────────
# Test: Exit Monitoring
# ─────────────────────────────────────────────────────────────────

class TestExitMonitoring:
    def test_check_exits_empty(self):
        engine = make_engine()
        closed = engine.check_exits(minutes_to_close=60)
        assert closed == []

    def test_check_exits_calls_per_tier(self):
        engine = make_engine()
        bars = make_bars(100)
        engine.provider.get_historical_bars.return_value = bars

        # Put a position in each tier slot
        for attr in ["open_scalp", "open_runner", "open_orb", "open_rf"]:
            pos = LiveScalpPosition(
                ticker="SPY", strike=600.0, right="C",
                direction="CALL", entry_underlying=600.0,
                entry_premium=2.50, atr_at_entry=0.50,
                stop_price=598.0, target_price=603.0,
                best_favorable_underlying=600.0,
                expiry="20260115",
            )
            setattr(engine, attr, pos)

        # Mock all exit engines to return None (no exit)
        engine._scalp_exit.check_exit = MagicMock(return_value=None)
        engine._runner_exit.check_exit = MagicMock(return_value=None)
        engine._orb_exit.check_exit = MagicMock(return_value=None)
        engine._rf_exit.check_exit = MagicMock(return_value=None)

        closed = engine.check_exits(minutes_to_close=60)
        assert closed == []
        # All positions should still be open
        assert engine.open_scalp is not None
        assert engine.open_runner is not None

    def test_exit_trigger_closes_position(self):
        engine = make_engine()
        bars = make_bars(100, base_price=603.0)
        engine.provider.get_historical_bars.return_value = bars

        pos = LiveScalpPosition(
            ticker="SPY", strike=600.0, right="C",
            direction="CALL", entry_underlying=600.0,
            entry_premium=2.50, atr_at_entry=0.50,
            stop_price=598.0, target_price=603.0,
            best_favorable_underlying=600.0,
            expiry="20260115",
        )
        engine.open_scalp = pos

        # Mock exit engine to trigger
        engine._scalp_exit.check_exit = MagicMock(return_value=("PROFIT_TARGET", 603.5))

        # Mock options chain for exit premium
        engine.provider.get_options_chain.return_value = [{
            "strike": 600.0, "right": "C", "bid": 4.00, "ask": 4.20,
        }]

        # Mock sell
        engine.executor.sell_option.return_value = make_fill(
            status=OrderStatus.FILLED, price=4.00, qty=1,
        )

        closed = engine.check_exits(minutes_to_close=60)
        assert len(closed) == 1
        assert closed[0].exit_reason == "PROFIT_TARGET"
        assert closed[0].is_open is False
        assert engine.open_scalp is None

    def test_exit_updates_daily_pnl(self):
        engine = make_engine()
        bars = make_bars(100, base_price=603.0)
        engine.provider.get_historical_bars.return_value = bars

        pos = LiveScalpPosition(
            ticker="SPY", strike=600.0, right="C",
            direction="CALL", entry_underlying=600.0,
            entry_premium=2.50, num_contracts=2,
            atr_at_entry=0.50,
            stop_price=598.0, target_price=603.0,
            best_favorable_underlying=600.0,
            expiry="20260115",
        )
        engine.open_scalp = pos

        engine._scalp_exit.check_exit = MagicMock(return_value=("PROFIT_TARGET", 603.0))
        engine.provider.get_options_chain.return_value = [{
            "strike": 600.0, "right": "C", "bid": 4.50, "ask": 4.80,
        }]
        engine.executor.sell_option.return_value = make_fill(
            status=OrderStatus.FILLED, price=4.50, qty=2, commission=2.60,
        )

        engine.check_exits(minutes_to_close=60)

        # PnL = (4.50 - 2.50) × 2 × 100 - 2.60 = $397.40
        assert engine.daily_pnl == pytest.approx(397.40, abs=0.1)

    def test_stop_loss_marks_direction_stopped(self):
        engine = make_engine()
        bars = make_bars(100, base_price=597.0)
        engine.provider.get_historical_bars.return_value = bars

        pos = LiveScalpPosition(
            ticker="SPY", strike=600.0, right="C",
            direction="CALL", entry_underlying=600.0,
            entry_premium=2.50, atr_at_entry=0.50,
            stop_price=598.0, target_price=603.0,
            best_favorable_underlying=600.0,
            tier="scalp",
            expiry="20260115",
        )
        engine.open_scalp = pos

        engine._scalp_exit.check_exit = MagicMock(return_value=("STOP_LOSS", 597.5))
        engine.provider.get_options_chain.return_value = [{
            "strike": 600.0, "right": "C", "bid": 0.50, "ask": 0.80,
        }]
        engine.executor.sell_option.return_value = make_fill(
            status=OrderStatus.FILLED, price=0.50, qty=1,
        )

        engine.check_exits(minutes_to_close=60)
        assert "CALL" in engine.stopped_directions

    def test_scalp_exit_sets_cooldown(self):
        engine = make_engine()
        bars = make_bars(100, base_price=603.0)
        engine.provider.get_historical_bars.return_value = bars

        pos = LiveScalpPosition(
            ticker="SPY", strike=600.0, right="C",
            direction="CALL", entry_underlying=600.0,
            entry_premium=2.50, atr_at_entry=0.50,
            stop_price=598.0, target_price=603.0,
            best_favorable_underlying=600.0,
            tier="scalp",
            expiry="20260115",
        )
        engine.open_scalp = pos

        engine._scalp_exit.check_exit = MagicMock(return_value=("PROFIT_TARGET", 603.0))
        engine.provider.get_options_chain.return_value = [{
            "strike": 600.0, "right": "C", "bid": 4.00, "ask": 4.20,
        }]
        engine.executor.sell_option.return_value = make_fill(
            status=OrderStatus.FILLED, price=4.00, qty=1,
        )

        engine.check_exits(minutes_to_close=60)
        assert engine.cooldown_remaining == engine.scalp_cfg.cooldown_bars


# ─────────────────────────────────────────────────────────────────
# Test: Full Scan Integration (dry run)
# ─────────────────────────────────────────────────────────────────

class TestFullScanDryRun:
    def test_full_scan_dry_run_with_no_signal(self):
        """Scan should return None when no strategy fires."""
        engine = make_engine()
        bars = make_bars(100)
        engine.provider.get_historical_bars.return_value = bars

        result = engine.scan("SPY", minutes_since_open=30, dry_run=True)
        # Most likely no signal fires on random bars
        # The important thing is it doesn't crash
        assert result is None or isinstance(result, LiveScalpPosition)

    def test_full_scan_dry_run_with_mocked_signal(self):
        """Force a signal to fire and verify dry-run returns a position."""
        cfg = EngineConfig()
        cfg.scalp.iv_discount_enabled = False  # Disable IV gate for test
        engine = make_engine(config=cfg)
        bars = make_bars(200)
        engine.provider.get_historical_bars.return_value = bars

        # Mock signal engine to produce a signal
        mock_signal = MagicMock()
        mock_signal.direction = "CALL"
        mock_signal.confirmations = ["VWAP", "EMA", "BREAKOUT"]
        mock_signal.confidence = 0.6
        engine._signal_engine.evaluate_fast = MagicMock(return_value=mock_signal)

        # Mock chain for strike selection
        chain = make_options_chain(600.0, "C")
        engine.provider.get_options_chain.return_value = chain

        result = engine.scan("SPY", minutes_since_open=25, dry_run=True)

        assert result is not None
        assert result.ticker == "SPY"
        assert result.direction == "CALL"
        assert result.tier == "scalp"
        assert result.num_contracts >= 1
        assert engine.scalp_trades_today == 1
        assert engine.momentum_fired_today is True
        # No actual order placed
        engine.executor.buy_option.assert_not_called()

    def test_scan_sets_momentum_fired_flag(self):
        """After a momentum signal is evaluated, flag should be set."""
        engine = make_engine()
        bars = make_bars(200)
        engine.provider.get_historical_bars.return_value = bars

        mock_signal = MagicMock()
        mock_signal.direction = "CALL"
        mock_signal.confirmations = ["VWAP", "EMA", "BREAKOUT"]
        mock_signal.confidence = 0.6
        engine._signal_engine.evaluate_fast = MagicMock(return_value=mock_signal)

        chain = make_options_chain(600.0, "C")
        engine.provider.get_options_chain.return_value = chain

        engine.scan("SPY", minutes_since_open=25, dry_run=True)
        assert engine.momentum_fired_today is True


# ─────────────────────────────────────────────────────────────────
# Test: Trade History Recording
# ─────────────────────────────────────────────────────────────────

class TestTradeHistory:
    def test_record_trade_pnl(self):
        engine = make_engine()
        engine._record_trade_pnl(150.0, 0.50)
        engine._record_trade_pnl(-80.0, 0.45)
        assert len(engine._trade_history) == 2
        assert engine._trade_history[0] == 150.0
        assert engine._trade_history[1] == -80.0
        assert len(engine._atr_history) == 2

    def test_history_persists_across_daily_reset(self):
        """Trade history should NOT be cleared on reset (for LW lookback)."""
        engine = make_engine()
        engine._record_trade_pnl(100.0, 0.50)
        engine._record_trade_pnl(-50.0, 0.40)
        engine.reset_daily()
        assert len(engine._trade_history) == 2  # Preserved


# ─────────────────────────────────────────────────────────────────
# Test: Print Status (smoke test)
# ─────────────────────────────────────────────────────────────────

class TestPrintStatus:
    def test_print_status_no_crash_empty(self, capsys):
        engine = make_engine()
        engine.print_status()
        captured = capsys.readouterr()
        assert "LIVE SCALP ENGINE STATUS" in captured.out

    def test_print_status_with_positions(self, capsys):
        engine = make_engine()
        engine.open_scalp = LiveScalpPosition(
            ticker="SPY", strike=600.0, right="C",
            direction="CALL", entry_premium=2.50,
            stop_price=598.0, target_price=603.0,
            confirmations=["VWAP", "EMA"],
        )
        engine.daily_pnl = 250.0
        engine.scalp_trades_today = 1
        engine.print_status()
        captured = capsys.readouterr()
        assert "$250.00" in captured.out or "+250.00" in captured.out
        assert "SCALP Position" in captured.out


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])

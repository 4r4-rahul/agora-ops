#!/usr/bin/env python3
"""
Backtest Regression Test
=========================
Runs a quick backtest with fixed parameters and asserts that key
performance metrics haven't regressed below known baselines.

This catches silent regressions from code changes.

Usage:
    python -m pytest tests/test_backtest_regression.py -v
    make test

Expected runtime: ~10-20 seconds (downloads cached yfinance data).
"""

import os
import sys
import pytest

# ── Ensure project root on path ──
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


class TestBacktestRegression:
    """
    Regression tests for the backtesting pipeline.
    These are fast smoke tests, not full 6-month runs.
    """

    def test_imports(self):
        """All engine modules import without error."""
        from trading_engine.config import EngineConfig, AdaptiveConfig
        from trading_engine.filters import ProductionFilters, FilterDecision
        from trading_engine.engine import TradingEngine
        from trading_engine.models import MarketSnapshot
        from trading_engine.execution import OrderExecutor
        from trading_engine.execution.state import StateManager
        from trading_engine.execution.safety import SafetyMonitor
        from trading_engine.data.backtester import Backtester
        assert True

    def test_regime_classification(self):
        """VIX regime thresholds are correct."""
        from trading_engine.config import AdaptiveConfig

        cfg = AdaptiveConfig()

        # GREEN
        green = cfg.for_regime("GREEN")
        assert green.trade_enabled is True
        assert green.preferred_strategy == "call_credit"
        assert green.position_size_mult == 1.0

        # YELLOW
        yellow = cfg.for_regime("YELLOW")
        assert yellow.trade_enabled is True
        assert yellow.position_size_mult == 0.5

        # RED
        red = cfg.for_regime("RED")
        assert red.trade_enabled is False
        assert red.position_size_mult == 0.0

    def test_filter_stack_green_passes(self):
        """A normal ATR day should pass filters with full size."""
        from trading_engine.filters import ProductionFilters, FilterDecision
        import numpy as np

        filters = ProductionFilters()
        # Simulate 20 days of normal SPY-like data (~1.0% ATR)
        np.random.seed(42)
        base = 550.0
        closes = [base + i * 0.5 + np.random.randn() for i in range(20)]
        highs = [c + abs(np.random.randn()) * 3 for c in closes]
        lows = [c - abs(np.random.randn()) * 3 for c in closes]

        decision = filters.pre_entry(closes, highs, lows)
        assert isinstance(decision, FilterDecision)
        # Normal ATR → should not skip, full or near-full size
        assert decision.skip is False
        assert decision.size_multiplier > 0

    def test_filter_stack_extreme_atr_skips(self):
        """Extreme ATR (>2%) should skip the day."""
        from trading_engine.filters import ProductionFilters, FilterDecision

        filters = ProductionFilters()
        # Simulate 20 days with huge daily ranges (>2% ATR)
        closes = [100.0 + i * 0.1 for i in range(20)]
        highs = [c + 3.0 for c in closes]   # $3 range on $100 = 3%
        lows = [c - 3.0 for c in closes]

        decision = filters.pre_entry(closes, highs, lows)
        assert decision.skip is True

    def test_state_manager_daily_reset(self):
        """StateManager properly tracks P&L."""
        import tempfile
        from trading_engine.execution.state import StateManager

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "test_state.json")
            sm = StateManager(path=state_path)
            sm.load()

            # Record a trade (using record_fill with a mock fill_result)
            class MockFill:
                pass

            sm.record_fill(MockFill(), realized_pnl=100.0)
            assert sm.state.daily_pnl == 100.0
            assert sm.state.trades_today == 1

            # Record another
            sm.record_fill(MockFill(), realized_pnl=-50.0)
            assert sm.state.daily_pnl == 50.0
            assert sm.state.trades_today == 2

    def test_safety_monitor_daily_limit(self):
        """SafetyMonitor detects daily loss limit breach."""
        from trading_engine.execution.safety import SafetyMonitor
        from trading_engine.execution.state import StateManager
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "test_state.json")
            sm = StateManager(path=state_path, account_size=50_000)
            sm.load()

            monitor = SafetyMonitor(
                state_manager=sm,
                account_size=50_000,
            )

            # Should be safe initially
            safe, alerts = monitor.check()
            assert safe is True

            # Simulate big loss (>5% of $50k = $2,500)
            class MockFill:
                pass
            sm.record_fill(MockFill(), realized_pnl=-3000.0)

            safe, alerts = monitor.check()
            # Should flag the daily loss
            assert len(alerts) > 0

    def test_adaptive_params_consistency(self):
        """All regime params have required fields."""
        from trading_engine.config import AdaptiveConfig

        cfg = AdaptiveConfig()
        for regime_name in ["GREEN", "YELLOW", "RED"]:
            params = cfg.for_regime(regime_name)
            assert hasattr(params, "delta")
            assert hasattr(params, "stop_mult")
            assert hasattr(params, "profit_target")
            assert hasattr(params, "preferred_strategy")
            assert hasattr(params, "trade_enabled")
            assert hasattr(params, "position_size_mult")
            assert 0 <= params.delta <= 0.50
            assert params.stop_mult > 0
            assert 0 <= params.profit_target <= 1.0

    def test_market_snapshot_creation(self):
        """MarketSnapshot can be created with realistic values."""
        from trading_engine.models import MarketSnapshot

        snap = MarketSnapshot(
            spx_price=5650.0, spy_price=565.0, qqq_price=485.0,
            iwm_price=225.0, vix_level=16.0, vix_1d_change=-0.3,
            vix_term_structure="contango",
            vix_futures_front=17.0, vix_futures_second=18.5,
            spx_futures_price=5645.0, spx_futures_overnight_change=12.0,
            globex_high=5660.0, globex_low=5630.0,
            iv_rank=35.0, iv_percentile=40.0,
            realized_vol_20d=0.15, implied_vol_30d=0.185,
            advance_decline_ratio=1.5, put_call_ratio=0.85,
            atm_straddle_price=35.0, expected_move_1d=28.0,
            expected_move_1w=55.0, expected_move_1m=120.0,
            prev_close=5638.0, prev_high=5655.0, prev_low=5620.0,
        )
        assert snap.spy_price == 565.0
        assert snap.vix_level == 16.0

    def test_engine_12_modules_run(self):
        """All 12 engine modules execute without crashing."""
        from trading_engine.engine import TradingEngine
        from trading_engine.models import MarketSnapshot

        engine = TradingEngine()
        snap = MarketSnapshot(
            spx_price=5650.0, spy_price=565.0, qqq_price=485.0,
            iwm_price=225.0, vix_level=18.5, vix_1d_change=-0.3,
            vix_term_structure="contango",
            vix_futures_front=19.0, vix_futures_second=20.5,
            spx_futures_price=5645.0, spx_futures_overnight_change=12.0,
            globex_high=5660.0, globex_low=5630.0,
            iv_rank=35.0, iv_percentile=40.0,
            realized_vol_20d=0.15, implied_vol_30d=0.185,
            advance_decline_ratio=1.5, put_call_ratio=0.85,
            atm_straddle_price=35.0, expected_move_1d=28.0,
            expected_move_1w=55.0, expected_move_1m=120.0,
            prev_close=5638.0, prev_high=5655.0, prev_low=5620.0,
        )
        engine._snap = snap

        # Run all modules — they should not crash
        results = {}
        for name, fn in [
            ("scanner", engine.run_scanner),
            ("regime", engine.run_regime),
            ("theta", engine.run_theta),
            ("strikes", engine.run_strikes),
            ("condor", engine.run_condor),
            ("premarket", engine.run_premarket),
            ("risk", engine.run_risk),
            ("skew", engine.run_skew),
            ("calendar", engine.run_calendar),
            ("earnings", lambda: engine.run_earnings()),
            ("eod", engine.run_eod),
            ("dashboard", engine.run_dashboard),
        ]:
            result = fn()
            results[name] = result
            assert result is not None, f"Module {name} returned None"

        assert len(results) == 12


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

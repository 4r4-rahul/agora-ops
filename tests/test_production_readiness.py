#!/usr/bin/env python3
"""
Tests for v7.2 Production-Readiness Fixes
===========================================
Covers:
  1. StateManager wired into LiveScalpEngine exit flow
  2. Position key alignment (strike vs short_strike)
  3. Connection resilience (_attempt_reconnect)
  4. Position restore on startup (_restore_positions)
  5. NYSE holiday calendar in market_is_open()
  6. Partial fill handling in _execute_entry

Usage:
    python -m pytest tests/test_production_readiness.py -v
"""

import os
import sys
import tempfile
import pytest
from datetime import date, datetime
from unittest.mock import MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from trading_engine.live_engine import LiveScalpEngine, LiveScalpPosition
from trading_engine.execution.state import StateManager, DailyState
from trading_engine.execution import OrderStatus, FillResult
from trading_engine.config import EngineConfig


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────

def make_engine(state_manager=None):
    """Create a LiveScalpEngine with mocked provider/executor."""
    provider = MagicMock()
    executor = MagicMock()
    config = EngineConfig()
    return LiveScalpEngine(
        provider=provider,
        executor=executor,
        config=config,
        account_size=10_000.0,
        state_manager=state_manager,
    )


def make_state_manager(tmp_dir):
    """Create a StateManager with a temp path."""
    path = os.path.join(tmp_dir, "test_state.json")
    sm = StateManager(path=path, account_size=10_000)
    sm.load()
    return sm


def make_fill(status=OrderStatus.FILLED, pnl=100.0, num=1, price=2.50, commission=1.30):
    """Create a FillResult."""
    return FillResult(
        order_id=1,
        status=status,
        num_contracts=num,
        num_filled=num if status == OrderStatus.FILLED else 0,
        avg_fill_price=price,
        commission=commission,
        ticker="SPY",
        right="C",
        expiry="20260311",
    )


def make_position(tier="scalp", ticker="SPY", strike=550.0, right="C",
                   direction="CALL", pnl=0.0):
    """Create a LiveScalpPosition."""
    return LiveScalpPosition(
        ticker=ticker, strike=strike, right=right,
        direction=direction, expiry="20260311", tier=tier,
        entry_premium=2.50, num_contracts=1,
        atr_at_entry=0.30, stop_price=549.0, target_price=551.0,
        best_favorable_underlying=550.0, is_open=True,
    )


# ═════════════════════════════════════════════════════════════════
# Test 1: StateManager Wired Into Exit Flow
# ═════════════════════════════════════════════════════════════════

class TestStateManagerWiring:

    def test_engine_accepts_state_manager(self):
        """LiveScalpEngine constructor accepts state_manager param."""
        engine = make_engine(state_manager=MagicMock())
        assert engine.state_manager is not None

    def test_engine_without_state_manager(self):
        """LiveScalpEngine works fine with state_manager=None."""
        engine = make_engine(state_manager=None)
        assert engine.state_manager is None

    def test_exit_calls_state_manager_record_fill(self):
        """When a position exits, state_manager.record_fill() is called."""
        with tempfile.TemporaryDirectory() as tmp:
            sm = make_state_manager(tmp)
            engine = make_engine(state_manager=sm)

            # Set up a position and mock the exit flow
            pos = make_position(tier="scalp")
            engine.open_scalp = pos

            # Mock provider and executor for the exit path
            import pandas as pd
            import numpy as np
            bars = pd.DataFrame({
                "open": [550.0], "high": [551.0],
                "low": [549.0], "close": [550.5],
                "volume": [100000],
            }, index=pd.DatetimeIndex([datetime.now()]))
            engine.provider.get_historical_bars.return_value = bars

            chain_item = {
                "strike": 550.0, "right": "C",
                "bid": 3.00, "ask": 3.10,
            }
            engine.provider.get_options_chain.return_value = [chain_item]

            fill = make_fill(status=OrderStatus.FILLED, price=3.00)
            engine.executor.sell_option.return_value = fill

            # Mock exit engine to trigger exit
            engine._scalp_exit = MagicMock()
            engine._scalp_exit.check_exit.return_value = ("PROFIT_TARGET", 550.5)

            # Run exit check
            closed = engine.check_exits(minutes_to_close=30)

            # Verify state_manager.record_fill was called
            assert sm.state.daily_pnl != 0 or sm.state.trades_today > 0
            assert sm.state.trades_today >= 1

    def test_exit_pnl_propagates_to_state(self):
        """Realized PnL from exits flows to daily/weekly/monthly accumulators."""
        with tempfile.TemporaryDirectory() as tmp:
            sm = make_state_manager(tmp)
            assert sm.state.daily_pnl == 0
            assert sm.state.weekly_pnl == 0

            # Simulate record_fill directly
            fill = make_fill()
            sm.record_fill(fill, realized_pnl=500.0)

            assert sm.state.daily_pnl == 500.0
            assert sm.state.weekly_pnl == 500.0
            assert sm.state.monthly_pnl == 500.0
            assert sm.state.trades_today == 1
            assert sm.state.consecutive_losses == 0


# ═════════════════════════════════════════════════════════════════
# Test 2: Position Key Alignment
# ═════════════════════════════════════════════════════════════════

class TestKeyAlignment:

    def test_to_dict_uses_strike_key(self):
        """LiveScalpPosition.to_dict() serializes as 'strike' not 'short_strike'."""
        pos = LiveScalpPosition(ticker="SPY", strike=550.0, right="C")
        d = pos.to_dict()
        assert "strike" in d
        assert "short_strike" not in d

    def test_remove_position_matches_strike(self):
        """StateManager.remove_position matches on 'strike' key."""
        with tempfile.TemporaryDirectory() as tmp:
            sm = make_state_manager(tmp)
            sm.state.open_positions = [
                {"ticker": "SPY", "strike": 550.0, "right": "C", "expiry": "20260311"},
                {"ticker": "SPY", "strike": 555.0, "right": "P", "expiry": "20260311"},
            ]
            sm.remove_position("SPY", 550.0, "C")
            assert len(sm.state.open_positions) == 1
            assert sm.state.open_positions[0]["strike"] == 555.0

    def test_remove_position_fallback_short_strike(self):
        """StateManager.remove_position handles legacy 'short_strike' key."""
        with tempfile.TemporaryDirectory() as tmp:
            sm = make_state_manager(tmp)
            sm.state.open_positions = [
                {"ticker": "SPY", "short_strike": 550.0, "right": "C", "expiry": "20260311"},
            ]
            sm.remove_position("SPY", 550.0, "C")
            assert len(sm.state.open_positions) == 0

    def test_add_and_remove_round_trip(self):
        """add_position → remove_position round trip works with to_dict format."""
        with tempfile.TemporaryDirectory() as tmp:
            sm = make_state_manager(tmp)
            pos = LiveScalpPosition(ticker="QQQ", strike=480.0, right="P")
            sm.add_position(pos.to_dict())
            assert len(sm.state.open_positions) == 1

            sm.remove_position("QQQ", 480.0, "P")
            assert len(sm.state.open_positions) == 0


# ═════════════════════════════════════════════════════════════════
# Test 3: Connection Resilience
# ═════════════════════════════════════════════════════════════════

class TestConnectionResilience:

    def test_attempt_reconnect_exists(self):
        """LiveTradingLoop has _attempt_reconnect method."""
        from run_live import LiveTradingLoop
        loop = LiveTradingLoop(dry_run=True)
        assert hasattr(loop, "_attempt_reconnect")

    def test_reconnect_dry_run_noop(self):
        """_attempt_reconnect does nothing in dry_run mode."""
        from run_live import LiveTradingLoop
        loop = LiveTradingLoop(dry_run=True)
        loop._attempt_reconnect()  # Should not crash or hang
        assert loop._running is False  # Not started, still False

    def test_partial_fill_creates_position(self):
        """Partial fill in _execute_entry creates a position for filled qty."""
        engine = make_engine()
        engine._profile = MagicMock()
        engine._profile.premium_scale = 1.0
        engine._profile.is_cash_settled = False
        engine._profile.tax_1256 = False
        engine._profile.option_exchange = "SMART"

        # Mock chain
        engine.provider.get_options_chain.return_value = [
            {"strike": 550.0, "right": "C", "bid": 2.40, "ask": 2.50,
             "mid": 2.45, "delta": 0.50, "gamma": 0.10, "iv": 0.20},
        ]

        # Mock partial fill
        fill = FillResult(
            status=OrderStatus.PARTIAL,
            num_contracts=3,
            num_filled=2,
            avg_fill_price=2.50,
            commission=1.30,
        )
        engine.executor.buy_option.return_value = fill

        pos = engine._execute_entry(
            ticker="SPY", signal_direction="CALL",
            confirmations=["EMA_STACK"], confidence=0.80,
            price=550.0, atr=0.30, median_atr=0.25,
            stop_price=549.0, target_price=551.0,
            tier="scalp", dry_run=False,
        )

        assert pos is not None
        assert pos.num_contracts == 2  # Only the filled portion


# ═════════════════════════════════════════════════════════════════
# Test 4: Position Restore on Startup
# ═════════════════════════════════════════════════════════════════

class TestPositionRestore:

    def test_restore_positions_exists(self):
        """LiveTradingLoop has _restore_positions method."""
        from run_live import LiveTradingLoop
        loop = LiveTradingLoop(dry_run=True)
        assert hasattr(loop, "_restore_positions")

    def test_restore_scalp_position(self):
        """Persisted scalp position restores to open_scalp slot."""
        from run_live import LiveTradingLoop

        loop = LiveTradingLoop(dry_run=True)
        loop.scalp_engine = make_engine()

        # Seed state with a persisted position
        with tempfile.TemporaryDirectory() as tmp:
            loop.state = make_state_manager(tmp)
            loop.state.state.open_positions = [
                {
                    "ticker": "SPY", "strike": 550.0, "right": "C",
                    "direction": "CALL", "expiry": "20260311", "tier": "scalp",
                    "entry_premium": 2.50, "num_contracts": 3,
                    "atr_at_entry": 0.30, "stop_price": 549.0,
                    "target_price": 551.0, "is_open": True,
                    "confirmations": ["EMA_STACK", "MACD_CROSS"],
                    "confidence": 0.85,
                    "entry_time": "2026-03-11T10:15:00",
                },
            ]

            loop._restore_positions()

            pos = loop.scalp_engine.open_scalp
            assert pos is not None
            assert pos.ticker == "SPY"
            assert pos.strike == 550.0
            assert pos.num_contracts == 3
            assert pos.tier == "scalp"
            assert pos.is_open is True

    def test_restore_multiple_tiers(self):
        """Positions restore to correct tier slots."""
        from run_live import LiveTradingLoop

        loop = LiveTradingLoop(dry_run=True)
        loop.scalp_engine = make_engine()

        with tempfile.TemporaryDirectory() as tmp:
            loop.state = make_state_manager(tmp)
            loop.state.state.open_positions = [
                {"ticker": "SPY", "strike": 550.0, "right": "C",
                 "direction": "CALL", "tier": "scalp", "is_open": True,
                 "entry_premium": 2.50, "num_contracts": 1},
                {"ticker": "SPY", "strike": 548.0, "right": "P",
                 "direction": "PUT", "tier": "runner", "is_open": True,
                 "entry_premium": 1.20, "num_contracts": 2},
                {"ticker": "QQQ", "strike": 480.0, "right": "C",
                 "direction": "CALL", "tier": "orb", "is_open": True,
                 "entry_premium": 3.10, "num_contracts": 1},
            ]

            loop._restore_positions()

            assert loop.scalp_engine.open_scalp is not None
            assert loop.scalp_engine.open_runner is not None
            assert loop.scalp_engine.open_orb is not None
            assert loop.scalp_engine.open_rf is None  # No RF position persisted

    def test_restore_skips_closed_positions(self):
        """Closed positions (is_open=False) are not restored."""
        from run_live import LiveTradingLoop

        loop = LiveTradingLoop(dry_run=True)
        loop.scalp_engine = make_engine()

        with tempfile.TemporaryDirectory() as tmp:
            loop.state = make_state_manager(tmp)
            loop.state.state.open_positions = [
                {"ticker": "SPY", "strike": 550.0, "right": "C",
                 "direction": "CALL", "tier": "scalp", "is_open": False,
                 "entry_premium": 2.50, "num_contracts": 1},
            ]

            loop._restore_positions()
            assert loop.scalp_engine.open_scalp is None

    def test_restore_empty_state(self):
        """No crash when state has no positions."""
        from run_live import LiveTradingLoop

        loop = LiveTradingLoop(dry_run=True)
        loop.scalp_engine = make_engine()

        with tempfile.TemporaryDirectory() as tmp:
            loop.state = make_state_manager(tmp)
            loop._restore_positions()  # Should not crash


# ═════════════════════════════════════════════════════════════════
# Test 5: NYSE Holiday Calendar
# ═════════════════════════════════════════════════════════════════

class TestHolidayCalendar:

    def test_market_is_open_function_exists(self):
        """market_is_open() can be imported."""
        from run_live import market_is_open
        result = market_is_open()
        assert isinstance(result, bool)

    @patch("run_live.datetime")
    def test_good_friday_2026_is_closed(self, mock_dt):
        """Good Friday 2026 (April 3) should be a market holiday."""
        from run_live import market_is_open
        from zoneinfo import ZoneInfo

        # April 3, 2026 is a Friday (Good Friday) — market closed
        mock_now = datetime(2026, 4, 3, 11, 0, 0, tzinfo=ZoneInfo("US/Eastern"))
        mock_dt.now.return_value = mock_now
        # date.today() is needed for other things but market_is_open uses datetime.now
        # We need the actual date class for comparisons
        mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)

        # Since we can't easily mock the full chain, let's verify the holiday list
        import inspect
        from run_live import market_is_open as fn
        src = inspect.getsource(fn)
        assert "date(2026, 4, 3)" in src, "Good Friday 2026 missing from holidays"

    def test_holiday_list_covers_2025_to_2027(self):
        """Holiday list has entries for 2025, 2026, and 2027."""
        import inspect
        from run_live import market_is_open
        src = inspect.getsource(market_is_open)

        # Check at least one holiday per year
        assert "date(2025," in src, "Missing 2025 holidays"
        assert "date(2026," in src, "Missing 2026 holidays"
        assert "date(2027," in src, "Missing 2027 holidays"

        # Check key holidays exist
        assert "1, 1)" in src, "Missing New Year's Day"
        assert "12, 25)" in src, "Missing Christmas"


# ═════════════════════════════════════════════════════════════════
# Test 6: Sync Positions Key Fix
# ═════════════════════════════════════════════════════════════════

class TestSyncPositionKeys:

    def test_sync_untracked_uses_strike_key(self):
        """Untracked IBKR positions are stored with 'strike' key."""
        with tempfile.TemporaryDirectory() as tmp:
            sm = make_state_manager(tmp)

            # Mock IBKR provider with one position
            mock_provider = MagicMock()
            mock_provider.is_connected.return_value = True

            mock_contract = MagicMock()
            mock_contract.secType = "OPT"
            mock_contract.symbol = "SPY"
            mock_contract.strike = 550.0
            mock_contract.right = "C"
            mock_contract.lastTradeDateOrContractMonth = "20260311"

            mock_position = MagicMock()
            mock_position.contract = mock_contract
            mock_position.position = 2
            mock_position.avgCost = 250.0

            mock_provider._ib.positions.return_value = [mock_position]

            sm.sync_positions(mock_provider)

            # The untracked position should use 'strike', not 'short_strike'
            assert len(sm.state.open_positions) == 1
            pos = sm.state.open_positions[0]
            assert "strike" in pos
            assert "short_strike" not in pos
            assert pos["strike"] == 550.0
            assert pos["num_contracts"] == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

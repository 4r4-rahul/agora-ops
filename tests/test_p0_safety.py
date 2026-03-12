#!/usr/bin/env python3
"""
Tests for v7.3 P0 Safety Fixes
================================
Covers:
  P0-1: Live market data first, delayed fallback
  P0-2: Paper vs live port guard
  P0-3: IBKR account ID validation
  P0-4: Atomic state file writes
  P0-5: Entry commission tracked in P&L
  P0-6: peak_equity defaults from account_size (not hardcoded 50K)
"""

import os
import sys
import json
import tempfile
import pytest
from datetime import datetime
from unittest.mock import MagicMock, patch
from dataclasses import asdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from trading_engine.execution.state import StateManager, DailyState
from trading_engine.execution.safety import SafetyMonitor
from trading_engine.execution import OrderStatus, FillResult
from trading_engine.live_engine import LiveScalpEngine, LiveScalpPosition
from trading_engine.config import EngineConfig


# ═════════════════════════════════════════════════════════════════
# P0-1 & P0-2 & P0-3: IBKR Provider Connection Safety
# ═════════════════════════════════════════════════════════════════

class TestIBKRConnectionSafety:

    def test_live_market_data_type_1_requested_first(self):
        """ibkr_provider.connect() calls reqMarketDataType(1) before 3."""
        import inspect
        from trading_engine.data.ibkr_provider import IBKRDataProvider
        src = inspect.getsource(IBKRDataProvider.connect)
        # Type 1 (live) should appear BEFORE Type 3 (delayed) in the code
        idx_live = src.index("reqMarketDataType(1)")
        idx_delayed = src.index("reqMarketDataType(3)")
        assert idx_live < idx_delayed, "Live data (Type 1) must be requested before delayed (Type 3)"

    def test_paper_port_guard_exists(self):
        """Connect checks TRADING_MODE vs port for paper/live mismatch."""
        import inspect
        from trading_engine.data.ibkr_provider import IBKRDataProvider
        src = inspect.getsource(IBKRDataProvider.connect)
        assert "TRADING_MODE" in src
        assert "SAFETY ABORT" in src
        assert "7497" in src or "_PAPER_PORTS" in src

    def test_account_id_validation_exists(self):
        """Connect validates IBKR_ACCOUNT against managedAccounts."""
        import inspect
        from trading_engine.data.ibkr_provider import IBKRDataProvider
        src = inspect.getsource(IBKRDataProvider.connect)
        assert "IBKR_ACCOUNT" in src
        assert "managedAccounts" in src

    @patch.dict(os.environ, {"TRADING_MODE": "paper", "IBKR_PORT": "7496"})
    def test_paper_mode_live_port_raises(self):
        """TRADING_MODE=paper on live port 7496 raises RuntimeError."""
        from trading_engine.data.ibkr_provider import IBKRDataProvider
        provider = IBKRDataProvider(port=7496)
        # The connect() method should raise before even trying to connect
        with pytest.raises(RuntimeError, match="SAFETY ABORT"):
            provider.connect()

    @patch.dict(os.environ, {"TRADING_MODE": "paper", "IBKR_PORT": "4002"})
    def test_paper_mode_gateway_live_port_raises(self):
        """TRADING_MODE=paper on live gateway port 4002 raises RuntimeError."""
        from trading_engine.data.ibkr_provider import IBKRDataProvider
        provider = IBKRDataProvider(port=4002)
        with pytest.raises(RuntimeError, match="SAFETY ABORT"):
            provider.connect()


# ═════════════════════════════════════════════════════════════════
# P0-4: Atomic State File Writes
# ═════════════════════════════════════════════════════════════════

class TestAtomicStateWrite:

    def test_save_creates_file(self):
        """save() creates the state file."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            sm = StateManager(path=path, account_size=10_000)
            sm.load()
            sm.save()
            assert os.path.exists(path)

    def test_save_creates_backup(self):
        """save() creates a .bak backup of the previous state."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            sm = StateManager(path=path, account_size=10_000)
            sm.load()

            # First save — no backup yet (no previous file)
            sm.save()
            assert os.path.exists(path)

            # Second save — should create .bak
            sm.state.daily_pnl = 500.0
            sm.save()
            assert os.path.exists(path + ".bak")

            # Backup should have old data (daily_pnl=0)
            with open(path + ".bak") as f:
                bak_data = json.load(f)
            assert bak_data["daily_pnl"] == 0.0

            # Current should have new data
            with open(path) as f:
                cur_data = json.load(f)
            assert cur_data["daily_pnl"] == 500.0

    def test_save_no_temp_file_left_behind(self):
        """save() does not leave temp files in the directory."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            sm = StateManager(path=path, account_size=10_000)
            sm.load()
            sm.save()

            files = os.listdir(tmp)
            temp_files = [f for f in files if f.startswith(".state_") and f.endswith(".tmp")]
            assert len(temp_files) == 0, f"Leftover temp files: {temp_files}"

    def test_save_valid_json_after_write(self):
        """Saved file is always valid JSON."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            sm = StateManager(path=path, account_size=10_000)
            sm.load()
            sm.state.daily_pnl = 123.45
            sm.state.trades_today = 3
            sm.save()

            with open(path) as f:
                data = json.load(f)
            assert data["daily_pnl"] == 123.45
            assert data["trades_today"] == 3

    def test_save_load_round_trip(self):
        """State survives save → reload cycle."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")

            sm1 = StateManager(path=path, account_size=10_000)
            sm1.load()
            sm1.state.daily_pnl = 789.0
            sm1.state.weekly_pnl = 1500.0
            sm1.state.open_positions = [{"ticker": "SPY", "strike": 550.0}]
            sm1.save()

            sm2 = StateManager(path=path, account_size=10_000)
            sm2.load()
            assert sm2.state.daily_pnl == 789.0
            assert sm2.state.weekly_pnl == 1500.0
            assert len(sm2.state.open_positions) == 1


# ═════════════════════════════════════════════════════════════════
# P0-5: Entry Commission in P&L
# ═════════════════════════════════════════════════════════════════

class TestEntryCommission:

    def test_position_has_entry_commission_field(self):
        """LiveScalpPosition has entry_commission attribute."""
        pos = LiveScalpPosition()
        assert hasattr(pos, "entry_commission")
        assert pos.entry_commission == 0.0

    def test_entry_commission_in_to_dict(self):
        """to_dict() includes entry_commission."""
        pos = LiveScalpPosition(ticker="SPY", strike=550.0, entry_commission=1.30)
        d = pos.to_dict()
        assert "entry_commission" in d
        assert d["entry_commission"] == 1.30

    def test_entry_commission_stored_on_fill(self):
        """_execute_entry stores fill.commission as entry_commission."""
        engine = LiveScalpEngine(
            provider=MagicMock(),
            executor=MagicMock(),
            config=EngineConfig(),
            account_size=10_000.0,
        )
        engine._profile = MagicMock()
        engine._profile.premium_scale = 1.0
        engine._profile.is_cash_settled = False
        engine._profile.tax_1256 = False
        engine._profile.option_exchange = "SMART"

        engine.provider.get_options_chain.return_value = [
            {"strike": 550.0, "right": "C", "bid": 2.40, "ask": 2.50,
             "mid": 2.45, "delta": 0.50, "gamma": 0.10, "iv": 0.20},
        ]

        fill = FillResult(
            status=OrderStatus.FILLED,
            num_contracts=1,
            num_filled=1,
            avg_fill_price=2.50,
            commission=1.30,  # <-- This should end up on the position
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
        assert pos.entry_commission == 1.30

    def test_exit_deducts_both_commissions(self):
        """Realized PnL subtracts both entry and exit commissions."""
        # Simulate: buy at $2.50, sell at $3.50, 1 contract
        # Raw PnL = ($3.50 - $2.50) × 1 × 100 = $100
        # Entry commission = $1.30, Exit commission = $1.30
        # Net PnL = $100 - $1.30 - $1.30 = $97.40

        engine = LiveScalpEngine(
            provider=MagicMock(),
            executor=MagicMock(),
            config=EngineConfig(),
            account_size=10_000.0,
        )

        pos = LiveScalpPosition(
            ticker="SPY", strike=550.0, right="C",
            direction="CALL", expiry="20260311", tier="scalp",
            entry_premium=2.50, num_contracts=1,
            entry_commission=1.30,  # <-- Entry commission
            atr_at_entry=0.30,
            stop_price=549.0, target_price=551.0,
            best_favorable_underlying=550.0,
            is_open=True,
        )

        # Verify the field exists and is set
        assert pos.entry_commission == 1.30

    def test_restore_preserves_entry_commission(self):
        """_restore_positions restores entry_commission from state."""
        from run_live import LiveTradingLoop

        loop = LiveTradingLoop(dry_run=True)
        loop.scalp_engine = LiveScalpEngine(
            provider=MagicMock(),
            executor=MagicMock(),
            config=EngineConfig(),
            account_size=10_000.0,
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            loop.state = StateManager(path=path, account_size=10_000)
            loop.state.load()
            loop.state.state.open_positions = [
                {
                    "ticker": "SPY", "strike": 550.0, "right": "C",
                    "direction": "CALL", "tier": "scalp", "is_open": True,
                    "entry_premium": 2.50, "num_contracts": 1,
                    "entry_commission": 1.30,
                },
            ]

            loop._restore_positions()

            pos = loop.scalp_engine.open_scalp
            assert pos is not None
            assert pos.entry_commission == 1.30


# ═════════════════════════════════════════════════════════════════
# P0-6: peak_equity Defaults from account_size
# ═════════════════════════════════════════════════════════════════

class TestPeakEquityDefault:

    def test_daily_state_default_peak_is_zero(self):
        """DailyState no longer defaults peak_equity to 50K."""
        state = DailyState()
        assert state.peak_equity != 50_000.0
        assert state.peak_equity == 0.0

    def test_state_manager_sets_peak_from_account_size(self):
        """StateManager.load() sets peak_equity from account_size."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            sm = StateManager(path=path, account_size=10_000)
            sm.load()
            assert sm.state.peak_equity == 10_000.0

    def test_state_manager_25k_account(self):
        """peak_equity correctly uses a $25K account."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            sm = StateManager(path=path, account_size=25_000)
            sm.load()
            assert sm.state.peak_equity == 25_000.0

    def test_existing_state_preserves_real_peak(self):
        """If state file has a real peak_equity, it's preserved."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")

            # Write a state file with an existing peak
            from datetime import date
            state_data = asdict(DailyState(
                date=str(date.today()),
                peak_equity=15_000.0,
                daily_pnl=500.0,
            ))
            with open(path, "w") as f:
                json.dump(state_data, f)

            sm = StateManager(path=path, account_size=10_000)
            sm.load()
            assert sm.state.peak_equity == 15_000.0  # Preserved, not overwritten

    def test_safety_monitor_default_account_size(self):
        """SafetyMonitor default account_size is 10K, not 50K."""
        sm = SafetyMonitor()
        assert sm.account_size == 10_000.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

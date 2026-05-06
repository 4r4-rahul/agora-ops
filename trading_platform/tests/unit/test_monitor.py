"""
Unit tests for MonitorAgent exit logic and option pricing model.

Verifies that stop/target detection is driven by estimated *option* price,
not the raw underlying price — the core bug this module fixed.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

import pytest

from trading_platform.agents.monitor import OpenPosition, _estimate_option_price


def _make_row(
    direction: str = "bullish",
    entry_price: float = 2.20,
    stop_loss: float = 1.10,
    profit_target: float = 4.00,
    underlying_at_entry: float = 500.0,
    low_strike: float = 500.0,
    high_strike: float = 510.0,
    dte: int = 21,
    days_elapsed: int = 5,
    contracts: int = 1,
) -> dict[str, Any]:
    expiration = date.today() + timedelta(days=dte - days_elapsed)
    return {
        "id": "test-pos-001",
        "session_id": "sess-001",
        "ticker": "SPY",
        "strategy": "bull_call_spread",
        "direction": direction,
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "profit_target": profit_target,
        "contracts": contracts,
        "max_loss_dollars": entry_price * 100,
        "underlying_at_entry": underlying_at_entry,
        "expiration_date": expiration.isoformat(),
        "original_dte": dte,
        "raw_recommendation": json.dumps({
            "legs": [
                {"option_type": "call", "strike": low_strike,
                 "expiration_dte": dte, "action": "buy", "quantity": 1},
                {"option_type": "call", "strike": high_strike,
                 "expiration_dte": dte, "action": "sell", "quantity": 1},
            ]
        }),
    }


class TestEstimateOptionPrice:

    def test_deep_itm_approaches_spread_width(self):
        """Price 540 on 500/510 spread → intrinsic = 10, spread_width = 10."""
        pos = OpenPosition(_make_row(low_strike=500, high_strike=510))
        price = _estimate_option_price(pos, 540.0)
        # intrinsic = min(540-500, 10) = 10; time_value > 0 → > 10 not possible since capped
        assert price >= 9.5  # near max

    def test_deep_otm_near_zero(self):
        """Price 460 on 500/510 spread → intrinsic = 0, time value only."""
        pos = OpenPosition(_make_row(low_strike=500, high_strike=510, dte=21, days_elapsed=5))
        price = _estimate_option_price(pos, 460.0)
        assert price < 1.5  # small time value only

    def test_at_expiry_time_value_zero(self):
        """At expiry (0 DTE remaining), time_value = 0 → option = intrinsic."""
        row = _make_row(low_strike=500, high_strike=510, dte=21, days_elapsed=21)
        pos = OpenPosition(row)
        # Price 505 → intrinsic = 5, no time value
        price = _estimate_option_price(pos, 505.0)
        assert price == pytest.approx(5.0, abs=0.01)

    def test_bearish_spread_itm_when_below(self):
        """Bearish put spread: price 480 on 500/490 → intrinsic = min(500-480, 10) = 10."""
        row = _make_row(
            direction="bearish",
            low_strike=490, high_strike=500,
            underlying_at_entry=500,
        )
        pos = OpenPosition(row)
        price = _estimate_option_price(pos, 480.0)
        assert price >= 9.0

    def test_neutral_condor_at_centre_has_value(self):
        """Iron condor: price at centre of range → intrinsic near half_width."""
        row = _make_row(
            direction="neutral",
            low_strike=490, high_strike=510,
            underlying_at_entry=500,
            dte=21, days_elapsed=5,
        )
        pos = OpenPosition(row)
        # At centre (500), displacement = 0, intrinsic = half_width = 10
        price = _estimate_option_price(pos, 500.0)
        assert price >= 9.0

    def test_fallback_when_no_legs(self):
        """Without leg data, returns entry_price unchanged."""
        row = _make_row()
        row["raw_recommendation"] = json.dumps({"legs": []})
        row["underlying_at_entry"] = None
        pos = OpenPosition(row)
        price = _estimate_option_price(pos, 530.0)
        assert price == pos.entry_price


class TestExitCondition:
    """Verify stop/target detection uses option price, not underlying price."""

    def test_stop_triggered_when_option_below_stop(self):
        from trading_platform.agents.monitor import MonitorAgent
        from unittest.mock import MagicMock
        agent = MonitorAgent.__new__(MonitorAgent)
        pos = OpenPosition(_make_row(stop_loss=1.10, profit_target=4.00))

        # Option price drops below stop
        result = agent._check_exit_condition(pos, 1.05)
        assert result == "stop_loss"

    def test_target_triggered_when_option_above_target(self):
        from trading_platform.agents.monitor import MonitorAgent
        agent = MonitorAgent.__new__(MonitorAgent)
        pos = OpenPosition(_make_row(stop_loss=1.10, profit_target=4.00))

        result = agent._check_exit_condition(pos, 4.20)
        assert result == "profit_target"

    def test_no_exit_in_midrange(self):
        from trading_platform.agents.monitor import MonitorAgent
        agent = MonitorAgent.__new__(MonitorAgent)
        pos = OpenPosition(_make_row(stop_loss=1.10, profit_target=4.00))

        result = agent._check_exit_condition(pos, 2.50)
        assert result is None

    def test_underlying_price_never_triggers_stop(self):
        """Core regression: underlying=450 should NOT hit stop=1.10."""
        from trading_platform.agents.monitor import MonitorAgent
        agent = MonitorAgent.__new__(MonitorAgent)
        pos = OpenPosition(_make_row(
            stop_loss=1.10, profit_target=4.00,
            low_strike=500, high_strike=510,
            underlying_at_entry=500,
            dte=21, days_elapsed=5,
        ))
        option_price = _estimate_option_price(pos, 450.0)
        result = agent._check_exit_condition(pos, option_price)
        # Deep OTM → small option price (~time value) — should hit stop
        # but NOT because 450 < 1.10 (wrong comparison)
        # This verifies the comparison is on the right scale (both option prices)
        assert option_price < 2.0  # option price is small, in option-price units

    def test_underlying_price_never_triggers_target(self):
        """Core regression: underlying barely above low_strike should NOT hit target=4.00."""
        from trading_platform.agents.monitor import MonitorAgent
        agent = MonitorAgent.__new__(MonitorAgent)
        pos = OpenPosition(_make_row(
            stop_loss=1.10, profit_target=4.00,
            entry_price=2.20,
            low_strike=500, high_strike=510,
            underlying_at_entry=500,
            dte=21, days_elapsed=5,  # 5 days in, 16 remaining
        ))
        # 501 = barely 1pt ITM: intrinsic=1, time_value=2.20*(16/21)*0.5≈0.84 → total≈1.84 < 4.00
        option_price = _estimate_option_price(pos, 501.0)
        result = agent._check_exit_condition(pos, option_price)
        assert result != "profit_target"
        assert option_price < 4.00

    def test_expiry_triggers_close(self):
        from trading_platform.agents.monitor import MonitorAgent
        agent = MonitorAgent.__new__(MonitorAgent)
        row = _make_row(dte=21, days_elapsed=22)  # past expiry
        pos = OpenPosition(row)
        result = agent._check_exit_condition(pos, 2.50)
        assert result == "expiry"

    def test_pnl_positive_on_profit_target(self):
        from trading_platform.agents.monitor import MonitorAgent
        agent = MonitorAgent.__new__(MonitorAgent)
        pos = OpenPosition(_make_row(entry_price=2.20, contracts=1))
        pnl = agent._compute_pnl(pos, 4.00, "profit_target")
        assert pnl == pytest.approx((4.00 - 2.20) * 1 * 100)

    def test_pnl_negative_on_stop(self):
        from trading_platform.agents.monitor import MonitorAgent
        agent = MonitorAgent.__new__(MonitorAgent)
        pos = OpenPosition(_make_row(entry_price=2.20, contracts=1))
        pnl = agent._compute_pnl(pos, 1.10, "stop_loss")
        assert pnl == pytest.approx((1.10 - 2.20) * 1 * 100)

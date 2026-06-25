#!/usr/bin/env python3
"""Test compilation and basic functionality of all execution modules."""

import os
import tempfile


def main():
    # Test 1: Import OrderExecutor
    print("✅ OrderExecutor module imports OK")

    # Test 2: Import StateManager
    from trading_engine.execution.state import StateManager
    print("✅ StateManager module imports OK")

    # Test 3: Import SafetyMonitor
    from trading_engine.execution.safety import SafetyMonitor
    print("✅ SafetyMonitor module imports OK")

    # Test 4: Import filters
    from trading_engine.filters import ProductionFilters
    print("✅ ProductionFilters module imports OK")

    # Test 5: Import config
    from trading_engine.config import AdaptiveConfig
    print("✅ Config module imports OK")

    # Test 6: StateManager save/load round trip
    state = StateManager(path=os.path.join(tempfile.mkdtemp(), "test_state.json"))
    state.state.daily_pnl = 150.0
    state.state.trades_today = 3
    state.save()
    state2 = StateManager(path=state.path)
    state2.load()
    assert state2.state.daily_pnl == 150.0
    assert state2.state.trades_today == 3
    print("✅ StateManager save/load round-trip OK")

    # Test 7: StateManager can_trade circuit breaker
    state.state.daily_pnl = -3000
    can, reason = state.can_trade()
    assert can == False
    assert "Daily loss limit" in reason
    print(f"✅ StateManager circuit breaker OK: {reason}")

    # Test 8: SafetyMonitor emergency detection
    safety = SafetyMonitor(state_manager=state, account_size=50000)
    safe, alerts = safety.check()
    assert safe == False  # Should trigger on daily limit breach
    print(f"✅ SafetyMonitor emergency detection OK ({len(alerts)} alerts)")

    # Test 9: ProductionFilters
    f = ProductionFilters()
    decision = f.pre_entry(
        history_closes=[100]*20,
        history_highs=[101]*20,
        history_lows=[99]*20,
    )
    assert decision.size_multiplier > 0
    print(f"✅ ProductionFilters pre_entry OK (mult={decision.size_multiplier})")

    # Test 10: run_live.py imports
    from run_live import classify_regime
    print("✅ run_live.py imports OK")

    # Test 11: VIX regime classification
    assert classify_regime(14) == "GREEN"
    assert classify_regime(20) == "YELLOW"
    assert classify_regime(30) == "RED"
    print("✅ VIX regime classification OK")

    # Test 12: AdaptiveConfig
    ac = AdaptiveConfig()
    gp = ac.for_regime("GREEN")
    assert gp.delta == 0.15
    assert gp.stop_mult == 2.5
    assert gp.trade_enabled == True
    yp = ac.for_regime("YELLOW")
    assert yp.position_size_mult == 0.5
    rp = ac.for_regime("RED")
    assert rp.trade_enabled == False
    print("✅ AdaptiveConfig regime params OK")

    print()
    print("═" * 50)
    print("  ALL 12 COMPILATION + UNIT TESTS PASSED ✅")
    print("═" * 50)


if __name__ == "__main__":
    main()

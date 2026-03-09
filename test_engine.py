#!/usr/bin/env python3
"""Quick test: import and run all 12 modules with simulated data."""

from trading_engine.engine import TradingEngine
from trading_engine.models import MarketSnapshot

# Create engine
engine = TradingEngine()

# Inject realistic market snapshot
snap = MarketSnapshot(
    spx_price=5650.0, spy_price=565.0, qqq_price=485.0, iwm_price=225.0,
    vix_level=18.5, vix_1d_change=-0.3, vix_term_structure="contango",
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

# Test each module
modules = [
    ("Module  1: Scanner", engine.run_scanner),
    ("Module  2: Regime", engine.run_regime),
    ("Module  3: Theta", engine.run_theta),
    ("Module  4: Strikes", engine.run_strikes),
    ("Module  5: Condor", engine.run_condor),
    ("Module  6: Premarket", engine.run_premarket),
    ("Module  7: Risk", engine.run_risk),
    ("Module  8: Skew", engine.run_skew),
    ("Module  9: Calendar", engine.run_calendar),
    ("Module 10: Earnings", lambda: engine.run_earnings()),
    ("Module 11: EOD", engine.run_eod),
    ("Module 12: Dashboard", engine.run_dashboard),
]

passed = 0
failed = 0
for name, runner in modules:
    try:
        output = runner()
        if output and len(output) > 50:
            passed += 1
            print(f"  ✅ {name} — OK ({len(output)} chars)")
        else:
            failed += 1
            print(f"  ⚠️  {name} — Output too short")
    except Exception as e:
        failed += 1
        print(f"  ❌ {name} — ERROR: {e}")

print(f"\n  Results: {passed}/12 passed, {failed}/12 failed")

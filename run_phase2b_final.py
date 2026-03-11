#!/usr/bin/env python3
"""Phase 2b FINAL validation: VWAP MR with tuned defaults."""

import pandas as pd
from trading_engine.config import EngineConfig
from trading_engine.data.scalp_backtester import ScalpBacktester

cfg = EngineConfig()
cfg.mean_reversion.enabled = False  # Still disabled (Strategy B)

bt = ScalpBacktester(config=cfg, account_size=10_000, spx_mode=True)
bars = pd.read_csv("data/intraday/SPY_ibkr_1m_180d.csv", parse_dates=["timestamp"])
bars.set_index("timestamp", inplace=True)

results = bt.run(bars, ticker="SPY", interval="1m", verbose=False)
bt.print_report(results)

# Regression check
print("\n" + "=" * 60)
print("  PHASE 2b FINAL REGRESSION CHECK")
print("=" * 60)
print(f"  Scalp trades:  {results.scalp_trades} (expected: 12)")
print(f"  Runner trades: {results.runner_trades} (expected: 3)")
print(f"  ORB trades:    {results.orb_trades} (expected: 31)")
print(f"  RF trades:     {results.rf_trades} (expected: 24)")
print(f"  VM trades:     {results.vm_trades} (NEW - VWAP MR)")
print(f"  Total trades:  {results.total_trades}")
print(f"  Total P&L:     ${results.total_pnl:+,.2f}")
print(f"  Days traded:   {results.days_traded} / {results.total_days}")

scalp_ok = results.scalp_trades == 12
runner_ok = results.runner_trades == 3
orb_ok = results.orb_trades == 31
rf_ok = results.rf_trades == 24
vm_ok = results.vm_trades > 0 and results.vm_pnl > 0

print(f"\n  Scalp:  {'✅' if scalp_ok else '❌'} ({results.scalp_trades})")
print(f"  Runner: {'✅' if runner_ok else '❌'} ({results.runner_trades})")
print(f"  ORB:    {'✅' if orb_ok else '❌'} ({results.orb_trades})")
print(f"  RF:     {'✅' if rf_ok else '❌'} ({results.rf_trades})")
print(f"  VM:     {'✅' if vm_ok else '⚠️'} ({results.vm_trades} trades, ${results.vm_pnl:+,.2f})")

all_ok = scalp_ok and runner_ok and orb_ok and rf_ok and vm_ok
print(f"\n  {'✅ ALL REGRESSION CHECKS PASS' if all_ok else '❌ REGRESSION FAILURE'}")

# v2a comparison
print(f"\n  v2a baseline:  70 trades, +$71,134, 62 days, PF=2.86")
print(f"  v2b (with VM): {results.total_trades} trades, ${results.total_pnl:+,.0f}, "
      f"{results.days_traded} days, PF={results.profit_factor:.2f}")
print(f"  Delta:         {results.total_trades - 70:+d} trades, "
      f"${results.total_pnl - 71134:+,.0f} P&L, "
      f"+{results.days_traded - 62} days")

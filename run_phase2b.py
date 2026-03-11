#!/usr/bin/env python3
"""Phase 2b baseline test: VWAP Mean-Reversion for DEAD_FLAT days."""

import pandas as pd
from trading_engine.config import EngineConfig
from trading_engine.data.scalp_backtester import ScalpBacktester

cfg = EngineConfig()
# All strategies enabled (regression check)
cfg.scalp.enabled = True
cfg.mean_reversion.enabled = False   # Still disabled
cfg.orb.enabled = True
cfg.range_fade.enabled = True
cfg.vwap_mr.enabled = True          # NEW: Phase 2b

bt = ScalpBacktester(config=cfg, account_size=10_000, spx_mode=True)
bars = pd.read_csv("data/intraday/SPY_ibkr_1m_180d.csv", parse_dates=["timestamp"])
bars.set_index("timestamp", inplace=True)

results = bt.run(bars, ticker="SPY", interval="1m", verbose=False)
bt.print_report(results)

# Quick regression check
print("\n" + "=" * 60)
print("  REGRESSION CHECK")
print("=" * 60)
print(f"  Scalp trades:  {results.scalp_trades} (expected: 12)")
print(f"  Runner trades: {results.runner_trades} (expected: 3)")
print(f"  ORB trades:    {results.orb_trades} (expected: 31)")
print(f"  RF trades:     {results.rf_trades} (expected: 24)")
print(f"  VM trades:     {results.vm_trades} (NEW - VWAP MR)")
print(f"  Total trades:  {results.total_trades}")
print(f"  Total P&L:     ${results.total_pnl:+,.2f}")
print(f"  Days traded:   {results.days_traded}")

# Show VM trade details if any
vm_trades = [t for t in results.trades if t.tier == "vwap_mr"]
if vm_trades:
    print(f"\n  VWAP-MR TRADE DETAILS:")
    print(f"  {'Date':<12} {'Dir':<5} {'Strike':<8} {'Entry$':<8} {'Exit$':<8} "
          f"{'P&L':>8} {'Reason':<15} {'Confirmations'}")
    print(f"  {'-'*90}")
    for t in vm_trades:
        print(f"  {str(t.expiry_date):<12} {t.direction:<5} {t.strike:<8.0f} "
              f"${t.entry_premium:<7.2f} ${t.exit_premium:<7.2f} "
              f"${t.total_pnl:>+7.2f} {t.exit_reason:<15} "
              f"{', '.join(t.confirmations)}")
else:
    print("\n  ⚠️  No VWAP-MR trades generated")

#!/usr/bin/env python3
"""
Phase 2a: Range-Fade Parameter Tuning

Key problems from baseline (46 trades, 41.3% WR, +$1,860):
  1. Win rate too low (41.3%) — too many weak entries
  2. Back-to-back losses on same day — 2nd trade revenge-enters after stop
  3. Late entries (after 16:00) fail more often
  4. Boundary zone 15% may be too loose (entering too early)
  
Tuning levers:
  A. max_trades_per_day: 2→1 (eliminate revenge trades)
  B. min_confirmations: 2→3 (require stronger signals)
  C. boundary_zone_pct: 0.15→0.10 (tighter zone = deeper fade)
  D. entry_end_bar: 350→300 (no entries after ~3:30pm)
  E. target_range_pct: 0.40→0.30 (take profits quicker)
  F. stop_range_pct: 0.20→0.15 (tighter stop, less risk per trade)
  G. max_hold_bars: 60→45 (exit faster if no reversion)
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from trading_engine.config import EngineConfig
from trading_engine.data.scalp_backtester import ScalpBacktester

# Load data once
print("Loading data...")
df = pd.read_csv('data/intraday/SPY_ibkr_1m_180d.csv', parse_dates=['timestamp'], index_col='timestamp')
df.index = pd.to_datetime(df.index, utc=True)
print(f"Loaded {len(df)} bars\n")

# Baseline config to modify
def run_variant(name, **overrides):
    config = EngineConfig()
    config.range_fade.enabled = True
    
    # Apply overrides to range_fade config
    for k, v in overrides.items():
        setattr(config.range_fade, k, v)
    
    bt = ScalpBacktester(config=config, account_size=10_000.0, spx_mode=True)
    results = bt.run(df, ticker='SPY', interval='1m', verbose=False)
    
    # Extract range-fade specific stats
    rf_trades = results.rf_trades if hasattr(results, 'rf_trades') else 0
    rf_pnl = results.rf_pnl if hasattr(results, 'rf_pnl') else 0.0
    rf_wins = results.rf_wins if hasattr(results, 'rf_wins') else 0
    rf_wr = (rf_wins / rf_trades * 100) if rf_trades > 0 else 0
    
    # Regression check
    scalp_ok = results.scalp_trades == 12
    runner_ok = results.runner_trades == 3
    orb_ok = results.orb_trades == 31
    regression = "✅" if (scalp_ok and runner_ok and orb_ok) else "❌ REGRESSION"
    
    print(f"  {name:40s}  RF={rf_trades:3d}  WR={rf_wr:5.1f}%  PnL=${rf_pnl:+8,.0f}  "
          f"Total={results.total_pnl:+10,.0f}  Days={results.days_traded:3d}  "
          f"PF={results.profit_factor:5.2f}  {regression}")
    
    return {
        "name": name,
        "rf_trades": rf_trades,
        "rf_wins": rf_wins,
        "rf_wr": rf_wr,
        "rf_pnl": rf_pnl,
        "total_pnl": results.total_pnl,
        "days_traded": results.days_traded,
        "profit_factor": results.profit_factor,
        "total_trades": results.total_trades,
        "total_wr": results.win_rate,
    }


print("=" * 130)
print("RANGE-FADE PARAMETER SWEEP")
print("=" * 130)

all_results = []

# 0. BASELINE (current config)
print("\n── BASELINE ──")
r = run_variant("Baseline (current)")
all_results.append(r)

# 1. Single trade per day (eliminate revenge trades)
print("\n── MAX TRADES PER DAY ──")
r = run_variant("1 trade/day", max_trades_per_day=1)
all_results.append(r)

# 2. Require 3 confirmations (stronger signal)
print("\n── MIN CONFIRMATIONS ──")
r = run_variant("3 confirmations", min_confirmations=3)
all_results.append(r)
r = run_variant("3 confirms + 1 trade/day", min_confirmations=3, max_trades_per_day=1)
all_results.append(r)

# 3. Tighter boundary zone (price must go deeper into range)
print("\n── BOUNDARY ZONE ──")
r = run_variant("10% boundary zone", boundary_zone_pct=0.10)
all_results.append(r)
r = run_variant("10% zone + 1 trade/day", boundary_zone_pct=0.10, max_trades_per_day=1)
all_results.append(r)
r = run_variant("10% zone + 3 confirms", boundary_zone_pct=0.10, min_confirmations=3)
all_results.append(r)

# 4. Earlier entry cutoff
print("\n── ENTRY CUTOFF ──")
r = run_variant("End bar 300", entry_end_bar=300)
all_results.append(r)
r = run_variant("End bar 300 + 1 trade/day", entry_end_bar=300, max_trades_per_day=1)
all_results.append(r)

# 5. Adjusted stop/target ratios
print("\n── STOP/TARGET ──")
r = run_variant("Target 30%, stop 15%", target_range_pct=0.30, stop_range_pct=0.15)
all_results.append(r)
r = run_variant("Target 50%, stop 15%", target_range_pct=0.50, stop_range_pct=0.15)
all_results.append(r)
r = run_variant("Target 50%, stop 25%", target_range_pct=0.50, stop_range_pct=0.25)
all_results.append(r)

# 6. Hold time
print("\n── HOLD TIME ──")
r = run_variant("45 bar hold", max_hold_bars=45)
all_results.append(r)
r = run_variant("30 bar hold", max_hold_bars=30)
all_results.append(r)

# 7. Combined best candidates
print("\n── COMBINED ──")
r = run_variant("Combo A: 1/day, 10%zone, end300",
    max_trades_per_day=1, boundary_zone_pct=0.10, entry_end_bar=300)
all_results.append(r)

r = run_variant("Combo B: 1/day, 10%zone, 3confirm",
    max_trades_per_day=1, boundary_zone_pct=0.10, min_confirmations=3)
all_results.append(r)

r = run_variant("Combo C: 1/day, 3confirm, end300",
    max_trades_per_day=1, min_confirmations=3, entry_end_bar=300)
all_results.append(r)

r = run_variant("Combo D: 1/day, 10%zone, tgt50/stp15",
    max_trades_per_day=1, boundary_zone_pct=0.10, 
    target_range_pct=0.50, stop_range_pct=0.15)
all_results.append(r)

r = run_variant("Combo E: 1/day, 10%zone, 45hold, end300",
    max_trades_per_day=1, boundary_zone_pct=0.10, 
    max_hold_bars=45, entry_end_bar=300)
all_results.append(r)

r = run_variant("Combo F: 1/day, 10%zone, tgt50/stp15, 45hold",
    max_trades_per_day=1, boundary_zone_pct=0.10,
    target_range_pct=0.50, stop_range_pct=0.15, max_hold_bars=45)
all_results.append(r)

r = run_variant("Combo G: ALL tuned",
    max_trades_per_day=1, boundary_zone_pct=0.10, min_confirmations=3,
    entry_end_bar=300, target_range_pct=0.50, stop_range_pct=0.15,
    max_hold_bars=45)
all_results.append(r)

# Sort by total PnL
print("\n" + "=" * 130)
print("RANKED BY TOTAL PnL")
print("=" * 130)
sorted_results = sorted(all_results, key=lambda x: x["total_pnl"], reverse=True)
for i, r in enumerate(sorted_results):
    marker = " ⭐" if i == 0 else ""
    print(f"  {i+1:2d}. {r['name']:40s}  RF={r['rf_trades']:3d}  WR={r['rf_wr']:5.1f}%  "
          f"RF_PnL=${r['rf_pnl']:+8,.0f}  Total=${r['total_pnl']:+10,.0f}  "
          f"Days={r['days_traded']:3d}  PF={r['profit_factor']:5.2f}{marker}")

print("\n")
print("RANKED BY RANGE-FADE PnL")
print("=" * 130)
sorted_rf = sorted(all_results, key=lambda x: x["rf_pnl"], reverse=True)
for i, r in enumerate(sorted_rf):
    marker = " ⭐" if i == 0 else ""
    print(f"  {i+1:2d}. {r['name']:40s}  RF={r['rf_trades']:3d}  WR={r['rf_wr']:5.1f}%  "
          f"RF_PnL=${r['rf_pnl']:+8,.0f}  Total=${r['total_pnl']:+10,.0f}  "
          f"Days={r['days_traded']:3d}  PF={r['profit_factor']:5.2f}{marker}")

# Best RF WR over 50%
print("\n")
print("RANKED BY WIN RATE (RF WR > 45%)")
print("=" * 130)
sorted_wr = sorted([r for r in all_results if r["rf_wr"] > 45], 
                    key=lambda x: x["rf_wr"], reverse=True)
for i, r in enumerate(sorted_wr):
    print(f"  {i+1:2d}. {r['name']:40s}  RF={r['rf_trades']:3d}  WR={r['rf_wr']:5.1f}%  "
          f"RF_PnL=${r['rf_pnl']:+8,.0f}  Total=${r['total_pnl']:+10,.0f}  "
          f"Days={r['days_traded']:3d}  PF={r['profit_factor']:5.2f}")

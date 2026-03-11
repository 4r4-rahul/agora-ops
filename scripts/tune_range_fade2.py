#!/usr/bin/env python3
"""
Focused range-fade tuning — based on findings from sweep:
- "1 trade/day" = best single lever: RF=24, WR=45.8%, PnL=$+5,261
- "10% zone" marginally better: RF=47, WR=42.6%, PnL=$+2,973
- "3 confirmations" hurts WR (39.1%) — actually worse
- "entry_end_bar=300" identical to baseline (no late entries anyway?)

Best path: 1 trade/day + stop/target tuning + hold time
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from trading_engine.config import EngineConfig
from trading_engine.data.scalp_backtester import ScalpBacktester

print("Loading data...")
df = pd.read_csv('data/intraday/SPY_ibkr_1m_180d.csv', parse_dates=['timestamp'], index_col='timestamp')
df.index = pd.to_datetime(df.index, utc=True)
print(f"Loaded {len(df)} bars\n")

def run_variant(name, **overrides):
    config = EngineConfig()
    config.range_fade.enabled = True
    for k, v in overrides.items():
        setattr(config.range_fade, k, v)
    bt = ScalpBacktester(config=config, account_size=10_000.0, spx_mode=True)
    results = bt.run(df, ticker='SPY', interval='1m', verbose=False)
    
    rf_trades = results.rf_trades
    rf_pnl = results.rf_pnl
    rf_wins = results.rf_wins
    rf_wr = (rf_wins / rf_trades * 100) if rf_trades > 0 else 0
    
    scalp_ok = results.scalp_trades == 12
    runner_ok = results.runner_trades == 3
    orb_ok = results.orb_trades == 31
    reg = "✅" if (scalp_ok and runner_ok and orb_ok) else "❌"
    
    print(f"  {name:45s}  RF={rf_trades:3d}  WR={rf_wr:5.1f}%  RF_PnL=${rf_pnl:+8,.0f}  "
          f"Total=${results.total_pnl:+10,.0f}  Days={results.days_traded:3d}  "
          f"PF={results.profit_factor:5.2f}  {reg}")
    
    return {
        "name": name, "rf_trades": rf_trades, "rf_wins": rf_wins,
        "rf_wr": rf_wr, "rf_pnl": rf_pnl, "total_pnl": results.total_pnl,
        "days_traded": results.days_traded, "profit_factor": results.profit_factor,
        "total_trades": results.total_trades, "total_wr": results.win_rate,
        "rf_biggest": results.rf_biggest_win,
    }

all_results = []

print("=" * 140)
print("FOCUSED RANGE-FADE TUNING (1 trade/day base)")
print("=" * 140)

# Base: 1 trade/day (already proven best single lever)
BASE = {"max_trades_per_day": 1}

print("\n── BASELINE vs 1/DAY ──")
r = run_variant("Baseline (2/day)", max_trades_per_day=2); all_results.append(r)
r = run_variant("1 trade/day", **BASE); all_results.append(r)

# Stop/target with 1/day
print("\n── STOP/TARGET (with 1/day) ──")
r = run_variant("1/day + tgt30/stp15", **BASE, target_range_pct=0.30, stop_range_pct=0.15); all_results.append(r)
r = run_variant("1/day + tgt50/stp15", **BASE, target_range_pct=0.50, stop_range_pct=0.15); all_results.append(r)
r = run_variant("1/day + tgt50/stp20", **BASE, target_range_pct=0.50, stop_range_pct=0.20); all_results.append(r)
r = run_variant("1/day + tgt60/stp20", **BASE, target_range_pct=0.60, stop_range_pct=0.20); all_results.append(r)
r = run_variant("1/day + tgt40/stp15", **BASE, target_range_pct=0.40, stop_range_pct=0.15); all_results.append(r)

# Hold time with 1/day
print("\n── HOLD TIME (with 1/day) ──")
r = run_variant("1/day + 45-bar hold", **BASE, max_hold_bars=45); all_results.append(r)
r = run_variant("1/day + 30-bar hold", **BASE, max_hold_bars=30); all_results.append(r)
r = run_variant("1/day + 90-bar hold", **BASE, max_hold_bars=90); all_results.append(r)

# Best combos
print("\n── BEST COMBOS ──")
r = run_variant("1/day + tgt50/stp15 + 45hold", **BASE, 
    target_range_pct=0.50, stop_range_pct=0.15, max_hold_bars=45); all_results.append(r)
r = run_variant("1/day + tgt50/stp15 + 90hold", **BASE,
    target_range_pct=0.50, stop_range_pct=0.15, max_hold_bars=90); all_results.append(r)
r = run_variant("1/day + tgt40/stp15 + 45hold", **BASE,
    target_range_pct=0.40, stop_range_pct=0.15, max_hold_bars=45); all_results.append(r)
r = run_variant("1/day + tgt60/stp20 + 90hold", **BASE,
    target_range_pct=0.60, stop_range_pct=0.20, max_hold_bars=90); all_results.append(r)

# MIXED only off
print("\n── MIXED REGIME ──")
r = run_variant("1/day + no MIXED", **BASE, also_trade_mixed=False); all_results.append(r)
r = run_variant("1/day + tgt50/stp15 + no MIXED", **BASE,
    target_range_pct=0.50, stop_range_pct=0.15, also_trade_mixed=False); all_results.append(r)

# Rankings
print("\n" + "=" * 140)
print("RANKED BY TOTAL PnL (with RF contribution)")
print("=" * 140)
sorted_r = sorted(all_results, key=lambda x: x["total_pnl"], reverse=True)
for i, r in enumerate(sorted_r):
    star = " ⭐" if i == 0 else ""
    print(f"  {i+1:2d}. {r['name']:45s}  RF={r['rf_trades']:3d}  WR={r['rf_wr']:5.1f}%  "
          f"RF=${r['rf_pnl']:+8,.0f}  Total=${r['total_pnl']:+10,.0f}  PF={r['profit_factor']:5.2f}{star}")

print("\n")
print("RANKED BY RF PnL ONLY")
print("=" * 140)
sorted_rf = sorted(all_results, key=lambda x: x["rf_pnl"], reverse=True)
for i, r in enumerate(sorted_rf[:10]):
    star = " ⭐" if i == 0 else ""
    print(f"  {i+1:2d}. {r['name']:45s}  RF={r['rf_trades']:3d}  WR={r['rf_wr']:5.1f}%  "
          f"RF=${r['rf_pnl']:+8,.0f}  BigWin=${r['rf_biggest']:+8,.0f}  "
          f"Total=${r['total_pnl']:+10,.0f}{star}")

#!/usr/bin/env python3
"""Quick final combos around the winner: 1/day + tgt50/stp15"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from trading_engine.config import EngineConfig
from trading_engine.data.scalp_backtester import ScalpBacktester

print("Loading data...")
df = pd.read_csv('data/intraday/SPY_ibkr_1m_180d.csv', parse_dates=['timestamp'], index_col='timestamp')
df.index = pd.to_datetime(df.index, utc=True)

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
    reg = "✅" if (results.scalp_trades == 12 and results.runner_trades == 3 and results.orb_trades == 31) else "❌"
    print(f"  {name:50s}  RF={rf_trades:3d}  WR={rf_wr:5.1f}%  RF=${rf_pnl:+8,.0f}  "
          f"Total=${results.total_pnl:+10,.0f}  Days={results.days_traded:3d}  PF={results.profit_factor:5.2f}  {reg}")
    return results

WINNER = {"max_trades_per_day": 1, "target_range_pct": 0.50, "stop_range_pct": 0.15}

print("\n── WINNER vs HOLD-TIME VARIANTS ──")
run_variant("WINNER: 1/day + tgt50/stp15", **WINNER)
run_variant("WINNER + 45-bar hold", **WINNER, max_hold_bars=45)
run_variant("WINNER + 90-bar hold", **WINNER, max_hold_bars=90)
run_variant("WINNER + 30-bar hold", **WINNER, max_hold_bars=30)

print("\n── WINNER vs MIXED OFF ──")
run_variant("WINNER + no MIXED", **WINNER, also_trade_mixed=False)

print("\n── WINNER vs BOUNDARY ZONE ──")
run_variant("WINNER + 10% boundary", **WINNER, boundary_zone_pct=0.10)
run_variant("WINNER + 20% boundary", **WINNER, boundary_zone_pct=0.20)

print("\n── WINNER WITH TRADE DETAILS ──")
config = EngineConfig()
config.range_fade.enabled = True
config.range_fade.max_trades_per_day = 1
config.range_fade.target_range_pct = 0.50
config.range_fade.stop_range_pct = 0.15
bt = ScalpBacktester(config=config, account_size=10_000.0, spx_mode=True)
r = bt.run(df, ticker='SPY', interval='1m', verbose=False)

rf_trades = [t for t in r.trades if t.tier == 'range_fade']
print(f"\nRF: {r.rf_trades} trades, WR={100*r.rf_wins/max(1,r.rf_trades):.1f}%, PnL=${r.rf_pnl:+,.0f}")
print(f"Total: {r.total_trades} trades, WR={r.win_rate:.1f}%, PnL=${r.total_pnl:+,.0f}, PF={r.profit_factor:.2f}")
print(f"Days: {r.days_traded}/{r.total_days}")
print()
for i, t in enumerate(rf_trades, 1):
    win = 'W' if t.total_pnl > 0 else 'L'
    print(f"  {i:2d}. {str(t.entry_time)[:16]}  {t.direction:4s}  ${t.total_pnl:+8,.0f}  {win}  "
          f"exit={t.exit_reason:15s}  hold={t.hold_minutes:.0f}m  "
          f"confirms={','.join(t.confirmations)}")

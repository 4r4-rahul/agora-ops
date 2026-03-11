#!/usr/bin/env python3
"""
Frequency Sweep v2 — Focus on improving frequency WITHOUT midday.

Key insight from v1: midday signals lose money (WR 34%, PF 0.74).
Instead, maximize frequency during PROVEN windows (morning + power hour).

Approach:
  A. Extend proven windows (start earlier, end later)
  B. Faster trade turnover (shorter max_hold, faster time_stop)
  C. Allow re-entry same direction after stop
  D. Lower cooldown
  E. Lower confirmations (2 instead of 3) during proven windows
  F. Combine above

The goal: get 3-4+ trades per active day while staying profitable.
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Redirect output to both console and file
class Tee:
    def __init__(self, *files):
        self.files = files
    def write(self, obj):
        for f in self.files:
            f.write(obj)
            f.flush()
    def flush(self):
        for f in self.files:
            f.flush()

_outfile = open('/tmp/sweep_v2_final.txt', 'w')
sys.stdout = Tee(sys.__stdout__, _outfile)

import pandas as pd
from trading_engine.config import EngineConfig, ScalpConfig
from trading_engine.data.scalp_backtester import ScalpBacktester

# Load data
data_path = "data/intraday/SPY_ibkr_1m_180d.csv"
df = pd.read_csv(data_path, index_col=0)

print("=" * 80)
print("  FREQUENCY SWEEP v2 — Maximize Trades in Proven Windows")
print("=" * 80)
print(f"  Data: {len(df)} bars")

# ── BASELINE ──
t0 = time.time()
bt_base = ScalpBacktester(config=EngineConfig(), account_size=10_000.0, spx_mode=True)
r_base = bt_base.run(df, ticker='SPY', interval='1m', verbose=False)
t_per = time.time() - t0
print(f"\n  BASELINE: {r_base.total_trades} trades, {r_base.win_rate:.1f}% WR, "
      f"PF={r_base.profit_factor:.2f}, ${r_base.total_pnl:+,.0f}  ({t_per:.1f}s)")
print(f"  Days: {r_base.days_traded}/{r_base.total_days}, Avg/active: {r_base.total_trades/r_base.days_traded:.1f}")
print(f"  Current windows: W1={EngineConfig().scalp.window_1_start}-{EngineConfig().scalp.window_1_end}min "
      f"(9:50-10:30), W2={EngineConfig().scalp.window_2_start}-{EngineConfig().scalp.window_2_end}min (2:00-3:30)")

# ── TARGETED COMBOS (no midday!) ──
combos = [
    # ── Group A: Extend windows ──
    # Morning: start at 15min (9:45) instead of 20min (9:50), end at 90min (11:00) instead of 60min (10:30)
    {"label": "wider_morning(15-90)",
     "w1_start": 15, "w1_end": 90, "w2_start": 270, "w2_end": 360},
    
    # Morning + extend power: 240min (1:30) instead of 270min (2:00)
    {"label": "wider_both(15-90,240-370)",
     "w1_start": 15, "w1_end": 90, "w2_start": 240, "w2_end": 370},
    
    # Bigger: morning 9:45-11:30, power 1:00-3:30
    {"label": "big_windows(15-120,210-360)",
     "w1_start": 15, "w1_end": 120, "w2_start": 210, "w2_end": 360},
    
    # ── Group B: Faster turnover (same windows) ──
    {"label": "fast_turnover(ts20,mh45)",
     "time_stop": 20, "max_hold": 45},
    
    {"label": "fast_turnover(ts15,mh30)",
     "time_stop": 15, "max_hold": 30},
    
    # ── Group C: Allow re-entry + lower cooldown ──
    {"label": "reentry+cool5",
     "reentry": False, "cooldown": 5},
    
    {"label": "reentry+cool3",
     "reentry": False, "cooldown": 3},
    
    # ── Group D: Lower confirmations in good windows ──
    {"label": "conf2",
     "conf": 2},
    
    {"label": "conf2+reentry+cool5",
     "conf": 2, "reentry": False, "cooldown": 5},
    
    # ── Group E: Raise max trades ──
    {"label": "max5",
     "max_trades": 5},
    
    {"label": "max5+cool5+reentry",
     "max_trades": 5, "cooldown": 5, "reentry": False},
    
    # ── Group F: Best combinations ──
    {"label": "wide+fast+re+cool5",
     "w1_start": 15, "w1_end": 90, "w2_start": 240, "w2_end": 370,
     "time_stop": 20, "max_hold": 45, "reentry": False, "cooldown": 5, "max_trades": 5},
    
    {"label": "wide+fast+conf2+re+c5",
     "w1_start": 15, "w1_end": 90, "w2_start": 240, "w2_end": 370,
     "time_stop": 20, "max_hold": 45, "reentry": False, "cooldown": 5,
     "max_trades": 6, "conf": 2},
    
    {"label": "bigwin+fast+conf2+re+c3",
     "w1_start": 15, "w1_end": 120, "w2_start": 210, "w2_end": 360,
     "time_stop": 20, "max_hold": 45, "reentry": False, "cooldown": 3,
     "max_trades": 8, "conf": 2},
    
    {"label": "bigwin+fast+conf3+re+c5",
     "w1_start": 15, "w1_end": 120, "w2_start": 210, "w2_end": 360,
     "time_stop": 20, "max_hold": 45, "reentry": False, "cooldown": 5,
     "max_trades": 6},
    
    # ── Group G: Modest extension ──
    {"label": "modwin+max5+cool5+re",
     "w1_start": 15, "w1_end": 75, "w2_start": 255, "w2_end": 370,
     "max_trades": 5, "cooldown": 5, "reentry": False},
    
    {"label": "modwin+fast+max5+c5+re",
     "w1_start": 15, "w1_end": 75, "w2_start": 255, "w2_end": 370,
     "time_stop": 20, "max_hold": 45, "max_trades": 5, "cooldown": 5, "reentry": False},
    
    # ── Group H: Just window overlap with midday early/late (exclude core chop) ──
    # "Extended morning" 9:45-12:00 + "Extended power" 1:30-3:40
    {"label": "extwin(15-150,240-370)+re+c5",
     "w1_start": 15, "w1_end": 150, "w2_start": 240, "w2_end": 370,
     "max_trades": 6, "cooldown": 5, "reentry": False},
    
    {"label": "extwin+fast+conf2+re+c3+m8",
     "w1_start": 15, "w1_end": 150, "w2_start": 240, "w2_end": 370,
     "time_stop": 20, "max_hold": 45, "max_trades": 8, "cooldown": 3,
     "reentry": False, "conf": 2},
]

print(f"\n  Testing {len(combos)} targeted configurations...\n")
print(f"  {'#':>2} {'Config':<34} | {'Trades':>6} {'Avg/D':>6} {'DaysT':>5} {'WR%':>6} {'PF':>6} {'P&L':>10} {'Time':>5}")
print(f"  " + "-" * 97)

results = []
for idx, combo in enumerate(combos):
    t0 = time.time()
    c = EngineConfig()
    s = c.scalp
    
    # Apply overrides (no midday!)
    s.enable_midday = False  # NEVER enable midday
    
    if 'w1_start' in combo:
        s.window_1_start = combo['w1_start']
    if 'w1_end' in combo:
        s.window_1_end = combo['w1_end']
    if 'w2_start' in combo:
        s.window_2_start = combo['w2_start']
    if 'w2_end' in combo:
        s.window_2_end = combo['w2_end']
    if 'time_stop' in combo:
        s.time_stop_minutes = combo['time_stop']
    if 'max_hold' in combo:
        s.max_hold_minutes = combo['max_hold']
    if 'reentry' in combo:
        s.no_reentry_same_direction = combo['reentry']
    if 'cooldown' in combo:
        s.cooldown_bars = combo['cooldown']
    if 'max_trades' in combo:
        s.max_trades_per_day = combo['max_trades']
    if 'conf' in combo:
        s.min_confirmations = combo['conf']
        s.runner_min_confirmations = combo['conf']
    
    bt = ScalpBacktester(config=c, account_size=10_000.0, spx_mode=True)
    r = bt.run(df, ticker='SPY', interval='1m', verbose=False)
    elapsed = time.time() - t0
    
    avg_per_day = r.total_trades / r.days_traded if r.days_traded > 0 else 0
    
    results.append({
        'label': combo['label'],
        'trades': r.total_trades,
        'avg_per_day': avg_per_day,
        'days_traded': r.days_traded,
        'wr': r.win_rate,
        'pf': r.profit_factor,
        'pnl': r.total_pnl,
        'scalp': r.scalp_trades,
        'runner': r.runner_trades,
        'time': elapsed,
    })
    
    sign = '+' if r.total_pnl >= 0 else ''
    pf_str = f"{r.profit_factor:.2f}" if r.profit_factor < 100 else "INF"
    marker = " ★" if r.total_trades > r_base.total_trades and r.total_pnl > 0 else ""
    print(f"  {idx+1:>2} {combo['label']:<34} | {r.total_trades:>6} {avg_per_day:>6.1f} "
          f"{r.days_traded:>5} {r.win_rate:>6.1f} {pf_str:>6} ${sign}{r.total_pnl:>9,.0f} {elapsed:>5.1f}s{marker}")

# ── SUMMARY ──
print(f"\n" + "=" * 80)
print(f"  SUMMARY — All configs ranked by P&L")
print(f"  " + "=" * 78)

results.sort(key=lambda x: x['pnl'], reverse=True)
for i, r in enumerate(results):
    pf_str = f"{r['pf']:.2f}" if r['pf'] < 100 else "INF"
    gain = "✅" if r['pnl'] > 0 else "❌"
    more = "↑" if r['trades'] > r_base.total_trades else "="
    print(f"  {i+1:>2}. {gain} {r['label']:<34} | {r['trades']:>3}{more} trades ({r['avg_per_day']:.1f}/day) "
          f"WR={r['wr']:.0f}% PF={pf_str} ${r['pnl']:+,.0f}")

# Best profitable with more trades
better = [r for r in results if r['pnl'] > 0 and r['trades'] > r_base.total_trades]
if better:
    better.sort(key=lambda x: x['trades'], reverse=True)
    print(f"\n  🏆 BEST MORE TRADES + PROFITABLE:")
    for r in better[:5]:
        pf_str = f"{r['pf']:.2f}" if r['pf'] < 100 else "INF"
        delta_trades = r['trades'] - r_base.total_trades
        delta_pnl = r['pnl'] - r_base.total_pnl
        print(f"     {r['label']:<34} | {r['trades']} trades ({delta_trades:+d}) "
              f"${r['pnl']:+,.0f} ({delta_pnl:+,.0f}) PF={pf_str}")
else:
    print(f"\n  ⚠️ No config is both profitable AND has more trades than baseline")
    # Show the closest to profitable with more trades
    more_trades = [r for r in results if r['trades'] > r_base.total_trades]
    if more_trades:
        more_trades.sort(key=lambda x: x['pnl'], reverse=True)
        print(f"  Closest losing configs with more trades:")
        for r in more_trades[:3]:
            pf_str = f"{r['pf']:.2f}" if r['pf'] < 100 else "INF"
            print(f"     {r['label']:<34} | {r['trades']} trades, ${r['pnl']:+,.0f}, PF={pf_str}")

print()

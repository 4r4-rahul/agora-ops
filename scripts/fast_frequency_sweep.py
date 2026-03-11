#!/usr/bin/env python3
"""
Fast targeted frequency sweep — tests only the key levers identified by Phase 1 diagnostic.

Phase 1 found:
  - 51.7% of quality signals are blocked by midday window
  - conf=1 and conf=2 are identical (VOLUME+VWAP mandatory = 2 already)
  - Only 23 morning signals vs 134 midday vs 67 power hour

Key levers to test (12 combos instead of 96):
  1. enable_midday (True) — unlocks 134 blocked signals
  2. max_trades_per_day (4, 5, 6, 8) — raise the cap
  3. cooldown_bars (15 vs 5) — faster re-entry
  4. no_reentry_same_direction (True vs False) — allow re-scalping
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
from trading_engine.config import EngineConfig
from trading_engine.data.scalp_backtester import ScalpBacktester

# Load data
data_path = "data/intraday/SPY_ibkr_1m_180d.csv"
df = pd.read_csv(data_path, index_col=0)
df.index = pd.to_datetime(df.index, utc=True).tz_convert('US/Eastern')

print("=" * 80)
print("  FAST FREQUENCY SWEEP — Targeted High-Frequency Configs")
print("=" * 80)
print(f"  Data: {len(df)} bars")

# ── BASELINE ──
t0 = time.time()
bt_base = ScalpBacktester(config=EngineConfig(), account_size=10_000.0, spx_mode=True)
r_base = bt_base.run(df, ticker='SPY', interval='1m', verbose=False)
t_per = time.time() - t0
print(f"\n  BASELINE: {r_base.total_trades} trades, {r_base.win_rate:.1f}% WR, "
      f"PF={r_base.profit_factor:.2f}, ${r_base.total_pnl:+,.0f}  ({t_per:.1f}s)")
print(f"  Days traded: {r_base.days_traded}, Avg/active day: {r_base.total_trades/r_base.days_traded:.1f}")

# ── TARGETED COMBOS ──
# Each combo is a dict of overrides. The key insight: enable_midday is the #1 lever.
combos = [
    # Group A: Just enable midday (keep everything else same)
    {"label": "midday ON only",             "enable_midday": True, "max_trades": 3, "cooldown": 15, "reentry": True},
    
    # Group B: Midday ON + raise max trades
    {"label": "midday ON, max=4",           "enable_midday": True, "max_trades": 4, "cooldown": 15, "reentry": True},
    {"label": "midday ON, max=5",           "enable_midday": True, "max_trades": 5, "cooldown": 15, "reentry": True},
    {"label": "midday ON, max=6",           "enable_midday": True, "max_trades": 6, "cooldown": 15, "reentry": True},
    {"label": "midday ON, max=8",           "enable_midday": True, "max_trades": 8, "cooldown": 15, "reentry": True},
    
    # Group C: Midday ON + raise max trades + lower cooldown
    {"label": "midday+max5+cool5",          "enable_midday": True, "max_trades": 5, "cooldown": 5,  "reentry": True},
    {"label": "midday+max6+cool5",          "enable_midday": True, "max_trades": 6, "cooldown": 5,  "reentry": True},
    {"label": "midday+max8+cool5",          "enable_midday": True, "max_trades": 8, "cooldown": 5,  "reentry": True},
    
    # Group D: Midday ON + max trades + lower cooldown + allow reentry
    {"label": "midday+max5+cool5+reenter",  "enable_midday": True, "max_trades": 5, "cooldown": 5,  "reentry": False},
    {"label": "midday+max6+cool5+reenter",  "enable_midday": True, "max_trades": 6, "cooldown": 5,  "reentry": False},
    {"label": "midday+max8+cool5+reenter",  "enable_midday": True, "max_trades": 8, "cooldown": 5,  "reentry": False},
    
    # Group E: All-day window (one massive window instead of midday toggle)
    {"label": "allday+max6+cool5",          "allday": True,        "max_trades": 6, "cooldown": 5,  "reentry": True},
    {"label": "allday+max8+cool5+reenter",  "allday": True,        "max_trades": 8, "cooldown": 5,  "reentry": False},
    
    # Group F: Midday ON + lower confirmations (conf=2 same as conf=1 per diagnostic)
    {"label": "midday+conf2+max6+cool5",    "enable_midday": True, "max_trades": 6, "cooldown": 5,  "reentry": True,  "conf": 2},
    {"label": "midday+conf2+max8+cool5+re", "enable_midday": True, "max_trades": 8, "cooldown": 5,  "reentry": False, "conf": 2},
]

print(f"\n  Testing {len(combos)} targeted configurations...\n")
print(f"  {'#':>2} {'Config':<32} | {'Trades':>6} {'Avg/D':>6} {'DaysT':>5} {'WR%':>6} {'PF':>6} {'P&L':>10} {'Time':>5}")
print(f"  " + "-" * 95)

results = []
for idx, combo in enumerate(combos):
    t0 = time.time()
    c = EngineConfig()
    s = c.scalp
    
    # Apply overrides
    s.max_trades_per_day = combo['max_trades']
    s.cooldown_bars = combo['cooldown']
    s.no_reentry_same_direction = combo['reentry']
    
    if combo.get('enable_midday'):
        s.enable_midday = True
    
    if combo.get('allday'):
        s.window_1_start = 15
        s.window_1_end = 360
        s.window_2_start = 0
        s.window_2_end = 0
        s.enable_midday = False  # Not needed if all-day window
    
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
    print(f"  {idx+1:>2} {combo['label']:<32} | {r.total_trades:>6} {avg_per_day:>6.1f} "
          f"{r.days_traded:>5} {r.win_rate:>6.1f} {pf_str:>6} ${sign}{r.total_pnl:>9,.0f} {elapsed:>5.1f}s")

# ── SUMMARY ──
print(f"\n" + "=" * 80)
print(f"  SUMMARY — Ranked by P&L (profitable only)")
print(f"  " + "=" * 78)

profitable = [r for r in results if r['pnl'] > 0]
profitable.sort(key=lambda x: x['pnl'], reverse=True)

for i, r in enumerate(profitable):
    marker = " ★" if r['avg_per_day'] >= 3.5 else ""
    pf_str = f"{r['pf']:.2f}" if r['pf'] < 100 else "INF"
    print(f"  {i+1:>2}. {r['label']:<32} | {r['trades']:>3} trades ({r['avg_per_day']:.1f}/day) "
          f"WR={r['wr']:.0f}% PF={pf_str} ${r['pnl']:+,.0f}{marker}")

# ── HIGH FREQ FOCUS ──
high = [r for r in results if r['avg_per_day'] >= 3.0]
print(f"\n  CONFIGS WITH 3+ AVG TRADES/ACTIVE DAY:")
if high:
    high.sort(key=lambda x: x['pnl'], reverse=True)
    for r in high:
        pf_str = f"{r['pf']:.2f}" if r['pf'] < 100 else "INF"
        status = "✅ PROFITABLE" if r['pnl'] > 0 else "❌ LOSING"
        print(f"    {r['label']:<32} → {r['trades']} trades ({r['avg_per_day']:.1f}/day), "
              f"PF={pf_str}, ${r['pnl']:+,.0f} {status}")
else:
    print(f"    None found — need more aggressive settings")

# ── BEST RECOMMENDATION ──
print(f"\n  {'='*78}")
best_freq = [r for r in results if r['avg_per_day'] >= 2.5 and r['pnl'] > 0]
if best_freq:
    best_freq.sort(key=lambda x: x['pnl'], reverse=True)
    b = best_freq[0]
    pf_str = f"{b['pf']:.2f}" if b['pf'] < 100 else "INF"
    print(f"  🏆 BEST HIGH-FREQUENCY CONFIG: {b['label']}")
    print(f"     Trades: {b['trades']} ({b['avg_per_day']:.1f}/active day, {b['days_traded']} days)")
    print(f"     WR: {b['wr']:.1f}%, PF: {pf_str}, P&L: ${b['pnl']:+,.0f}")
    print(f"     Scalps: {b['scalp']}, Runners: {b['runner']}")
    
    # Compare to baseline
    pnl_delta = b['pnl'] - r_base.total_pnl
    trade_delta = b['trades'] - r_base.total_trades
    print(f"     vs Baseline: {trade_delta:+d} trades, ${pnl_delta:+,.0f} P&L")
else:
    print(f"  ⚠️ No profitable config with 2.5+ avg trades/day found")
    # Show best losing one
    losing = [r for r in results if r['avg_per_day'] >= 2.5]
    if losing:
        losing.sort(key=lambda x: x['pnl'], reverse=True)
        b = losing[0]
        print(f"  Best losing: {b['label']} → {b['trades']} trades, ${b['pnl']:+,.0f}")

print()

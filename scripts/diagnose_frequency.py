"""
Diagnose WHY we only get 0.28 trades/day.
Break down every bottleneck: windows, signals, cooldowns, caps.
Then sweep aggressive configs to find max frequency while staying profitable.
"""
import sys, pandas as pd, numpy as np
from collections import Counter, defaultdict
from dataclasses import dataclass
sys.path.insert(0, '.')
from trading_engine.config import EngineConfig, ScalpConfig
from trading_engine.scalper import SignalEngine
from trading_engine.data.scalp_backtester import ScalpBacktester

# Load data
df = pd.read_csv('data/intraday/SPY_ibkr_1m_180d.csv',
                  parse_dates=['timestamp'], index_col='timestamp')
df.index = pd.to_datetime(df.index, utc=True).tz_convert('US/Eastern')

config = EngineConfig()
cfg = config.scalp

# ─── PHASE 1: Count raw signals with various confirmation levels ───

engine = SignalEngine(cfg, bar_minutes=1)
price_scale = 10.0  # SPX mode

print("=" * 70)
print("  FREQUENCY BOTTLENECK DIAGNOSTIC")
print("=" * 70)

# Group by day
df['_date'] = [t.date() for t in df.index]
days = sorted(set(df['_date']))

# Track signals per day at different confirmation levels
results_by_conf = {1: [], 2: [], 3: []}
window_blocks = defaultdict(int)  # day -> signals blocked by window
cooldown_blocks = 0
direction_blocks = 0

for day_date in days:
    day_data = df[df['_date'] == day_date].copy()
    if len(day_data) < 60:
        continue

    # Simulate scanning through the day with different configs
    for min_conf in [1, 2, 3]:
        # Temporarily override min_confirmations
        old_conf = cfg.min_confirmations
        cfg.min_confirmations = min_conf
        eng = SignalEngine(cfg, bar_minutes=1)
        
        signals_today = 0
        signal_times = []
        signal_details = []
        
        for i in range(30, len(day_data)):
            bar = day_data.iloc[i]
            current_time = day_data.index[i]
            price = float(bar['close'])
            
            bars_so_far = day_data.iloc[:i+1].copy()
            sig = eng.evaluate(bars_so_far, price, 'SPY')
            
            if sig:
                signals_today += 1
                
                # Minutes since open
                if hasattr(current_time, 'hour'):
                    if hasattr(current_time, 'tzinfo') and current_time.tzinfo is not None:
                        import pytz
                        et = pytz.timezone("US/Eastern")
                        lt = current_time.astimezone(et)
                        mso = (lt - lt.replace(hour=9, minute=30, second=0)).total_seconds() / 60
                    else:
                        mso = (current_time - current_time.replace(hour=9, minute=30, second=0)).total_seconds() / 60
                else:
                    mso = i
                
                signal_times.append(mso)
                signal_details.append({
                    'time': current_time,
                    'mso': mso,
                    'dir': sig.direction,
                    'confs': sig.confirmations,
                    'in_w1': cfg.window_1_start <= mso <= cfg.window_1_end,
                    'in_w2': cfg.window_2_start <= mso <= cfg.window_2_end,
                    'in_midday': cfg.window_1_end < mso < cfg.window_2_start,
                })
        
        results_by_conf[min_conf].append({
            'date': day_date,
            'signals': signals_today,
            'details': signal_details,
        })
        
        cfg.min_confirmations = old_conf

# ─── Report: Raw Signal Counts ───
print("\n  ── RAW SIGNALS (no window/cooldown/cap filter) ──────")
print(f"  {'Confirmations':<15} {'Total Sigs':>10} {'Avg/Day':>8} {'Days>0':>7} {'Max/Day':>8}")
print(f"  " + "-" * 55)
for min_conf in [1, 2, 3]:
    total = sum(r['signals'] for r in results_by_conf[min_conf])
    active = sum(1 for r in results_by_conf[min_conf] if r['signals'] > 0)
    max_day = max(r['signals'] for r in results_by_conf[min_conf])
    avg = total / len(results_by_conf[min_conf])
    print(f"  {min_conf} confirmations  {total:>10} {avg:>8.1f} {active:>7} {max_day:>8}")

# ─── Where signals land (time distribution) ───
print("\n  ── SIGNAL TIME DISTRIBUTION (conf=3, current config) ──")
w1_sigs = 0
w2_sigs = 0
mid_sigs = 0
pre_sigs = 0
post_sigs = 0

for r in results_by_conf[3]:
    for d in r['details']:
        mso = d['mso']
        if mso < cfg.window_1_start:
            pre_sigs += 1
        elif d['in_w1']:
            w1_sigs += 1
        elif d['in_midday']:
            mid_sigs += 1
        elif d['in_w2']:
            w2_sigs += 1
        else:
            post_sigs += 1

total_3 = w1_sigs + w2_sigs + mid_sigs + pre_sigs + post_sigs
print(f"  Before Window 1 (0-{cfg.window_1_start}min):      {pre_sigs:>5} ({pre_sigs/max(total_3,1)*100:>5.1f}%)")
print(f"  Window 1 ({cfg.window_1_start}-{cfg.window_1_end}min, Morning):  {w1_sigs:>5} ({w1_sigs/max(total_3,1)*100:>5.1f}%)")
print(f"  Midday ({cfg.window_1_end}-{cfg.window_2_start}min, BLOCKED): {mid_sigs:>5} ({mid_sigs/max(total_3,1)*100:>5.1f}%)")
print(f"  Window 2 ({cfg.window_2_start}-{cfg.window_2_end}min, Power): {w2_sigs:>5} ({w2_sigs/max(total_3,1)*100:>5.1f}%)")
print(f"  After Window 2 ({cfg.window_2_end}+min):     {post_sigs:>5} ({post_sigs/max(total_3,1)*100:>5.1f}%)")
print(f"  TOTAL:                              {total_3:>5}")
print(f"\n  >>> Midday signals BLOCKED by window: {mid_sigs} ({mid_sigs/max(total_3,1)*100:.1f}%)")

# ─── Conf=2 distribution ───
print("\n  ── SIGNAL TIME DISTRIBUTION (conf=2) ────────────────")
w1_2 = w2_2 = mid_2 = pre_2 = post_2 = 0
for r in results_by_conf[2]:
    for d in r['details']:
        mso = d['mso']
        if mso < cfg.window_1_start:
            pre_2 += 1
        elif cfg.window_1_start <= mso <= cfg.window_1_end:
            w1_2 += 1
        elif cfg.window_1_end < mso < cfg.window_2_start:
            mid_2 += 1
        elif cfg.window_2_start <= mso <= cfg.window_2_end:
            w2_2 += 1
        else:
            post_2 += 1

total_2 = w1_2 + w2_2 + mid_2 + pre_2 + post_2
print(f"  Window 1 (Morning):  {w1_2:>5}")
print(f"  Midday (blocked):    {mid_2:>5}")
print(f"  Window 2 (Power):    {w2_2:>5}")
print(f"  Other:               {pre_2 + post_2:>5}")
print(f"  TOTAL:               {total_2:>5}")

# ─── PHASE 2: Sweep aggressive frequency configs ───
print("\n" + "=" * 70)
print("  FREQUENCY SWEEP — Finding Max Trades While Profitable")
print("=" * 70)

# Test combinations
combos = []

# Vary: confirmations, max_trades, windows, cooldown, reentry
for min_conf in [2, 3]:
    for max_trades in [3, 4, 5, 6]:
        for window_mode in ['current', 'expanded', 'allday']:
            for cooldown in [15, 5]:
                for reentry in [True, False]:
                    combos.append({
                        'min_conf': min_conf,
                        'max_trades': max_trades,
                        'window': window_mode,
                        'cooldown': cooldown,
                        'no_reentry': reentry,
                    })

print(f"\n  Testing {len(combos)} configurations...")

# Baseline first
bt_base = ScalpBacktester(config=EngineConfig(), account_size=10_000.0, spx_mode=True)
r_base = bt_base.run(df, ticker='SPY', interval='1m', verbose=False)
base_pf = r_base.profit_factor
base_pnl = r_base.total_pnl
base_trades = r_base.total_trades
base_wr = r_base.win_rate

print(f"\n  BASELINE: {base_trades} trades, {base_wr:.1f}% WR, PF={base_pf:.2f}, ${base_pnl:+,.0f}")
print(f"  " + "-" * 65)

results = []
for idx, combo in enumerate(combos):
    # Build custom config
    c = EngineConfig()
    s = c.scalp
    s.min_confirmations = combo['min_conf']
    s.max_trades_per_day = combo['max_trades']
    s.cooldown_bars = combo['cooldown']
    s.no_reentry_same_direction = combo['no_reentry']
    
    if combo['window'] == 'expanded':
        # Add midday window (10:30 AM - 2:00 PM)
        s.enable_midday = True
    elif combo['window'] == 'allday':
        s.window_1_start = 15
        s.window_1_end = 360
        s.window_2_start = 0
        s.window_2_end = 0
    
    # Runner uses same confirmations
    s.runner_min_confirmations = combo['min_conf']
    
    bt = ScalpBacktester(config=c, account_size=10_000.0, spx_mode=True)
    r = bt.run(df, ticker='SPY', interval='1m', verbose=False)
    
    results.append({
        **combo,
        'trades': r.total_trades,
        'wr': r.win_rate,
        'pf': r.profit_factor,
        'pnl': r.total_pnl,
        'scalp_trades': r.scalp_trades,
        'runner_trades': r.runner_trades,
        'avg_per_day': r.total_trades / r.days_traded if r.days_traded > 0 else 0,
        'days_traded': r.days_traded,
    })
    
    if (idx + 1) % 20 == 0:
        print(f"  ... {idx+1}/{len(combos)} done")

# Sort by P&L
results.sort(key=lambda x: x['pnl'], reverse=True)

# Show top 20 by P&L that have more trades than baseline
print(f"\n  ── TOP CONFIGS BY P&L (more trades than baseline {base_trades}) ──")
print(f"  {'Conf':>4} {'Max':>3} {'Window':>8} {'Cool':>4} {'Reent':>5} | "
      f"{'Trades':>6} {'Avg/D':>5} {'WR%':>5} {'PF':>5} {'P&L':>10}")
print(f"  " + "-" * 75)

shown = 0
for r in results:
    if r['trades'] > base_trades and shown < 25:
        sign = '+' if r['pnl'] >= 0 else ''
        reentry_str = 'No' if r['no_reentry'] else 'Yes'
        print(f"  {r['min_conf']:>4} {r['max_trades']:>3} {r['window']:>8} {r['cooldown']:>4} "
              f"{reentry_str:>5} | "
              f"{r['trades']:>6} {r['avg_per_day']:>5.1f} {r['wr']:>5.1f} {r['pf']:>5.2f} "
              f"${sign}{r['pnl']:>9,.0f}")
        shown += 1

# Show configs with 4+ avg trades per active day
print(f"\n  ── CONFIGS WITH 4+ AVG TRADES/ACTIVE DAY ────────────")
high_freq = [r for r in results if r['avg_per_day'] >= 3.5 and r['pnl'] > 0]
high_freq.sort(key=lambda x: x['pnl'], reverse=True)
print(f"  Found {len(high_freq)} profitable configs with 4+ trades/active day")
print(f"  {'Conf':>4} {'Max':>3} {'Window':>8} {'Cool':>4} {'Reent':>5} | "
      f"{'Trades':>6} {'Avg/D':>5} {'DaysT':>5} {'WR%':>5} {'PF':>5} {'P&L':>10}")
print(f"  " + "-" * 75)
for r in high_freq[:15]:
    sign = '+' if r['pnl'] >= 0 else ''
    reentry_str = 'No' if r['no_reentry'] else 'Yes'
    print(f"  {r['min_conf']:>4} {r['max_trades']:>3} {r['window']:>8} {r['cooldown']:>4} "
          f"{reentry_str:>5} | "
          f"{r['trades']:>6} {r['avg_per_day']:>5.1f} {r['days_traded']:>5} "
          f"{r['wr']:>5.1f} {r['pf']:>5.2f} ${sign}{r['pnl']:>9,.0f}")

# ─── Best overall: highest P&L with more trades ───
print(f"\n  ── BEST OVERALL (most profitable with more trades) ──")
better = [r for r in results if r['trades'] > base_trades and r['pnl'] > base_pnl]
better.sort(key=lambda x: x['pnl'], reverse=True)
if better:
    b = better[0]
    reentry_str = 'No' if b['no_reentry'] else 'Yes'
    print(f"  Config: conf={b['min_conf']}, max_trades={b['max_trades']}, "
          f"window={b['window']}, cooldown={b['cooldown']}, reentry={reentry_str}")
    print(f"  Trades: {b['trades']} ({b['avg_per_day']:.1f}/active day, "
          f"{b['days_traded']} days traded)")
    print(f"  WR: {b['wr']:.1f}%, PF: {b['pf']:.2f}, P&L: ${b['pnl']:+,.0f}")
    print(f"  vs Baseline: {b['trades'] - base_trades:+d} trades, "
          f"${b['pnl'] - base_pnl:+,.0f} P&L, "
          f"PF {b['pf'] - base_pf:+.2f}")
else:
    print("  No config found with more trades AND better P&L than baseline!")

# ─── Specifically: what about 4+ trades/day configs? ───
print(f"\n  ── ANSWER: CAN WE GET 4 TRADES/DAY? ─────────────────")
four_plus = [r for r in results if r['avg_per_day'] >= 3.5]
four_plus_profitable = [r for r in four_plus if r['pnl'] > 0]
four_plus.sort(key=lambda x: x['pnl'], reverse=True)

if four_plus:
    print(f"  {len(four_plus)} configs achieve ~4 trades/active day")
    print(f"  {len(four_plus_profitable)} of those are profitable")
    if four_plus_profitable:
        b = four_plus_profitable[0]
        reentry_str = 'No' if b['no_reentry'] else 'Yes'
        print(f"\n  BEST 4+/day config:")
        print(f"    conf={b['min_conf']}, max={b['max_trades']}, window={b['window']}, "
              f"cooldown={b['cooldown']}, reentry={reentry_str}")
        print(f"    {b['trades']} trades, {b['avg_per_day']:.1f}/day, "
              f"WR={b['wr']:.1f}%, PF={b['pf']:.2f}, ${b['pnl']:+,.0f}")
    if four_plus:
        w = four_plus[-1]
        reentry_str = 'No' if w['no_reentry'] else 'Yes'
        print(f"\n  WORST 4+/day config:")
        print(f"    conf={w['min_conf']}, max={w['max_trades']}, window={w['window']}, "
              f"cooldown={w['cooldown']}, reentry={reentry_str}")
        print(f"    {w['trades']} trades, {w['avg_per_day']:.1f}/day, "
              f"WR={w['wr']:.1f}%, PF={w['pf']:.2f}, ${w['pnl']:+,.0f}")
else:
    print("  No configs achieved 4 trades/active day!")
    # What's the max we can get?
    max_avg = max(r['avg_per_day'] for r in results)
    best_freq = [r for r in results if r['avg_per_day'] == max_avg][0]
    print(f"  Max achievable: {max_avg:.1f} trades/active day")
    print(f"    Config: conf={best_freq['min_conf']}, max={best_freq['max_trades']}, "
          f"window={best_freq['window']}")

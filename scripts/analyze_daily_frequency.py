"""Analyze daily trade frequency from backtest results."""
import sys, pandas as pd
from collections import Counter
sys.path.insert(0, '.')
from trading_engine.config import EngineConfig
from trading_engine.data.scalp_backtester import ScalpBacktester

df = pd.read_csv('data/intraday/SPY_ibkr_1m_180d.csv', parse_dates=['timestamp'], index_col='timestamp')
bt = ScalpBacktester(config=EngineConfig(), account_size=10_000.0, spx_mode=True)
r = bt.run(df, ticker='SPY', interval='1m', verbose=False)

# Daily trade counts
daily_trades = {}
for t in r.trades:
    d = str(t.expiry_date)
    if d not in daily_trades:
        daily_trades[d] = {'scalp': 0, 'runner': 0}
    daily_trades[d][t.tier] += 1

print('=' * 60)
print('  DAILY TRADE FREQUENCY ANALYSIS')
print('=' * 60)
print()
print(f'  Total calendar days:     {r.total_days}')
print(f'  Days with trades:        {r.days_traded} ({r.days_traded/r.total_days*100:.1f}%)')
print(f'  Days with NO trades:     {r.total_days - r.days_traded} ({(r.total_days-r.days_traded)/r.total_days*100:.1f}%)')
print()
print(f'  Total trades:            {r.total_trades}')
print(f'    Scalp:                 {r.scalp_trades}')
print(f'    Runner:                {r.runner_trades}')
print()
print(f'  Avg trades/calendar day: {r.total_trades/r.total_days:.2f}')
print(f'  Avg trades/trading day:  {r.total_trades/r.days_traded:.2f}')

# Distribution
trade_counts = []
for d in r.daily_results:
    trade_counts.append(d.trades_entered)
c = Counter(trade_counts)

print()
print('  -- DISTRIBUTION ------------------------------------')
for n in sorted(c.keys()):
    bar = '#' * c[n]
    pct = c[n] / len(trade_counts) * 100
    print(f'    {n} trades/day:  {c[n]:>3} days ({pct:>5.1f}%)  {bar}')

# Days with trades detail
print()
print(f'  -- DAYS WITH TRADES (all {r.days_traded}) ---------------------')
hdr = f'  {"Date":<12} {"Scalp":>5} {"Runner":>6} {"Total":>5} {"P&L":>10}'
print(hdr)
print(f'  ' + '-' * 45)
for d in sorted(r.daily_results, key=lambda x: x.date):
    if d.trades_entered > 0:
        dt = str(d.date)
        sc = daily_trades.get(dt, {}).get('scalp', 0)
        rn = daily_trades.get(dt, {}).get('runner', 0)
        sign = '+' if d.day_pnl >= 0 else ''
        print(f'  {dt:<12} {sc:>5} {rn:>6} {d.trades_entered:>5}  ${sign}{d.day_pnl:>8,.2f}')

# Monthly breakdown
print()
print('  -- MONTHLY FREQUENCY --------------------------------')
monthly = {}
for d in r.daily_results:
    m = str(d.date)[:7]
    if m not in monthly:
        monthly[m] = {'days': 0, 'traded': 0, 'trades': 0, 'pnl': 0}
    monthly[m]['days'] += 1
    if d.trades_entered > 0:
        monthly[m]['traded'] += 1
    monthly[m]['trades'] += d.trades_entered
    monthly[m]['pnl'] += d.day_pnl

hdr2 = f'  {"Month":<10} {"Days":>5} {"Traded":>6} {"Trades":>6} {"Avg/Day":>7} {"P&L":>10}'
print(hdr2)
print(f'  ' + '-' * 50)
for m in sorted(monthly):
    s = monthly[m]
    avg = s['trades'] / s['days'] if s['days'] else 0
    sign = '+' if s['pnl'] >= 0 else ''
    print(f'  {m:<10} {s["days"]:>5} {s["traded"]:>6} {s["trades"]:>6} {avg:>7.2f}  ${sign}{s["pnl"]:>8,.2f}')

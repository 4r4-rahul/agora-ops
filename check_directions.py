import pandas as pd
from trading_engine.config import EngineConfig
from trading_engine.data.scalp_backtester import ScalpBacktester

df = pd.read_csv('data/intraday/SPY_ibkr_1m_180d.csv', parse_dates=['timestamp'], index_col='timestamp')
df.index = pd.to_datetime(df.index, utc=True)

config = EngineConfig()
bt = ScalpBacktester(config=config, account_size=10_000.0, spx_mode=True)
r = bt.run(df, ticker='SPY', interval='1m', verbose=False)

print(f'Total Trades: {r.total_trades}, PnL: ${r.total_pnl:+,.0f}')
print()

calls = [t for t in r.trades if t.direction == 'CALL']
puts = [t for t in r.trades if t.direction == 'PUT']

print(f'=== DIRECTION BREAKDOWN ===')
print(f'CALLS: {len(calls)} trades')
if calls:
    call_pnl = sum(t.total_pnl for t in calls)
    call_wins = sum(1 for t in calls if t.total_pnl > 0)
    print(f'  WR: {100*call_wins/len(calls):.1f}%, PnL: ${call_pnl:+,.0f}')

print(f'PUTS:  {len(puts)} trades')
if puts:
    put_pnl = sum(t.total_pnl for t in puts)
    put_wins = sum(1 for t in puts if t.total_pnl > 0)
    print(f'  WR: {100*put_wins/len(puts):.1f}%, PnL: ${put_pnl:+,.0f}')

print()
print(f'=== ALL TRADES DETAIL ===')
for i, t in enumerate(r.trades, 1):
    win = 'W' if t.total_pnl > 0 else 'L'
    print(f'  {i:2d}. {str(t.entry_time)[:16]}  {t.direction:4s}  {t.tier:6s}  ${t.total_pnl:+8,.0f}  {win}  exit={t.exit_reason}')

# By strategy
print()
print(f'=== BY STRATEGY + DIRECTION ===')
for tier in ['scalp', 'runner', 'orb']:
    tier_trades = [t for t in r.trades if t.tier == tier]
    if not tier_trades:
        continue
    tc = [t for t in tier_trades if t.direction == 'CALL']
    tp = [t for t in tier_trades if t.direction == 'PUT']
    print(f'{tier.upper():6s}: {len(tc)} calls, {len(tp)} puts')

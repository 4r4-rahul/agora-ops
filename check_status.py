import pandas as pd
from trading_engine.config import EngineConfig
from trading_engine.data.scalp_backtester import ScalpBacktester

df = pd.read_csv('data/intraday/SPY_ibkr_1m_180d.csv', parse_dates=['timestamp'], index_col='timestamp')
df.index = pd.to_datetime(df.index, utc=True)

config = EngineConfig()
bt = ScalpBacktester(config=config, account_size=10_000.0, spx_mode=True)
r = bt.run(df, ticker='SPY', interval='1m', verbose=False)

print('=== CURRENT RESULTS ===')
print(f'Total Trades: {r.total_trades}')
print(f'Win Rate: {r.win_rate:.1f}%')
print(f'Profit Factor: {r.profit_factor:.2f}')
print(f'Total PnL: ${r.total_pnl:+,.0f}')
print(f'Days Traded: {r.days_traded}/{r.total_days}')
print(f'  Scalp: {r.scalp_trades} trades, PnL=${r.scalp_pnl:+,.0f}')
print(f'  Runner: {r.runner_trades} trades, PnL=${r.runner_pnl:+,.0f}')
print(f'  ORB: {r.orb_trades} trades, WR={100*r.orb_wins/max(1,r.orb_trades):.1f}%, PnL=${r.orb_pnl:+,.0f}')
print(f'  ORB Regime Skipped: {r.orb_regime_skipped} days')
print(f'Max Drawdown: {r.max_drawdown_pct:.1f}%')

# Per-day analysis
print('\n=== DAY-BY-DAY BREAKDOWN ===')
trades_by_day = {}
for t in r.trades:
    day = str(t.entry_time)[:10]
    if day not in trades_by_day:
        trades_by_day[day] = {'count': 0, 'pnl': 0, 'types': []}
    trades_by_day[day]['count'] += 1
    trades_by_day[day]['pnl'] += t.total_pnl
    trades_by_day[day]['types'].append(t.tier)

multi_trade_days = 0
single_trade_days = 0
zero_trade_days = r.total_days - len(trades_by_day)

for day in sorted(trades_by_day.keys()):
    info = trades_by_day[day]
    types_str = ', '.join(info['types'])
    if info['count'] > 1:
        multi_trade_days += 1
    else:
        single_trade_days += 1
    print(f'  {day}: {info["count"]} trades  ${info["pnl"]:+,.0f}  [{types_str}]')

print(f'\n=== TRADING FREQUENCY ===')
print(f'Total days: {r.total_days}')
print(f'Days with trades: {len(trades_by_day)} ({100*len(trades_by_day)/r.total_days:.1f}%)')
print(f'  Multi-trade days: {multi_trade_days}')
print(f'  Single-trade days: {single_trade_days}')
print(f'  Zero-trade days: {zero_trade_days}')
print(f'Avg trades/trading day: {r.total_trades/max(1,len(trades_by_day)):.1f}')
print(f'Avg trades/all days: {r.total_trades/r.total_days:.2f}')

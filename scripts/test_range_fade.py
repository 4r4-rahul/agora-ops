"""Test Phase 2a: Range-Fade strategy integration."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
from trading_engine.config import EngineConfig
from trading_engine.data.scalp_backtester import ScalpBacktester

df = pd.read_csv('data/intraday/SPY_ibkr_1m_180d.csv', parse_dates=['timestamp'], index_col='timestamp')
df.index = pd.to_datetime(df.index, utc=True)

config = EngineConfig()
bt = ScalpBacktester(config=config, account_size=10_000.0, spx_mode=True)
r = bt.run(df, ticker='SPY', interval='1m', verbose=False)

print('=== PHASE 2a RESULTS ===')
print(f'Total Trades: {r.total_trades}')
print(f'Win Rate: {r.win_rate:.1f}%')
print(f'Profit Factor: {r.profit_factor:.2f}')
print(f'Total PnL: ${r.total_pnl:+,.0f}')
print(f'Days Traded: {r.days_traded}/{r.total_days}')
print(f'Max Drawdown: {r.max_drawdown_pct:.1%}')
print()
print(f'  Scalp:     {r.scalp_trades} trades, PnL=${r.scalp_pnl:+,.0f}')
print(f'  Runner:    {r.runner_trades} trades, PnL=${r.runner_pnl:+,.0f}')
print(f'  ORB:       {r.orb_trades} trades, WR={100*r.orb_wins/max(1,r.orb_trades):.1f}%, PnL=${r.orb_pnl:+,.0f}')
print(f'  RangeFade: {r.rf_trades} trades, WR={100*r.rf_wins/max(1,r.rf_trades):.1f}%, PnL=${r.rf_pnl:+,.0f}')
print(f'  ORB regime skipped: {r.orb_regime_skipped}')

# Regression check
print()
print('=== REGRESSION CHECK ===')
baseline_scalp = 12
baseline_runner = 3
baseline_orb = 31
baseline_total_pnl = 57472

print(f'  Scalp trades: {r.scalp_trades} (expected {baseline_scalp}) {"✅" if r.scalp_trades == baseline_scalp else "❌ REGRESSION"}')
print(f'  Runner trades: {r.runner_trades} (expected {baseline_runner}) {"✅" if r.runner_trades == baseline_runner else "❌ REGRESSION"}')
print(f'  ORB trades: {r.orb_trades} (expected {baseline_orb}) {"✅" if r.orb_trades == baseline_orb else "❌ REGRESSION"}')
print(f'  RangeFade trades: {r.rf_trades} (NEW strategy)')

# Per-trade detail for range-fade
rf_trades = [t for t in r.trades if t.tier == 'range_fade']
if rf_trades:
    print(f'\n=== RANGE-FADE TRADE DETAILS ===')
    for i, t in enumerate(rf_trades, 1):
        win = 'W' if t.total_pnl > 0 else 'L'
        print(f'  {i:2d}. {str(t.entry_time)[:16]}  {t.direction:4s}  ${t.total_pnl:+8,.0f}  {win}  '
              f'exit={t.exit_reason}  hold={t.hold_minutes:.0f}m  '
              f'confirms={",".join(t.confirmations)}')

# Day-by-day
print(f'\n=== TRADING DAYS BREAKDOWN ===')
trades_by_day = {}
for t in r.trades:
    day = str(t.entry_time)[:10]
    if day not in trades_by_day:
        trades_by_day[day] = {'count': 0, 'pnl': 0, 'types': []}
    trades_by_day[day]['count'] += 1
    trades_by_day[day]['pnl'] += t.total_pnl
    trades_by_day[day]['types'].append(t.tier)

multi = sum(1 for d in trades_by_day.values() if d['count'] > 1)
single = sum(1 for d in trades_by_day.values() if d['count'] == 1)
print(f'  Days with trades: {len(trades_by_day)}')
print(f'  Multi-trade days: {multi}')
print(f'  Single-trade days: {single}')
print(f'  Zero-trade days: {r.total_days - len(trades_by_day)}')

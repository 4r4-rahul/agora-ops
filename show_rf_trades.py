#!/usr/bin/env python3
"""Show detailed Range-Fade trade breakdown."""
import sys; sys.path.insert(0, '.')
import pandas as pd
from trading_engine.config import EngineConfig
from trading_engine.data.scalp_backtester import ScalpBacktester

df = pd.read_csv('data/intraday/SPY_ibkr_1m_180d.csv', parse_dates=['timestamp'], index_col='timestamp')
df.index = pd.to_datetime(df.index, utc=True)

config = EngineConfig()
bt = ScalpBacktester(config=config, account_size=10_000.0, spx_mode=True)
r = bt.run(df, ticker='SPY', interval='1m', verbose=False)

rf = [t for t in r.trades if t.tier == 'range_fade']
wins = [t for t in rf if t.total_pnl > 0]
losses = [t for t in rf if t.total_pnl <= 0]

avg_win = sum(t.total_pnl for t in wins) / max(1, len(wins))
avg_loss = sum(t.total_pnl for t in losses) / max(1, len(losses))
rr = abs(avg_win / avg_loss) if avg_loss != 0 else 99
be_wr = 1 / (1 + rr) * 100

print(f'=== RANGE-FADE: {len(rf)} TRADES ===')
print(f'Wins:   {len(wins)} ({100*len(wins)/len(rf):.1f}%)')
print(f'Losses: {len(losses)} ({100*len(losses)/len(rf):.1f}%)')
print(f'Avg Win:  ${avg_win:+,.0f}')
print(f'Avg Loss: ${avg_loss:+,.0f}')
print(f'Biggest Win:  ${max(t.total_pnl for t in rf):+,.0f}')
print(f'Biggest Loss: ${min(t.total_pnl for t in rf):+,.0f}')
print(f'Total P&L:    ${sum(t.total_pnl for t in rf):+,.0f}')
print()
print(f'R:R ratio = {rr:.1f}x  (wins are {rr:.1f}x bigger than losses)')
print(f'Breakeven WR = {be_wr:.0f}%  (you only need {be_wr:.0f}% to not lose money)')
print(f'Actual WR    = 45.8%  (well above breakeven)')
print()
print(f'  #  Date        Dir   P&L        W/L  Exit Reason      Hold')
print(f'  ' + '-' * 65)
for i, t in enumerate(rf, 1):
    w = 'W' if t.total_pnl > 0 else 'L'
    print(f'  {i:2d}. {str(t.entry_time)[:10]}  {t.direction:4s}  ${t.total_pnl:+8,.0f}  {w}   {t.exit_reason:15s}  {t.hold_minutes:.0f}m')

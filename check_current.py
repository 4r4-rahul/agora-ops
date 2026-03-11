#!/usr/bin/env python3
"""Check current backtest state and per-strategy breakdown."""
import sys; sys.path.insert(0, '.')
import pandas as pd
from trading_engine.config import EngineConfig
from trading_engine.data.scalp_backtester import ScalpBacktester

df = pd.read_csv('data/intraday/SPY_ibkr_1m_180d.csv', parse_dates=['timestamp'], index_col='timestamp')
df.index = pd.to_datetime(df.index, utc=True)

config = EngineConfig()
bt = ScalpBacktester(config=config, account_size=10_000.0, spx_mode=True)
r = bt.run(df, ticker='SPY', interval='1m', verbose=False)

print('=== CURRENT STATE (Phase 2a) ===')
print(f'Total: {r.total_trades} trades, WR={r.win_rate:.1f}%, PF={r.profit_factor:.2f}, PnL=${r.total_pnl:+,.0f}')
print(f'Days: {r.days_traded}/{r.total_days}')
print()
print('Per strategy:')
print(f'  Scalp:  {r.scalp_trades:3d} trades, PnL=${r.scalp_pnl:+,.0f}')
print(f'  Runner: {r.runner_trades:3d} trades, PnL=${r.runner_pnl:+,.0f}')
print(f'  ORB:    {r.orb_trades:3d} trades, PnL=${r.orb_pnl:+,.0f}')
rf_wr = 100 * r.rf_wins / max(1, r.rf_trades)
print(f'  RF:     {r.rf_trades:3d} trades, WR={rf_wr:.1f}%, PnL=${r.rf_pnl:+,.0f}')
print()

# Compute non-RF metrics
non_rf = [t for t in r.trades if t.tier != 'range_fade']
rf = [t for t in r.trades if t.tier == 'range_fade']
non_rf_wins = sum(1 for t in non_rf if t.total_pnl > 0)
non_rf_pnl = sum(t.total_pnl for t in non_rf)
non_rf_gross_win = sum(t.total_pnl for t in non_rf if t.total_pnl > 0)
non_rf_gross_loss = abs(sum(t.total_pnl for t in non_rf if t.total_pnl < 0))
non_rf_pf = non_rf_gross_win / non_rf_gross_loss if non_rf_gross_loss > 0 else float('inf')

rf_wins_n = sum(1 for t in rf if t.total_pnl > 0)
rf_pnl = sum(t.total_pnl for t in rf)

print('=== COMPARISON ===')
print(f'Phase 1 (without RF): {len(non_rf)} trades, WR={100*non_rf_wins/max(1,len(non_rf)):.1f}%, PF={non_rf_pf:.2f}, PnL=${non_rf_pnl:+,.0f}')
print(f'RF only:              {len(rf)} trades, WR={100*rf_wins_n/max(1,len(rf)):.1f}%, PnL=${rf_pnl:+,.0f}')
print(f'Combined (Phase 2a):  {r.total_trades} trades, WR={r.win_rate:.1f}%, PF={r.profit_factor:.2f}, PnL=${r.total_pnl:+,.0f}')
print()
print('=== WHY BLENDED WR/PF ARE LOWER ===')
print(f'Phase 1 strategies: {len(non_rf)} trades @ {100*non_rf_wins/max(1,len(non_rf)):.1f}% WR (high selectivity)')
print(f'Range-Fade adds:    {len(rf)} trades @ {100*rf_wins_n/max(1,len(rf)):.1f}% WR (lower selectivity)')
print(f'Blended average:    {r.win_rate:.1f}% WR  (diluted but MORE PROFIT)')
print(f'')
print(f'Net P&L impact:     +${rf_pnl:+,.0f} from Range-Fade')
print(f'Return impact:      {non_rf_pnl/100:.1f}% → {r.total_pnl/100:.1f}% (+{rf_pnl/100:.1f}pp)')

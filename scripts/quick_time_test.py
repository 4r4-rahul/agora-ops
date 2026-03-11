#!/usr/bin/env python3
"""Quick timing test for a single backtest."""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
from trading_engine.config import EngineConfig
from trading_engine.data.scalp_backtester import ScalpBacktester

df = pd.read_csv('data/intraday/SPY_ibkr_1m_180d.csv', index_col=0)

t0 = time.time()
bt = ScalpBacktester(config=EngineConfig(), account_size=10_000.0, spx_mode=True)
r = bt.run(df, ticker='SPY', interval='1m', verbose=False)
elapsed = time.time() - t0

print(f'Trades: {r.total_trades}, PF: {r.profit_factor:.2f}, PnL: ${r.total_pnl:+,.0f}')
print(f'Time: {elapsed:.1f}s')

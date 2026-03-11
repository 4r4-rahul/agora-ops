#!/usr/bin/env python3
import sys; sys.path.insert(0, ".")
import pandas as pd
from trading_engine.config import EngineConfig as EC
from trading_engine.data.scalp_backtester import ScalpBacktester as SB
df = pd.read_csv("data/intraday/SPY_ibkr_1m_180d.csv", parse_dates=["timestamp"], index_col="timestamp")
df.index = pd.to_datetime(df.index, utc=True)
c = EC()
print("AM ratio:", c.scalp.rv_iv_min_ratio_w1, "PM ratio:", c.scalp.rv_iv_min_ratio)
r = SB(config=c, account_size=10000, spx_mode=True).run(df, ticker="SPY", interval="1m", verbose=False)
print(f"trades={r.total_trades} WR={r.win_rate:.1f}% PF={r.profit_factor:.2f} PnL=${r.total_pnl:+,.0f}")
print(f"scalp=${r.scalp_pnl:+,.0f} runner=${r.runner_pnl:+,.0f}")
print(f"blocked={r.iv_blocked_signals}")

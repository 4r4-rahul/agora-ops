#!/usr/bin/env python3
"""Test different IV filter thresholds to find optimal PM ratio."""
import os, sys, copy
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
from trading_engine.config import EngineConfig
from trading_engine.data.scalp_backtester import ScalpBacktester

data_path = os.path.join("data", "intraday", "SPY_ibkr_1m_180d.csv")
df = pd.read_csv(data_path, parse_dates=["timestamp"], index_col="timestamp")
df.index = pd.to_datetime(df.index, utc=True)

# Test combos: (AM_ratio, PM_ratio)
combos = [
    (0.9, 0.80),  # Current baseline
    (0.9, 0.75),  # Slightly relaxed PM
    (0.9, 0.70),  # More relaxed PM
    (0.9, 0.65),  # Aggressive PM relaxation
    (0.85, 0.75), # Slightly relaxed both
    (0.85, 0.70), # Moderate both
    (0.80, 0.70), # Relaxed both
    (0.90, 0.80, 10),  # Baseline + cooldown=10
    (0.90, 0.80, 8),   # Baseline + cooldown=8
]

print(f"{'AM':>5} {'PM':>5} {'CD':>3} | {'Trades':>6} {'WR':>6} {'PF':>6} {'P&L':>10} {'ScalpPnL':>10} {'RunPnL':>8} | {'IVBlock':>7}")
print("-" * 95)

for combo in combos:
    am = combo[0]
    pm = combo[1]
    cd = combo[2] if len(combo) > 2 else 15
    
    cfg = EngineConfig()
    cfg.scalp.rv_iv_min_ratio_w1 = am
    cfg.scalp.rv_iv_min_ratio = pm
    cfg.scalp.cooldown_bars = cd
    
    bt = ScalpBacktester(config=cfg, account_size=10000, spx_mode=True)
    res = bt.run(df.copy(), ticker="SPY", interval="1m", verbose=False)
    
    pf = res.profit_factor
    wr = res.win_rate * 100
    
    print(f"{am:>5.2f} {pm:>5.2f} {cd:>3} | {res.total_trades:>6} {wr:>5.1f}% {pf:>6.2f} ${res.total_pnl:>+9.2f} ${res.scalp_pnl:>+9.2f} ${res.runner_pnl:>+7.2f} | {res.iv_blocked_signals:>7}")

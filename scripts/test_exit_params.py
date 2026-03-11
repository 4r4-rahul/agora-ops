#!/usr/bin/env python3
"""Test exit parameter combos to maximize profit from existing trades.

The signal engine finds 15 high-quality trades. Instead of trying to find
more trades (which degrades quality), let's extract more $$$ from each.

Key levers:
  - profit_target_atr_mult: widen target → bigger winners
  - trailing_activation_atr: activate trail earlier → ride trends
  - trailing_distance_atr: tighter trail → lock in more profit
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
from trading_engine.config import EngineConfig
from trading_engine.data.scalp_backtester import ScalpBacktester

data_path = os.path.join("data", "intraday", "SPY_ibkr_1m_180d.csv")
df = pd.read_csv(data_path, parse_dates=["timestamp"], index_col="timestamp")
df.index = pd.to_datetime(df.index, utc=True)

# Test combos: (target, trail_activation, trail_distance)
combos = [
    # Baseline
    (3.5, 3.5, 1.5, "BASELINE"),
    # Wider target, ride winners longer
    (4.0, 3.0, 1.5, "wider_target+early_trail"),
    (4.5, 3.0, 1.5, "wider4.5+early_trail"),
    (5.0, 3.0, 1.5, "wider5.0+early_trail"),
    (5.0, 2.5, 1.0, "wider5.0+earlier+tight_trail"),
    # Just earlier trailing (no target change)
    (3.5, 2.5, 1.0, "early_trail+tight"),
    (3.5, 3.0, 1.0, "same_target+tight_trail"),
    # Wider target only (trail = target, so trail never activates before target)
    (4.5, 4.5, 1.5, "wider4.5_only"),
    (5.0, 5.0, 1.5, "wider5.0_only"),
    # Aggressive ride: early trail, medium distance
    (6.0, 2.5, 1.5, "ride6.0+early_trail"),
    (7.0, 3.0, 2.0, "ride7.0+trail3.0"),
    # Also test with max_hold extended
    (5.0, 3.0, 1.5, "wider5.0+early_trail+hold90"),  # will set max_hold=90
]

print(f"{'Target':>6} {'TrlAct':>6} {'TrlDst':>6} {'Hold':>4} | {'Trades':>6} {'WR':>6} {'PF':>6} {'P&L':>10} {'AvgWin':>8} {'AvgLoss':>8} {'BigWin':>8} | {'Label'}")
print("-" * 110)

for combo in combos:
    target, trail_act, trail_dist, label = combo
    
    cfg = EngineConfig()
    cfg.scalp.profit_target_atr_mult = target
    cfg.scalp.trailing_activation_atr = trail_act
    cfg.scalp.trailing_distance_atr = trail_dist
    if "hold90" in label:
        cfg.scalp.max_hold_minutes = 90
    
    bt = ScalpBacktester(config=cfg, account_size=10000, spx_mode=True)
    res = bt.run(df.copy(), ticker="SPY", interval="1m", verbose=False)
    
    pf = res.profit_factor
    wr = res.win_rate * 100
    hold = cfg.scalp.max_hold_minutes
    
    print(f"{target:>6.1f} {trail_act:>6.1f} {trail_dist:>6.1f} {hold:>4} | {res.total_trades:>6} {wr:>5.1f}% {pf:>6.2f} ${res.total_pnl:>+9.2f} ${res.avg_win:>+7.2f} ${res.avg_loss:>+7.2f} ${res.biggest_win:>+7.2f} | {label}")

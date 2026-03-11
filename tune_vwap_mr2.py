#!/usr/bin/env python3
"""Phase 2b focused tuning around best configs (Combo C and H)."""

import pandas as pd
import numpy as np
from trading_engine.config import EngineConfig
from trading_engine.data.scalp_backtester import ScalpBacktester

bars = pd.read_csv("data/intraday/SPY_ibkr_1m_180d.csv", parse_dates=["timestamp"])
bars.set_index("timestamp", inplace=True)

# ── Focused Grid around Combo C (1tpd+short) and Combo H (ultra-sel) ──
configs = [
    # name, deviation, max_trades, max_hold, stop_dev, target_return, min_conf, rsi_ob, rsi_os
    # Combo C variations (1 trade/day + shorter hold)
    ("C: base",              0.15, 1, 25, 0.30, 0.06, 2, 65, 35),
    ("C: hold=20",           0.15, 1, 20, 0.30, 0.06, 2, 65, 35),
    ("C: hold=30",           0.15, 1, 30, 0.30, 0.06, 2, 65, 35),
    ("C: hold=35",           0.15, 1, 35, 0.30, 0.06, 2, 65, 35),
    ("C: stop=0.35",         0.15, 1, 25, 0.35, 0.06, 2, 65, 35),
    ("C: stop=0.25",         0.15, 1, 25, 0.25, 0.06, 2, 65, 35),
    ("C: target=0.04",       0.15, 1, 25, 0.30, 0.04, 2, 65, 35),
    ("C: target=0.08",       0.15, 1, 25, 0.30, 0.08, 2, 65, 35),
    ("C: dev=0.18",          0.18, 1, 25, 0.30, 0.06, 2, 65, 35),
    ("C: dev=0.12",          0.12, 1, 25, 0.30, 0.06, 2, 65, 35),
    # Hybrid C+H: balanced
    ("C+H: dev18+hold30",    0.18, 1, 30, 0.35, 0.05, 2, 65, 35),
    ("C+H: dev20+hold25",    0.20, 1, 25, 0.35, 0.05, 2, 65, 35),
    ("C+H: dev18+hold25+s35", 0.18, 1, 25, 0.35, 0.06, 2, 65, 35),
    ("C+H: dev20+hold30",    0.20, 1, 30, 0.35, 0.06, 2, 65, 35),
    ("C+H: dev20+hold30+s40", 0.20, 1, 30, 0.40, 0.05, 2, 65, 35),
    # Combo H variations (ultra-selective)
    ("H: base",              0.25, 1, 30, 0.40, 0.04, 2, 70, 30),
    ("H: hold=25",           0.25, 1, 25, 0.40, 0.04, 2, 70, 30),
    ("H: hold=35",           0.25, 1, 35, 0.40, 0.04, 2, 70, 30),
    ("H: stop=0.35",         0.25, 1, 30, 0.35, 0.04, 2, 70, 30),
    ("H: dev=0.22",          0.22, 1, 30, 0.40, 0.04, 2, 70, 30),
    ("H: rsi65/35",          0.25, 1, 30, 0.40, 0.04, 2, 65, 35),
    ("H: target=0.05",       0.25, 1, 30, 0.40, 0.05, 2, 70, 30),
    ("H: target=0.06",       0.25, 1, 30, 0.40, 0.06, 2, 70, 30),
]

print(f"{'Config':<26} {'#VM':>4} {'Wins':>4} {'WR':>6} {'VM P&L':>10} "
      f"{'Total P&L':>10} {'Days':>5} {'PF':>5} {'AvgW':>8} {'AvgL':>8}")
print("=" * 95)

best_pnl = -float('inf')
best_cfg_name = ""
best_params = None

for name, dev, mtpd, mhb, sd, tvr, mc, rsi_ob, rsi_os in configs:
    cfg = EngineConfig()
    cfg.scalp.enabled = True
    cfg.mean_reversion.enabled = False
    cfg.orb.enabled = True
    cfg.range_fade.enabled = True
    cfg.vwap_mr.enabled = True
    
    cfg.vwap_mr.min_vwap_deviation_pct = dev
    cfg.vwap_mr.max_trades_per_day = mtpd
    cfg.vwap_mr.max_hold_bars = mhb
    cfg.vwap_mr.stop_deviation_pct = sd
    cfg.vwap_mr.target_vwap_return_pct = tvr
    cfg.vwap_mr.min_confirmations = mc
    cfg.vwap_mr.rsi_overbought = rsi_ob
    cfg.vwap_mr.rsi_oversold = rsi_os
    
    bt = ScalpBacktester(config=cfg, account_size=10_000, spx_mode=True)
    results = bt.run(bars, ticker="SPY", interval="1m", verbose=False)
    
    vm_trades = [t for t in results.trades if t.tier == "vwap_mr"]
    vm_wins = sum(1 for t in vm_trades if t.total_pnl > 5)
    vm_losses = [t for t in vm_trades if t.total_pnl < -5]
    vm_win_trades = [t for t in vm_trades if t.total_pnl > 5]
    vm_pnl = sum(t.total_pnl for t in vm_trades)
    vm_wr = vm_wins / len(vm_trades) * 100 if vm_trades else 0
    avg_w = np.mean([t.total_pnl for t in vm_win_trades]) if vm_win_trades else 0
    avg_l = np.mean([t.total_pnl for t in vm_losses]) if vm_losses else 0
    
    marker = ""
    if vm_pnl > best_pnl and vm_pnl > 0:
        best_pnl = vm_pnl
        best_cfg_name = name
        best_params = (dev, mtpd, mhb, sd, tvr, mc, rsi_ob, rsi_os)
        marker = " ★"
    
    print(f"{name:<26} {len(vm_trades):>4} {vm_wins:>4} {vm_wr:>5.1f}% "
          f"${vm_pnl:>+9,.0f} ${results.total_pnl:>+9,.0f} "
          f"{results.days_traded:>5} {results.profit_factor:>5.2f} "
          f"${avg_w:>+7,.0f} ${avg_l:>+7,.0f}{marker}")

print(f"\n{'='*60}")
print(f"Best: {best_cfg_name}")
print(f"  Params: dev={best_params[0]}, max_trades={best_params[1]}, "
      f"max_hold={best_params[2]}, stop_dev={best_params[3]}, "
      f"target_return={best_params[4]}, min_conf={best_params[5]}, "
      f"rsi={best_params[6]}/{best_params[7]}")
print(f"  VM P&L: ${best_pnl:+,.0f}")

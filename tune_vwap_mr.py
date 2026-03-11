#!/usr/bin/env python3
"""Phase 2b parameter tuning for VWAP Mean-Reversion strategy."""

import pandas as pd
import numpy as np
from trading_engine.config import EngineConfig
from trading_engine.data.scalp_backtester import ScalpBacktester

bars = pd.read_csv("data/intraday/SPY_ibkr_1m_180d.csv", parse_dates=["timestamp"])
bars.set_index("timestamp", inplace=True)

# ── Parameter Grid ──────────────────────────────────────────────
configs = [
    # name, deviation, max_trades, max_hold, stop_dev, target_return, min_conf, rsi_ob, rsi_os
    ("Baseline",     0.15, 2, 45, 0.30, 0.06, 2, 65, 35),
    ("Higher dev",   0.20, 2, 45, 0.30, 0.06, 2, 65, 35),
    ("1 trade/day",  0.15, 1, 45, 0.30, 0.06, 2, 65, 35),
    ("Shorter hold", 0.15, 2, 25, 0.30, 0.06, 2, 65, 35),
    ("Wider stop",   0.15, 2, 45, 0.40, 0.06, 2, 65, 35),
    ("3 confirms",   0.15, 2, 45, 0.30, 0.06, 3, 65, 35),
    ("Strict RSI",   0.15, 2, 45, 0.30, 0.06, 2, 70, 30),
    # Combos
    ("Combo A: hi_dev+1tpd",    0.20, 1, 45, 0.30, 0.06, 2, 65, 35),
    ("Combo B: hi_dev+short",   0.20, 2, 25, 0.30, 0.06, 2, 65, 35),
    ("Combo C: 1tpd+short",     0.15, 1, 25, 0.30, 0.06, 2, 65, 35),
    ("Combo D: all tight",      0.20, 1, 25, 0.35, 0.04, 2, 70, 30),
    ("Combo E: hi_dev+3conf",   0.20, 2, 45, 0.30, 0.06, 3, 65, 35),
    ("Combo F: 1tpd+strict",    0.15, 1, 45, 0.30, 0.06, 2, 70, 30),
    ("Combo G: selective",      0.22, 1, 30, 0.35, 0.05, 2, 70, 30),
    ("Combo H: ultra-sel",      0.25, 1, 30, 0.40, 0.04, 2, 70, 30),
]

print(f"{'Config':<25} {'Trades':>6} {'Wins':>5} {'WR':>6} {'VM P&L':>10} "
      f"{'Total P&L':>10} {'Days':>5} {'PF':>5}")
print("=" * 80)

best_pnl = -float('inf')
best_cfg_name = ""

for name, dev, mtpd, mhb, sd, tvr, mc, rsi_ob, rsi_os in configs:
    cfg = EngineConfig()
    cfg.scalp.enabled = True
    cfg.mean_reversion.enabled = False
    cfg.orb.enabled = True
    cfg.range_fade.enabled = True
    cfg.vwap_mr.enabled = True
    
    # Tune VM params
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
    vm_pnl = sum(t.total_pnl for t in vm_trades)
    vm_wr = vm_wins / len(vm_trades) * 100 if vm_trades else 0
    
    marker = ""
    if vm_pnl > best_pnl and vm_pnl > 0:
        best_pnl = vm_pnl
        best_cfg_name = name
        marker = " ← BEST"
    
    print(f"{name:<25} {len(vm_trades):>6} {vm_wins:>5} {vm_wr:>5.1f}% "
          f"${vm_pnl:>+9,.0f} ${results.total_pnl:>+9,.0f} "
          f"{results.days_traded:>5} {results.profit_factor:>5.2f}{marker}")

print(f"\nBest config: {best_cfg_name} (VM P&L: ${best_pnl:+,.0f})")

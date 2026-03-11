#!/usr/bin/env python3
"""Phase 2b: Fix compounding impact with smaller VM position sizes."""

import pandas as pd
from trading_engine.config import EngineConfig
from trading_engine.data.scalp_backtester import ScalpBacktester

bars = pd.read_csv("data/intraday/SPY_ibkr_1m_180d.csv", parse_dates=["timestamp"])
bars.set_index("timestamp", inplace=True)

# Baseline
cfg = EngineConfig()
cfg.vwap_mr.enabled = False
cfg.mean_reversion.enabled = False
bt = ScalpBacktester(config=cfg, account_size=10_000, spx_mode=True)
r = bt.run(bars, ticker="SPY", interval="1m", verbose=False)
base_pnl = r.total_pnl
base_days = r.days_traded
print(f"BASELINE:  ${base_pnl:+,.0f}, {base_days} days\n")

# Grid: smaller position sizes + best params
tests = [
    # (name, max_risk, max_contracts, max_risk_pct, max_premium)
    # H: hold=25 base params for all
    ("H base (risk=300,c=2)",    300, 2, 0.03, 4.0),
    ("H small (risk=200,c=1)",   200, 1, 0.02, 3.0),
    ("H tiny  (risk=150,c=1)",   150, 1, 0.015, 3.0),
    ("H micro (risk=100,c=1)",   100, 1, 0.01, 2.5),
]

print(f"{'Config':<28} {'#VM':>4} {'Wins':>4} {'WR':>6} {'VM P&L':>9} "
      f"{'Total':>10} {'Δ':>8} {'Days':>5} {'PF':>5}")
print("=" * 85)

for name, mr, mc, mrp, mp in tests:
    cfg = EngineConfig()
    cfg.mean_reversion.enabled = False
    cfg.vwap_mr.enabled = True
    # H: hold=25 params
    cfg.vwap_mr.min_vwap_deviation_pct = 0.25
    cfg.vwap_mr.max_trades_per_day = 1
    cfg.vwap_mr.max_hold_bars = 25
    cfg.vwap_mr.stop_deviation_pct = 0.40
    cfg.vwap_mr.target_vwap_return_pct = 0.04
    cfg.vwap_mr.rsi_overbought = 70.0
    cfg.vwap_mr.rsi_oversold = 30.0
    # Position sizing
    cfg.vwap_mr.max_risk_per_trade = mr
    cfg.vwap_mr.max_contracts = mc
    cfg.vwap_mr.max_risk_pct = mrp
    cfg.vwap_mr.max_premium = mp
    
    bt = ScalpBacktester(config=cfg, account_size=10_000, spx_mode=True)
    r = bt.run(bars, ticker="SPY", interval="1m", verbose=False)
    vm_t = [t for t in r.trades if t.tier == "vwap_mr"]
    vm_pnl = sum(t.total_pnl for t in vm_t)
    
    print(f"{name:<28} {len(vm_t):>4} "
          f"{sum(1 for t in vm_t if t.total_pnl > 5):>4} "
          f"{sum(1 for t in vm_t if t.total_pnl > 5)/len(vm_t)*100 if vm_t else 0:>5.1f}% "
          f"${vm_pnl:>+8,.0f} ${r.total_pnl:>+9,.0f} "
          f"${r.total_pnl-base_pnl:>+7,.0f} {r.days_traded:>5} {r.profit_factor:>5.2f}")

# Also try C: hold=20 with small sizing
print("\n--- C: hold=20 variants ---")
for name, mr, mc, mrp, mp in tests:
    cfg = EngineConfig()
    cfg.mean_reversion.enabled = False
    cfg.vwap_mr.enabled = True
    # C: hold=20 params
    cfg.vwap_mr.min_vwap_deviation_pct = 0.15
    cfg.vwap_mr.max_trades_per_day = 1
    cfg.vwap_mr.max_hold_bars = 20
    cfg.vwap_mr.stop_deviation_pct = 0.30
    cfg.vwap_mr.target_vwap_return_pct = 0.06
    cfg.vwap_mr.rsi_overbought = 65.0
    cfg.vwap_mr.rsi_oversold = 35.0
    # Position sizing
    cfg.vwap_mr.max_risk_per_trade = mr
    cfg.vwap_mr.max_contracts = mc
    cfg.vwap_mr.max_risk_pct = mrp
    cfg.vwap_mr.max_premium = mp
    
    bt = ScalpBacktester(config=cfg, account_size=10_000, spx_mode=True)
    r = bt.run(bars, ticker="SPY", interval="1m", verbose=False)
    vm_t = [t for t in r.trades if t.tier == "vwap_mr"]
    vm_pnl = sum(t.total_pnl for t in vm_t)
    
    print(f"C+{name:<24} {len(vm_t):>4} "
          f"{sum(1 for t in vm_t if t.total_pnl > 5):>4} "
          f"{sum(1 for t in vm_t if t.total_pnl > 5)/len(vm_t)*100 if vm_t else 0:>5.1f}% "
          f"${vm_pnl:>+8,.0f} ${r.total_pnl:>+9,.0f} "
          f"${r.total_pnl-base_pnl:>+7,.0f} {r.days_traded:>5} {r.profit_factor:>5.2f}")

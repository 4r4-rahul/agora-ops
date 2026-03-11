#!/usr/bin/env python3
"""Sweep per-window IV thresholds - find optimal morning gate."""
import sys; sys.path.insert(0, ".")
import pandas as pd
from trading_engine.config import EngineConfig as EC
from trading_engine.data.scalp_backtester import ScalpBacktester as SB

df = pd.read_csv("data/intraday/SPY_ibkr_1m_180d.csv", parse_dates=["timestamp"], index_col="timestamp")
df.index = pd.to_datetime(df.index, utc=True)

def test(label, **overrides):
    c = EC()
    c.scalp.iv_discount_enabled = True
    c.scalp.rv_iv_min_ratio = 0.8  # PM threshold
    for k, v in overrides.items():
        setattr(c.scalp, k, v)
    r = SB(config=c, account_size=10000, spx_mode=True).run(df, ticker="SPY", interval="1m", verbose=False)
    pf = "%.2f" % r.profit_factor if r.profit_factor < 100 else "INF"
    print(f"  {label:45s}: t={r.total_trades:2d} WR={r.win_rate:.1f}% PF={pf:>6s} PnL=${r.total_pnl:+,.0f} s=${r.scalp_pnl:+,.0f} r=${r.runner_pnl:+,.0f} blk={r.iv_blocked_signals}")

print("=== MORNING IV THRESHOLD SWEEP ===")
print("(PM always at 0.8, sweeping AM threshold)")
for w1_ratio in [0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.5, 2.0, 999]:
    label = f"AM_IV={w1_ratio}" if w1_ratio < 100 else "AM=OFF(PM_only)"
    test(label, rv_iv_min_ratio_w1=w1_ratio)

print("\n=== ALSO SWEEP PM THRESHOLD WITH AM=1.2 ===")
for pm_ratio in [0.5, 0.6, 0.7, 0.8, 0.9]:
    test(f"AM=1.2 PM={pm_ratio}", rv_iv_min_ratio_w1=1.2, rv_iv_min_ratio=pm_ratio)

print("\n=== BEST COMBOS WITH PER-WINDOW IV ===")
test("BASELINE (AM=0.8, PM=0.8)", rv_iv_min_ratio_w1=0.8)
test("AM=1.2 PM=0.8", rv_iv_min_ratio_w1=1.2)
test("AM=1.2 PM=0.7", rv_iv_min_ratio_w1=1.2, rv_iv_min_ratio=0.7)
test("AM=1.2 PM=0.6", rv_iv_min_ratio_w1=1.2, rv_iv_min_ratio=0.6)
test("AM=1.5 PM=0.8", rv_iv_min_ratio_w1=1.5)
test("AM=1.5 PM=0.7", rv_iv_min_ratio_w1=1.5, rv_iv_min_ratio=0.7)
test("AM=OFF PM=0.8", rv_iv_min_ratio_w1=999)
test("AM=OFF PM=0.7", rv_iv_min_ratio_w1=999, rv_iv_min_ratio=0.7)
test("AM=OFF PM=0.6", rv_iv_min_ratio_w1=999, rv_iv_min_ratio=0.6)

print("\nDONE")

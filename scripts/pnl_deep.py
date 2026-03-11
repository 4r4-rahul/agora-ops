#!/usr/bin/env python3
"""Deep-dive into W2-only performance and IV=0.6 + wider combos."""
import sys; sys.path.insert(0, ".")
import pandas as pd
from trading_engine.config import EngineConfig as EC
from trading_engine.data.scalp_backtester import ScalpBacktester as SB

df = pd.read_csv("data/intraday/SPY_ibkr_1m_180d.csv", parse_dates=["timestamp"], index_col="timestamp")
df.index = pd.to_datetime(df.index, utc=True)

def test(label, **overrides):
    c = EC()
    c.scalp.iv_discount_enabled = True
    c.scalp.rv_iv_min_ratio = 0.8
    for k, v in overrides.items():
        setattr(c.scalp, k, v)
    r = SB(config=c, account_size=10000, spx_mode=True).run(df, ticker="SPY", interval="1m", verbose=False)
    pf = "%.2f" % r.profit_factor if r.profit_factor < 100 else "INF"
    print(f"  {label:50s}: t={r.total_trades:2d} WR={r.win_rate:.1f}% PF={pf:>6s} PnL=${r.total_pnl:+,.0f}")
    return r

# The W2-only finding is incredible. Let's test W2 variants.
print("=== WINDOW-2 FOCUSED (afternoon edge) ===")
test("w2_only 270-360", window_1_start=9999, window_1_end=9999)
test("w2_only 250-360", window_1_start=9999, window_1_end=9999, window_2_start=250, window_2_end=360)
test("w2_only 260-360", window_1_start=9999, window_1_end=9999, window_2_start=260, window_2_end=360)
test("w2_only 280-360", window_1_start=9999, window_1_end=9999, window_2_start=280, window_2_end=360)
test("w2_only 270-350", window_1_start=9999, window_1_end=9999, window_2_start=270, window_2_end=350)
test("w2_only 270-330", window_1_start=9999, window_1_end=9999, window_2_start=270, window_2_end=330)

# W2-only with looser IV to get more trades
print("\n=== W2-ONLY + LOOSER IV ===")
for ratio in [0.5, 0.6, 0.7, 0.8]:
    c = EC()
    c.scalp.iv_discount_enabled = True
    c.scalp.rv_iv_min_ratio = ratio
    c.scalp.window_1_start = 9999
    c.scalp.window_1_end = 9999
    r = SB(config=c, account_size=10000, spx_mode=True).run(df, ticker="SPY", interval="1m", verbose=False)
    pf = "%.2f" % r.profit_factor if r.profit_factor < 100 else "INF"
    print(f"  w2_only IV={ratio}: t={r.total_trades:2d} WR={r.win_rate:.1f}% PF={pf:>6s} PnL=${r.total_pnl:+,.0f}")

# What if we keep W1 but TIGHTEN it? Morning is losing money.
print("\n=== TIGHTEN W1 (reduce morning losses) ===")
test("w1=30-45 w2=default", window_1_start=30, window_1_end=45)
test("w1=35-50 w2=default", window_1_start=35, window_1_end=50)
test("w1=20-40 w2=default", window_1_start=20, window_1_end=40)

# What if we use a higher IV threshold for W1 (morning)?
# This means only trade morning when vol is REALLY cheap
print("\n=== DIFFERENT IV THRESHOLDS PER WINDOW ===")
# We can't directly do per-window IV thresholds, but let's try
# higher overall thresholds which effectively block more morning trades
for ratio in [0.9, 1.0, 1.1]:
    c = EC()
    c.scalp.iv_discount_enabled = True
    c.scalp.rv_iv_min_ratio = ratio
    r = SB(config=c, account_size=10000, spx_mode=True).run(df, ticker="SPY", interval="1m", verbose=False)
    pf = "%.2f" % r.profit_factor if r.profit_factor < 100 else "INF"
    s, rr = r.scalp_pnl, r.runner_pnl
    print(f"  IV={ratio}: t={r.total_trades:2d} WR={r.win_rate:.1f}% PF={pf:>6s} PnL=${r.total_pnl:+,.0f} s=${s:+,.0f} r=${rr:+,.0f}")

# Combo: IV=0.6 + W2-only (more trades but PM only)
print("\n=== BEST COMBOS TO MAXIMIZE P&L ===")
test("BASELINE(IV=0.8, both windows)")
test("A: W2-only(270-360) IV=0.8", window_1_start=9999, window_1_end=9999)
test("B: W2-only(270-360) IV=0.6", rv_iv_min_ratio=0.6, window_1_start=9999, window_1_end=9999)
test("C: W2-only(250-360) IV=0.6", rv_iv_min_ratio=0.6, window_1_start=9999, window_1_end=9999, window_2_start=250, window_2_end=360)
test("D: W2-only(270-360) IV=0.5", rv_iv_min_ratio=0.5, window_1_start=9999, window_1_end=9999)
test("E: IV=0.6 + both windows", rv_iv_min_ratio=0.6)
test("F: IV=0.6 wider(15-50,250-360)", rv_iv_min_ratio=0.6, window_1_start=15, window_1_end=50, window_2_start=250, window_2_end=360)

print("\nDONE")

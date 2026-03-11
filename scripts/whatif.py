#!/usr/bin/env python3
"""Quick what-if scenarios - remaining tests."""
import sys; sys.path.insert(0, ".")
import pandas as pd
from trading_engine.config import EngineConfig as EC
from trading_engine.data.scalp_backtester import ScalpBacktester as SB

df = pd.read_csv("data/intraday/SPY_ibkr_1m_180d.csv", parse_dates=["timestamp"], index_col="timestamp")
df.index = pd.to_datetime(df.index, utc=True)

def test(label, **overrides):
    cc = EC()
    for k, v in overrides.items():
        setattr(cc.scalp, k, v)
    r = SB(config=cc, account_size=10000, spx_mode=True).run(df, ticker="SPY", interval="1m", verbose=False)
    pf = "%.2f" % r.profit_factor if r.profit_factor < 100 else "INF"
    print(f"  {label:55s}: t={r.total_trades:2d} WR={r.win_rate:.1f}% PF={pf:>6s} PnL=${r.total_pnl:+,.0f}")

print("=== CURRENT ===")
test("BASELINE")

print("\n=== RELAXING FILTERS ===")
test("Cooldown=5 (from 15)", cooldown_bars=5)
test("Cooldown=0", cooldown_bars=0)
test("Max trades=5/day", max_trades_per_day=5)
test("Max trades=10/day", max_trades_per_day=10)
test("Lower volume 1.2x (from 1.5x)", volume_surge_mult=1.2)
test("Lower volume 1.0x", volume_surge_mult=1.0)
test("Chop wider (0.05%)", chop_ema_pct=0.0005, chop_vwap_pct=0.0005)
test("Chop OFF", chop_ema_pct=0.0, chop_vwap_pct=0.0)
test("All-day window (20-360)", window_1_start=20, window_1_end=360, window_2_start=9999, window_2_end=9999)

print("\n=== INCREASING SIGNALS (combos) ===")
test("vol=1.2x + cool=5", volume_surge_mult=1.2, cooldown_bars=5)
test("vol=1.2x + cool=5 + maxT=5", volume_surge_mult=1.2, cooldown_bars=5, max_trades_per_day=5)
test("vol=1.0x + cool=5 + maxT=5", volume_surge_mult=1.0, cooldown_bars=5, max_trades_per_day=5)
test("vol=1.2x + all-day", volume_surge_mult=1.2, window_1_start=20, window_1_end=360, window_2_start=9999, window_2_end=9999)
test("vol=1.2x + cool=5 + all-day", volume_surge_mult=1.2, cooldown_bars=5, window_1_start=20, window_1_end=360, window_2_start=9999, window_2_end=9999)

print("\n=== IV FILTER VARIANTS WITH MORE SIGNALS ===")
test("IV=0.7 PM, 0.9 AM, vol=1.2, cool=5", rv_iv_min_ratio=0.7, rv_iv_min_ratio_w1=0.9, volume_surge_mult=1.2, cooldown_bars=5)
test("IV=0.6 PM, 0.8 AM, vol=1.2, cool=5", rv_iv_min_ratio=0.6, rv_iv_min_ratio_w1=0.8, volume_surge_mult=1.2, cooldown_bars=5)
test("IV=0.7, vol=1.2, cool=5, maxT=5", rv_iv_min_ratio=0.7, rv_iv_min_ratio_w1=0.9, volume_surge_mult=1.2, cooldown_bars=5, max_trades_per_day=5)
test("IV=0.6, vol=1.2, cool=5, all-day", rv_iv_min_ratio=0.6, rv_iv_min_ratio_w1=0.8, volume_surge_mult=1.2, cooldown_bars=5, window_1_start=20, window_1_end=360, window_2_start=9999, window_2_end=9999)

print("\nDONE")

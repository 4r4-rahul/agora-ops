#!/usr/bin/env python3
"""Final sweep — windows, runner, and combined best configs."""
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
    print(f"  {label:45s}: t={r.total_trades:2d} WR={r.win_rate:.1f}% PF={pf:>6s} PnL=${r.total_pnl:+,.0f} s=${r.scalp_pnl:+,.0f} r=${r.runner_pnl:+,.0f}")

print("--- Windows ---")
test("default(20-60,270-360)")
test("wider_both(15-50,250-350)", window_1_start=15, window_1_end=50, window_2_start=250, window_2_end=350)
test("w1_only(20-60)", window_2_start=9999, window_2_end=9999)
test("w2_only(270-360)", window_1_start=9999, window_1_end=9999)
test("long_w2(250-360)", window_2_start=250, window_2_end=360)

print("\n--- Runner ---")
test("runner_OFF", runner_enabled=False)
test("runner_wide_250-350", runner_window_start=250, runner_window_end=350)

print("\n--- Daily Loss Limit ---")
for dll in [300, 500, 700, 1000]:
    test(f"daily_loss={dll}", daily_loss_limit=dll)

print("\n\n========== COMBINED BEST CONFIGS ==========")
test("BASELINE_CURRENT")
test("combo1: wider_w2(250-360)", window_2_start=250, window_2_end=360)
test("combo2: runner_OFF", runner_enabled=False)
test("combo3: runner_OFF + wider_w2", runner_enabled=False, window_2_start=250, window_2_end=360)
test("combo4: max_contracts=5", max_contracts=5)
test("combo5: no_reentry=False", no_reentry_same_direction=False)
test("combo6: max_trades=4/day", max_trades_per_day=4)
test("combo7: max_trades=5/day", max_trades_per_day=5)
test("combo8: max_trades=4 + cool=5", max_trades_per_day=4, cooldown_bars=5)
test("combo9: eod=5 + wider_w2(250-360)", eod_exit_minutes=5, window_2_start=250, window_2_end=360)
test("combo10: IV=0.7 ratio", rv_iv_min_ratio=0.7)
test("combo11: IV=0.7 + wider_w2", rv_iv_min_ratio=0.7, window_2_start=250, window_2_end=360)
test("combo12: IV=0.7 + maxT=4 + cool=5", rv_iv_min_ratio=0.7, max_trades_per_day=4, cooldown_bars=5)
test("combo13: IV=0.6 ratio (more trades)", rv_iv_min_ratio=0.6)
test("combo14: IV=0.6 + wider_w2", rv_iv_min_ratio=0.6, window_2_start=250, window_2_end=360)

print("\nDONE")

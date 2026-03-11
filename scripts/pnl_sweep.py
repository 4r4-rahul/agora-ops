#!/usr/bin/env python3
"""Focused P&L improvement sweep — key levers only."""
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
    print(f"  {label:40s}: t={r.total_trades:2d} WR={r.win_rate:.1f}% PF={pf:>6s} PnL=${r.total_pnl:+,.0f} s=${r.scalp_pnl:+,.0f} r=${r.runner_pnl:+,.0f}")
    return r.total_pnl

print("=== BASELINE (current defaults) ===")
test("CURRENT")

# 1) Stop-loss sweep
print("\n--- Stop-Loss ATR Mult ---")
for sl in [2.0, 2.5, 3.0, 3.5, 4.0, 5.0]:
    test(f"SL={sl}xATR", stop_atr_mult=sl)

# 2) Profit target sweep
print("\n--- Profit Target ATR Mult ---")
for tp in [2.5, 3.0, 3.5, 4.0, 5.0, 6.0]:
    test(f"TP={tp}xATR", profit_target_atr_mult=tp, trailing_activation_atr=tp)

# 3) SL/TP combos
print("\n--- Key SL/TP Combos ---")
for sl, tp in [(2.5,4.0), (2.5,5.0), (3.0,4.0), (3.0,5.0), (3.0,6.0), (3.5,5.0), (3.5,6.0), (4.0,6.0)]:
    test(f"SL={sl}/TP={tp} R:R={tp/sl:.1f}", stop_atr_mult=sl, profit_target_atr_mult=tp, trailing_activation_atr=tp)

# 4) Time stop
print("\n--- Time Stop ---")
for ts in [20, 25, 30, 40, 60]:
    test(f"time_stop={ts}min", time_stop_minutes=ts)

# 5) Max hold
print("\n--- Max Hold ---")
for mh in [45, 60, 90, 120]:
    test(f"max_hold={mh}min", max_hold_minutes=mh)

# 6) Trailing distance
print("\n--- Trail Distance ATR ---")
for td in [0.5, 1.0, 1.5, 2.0, 2.5]:
    test(f"trail_dist={td}xATR", trailing_distance_atr=td)

# 7) Confirmation threshold
print("\n--- Min Confirmations ---")
for mc in [2, 3, 4]:
    test(f"min_confirms={mc}", min_confirmations=mc)

# 8) Max contracts
print("\n--- Max Contracts ---")
for mc in [1, 2, 3, 5]:
    test(f"max_contracts={mc}", max_contracts=mc)

# 9) Cooldown
print("\n--- Cooldown Bars ---")
for cb in [5, 10, 15, 20]:
    test(f"cooldown={cb}bars", cooldown_bars=cb)

# 10) Windows + IV (combo)
print("\n--- Windows + IV combo ---")
test("wider_w1_15_50", window_1_start=15, window_1_end=50)
test("wider_w2_250_350", window_2_start=250, window_2_end=350)
test("wider_both", window_1_start=15, window_1_end=50, window_2_start=250, window_2_end=350)
test("w1_only(no_w2)", window_2_start=9999, window_2_end=9999)
test("w2_only(no_w1)", window_1_start=9999, window_1_end=9999)

# 11) Runner tier variants
print("\n--- Runner Config ---")
test("runner_OFF", runner_enabled=False)
test("runner_win2x_start=250", runner_window_start=250)
test("runner_wide_trail_2.0", runner_trail_distance_atr=2.0)
test("runner_tight_trail_1.0", runner_trail_distance_atr=1.0)
test("runner_5min_eod", runner_eod_exit_minutes=5)
test("runner_2min_eod", runner_eod_exit_minutes=2)

# 12) Daily loss limit
print("\n--- Daily Loss Limit ---")
for dll in [300, 500, 700, 1000]:
    test(f"daily_loss_limit={dll}", daily_loss_limit=dll)

# 13) EOD exit
print("\n--- EOD Exit Minutes ---")
for eod in [5, 10, 15, 20]:
    test(f"eod_exit={eod}min", eod_exit_minutes=eod)

print("\nDONE")

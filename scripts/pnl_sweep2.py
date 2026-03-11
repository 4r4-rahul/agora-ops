#!/usr/bin/env python3
"""Focused sweep part 2 — remaining params."""
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

# Time stop
print("--- Time Stop ---")
for ts in [25, 30, 40, 60]:
    test(f"time_stop={ts}min", time_stop_minutes=ts)

# Max hold
print("\n--- Max Hold ---")
for mh in [45, 60, 90, 120]:
    test(f"max_hold={mh}min", max_hold_minutes=mh)

# Trail distance
print("\n--- Trail Distance ---")
for td in [0.5, 1.0, 1.5, 2.0]:
    test(f"trail_dist={td}xATR", trailing_distance_atr=td)

# Min confirms
print("\n--- Min Confirmations ---")
for mc in [2, 3, 4]:
    test(f"min_confirms={mc}", min_confirmations=mc)

# Max contracts
print("\n--- Max Contracts ---")
for mc in [1, 2, 3, 5]:
    test(f"max_contracts={mc}", max_contracts=mc)

# Cooldown
print("\n--- Cooldown ---")
for cb in [5, 10, 15, 20]:
    test(f"cooldown={cb}bars", cooldown_bars=cb)

# Windows
print("\n--- Windows ---")
test("default(20-60,270-360)")
test("wider_w1(15-50)", window_1_start=15, window_1_end=50)
test("wider_w2(250-350)", window_2_start=250, window_2_end=350)
test("wider_both", window_1_start=15, window_1_end=50, window_2_start=250, window_2_end=350)
test("w1_only", window_2_start=9999, window_2_end=9999)
test("w2_only", window_1_start=9999, window_1_end=9999)
test("long_w2(250-360)", window_2_start=250, window_2_end=360)

# Runner
print("\n--- Runner Config ---")
test("runner_OFF", runner_enabled=False)
test("runner_trail_1.0", runner_trail_distance_atr=1.0)
test("runner_trail_2.0", runner_trail_distance_atr=2.0)
test("runner_wide_window(250-350)", runner_window_start=250, runner_window_end=350)

# Daily loss limit
print("\n--- Daily Loss Limit ---")
for dll in [300, 500, 700, 1000]:
    test(f"daily_loss={dll}", daily_loss_limit=dll)

# EOD exit
print("\n--- EOD Exit ---")
for eod in [5, 10, 15, 20]:
    test(f"eod_exit={eod}min", eod_exit_minutes=eod)

# ===== COMBINED BEST-OF-BREED =====
print("\n\n=== COMBINED OPTIMAL CONFIGS ===")
# Baseline
test("CURRENT_BASELINE")

# Try combining best params we've found
test("combo1: SL=3.0/TP=3.5 + trail=1.0", trailing_distance_atr=1.0)
test("combo2: cooldown=5", cooldown_bars=5)
test("combo3: cooldown=5 + trail=1.0", cooldown_bars=5, trailing_distance_atr=1.0)
test("combo4: max_hold=90", max_hold_minutes=90)
test("combo5: max_hold=90 + cool=5", max_hold_minutes=90, cooldown_bars=5)
test("combo6: eod=5min", eod_exit_minutes=5)
test("combo7: eod=5 + cool=5", eod_exit_minutes=5, cooldown_bars=5)
test("combo8: min_conf=2", min_confirmations=2)
test("combo9: mc=5 contracts", max_contracts=5)
test("combo10: mc=5 + cool=5", max_contracts=5, cooldown_bars=5)

print("\nDONE")

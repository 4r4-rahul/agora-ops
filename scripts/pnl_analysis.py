#!/usr/bin/env python3
"""Analyze current P&L breakdown to find improvement levers."""
import sys; sys.path.insert(0, ".")
import pandas as pd
from trading_engine.config import EngineConfig as EC
from trading_engine.data.scalp_backtester import ScalpBacktester as SB

df = pd.read_csv("data/intraday/SPY_ibkr_1m_180d.csv", parse_dates=["timestamp"], index_col="timestamp")
df.index = pd.to_datetime(df.index, utc=True)

# Current best config
c = EC()
c.scalp.iv_discount_enabled = True
c.scalp.rv_iv_min_ratio = 0.8
r = SB(config=c, account_size=10000, spx_mode=True).run(df, ticker="SPY", interval="1m", verbose=False)

print("=== CURRENT STATE ===")
print(f"Trades: {r.total_trades}, WR: {r.win_rate:.1f}%, PF: {r.profit_factor:.2f}, PnL: ${r.total_pnl:+,.0f}")
print(f"Scalp: {r.scalp_trades} trades, ${r.scalp_pnl:+,.0f}  |  Runner: {r.runner_trades} trades, ${r.runner_pnl:+,.0f}")
print(f"Avg win: ${r.avg_win:+,.0f}  Avg loss: ${r.avg_loss:+,.0f}")
print(f"Biggest win: ${r.biggest_win:+,.0f}  Biggest loss: ${r.biggest_loss:+,.0f}")
print(f"Avg hold: {r.avg_hold_minutes:.1f} min")
print(f"IV blocked: {r.iv_blocked_signals}, IV passed: {r.iv_passed_signals}")

print("\n=== EXIT BREAKDOWN ===")
for reason, stats in sorted(r.exit_stats.items()):
    cnt = stats.get("count", 0)
    pnl = stats.get("total_pnl", 0)
    avg = stats.get("avg_pnl", 0)
    print(f"  {reason:25s}: count={cnt:3d} pnl=${pnl:+,.0f}  avg=${avg:+,.0f}")

print("\n=== INDIVIDUAL TRADES ===")
total_wins = 0
total_losses = 0
for t in r.trades:
    tag = "R" if t.tier == "runner" else "S"
    d = getattr(t, "direction", "?")
    pnl = t.total_pnl
    if pnl > 0:
        total_wins += pnl
    else:
        total_losses += abs(pnl)
    print(f"  [{tag}] {t.entry_time:%m/%d %H:%M}->{t.exit_time:%H:%M} | {d:5s} | entry=${t.entry_underlying:.0f} exit=${t.exit_underlying:.0f} prem=${t.entry_premium:.1f}->{t.exit_premium:.1f} | pnl=${pnl:+,.0f} | {t.exit_reason} | {t.hold_minutes:.0f}m")

print(f"\nGross wins: ${total_wins:+,.0f}  Gross losses: ${total_losses:,.0f}")

# --- Now test COMBINATIONS ---
print("\n\n========================================")
print("=== IMPROVEMENT LEVER TESTS ===")
print("========================================")

# 1) IV filter + wider windows (from earlier finding)
print("\n--- 1. IV Filter + Wider Windows ---")
c = EC()
c.scalp.iv_discount_enabled = True
c.scalp.rv_iv_min_ratio = 0.8
c.scalp.window_1_start = 15
c.scalp.window_1_end = 50
c.scalp.window_2_start = 250
c.scalp.window_2_end = 350
r = SB(config=c, account_size=10000, spx_mode=True).run(df, ticker="SPY", interval="1m", verbose=False)
print(f"  trades={r.total_trades} WR={r.win_rate:.1f}% PF={r.profit_factor:.2f} PnL=${r.total_pnl:+,.0f} s=${r.scalp_pnl:+,.0f} r=${r.runner_pnl:+,.0f}")

# 2) Test different stop-loss levels (ATR multiplier)
print("\n--- 2. Stop-Loss ATR Mult Sweep ---")
for sl in [1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0]:
    c = EC()
    c.scalp.iv_discount_enabled = True
    c.scalp.rv_iv_min_ratio = 0.8
    c.scalp.stop_atr_mult = sl
    r = SB(config=c, account_size=10000, spx_mode=True).run(df, ticker="SPY", interval="1m", verbose=False)
    pf = "%.2f" % r.profit_factor if r.profit_factor < 100 else "INF"
    print(f"  SL={sl}xATR: trades={r.total_trades} WR={r.win_rate:.1f}% PF={pf} PnL=${r.total_pnl:+,.0f}")

# 3) Test different take-profit levels (ATR multiplier)
print("\n--- 3. Take-Profit ATR Mult Sweep ---")
for tp in [2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 6.0, 8.0]:
    c = EC()
    c.scalp.iv_discount_enabled = True
    c.scalp.rv_iv_min_ratio = 0.8
    c.scalp.profit_target_atr_mult = tp
    c.scalp.trailing_activation_atr = tp  # Match trailing to target
    r = SB(config=c, account_size=10000, spx_mode=True).run(df, ticker="SPY", interval="1m", verbose=False)
    pf = "%.2f" % r.profit_factor if r.profit_factor < 100 else "INF"
    print(f"  TP={tp}xATR: trades={r.total_trades} WR={r.win_rate:.1f}% PF={pf} PnL=${r.total_pnl:+,.0f}")

# 4) Test SL/TP combos (key R:R ratios)
print("\n--- 4. Stop/Target Combos (R:R sweep) ---")
for sl, tp in [(2.0,3.0), (2.0,4.0), (2.5,3.5), (2.5,5.0), (3.0,3.5), (3.0,4.0), (3.0,5.0), (3.0,6.0), (3.5,5.0), (4.0,6.0)]:
    c = EC()
    c.scalp.iv_discount_enabled = True
    c.scalp.rv_iv_min_ratio = 0.8
    c.scalp.stop_atr_mult = sl
    c.scalp.profit_target_atr_mult = tp
    c.scalp.trailing_activation_atr = tp
    r = SB(config=c, account_size=10000, spx_mode=True).run(df, ticker="SPY", interval="1m", verbose=False)
    pf = "%.2f" % r.profit_factor if r.profit_factor < 100 else "INF"
    rr = tp / sl
    print(f"  SL={sl}/TP={tp} (R:R={rr:.1f}): trades={r.total_trades} WR={r.win_rate:.1f}% PF={pf} PnL=${r.total_pnl:+,.0f}")

# 5) Runner config sweep
print("\n--- 5. Runner Trail Activation Sweep ---")
for act in [2.0, 2.5, 3.0, 4.0, 5.0]:
    for dist in [1.0, 1.5, 2.0]:
        c = EC()
        c.scalp.iv_discount_enabled = True
        c.scalp.rv_iv_min_ratio = 0.8
        c.scalp.runner_trail_activation_atr = act
        c.scalp.runner_trail_distance_atr = dist
        r = SB(config=c, account_size=10000, spx_mode=True).run(df, ticker="SPY", interval="1m", verbose=False)
        pf = "%.2f" % r.profit_factor if r.profit_factor < 100 else "INF"
        print(f"  act={act} dist={dist}: trades={r.total_trades} WR={r.win_rate:.1f}% PF={pf} PnL=${r.total_pnl:+,.0f} r_pnl=${r.runner_pnl:+,.0f}")

# 6) Test confirmation thresholds
print("\n--- 6. Confirmation Threshold Sweep ---")
for thr in [2, 3, 4, 5]:
    c = EC()
    c.scalp.iv_discount_enabled = True
    c.scalp.rv_iv_min_ratio = 0.8
    c.scalp.min_confirmations = thr
    r = SB(config=c, account_size=10000, spx_mode=True).run(df, ticker="SPY", interval="1m", verbose=False)
    pf = "%.2f" % r.profit_factor if r.profit_factor < 100 else "INF"
    print(f"  min_confirms={thr}: trades={r.total_trades} WR={r.win_rate:.1f}% PF={pf} PnL=${r.total_pnl:+,.0f}")

# 7) Time stop sweep
print("\n--- 7. Time Stop Sweep ---")
for ts in [15, 20, 25, 30, 40, 50, 60]:
    c = EC()
    c.scalp.iv_discount_enabled = True
    c.scalp.rv_iv_min_ratio = 0.8
    c.scalp.time_stop_minutes = ts
    r = SB(config=c, account_size=10000, spx_mode=True).run(df, ticker="SPY", interval="1m", verbose=False)
    pf = "%.2f" % r.profit_factor if r.profit_factor < 100 else "INF"
    print(f"  time_stop={ts}min: trades={r.total_trades} WR={r.win_rate:.1f}% PF={pf} PnL=${r.total_pnl:+,.0f}")

# 8) Max hold sweep
print("\n--- 8. Max Hold Minutes Sweep ---")
for mh in [30, 45, 60, 90, 120]:
    c = EC()
    c.scalp.iv_discount_enabled = True
    c.scalp.rv_iv_min_ratio = 0.8
    c.scalp.max_hold_minutes = mh
    r = SB(config=c, account_size=10000, spx_mode=True).run(df, ticker="SPY", interval="1m", verbose=False)
    pf = "%.2f" % r.profit_factor if r.profit_factor < 100 else "INF"
    print(f"  max_hold={mh}min: trades={r.total_trades} WR={r.win_rate:.1f}% PF={pf} PnL=${r.total_pnl:+,.0f}")

# 9) Max contracts sweep
print("\n--- 9. Max Contracts Sweep ---")
for mc in [1, 2, 3, 5, 7, 10]:
    c = EC()
    c.scalp.iv_discount_enabled = True
    c.scalp.rv_iv_min_ratio = 0.8
    c.scalp.max_contracts = mc
    r = SB(config=c, account_size=10000, spx_mode=True).run(df, ticker="SPY", interval="1m", verbose=False)
    pf = "%.2f" % r.profit_factor if r.profit_factor < 100 else "INF"
    print(f"  max_contracts={mc}: trades={r.total_trades} WR={r.win_rate:.1f}% PF={pf} PnL=${r.total_pnl:+,.0f}")

# 10) Cooldown bars
print("\n--- 10. Cooldown Bars Sweep ---")
for cb in [5, 10, 15, 20, 30]:
    c = EC()
    c.scalp.iv_discount_enabled = True
    c.scalp.rv_iv_min_ratio = 0.8
    c.scalp.cooldown_bars = cb
    r = SB(config=c, account_size=10000, spx_mode=True).run(df, ticker="SPY", interval="1m", verbose=False)
    pf = "%.2f" % r.profit_factor if r.profit_factor < 100 else "INF"
    print(f"  cooldown={cb}bars: trades={r.total_trades} WR={r.win_rate:.1f}% PF={pf} PnL=${r.total_pnl:+,.0f}")

print("\nDONE")

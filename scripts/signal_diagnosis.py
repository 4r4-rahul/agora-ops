#!/usr/bin/env python3
"""Diagnose WHERE signals are being blocked in the pipeline.
Counts signals at each stage to find the bottleneck."""
import sys; sys.path.insert(0, ".")
import pandas as pd
import numpy as np
from trading_engine.config import EngineConfig as EC
from trading_engine.scalper import SignalEngine
from trading_engine.data.scalp_backtester import ScalpBacktester as SB
import pytz

df = pd.read_csv("data/intraday/SPY_ibkr_1m_180d.csv", parse_dates=["timestamp"], index_col="timestamp")
df.index = pd.to_datetime(df.index, utc=True)

c = EC()
se = SignalEngine(c.scalp)

# Count signals at every stage
days = df.groupby(df.index.date)
et = pytz.timezone("US/Eastern")

total_bars_in_window = 0
total_component_votes = {"VWAP": 0, "EMA": 0, "BREAKOUT": 0, "VOLUME": 0, "ORB": 0}
total_signals_3plus = 0
total_signals_with_vol_vwap = 0
total_chop_blocked = 0
total_iv_blocked = 0
total_entered = 0
days_with_signal = 0
days_no_signal = 0

for day_date, day_bars in days:
    if len(day_bars) < 30:
        continue
    
    precomp = se.precompute_day_indicators(day_bars)
    n = len(day_bars)
    day_bars_index = day_bars.index
    closes = day_bars["close"].values
    
    # Estimate IV for this day
    day_iv = 0.0
    if n >= 30:
        log_rets = np.log(closes[1:30] / closes[:29])
        day_iv = np.std(log_rets, ddof=1) * np.sqrt(390 * 252)
    
    day_has_signal = False
    
    for i in range(20, n):
        local_time = day_bars_index[i].tz_convert(et) if hasattr(day_bars_index[i], 'tz_convert') else day_bars_index[i]
        open_time = local_time.replace(hour=9, minute=30, second=0)
        minutes_since_open = (local_time - open_time).total_seconds() / 60
        
        # Check if in window
        in_w1 = c.scalp.window_1_start <= minutes_since_open <= c.scalp.window_1_end
        in_w2 = c.scalp.window_2_start <= minutes_since_open <= c.scalp.window_2_end
        if not (in_w1 or in_w2):
            continue
        
        total_bars_in_window += 1
        price = closes[i]
        
        # Check individual components (manually replicate evaluate_fast logic)
        # Check chop
        ema9 = precomp["ema9"][i]
        ema21 = precomp["ema21"][i]
        vwap = precomp["vwap"][i]
        ema_gap = abs(ema9 - ema21) / price if price > 0 else 0
        vwap_gap = abs(price - vwap) / price if price > 0 else 0
        
        is_chop = (ema_gap < c.scalp.chop_ema_pct and vwap_gap < c.scalp.chop_vwap_pct)
        if is_chop:
            total_chop_blocked += 1
            continue
        
        # Try to get signal
        signal = se.evaluate_fast(precomp, i, price, "SPY", bars_index=day_bars_index)
        
        if signal is not None:
            total_signals_with_vol_vwap += 1
            day_has_signal = True
            
            # Check IV gate
            rv = precomp.get("rv", np.full(n, np.nan))
            current_rv = rv[i] if i < len(rv) else float("nan")
            if not np.isnan(current_rv) and day_iv > 0:
                rv_ratio = current_rv / day_iv
                # Determine which window for threshold
                w1_end = c.scalp.window_1_end
                if minutes_since_open <= w1_end:
                    required = c.scalp.rv_iv_min_ratio_w1
                else:
                    required = c.scalp.rv_iv_min_ratio
                
                if rv_ratio < required:
                    total_iv_blocked += 1
                else:
                    total_entered += 1
            else:
                total_entered += 1
    
    if day_has_signal:
        days_with_signal += 1
    else:
        days_no_signal += 1

total_days = days_with_signal + days_no_signal
print("=== SIGNAL PIPELINE DIAGNOSIS ===")
print(f"Total trading days: {total_days}")
print(f"Days with at least 1 valid signal: {days_with_signal} ({days_with_signal/total_days*100:.0f}%)")
print(f"Days with ZERO signals: {days_no_signal} ({days_no_signal/total_days*100:.0f}%)")
print()
print(f"Bars inside time windows: {total_bars_in_window}")
print(f"  Blocked by CHOP filter: {total_chop_blocked} ({total_chop_blocked/total_bars_in_window*100:.1f}%)")
print(f"  Passed chop, fired 3+ w/ VOL+VWAP: {total_signals_with_vol_vwap}")
print(f"  Blocked by IV discount: {total_iv_blocked}")
print(f"  Would enter (passed all gates): {total_entered}")
print()
print(f"Pipeline: {total_bars_in_window} window bars -> {total_bars_in_window - total_chop_blocked} non-chop -> {total_signals_with_vol_vwap} signals -> {total_entered} entries")
print(f"  Chop kills: {total_chop_blocked/total_bars_in_window*100:.1f}%")
if total_signals_with_vol_vwap > 0:
    print(f"  IV kills: {total_iv_blocked/total_signals_with_vol_vwap*100:.1f}% of signals")
print()

# Now test: what if we relax filters?
print("=== WHAT-IF SCENARIOS ===")

def run_test(label, **overrides):
    cc = EC()
    for k, v in overrides.items():
        setattr(cc.scalp, k, v)
    r = SB(config=cc, account_size=10000, spx_mode=True).run(df, ticker="SPY", interval="1m", verbose=False)
    pf = "%.2f" % r.profit_factor if r.profit_factor < 100 else "INF"
    print(f"  {label:50s}: t={r.total_trades:2d} WR={r.win_rate:.1f}% PF={pf:>6s} PnL=${r.total_pnl:+,.0f}")

run_test("CURRENT (AM=0.9, PM=0.8)")
run_test("No IV filter at all", iv_discount_enabled=False)
run_test("IV filter OFF + min_confirms=2", iv_discount_enabled=False, min_confirmations=2)
run_test("min_confirms=2 (keep IV)", min_confirmations=2)
run_test("Cooldown=5 (from 15)", cooldown_bars=5)
run_test("Cooldown=0", cooldown_bars=0)
run_test("Max trades=5/day", max_trades_per_day=5)
run_test("Max trades=10/day", max_trades_per_day=10)
run_test("All day window (20-360)", window_1_start=20, window_1_end=360, window_2_start=9999, window_2_end=9999)
run_test("Remove VOLUME mandatory", volume_surge_mult=0.01)
run_test("Lower volume threshold 1.2x", volume_surge_mult=1.2)
run_test("Chop filter off (0.0)", chop_ema_pct=0.0, chop_vwap_pct=0.0)
run_test("Wider chop (0.05%)", chop_ema_pct=0.0005, chop_vwap_pct=0.0005)
# Combos
run_test("vol=1.2 + cool=5 + maxT=5", volume_surge_mult=1.2, cooldown_bars=5, max_trades_per_day=5)
run_test("vol=1.2 + cool=5 + maxT=5 + IV off", volume_surge_mult=1.2, cooldown_bars=5, max_trades_per_day=5, iv_discount_enabled=False)

print("\nDONE")

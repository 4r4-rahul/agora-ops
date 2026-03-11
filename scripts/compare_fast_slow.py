#!/usr/bin/env python3
"""Compare fast vs slow signal evaluation to find discrepancies."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
import numpy as np
from trading_engine.config import EngineConfig, ScalpConfig
from trading_engine.scalper import SignalEngine

# Load data
df = pd.read_csv('data/intraday/SPY_ibkr_1m_180d.csv', index_col=0)
df.index = pd.to_datetime(df.index, utc=True)

# Scale to SPX
for col in ["open", "high", "low", "close"]:
    df[col] = df[col] * 10.0
if "vwap" in df.columns:
    df["vwap"] = df["vwap"] * 10.0

cfg = EngineConfig()
engine = SignalEngine(cfg.scalp, bar_minutes=1)

# Pick one day
days = {}
for d, group in df.groupby(df.index.date):
    if len(group) >= 30:
        days[d] = group

day_date = sorted(days.keys())[5]  # Pick 6th day
day_list = sorted(days.keys())
print(f"Testing {len(day_list)} days...")

total_mismatches = 0
total_slow = 0
total_fast = 0
total_bars = 0

for day_idx, day_date in enumerate(day_list):
    day_bars = days[day_date]
    precomp = engine.precompute_day_indicators(day_bars)
    
    for i in range(30, len(day_bars)):
        price = float(day_bars["close"].iloc[i])
        
        bars_so_far = day_bars.iloc[:i + 1].copy()
        slow_sig = engine.evaluate(bars_so_far, price, "SPX")
        fast_sig = engine.evaluate_fast(precomp, i, price, "SPX", bars_index=day_bars.index)
        
        slow_dir = slow_sig.direction if slow_sig else None
        fast_dir = fast_sig.direction if fast_sig else None
        
        if slow_dir != fast_dir:
            total_mismatches += 1
            if total_mismatches <= 5:
                print(f"  Day {day_date} Bar {i}: slow={slow_dir}, fast={fast_dir}")
        
        if slow_dir:
            total_slow += 1
        if fast_dir:
            total_fast += 1
        total_bars += 1
    
    if (day_idx + 1) % 20 == 0:
        print(f"  ... {day_idx+1}/{len(day_list)} days done, mismatches so far: {total_mismatches}")

print(f"\n  TOTAL bars checked: {total_bars}")
print(f"  Slow signals: {total_slow}")
print(f"  Fast signals: {total_fast}")
print(f"  Mismatches: {total_mismatches}")
print(f"  Match rate: {(1 - total_mismatches/total_bars)*100:.4f}%")

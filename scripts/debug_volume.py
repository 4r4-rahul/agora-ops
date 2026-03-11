#!/usr/bin/env python3
"""Debug volume discrepancy at bar 132."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
import numpy as np
from trading_engine.config import EngineConfig
from trading_engine.scalper import SignalEngine

# Load data
df = pd.read_csv('data/intraday/SPY_ibkr_1m_180d.csv', index_col=0)
df.index = pd.to_datetime(df.index, utc=True)
for col in ["open", "high", "low", "close"]:
    df[col] = df[col] * 10.0
if "vwap" in df.columns:
    df["vwap"] = df["vwap"] * 10.0

cfg = EngineConfig()
engine = SignalEngine(cfg.scalp, bar_minutes=1)

days = {}
for d, group in df.groupby(df.index.date):
    if len(group) >= 30:
        days[d] = group

day_date = sorted(days.keys())[5]
day_bars = days[day_date]
i = 132

# Slow path volume check
bars_so_far = day_bars.iloc[:i + 1].copy()
vol_lookback = engine.vol_lookback
vol_recent = engine.vol_recent
print(f"vol_lookback={vol_lookback}, vol_recent={vol_recent}")

min_bars_slow = vol_lookback + vol_recent + 5
print(f"min_bars for slow: {min_bars_slow}, bars available: {len(bars_so_far)}")

# Slow volume computation
avg_vol_slow = bars_so_far["volume"].iloc[-(vol_lookback + 5):-5].mean()
recent_vol_slow = bars_so_far["volume"].iloc[-vol_recent:].mean()
vol_ratio_slow = recent_vol_slow / avg_vol_slow if avg_vol_slow > 0 else 0
net_move_slow = bars_so_far["close"].iloc[-1] - bars_so_far["close"].iloc[-vol_recent - 1]

print(f"\nSlow path:")
print(f"  avg_vol: {avg_vol_slow:.2f}")
print(f"  recent_vol: {recent_vol_slow:.2f}")
print(f"  vol_ratio: {vol_ratio_slow:.4f}")
print(f"  surge_mult threshold: {engine.cfg.volume_surge_mult}")
print(f"  net_move: {net_move_slow:.4f}")

# Slow uses iloc from end of bars_so_far (which is 133 bars long, indices 0-132)
# avg_vol slice: iloc[-(20+5):-5] = iloc[-25:-5] = bars[108:128]
# recent_vol slice: iloc[-2:] = bars[131:133] (bars 131-132)
print(f"\n  avg_vol from bars[{len(bars_so_far)-vol_lookback-5}:{len(bars_so_far)-5}]")
print(f"  recent_vol from bars[{len(bars_so_far)-vol_recent}:{len(bars_so_far)}]")

# Fast path volume computation
precomp = engine.precompute_day_indicators(day_bars)
avg_vol_fast = precomp['vol_avg'][i]
recent_vol_fast = precomp['vol_recent'][i]

print(f"\nFast path:")
print(f"  avg_vol: {avg_vol_fast}")
print(f"  recent_vol: {recent_vol_fast}")

# The fast path computes from day_bars (which has 390 bars, index 0-389)
# At position i=132: vol_avg[132] = volume[132-20-5:132-5] = volume[107:127]
# recent_vol[132] = volume[132-2:132] = volume[130:132]
vl = engine.vol_lookback
vr = engine.vol_recent

volume = day_bars["volume"].values
print(f"\nFast expected:")
print(f"  avg_vol from volume[{i - vl - 5}:{i - 5}] = {volume[i-vl-5:i-5].mean():.2f}")
print(f"  recent from volume[{i - vr}:{i}] = {volume[i-vr:i].mean():.2f}")

# The slow path slices from bars_so_far[0:133]
# Which maps to day_bars[0:133]
# avg_vol: bars_so_far.iloc[-25:-5] = day_bars.iloc[108:128] 
# recent: bars_so_far.iloc[-2:] = day_bars.iloc[131:133]

# The fast path slices from day_bars with absolute indices
# avg_vol: volume[107:127]  
# recent: volume[130:132]

# They differ!
print(f"\nSlow slices (using negative indexing on 133 bars):")
print(f"  avg: day_bars[{133-25}:{133-5}] = day_bars[108:128]")
print(f"  recent: day_bars[{133-2}:133] = day_bars[131:133]")
print(f"\nFast slices (using absolute index 132):")
print(f"  avg: day_bars[{132-20-5}:{132-5}] = day_bars[107:127]")
print(f"  recent: day_bars[{132-2}:{132}] = day_bars[130:132]")

print(f"\n>>> Difference: slow uses len(bars_so_far)={len(bars_so_far)} for negative indexing")
print(f"    Fast uses i={i} for absolute indexing")
print(f"    bars_so_far = day_bars[:133] (0 through 132 inclusive)")
print(f"    len(bars_so_far) = 133, i = 132")
print(f"    Slow: -25 from end of 133 = index 108; -5 = index 128")
print(f"    Fast: i - vl - 5 = 132-20-5 = 107;  i - 5 = 127")
print(f"    OFF BY ONE in fast path!")

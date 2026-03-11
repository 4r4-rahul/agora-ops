#!/usr/bin/env python3
"""Quick count: how many signals fire with tiered mandatory."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
import numpy as np
from collections import Counter
from trading_engine.config import EngineConfig
from trading_engine.scalper import SignalEngine

data_path = os.path.join("data", "intraday", "SPY_ibkr_1m_180d.csv")
df = pd.read_csv(data_path, parse_dates=["timestamp"], index_col="timestamp")
df.index = pd.to_datetime(df.index, utc=True)
for col in ["open", "high", "low", "close"]:
    df[col] = df[col] * 10.0

cfg = EngineConfig()
engine = SignalEngine(cfg.scalp, bar_minutes=1)

days = []
for d, group in df.groupby(df.index.date):
    if len(group) >= 30:
        days.append((d, group))
days.sort(key=lambda x: x[0])

w1_start, w1_end = cfg.scalp.window_1_start, cfg.scalp.window_1_end
w2_start, w2_end = cfg.scalp.window_2_start, cfg.scalp.window_2_end

# Count signals under different mandatory schemes
old_strict = 0     # VOL+VWAP mandatory (old)
new_tiered = 0     # VOL+VWAP at 3, VOL only at 4+ (new)
tiered_only = 0    # Only fire under tiered (new signals)
combo_counts = Counter()
prev_day_high = None
prev_day_low = None

for day_date, day_bars in days:
    precomp = engine.precompute_day_indicators(day_bars)
    precomp['prev_day_high'] = prev_day_high
    precomp['prev_day_low'] = prev_day_low
    
    close = precomp['close']
    high = precomp['high']
    low = precomp['low']
    vwap = precomp['vwap']
    ema9 = precomp['ema9']
    ema21 = precomp['ema21']
    n = len(day_bars)
    
    for i in range(30, n):
        ts = day_bars.index[i]
        if hasattr(ts, 'tzinfo') and ts.tzinfo is not None:
            import pytz
            et = pytz.timezone("US/Eastern")
            local_time = ts.astimezone(et)
            open_time = local_time.replace(hour=9, minute=30, second=0)
            mso = (local_time - open_time).total_seconds() / 60
        else:
            mso = i
        
        in_w1 = w1_start <= mso <= w1_end
        in_w2 = w2_start <= mso <= w2_end
        if not (in_w1 or in_w2):
            continue
            
        price = float(close[i])
        atr_val = precomp['atr'][i]
        if np.isnan(atr_val) or atr_val <= 0 or atr_val < cfg.scalp.min_atr:
            continue
        
        # Chop check
        cur_ema9 = ema9[i]
        cur_ema21 = ema21[i]
        cur_vwap = vwap[i]
        ema_gap_pct = abs(cur_ema9 - cur_ema21) / price if price > 0 else 0
        if not np.isnan(cur_vwap) and cur_vwap > 0:
            vwap_dist_pct = abs(price - cur_vwap) / price if price > 0 else 0
            if ema_gap_pct < cfg.scalp.chop_ema_pct and vwap_dist_pct < cfg.scalp.chop_vwap_pct:
                continue
        
        bullish = []
        bearish = []
        
        v = engine._check_vwap_fast(close, vwap, i, price)
        if v == "BULL": bullish.append("VWAP")
        elif v == "BEAR": bearish.append("VWAP")
        
        e = engine._check_ema_trend_fast(ema9, ema21, i, price)
        if e == "BULL": bullish.append("EMA")
        elif e == "BEAR": bearish.append("EMA")
        
        b = engine._check_breakout_fast(close, high, low, i, price)
        if b == "BULL": bullish.append("BREAKOUT")
        elif b == "BEAR": bearish.append("BREAKOUT")
        
        vol = engine._check_volume_fast(precomp, close, i)
        if vol == "BULL": bullish.append("VOLUME")
        elif vol == "BEAR": bearish.append("VOLUME")
        
        orb = engine._check_orb_fast(precomp, i, price)
        if orb == "BULL": bullish.append("ORB")
        elif orb == "BEAR": bearish.append("ORB")
        
        cm = engine._check_candle_momentum_fast(precomp, close, high, low, i)
        if cm == "BULL": bullish.append("CANDLE_MOM")
        elif cm == "BEAR": bearish.append("CANDLE_MOM")
        
        rb = engine._check_range_breakout_fast(close, high, low, i)
        if rb == "BULL": bullish.append("RANGE_BRK")
        elif rb == "BEAR": bearish.append("RANGE_BRK")
        
        ph = engine._check_prev_day_hl_fast(precomp, close, i, price)
        if ph == "BULL": bullish.append("PREV_HL")
        elif ph == "BEAR": bearish.append("PREV_HL")
        
        for direction, sigs in [("BULL", bullish), ("BEAR", bearish)]:
            if len(sigs) < 3:
                continue
            has_vol = "VOLUME" in sigs
            has_vwap = "VWAP" in sigs
            
            # Old: strict VOL+VWAP
            old_fires = has_vol and has_vwap and len(sigs) >= 3
            # New: tiered
            if len(sigs) >= 4:
                new_fires = has_vol  # Only need VOLUME at 4+
            else:
                new_fires = has_vol and has_vwap  # Need both at 3
            
            if old_fires:
                old_strict += 1
            if new_fires:
                new_tiered += 1
                if not old_fires:
                    tiered_only += 1
                    combo = "+".join(sorted(sigs))
                    combo_counts[combo] += 1

    prev_day_high = float(day_bars['high'].max())
    prev_day_low = float(day_bars['low'].min())

print("=" * 60)
print("TIERED MANDATORY SIGNAL COUNT")
print("=" * 60)
print(f"Old (strict VOL+VWAP):     {old_strict} signal-bars")
print(f"New (tiered mandatory):     {new_tiered} signal-bars")
print(f"Purely new (tiered-only):   {tiered_only} signal-bars")
print(f"\nNew signal combos (no VWAP, 4+ components):")
for combo, cnt in combo_counts.most_common(20):
    print(f"  {combo}: {cnt}")

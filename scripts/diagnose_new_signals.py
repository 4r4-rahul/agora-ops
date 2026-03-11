#!/usr/bin/env python3
"""Diagnose where new signal components fire vs old ones."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
import numpy as np
from trading_engine.config import EngineConfig
from trading_engine.scalper import SignalEngine

# Load data
data_path = os.path.join("data", "intraday", "SPY_ibkr_1m_180d.csv")
df = pd.read_csv(data_path, parse_dates=["timestamp"], index_col="timestamp")
df.index = pd.to_datetime(df.index, utc=True)

# Scale to SPX
for col in ["open", "high", "low", "close"]:
    df[col] = df[col] * 10.0

cfg = EngineConfig()
engine = SignalEngine(cfg.scalp, bar_minutes=1)

# Split into days
days = []
for d, group in df.groupby(df.index.date):
    if len(group) >= 30:
        days.append((d, group))
days.sort(key=lambda x: x[0])

# Windows
w1_start, w1_end = cfg.scalp.window_1_start, cfg.scalp.window_1_end
w2_start, w2_end = cfg.scalp.window_2_start, cfg.scalp.window_2_end

# Count by component firing
from collections import Counter
old_components = {"VWAP", "EMA", "BREAKOUT", "VOLUME", "ORB"}
new_components = {"CANDLE_MOM", "RANGE_BRK", "PREV_HL"}

stats = Counter()
combo_counts = Counter()
new_signal_bars = []  # Bars where new components create a signal that old ones didn't

prev_day_high = None
prev_day_low = None

for day_date, day_bars in days:
    n = len(day_bars)
    precomp = engine.precompute_day_indicators(day_bars)
    precomp['prev_day_high'] = prev_day_high
    precomp['prev_day_low'] = prev_day_low
    
    close = precomp['close']
    high = precomp['high']
    low = precomp['low']
    vwap = precomp['vwap']
    ema9 = precomp['ema9']
    ema21 = precomp['ema21']
    
    for i in range(30, n):
        ts = day_bars.index[i]
        # Minutes since open
        if hasattr(ts, 'tzinfo') and ts.tzinfo is not None:
            import pytz
            et = pytz.timezone("US/Eastern")
            local_time = ts.astimezone(et)
            open_time = local_time.replace(hour=9, minute=30, second=0)
            minutes_since_open = (local_time - open_time).total_seconds() / 60
        else:
            minutes_since_open = i
        
        # Time window check
        in_w1 = w1_start <= minutes_since_open <= w1_end
        in_w2 = w2_start <= minutes_since_open <= w2_end
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
        emas_tangled = ema_gap_pct < cfg.scalp.chop_ema_pct
        if not np.isnan(cur_vwap) and cur_vwap > 0:
            vwap_dist_pct = abs(price - cur_vwap) / price if price > 0 else 0
            price_on_vwap = vwap_dist_pct < cfg.scalp.chop_vwap_pct
        else:
            price_on_vwap = False
        if emas_tangled and price_on_vwap:
            continue
        
        stats['total_non_chop_window_bars'] += 1
        
        # Check each component individually
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
        
        # Check both directions
        for direction, sigs in [("BULL", bullish), ("BEAR", bearish)]:
            if len(sigs) < 2:
                continue
            
            has_vol = "VOLUME" in sigs
            has_vwap = "VWAP" in sigs
            old_sigs = [s for s in sigs if s in old_components]
            new_sigs = [s for s in sigs if s in new_components]
            
            if has_vol and has_vwap:
                stats['bars_with_vol_vwap'] += 1
                old_other = [s for s in old_sigs if s not in {"VOLUME", "VWAP"}]
                
                # Old system: would fire if len(old_sigs) >= 3 (i.e., 1+ non-VOL/VWAP)
                old_would_fire = len(old_sigs) >= 3
                # New system: would fire if total sigs >= 3
                new_would_fire = len(sigs) >= 3
                
                if old_would_fire:
                    stats['old_signals'] += 1
                if new_would_fire:
                    stats['new_signals'] += 1
                if new_would_fire and not old_would_fire:
                    stats['purely_new_signals'] += 1
                    new_signal_bars.append({
                        'date': day_date,
                        'time': ts,
                        'direction': direction,
                        'old_sigs': old_sigs,
                        'new_sigs': new_sigs,
                        'all_sigs': sigs,
                    })
                
                # Track: VOL+VWAP but only 0 old others → need 1+ new to fire
                if len(old_other) == 0:
                    stats['vol_vwap_only_0old'] += 1
                    if len(new_sigs) >= 1:
                        stats['vol_vwap_0old_1new+'] += 1
                elif len(old_other) == 1:
                    stats['vol_vwap_only_1old'] += 1
                    # With old system: 3 (VOL+VWAP+1) → fires
                    # With new system: also fires (3+)
                elif len(old_other) >= 2:
                    stats['vol_vwap_2old+'] += 1
                
                # Specific new component firing
                for ns in new_sigs:
                    stats[f'new_{ns}_fired_with_vol_vwap'] += 1
    
    prev_day_high = float(day_bars['high'].max())
    prev_day_low = float(day_bars['low'].min())

print("=" * 60)
print("SIGNAL COMPONENT DIAGNOSIS")
print("=" * 60)
print(f"\nTotal non-chop window bars: {stats['total_non_chop_window_bars']}")
print(f"\nBars with VOL+VWAP firing (same direction): {stats['bars_with_vol_vwap']}")
print(f"  With 0 old others (EMA/BREAKOUT/ORB): {stats['vol_vwap_only_0old']}")
print(f"    → of those, 1+ new component fires: {stats['vol_vwap_0old_1new+']}")
print(f"  With 1 old other:                     {stats['vol_vwap_only_1old']}")
print(f"  With 2+ old others:                   {stats['vol_vwap_2old+']}")
print(f"\nOld signals (3+ with VOL+VWAP, old components only): {stats['old_signals']}")
print(f"New signals (3+ with VOL+VWAP, all components):       {stats['new_signals']}")
print(f"PURELY NEW signals (would NOT fire without new components): {stats['purely_new_signals']}")
print(f"\nNew component fire rates (when VOL+VWAP present):")
print(f"  CANDLE_MOM: {stats.get('new_CANDLE_MOM_fired_with_vol_vwap', 0)}")
print(f"  RANGE_BRK:  {stats.get('new_RANGE_BRK_fired_with_vol_vwap', 0)}")
print(f"  PREV_HL:    {stats.get('new_PREV_HL_fired_with_vol_vwap', 0)}")

if new_signal_bars:
    print(f"\n{'='*60}")
    print(f"PURELY NEW SIGNAL BARS (first 20):")
    print(f"{'='*60}")
    for bar in new_signal_bars[:20]:
        print(f"  {bar['date']} {bar['time']} {bar['direction']}")
        print(f"    Old: {bar['old_sigs']} | New: {bar['new_sigs']}")

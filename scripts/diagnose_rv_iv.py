#!/usr/bin/env python3
"""Check RV/IV ratios for actual trade entries."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd, numpy as np
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

# Check RV/IV for bars that fire signals
prev_day_high, prev_day_low = None, None
for day_date, day_bars in days:
    precomp = engine.precompute_day_indicators(day_bars)
    precomp['prev_day_high'] = prev_day_high
    precomp['prev_day_low'] = prev_day_low
    
    # Estimate day IV
    from trading_engine.data.scalp_backtester import ScalpOptionPricer
    pricer = ScalpOptionPricer()
    day_iv = pricer.estimate_iv(day_bars.head(30), day_bars["close"].iloc[0])
    
    n = len(day_bars)
    for i in range(30, n):
        signal = engine.evaluate_fast(precomp, i, float(precomp['close'][i]), "SPX", day_bars.index)
        if signal:
            rv = precomp['rv'][i]
            if not np.isnan(rv) and day_iv > 0:
                ratio = rv / day_iv
                # Check time window
                ts = day_bars.index[i]
                if hasattr(ts, 'tzinfo') and ts.tzinfo is not None:
                    import pytz
                    et = pytz.timezone("US/Eastern")
                    lt = ts.astimezone(et)
                    ot = lt.replace(hour=9, minute=30, second=0)
                    mso = (lt - ot).total_seconds() / 60
                else:
                    mso = i
                
                in_w1 = cfg.scalp.window_1_start <= mso <= cfg.scalp.window_1_end
                in_w2 = cfg.scalp.window_2_start <= mso <= cfg.scalp.window_2_end
                if in_w1 or in_w2:
                    window = "AM" if in_w1 else "PM"
                    req = cfg.scalp.rv_iv_min_ratio_w1 if in_w1 else cfg.scalp.rv_iv_min_ratio
                    passes = ratio >= req
                    deeply_cheap = ratio >= cfg.scalp.rv_iv_premium_ratio
                    print(f"  {day_date} {window} RV/IV={ratio:.3f} req={req:.2f} "
                          f"{'✓ PASS' if passes else '✗ BLOCK'} "
                          f"{'💰 DEEPLY CHEAP' if deeply_cheap else ''} "
                          f"sigs={'+'.join(signal.confirmations)}")
    
    prev_day_high = float(day_bars['high'].max())
    prev_day_low = float(day_bars['low'].min())

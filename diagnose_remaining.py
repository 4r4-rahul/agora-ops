#!/usr/bin/env python3
"""Diagnose the remaining 67 zero-trade days after Phase 2a."""
import sys; sys.path.insert(0, '.')
import pandas as pd
import numpy as np
from trading_engine.config import EngineConfig
from trading_engine.data.scalp_backtester import ScalpBacktester
from trading_engine.regime import RegimeDetector

df = pd.read_csv('data/intraday/SPY_ibkr_1m_180d.csv', parse_dates=['timestamp'], index_col='timestamp')
df.index = pd.to_datetime(df.index, utc=True)

# Run current system
config = EngineConfig()
bt = ScalpBacktester(config=config, account_size=10_000.0, spx_mode=True)
r = bt.run(df, ticker='SPY', interval='1m', verbose=False)

# Identify trade days
trade_days = set()
for t in r.trades:
    trade_days.add(str(t.entry_time)[:10])

# Classify all days
rd = RegimeDetector()
all_days = sorted(df.index.normalize().unique())
print(f"Total days: {len(all_days)}, Days with trades: {len(trade_days)}, Days without: {len(all_days) - len(trade_days)}")

# Analyze no-trade days
regime_counts = {}
no_trade_details = []

for day_ts in all_days:
    day_str = str(day_ts)[:10]
    day_mask = df.index.normalize() == day_ts
    day_bars = df[day_mask]
    if len(day_bars) < 30:
        continue
    
    # Scale for SPX
    day_scaled = day_bars.copy()
    for col in ['open', 'high', 'low', 'close']:
        day_scaled[col] = day_scaled[col] * 10.0
    
    regime_info = rd.classify(day_scaled)
    regime = regime_info.regime
    
    if day_str in trade_days:
        continue
    
    # No-trade day
    c = day_bars['close'].values
    h = day_bars['high'].values
    l = day_bars['low'].values
    day_range = (h.max() - l.min()) / c[0] * 100
    
    # Compute VWAP deviation pattern
    v = day_bars['volume'].values
    tp = (h + l + c) / 3
    cum_tpv = np.cumsum(tp * v)
    cum_vol = np.cumsum(v)
    vwap = cum_tpv / np.where(cum_vol > 0, cum_vol, 1)
    max_vwap_dev = max(abs(c[i] - vwap[i]) / vwap[i] * 100 for i in range(30, len(c)))
    
    # Morning range (first 30 bars)
    am_range = (h[:30].max() - l[:30].min()) / c[0] * 100
    
    # Afternoon range (after bar 180)
    if len(c) > 200:
        pm_range = (h[180:].max() - l[180:].min()) / c[0] * 100
    else:
        pm_range = 0
    
    # Max consecutive move (any direction)
    max_run = 0
    cur_run = 0
    cur_dir = 0
    for i in range(1, len(c)):
        d = 1 if c[i] > c[i-1] else -1 if c[i] < c[i-1] else 0
        if d == cur_dir:
            cur_run += 1
        else:
            cur_run = 1
            cur_dir = d
        max_run = max(max_run, cur_run)
    
    regime_counts[regime] = regime_counts.get(regime, 0) + 1
    no_trade_details.append({
        'date': day_str, 'regime': regime,
        'day_range': day_range, 'am_range': am_range, 'pm_range': pm_range,
        'max_vwap_dev': max_vwap_dev, 'max_run': max_run,
        'bars': len(day_bars),
        'trend_ratio': regime_info.trend_ratio,
        'chop_rate': regime_info.chop_rate,
    })

print(f"\n=== NO-TRADE DAYS BY REGIME ===")
for regime, cnt in sorted(regime_counts.items(), key=lambda x: -x[1]):
    print(f"  {regime:20s}: {cnt:3d} days")

ntdf = pd.DataFrame(no_trade_details)

print(f"\n=== STATS PER REGIME (no-trade days) ===")
for regime in ntdf['regime'].unique():
    sub = ntdf[ntdf['regime'] == regime]
    print(f"\n  {regime} ({len(sub)} days):")
    print(f"    Day range:    avg={sub['day_range'].mean():.2f}%  med={sub['day_range'].median():.2f}%  max={sub['day_range'].max():.2f}%")
    print(f"    AM range:     avg={sub['am_range'].mean():.2f}%  med={sub['am_range'].median():.2f}%")
    print(f"    PM range:     avg={sub['pm_range'].mean():.2f}%  med={sub['pm_range'].median():.2f}%")
    print(f"    Max VWAP dev: avg={sub['max_vwap_dev'].mean():.3f}%  max={sub['max_vwap_dev'].max():.3f}%")
    print(f"    Max run:      avg={sub['max_run'].mean():.1f} bars")
    print(f"    Trend ratio:  avg={sub['trend_ratio'].mean():.2f}")
    print(f"    Chop rate:    avg={sub['chop_rate'].mean():.2f}")

# Which regimes are tradeable?
print(f"\n=== OPPORTUNITY ASSESSMENT ===")
for regime in ['DEAD_FLAT', 'MIXED', 'RANGE_BOUND', 'MODERATE_TREND', 'STRONG_TREND']:
    sub = ntdf[ntdf['regime'] == regime]
    if len(sub) == 0:
        continue
    big_range = sub[sub['day_range'] > 0.5]
    has_vwap = sub[sub['max_vwap_dev'] > 0.10]
    has_pm = sub[sub['pm_range'] > 0.3]
    print(f"  {regime:20s}: {len(sub)} days | range>0.5%: {len(big_range)} | vwap_dev>0.1%: {len(has_vwap)} | pm_range>0.3%: {len(has_pm)}")

# Show the DEAD_FLAT days sorted by day range
print(f"\n=== DEAD_FLAT DAYS (sorted by range) ===")
df_flat = ntdf[ntdf['regime'] == 'DEAD_FLAT'].sort_values('day_range', ascending=False)
for _, row in df_flat.iterrows():
    print(f"  {row['date']}  range={row['day_range']:.2f}%  am={row['am_range']:.2f}%  pm={row['pm_range']:.2f}%  vwap={row['max_vwap_dev']:.3f}%  run={row['max_run']}  trend={row['trend_ratio']:.2f}")

# Show MIXED days
print(f"\n=== MIXED no-trade DAYS ===")
df_mix = ntdf[ntdf['regime'] == 'MIXED'].sort_values('day_range', ascending=False)
for _, row in df_mix.iterrows():
    print(f"  {row['date']}  range={row['day_range']:.2f}%  am={row['am_range']:.2f}%  pm={row['pm_range']:.2f}%  vwap={row['max_vwap_dev']:.3f}%  run={row['max_run']}  trend={row['trend_ratio']:.2f}")

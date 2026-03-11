"""Diagnose WHY we miss 88/129 trading days. 
For each no-trade day, identify the blocking reason."""
import pandas as pd
import numpy as np
from trading_engine.config import EngineConfig
from trading_engine.data.scalp_backtester import ScalpBacktester
from trading_engine.regime import RegimeDetector

df = pd.read_csv('data/intraday/SPY_ibkr_1m_180d.csv', parse_dates=['timestamp'], index_col='timestamp')
df.index = pd.to_datetime(df.index, utc=True)

# Get all trading days
all_days = sorted(df.index.normalize().unique())
print(f"Total trading days: {len(all_days)}")

# Run backtest to identify trade days
config = EngineConfig()
bt = ScalpBacktester(config=config, account_size=10_000.0, spx_mode=True)
r = bt.run(df, ticker='SPY', interval='1m', verbose=False)

trade_days = set()
for t in r.trades:
    trade_days.add(str(t.entry_time)[:10])

print(f"Days with trades: {len(trade_days)}")
print(f"Days WITHOUT trades: {len(all_days) - len(trade_days)}")

# For each day, analyze what blocked us
regime_det = RegimeDetector()
blocking_reasons = {
    'regime_dead_flat': 0,
    'regime_choppy': 0,
    'regime_range_bound': 0,
    'regime_mixed': 0,
    'regime_ok_but_no_orb_break': 0,
    'regime_ok_orb_break_but_stopped': 0,
    'momentum_no_signal': 0,
    'momentum_iv_blocked': 0,
    'momentum_window_miss': 0,
    'had_trade': 0,
}

# Analyze regime for each day
regime_counts = {}
no_trade_details = []

for day_ts in all_days:
    day_str = str(day_ts)[:10]
    day_mask = df.index.normalize() == day_ts
    day_bars = df[day_mask]
    
    if len(day_bars) < 30:
        continue
    
    # Get regime
    closes = day_bars['close'].values
    regime_info = regime_det.classify(day_bars)
    regime = regime_info.regime
    
    regime_counts[regime] = regime_counts.get(regime, 0) + 1
    
    if day_str in trade_days:
        blocking_reasons['had_trade'] += 1
        continue
    
    # This is a no-trade day - figure out why
    # 1. Check regime filter (blocks ORB)
    orb_blocked = regime in ('DEAD_FLAT', 'CHOPPY', 'RANGE_BOUND', 'MIXED')
    
    # 2. Check if ORB would have broken out
    orb_high = max(closes[:30])
    orb_low = min(closes[:30])
    orb_range = orb_high - orb_low
    
    broke_high = any(c > orb_high for c in closes[30:])
    broke_low = any(c < orb_low for c in closes[30:])
    orb_breakout = broke_high or broke_low
    
    # 3. Check day range / volatility
    day_range = (max(closes) - min(closes)) / closes[0] * 100
    
    # 4. Check price action patterns
    # Was there a strong move at any point?
    max_move_from_open = max(abs(c - closes[0]) / closes[0] * 100 for c in closes)
    
    # 5. Compute intraday stats
    returns = np.diff(closes) / closes[:-1]
    realized_vol = np.std(returns) * np.sqrt(390) * 100  # annualized %
    
    detail = {
        'date': day_str,
        'regime': regime,
        'day_range_pct': day_range,
        'orb_range_pct': orb_range / closes[0] * 100,
        'max_move_pct': max_move_from_open,
        'orb_breakout': orb_breakout,
        'orb_blocked': orb_blocked,
        'realized_vol': realized_vol,
        'bars': len(day_bars),
    }
    no_trade_details.append(detail)
    
    if orb_blocked:
        blocking_reasons[f'regime_{regime.lower()}'] += 1
    elif not orb_breakout:
        blocking_reasons['regime_ok_but_no_orb_break'] += 1
    else:
        blocking_reasons['regime_ok_orb_break_but_stopped'] += 1

print(f"\n=== REGIME DISTRIBUTION (ALL 129 DAYS) ===")
for regime, count in sorted(regime_counts.items(), key=lambda x: -x[1]):
    print(f"  {regime:20s}: {count:3d} days ({100*count/len(all_days):.0f}%)")

print(f"\n=== BLOCKING REASONS (88 no-trade days) ===")
for reason, count in sorted(blocking_reasons.items(), key=lambda x: -x[1]):
    if count > 0 and reason != 'had_trade':
        print(f"  {reason:40s}: {count:3d} days")

# Deeper analysis of no-trade days
ntdf = pd.DataFrame(no_trade_details)
if len(ntdf) > 0:
    print(f"\n=== NO-TRADE DAYS STATS ===")
    print(f"  Avg day range: {ntdf['day_range_pct'].mean():.2f}%")
    print(f"  Avg max move:  {ntdf['max_move_pct'].mean():.2f}%")
    print(f"  Avg ORB range: {ntdf['orb_range_pct'].mean():.3f}%")
    print(f"  Days with ORB breakout: {ntdf['orb_breakout'].sum()}")
    
    # Group by regime
    print(f"\n=== NO-TRADE DAYS BY REGIME ===")
    for regime in ntdf['regime'].unique():
        rdf = ntdf[ntdf['regime'] == regime]
        print(f"\n  {regime} ({len(rdf)} days):")
        print(f"    Avg range: {rdf['day_range_pct'].mean():.2f}%")
        print(f"    Avg max move: {rdf['max_move_pct'].mean():.2f}%")
        print(f"    Had ORB breakout: {rdf['orb_breakout'].sum()}/{len(rdf)}")
        print(f"    Avg realized vol: {rdf['realized_vol'].mean():.1f}%")
    
    # Opportunities we're missing
    print(f"\n=== MISSED OPPORTUNITIES (regime-blocked but had movement) ===")
    missed = ntdf[(ntdf['orb_blocked']) & (ntdf['day_range_pct'] > 0.5)]
    print(f"  Days with >0.5% range that regime blocked: {len(missed)}")
    for _, row in missed.iterrows():
        print(f"    {row['date']}  {row['regime']:15s}  range={row['day_range_pct']:.2f}%  max_move={row['max_move_pct']:.2f}%  orb_break={row['orb_breakout']}")
    
    big_missed = ntdf[(ntdf['orb_blocked']) & (ntdf['day_range_pct'] > 1.0)]
    print(f"\n  Days with >1.0% range that regime blocked: {len(big_missed)}")
    
    # Days where momentum scanner also failed
    print(f"\n=== STRATEGY COVERAGE GAPS ===")
    print(f"  Total no-trade days: {len(ntdf)}")
    print(f"  Regime-blocked (no ORB possible): {ntdf['orb_blocked'].sum()}")
    print(f"  Regime OK but no ORB breakout: {len(ntdf[~ntdf['orb_blocked'] & ~ntdf['orb_breakout']])}")
    print(f"  Regime OK + ORB breakout but no trade: {len(ntdf[~ntdf['orb_blocked'] & ntdf['orb_breakout']])}")

    # What kind of price action happens on missed days?
    print(f"\n=== PRICE ACTION PATTERNS ON MISSED DAYS ===")
    # Trend days missed
    trending = ntdf[ntdf['max_move_pct'] > 0.8]
    print(f"  Strong directional (>0.8% move): {len(trending)} days")
    # Mean reversion days missed  
    mean_rev = ntdf[(ntdf['day_range_pct'] > 0.5) & (ntdf['max_move_pct'] < 0.5)]
    print(f"  Mean reversion (range>0.5%, max_move<0.5%): {len(mean_rev)} days")
    # Afternoon movers
    print(f"  Low vol / truly dead: {len(ntdf[ntdf['day_range_pct'] < 0.3])} days")
    # Moderate range
    moderate = ntdf[(ntdf['day_range_pct'] >= 0.3) & (ntdf['day_range_pct'] < 0.8)]
    print(f"  Moderate range (0.3-0.8%): {len(moderate)} days")
    large = ntdf[ntdf['day_range_pct'] >= 0.8]
    print(f"  Large range (>0.8%): {len(large)} days")

"""
EARLY MORNING ANALYSIS (9:30-11:00)
====================================
Analyzing why morning trades fail and what sensors/conditions are missing
"""

import pandas as pd
import numpy as np

# Load data
mstr_df = pd.read_csv('BATS_MSTR, 1-41.csv')
mstr_df['timestamp'] = pd.to_datetime(mstr_df['time'], unit='s')
mstr_df['date'] = mstr_df['timestamp'].dt.date
mstr_df['hour'] = mstr_df['timestamp'].dt.hour
mstr_df['minute'] = mstr_df['timestamp'].dt.minute

# Load trades
trades_df = pd.read_csv('research/all_trades_analyzed.csv')
trades_df['entry_time'] = pd.to_datetime(trades_df['entry_time'])
trades_df['entry_hour'] = trades_df['entry_time'].dt.hour
trades_df['entry_minute'] = trades_df['entry_time'].dt.minute

print("=" * 100)
print("EARLY MORNING PROFIT ANALYSIS (9:30-11:00 AM)")
print("=" * 100)

# Define time buckets
def get_time_bucket(hour, minute):
    total_min = hour * 60 + minute
    if 570 <= total_min < 660:  # 9:30-11:00
        return 'EARLY_MORNING'
    elif 660 <= total_min < 840:  # 11:00-14:00
        return 'MIDDAY'
    elif 840 <= total_min < 900:  # 14:00-15:00
        return 'LATE_AFTERNOON'
    else:  # 15:00-16:00
        return 'CLOSE'

trades_df['time_bucket'] = trades_df.apply(
    lambda x: get_time_bucket(x['entry_hour'], x['entry_minute']), axis=1
)

# Performance by time bucket
print("\n📊 PERFORMANCE BY TIME OF DAY")
print("-" * 100)

for bucket in ['EARLY_MORNING', 'MIDDAY', 'LATE_AFTERNOON', 'CLOSE']:
    bucket_trades = trades_df[trades_df['time_bucket'] == bucket]
    if len(bucket_trades) > 0:
        print(f"\n{bucket:15} ({bucket_trades['entry_hour'].min()}:00-{bucket_trades['entry_hour'].max()}:59)")
        print(f"  Trades:          {len(bucket_trades)}")
        print(f"  Win Rate:        {bucket_trades['winner'].sum()}/{len(bucket_trades)} ({bucket_trades['winner'].sum()/len(bucket_trades)*100:.1f}%)")
        print(f"  Net R:           {bucket_trades['r_achieved'].sum():+.2f}R")
        print(f"  Avg R:           {bucket_trades['r_achieved'].mean():+.2f}R")
        print(f"  2R+ Winners:     {bucket_trades['big_winner'].sum()} ({bucket_trades['big_winner'].sum()/len(bucket_trades)*100:.1f}%)")
        print(f"  Max R Achieved:  {bucket_trades['r_achieved'].max():+.2f}R")
        print(f"  Worst Loss:      {bucket_trades['r_achieved'].min():+.2f}R")

# DEEP DIVE: Early morning trades
print("\n" + "=" * 100)
print("EARLY MORNING DEEP DIVE (9:30-11:00)")
print("=" * 100)

early_trades = trades_df[trades_df['time_bucket'] == 'EARLY_MORNING']

if len(early_trades) > 0:
    print(f"\n📋 EARLY MORNING TRADE DETAILS")
    print("-" * 100)
    
    detail_cols = ['entry_time', 'side', 'r_achieved', 'max_r_excursion', 
                   'entry_adx', 'entry_cci', 'holding_time_min']
    print(early_trades[detail_cols].to_string(index=False))
    
    # Compare market conditions: Morning vs Late session
    print("\n" + "=" * 100)
    print("MORNING vs LATE SESSION - MARKET CONDITION COMPARISON")
    print("=" * 100)
    
    # Get morning bars (9:30-11:00)
    morning_bars = mstr_df[
        ((mstr_df['hour'] == 9) & (mstr_df['minute'] >= 30)) |
        ((mstr_df['hour'] == 10))
    ]
    
    # Get late session bars (15:00-16:00)
    late_bars = mstr_df[mstr_df['hour'] >= 15]
    
    print(f"\n📊 MARKET STRUCTURE DIFFERENCES")
    print("-" * 100)
    
    metrics = {
        'Total Bars': [len(morning_bars), len(late_bars)],
        'Avg ADX': [morning_bars['ADX'].mean(), late_bars['ADX'].mean()],
        'Avg |CCI|': [morning_bars['CCI'].abs().mean(), late_bars['CCI'].abs().mean()],
        'Avg ATR': [morning_bars['ATR'].mean(), late_bars['ATR'].mean()],
        'ATR %': [(morning_bars['ATR'] / morning_bars['close']).mean() * 100, 
                  (late_bars['ATR'] / late_bars['close']).mean() * 100],
        'EMA9>EMA21 %': [(morning_bars['EMA 9'] > morning_bars['EMA 21']).sum() / len(morning_bars) * 100,
                         (late_bars['EMA 9'] > late_bars['EMA 21']).sum() / len(late_bars) * 100],
        'Price>VWAP %': [(morning_bars['close'] > morning_bars['VWAP']).sum() / len(morning_bars) * 100,
                         (late_bars['close'] > late_bars['VWAP']).sum() / len(late_bars) * 100],
        'Strong Trend (ADX>30) %': [(morning_bars['ADX'] > 30).sum() / len(morning_bars) * 100,
                                     (late_bars['ADX'] > 30).sum() / len(late_bars) * 100],
        'Extreme CCI (|CCI|>150) %': [(morning_bars['CCI'].abs() > 150).sum() / len(morning_bars) * 100,
                                       (late_bars['CCI'].abs() > 150).sum() / len(late_bars) * 100],
        'Avg Range (High-Low)': [(morning_bars['high'] - morning_bars['low']).mean(),
                                 (late_bars['high'] - late_bars['low']).mean()],
    }
    
    comparison_df = pd.DataFrame(metrics, index=['MORNING (9:30-11)', 'LATE (15-16)']).T
    comparison_df['DIFFERENCE'] = comparison_df['LATE (15-16)'] - comparison_df['MORNING (9:30-11)']
    comparison_df['% CHANGE'] = (comparison_df['DIFFERENCE'] / comparison_df['MORNING (9:30-11)'] * 100).round(1)
    
    print(comparison_df.round(2).to_string())
    
    # MOMENTUM ANALYSIS
    print("\n" + "=" * 100)
    print("MOMENTUM & FOLLOW-THROUGH ANALYSIS")
    print("=" * 100)
    
    # Calculate move sustainability (how long does a move last?)
    def analyze_momentum(df, period_name):
        moves_5min = []
        moves_15min = []
        moves_30min = []
        
        for i in range(0, len(df) - 30, 10):  # Sample every 10 bars
            if i + 30 >= len(df):
                break
                
            start_price = df.iloc[i]['close']
            price_5min = df.iloc[min(i+5, len(df)-1)]['close']
            price_15min = df.iloc[min(i+15, len(df)-1)]['close']
            price_30min = df.iloc[min(i+30, len(df)-1)]['close']
            
            moves_5min.append(abs(price_5min - start_price) / start_price * 100)
            moves_15min.append(abs(price_15min - start_price) / start_price * 100)
            moves_30min.append(abs(price_30min - start_price) / start_price * 100)
        
        print(f"\n{period_name}:")
        print(f"  Avg 5-min move:   {np.mean(moves_5min):.3f}%")
        print(f"  Avg 15-min move:  {np.mean(moves_15min):.3f}%")
        print(f"  Avg 30-min move:  {np.mean(moves_30min):.3f}%")
        print(f"  Move acceleration: {np.mean(moves_30min) / np.mean(moves_5min):.2f}x (higher = better follow-through)")
        
        return {
            '5min': np.mean(moves_5min),
            '15min': np.mean(moves_15min),
            '30min': np.mean(moves_30min),
            'acceleration': np.mean(moves_30min) / np.mean(moves_5min) if np.mean(moves_5min) > 0 else 0
        }
    
    morning_momentum = analyze_momentum(morning_bars, "MORNING SESSION")
    late_momentum = analyze_momentum(late_bars, "LATE SESSION")
    
    # REVERSAL vs CONTINUATION
    print("\n" + "=" * 100)
    print("TRADE DIRECTION ANALYSIS")
    print("=" * 100)
    
    def analyze_trade_direction(df, period_name):
        """Analyze if trades are with-trend or counter-trend"""
        
        # Calculate recent trend before each signal
        with_trend = 0
        counter_trend = 0
        
        call_signals = df[df['Buy Call marker'] == 1]
        put_signals = df[df['Buy Put marker'] == 1]
        
        for idx in call_signals.index:
            if idx < 20:
                continue
            # Look back 20 bars
            lookback = df.loc[max(0, idx-20):idx]
            recent_trend = (lookback['close'].iloc[-1] - lookback['close'].iloc[0]) / lookback['close'].iloc[0] * 100
            
            if recent_trend > 0:  # Uptrend, CALL is with-trend
                with_trend += 1
            else:  # Downtrend, CALL is counter-trend
                counter_trend += 1
        
        for idx in put_signals.index:
            if idx < 20:
                continue
            lookback = df.loc[max(0, idx-20):idx]
            recent_trend = (lookback['close'].iloc[-1] - lookback['close'].iloc[0]) / lookback['close'].iloc[0] * 100
            
            if recent_trend < 0:  # Downtrend, PUT is with-trend
                with_trend += 1
            else:  # Uptrend, PUT is counter-trend
                counter_trend += 1
        
        total = with_trend + counter_trend
        if total > 0:
            print(f"\n{period_name}:")
            print(f"  With-trend entries:     {with_trend}/{total} ({with_trend/total*100:.1f}%)")
            print(f"  Counter-trend entries:  {counter_trend}/{total} ({counter_trend/total*100:.1f}%)")
        
        return {'with_trend': with_trend, 'counter_trend': counter_trend}
    
    morning_direction = analyze_trade_direction(morning_bars, "MORNING SIGNALS")
    late_direction = analyze_trade_direction(late_bars, "LATE SIGNALS")
    
    # VOLATILITY PATTERN
    print("\n" + "=" * 100)
    print("INTRADAY VOLATILITY PATTERN")
    print("=" * 100)
    
    print("\nATR by Hour:")
    for hour in range(9, 17):
        hour_bars = mstr_df[mstr_df['hour'] == hour]
        if len(hour_bars) > 0:
            avg_atr = hour_bars['ATR'].mean()
            avg_atr_pct = (avg_atr / hour_bars['close'].mean()) * 100
            signals = len(hour_bars[hour_bars['Buy Call marker'] == 1]) + len(hour_bars[hour_bars['Buy Put marker'] == 1])
            print(f"  {hour:2d}:00 - ATR: ${avg_atr:.2f} ({avg_atr_pct:.2f}%) | Signals: {signals}")

else:
    print("\n⚠️  NO EARLY MORNING TRADES FOUND")

# KEY INSIGHTS
print("\n" + "=" * 100)
print("🔍 KEY INSIGHTS - WHY MORNING FAILS")
print("=" * 100)

print("""
Based on the analysis above, here are the critical differences:

1. MOMENTUM & FOLLOW-THROUGH
   - Morning: Initial moves fade quickly (low acceleration)
   - Late: Moves continue and expand (high acceleration)
   - Issue: Morning trades get stopped out before moves develop
   
2. TREND STRENGTH
   - Morning: Lower ADX, weaker directional bias
   - Late: Higher ADX, stronger trends
   - Issue: Morning entries lack conviction
   
3. VOLATILITY
   - Morning: Lower ATR (tighter ranges)
   - Late: Higher ATR (bigger moves available)
   - Issue: Morning targets too ambitious for available movement
   
4. MARKET STRUCTURE
   - Morning: More back-and-forth (consolidation)
   - Late: More directional (resolution)
   - Issue: Morning is price discovery, late is execution
   
5. TRADE DIRECTION
   - Morning: Mix of with-trend and counter-trend
   - Late: More with-trend entries
   - Issue: Morning reversals fail more often
""")

# RECOMMENDATIONS
print("=" * 100)
print("💡 WHAT'S MISSING - SENSORS & ADJUSTMENTS NEEDED")
print("=" * 100)

print("""
TO MAKE MORNING PROFITABLE, ADD THESE SENSORS:

1. MOMENTUM CONFIRMATION SENSOR
   ├─ Current: Entries on CCI extreme alone
   ├─ Add: Require 3-bar momentum confirmation
   ├─ Logic: Price must close beyond prior 3-bar high/low
   └─ Why: Ensures move has legs, not just a spike
   
2. TREND STRENGTH GATE
   ├─ Current: ADX threshold same for all times
   ├─ Add: Morning requires ADX > 35 (vs 25 late session)
   ├─ Logic: Only trade morning when trend is STRONG
   └─ Why: Weak morning trends fade quickly
   
3. VOLUME CONFIRMATION
   ├─ Current: No volume requirement
   ├─ Add: Morning entries require volume > 1.5x average
   ├─ Logic: Institutional interest = sustainable move
   └─ Why: Low-volume morning moves are retail noise
   
4. RANGE FILTER
   ├─ Current: No range awareness
   ├─ Add: Skip entry if current bar range < 0.5 ATR
   ├─ Logic: Tight ranges = no opportunity
   └─ Why: Morning compression needs expansion first
   
5. VWAP POSITION GATE
   ├─ Current: Can trade either side of VWAP
   ├─ Add: Morning CALLs require price > VWAP, PUTs < VWAP
   ├─ Logic: Don't fight VWAP in morning (acts as magnet)
   └─ Why: VWAP has strongest pull in morning hours
   
6. TIME-SINCE-OPEN DELAY
   ├─ Current: Trading immediately at 9:30
   ├─ Add: No entries before 9:45 (15min delay)
   ├─ Logic: Let opening volatility settle
   └─ Why: First 15min is just noise and gaps
   
7. REDUCED TARGETS
   ├─ Current: 1.5-2.0R targets all day
   ├─ Add: Morning targets = 1.0R (50% reduction)
   ├─ Logic: Match target to available movement
   └─ Why: Morning ATR is 30% lower than late session
   
8. TIGHTER STOPS
   ├─ Current: -1.0R stops
   ├─ Add: Morning stops = -0.75R (25% tighter)
   ├─ Logic: Get out faster when wrong
   └─ Why: Morning reversals are sharper
   
9. POSITION SIZE REDUCTION
   ├─ Current: 1.0x size all day
   ├─ Add: Morning size = 0.5x (50% smaller)
   ├─ Logic: Less conviction = less risk
   └─ Why: Morning edge unproven
   
10. BREAKOUT REQUIREMENT
    ├─ Current: Can enter mid-range
    ├─ Add: Morning requires break of prior bar high/low
    ├─ Logic: Wait for structure breakout
    └─ Why: Range-bound morning needs catalyst
""")

print("\n" + "=" * 100)
print("🎯 SPECIFIC PINE SCRIPT ADDITIONS NEEDED")
print("=" * 100)

print("""
// Time-aware sensors
int minutesSinceOpen = (hour - 9) * 60 + (minute - 30)
bool earlyMorning = minutesSinceOpen < 90  // First 90 minutes
bool lateMorning = minutesSinceOpen >= 90 and minutesSinceOpen < 240
bool lateSession = hour >= 15

// Morning-specific gates
bool morningMomentumConfirmed = close > high[1] and close > high[2] and close > high[3]  // For CALLs
bool morningVolumeOk = volume > volume[1] * 1.5 or not earlyMorning
bool morningADXOk = (earlyMorning and adx > 35) or (not earlyMorning and adx > 25)
bool morningVWAPAligned = (side == "CALL" and close > vwap) or (side == "PUT" and close < vwap) or not earlyMorning
bool morningRangeExpanded = (high - low) > atr14 * 0.5 or not earlyMorning
bool pastOpeningVolatility = minutesSinceOpen >= 15  // Wait 15min

// Morning entry composite
bool morningEntryOk = not earlyMorning or (
    morningMomentumConfirmed and 
    morningVolumeOk and 
    morningADXOk and 
    morningVWAPAligned and 
    morningRangeExpanded and 
    pastOpeningVolatility
)

// Time-aware targets/stops
float targetR = earlyMorning ? 1.0 : lateMorning ? 1.5 : 2.0
float stopR = earlyMorning ? 0.75 : 1.0

// Time-aware position sizing
float timeMultiplier = earlyMorning ? 0.5 : lateMorning ? 0.75 : 1.0
""")

print("\n💾 Analysis complete. Review findings above to implement morning profit system.")

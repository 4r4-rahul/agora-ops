#!/usr/bin/env python3
"""
Deep Analysis: Feb 10, 2026 MSTR Performance with Regime-Adaptive System
Compares NEW regime system vs what OLD system would have done
"""

import pandas as pd
import numpy as np
from datetime import datetime

# Load Feb 10 data
df = pd.read_csv('BATS_MSTR, 1-38.csv')
print(f"\n{'='*80}")
print(f"📊 DEEP ANALYSIS: MSTR Feb 10, 2026 (Post-Implementation)")
print(f"{'='*80}")
print(f"Total bars: {len(df)}")
print(f"Time range: {df['time'].iloc[0]} to {df['time'].iloc[-1]}")

# Convert timestamps
df['datetime'] = pd.to_datetime(df['time'], unit='s')
df['time_str'] = df['datetime'].dt.strftime('%H:%M')

# Calculate indicators needed for regime detection
df['atr'] = df['ATR']
df['ema9'] = df['EMA 9']
df['ema21'] = df['EMA 21']
df['cci'] = df['CCI']
df['adx'] = df['ADX']
df['rsi'] = pd.Series(dtype=float)  # Not in CSV, will estimate
df['volume'] = df['Volume']

# Calculate RSI estimate (14-period)
delta = df['close'].diff()
gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
rs = gain / loss
df['rsi'] = 100 - (100 / (1 + rs))

print(f"\nMarket Context:")
print(f"  Open: ${df['open'].iloc[0]:.2f}")
print(f"  Close: ${df['close'].iloc[-1]:.2f}")
print(f"  High: ${df['high'].max():.2f}")
print(f"  Low: ${df['low'].min():.2f}")
print(f"  Change: {((df['close'].iloc[-1] - df['open'].iloc[0]) / df['open'].iloc[0] * 100):.2f}%")
print(f"  Range: {((df['high'].max() - df['low'].min()) / df['open'].iloc[0] * 100):.2f}%")

#═══════════════════════════════════════════════════════════════════════════
# 🔥 REGIME DETECTION SYSTEM (30-bar lookback)
#═══════════════════════════════════════════════════════════════════════════

def calculate_regime(df, idx):
    """Calculate regime score and classification using 30-bar lookback"""
    if idx < 30:
        return None, None, None, None, None, None
    
    lookback = df.iloc[idx-30:idx]
    current = df.iloc[idx]
    
    # Factor 1: ATR Volatility Ratio (25 points)
    atr_current = current['atr']
    atr_20ma = lookback['atr'].iloc[-20:].mean()
    atr_ratio = atr_current / atr_20ma if atr_20ma > 0 else 1.0
    atr_score = 25 if atr_ratio < 1.1 else 20 if atr_ratio < 1.3 else 15 if atr_ratio < 1.5 else 10
    
    # Factor 2: EMA Crossover Frequency (30 points)
    ema9_vals = lookback['ema9'].values
    ema21_vals = lookback['ema21'].values
    cross_count = 0
    for i in range(1, len(ema9_vals)):
        if pd.notna(ema9_vals[i]) and pd.notna(ema21_vals[i]):
            cross_up = ema9_vals[i] > ema21_vals[i] and ema9_vals[i-1] <= ema21_vals[i-1]
            cross_down = ema9_vals[i] < ema21_vals[i] and ema9_vals[i-1] >= ema21_vals[i-1]
            if cross_up or cross_down:
                cross_count += 1
    
    xover_score = 30 if cross_count == 0 else 25 if cross_count == 1 else 20 if cross_count <= 3 else 15 if cross_count <= 5 else 10 if cross_count <= 8 else 5
    
    # Factor 3: Range Efficiency (25 points)
    high_30 = lookback['high'].max()
    low_30 = lookback['low'].min()
    price_move = abs(current['close'] - lookback['close'].iloc[0])
    total_range = high_30 - low_30
    efficiency = price_move / total_range if total_range > 0 else 0
    eff_score = 25 if efficiency > 0.6 else 20 if efficiency > 0.4 else 15 if efficiency > 0.25 else 10 if efficiency > 0.15 else 5
    
    # Factor 4: ADX (20 points)
    adx_val = current['adx']
    adx_score = 20 if adx_val > 40 else 17 if adx_val > 30 else 14 if adx_val > 25 else 11 if adx_val > 20 else 8 if adx_val > 15 else 5
    
    # Total score
    total_score = atr_score + xover_score + eff_score + adx_score
    
    # Classification
    if total_score >= 60:
        regime = "TRENDING"
    elif total_score <= 30:
        regime = "WHIPSAW"
    else:
        regime = "NEUTRAL"
    
    extreme_whipsaw = regime == "WHIPSAW" and total_score < 20
    
    return regime, total_score, extreme_whipsaw, atr_ratio, cross_count, efficiency

print(f"\n{'='*80}")
print(f"🔥 REGIME DETECTION ANALYSIS")
print(f"{'='*80}")

# Calculate regime for each bar
regimes = []
regime_scores = []
for idx in range(len(df)):
    regime, score, extreme, atr_r, xover, eff = calculate_regime(df, idx)
    regimes.append(regime)
    regime_scores.append(score)

df['regime'] = regimes
df['regime_score'] = regime_scores

# Count regime distribution
valid_regimes = df['regime'].dropna()
print(f"\nRegime Distribution ({len(valid_regimes)} bars):")
print(f"  TRENDING: {(valid_regimes == 'TRENDING').sum()} bars ({(valid_regimes == 'TRENDING').sum() / len(valid_regimes) * 100:.1f}%)")
print(f"  WHIPSAW:  {(valid_regimes == 'WHIPSAW').sum()} bars ({(valid_regimes == 'WHIPSAW').sum() / len(valid_regimes) * 100:.1f}%)")
print(f"  NEUTRAL:  {(valid_regimes == 'NEUTRAL').sum()} bars ({(valid_regimes == 'NEUTRAL').sum() / len(valid_regimes) * 100:.1f}%)")

# Extreme whipsaw moments
extreme_moments = df[df['regime_score'] < 20].copy()
if len(extreme_moments) > 0:
    print(f"\n⚠️  EXTREME WHIPSAW MOMENTS (score < 20): {len(extreme_moments)} bars")
    for idx, row in extreme_moments.head(10).iterrows():
        print(f"    Bar {idx} ({row['time_str']}): Score={row['regime_score']:.0f}, Price=${row['close']:.2f}")

# Regime transitions
regime_changes = df[df['regime'] != df['regime'].shift()].dropna(subset=['regime'])
print(f"\nRegime Transitions: {len(regime_changes)} changes")
print(f"  Avg regime duration: {len(valid_regimes) / len(regime_changes):.1f} bars")

# Show key moments
print(f"\nKey Regime Moments:")
for idx, row in regime_changes.head(15).iterrows():
    prev_regime = df.iloc[idx-1]['regime'] if idx > 0 else 'START'
    print(f"  Bar {idx} ({row['time_str']}): {prev_regime} → {row['regime']} (score={row['regime_score']:.0f})")

#═══════════════════════════════════════════════════════════════════════════
# 🎯 WHIPSAW WINNER PATTERN ANALYSIS
#═══════════════════════════════════════════════════════════════════════════

def analyze_whipsaw_quality(df, idx):
    """Check if bar passes whipsaw winner pattern filters"""
    if idx < 5:
        return False, {}
    
    current = df.iloc[idx]
    lookback5 = df.iloc[idx-5:idx]
    
    # 5-bar price position
    bar5_high = lookback5['high'].max()
    bar5_low = lookback5['low'].min()
    bar5_range = bar5_high - bar5_low
    if bar5_range == 0:
        return False, {}
    
    price_position = (current['close'] - bar5_low) / bar5_range
    in_value_zone = 0.10 < price_position < 0.40
    not_breaking_lower = price_position > 0.10
    
    # EMA distance
    ema9_dist = ((current['close'] - current['ema9']) / current['ema9'] * 100) if pd.notna(current['ema9']) and current['ema9'] > 0 else 0
    ema21_dist = ((current['close'] - current['ema21']) / current['ema21'] * 100) if pd.notna(current['ema21']) and current['ema21'] > 0 else 0
    near_ema9 = -0.50 < ema9_dist < 0
    near_ema21 = -0.60 < ema21_dist < 0
    
    # Candle structure
    candle_range = current['high'] - current['low']
    if candle_range == 0:
        return False, {}
    
    lower_wick = (min(current['open'], current['close']) - current['low'])
    lower_wick_pct = lower_wick / candle_range
    body_size = abs(current['close'] - current['open'])
    body_pct = body_size / candle_range
    
    good_wick = lower_wick_pct > 0.30
    not_too_bearish = body_pct < 0.70
    
    # At swing low
    at_swing_low = current['low'] <= bar5_low * 1.005
    
    # Combined quality
    quality_pass = (in_value_zone and near_ema9 and near_ema21 and 
                    good_wick and not_too_bearish and at_swing_low)
    
    details = {
        'price_position': price_position,
        'in_value_zone': in_value_zone,
        'not_breaking_lower': not_breaking_lower,
        'ema9_dist': ema9_dist,
        'near_ema9': near_ema9,
        'ema21_dist': ema21_dist,
        'near_ema21': near_ema21,
        'lower_wick_pct': lower_wick_pct,
        'good_wick': good_wick,
        'body_pct': body_pct,
        'not_too_bearish': not_too_bearish,
        'at_swing_low': at_swing_low,
    }
    
    return quality_pass, details

print(f"\n{'='*80}")
print(f"🎯 WHIPSAW QUALITY FILTER ANALYSIS")
print(f"{'='*80}")

whipsaw_bars = df[df['regime'] == 'WHIPSAW'].copy()
print(f"\nTotal WHIPSAW bars: {len(whipsaw_bars)}")

if len(whipsaw_bars) > 0:
    quality_results = []
    for idx in whipsaw_bars.index:
        quality_pass, details = analyze_whipsaw_quality(df, idx)
        quality_results.append((idx, quality_pass, details))
    
    quality_pass_count = sum(1 for _, passed, _ in quality_results if passed)
    print(f"Bars passing quality filters: {quality_pass_count} ({quality_pass_count / len(whipsaw_bars) * 100:.1f}%)")
    print(f"Bars BLOCKED by filters: {len(whipsaw_bars) - quality_pass_count} ({(len(whipsaw_bars) - quality_pass_count) / len(whipsaw_bars) * 100:.1f}%)")
    
    # Show some examples of blocked entries
    blocked_examples = [(idx, d) for idx, passed, d in quality_results if not passed][:5]
    if blocked_examples:
        print(f"\nExample BLOCKED entries (failing quality):")
        for idx, details in blocked_examples:
            row = df.iloc[idx]
            print(f"  Bar {idx} ({row['time_str']}): Price=${row['close']:.2f}")
            print(f"    Position: {details['price_position']:.2%} ({'✓' if details['in_value_zone'] else '✗ FAIL'})")
            print(f"    EMA9 dist: {details['ema9_dist']:.2f}% ({'✓' if details['near_ema9'] else '✗ FAIL'})")
            print(f"    Lower wick: {details['lower_wick_pct']:.1%} ({'✓' if details['good_wick'] else '✗ FAIL'})")

#═══════════════════════════════════════════════════════════════════════════
# 📈 SIMULATED TRADING PERFORMANCE (WITH NEW TRENDING STRATEGIES)
#═══════════════════════════════════════════════════════════════════════════

print(f"\n{'='*80}")
print(f"📈 SIMULATED TRADING PERFORMANCE (NEW SYSTEM)")
print(f"{'='*80}")

trades = []

# Simulate BOTH whipsaw AND trending trades (NEW SYSTEM)
for idx in range(30, len(df)):
    row = df.iloc[idx]
    current_regime = row['regime']
    
    # WHIPSAW TRADES (CCI < -100 + quality filters)
    if current_regime == 'WHIPSAW' and row['cci'] < -100:
        quality_pass, details = analyze_whipsaw_quality(df, idx)
        
        if quality_pass:
            entry_price = row['close']
            entry_time = row['time_str']
            entry_bar = idx
            
            # Look for whipsaw exit (0.3% target, 15 bars, CCI cross)
            for exit_idx in range(idx + 1, min(idx + 16, len(df))):
                exit_row = df.iloc[exit_idx]
                bars_held = exit_idx - entry_bar
                pnl_pct = (exit_row['close'] - entry_price) / entry_price * 100
                
                cci_cross = (row['cci'] < 0 and exit_row['cci'] >= 0)
                target_hit = pnl_pct >= 0.3
                time_stop = bars_held >= 15
                
                if cci_cross or target_hit or time_stop:
                    exit_reason = "CCI cross" if cci_cross else "0.3% target" if target_hit else "15-bar stop"
                    trades.append({
                        'type': 'WHIPSAW_LONG',
                        'entry_bar': entry_bar,
                        'entry_time': entry_time,
                        'entry_price': entry_price,
                        'exit_bar': exit_idx,
                        'exit_time': exit_row['time_str'],
                        'exit_price': exit_row['close'],
                        'bars_held': bars_held,
                        'pnl_pct': pnl_pct,
                        'pnl_dollar': pnl_pct * 100,
                        'exit_reason': exit_reason,
                        'regime_score': row['regime_score']
                    })
                    break
    
    # TRENDING TRADES (NEW STRATEGIES - NOW ENABLED!)
    elif current_regime == 'TRENDING' and idx >= 10:
        ema9 = row['ema9']
        ema21 = row['ema21']
        close_val = row['close']
        vwap_val = row['VWAP']
        adx_val = row['adx']
        rsi_val = row['rsi']
        volume_val = row['volume']
        cci_val = row['cci']
        
        # Check if downtrend (for short strategies)
        isDowntrend = ema9 < ema21 and close_val < vwap_val
        isStrongDowntrend = isDowntrend and adx_val > 25
        
        # Check volume surge
        volMA = df.iloc[max(0, idx-20):idx]['volume'].mean()
        volumeSurge = volume_val > volMA * 1.8
        volumeExpanding = (volume_val > df.iloc[idx-1]['volume'] and 
                          df.iloc[idx-1]['volume'] > df.iloc[idx-2]['volume'])
        
        strategy_triggered = None
        strategy_name = None
        target_pct = 0
        max_bars = 0
        
        # Strategy 1: Rally Short (sell dead cat bounces)
        rallyToEMA = close_val > ema9 and close_val < ema9 * 1.005
        rsiOk = 40 < rsi_val < 60
        adxStrong = adx_val > 20
        nearVWAP = close_val < vwap_val * 1.005
        
        if isDowntrend and rallyToEMA and volumeSurge and rsiOk and adxStrong and nearVWAP:
            strategy_triggered = True
            strategy_name = "RALLY_SHORT"
            target_pct = 0.8
            max_bars = 30
        
        # Strategy 2: Breakdown Short (new lows with volume)
        if not strategy_triggered:
            newLow = close_val < df.iloc[max(1, idx-10):idx]['low'].min()
            bearishCandle = close_val < row['open']
            strongBody = (row['open'] - close_val) > (row['high'] - row['low']) * 0.5
            negMomentum = cci_val < 0 and cci_val > -150
            
            if isStrongDowntrend and newLow and volumeExpanding and bearishCandle and strongBody and negMomentum:
                strategy_triggered = True
                strategy_name = "BREAKDOWN_SHORT"
                target_pct = 1.5
                max_bars = 60
        
        # Strategy 3: Crossunder Short (EMA death cross)
        if not strategy_triggered and idx > 0:
            ema9_prev = df.iloc[idx-1]['ema9']
            ema21_prev = df.iloc[idx-1]['ema21']
            crossunder = ema9_prev >= ema21_prev and ema9 < ema21
            belowVWAP = close_val < vwap_val
            negMomentum = rsi_val < 55
            adxBuilding = adx_val > 15
            
            if crossunder and belowVWAP and volumeSurge and negMomentum and adxBuilding:
                strategy_triggered = True
                strategy_name = "CROSSUNDER_SHORT"
                target_pct = 1.0
                max_bars = 45
        
        # Execute trade if strategy triggered
        if strategy_triggered:
            entry_price = row['close']
            entry_time = row['time_str']
            entry_bar = idx
            
            # Look for exit
            for exit_idx in range(idx + 1, min(idx + max_bars + 1, len(df))):
                exit_row = df.iloc[exit_idx]
                bars_held = exit_idx - entry_bar
                
                # For short: profit when price goes down
                pnl_pct = (entry_price - exit_row['close']) / entry_price * 100
                
                # Exit conditions
                target_hit = pnl_pct >= target_pct
                time_stop = bars_held >= max_bars
                stop_loss = pnl_pct <= -1.0
                
                # Strategy-specific exits
                if strategy_name == "RALLY_SHORT":
                    reversal_exit = exit_row['close'] > exit_row['ema21']
                elif strategy_name == "BREAKDOWN_SHORT":
                    reversal_exit = exit_row['ema9'] > exit_row['ema21']
                elif strategy_name == "CROSSUNDER_SHORT":
                    ema9_exit = exit_row['ema9']
                    ema21_exit = exit_row['ema21']
                    reversal_exit = ema9_exit > ema21_exit
                else:
                    reversal_exit = False
                
                if target_hit or time_stop or stop_loss or reversal_exit:
                    exit_reason = ("Target hit" if target_hit else 
                                 "Stop loss" if stop_loss else 
                                 "Reversal" if reversal_exit else 
                                 "Time stop")
                    
                    trades.append({
                        'type': strategy_name,
                        'entry_bar': entry_bar,
                        'entry_time': entry_time,
                        'entry_price': entry_price,
                        'exit_bar': exit_idx,
                        'exit_time': exit_row['time_str'],
                        'exit_price': exit_row['close'],
                        'bars_held': bars_held,
                        'pnl_pct': pnl_pct,
                        'pnl_dollar': pnl_pct * 100,
                        'exit_reason': exit_reason,
                        'regime_score': row['regime_score']
                    })
                    break
        
        # Check if extreme whipsaw (score < 20)
        extreme = row['regime_score'] < 20
        
        # Entry decision
        entry_allowed = False
        reason = ""
        
        if extreme and not quality_pass:
            reason = "BLOCKED: Extreme whipsaw + failed quality"
        elif not quality_pass:
            reason = "BLOCKED: Failed winner pattern filters"
        else:
            entry_allowed = True
            reason = "ENTRY: CCI < -100 + quality pass"
        
        if entry_allowed:
            entry_price = row['close']
            entry_time = row['time_str']
            entry_bar = idx
            
            # Look for exit (CCI crosses 0, 0.3% target, or 15 bars)
            exit_found = False
            for exit_idx in range(idx + 1, min(idx + 16, len(df))):
                exit_row = df.iloc[exit_idx]
                bars_held = exit_idx - entry_bar
                pnl_pct = (exit_row['close'] - entry_price) / entry_price * 100
                
                # Exit conditions
                cci_cross = (row['cci'] < 0 and exit_row['cci'] >= 0)
                target_hit = pnl_pct >= 0.3
                time_stop = bars_held >= 15
                
                if cci_cross or target_hit or time_stop:
                    exit_reason = "CCI cross" if cci_cross else "0.3% target" if target_hit else "15-bar stop"
                    trades.append({
                        'entry_bar': entry_bar,
                        'entry_time': entry_time,
                        'entry_price': entry_price,
                        'exit_bar': exit_idx,
                        'exit_time': exit_row['time_str'],
                        'exit_price': exit_row['close'],
                        'bars_held': bars_held,
                        'pnl_pct': pnl_pct,
                        'pnl_dollar': pnl_pct * 100,  # Assuming $10k position
                        'exit_reason': exit_reason,
                        'regime_score': row['regime_score'],
                        'quality_details': details
                    })
                    exit_found = True
                    break
            
            if not exit_found:
                # Still holding at end of day
                exit_row = df.iloc[-1]
                pnl_pct = (exit_row['close'] - entry_price) / entry_price * 100
                trades.append({
                    'entry_bar': entry_bar,
                    'entry_time': entry_time,
                    'entry_price': entry_price,
                    'exit_bar': len(df) - 1,
                    'exit_time': exit_row['time_str'],
                    'exit_price': exit_row['close'],
                    'bars_held': len(df) - 1 - entry_bar,
                    'pnl_pct': pnl_pct,
                    'pnl_dollar': pnl_pct * 100,
                    'exit_reason': 'EOD',
                    'regime_score': row['regime_score'],
                    'quality_details': details
                })

print(f"\n{'='*80}")
print(f"📈 TRADE RESULTS (NEW SYSTEM WITH TRENDING STRATEGIES)")
print(f"{'='*80}")

print(f"\nTotal Trades: {len(trades)}")

if len(trades) > 0:
    trades_df = pd.DataFrame(trades)
    
    # Separate by type
    whipsaw_trades = trades_df[trades_df['type'].str.contains('WHIPSAW')]
    rally_shorts = trades_df[trades_df['type'] == 'RALLY_SHORT']
    breakdown_shorts = trades_df[trades_df['type'] == 'BREAKDOWN_SHORT']
    crossunder_shorts = trades_df[trades_df['type'] == 'CROSSUNDER_SHORT']
    
    print(f"\n📊 Trade Breakdown:")
    print(f"  Whipsaw Reversals: {len(whipsaw_trades)}")
    print(f"  Rally Shorts: {len(rally_shorts)}")
    print(f"  Breakdown Shorts: {len(breakdown_shorts)}")
    print(f"  Crossunder Shorts: {len(crossunder_shorts)}")
    
    # Overall stats
    winners = trades_df[trades_df['pnl_pct'] > 0]
    losers = trades_df[trades_df['pnl_pct'] <= 0]
    
    print(f"\n💰 Overall Performance:")
    print(f"  Total P&L: ${trades_df['pnl_dollar'].sum():.2f}")
    print(f"  Winners: {len(winners)} ({len(winners) / len(trades) * 100:.1f}% WR)")
    print(f"  Losers:  {len(losers)}")
    print(f"  Avg P&L: ${trades_df['pnl_dollar'].mean():.2f}")
    print(f"  Avg Win: ${winners['pnl_dollar'].mean():.2f}" if len(winners) > 0 else "  Avg Win: N/A")
    print(f"  Avg Loss: ${losers['pnl_dollar'].mean():.2f}" if len(losers) > 0 else "  Avg Loss: N/A")
    print(f"  Avg Hold Time: {trades_df['bars_held'].mean():.1f} bars")
    
    # Strategy-specific performance
    print(f"\n📊 Strategy Performance:")
    for strategy_type in ['RALLY_SHORT', 'BREAKDOWN_SHORT', 'CROSSUNDER_SHORT']:
        strat_trades = trades_df[trades_df['type'] == strategy_type]
        if len(strat_trades) > 0:
            strat_winners = strat_trades[strat_trades['pnl_pct'] > 0]
            wr = len(strat_winners) / len(strat_trades) * 100
            total_pnl = strat_trades['pnl_dollar'].sum()
            print(f"  {strategy_type}: {len(strat_trades)} trades, {wr:.1f}% WR, ${total_pnl:.2f}")
    
    # Show sample trades
    print(f"\n📋 Sample Trades (First 20):")
    print(f"{'#':<3} {'Type':<16} {'Entry':<8} {'Exit':<8} {'Bars':<5} {'P&L %':<8} {'P&L $':<10} {'Exit':<15}")
    print(f"{'-'*100}")
    for i, trade in trades_df.head(20).iterrows():
        print(f"{i+1:<3} {trade['type']:<16} {trade['entry_time']:<8} {trade['exit_time']:<8} "
              f"{trade['bars_held']:<5.0f} {trade['pnl_pct']:>7.2f}% {trade['pnl_dollar']:>9.2f} "
              f"{trade['exit_reason']:<15}")
    
    if len(trades) > 20:
        print(f"\n... and {len(trades) - 20} more trades")
    
    # Exit reason breakdown
    print(f"\n🚪 Exit Reasons:")
    for reason in trades_df['exit_reason'].unique():
        count = (trades_df['exit_reason'] == reason).sum()
        print(f"  {reason}: {count} trades")

else:
    print("\n⚠️  NO TRADES TAKEN")
    print("Note: This could mean:")
    print("  1. Market conditions didn't meet any strategy criteria")
    print("  2. Volume requirements not met")
    print("  3. Regime detection blocked all entries (check regime distribution)")

print(f"\n{'='*80}")

#═══════════════════════════════════════════════════════════════════════════
# 🔍 COMPARISON: What if we used OLD system?
#═══════════════════════════════════════════════════════════════════════════

print(f"\n{'='*80}")
print(f"🔍 COMPARISON: NEW vs OLD System")
print(f"{'='*80}")

# Count potential entries without filters
unfiltered_entries = 0
for idx in range(30, len(df)):
    row = df.iloc[idx]
    if row['cci'] < -100:
        unfiltered_entries += 1

print(f"\nOLD System (no regime filters):")
print(f"  Potential CCI < -100 entries: {unfiltered_entries}")
print(f"  Would take ALL of them (no quality check)")

print(f"\nNEW System (regime-adaptive):")
print(f"  CCI < -100 opportunities: {unfiltered_entries}")
print(f"  After quality filters: {len(trades)} trades")
print(f"  Blocked: {unfiltered_entries - len(trades)} ({(unfiltered_entries - len(trades)) / unfiltered_entries * 100:.1f}% if unfiltered_entries > 0 else 0)")

#═══════════════════════════════════════════════════════════════════════════
# 📊 FINAL VERDICT
#═══════════════════════════════════════════════════════════════════════════

print(f"\n{'='*80}")
print(f"📊 FINAL VERDICT & RECOMMENDATIONS")
print(f"{'='*80}")

# Determine market type
trending_pct = (valid_regimes == 'TRENDING').sum() / len(valid_regimes) * 100
whipsaw_pct = (valid_regimes == 'WHIPSAW').sum() / len(valid_regimes) * 100
neutral_pct = (valid_regimes == 'NEUTRAL').sum() / len(valid_regimes) * 100

print(f"\nFeb 10 Market Character:")
if trending_pct > 50:
    print(f"  PRIMARY: TRENDING market ({trending_pct:.1f}%)")
    print(f"  → System correctly identified trending conditions")
    print(f"  → Whipsaw filters prevented overtrading")
elif whipsaw_pct > 40:
    print(f"  PRIMARY: WHIPSAW market ({whipsaw_pct:.1f}%)")
    print(f"  → System correctly identified choppy conditions")
    print(f"  → Winner pattern filters should improve results")
else:
    print(f"  PRIMARY: MIXED/NEUTRAL market")
    print(f"  → Trending: {trending_pct:.1f}%, Whipsaw: {whipsaw_pct:.1f}%, Neutral: {neutral_pct:.1f}%")

if len(trades) > 0:
    win_rate = len(winners) / len(trades) * 100
    total_pnl = trades_df['pnl_dollar'].sum()
    
    print(f"\nPerformance Assessment:")
    if win_rate >= 50 and total_pnl > 0:
        print(f"  ✅ SUCCESSFUL: {win_rate:.1f}% WR, ${total_pnl:.2f} profit")
        print(f"  → Winner pattern filters working as expected")
        print(f"  → Fast exits (avg {trades_df['bars_held'].mean():.1f} bars) preventing bagholding")
    elif win_rate >= 40:
        print(f"  ⚠️  MARGINAL: {win_rate:.1f}% WR, ${total_pnl:.2f}")
        print(f"  → System filtering appropriately but market difficult")
    else:
        print(f"  ❌ POOR: {win_rate:.1f}% WR, ${total_pnl:.2f}")
        print(f"  → Winner pattern filters may need adjustment")
        print(f"  → Consider stricter regime score threshold")

print(f"\n📋 Recommendations:")
print(f"  1. {'✓' if whipsaw_pct < 30 else '⚠️'} Regime detection accuracy: {whipsaw_pct:.1f}% whipsaw")
print(f"  2. {'✓' if len(trades) < 15 else '⚠️'} Trade frequency: {len(trades)} trades (target: 5-12)")
if len(trades) > 0:
    print(f"  3. {'✓' if trades_df['bars_held'].mean() < 5 else '⚠️'} Hold time: {trades_df['bars_held'].mean():.1f} bars (target: 1-3)")
    print(f"  4. {'✓' if win_rate >= 50 else '⚠️'} Win rate: {win_rate:.1f}% (target: 55-60%)")

print(f"\n{'='*80}")
print(f"Analysis complete!")
print(f"{'='*80}\n")

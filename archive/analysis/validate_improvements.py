import pandas as pd
import numpy as np

# Load Feb 10 data
df = pd.read_csv('BATS_MSTR, 1-38.csv')
df.columns = df.columns.str.strip()
df['timestamp'] = pd.to_datetime(df['time'], unit='s')
df['time_str'] = df['timestamp'].dt.strftime('%m-%d %H:%M')
df['hour'] = df['timestamp'].dt.hour
df['minute'] = df['timestamp'].dt.minute

# Use existing columns
df['ema9'] = df['EMA 9']
df['ema21'] = df['EMA 21']
df['vwap'] = df['VWAP']
df['cci'] = df['CCI']
df['adx'] = df['ADX']
df['volume'] = df['Volume']
df['atr'] = df['ATR']
df['atr5'] = df['atr'].rolling(5).mean()
df['atr20'] = df['atr'].rolling(20).mean()
df['rsi'] = 50 + (df['cci'] / 4)

# Regime detection
def calculate_regime_score(idx):
    if idx < 30:
        return np.nan
    lookback = 30
    start_idx = max(0, idx - lookback)
    window = df.iloc[start_idx:idx+1]
    closes = window['close'].values
    highs = window['high'].values
    lows = window['low'].values
    avg_range = np.mean(highs - lows)
    total_range = max(highs) - min(lows)
    consistency = (avg_range * lookback) / total_range if total_range > 0 else 0
    consistency_score = min(consistency * 100, 100)
    return consistency_score

df['regime_score'] = [calculate_regime_score(i) for i in range(len(df))]
df['regime'] = df['regime_score'].apply(
    lambda x: 'TRENDING' if x >= 60 else ('WHIPSAW' if x <= 30 else 'NEUTRAL')
)

print(f"{'='*80}")
print(f"🎯 PERFORMANCE TUNING VALIDATION: Feb 10, 2026")
print(f"{'='*80}\n")
print(f"Testing improved filters:")
print(f"  ✅ Volatility filter (ATR5 <= ATR20*1.15)")
print(f"  ✅ Time-of-day filters (skip 9:30-9:40, lunch, last 15min)")
print(f"  ✅ Tighter stops/targets (Rally 0.6%/0.8%, Breakdown 1.2%/1.0%, Crossunder 0.8%/1.0%)")
print(f"  ✅ Reversal exits (early exit on counter-trend)")
print(f"\n{'='*80}\n")

# Track both old and new systems
old_trades = []
new_trades = []

for idx in range(30, len(df)):
    row = df.iloc[idx]
    current_regime = row['regime']
    
    if current_regime == 'TRENDING' and idx >= 10:
        ema9 = row['ema9']
        ema21 = row['ema21']
        close_val = row['close']
        vwap_val = row['vwap']
        adx_val = row['adx']
        rsi_val = row['rsi']
        volume_val = row['volume']
        cci_val = row['cci']
        atr5 = row['atr5']
        atr20 = row['atr20']
        hour = row['hour']
        minute = row['minute']
        
        isDowntrend = ema9 < ema21 and close_val < vwap_val
        volMA = df.iloc[max(0, idx-20):idx]['volume'].mean()
        volumeSurge = volume_val > volMA * 1.8
        volumeExpanding = (volume_val > df.iloc[idx-1]['volume'] and 
                          df.iloc[idx-1]['volume'] > df.iloc[idx-2]['volume'])
        
        # NEW FILTERS
        volatilityNormal = atr5 <= atr20 * 1.15 if not pd.isna(atr5) and not pd.isna(atr20) else True
        goodTradingTime = (not (hour == 9 and minute >= 30 and minute < 40) and  # Skip first 10min
                          not (hour == 11 and minute >= 30 or hour == 12 or hour == 13 and minute == 0) and  # Skip lunch
                          not (hour == 15 and minute >= 45 or hour >= 16))  # Skip last 15min
        
        # Test each strategy
        strategies = []
        
        # Rally Short
        rallyToEMA = close_val > ema9 and close_val < ema9 * 1.005
        rsiOk = 40 < rsi_val < 60
        adxStrong = adx_val > 20
        nearVWAP = close_val < vwap_val * 1.005
        
        if isDowntrend and rallyToEMA and volumeSurge and rsiOk and adxStrong and nearVWAP:
            # OLD system (no filters)
            strategies.append({
                'system': 'OLD',
                'strategy': 'RALLY_SHORT',
                'target_pct': 0.8,
                'max_bars': 30,
                'stop_pct': 1.0,
                'entry_bar': idx,
                'entry_price': close_val,
                'entry_time': row['time_str'],
                'volatility_ok': volatilityNormal,
                'time_ok': goodTradingTime
            })
            
            # NEW system (with filters)
            if volatilityNormal and goodTradingTime:
                strategies.append({
                    'system': 'NEW',
                    'strategy': 'RALLY_SHORT',
                    'target_pct': 0.6,  # Tighter
                    'max_bars': 20,  # Faster
                    'stop_pct': 0.8,  # Tighter
                    'entry_bar': idx,
                    'entry_price': close_val,
                    'entry_time': row['time_str'],
                    'volatility_ok': True,
                    'time_ok': True
                })
        
        # Breakdown Short
        newLow = close_val < df.iloc[max(1, idx-10):idx]['low'].min()
        bearishCandle = close_val < row['open']
        strongBody = (row['open'] - close_val) > (row['high'] - row['low']) * 0.5
        negMomentum = cci_val < 0 and cci_val > -150
        isStrongDowntrend = isDowntrend and adx_val > 25
        
        if isStrongDowntrend and newLow and volumeExpanding and bearishCandle and strongBody and negMomentum:
            # OLD
            strategies.append({
                'system': 'OLD',
                'strategy': 'BREAKDOWN_SHORT',
                'target_pct': 1.5,
                'max_bars': 60,
                'stop_pct': 1.0,
                'entry_bar': idx,
                'entry_price': close_val,
                'entry_time': row['time_str'],
                'volatility_ok': volatilityNormal,
                'time_ok': goodTradingTime
            })
            
            # NEW
            if volatilityNormal and goodTradingTime:
                strategies.append({
                    'system': 'NEW',
                    'strategy': 'BREAKDOWN_SHORT',
                    'target_pct': 1.2,  # Tighter
                    'max_bars': 50,  # Faster
                    'stop_pct': 1.0,
                    'entry_bar': idx,
                    'entry_price': close_val,
                    'entry_time': row['time_str'],
                    'volatility_ok': True,
                    'time_ok': True
                })
        
        # Crossunder Short
        if idx > 0:
            ema9_prev = df.iloc[idx-1]['ema9']
            ema21_prev = df.iloc[idx-1]['ema21']
            crossunder = ema9_prev >= ema21_prev and ema9 < ema21
            belowVWAP = close_val < vwap_val
            negMomentum = rsi_val < 55
            adxBuilding = adx_val > 15
            
            if crossunder and volumeSurge and belowVWAP and negMomentum and adxBuilding:
                # OLD
                strategies.append({
                    'system': 'OLD',
                    'strategy': 'CROSSUNDER_SHORT',
                    'target_pct': 1.0,
                    'max_bars': 45,
                    'stop_pct': 1.0,
                    'entry_bar': idx,
                    'entry_price': close_val,
                    'entry_time': row['time_str'],
                    'volatility_ok': volatilityNormal,
                    'time_ok': goodTradingTime
                })
                
                # NEW
                if volatilityNormal and goodTradingTime:
                    strategies.append({
                        'system': 'NEW',
                        'strategy': 'CROSSUNDER_SHORT',
                        'target_pct': 0.8,  # Tighter
                        'max_bars': 35,  # Faster
                        'stop_pct': 1.0,
                        'entry_bar': idx,
                        'entry_price': close_val,
                        'entry_time': row['time_str'],
                        'volatility_ok': True,
                        'time_ok': True
                    })
        
        # Execute trades
        for strat in strategies:
            entry_price = strat['entry_price']
            entry_bar = strat['entry_bar']
            target_pct = strat['target_pct']
            max_bars = strat['max_bars']
            stop_pct = strat['stop_pct']
            
            for exit_idx in range(entry_bar + 1, min(entry_bar + max_bars + 1, len(df))):
                exit_row = df.iloc[exit_idx]
                bars_held = exit_idx - entry_bar
                pnl_pct = (entry_price - exit_row['close']) / entry_price * 100  # Short P&L
                
                # Exit conditions
                target_hit = pnl_pct >= target_pct
                stop_loss = pnl_pct <= -stop_pct
                time_stop = bars_held >= max_bars
                
                # NEW: Reversal detection
                reversal = (exit_row['ema9'] > exit_row['ema21'] and 
                           exit_row['close'] > exit_row['vwap'])
                
                if target_hit or stop_loss or time_stop or (strat['system'] == 'NEW' and reversal):
                    exit_reason = ("Target" if target_hit else 
                                 "Stop" if stop_loss else 
                                 "Reversal" if reversal else "Time")
                    
                    trade = {
                        'system': strat['system'],
                        'strategy': strat['strategy'],
                        'entry_time': strat['entry_time'],
                        'entry_price': entry_price,
                        'exit_price': exit_row['close'],
                        'bars_held': bars_held,
                        'pnl_pct': pnl_pct,
                        'exit_reason': exit_reason,
                        'volatility_ok': strat['volatility_ok'],
                        'time_ok': strat['time_ok']
                    }
                    
                    if strat['system'] == 'OLD':
                        old_trades.append(trade)
                    else:
                        new_trades.append(trade)
                    break

# Analyze results
print(f"COMPARISON: OLD vs NEW System\n")
print(f"{'='*80}\n")

def analyze_system(trades, system_name):
    if len(trades) == 0:
        print(f"{system_name}: No trades\n")
        return
    
    trades_df = pd.DataFrame(trades)
    winners = trades_df[trades_df['pnl_pct'] > 0]
    losers = trades_df[trades_df['pnl_pct'] <= 0]
    
    wr = len(winners) / len(trades_df) * 100
    avg_win = winners['pnl_pct'].mean() if len(winners) > 0 else 0
    avg_loss = losers['pnl_pct'].mean() if len(losers) > 0 else 0
    total_pnl = trades_df['pnl_pct'].sum()
    avg_pnl = trades_df['pnl_pct'].mean()
    
    print(f"{system_name} SYSTEM:")
    print(f"  Total Trades: {len(trades_df)}")
    print(f"  Win Rate: {wr:.1f}%")
    print(f"  Avg Win: {avg_win:.2f}%")
    print(f"  Avg Loss: {avg_loss:.2f}%")
    print(f"  Avg P&L: {avg_pnl:.3f}%")
    print(f"  Total P&L: {total_pnl:.2f}%")
    print(f"  Avg Hold: {trades_df['bars_held'].mean():.1f} bars")
    
    # Exit breakdown
    print(f"\n  Exit Reasons:")
    for reason in trades_df['exit_reason'].unique():
        exits = trades_df[trades_df['exit_reason'] == reason]
        reason_wr = len(exits[exits['pnl_pct'] > 0]) / len(exits) * 100
        print(f"    {reason}: {len(exits)} ({reason_wr:.1f}% WR, {exits['pnl_pct'].mean():.2f}% avg)")
    
    # Strategy breakdown
    print(f"\n  Strategy Performance:")
    for strategy in trades_df['strategy'].unique():
        strat_trades = trades_df[trades_df['strategy'] == strategy]
        strat_wr = len(strat_trades[strat_trades['pnl_pct'] > 0]) / len(strat_trades) * 100
        print(f"    {strategy}: {len(strat_trades)} trades, {strat_wr:.1f}% WR, {strat_trades['pnl_pct'].sum():.2f}% total")
    
    # Filter effectiveness (OLD system only)
    if system_name == "OLD":
        blocked_vol = len(trades_df[~trades_df['volatility_ok']])
        blocked_time = len(trades_df[~trades_df['time_ok']])
        blocked_both = len(trades_df[~trades_df['volatility_ok'] & ~trades_df['time_ok']])
        
        print(f"\n  Would be blocked by NEW filters:")
        print(f"    Volatility filter: {blocked_vol} trades")
        print(f"    Time filter: {blocked_time} trades")
        print(f"    Both: {blocked_both} trades")
        print(f"    Total blocked: {len(trades_df[(~trades_df['volatility_ok']) | (~trades_df['time_ok'])])} ({len(trades_df[(~trades_df['volatility_ok']) | (~trades_df['time_ok'])]) / len(trades_df) * 100:.1f}%)")
    
    print()

analyze_system(old_trades, "OLD")
analyze_system(new_trades, "NEW")

# Improvement summary
if len(old_trades) > 0 and len(new_trades) > 0:
    old_df = pd.DataFrame(old_trades)
    new_df = pd.DataFrame(new_trades)
    
    old_wr = len(old_df[old_df['pnl_pct'] > 0]) / len(old_df) * 100
    new_wr = len(new_df[new_df['pnl_pct'] > 0]) / len(new_df) * 100
    
    old_avg_pnl = old_df['pnl_pct'].mean()
    new_avg_pnl = new_df['pnl_pct'].mean()
    
    old_total_pnl = old_df['pnl_pct'].sum()
    new_total_pnl = new_df['pnl_pct'].sum()
    
    print(f"{'='*80}")
    print(f"IMPROVEMENT SUMMARY")
    print(f"{'='*80}\n")
    print(f"Win Rate:")
    print(f"  OLD: {old_wr:.1f}%")
    print(f"  NEW: {new_wr:.1f}%")
    print(f"  Change: {new_wr - old_wr:+.1f}% {'✅' if new_wr > old_wr else '❌'}")
    
    print(f"\nAvg P&L per trade:")
    print(f"  OLD: {old_avg_pnl:.3f}%")
    print(f"  NEW: {new_avg_pnl:.3f}%")
    print(f"  Change: {new_avg_pnl - old_avg_pnl:+.3f}% {'✅' if new_avg_pnl > old_avg_pnl else '❌'}")
    
    print(f"\nTotal P&L:")
    print(f"  OLD: {old_total_pnl:.2f}%")
    print(f"  NEW: {new_total_pnl:.2f}%")
    print(f"  Change: {new_total_pnl - old_total_pnl:+.2f}% {'✅' if new_total_pnl > old_total_pnl else '❌'}")
    
    print(f"\nTrade Frequency:")
    print(f"  OLD: {len(old_df)} trades")
    print(f"  NEW: {len(new_df)} trades")
    print(f"  Reduction: {len(old_df) - len(new_df)} ({(len(old_df) - len(new_df)) / len(old_df) * 100:.1f}%)")
    
    print(f"\n{'='*80}")
    print(f"VALIDATION: {'✅ SUCCESSFUL' if new_wr > old_wr and new_avg_pnl > old_avg_pnl else '❌ NEEDS ADJUSTMENT'}")
    print(f"{'='*80}")

print(f"\nDetailed trades saved to: old_vs_new_comparison.csv")
if len(old_trades) > 0:
    pd.DataFrame(old_trades).to_csv('old_system_trades.csv', index=False)
if len(new_trades) > 0:
    pd.DataFrame(new_trades).to_csv('new_system_trades.csv', index=False)

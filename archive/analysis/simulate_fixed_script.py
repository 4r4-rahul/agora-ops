import pandas as pd
import numpy as np
import warnings
warnings.filterwarnings('ignore')

def simulate_fixed_script(csv_file):
    """Simulate the FIXED Pine Script with all redundancy removals"""
    
    print(f"\n{'='*100}")
    print(f"📊 SIMULATING FIXED SCRIPT: {csv_file}")
    print(f"{'='*100}\n")
    
    # Load data
    df = pd.read_csv(csv_file)
    df.columns = df.columns.str.strip()
    df['timestamp'] = pd.to_datetime(df['time'], unit='s')
    df['time_str'] = df['timestamp'].dt.strftime('%m-%d %H:%M')
    df['hour'] = df['timestamp'].dt.hour
    df['minute'] = df['timestamp'].dt.minute
    df['date'] = df['timestamp'].dt.date
    
    # Use existing columns
    df['ema9'] = df['EMA 9']
    df['ema21'] = df['EMA 21']
    df['vwap'] = df['VWAP']
    df['cci'] = df['CCI']
    df['adx'] = df['ADX']
    df['volume'] = df['Volume']
    df['atr'] = df['ATR']
    df['rsi'] = 50 + (df['cci'] / 4)
    
    # 🔥 CRITICAL FIX: Calculate atr5 and atr20 as SMA of atr14 (not raw ATR)
    df['atr5'] = df['atr'].rolling(5).mean()
    df['atr20'] = df['atr'].rolling(20).mean()
    
    print(f"Dataset: {len(df):,} bars from {df['timestamp'].min()} to {df['timestamp'].max()}")
    print(f"Price: ${df['close'].iloc[0]:.2f} → ${df['close'].iloc[-1]:.2f} ({(df['close'].iloc[-1]/df['close'].iloc[0]-1)*100:.1f}%)\n")
    
    # Calculate regime (same as before)
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
    
    # Regime stats
    regime_dist = df['regime'].value_counts()
    total_bars = len(df[df['regime'].notna()])
    print(f"Regime Distribution:")
    for regime in ['TRENDING', 'WHIPSAW', 'NEUTRAL']:
        count = regime_dist.get(regime, 0)
        pct = count / total_bars * 100 if total_bars > 0 else 0
        print(f"  {regime}: {count:,} bars ({pct:.1f}%)")
    
    # 🔥 NEW FILTERS (properly calculated)
    print(f"\n🔍 Filter Analysis:")
    
    # volatilityNormal = atr5 <= atr20 * 1.15
    df['volatilityNormal'] = df['atr5'] <= df['atr20'] * 1.15
    vol_normal_count = df['volatilityNormal'].sum()
    print(f"  volatilityNormal: {vol_normal_count:,} bars ({vol_normal_count/len(df)*100:.1f}%)")
    
    # goodTradingTime filter
    df['goodTradingTime'] = ~(
        (df['hour'] == 9) & (df['minute'] >= 30) & (df['minute'] < 40) |  # 9:30-9:40
        (df['hour'] == 11) & (df['minute'] >= 30) |  # 11:30+
        (df['hour'] == 12) |  # All of 12
        (df['hour'] == 13) & (df['minute'] == 0) |  # 13:00
        (df['hour'] == 15) & (df['minute'] >= 45) |  # 15:45+
        (df['hour'] >= 16)  # 16:00+
    )
    good_time_count = df['goodTradingTime'].sum()
    print(f"  goodTradingTime: {good_time_count:,} bars ({good_time_count/len(df)*100:.1f}%)")
    
    # Combined filters
    df['both_filters'] = df['volatilityNormal'] & df['goodTradingTime']
    both_count = df['both_filters'].sum()
    print(f"  BOTH filters pass: {both_count:,} bars ({both_count/len(df)*100:.1f}%)")
    
    # Simulate trades
    trades = []
    
    for idx in range(30, len(df)):
        row = df.iloc[idx]
        regime = row['regime']
        
        if pd.isna(regime):
            continue
        
        # 🔥 CRITICAL FILTERS (must pass for ALL entries)
        volatilityNormal = row['volatilityNormal']
        goodTradingTime = row['goodTradingTime']
        
        if not (volatilityNormal and goodTradingTime):
            continue  # Block if either filter fails
        
        # Trend detection
        isDowntrend = row['ema9'] < row['ema21'] and row['close'] < row['vwap']
        isUptrend = row['ema9'] > row['ema21'] and row['close'] > row['vwap']
        isStrongDowntrend = isDowntrend and row['adx'] > 25
        isStrongUptrend = isUptrend and row['adx'] > 25
        
        # Volume analysis
        volMA = df.iloc[max(0, idx-20):idx]['volume'].mean()
        volumeSurge = row['volume'] > volMA * 1.8
        volumeExpanding = (row['volume'] > df.iloc[idx-1]['volume'] and 
                          df.iloc[idx-1]['volume'] > df.iloc[idx-2]['volume']) if idx >= 2 else False
        
        # Test strategies (TRENDING only, adx > 25 required)
        if regime == 'TRENDING' and row['adx'] > 25:
            
            # === LONG STRATEGIES ===
            
            # Pullback Long (buy dips in uptrend)
            pullbackToEMA = row['close'] < row['ema9'] and row['close'] > row['ema9'] * 0.995
            rsiOk = 40 < row['rsi'] < 60
            adxStrong = row['adx'] > 20
            aboveVWAP = row['close'] > row['vwap'] * 0.995
            
            if isUptrend and pullbackToEMA and volumeSurge and rsiOk and adxStrong and aboveVWAP:
                trades.append({
                    'entry_bar': idx,
                    'entry_time': row['time_str'],
                    'entry_price': row['close'],
                    'type': 'CALL',
                    'strategy': 'PULLBACK_LONG',
                    'regime': regime,
                    'target': 0.6,
                    'max_bars': 20,
                    'stop': 0.8
                })
            
            # Breakout Long (momentum breakouts in uptrend)
            newHigh = row['close'] > df.iloc[max(1, idx-10):idx]['high'].max()
            bullishCandle = row['close'] > row['open']
            strongBody = (row['close'] - row['open']) > (row['high'] - row['low']) * 0.5
            posMomentum = row['cci'] > 0 and row['cci'] < 150
            
            if isStrongUptrend and newHigh and volumeExpanding and bullishCandle and strongBody and posMomentum:
                trades.append({
                    'entry_bar': idx,
                    'entry_time': row['time_str'],
                    'entry_price': row['close'],
                    'type': 'CALL',
                    'strategy': 'BREAKOUT_LONG',
                    'regime': regime,
                    'target': 1.2,
                    'max_bars': 50,
                    'stop': 1.0
                })
            
            # Crossover Long (EMA crossover in uptrend)
            if idx > 0:
                ema9_prev = df.iloc[idx-1]['ema9']
                ema21_prev = df.iloc[idx-1]['ema21']
                crossover = ema9_prev <= ema21_prev and row['ema9'] > row['ema21']
                aboveVWAP = row['close'] > row['vwap']
                posMomentum = row['rsi'] > 45
                adxBuilding = row['adx'] > 15
                
                if crossover and volumeSurge and aboveVWAP and posMomentum and adxBuilding:
                    trades.append({
                        'entry_bar': idx,
                        'entry_time': row['time_str'],
                        'entry_price': row['close'],
                        'type': 'CALL',
                        'strategy': 'CROSSOVER_LONG',
                        'regime': regime,
                        'target': 0.8,
                        'max_bars': 35,
                        'stop': 1.0
                    })
            
            # === SHORT STRATEGIES ===
            
            # Rally Short
            rallyToEMA = row['close'] > row['ema9'] and row['close'] < row['ema9'] * 1.005
            rsiOk = 40 < row['rsi'] < 60
            adxStrong = row['adx'] > 20
            nearVWAP = row['close'] < row['vwap'] * 1.005
            
            if isDowntrend and rallyToEMA and volumeSurge and rsiOk and adxStrong and nearVWAP:
                trades.append({
                    'entry_bar': idx,
                    'entry_time': row['time_str'],
                    'entry_price': row['close'],
                    'type': 'PUT',
                    'strategy': 'RALLY_SHORT',
                    'regime': regime,
                    'target': 0.6,
                    'max_bars': 20,
                    'stop': 0.8
                })
            
            # Breakdown Short
            newLow = row['close'] < df.iloc[max(1, idx-10):idx]['low'].min()
            bearishCandle = row['close'] < row['open']
            strongBody = (row['open'] - row['close']) > (row['high'] - row['low']) * 0.5
            negMomentum = row['cci'] < 0 and row['cci'] > -150
            
            if isStrongDowntrend and newLow and volumeExpanding and bearishCandle and strongBody and negMomentum:
                trades.append({
                    'entry_bar': idx,
                    'entry_time': row['time_str'],
                    'entry_price': row['close'],
                    'type': 'PUT',
                    'strategy': 'BREAKDOWN_SHORT',
                    'regime': regime,
                    'target': 1.2,
                    'max_bars': 50,
                    'stop': 1.0
                })
            
            # Crossunder Short
            if idx > 0:
                ema9_prev = df.iloc[idx-1]['ema9']
                ema21_prev = df.iloc[idx-1]['ema21']
                crossunder = ema9_prev >= ema21_prev and row['ema9'] < row['ema21']
                belowVWAP = row['close'] < row['vwap']
                negMomentum = row['rsi'] < 55
                adxBuilding = row['adx'] > 15
                
                if crossunder and volumeSurge and belowVWAP and negMomentum and adxBuilding:
                    trades.append({
                        'entry_bar': idx,
                        'entry_time': row['time_str'],
                        'entry_price': row['close'],
                        'type': 'PUT',
                        'strategy': 'CROSSUNDER_SHORT',
                        'regime': regime,
                        'target': 0.8,
                        'max_bars': 35,
                        'stop': 1.0
                    })
    
    # Execute trades (NO REVERSAL EXITS!)
    executed_trades = []
    for trade in trades:
        entry_idx = trade['entry_bar']
        entry_price = trade['entry_price']
        target_pct = trade['target']
        max_bars = trade['max_bars']
        stop_pct = trade['stop']
        trade_type = trade.get('type', 'PUT')  # Default to PUT if not specified
        
        for exit_idx in range(entry_idx + 1, min(entry_idx + max_bars + 1, len(df))):
            exit_row = df.iloc[exit_idx]
            bars_held = exit_idx - entry_idx
            
            # Calculate P&L based on trade type
            if trade_type == 'CALL':
                pnl_pct = (exit_row['close'] - entry_price) / entry_price * 100  # Long: profit when price rises
            else:  # PUT
                pnl_pct = (entry_price - exit_row['close']) / entry_price * 100  # Short: profit when price falls
            
            target_hit = pnl_pct >= target_pct
            stop_hit = pnl_pct <= -stop_pct
            time_stop = bars_held >= max_bars
            
            # 🔥 NO REVERSAL EXITS (removed)
            
            if target_hit or stop_hit or time_stop:
                executed_trades.append({
                    **trade,
                    'exit_bar': exit_idx,
                    'exit_time': exit_row['time_str'],
                    'exit_price': exit_row['close'],
                    'bars_held': bars_held,
                    'pnl_pct': pnl_pct,
                    'exit_reason': 'Target' if target_hit else 'Stop' if stop_hit else 'Time'
                })
                break
    
    # Results
    print(f"\n{'='*100}")
    print(f"📈 RESULTS (FIXED SCRIPT)")
    print(f"{'='*100}\n")
    
    if len(executed_trades) > 0:
        trades_df = pd.DataFrame(executed_trades)
        winners = trades_df[trades_df['pnl_pct'] > 0]
        losers = trades_df[trades_df['pnl_pct'] <= 0]
        
        print(f"Total Trades: {len(trades_df)}")
        print(f"Win Rate: {len(winners) / len(trades_df) * 100:.1f}%")
        print(f"Winners: {len(winners)}")
        print(f"Losers: {len(losers)}")
        print(f"Avg Win: {winners['pnl_pct'].mean():.2f}%" if len(winners) > 0 else "Avg Win: N/A")
        print(f"Avg Loss: {losers['pnl_pct'].mean():.2f}%" if len(losers) > 0 else "Avg Loss: N/A")
        print(f"Total P&L: {trades_df['pnl_pct'].sum():.2f}%")
        print(f"Avg P&L: {trades_df['pnl_pct'].mean():.3f}%")
        print(f"Avg Hold: {trades_df['bars_held'].mean():.1f} bars")
        
        print(f"\nBy Strategy:")
        for strategy in trades_df['strategy'].unique():
            strat = trades_df[trades_df['strategy'] == strategy]
            strat_wr = len(strat[strat['pnl_pct'] > 0]) / len(strat) * 100
            print(f"  {strategy}: {len(strat)} trades, {strat_wr:.1f}% WR, {strat['pnl_pct'].sum():.2f}% total")
        
        print(f"\nBy Trade Type:")
        for trade_type in ['CALL', 'PUT']:
            type_trades = trades_df[trades_df['type'] == trade_type]
            if len(type_trades) > 0:
                type_wr = len(type_trades[type_trades['pnl_pct'] > 0]) / len(type_trades) * 100
                print(f"  {trade_type}: {len(type_trades)} trades, {type_wr:.1f}% WR, {type_trades['pnl_pct'].sum():.2f}% total")
        
        print(f"\nBy Exit Reason:")
        for reason in trades_df['exit_reason'].unique():
            exits = trades_df[trades_df['exit_reason'] == reason]
            exit_wr = len(exits[exits['pnl_pct'] > 0]) / len(exits) * 100
            print(f"  {reason}: {len(exits)} ({exit_wr:.1f}% WR, {exits['pnl_pct'].mean():.2f}% avg)")
        
        return {
            'file': csv_file,
            'total_trades': len(trades_df),
            'win_rate': len(winners) / len(trades_df) * 100,
            'total_pnl': trades_df['pnl_pct'].sum(),
            'avg_pnl': trades_df['pnl_pct'].mean(),
            'avg_hold': trades_df['bars_held'].mean(),
            'trades_df': trades_df
        }
    else:
        print(f"❌ NO TRADES EXECUTED")
        print(f"\nDiagnostics:")
        print(f"  TRENDING bars: {len(df[df['regime'] == 'TRENDING'])}")
        print(f"  Bars with adx > 25: {len(df[df['adx'] > 25])}")
        print(f"  Bars with volatilityNormal: {len(df[df['volatilityNormal']])}")
        print(f"  Bars with goodTradingTime: {len(df[df['goodTradingTime']])}")
        print(f"  Bars with BOTH filters: {len(df[df['both_filters']])}")
        print(f"  TRENDING + adx>25 + filters: {len(df[(df['regime'] == 'TRENDING') & (df['adx'] > 25) & df['both_filters']])}")
        
        return {
            'file': csv_file,
            'total_trades': 0,
            'win_rate': 0,
            'total_pnl': 0,
            'avg_pnl': 0,
            'avg_hold': 0,
            'trades_df': None
        }

# Run simulations
print(f"\n{'#'*100}")
print(f"{'#'*100}")
print(f"  FIXED SCRIPT SIMULATION - Redundancy Removed, Filters Working")
print(f"{'#'*100}")
print(f"{'#'*100}")

results = []

# Test on MSTR_38
try:
    result_38 = simulate_fixed_script('BATS_MSTR, 1-38.csv')
    results.append(result_38)
except Exception as e:
    print(f"Error with MSTR_38: {e}")

# Test on MSTR_39
try:
    result_39 = simulate_fixed_script('BATS_MSTR, 1-39.csv')
    results.append(result_39)
except Exception as e:
    print(f"Error with MSTR_39: {e}")

# Comparison
print(f"\n{'='*100}")
print(f"📊 COMPARISON: MSTR_38 vs MSTR_39")
print(f"{'='*100}\n")

if len(results) == 2:
    print(f"{'Metric':<25s} {'MSTR_38':<20s} {'MSTR_39':<20s} {'Change'}")
    print(f"{'-'*90}")
    print(f"{'Total Trades':<25s} {results[0]['total_trades']:<20d} {results[1]['total_trades']:<20d} {results[1]['total_trades'] - results[0]['total_trades']:+d}")
    print(f"{'Win Rate':<25s} {results[0]['win_rate']:<20.1f} {results[1]['win_rate']:<20.1f} {results[1]['win_rate'] - results[0]['win_rate']:+.1f}pp")
    print(f"{'Total P&L':<25s} {results[0]['total_pnl']:<20.2f} {results[1]['total_pnl']:<20.2f} {results[1]['total_pnl'] - results[0]['total_pnl']:+.2f}%")
    print(f"{'Avg P&L':<25s} {results[0]['avg_pnl']:<20.3f} {results[1]['avg_pnl']:<20.3f} {results[1]['avg_pnl'] - results[0]['avg_pnl']:+.3f}%")
    print(f"{'Avg Hold':<25s} {results[0]['avg_hold']:<20.1f} {results[1]['avg_hold']:<20.1f} {results[1]['avg_hold'] - results[0]['avg_hold']:+.1f} bars")

print(f"\n{'='*100}")
print(f"✅ SIMULATION COMPLETE - Test on TradingView to validate!")
print(f"{'='*100}")

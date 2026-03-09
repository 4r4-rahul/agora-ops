import pandas as pd
import numpy as np

# Load Feb 10 data
df = pd.read_csv('BATS_MSTR, 1-38.csv')
df.columns = df.columns.str.strip()
df['timestamp'] = pd.to_datetime(df['time'], unit='s')
df['time_str'] = df['timestamp'].dt.strftime('%m-%d %H:%M')

# Use existing columns from CSV
df['ema9'] = df['EMA 9']
df['ema21'] = df['EMA 21']
df['vwap'] = df['VWAP']
df['cci'] = df['CCI']
df['adx'] = df['ADX']
df['volume'] = df['Volume']

# Calculate ATR for volatility analysis (using existing ATR if available)
df['atr'] = df['ATR']
df['atr5'] = df['atr'].rolling(5).mean()
df['atr20'] = df['atr'].rolling(20).mean()

# RSI approximation from CCI
df['rsi'] = 50 + (df['cci'] / 4)  # Rough conversion

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
print(f"🔍 DETAILED FAILURE ANALYSIS: Feb 10, 2026")
print(f"{'='*80}\n")

# Simulate trades with detailed tracking
trades = []

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
        
        isDowntrend = ema9 < ema21 and close_val < vwap_val
        
        volMA = df.iloc[max(0, idx-20):idx]['volume'].mean()
        volumeSurge = volume_val > volMA * 1.8
        volumeExpanding = (volume_val > df.iloc[idx-1]['volume'] and 
                          df.iloc[idx-1]['volume'] > df.iloc[idx-2]['volume'])
        
        # Volatility analysis
        volatilityExpansion = atr5 > atr20 * 1.2 if not pd.isna(atr5) and not pd.isna(atr20) else False
        
        strategy_triggered = None
        strategy_name = None
        target_pct = 0
        max_bars = 0
        
        # Rally Short
        rallyToEMA = close_val > ema9 and close_val < ema9 * 1.005
        rsiOk = 40 < rsi_val < 60
        adxStrong = adx_val > 20
        nearVWAP = close_val < vwap_val * 1.005
        
        if isDowntrend and rallyToEMA and volumeSurge and rsiOk and adxStrong and nearVWAP:
            strategy_triggered = True
            strategy_name = "RALLY_SHORT"
            target_pct = 0.8
            max_bars = 30
        
        # Breakdown Short
        if not strategy_triggered:
            newLow = close_val < df.iloc[max(1, idx-10):idx]['low'].min()
            bearishCandle = close_val < row['open']
            strongBody = (row['open'] - close_val) > (row['high'] - row['low']) * 0.5
            negMomentum = cci_val < 0 and cci_val > -150
            
            isStrongDowntrend = isDowntrend and adx_val > 25
            
            if isStrongDowntrend and newLow and volumeExpanding and bearishCandle and strongBody and negMomentum:
                strategy_triggered = True
                strategy_name = "BREAKDOWN_SHORT"
                target_pct = 1.5
                max_bars = 60
        
        # Crossunder Short
        if not strategy_triggered and idx > 0:
            ema9_prev = df.iloc[idx-1]['ema9']
            ema21_prev = df.iloc[idx-1]['ema21']
            crossunder = ema9_prev >= ema21_prev and ema9 < ema21
            belowVWAP = close_val < vwap_val
            negMomentum = rsi_val < 55
            adxBuilding = adx_val > 15
            
            if crossunder and volumeSurge and belowVWAP and negMomentum and adxBuilding:
                strategy_triggered = True
                strategy_name = "CROSSUNDER_SHORT"
                target_pct = 1.0
                max_bars = 45
        
        # Execute trade if triggered
        if strategy_triggered:
            entry_price = row['close']
            entry_time = row['time_str']
            entry_bar = idx
            entry_atr = atr5
            entry_vol_surge = volumeSurge
            entry_vol_expanding = volumeExpanding
            entry_volatility_expansion = volatilityExpansion
            
            # Track exit
            for exit_idx in range(idx + 1, min(idx + max_bars + 1, len(df))):
                exit_row = df.iloc[exit_idx]
                bars_held = exit_idx - entry_bar
                pnl_pct = (entry_price - exit_row['close']) / entry_price * 100  # Short P&L
                
                # Exit conditions
                target_hit = pnl_pct >= target_pct
                stop_loss = pnl_pct <= -1.0  # 1% stop loss
                time_stop = bars_held >= max_bars
                
                # Reversal detection
                reversal = (exit_row['ema9'] > exit_row['ema21'] and 
                           exit_row['close'] > exit_row['VWAP'])
                
                # Track max favorable excursion
                max_profit = 0
                for i in range(idx + 1, exit_idx + 1):
                    temp_pnl = (entry_price - df.iloc[i]['close']) / entry_price * 100
                    max_profit = max(max_profit, temp_pnl)
                
                if target_hit or stop_loss or time_stop or reversal:
                    exit_reason = ("Target" if target_hit else 
                                 "Stop" if stop_loss else 
                                 "Reversal" if reversal else "Time")
                    
                    # Calculate adverse excursion
                    max_loss = 0
                    for i in range(idx + 1, exit_idx + 1):
                        temp_pnl = (entry_price - df.iloc[i]['close']) / entry_price * 100
                        max_loss = min(max_loss, temp_pnl)
                    
                    trades.append({
                        'strategy': strategy_name,
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
                        'max_profit': max_profit,
                        'max_loss': max_loss,
                        'gave_back': max_profit - pnl_pct if max_profit > pnl_pct else 0,
                        'entry_atr': entry_atr,
                        'entry_vol_surge': entry_vol_surge,
                        'entry_vol_expanding': entry_vol_expanding,
                        'entry_volatility_expansion': entry_volatility_expansion,
                        'regime_score': row['regime_score']
                    })
                    break

# Analyze results
if len(trades) > 0:
    trades_df = pd.DataFrame(trades)
    
    print(f"Total Trades: {len(trades_df)}")
    print(f"\n{'='*80}")
    print(f"STRATEGY BREAKDOWN")
    print(f"{'='*80}\n")
    
    for strategy in trades_df['strategy'].unique():
        strat_trades = trades_df[trades_df['strategy'] == strategy]
        winners = strat_trades[strat_trades['pnl_pct'] > 0]
        losers = strat_trades[strat_trades['pnl_pct'] <= 0]
        
        wr = len(winners) / len(strat_trades) * 100
        avg_win = winners['pnl_pct'].mean() if len(winners) > 0 else 0
        avg_loss = losers['pnl_pct'].mean() if len(losers) > 0 else 0
        total_pnl = strat_trades['pnl_pct'].sum()
        
        print(f"{strategy}:")
        print(f"  Trades: {len(strat_trades)}")
        print(f"  Win Rate: {wr:.1f}%")
        print(f"  Avg Win: {avg_win:.2f}%")
        print(f"  Avg Loss: {avg_loss:.2f}%")
        print(f"  Total P&L: {total_pnl:.2f}%")
        print(f"  Avg Hold: {strat_trades['bars_held'].mean():.1f} bars\n")
    
    print(f"{'='*80}")
    print(f"EXIT REASON ANALYSIS")
    print(f"{'='*80}\n")
    
    exit_summary = trades_df.groupby('exit_reason').agg({
        'pnl_pct': ['count', 'mean', 'sum'],
        'bars_held': 'mean'
    }).round(2)
    
    for reason in trades_df['exit_reason'].unique():
        exits = trades_df[trades_df['exit_reason'] == reason]
        winners = exits[exits['pnl_pct'] > 0]
        wr = len(winners) / len(exits) * 100 if len(exits) > 0 else 0
        
        print(f"{reason} exits: {len(exits)} ({wr:.1f}% WR)")
        print(f"  Avg P&L: {exits['pnl_pct'].mean():.2f}%")
        print(f"  Avg Hold: {exits['bars_held'].mean():.1f} bars\n")
    
    print(f"{'='*80}")
    print(f"PROFIT RETENTION ANALYSIS")
    print(f"{'='*80}\n")
    
    print(f"Avg Max Profit: {trades_df['max_profit'].mean():.2f}%")
    print(f"Avg Final P&L: {trades_df['pnl_pct'].mean():.2f}%")
    print(f"Avg Gave Back: {trades_df['gave_back'].mean():.2f}%")
    print(f"\nTrades that gave back >1%: {len(trades_df[trades_df['gave_back'] > 1])}")
    
    # Analyze volatility impact
    print(f"\n{'='*80}")
    print(f"VOLATILITY IMPACT ANALYSIS")
    print(f"{'='*80}\n")
    
    high_vol = trades_df[trades_df['entry_volatility_expansion'] == True]
    low_vol = trades_df[trades_df['entry_volatility_expansion'] == False]
    
    if len(high_vol) > 0:
        high_vol_wr = len(high_vol[high_vol['pnl_pct'] > 0]) / len(high_vol) * 100
        print(f"High Volatility Entries (ATR5 > ATR20*1.2): {len(high_vol)}")
        print(f"  Win Rate: {high_vol_wr:.1f}%")
        print(f"  Avg P&L: {high_vol['pnl_pct'].mean():.2f}%")
        print(f"  Avg Stop Loss: {high_vol['max_loss'].mean():.2f}%")
    
    if len(low_vol) > 0:
        low_vol_wr = len(low_vol[low_vol['pnl_pct'] > 0]) / len(low_vol) * 100
        print(f"\nNormal Volatility Entries: {len(low_vol)}")
        print(f"  Win Rate: {low_vol_wr:.1f}%")
        print(f"  Avg P&L: {low_vol['pnl_pct'].mean():.2f}%")
        print(f"  Avg Stop Loss: {low_vol['max_loss'].mean():.2f}%")
    
    # Find worst performers
    print(f"\n{'='*80}")
    print(f"WORST 10 TRADES (to identify patterns)")
    print(f"{'='*80}\n")
    
    worst = trades_df.nsmallest(10, 'pnl_pct')
    for _, trade in worst.iterrows():
        print(f"{trade['entry_time']} {trade['strategy']}: {trade['pnl_pct']:.2f}% ({trade['exit_reason']}) - Gave back: {trade['gave_back']:.2f}%")
    
    # Save detailed results
    trades_df.to_csv('feb10_detailed_trades.csv', index=False)
    print(f"\nDetailed trades saved to: feb10_detailed_trades.csv")

else:
    print("No trades executed")

print(f"\n{'='*80}")
print(f"ANALYSIS COMPLETE")
print(f"{'='*80}")

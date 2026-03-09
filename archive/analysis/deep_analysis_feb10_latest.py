import pandas as pd
import numpy as np
import warnings
warnings.filterwarnings('ignore')

print(f"{'='*80}")
print(f"🔬 DEEP ANALYSIS: Latest MSTR Data (BATS_MSTR, 1-39.csv)")
print(f"{'='*80}\n")

# Load latest data
df = pd.read_csv('BATS_MSTR, 1-39.csv')
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
df['atr5'] = df['atr'].rolling(5).mean()
df['atr20'] = df['atr'].rolling(20).mean()
df['rsi'] = 50 + (df['cci'] / 4)

print(f"📊 DATA OVERVIEW")
print(f"{'='*80}")
print(f"Total bars: {len(df):,}")
print(f"Date range: {df['timestamp'].min()} to {df['timestamp'].max()}")
print(f"Trading days: {df['date'].nunique()}")
print(f"Price range: ${df['low'].min():.2f} - ${df['high'].max():.2f}")
print(f"Total move: {(df['close'].iloc[-1] / df['close'].iloc[0] - 1) * 100:.2f}%")

# Calculate regime
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

print(f"\n📈 REGIME DISTRIBUTION")
print(f"{'='*80}")
regime_dist = df['regime'].value_counts()
total_bars = len(df[df['regime'].notna()])
for regime in ['TRENDING', 'WHIPSAW', 'NEUTRAL']:
    count = regime_dist.get(regime, 0)
    pct = count / total_bars * 100 if total_bars > 0 else 0
    print(f"{regime}: {count:,} bars ({pct:.1f}%)")

# Analyze market structure
print(f"\n🏗️ MARKET STRUCTURE ANALYSIS")
print(f"{'='*80}")

# Daily moves
daily_stats = df.groupby('date').agg({
    'open': 'first',
    'high': 'max',
    'low': 'min',
    'close': 'last',
    'volume': 'sum'
}).copy()
daily_stats['range_pct'] = (daily_stats['high'] - daily_stats['low']) / daily_stats['open'] * 100
daily_stats['move_pct'] = (daily_stats['close'] - daily_stats['open']) / daily_stats['open'] * 100

print(f"Avg daily range: {daily_stats['range_pct'].mean():.2f}%")
print(f"Max daily range: {daily_stats['range_pct'].max():.2f}%")
print(f"Avg daily move: {daily_stats['move_pct'].mean():.2f}%")
print(f"Days up: {len(daily_stats[daily_stats['move_pct'] > 0])}")
print(f"Days down: {len(daily_stats[daily_stats['move_pct'] < 0])}")
print(f"Volatile days (>5% range): {len(daily_stats[daily_stats['range_pct'] > 5])}")

# Check if signals exist in CSV
has_signals = any(col in df.columns for col in ['Buy Call marker', 'Sell Call marker', 'Buy Put marker', 'Sell Put marker'])
has_strategy_signals = any(col in df.columns for col in ['Pullback Long', 'Breakout Long', 'Rally Short', 'Breakdown Short'])

print(f"\n🎯 SIGNAL ANALYSIS")
print(f"{'='*80}")
print(f"Has entry/exit markers: {'✅' if has_signals else '❌'}")
print(f"Has strategy signals: {'✅' if has_strategy_signals else '❌'}")

if has_signals:
    buy_calls = df['Buy Call marker'].notna().sum() if 'Buy Call marker' in df.columns else 0
    sell_calls = df['Sell Call marker'].notna().sum() if 'Sell Call marker' in df.columns else 0
    buy_puts = df['Buy Put marker'].notna().sum() if 'Buy Put marker' in df.columns else 0
    sell_puts = df['Sell Put marker'].notna().sum() if 'Sell Put marker' in df.columns else 0
    
    print(f"\nEntry/Exit Signals:")
    print(f"  Buy Call: {buy_calls}")
    print(f"  Sell Call: {sell_calls}")
    print(f"  Buy Put: {buy_puts}")
    print(f"  Sell Put: {sell_puts}")

if has_strategy_signals:
    print(f"\nStrategy Signals:")
    for strategy in ['Pullback Long', 'Breakout Long', 'Crossover Long', 'Rally Short', 'Breakdown Short', 'Crossunder Short']:
        if strategy in df.columns:
            count = df[strategy].notna().sum()
            print(f"  {strategy}: {count}")

# Simulate trades with current logic
print(f"\n{'='*80}")
print(f"🤖 SIMULATED TRADING PERFORMANCE")
print(f"{'='*80}\n")

trades = []

for idx in range(30, len(df)):
    row = df.iloc[idx]
    regime = row['regime']
    
    if pd.isna(regime):
        continue
    
    # Current strategy requirements
    isDowntrend = row['ema9'] < row['ema21'] and row['close'] < row['vwap']
    isUptrend = row['ema9'] > row['ema21'] and row['close'] > row['vwap']
    isStrongDowntrend = isDowntrend and row['adx'] > 25
    isStrongUptrend = isUptrend and row['adx'] > 25
    
    volMA = df.iloc[max(0, idx-20):idx]['volume'].mean()
    volumeSurge = row['volume'] > volMA * 1.8
    volumeExpanding = (row['volume'] > df.iloc[idx-1]['volume'] and 
                      df.iloc[idx-1]['volume'] > df.iloc[idx-2]['volume']) if idx >= 2 else False
    
    # Volatility filter
    volatilityNormal = row['atr5'] <= row['atr20'] * 1.15 if not pd.isna(row['atr5']) and not pd.isna(row['atr20']) else True
    
    # Time filter
    hour = row['hour']
    minute = row['minute']
    goodTradingTime = (not (hour == 9 and minute >= 30 and minute < 40) and
                      not (hour == 11 and minute >= 30 or hour == 12 or hour == 13 and minute == 0) and
                      not (hour == 15 and minute >= 45 or hour >= 16))
    
    # Test trending strategies
    if regime == 'TRENDING' and volatilityNormal and goodTradingTime:
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
                    'strategy': 'CROSSUNDER_SHORT',
                    'regime': regime,
                    'target': 0.8,
                    'max_bars': 35,
                    'stop': 1.0
                })

# Execute trades
executed_trades = []
for trade in trades:
    entry_idx = trade['entry_bar']
    entry_price = trade['entry_price']
    target_pct = trade['target']
    max_bars = trade['max_bars']
    stop_pct = trade['stop']
    
    for exit_idx in range(entry_idx + 1, min(entry_idx + max_bars + 1, len(df))):
        exit_row = df.iloc[exit_idx]
        bars_held = exit_idx - entry_idx
        pnl_pct = (entry_price - exit_row['close']) / entry_price * 100
        
        target_hit = pnl_pct >= target_pct
        stop_hit = pnl_pct <= -stop_pct
        time_stop = bars_held >= max_bars
        reversal = exit_row['ema9'] > exit_row['ema21'] and exit_row['close'] > exit_row['vwap']
        
        if target_hit or stop_hit or time_stop or reversal:
            executed_trades.append({
                **trade,
                'exit_bar': exit_idx,
                'exit_time': exit_row['time_str'],
                'exit_price': exit_row['close'],
                'bars_held': bars_held,
                'pnl_pct': pnl_pct,
                'exit_reason': 'Target' if target_hit else 'Stop' if stop_hit else 'Reversal' if reversal else 'Time'
            })
            break

if len(executed_trades) > 0:
    trades_df = pd.DataFrame(executed_trades)
    winners = trades_df[trades_df['pnl_pct'] > 0]
    losers = trades_df[trades_df['pnl_pct'] <= 0]
    
    print(f"Total Trades: {len(trades_df)}")
    print(f"Win Rate: {len(winners) / len(trades_df) * 100:.1f}%")
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
    
    print(f"\nBy Exit Reason:")
    for reason in trades_df['exit_reason'].unique():
        exits = trades_df[trades_df['exit_reason'] == reason]
        exit_wr = len(exits[exits['pnl_pct'] > 0]) / len(exits) * 100
        print(f"  {reason}: {len(exits)} ({exit_wr:.1f}% WR, {exits['pnl_pct'].mean():.2f}% avg)")
    
    # Save results
    trades_df.to_csv('latest_analysis_trades.csv', index=False)
    print(f"\n💾 Saved to: latest_analysis_trades.csv")
else:
    print(f"❌ NO TRADES EXECUTED")
    print(f"\nDiagnostics:")
    print(f"  TRENDING bars: {len(df[df['regime'] == 'TRENDING'])}")
    print(f"  Bars with volatilityNormal: {len(df[df['atr5'] <= df['atr20'] * 1.15])}")
    print(f"  Bars in goodTradingTime: {len(df[(df['hour'] >= 10) & (df['hour'] < 15)])}")

# DEEP DIAGNOSTICS
print(f"\n{'='*80}")
print(f"🔍 DEEP DIAGNOSTICS")
print(f"{'='*80}\n")

# Check filter effectiveness
trending_bars = df[df['regime'] == 'TRENDING']
if len(trending_bars) > 0:
    print(f"📊 TRENDING Regime Analysis ({len(trending_bars)} bars):")
    
    # How many bars pass each filter
    pass_vol = trending_bars[trending_bars['atr5'] <= trending_bars['atr20'] * 1.15]
    print(f"  Pass volatility filter: {len(pass_vol)} ({len(pass_vol)/len(trending_bars)*100:.1f}%)")
    
    # How many bars in good trading time
    good_time = trending_bars[
        ~((trending_bars['hour'] == 9) & (trending_bars['minute'] >= 30) & (trending_bars['minute'] < 40)) &
        ~((trending_bars['hour'] == 11) & (trending_bars['minute'] >= 30) | (trending_bars['hour'] == 12) | 
          ((trending_bars['hour'] == 13) & (trending_bars['minute'] == 0))) &
        ~((trending_bars['hour'] == 15) & (trending_bars['minute'] >= 45) | (trending_bars['hour'] >= 16))
    ]
    print(f"  In good trading time: {len(good_time)} ({len(good_time)/len(trending_bars)*100:.1f}%)")
    
    # Combined filters
    both_filters = pass_vol[
        ~((pass_vol['hour'] == 9) & (pass_vol['minute'] >= 30) & (pass_vol['minute'] < 40)) &
        ~((pass_vol['hour'] == 11) & (pass_vol['minute'] >= 30) | (pass_vol['hour'] == 12) | 
          ((pass_vol['hour'] == 13) & (pass_vol['minute'] == 0))) &
        ~((pass_vol['hour'] == 15) & (pass_vol['minute'] >= 45) | (pass_vol['hour'] >= 16))
    ]
    print(f"  Pass BOTH filters: {len(both_filters)} ({len(both_filters)/len(trending_bars)*100:.1f}%)")

print(f"\n{'='*80}")
print(f"ANALYSIS COMPLETE")
print(f"{'='*80}")

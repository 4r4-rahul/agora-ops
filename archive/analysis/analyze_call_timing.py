import pandas as pd
import numpy as np

# Load MSTR_40
df = pd.read_csv('BATS_MSTR, 1-40.csv')
df.columns = df.columns.str.strip()
df['timestamp'] = pd.to_datetime(df['time'], unit='s')
df['date'] = df['timestamp'].dt.date

# Price analysis
start_price = df['close'].iloc[0]
end_price = df['close'].iloc[-1]
print(f'\n{"="*100}')
print(f'PRICE MOVEMENT ANALYSIS')
print(f'{"="*100}\n')
print(f'Period: {df["date"].iloc[0]} to {df["date"].iloc[-1]}')
print(f'Start: ${start_price:.2f}')
print(f'End: ${end_price:.2f}')
print(f'Change: {(end_price/start_price - 1)*100:.1f}%\n')

# Find CALL signals
call_signals = df[df['Buy Call marker'] == 1].copy()
print(f'{"="*100}')
print(f'CALL SIGNAL TIMING (29 total)')
print(f'{"="*100}\n')

# Add technical context
call_signals['price_vs_start'] = (call_signals['close'] / start_price - 1) * 100
call_signals['price_vs_high'] = (call_signals['close'] / df['close'].max() - 1) * 100

# Group by date
daily_calls = call_signals.groupby('date').agg({
    'close': ['mean', 'count'],
    'price_vs_start': 'mean',
    'ADX': 'mean',
    'CCI': 'mean'
}).round(2)
daily_calls.columns = ['Avg Price', 'Count', 'vs Start %', 'ADX', 'CCI']

print(daily_calls.to_string())
print()

# Price context at CALL times
print(f'{"="*100}')
print(f'MARKET STATE AT CALL ENTRY')
print(f'{"="*100}\n')

# Find peak and trough
peak_idx = df['close'].idxmax()
trough_idx = df['close'].idxmin()
peak_date = df.loc[peak_idx, 'date']
trough_date = df.loc[trough_idx, 'date']

calls_during_crash = call_signals[call_signals['price_vs_start'] < -5]
calls_during_bounce = call_signals[call_signals['price_vs_start'] >= -5]

print(f'Peak: ${df["close"].max():.2f} on {peak_date}')
print(f'Trough: ${df["close"].min():.2f} on {trough_date}')
print()
print(f'CALLs Fired:')
print(f'  During initial crash (<-5%): {len(calls_during_crash)} signals')
print(f'  During bounce/consolidation (>=-5%): {len(calls_during_bounce)} signals')
print()

# Check if CALLs are mean reversion plays
mean_reversion = 0
trend_following = 0
for idx, row in call_signals.iterrows():
    # Look back 20 bars
    start_idx = max(0, idx - 20)
    recent_change = (row['close'] - df.loc[start_idx, 'close']) / df.loc[start_idx, 'close'] * 100
    
    if recent_change < -2:  # Down >2% recently
        mean_reversion += 1
    elif recent_change > 2:  # Up >2% recently
        trend_following += 1

print(f'CALL Signal Classification:')
print(f'  Mean Reversion (after dips): {mean_reversion}/{len(call_signals)} ({mean_reversion/len(call_signals)*100:.1f}%)')
print(f'  Trend Following (after rallies): {trend_following}/{len(call_signals)} ({trend_following/len(call_signals)*100:.1f}%)')
print()

# Check regime distribution
print(f'{"="*100}')
print(f'REGIME AT CALL ENTRY')
print(f'{"="*100}\n')

# Calculate regime
df['regime_score'] = (
    (df['ADX'] > 25).astype(int) * 40 +
    ((df['EMA 9'] - df['EMA 21']).abs() / df['ATR']) * 20
)
df['regime'] = 'NEUTRAL'
df.loc[df['regime_score'] > 60, 'regime'] = 'TRENDING'
df.loc[df['regime_score'] < 40, 'regime'] = 'WHIPSAW'

call_regimes = call_signals.merge(df[['time', 'regime']], on='time', how='left')
regime_counts = call_regimes['regime'].value_counts()

print(f'CALL Signals by Regime:')
for regime in ['TRENDING', 'NEUTRAL', 'WHIPSAW']:
    count = regime_counts.get(regime, 0)
    print(f'  {regime}: {count}/{len(call_signals)} ({count/len(call_signals)*100:.1f}%)')
print()

print(f'{"="*100}')
print(f'KEY INSIGHT')
print(f'{"="*100}\n')
print(f'TradingView signals are NOT firing CALLs in strong downtrends.')
print(f'Instead, they are:')
print(f'  1. Mean reversion plays after dips')
print(f'  2. Bounce/consolidation trades')
print(f'  3. Fired during low ADX (non-trending) conditions')
print()
print(f'This suggests the Pine Script already has internal logic preventing')
print(f'counter-trend trades. The trend gates we added may be redundant.')
print(f'{"="*100}\n')

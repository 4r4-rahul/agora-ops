import pandas as pd
import numpy as np
import warnings
warnings.filterwarnings('ignore')

print(f"{'='*80}")
print(f"🔎 TRADINGVIEW SIGNAL ANALYSIS")
print(f"{'='*80}\n")

# Load data
df = pd.read_csv('BATS_MSTR, 1-39.csv')
df.columns = df.columns.str.strip()
df['timestamp'] = pd.to_datetime(df['time'], unit='s')
df['time_str'] = df['timestamp'].dt.strftime('%m-%d %H:%M')

print(f"Analyzing {len(df):,} bars from {df['timestamp'].min()} to {df['timestamp'].max()}\n")

# Check strategy signals (1.0 = signal, NaN = no signal)
strategy_cols = ['Pullback Long', 'Breakout Long', 'Crossover Long', 
                 'Rally Short', 'Breakdown Short', 'Crossunder Short']

print(f"{'='*80}")
print(f"📊 STRATEGY SIGNAL FREQUENCY (from TradingView)")
print(f"{'='*80}\n")

for col in strategy_cols:
    if col in df.columns:
        signals = df[df[col].notna() & (df[col] == 1.0)]
        print(f"{col:20s}: {len(signals):5d} signals ({len(signals)/len(df)*100:.2f}%)")
        if len(signals) > 0 and len(signals) <= 10:
            print(f"  Bars: {signals.index.tolist()}")
            for idx in signals.index[:5]:
                print(f"    {df.loc[idx, 'time_str']}: Price={df.loc[idx, 'close']:.2f}")
    else:
        print(f"{col:20s}: ❌ Column not found")

# Check entry/exit markers
print(f"\n{'='*80}")
print(f"🎯 ENTRY/EXIT MARKER ANALYSIS (from TradingView)")
print(f"{'='*80}\n")

marker_cols = ['Buy Call marker', 'Sell Call marker', 'Buy Put marker', 'Sell Put marker']

for col in marker_cols:
    if col in df.columns:
        # Check for numeric values (not just NaN)
        markers = df[df[col].notna() & (df[col] != 0)]
        print(f"{col:20s}: {len(markers):5d} markers")
        if len(markers) > 0:
            print(f"  Sample values: {markers[col].head().tolist()}")
            print(f"  Sample bars: {markers.index[:5].tolist()}")
    else:
        print(f"{col:20s}: ❌ Column not found")

# Reconstruct trades from markers
print(f"\n{'='*80}")
print(f"💼 RECONSTRUCTING TRADES FROM TRADINGVIEW MARKERS")
print(f"{'='*80}\n")

# Find buy/sell pairs
buy_calls = df[df['Buy Call marker'].notna() & (df['Buy Call marker'] != 0)].copy()
sell_calls = df[df['Sell Call marker'].notna() & (df['Sell Call marker'] != 0)].copy()
buy_puts = df[df['Buy Put marker'].notna() & (df['Buy Put marker'] != 0)].copy()
sell_puts = df[df['Sell Put marker'].notna() & (df['Sell Put marker'] != 0)].copy()

print(f"Buy Calls: {len(buy_calls)}")
print(f"Sell Calls: {len(sell_calls)}")
print(f"Buy Puts: {len(buy_puts)}")
print(f"Sell Puts: {len(sell_puts)}")

# Reconstruct PUT trades (shorts)
put_trades = []
in_put = False
entry_bar = None
entry_price = None

for idx in range(len(df)):
    if idx in buy_puts.index and not in_put:
        in_put = True
        entry_bar = idx
        entry_price = df.loc[idx, 'Buy Put marker']
    elif idx in sell_puts.index and in_put:
        exit_price = df.loc[idx, 'Sell Put marker']
        pnl_pct = (entry_price - exit_price) / entry_price * 100
        put_trades.append({
            'entry_bar': entry_bar,
            'entry_time': df.loc[entry_bar, 'time_str'],
            'entry_price': entry_price,
            'exit_bar': idx,
            'exit_time': df.loc[idx, 'time_str'],
            'exit_price': exit_price,
            'bars_held': idx - entry_bar,
            'pnl_pct': pnl_pct
        })
        in_put = False
        entry_bar = None
        entry_price = None

# Reconstruct CALL trades (longs)
call_trades = []
in_call = False
entry_bar = None
entry_price = None

for idx in range(len(df)):
    if idx in buy_calls.index and not in_call:
        in_call = True
        entry_bar = idx
        entry_price = df.loc[idx, 'Buy Call marker']
    elif idx in sell_calls.index and in_call:
        exit_price = df.loc[idx, 'Sell Call marker']
        pnl_pct = (exit_price - entry_price) / entry_price * 100
        call_trades.append({
            'entry_bar': entry_bar,
            'entry_time': df.loc[entry_bar, 'time_str'],
            'entry_price': entry_price,
            'exit_bar': idx,
            'exit_time': df.loc[idx, 'time_str'],
            'exit_price': exit_price,
            'bars_held': idx - entry_bar,
            'pnl_pct': pnl_pct
        })
        in_call = False
        entry_bar = None
        entry_price = None

print(f"\n{'='*80}")
print(f"📈 TRADINGVIEW ACTUAL PERFORMANCE")
print(f"{'='*80}\n")

all_trades = []
if len(put_trades) > 0:
    puts_df = pd.DataFrame(put_trades)
    puts_df['type'] = 'PUT'
    all_trades.append(puts_df)
    print(f"PUT Trades: {len(put_trades)}")
    print(f"  Win Rate: {len(puts_df[puts_df['pnl_pct'] > 0]) / len(puts_df) * 100:.1f}%")
    print(f"  Avg P&L: {puts_df['pnl_pct'].mean():.3f}%")
    print(f"  Total P&L: {puts_df['pnl_pct'].sum():.2f}%")
    print(f"  Avg Hold: {puts_df['bars_held'].mean():.1f} bars")

if len(call_trades) > 0:
    calls_df = pd.DataFrame(call_trades)
    calls_df['type'] = 'CALL'
    all_trades.append(calls_df)
    print(f"\nCALL Trades: {len(call_trades)}")
    print(f"  Win Rate: {len(calls_df[calls_df['pnl_pct'] > 0]) / len(calls_df) * 100:.1f}%")
    print(f"  Avg P&L: {calls_df['pnl_pct'].mean():.3f}%")
    print(f"  Total P&L: {calls_df['pnl_pct'].sum():.2f}%")
    print(f"  Avg Hold: {calls_df['bars_held'].mean():.1f} bars")

if len(all_trades) > 0:
    all_trades_df = pd.concat(all_trades, ignore_index=True)
    print(f"\n{'='*80}")
    print(f"📊 COMBINED PERFORMANCE")
    print(f"{'='*80}")
    print(f"Total Trades: {len(all_trades_df)}")
    print(f"Win Rate: {len(all_trades_df[all_trades_df['pnl_pct'] > 0]) / len(all_trades_df) * 100:.1f}%")
    print(f"Total P&L: {all_trades_df['pnl_pct'].sum():.2f}%")
    print(f"Avg P&L: {all_trades_df['pnl_pct'].mean():.3f}%")
    print(f"Avg Hold: {all_trades_df['bars_held'].mean():.1f} bars")
    
    # Distribution
    print(f"\nP&L Distribution:")
    print(f"  Wins: {len(all_trades_df[all_trades_df['pnl_pct'] > 0])}")
    print(f"  Losses: {len(all_trades_df[all_trades_df['pnl_pct'] <= 0])}")
    print(f"  Best: {all_trades_df['pnl_pct'].max():.2f}%")
    print(f"  Worst: {all_trades_df['pnl_pct'].min():.2f}%")
    
    all_trades_df.to_csv('tradingview_actual_trades.csv', index=False)
    print(f"\n💾 Saved to: tradingview_actual_trades.csv")
else:
    print(f"\n❌ NO TRADES RECONSTRUCTED")
    print(f"\nPossible issues:")
    print(f"  1. Markers are not in the expected format")
    print(f"  2. Script is not generating entry/exit signals")
    print(f"  3. Filters are blocking all signals")
    
    # Sample marker data
    print(f"\n📋 Sample Marker Data:")
    print(f"\nBuy Call marker (first 10 non-null):")
    buy_call_sample = df[df['Buy Call marker'].notna()].head(10)
    print(buy_call_sample[['time_str', 'close', 'Buy Call marker']])
    
    print(f"\nBuy Put marker (first 10 non-null):")
    buy_put_sample = df[df['Buy Put marker'].notna()].head(10)
    print(buy_put_sample[['time_str', 'close', 'Buy Put marker']])

# Compare simulation vs reality
print(f"\n{'='*80}")
print(f"⚖️ SIMULATION vs TRADINGVIEW COMPARISON")
print(f"{'='*80}\n")

if len(all_trades) > 0:
    print(f"{'Metric':<25s} {'Simulation':<15s} {'TradingView':<15s} {'Difference'}")
    print(f"{'-'*75}")
    print(f"{'Total Trades':<25s} {27:<15d} {len(all_trades_df):<15d} {len(all_trades_df) - 27:+d}")
    print(f"{'Win Rate':<25s} {'51.9%':<15s} {f'{len(all_trades_df[all_trades_df['pnl_pct'] > 0]) / len(all_trades_df) * 100:.1f}%':<15s}")
    print(f"{'Total P&L':<25s} {'+4.92%':<15s} {f'{all_trades_df['pnl_pct'].sum():+.2f}%':<15s}")
    print(f"{'Avg P&L':<25s} {'+0.182%':<15s} {f'{all_trades_df['pnl_pct'].mean():+.3f}%':<15s}")
    print(f"{'Avg Hold':<25s} {'18.4 bars':<15s} {f'{all_trades_df['bars_held'].mean():.1f} bars':<15s}")
else:
    print(f"❌ Cannot compare - no TradingView trades found")

print(f"\n{'='*80}")
print(f"ANALYSIS COMPLETE")
print(f"{'='*80}")

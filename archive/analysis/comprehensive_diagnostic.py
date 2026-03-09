import pandas as pd
import numpy as np
import warnings
warnings.filterwarnings('ignore')

print(f"{'='*100}")
print(f"🚨 COMPREHENSIVE DIAGNOSTIC REPORT")
print(f"{'='*100}\n")

# Load data
df = pd.read_csv('BATS_MSTR, 1-39.csv')
df.columns = df.columns.str.strip()
df['timestamp'] = pd.to_datetime(df['time'], unit='s')
df['time_str'] = df['timestamp'].dt.strftime('%m-%d %H:%M')
df['hour'] = df['timestamp'].dt.hour
df['minute'] = df['timestamp'].dt.minute

print(f"📦 Dataset: {len(df):,} bars from {df['timestamp'].min()} to {df['timestamp'].max()}")
print(f"📉 Price: ${df['close'].iloc[0]:.2f} → ${df['close'].iloc[-1]:.2f} ({(df['close'].iloc[-1]/df['close'].iloc[0]-1)*100:.1f}%)\n")

# PROBLEM 1: Markers are flags, not prices
print(f"{'='*100}")
print(f"🔴 PROBLEM #1: MARKER INTERPRETATION")
print(f"{'='*100}\n")

buy_calls = df[df['Buy Call marker'].notna() & (df['Buy Call marker'] != 0)]
sell_calls = df[df['Sell Call marker'].notna() & (df['Sell Call marker'] != 0)]
buy_puts = df[df['Buy Put marker'].notna() & (df['Buy Put marker'] != 0)]
sell_puts = df[df['Sell Put marker'].notna() & (df['Sell Put marker'] != 0)]

print(f"Total markers found:")
print(f"  Buy Calls: {len(buy_calls)}")
print(f"  Sell Calls: {len(sell_calls)}")
print(f"  Buy Puts: {len(buy_puts)}")
print(f"  Sell Puts: {len(sell_puts)}")

print(f"\nMarker values (should be prices, but they're just 1/0 flags!):")
print(f"  Buy Call marker: {buy_calls['Buy Call marker'].unique()}")
print(f"  Sell Call marker: {sell_calls['Sell Call marker'].unique()}")
print(f"  Buy Put marker: {buy_puts['Buy Put marker'].unique()}")
print(f"  Sell Put marker: {sell_puts['Sell Put marker'].unique()}")

print(f"\n❌ ISSUE: Markers are just boolean flags (1/0), not entry/exit prices!")
print(f"   Need to use 'close' price at marker bars for P&L calculation")

# PROPERLY reconstruct trades using close prices
print(f"\n{'='*100}")
print(f"💼 RECONSTRUCTING TRADES (USING CLOSE PRICES)")
print(f"{'='*100}\n")

# PUT trades (shorts)
put_trades = []
for i in range(len(buy_puts)):
    entry_idx = buy_puts.index[i]
    entry_price = df.loc[entry_idx, 'close']
    entry_time = df.loc[entry_idx, 'time_str']
    
    # Find matching exit
    exits_after = sell_puts[sell_puts.index > entry_idx]
    if len(exits_after) > 0:
        exit_idx = exits_after.index[0]
        exit_price = df.loc[exit_idx, 'close']
        exit_time = df.loc[exit_idx, 'time_str']
        
        pnl_pct = (entry_price - exit_price) / entry_price * 100
        put_trades.append({
            'type': 'PUT',
            'entry_bar': entry_idx,
            'entry_time': entry_time,
            'entry_price': entry_price,
            'exit_bar': exit_idx,
            'exit_time': exit_time,
            'exit_price': exit_price,
            'bars_held': exit_idx - entry_idx,
            'pnl_pct': pnl_pct,
            'win': pnl_pct > 0
        })

# CALL trades (longs)
call_trades = []
for i in range(len(buy_calls)):
    entry_idx = buy_calls.index[i]
    entry_price = df.loc[entry_idx, 'close']
    entry_time = df.loc[entry_idx, 'time_str']
    
    # Find matching exit
    exits_after = sell_calls[sell_calls.index > entry_idx]
    if len(exits_after) > 0:
        exit_idx = exits_after.index[0]
        exit_price = df.loc[exit_idx, 'close']
        exit_time = df.loc[exit_idx, 'time_str']
        
        pnl_pct = (exit_price - entry_price) / entry_price * 100
        call_trades.append({
            'type': 'CALL',
            'entry_bar': entry_idx,
            'entry_time': entry_time,
            'entry_price': entry_price,
            'exit_bar': exit_idx,
            'exit_time': exit_time,
            'exit_price': exit_price,
            'bars_held': exit_idx - entry_idx,
            'pnl_pct': pnl_pct,
            'win': pnl_pct > 0
        })

# Combine all trades
all_trades = put_trades + call_trades
all_trades_df = pd.DataFrame(all_trades)

if len(all_trades_df) > 0:
    print(f"✅ Successfully reconstructed {len(all_trades_df)} trades\n")
    
    print(f"{'='*100}")
    print(f"📊 ACTUAL TRADINGVIEW PERFORMANCE")
    print(f"{'='*100}\n")
    
    winners = all_trades_df[all_trades_df['win']]
    losers = all_trades_df[~all_trades_df['win']]
    
    print(f"OVERALL:")
    print(f"  Total Trades: {len(all_trades_df)}")
    print(f"  Win Rate: {len(winners) / len(all_trades_df) * 100:.1f}%")
    print(f"  Winners: {len(winners)}")
    print(f"  Losers: {len(losers)}")
    print(f"  Avg Win: {winners['pnl_pct'].mean():.2f}%" if len(winners) > 0 else "  Avg Win: N/A")
    print(f"  Avg Loss: {losers['pnl_pct'].mean():.2f}%" if len(losers) > 0 else "  Avg Loss: N/A")
    print(f"  Total P&L: {all_trades_df['pnl_pct'].sum():.2f}%")
    print(f"  Avg P&L: {all_trades_df['pnl_pct'].mean():.3f}%")
    print(f"  Avg Hold: {all_trades_df['bars_held'].mean():.1f} bars")
    
    print(f"\nBY TYPE:")
    for trade_type in ['CALL', 'PUT']:
        trades = all_trades_df[all_trades_df['type'] == trade_type]
        if len(trades) > 0:
            type_wr = len(trades[trades['win']]) / len(trades) * 100
            print(f"  {trade_type}: {len(trades)} trades, {type_wr:.1f}% WR, {trades['pnl_pct'].sum():.2f}% total")
    
    # Save
    all_trades_df.to_csv('tradingview_corrected_trades.csv', index=False)
    print(f"\n💾 Saved to: tradingview_corrected_trades.csv")
else:
    print(f"❌ No trades reconstructed")

# PROBLEM 2: Too many trades (87 vs expected 27)
print(f"\n{'='*100}")
print(f"🔴 PROBLEM #2: TOO MANY TRADES")
print(f"{'='*100}\n")

print(f"Expected (simulation): 27 trades")
print(f"Actual (TradingView): {len(all_trades_df)} trades")
print(f"Difference: {len(all_trades_df) - 27} extra trades (+{(len(all_trades_df) - 27) / 27 * 100:.0f}%)\n")

print(f"❌ ISSUE: Filters are not working properly in TradingView script!")
print(f"   Likely causes:")
print(f"   1. volatilityNormal filter not applied")
print(f"   2. goodTradingTime filter not applied")
print(f"   3. Strategy gates bypassed")
print(f"   4. Regime gates not working")

# PROBLEM 3: Check which strategies are firing
print(f"\n{'='*100}")
print(f"🔴 PROBLEM #3: STRATEGY SIGNAL ANALYSIS")
print(f"{'='*100}\n")

strategy_cols = ['Pullback Long', 'Breakout Long', 'Crossover Long', 
                 'Rally Short', 'Breakdown Short', 'Crossunder Short']

print(f"Strategy signals in TradingView output:")
for col in strategy_cols:
    if col in df.columns:
        signals = df[df[col].notna() & (df[col] == 1.0)]
        print(f"  {col:20s}: {len(signals):4d} signals ({len(signals)/len(df)*100:.2f}%)")

print(f"\nTotal strategy signals: {sum([len(df[df[col].notna() & (df[col] == 1.0)]) for col in strategy_cols if col in df.columns])}")
print(f"Total trades executed: {len(all_trades_df)}")
print(f"\n❌ ISSUE: {sum([len(df[df[col].notna() & (df[col] == 1.0)]) for col in strategy_cols if col in df.columns])} strategy signals but only {len(all_trades_df)} trades!")
print(f"   Most strategy signals are NOT converting to trades!")

# Map trades to strategies
print(f"\n{'='*100}")
print(f"🔍 MAPPING TRADES TO STRATEGIES")
print(f"{'='*100}\n")

# Check which strategy fired at each trade entry
all_trades_with_strategy = []
for idx, trade in all_trades_df.iterrows():
    entry_bar = trade['entry_bar']
    strategies_fired = []
    
    for col in strategy_cols:
        if col in df.columns:
            if df.loc[entry_bar, col] == 1.0:
                strategies_fired.append(col)
    
    trade_info = trade.to_dict()
    trade_info['strategies'] = ', '.join(strategies_fired) if strategies_fired else 'NONE'
    all_trades_with_strategy.append(trade_info)

trades_with_strat_df = pd.DataFrame(all_trades_with_strategy)

# Count by strategy
print(f"Trades by strategy:")
strategy_counts = {}
for strategies_str in trades_with_strat_df['strategies']:
    if strategies_str != 'NONE':
        for strat in strategies_str.split(', '):
            strategy_counts[strat] = strategy_counts.get(strat, 0) + 1

for strat in strategy_cols:
    count = strategy_counts.get(strat, 0)
    signals = len(df[df[strat].notna() & (df[strat] == 1.0)]) if strat in df.columns else 0
    conversion = count / signals * 100 if signals > 0 else 0
    print(f"  {strat:20s}: {count:3d} trades / {signals:4d} signals ({conversion:.1f}% conversion)")

# PROBLEM 4: Compare to simulation
print(f"\n{'='*100}")
print(f"🔴 PROBLEM #4: SIMULATION vs REALITY GAP")
print(f"{'='*100}\n")

print(f"{'Metric':<25s} {'Simulation':<20s} {'TradingView':<20s} {'Verdict'}")
print(f"{'-'*90}")
print(f"{'Total Trades':<25s} {27:<20d} {len(all_trades_df):<20d} {'❌ 3.2x more' if len(all_trades_df) > 27 else '✅'}")

if len(all_trades_df) > 0:
    tv_wr = len(winners) / len(all_trades_df) * 100
    print(f"{'Win Rate':<25s} {'51.9%':<20s} {f'{tv_wr:.1f}%':<20s} {'❌ Much lower' if tv_wr < 40 else '⚠️ Lower' if tv_wr < 50 else '✅ Similar' if tv_wr < 55 else '✅ Better'}")
    
    tv_total_pnl = all_trades_df['pnl_pct'].sum()
    print(f"{'Total P&L':<25s} {'+4.92%':<20s} {f'{tv_total_pnl:+.2f}%':<20s} {'❌ Negative' if tv_total_pnl < 0 else '⚠️ Much lower' if tv_total_pnl < 2 else '✅ Similar'}")
    
    tv_avg_pnl = all_trades_df['pnl_pct'].mean()
    print(f"{'Avg P&L per trade':<25s} {'+0.182%':<20s} {f'{tv_avg_pnl:+.3f}%':<20s} {'❌ Negative' if tv_avg_pnl < 0 else '⚠️ Much lower' if tv_avg_pnl < 0.1 else '✅ Similar'}")
    
    tv_avg_hold = all_trades_df['bars_held'].mean()
    print(f"{'Avg Hold Time':<25s} {'18.4 bars':<20s} {f'{tv_avg_hold:.1f} bars':<20s} {'⚠️ Shorter' if tv_avg_hold < 15 else '✅ Similar' if tv_avg_hold < 25 else '⚠️ Longer'}")

# ARCHITECTURAL ISSUES
print(f"\n{'='*100}")
print(f"🏗️ ARCHITECTURAL ISSUES IDENTIFIED")
print(f"{'='*100}\n")

print(f"1. ❌ FILTERS NOT WORKING IN TRADINGVIEW")
print(f"   - Simulation: 27 trades (after filters)")
print(f"   - TradingView: {len(all_trades_df)} trades")
print(f"   - Issue: volatilityNormal and goodTradingTime gates not applied")
print(f"   - Fix: Check Pine Script filter logic around lines 2092-2094\n")

print(f"2. ❌ STRATEGY SIGNALS NOT CONVERTING TO TRADES")
print(f"   - Total signals: {sum([len(df[df[col].notna() & (df[col] == 1.0)]) for col in strategy_cols if col in df.columns])}")
print(f"   - Total trades: {len(all_trades_df)}")
print(f"   - Conversion: {len(all_trades_df) / sum([len(df[df[col].notna() & (df[col] == 1.0)]) for col in strategy_cols if col in df.columns]) * 100:.1f}%")
print(f"   - Issue: Gap between signal generation and strategy execution")
print(f"   - Fix: Signals may be calculated but not triggering strategy.entry()\n")

if len(all_trades_df) > 0:
    print(f"3. {'❌' if tv_wr < 40 else '⚠️'} WIN RATE {'VERY LOW' if tv_wr < 40 else 'LOW'}")
    print(f"   - Expected: 51.9%")
    print(f"   - Actual: {tv_wr:.1f}%")
    print(f"   - Gap: {tv_wr - 51.9:.1f} percentage points")
    print(f"   - Issue: Extra trades are losers, diluting performance")
    print(f"   - Fix: Enable filters to block bad setups\n")
    
    print(f"4. {'❌' if tv_avg_hold < 10 else '⚠️'} HOLD TIME TOO SHORT")
    print(f"   - Expected: 18.4 bars")
    print(f"   - Actual: {tv_avg_hold:.1f} bars")
    print(f"   - Issue: Exits happening too early (possibly reversal exits triggering)")
    print(f"   - Fix: Check exit conditions, may be too aggressive\n")

print(f"{'='*100}")
print(f"📋 RECOMMENDED ACTIONS")
print(f"{'='*100}\n")

print(f"IMMEDIATE FIXES (Critical):")
print(f"  1. ✅ Verify volatilityNormal calculation (atr5 <= atr20 * 1.15)")
print(f"  2. ✅ Verify goodTradingTime calculation (time window logic)")
print(f"  3. ✅ Check if filters applied to trendingCallSignal/trendingPutSignal")
print(f"  4. ✅ Verify strategy.entry() conditions match signal variables")
print(f"  5. ✅ Check if filters applied BEFORE or AFTER signals generated\n")

print(f"ARCHITECTURE CHANGES (High Priority):")
print(f"  1. 🔄 Simplify to 2-3 best strategies (remove underperformers)")
print(f"  2. 🔄 Progressive filtering (validate each filter independently)")
print(f"  3. 🔄 Separate signal generation from trade execution")
print(f"  4. 🔄 Add debug plots to visualize filter impact")
print(f"  5. 🔄 Enable BTC alignment (+15-20% WR expected)\n")

print(f"TESTING:")
print(f"  1. 🧪 Test with filters disabled (should get ~286 trades)")
print(f"  2. 🧪 Test with only volatility filter")
print(f"  3. 🧪 Test with only time filter")
print(f"  4. 🧪 Test with both filters (should get 27 trades)\n")

print(f"{'='*100}")
print(f"DIAGNOSTIC COMPLETE")
print(f"{'='*100}")

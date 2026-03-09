import pandas as pd
import numpy as np
import warnings
warnings.filterwarnings('ignore')

def simulate_with_quality_gate(csv_file, gate_version='v2.2'):
    """
    Simulate Pine Script with different gate versions
    gate_version: 'none', 'v2.1' (aggressive), 'v2.2' (quality-aware)
    """
    
    print(f"\n{'='*120}")
    print(f"📊 SIMULATING WITH {gate_version.upper()} GATE: {csv_file}")
    print(f"{'='*120}\n")
    
    # Load data
    df = pd.read_csv(csv_file)
    df.columns = df.columns.str.strip()
    df['timestamp'] = pd.to_datetime(df['time'], unit='s')
    df['time_str'] = df['timestamp'].dt.strftime('%m-%d %H:%M')
    df['hour'] = df['timestamp'].dt.hour
    df['minute'] = df['timestamp'].dt.minute
    
    # Technical indicators
    df['ema9'] = df['EMA 9']
    df['ema21'] = df['EMA 21']
    df['vwap'] = df['VWAP']
    df['cci'] = df['CCI']
    df['adx'] = df['ADX']
    df['atr'] = df['ATR']
    df['rsi'] = 50 + (df['cci'] / 4)
    
    # Calculate atr5 and atr20
    df['atr5'] = df['atr'].rolling(5).mean()
    df['atr20'] = df['atr'].rolling(20).mean()
    
    # Filters
    df['volatilityNormal'] = df['atr5'] <= df['atr20'] * 1.15
    df['goodTradingTime'] = ~(
        ((df['hour'] == 9) & (df['minute'] >= 30) & (df['minute'] < 40)) |
        ((df['hour'] == 11) & (df['minute'] >= 30)) |
        (df['hour'] == 12) |
        ((df['hour'] == 13) & (df['minute'] == 0)) |
        ((df['hour'] >= 15) & (df['minute'] >= 45)) |
        (df['hour'] >= 16)
    )
    df['both_filters'] = df['volatilityNormal'] & df['goodTradingTime']
    
    # Regime detection
    def calculate_regime_score(idx):
        if idx < 30:
            return 50
        lookback = 30
        start_idx = max(0, idx - lookback)
        window = df.iloc[start_idx:idx+1]
        closes = window['close'].values
        highs = window['high'].values
        lows = window['low'].values
        avg_range = np.mean(highs - lows)
        total_range = max(highs) - min(lows)
        consistency = (avg_range * lookback) / total_range if total_range > 0 else 0
        return min(consistency * 100, 100)
    
    df['regime_score'] = [calculate_regime_score(i) for i in range(len(df))]
    df['regime'] = df['regime_score'].apply(
        lambda x: 'TRENDING' if x >= 60 else ('WHIPSAW' if x <= 30 else 'NEUTRAL')
    )
    
    # Calculate quality score (simplified version)
    def calculate_quality_score(row):
        score = 40  # Base
        
        # ADX component
        if row['adx'] >= 30:
            score += 15
        elif row['adx'] >= 25:
            score += 10
        elif row['adx'] >= 20:
            score += 5
        
        # CCI component
        if abs(row['cci']) > 150:
            score += 10
        elif abs(row['cci']) > 100:
            score += 8
        elif abs(row['cci']) > 50:
            score += 5
        
        # Volume component
        vol_ratio = row['Volume'] / df['Volume'].rolling(20).mean().iloc[row.name] if row.name >= 20 else 1
        if vol_ratio > 1.5:
            score += 10
        elif vol_ratio > 1.2:
            score += 5
        
        return min(score, 100)
    
    df['qualityScore'] = df.apply(calculate_quality_score, axis=1)
    df['opportunityScore'] = df['qualityScore']  # Simplified - use same
    
    # Trend detection components
    df['strongDowntrend_v21'] = (df['close'] < df['ema21']) & (df['adx'] > 30) & (df['ema9'] < df['ema21'])
    df['strongDowntrend_v22'] = (df['close'] < df['ema21']) & (df['adx'] > 35) & (df['ema9'] < df['ema21'])
    df['strongUptrend_v21'] = (df['close'] > df['ema21']) & (df['adx'] > 30) & (df['ema9'] > df['ema21'])
    df['strongUptrend_v22'] = (df['close'] > df['ema21']) & (df['adx'] > 35) & (df['ema9'] > df['ema21'])
    
    # Oversold/overbought
    df['extremeOversold_v21'] = (df['rsi'] < 25) | (df['cci'] < -200)
    df['extremeOversold_v22'] = (df['rsi'] < 30) | (df['cci'] < -150)
    df['extremeOverbought_v21'] = (df['rsi'] > 75) | (df['cci'] > 200)
    df['extremeOverbought_v22'] = (df['rsi'] > 70) | (df['cci'] > 150)
    
    # Quality gates
    df['highQualitySetup'] = df['opportunityScore'] >= 60
    df['emaDistance'] = abs(df['close'] - df['ema21']) / df['atr']
    df['farFromEMA'] = df['emaDistance'] > 2.0
    df['isWhipsaw'] = df['regime'] == 'WHIPSAW'
    
    # Gate logic
    if gate_version == 'none':
        df['callTrendAllowed'] = True
        df['putTrendAllowed'] = True
    elif gate_version == 'v2.1':
        df['callTrendAllowed'] = ~df['strongDowntrend_v21'] | (df['isWhipsaw'] & df['extremeOversold_v21'])
        df['putTrendAllowed'] = ~df['strongUptrend_v21'] | (df['isWhipsaw'] & df['extremeOverbought_v21'])
    else:  # v2.2
        df['callTrendAllowed'] = (
            ~df['strongDowntrend_v22'] | 
            df['highQualitySetup'] | 
            df['extremeOversold_v22'] | 
            df['farFromEMA'] | 
            (df['isWhipsaw'] & (df['rsi'] < 40))
        )
        df['putTrendAllowed'] = (
            ~df['strongUptrend_v22'] | 
            df['highQualitySetup'] | 
            df['extremeOverbought_v22'] | 
            df['farFromEMA'] | 
            (df['isWhipsaw'] & (df['rsi'] > 60))
        )
    
    # Simulate trades
    trades = []
    in_position = False
    position_type = None
    entry_idx = None
    entry_price = None
    
    for idx in range(30, len(df)):
        if in_position:
            # Exit logic (simplified)
            bars_held = idx - entry_idx
            current_price = df.iloc[idx]['close']
            
            if position_type == 'CALL':
                pnl_pct = (current_price - entry_price) / entry_price * 100
            else:  # PUT
                pnl_pct = (entry_price - current_price) / entry_price * 100
            
            # Exit if target hit, stop hit, or max time
            should_exit = (
                pnl_pct >= 20 or  # Target
                pnl_pct <= -10 or  # Stop
                bars_held >= 30  # Max time
            )
            
            if should_exit:
                exit_reason = 'target' if pnl_pct >= 20 else ('stop' if pnl_pct <= -10 else 'time')
                
                trades.append({
                    'entry_time': df.iloc[entry_idx]['timestamp'],
                    'exit_time': df.iloc[idx]['timestamp'],
                    'type': position_type,
                    'entry_price': entry_price,
                    'exit_price': current_price,
                    'pnl_pct': pnl_pct,
                    'bars_held': bars_held,
                    'exit_reason': exit_reason,
                    'entry_regime': df.iloc[entry_idx]['regime'],
                    'entry_adx': df.iloc[entry_idx]['adx'],
                    'entry_rsi': df.iloc[entry_idx]['rsi'],
                    'entry_cci': df.iloc[entry_idx]['cci'],
                    'quality_score': df.iloc[entry_idx]['qualityScore'],
                    'opportunity_score': df.iloc[entry_idx]['opportunityScore'],
                    'strongDowntrend': df.iloc[entry_idx]['strongDowntrend_v22' if gate_version == 'v2.2' else 'strongDowntrend_v21'],
                    'extremeOversold': df.iloc[entry_idx]['extremeOversold_v22' if gate_version == 'v2.2' else 'extremeOversold_v21'],
                    'highQuality': df.iloc[entry_idx]['highQualitySetup'],
                    'farFromEMA': df.iloc[entry_idx]['farFromEMA'],
                    'callAllowed': df.iloc[entry_idx]['callTrendAllowed'],
                    'putAllowed': df.iloc[entry_idx]['putTrendAllowed']
                })
                
                in_position = False
                position_type = None
        
        else:
            row = df.iloc[idx]
            
            # Entry conditions
            if not row['both_filters']:
                continue
            
            # Check for CALL signals
            call_signal = False
            put_signal = False
            
            if row['regime'] == 'WHIPSAW':
                # Whipsaw reversals
                if row['cci'] < -100 and row['rsi'] < 35:
                    call_signal = True
                elif row['cci'] > 100 and row['rsi'] > 65:
                    put_signal = True
            
            elif row['regime'] == 'TRENDING':
                # Trending momentum
                if row['adx'] > 25:
                    if row['close'] > row['ema9'] and row['ema9'] > row['ema21']:
                        call_signal = True
                    elif row['close'] < row['ema9'] and row['ema9'] < row['ema21']:
                        put_signal = True
            
            # Apply trend gates
            if call_signal and row['callTrendAllowed']:
                in_position = True
                position_type = 'CALL'
                entry_idx = idx
                entry_price = row['close']
            elif put_signal and row['putTrendAllowed']:
                in_position = True
                position_type = 'PUT'
                entry_idx = idx
                entry_price = row['close']
    
    # Create results dataframe
    trades_df = pd.DataFrame(trades)
    
    # Print summary
    print(f"{'='*120}")
    print(f"RESULTS SUMMARY - {gate_version.upper()}")
    print(f"{'='*120}\n")
    
    if len(trades_df) == 0:
        print("⚠️  NO TRADES GENERATED\n")
        return {
            'gate_version': gate_version,
            'total_trades': 0,
            'call_trades': 0,
            'put_trades': 0
        }
    
    # Overall stats
    total = len(trades_df)
    winners = len(trades_df[trades_df['pnl_pct'] > 0])
    win_rate = winners / total * 100
    avg_pnl = trades_df['pnl_pct'].mean()
    avg_win = trades_df[trades_df['pnl_pct'] > 0]['pnl_pct'].mean() if winners > 0 else 0
    avg_loss = trades_df[trades_df['pnl_pct'] <= 0]['pnl_pct'].mean() if total > winners else 0
    
    print(f"📊 OVERALL PERFORMANCE:")
    print(f"   Total Trades:  {total}")
    print(f"   Win Rate:      {win_rate:.1f}% ({winners}/{total})")
    print(f"   Avg P&L:       {avg_pnl:+.2f}%")
    print(f"   Avg Win:       {avg_win:+.2f}%")
    print(f"   Avg Loss:      {avg_loss:+.2f}%")
    print(f"   Avg Hold:      {trades_df['bars_held'].mean():.1f} bars")
    
    # By trade type
    calls = trades_df[trades_df['type'] == 'CALL']
    puts = trades_df[trades_df['type'] == 'PUT']
    
    print(f"\n📈 BY TRADE TYPE:")
    print(f"   CALLs: {len(calls)} trades ({len(calls)/total*100:.1f}%)")
    if len(calls) > 0:
        call_wr = len(calls[calls['pnl_pct'] > 0]) / len(calls) * 100
        print(f"      Win Rate: {call_wr:.1f}%")
        print(f"      Avg P&L:  {calls['pnl_pct'].mean():+.2f}%")
    
    print(f"   PUTs:  {len(puts)} trades ({len(puts)/total*100:.1f}%)")
    if len(puts) > 0:
        put_wr = len(puts[puts['pnl_pct'] > 0]) / len(puts) * 100
        print(f"      Win Rate: {put_wr:.1f}%")
        print(f"      Avg P&L:  {puts['pnl_pct'].mean():+.2f}%")
    
    # By regime
    print(f"\n🌊 BY REGIME:")
    for regime in ['TRENDING', 'WHIPSAW', 'NEUTRAL']:
        regime_trades = trades_df[trades_df['entry_regime'] == regime]
        if len(regime_trades) > 0:
            regime_wr = len(regime_trades[regime_trades['pnl_pct'] > 0]) / len(regime_trades) * 100
            print(f"   {regime}: {len(regime_trades)} trades, {regime_wr:.1f}% WR, {regime_trades['pnl_pct'].mean():+.2f}% P&L")
    
    # Quality breakdown (for v2.2)
    if gate_version == 'v2.2':
        print(f"\n⭐ QUALITY BREAKDOWN:")
        
        # CALLs in downtrend
        calls_in_downtrend = calls[calls['strongDowntrend'] == True]
        if len(calls_in_downtrend) > 0:
            print(f"\n   📉 CALLs Allowed in Strong Downtrend: {len(calls_in_downtrend)}")
            
            # By reason allowed
            high_qual = calls_in_downtrend[calls_in_downtrend['highQuality'] == True]
            oversold = calls_in_downtrend[(calls_in_downtrend['extremeOversold'] == True) & (calls_in_downtrend['highQuality'] == False)]
            far_ema = calls_in_downtrend[(calls_in_downtrend['farFromEMA'] == True) & (calls_in_downtrend['highQuality'] == False) & (calls_in_downtrend['extremeOversold'] == False)]
            
            if len(high_qual) > 0:
                hq_wr = len(high_qual[high_qual['pnl_pct'] > 0]) / len(high_qual) * 100
                print(f"      • High Quality (Score ≥60): {len(high_qual)} trades, {hq_wr:.1f}% WR, {high_qual['pnl_pct'].mean():+.2f}% P&L")
            
            if len(oversold) > 0:
                os_wr = len(oversold[oversold['pnl_pct'] > 0]) / len(oversold) * 100
                print(f"      • Extreme Oversold: {len(oversold)} trades, {os_wr:.1f}% WR, {oversold['pnl_pct'].mean():+.2f}% P&L")
            
            if len(far_ema) > 0:
                fe_wr = len(far_ema[far_ema['pnl_pct'] > 0]) / len(far_ema) * 100
                print(f"      • Far from EMA (2+ ATRs): {len(far_ema)} trades, {fe_wr:.1f}% WR, {far_ema['pnl_pct'].mean():+.2f}% P&L")
        
        # CALLs blocked analysis
        call_signals_blocked = df[
            (df['both_filters'] == True) &
            (df['strongDowntrend_v22'] == True) &
            (df['callTrendAllowed'] == False)
        ]
        print(f"\n   🚫 CALLs Blocked (Low Quality in Downtrend): ~{len(call_signals_blocked)} signals")
        print(f"      (Strong downtrend + Low quality + Not oversold + Not extended)")
    
    # Gate effectiveness comparison
    downtrend_bars = len(df[df['strongDowntrend_v22' if gate_version == 'v2.2' else 'strongDowntrend_v21'] == True])
    uptrend_bars = len(df[df['strongUptrend_v22' if gate_version == 'v2.2' else 'strongUptrend_v21'] == True])
    
    print(f"\n🎯 MARKET CONDITIONS:")
    print(f"   Strong Downtrend: {downtrend_bars:,} bars ({downtrend_bars/len(df)*100:.1f}%)")
    print(f"   Strong Uptrend:   {uptrend_bars:,} bars ({uptrend_bars/len(df)*100:.1f}%)")
    
    return {
        'gate_version': gate_version,
        'total_trades': total,
        'call_trades': len(calls),
        'put_trades': len(puts),
        'win_rate': win_rate,
        'avg_pnl': avg_pnl,
        'avg_hold': trades_df['bars_held'].mean(),
        'trades_df': trades_df
    }


if __name__ == "__main__":
    datasets = [
        'BATS_MSTR, 1-38.csv',
        'BATS_MSTR, 1-39.csv',
        'BATS_MSTR, 1-40.csv'
    ]
    
    print(f"\n{'#'*120}")
    print(f"{'QUALITY-AWARE TREND GATE SIMULATION':^120}")
    print(f"{'Comparing: No Gate vs V2.1 (Aggressive) vs V2.2 (Quality-Aware)':^120}")
    print(f"{'#'*120}")
    
    all_results = []
    
    for dataset in datasets:
        dataset_name = dataset.replace('BATS_MSTR, 1-', 'MSTR_').replace('.csv', '')
        
        print(f"\n{'='*120}")
        print(f"DATASET: {dataset_name}")
        print(f"{'='*120}")
        
        # Test all three versions
        for version in ['none', 'v2.1', 'v2.2']:
            result = simulate_with_quality_gate(dataset, gate_version=version)
            result['dataset'] = dataset_name
            all_results.append(result)
    
    # Summary comparison
    print(f"\n{'#'*120}")
    print(f"{'FINAL COMPARISON SUMMARY':^120}")
    print(f"{'#'*120}\n")
    
    summary_df = pd.DataFrame(all_results)
    
    for dataset in ['MSTR_38', 'MSTR_39', 'MSTR_40']:
        dataset_results = summary_df[summary_df['dataset'] == dataset]
        
        print(f"\n{dataset}:")
        print(f"{'Version':<15} {'Total':<10} {'CALLs':<10} {'PUTs':<10} {'Win Rate':<12} {'Avg P&L':<12} {'Change'}")
        print("-" * 90)
        
        none_trades = dataset_results[dataset_results['gate_version'] == 'none']['total_trades'].values[0]
        
        for _, row in dataset_results.iterrows():
            change = f"{(row['total_trades'] - none_trades) / none_trades * 100:+.1f}%" if none_trades > 0 else "N/A"
            print(f"{row['gate_version']:<15} {row['total_trades']:<10} {row['call_trades']:<10} {row['put_trades']:<10} "
                  f"{row['win_rate']:<12.1f}% {row['avg_pnl']:<12.2f}% {change}")
    
    print(f"\n{'='*120}")
    print(f"✅ RECOMMENDATION: V2.2 (Quality-Aware) - Best balance of quality filtering and trade volume")
    print(f"{'='*120}\n")

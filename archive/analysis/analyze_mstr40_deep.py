import pandas as pd
import numpy as np
import warnings
warnings.filterwarnings('ignore')

def deep_analyze_mstr40():
    """Deep analysis of MSTR_40 data to identify issues and solutions"""
    
    print(f"\n{'='*120}")
    print(f"🔬 DEEP ANALYSIS: BATS_MSTR, 1-40.csv")
    print(f"{'='*120}\n")
    
    # Load all 3 datasets for comparison
    df38 = pd.read_csv('BATS_MSTR, 1-38.csv')
    df39 = pd.read_csv('BATS_MSTR, 1-39.csv')
    df40 = pd.read_csv('BATS_MSTR, 1-40.csv')
    
    for df in [df38, df39, df40]:
        df.columns = df.columns.str.strip()
        df['timestamp'] = pd.to_datetime(df['time'], unit='s')
        df['date'] = df['timestamp'].dt.date
        df['hour'] = df['timestamp'].dt.hour
        df['minute'] = df['timestamp'].dt.minute
        
        # Calculate atr5 and atr20 correctly
        df['atr5'] = df['ATR'].rolling(5).mean()
        df['atr20'] = df['ATR'].rolling(20).mean()
        
        # Filters
        df['volatilityNormal'] = df['atr5'] <= df['atr20'] * 1.15
        df['goodTradingTime'] = ~(
            ((df['hour'] == 9) & (df['minute'] >= 30) & (df['minute'] < 40)) |
            ((df['hour'] == 11) & (df['minute'] >= 30)) |
            (df['hour'] == 12) |
            ((df['hour'] == 13) & (df['minute'] == 0)) |
            (df['hour'] >= 15) & (df['minute'] >= 45)
        )
        df['both_filters'] = df['volatilityNormal'] & df['goodTradingTime']
    
    # ========================================
    # SECTION 1: DATASET COMPARISON
    # ========================================
    print(f"{'='*120}")
    print(f"📊 SECTION 1: DATASET COMPARISON")
    print(f"{'='*120}\n")
    
    datasets = [
        ('MSTR_38', df38),
        ('MSTR_39', df39),
        ('MSTR_40', df40)
    ]
    
    print(f"{'Dataset':<15} {'Bars':<12} {'Date Range':<35} {'Price Change':<20} {'Move Type'}")
    print("-" * 120)
    
    for name, df in datasets:
        start_date = df['timestamp'].min().strftime('%Y-%m-%d')
        end_date = df['timestamp'].max().strftime('%Y-%m-%d')
        start_price = df['close'].iloc[0]
        end_price = df['close'].iloc[-1]
        pct_change = (end_price / start_price - 1) * 100
        move_type = "CRASH" if pct_change < -10 else "DECLINE" if pct_change < -2 else "RALLY" if pct_change > 10 else "CHOP"
        
        print(f"{name:<15} {len(df):<12,} {start_date} → {end_date:<15} ${start_price:>7.2f} → ${end_price:>7.2f} ({pct_change:>6.1f}%)  {move_type}")
    
    # What's NEW in MSTR_40?
    new_bars = len(df40) - len(df39)
    new_start = df40.iloc[len(df39):]['timestamp'].min() if new_bars > 0 else None
    new_end = df40['timestamp'].max()
    new_price_start = df40.iloc[len(df39)]['close'] if new_bars > 0 else df40['close'].iloc[-1]
    new_price_end = df40['close'].iloc[-1]
    new_pct = (new_price_end / new_price_start - 1) * 100 if new_bars > 0 else 0
    
    print(f"\n🆕 NEW DATA in MSTR_40:")
    print(f"   Added Bars: {new_bars:,}")
    if new_bars > 0:
        print(f"   Time Range: {new_start.strftime('%Y-%m-%d %H:%M')} → {new_end.strftime('%Y-%m-%d %H:%M')}")
        print(f"   Price Move: ${new_price_start:.2f} → ${new_price_end:.2f} ({new_pct:+.2f}%)")
    
    # ========================================
    # SECTION 2: TRADINGVIEW SIGNAL ANALYSIS
    # ========================================
    print(f"\n{'='*120}")
    print(f"📡 SECTION 2: TRADINGVIEW SIGNALS IN MSTR_40")
    print(f"{'='*120}\n")
    
    # Count TradingView markers
    call_markers = df40[df40['Buy Call marker'] == 1]
    put_markers = df40[df40['Buy Put marker'] == 1]
    
    print(f"TradingView Markers Found:")
    print(f"   Buy CALL markers: {len(call_markers):,}")
    print(f"   Buy PUT markers:  {len(put_markers):,}")
    print(f"   Total entries:    {len(call_markers) + len(put_markers):,}")
    
    if len(call_markers) + len(put_markers) > 0:
        print(f"\n🔍 Analyzing signal quality...")
        
        all_signals = pd.concat([
            call_markers.assign(trade_type='CALL'),
            put_markers.assign(trade_type='PUT')
        ]).sort_values('timestamp')
        
        # Check filter status at signal times
        signals_pass_volatility = all_signals['volatilityNormal'].sum()
        signals_pass_time = all_signals['goodTradingTime'].sum()
        signals_pass_both = all_signals['both_filters'].sum()
        
        print(f"\n📊 Filter Status at Signal Times:")
        print(f"   Pass volatilityNormal: {signals_pass_volatility}/{len(all_signals)} ({signals_pass_volatility/len(all_signals)*100:.1f}%)")
        print(f"   Pass goodTradingTime:  {signals_pass_time}/{len(all_signals)} ({signals_pass_time/len(all_signals)*100:.1f}%)")
        print(f"   Pass BOTH filters:     {signals_pass_both}/{len(all_signals)} ({signals_pass_both/len(all_signals)*100:.1f}%)")
        
        # Show signal distribution
        print(f"\n📅 Signal Distribution:")
        for trade_type in ['CALL', 'PUT']:
            type_signals = all_signals[all_signals['trade_type'] == trade_type]
            if len(type_signals) > 0:
                print(f"\n   {trade_type} Signals ({len(type_signals)}):")
                for _, sig in type_signals.iterrows():
                    time_str = sig['timestamp'].strftime('%m-%d %H:%M')
                    filt_status = "✅" if sig['both_filters'] else "❌"
                    adx = sig['ADX']
                    cci = sig['CCI']
                    print(f"      {time_str}  Price: ${sig['close']:>7.2f}  ADX: {adx:>5.1f}  CCI: {cci:>6.1f}  {filt_status}")
    
    # ========================================
    # SECTION 3: REGIME ANALYSIS
    # ========================================
    print(f"\n{'='*120}")
    print(f"🌊 SECTION 3: REGIME ANALYSIS")
    print(f"{'='*120}\n")
    
    def classify_regime(row):
        if pd.isna(row['ADX']):
            return 'LOADING'
        adx = row['ADX']
        if adx >= 25:
            return 'TRENDING'
        elif adx >= 20:
            return 'NEUTRAL'
        else:
            return 'WHIPSAW'
    
    for name, df in datasets:
        df['regime'] = df.apply(classify_regime, axis=1)
        regime_counts = df['regime'].value_counts()
        total = len(df[df['regime'] != 'LOADING'])
        
        print(f"{name}:")
        for regime in ['TRENDING', 'NEUTRAL', 'WHIPSAW', 'LOADING']:
            if regime in regime_counts:
                count = regime_counts[regime]
                pct = count / total * 100 if total > 0 and regime != 'LOADING' else count / len(df) * 100
                print(f"   {regime:<12}: {count:>6,} bars ({pct:>5.1f}%)")
        print()
    
    # ========================================
    # SECTION 4: FILTER EFFECTIVENESS
    # ========================================
    print(f"\n{'='*120}")
    print(f"🔒 SECTION 4: FILTER EFFECTIVENESS")
    print(f"{'='*120}\n")
    
    for name, df in datasets:
        total = len(df)
        vol_pass = df['volatilityNormal'].sum()
        time_pass = df['goodTradingTime'].sum()
        both_pass = df['both_filters'].sum()
        
        print(f"{name}:")
        print(f"   volatilityNormal pass: {vol_pass:>6,}/{total:>6,} ({vol_pass/total*100:>5.1f}%) - Blocks {total-vol_pass:,} bars")
        print(f"   goodTradingTime pass:  {time_pass:>6,}/{total:>6,} ({time_pass/total*100:>5.1f}%) - Blocks {total-time_pass:,} bars")
        print(f"   BOTH filters pass:     {both_pass:>6,}/{total:>6,} ({both_pass/total*100:>5.1f}%) - Blocks {total-both_pass:,} bars")
        print()
    
    # ========================================
    # SECTION 5: ATR VOLATILITY ANALYSIS
    # ========================================
    print(f"\n{'='*120}")
    print(f"💨 SECTION 5: ATR VOLATILITY ANALYSIS")
    print(f"{'='*120}\n")
    
    for name, df in datasets:
        atr_valid = df.dropna(subset=['atr5', 'atr20'])
        if len(atr_valid) == 0:
            print(f"{name}: No valid ATR data")
            continue
            
        atr14_avg = atr_valid['ATR'].mean()
        atr5_avg = atr_valid['atr5'].mean()
        atr20_avg = atr_valid['atr20'].mean()
        ratio = atr5_avg / atr20_avg if atr20_avg > 0 else 0
        
        # Volatility spike detection
        spikes = atr_valid[atr_valid['atr5'] > atr_valid['atr20'] * 1.15]
        
        print(f"{name}:")
        print(f"   ATR14 average:     ${atr14_avg:.4f}")
        print(f"   ATR5 average:      ${atr5_avg:.4f}")
        print(f"   ATR20 average:     ${atr20_avg:.4f}")
        print(f"   ATR5/ATR20 ratio:  {ratio:.4f} ({'EXPANDING' if ratio > 1.10 else 'CONTRACTING' if ratio < 0.95 else 'STABLE'})")
        print(f"   Volatility spikes: {len(spikes):,} bars ({len(spikes)/len(atr_valid)*100:.1f}%)")
        print()
    
    # ========================================
    # SECTION 6: TECHNICAL INDICATORS
    # ========================================
    print(f"\n{'='*120}")
    print(f"📈 SECTION 6: TECHNICAL INDICATOR SUMMARY")
    print(f"{'='*120}\n")
    
    for name, df in datasets:
        valid = df.dropna(subset=['ADX', 'CCI', 'ATR'])
        if len(valid) == 0:
            continue
            
        adx_avg = valid['ADX'].mean()
        cci_avg = valid['CCI'].mean()
        atr_avg = valid['ATR'].mean()
        
        # Count extreme readings
        adx_strong = len(valid[valid['ADX'] > 30])
        cci_extreme = len(valid[(valid['CCI'] > 100) | (valid['CCI'] < -100)])
        
        print(f"{name}:")
        print(f"   ADX average:       {adx_avg:.1f} ({'STRONG' if adx_avg > 30 else 'MODERATE' if adx_avg > 20 else 'WEAK'} trend)")
        print(f"   ADX > 30:          {adx_strong:,} bars ({adx_strong/len(valid)*100:.1f}%)")
        print(f"   CCI average:       {cci_avg:.1f} ({'OVERBOUGHT' if cci_avg > 100 else 'OVERSOLD' if cci_avg < -100 else 'NEUTRAL'})")
        print(f"   CCI extreme:       {cci_extreme:,} bars ({cci_extreme/len(valid)*100:.1f}%)")
        print(f"   ATR average:       ${atr_avg:.4f}")
        print()
    
    # ========================================
    # SECTION 7: PRICE ACTION PATTERNS
    # ========================================
    print(f"\n{'='*120}")
    print(f"📊 SECTION 7: PRICE ACTION PATTERNS IN MSTR_40")
    print(f"{'='*120}\n")
    
    # Analyze last 100 bars for recent patterns
    recent = df40.tail(100).copy()
    recent['bar_range'] = recent['high'] - recent['low']
    recent['body'] = abs(recent['close'] - recent['open'])
    recent['wick_ratio'] = (recent['bar_range'] - recent['body']) / recent['bar_range']
    
    avg_range = recent['bar_range'].mean()
    avg_body = recent['body'].mean()
    avg_wick = recent['wick_ratio'].mean()
    
    print(f"Last 100 bars analysis:")
    print(f"   Average Range: ${avg_range:.2f}")
    print(f"   Average Body:  ${avg_body:.2f} ({avg_body/avg_range*100:.1f}% of range)")
    print(f"   Wick Ratio:    {avg_wick*100:.1f}% (high = indecision)")
    
    # Find largest moves
    print(f"\n🔥 Largest Single-Bar Moves:")
    df40['bar_range'] = df40['high'] - df40['low']
    top_moves = df40.nlargest(10, 'bar_range')
    for _, row in top_moves.head(5).iterrows():
        time_str = row['timestamp'].strftime('%m-%d %H:%M')
        pct_move = (row['bar_range'] / row['open']) * 100
        direction = "UP" if row['close'] > row['open'] else "DOWN"
        print(f"   {time_str}  ${row['low']:.2f} → ${row['high']:.2f} (${row['bar_range']:.2f}, {pct_move:.2f}%) {direction}")
    
    # ========================================
    # SECTION 8: ENTRY OPPORTUNITY ANALYSIS
    # ========================================
    print(f"\n{'='*120}")
    print(f"🎯 SECTION 8: ENTRY OPPORTUNITY ANALYSIS")
    print(f"{'='*120}\n")
    
    # Find high-quality setups (pass filters + strong ADX + extreme CCI)
    opportunities = df40[
        (df40['both_filters'] == True) &
        (df40['ADX'] > 25) &
        ((df40['CCI'] > 100) | (df40['CCI'] < -100))
    ].copy()
    
    print(f"High-Quality Setups Found: {len(opportunities):,}")
    print(f"(Criteria: Both filters pass + ADX > 25 + CCI extreme)\n")
    
    if len(opportunities) > 0:
        # Classify by CCI
        overbought = opportunities[opportunities['CCI'] > 100]
        oversold = opportunities[opportunities['CCI'] < -100]
        
        print(f"   SHORT opportunities (CCI > 100):  {len(overbought):,}")
        print(f"   LONG opportunities (CCI < -100):  {len(oversold):,}")
        
        # Show top 5 of each
        if len(overbought) > 0:
            print(f"\n   📉 Top SHORT Setups:")
            for _, row in overbought.nlargest(5, 'CCI').iterrows():
                time_str = row['timestamp'].strftime('%m-%d %H:%M')
                print(f"      {time_str}  Price: ${row['close']:>7.2f}  ADX: {row['ADX']:>5.1f}  CCI: {row['CCI']:>7.1f}")
        
        if len(oversold) > 0:
            print(f"\n   📈 Top LONG Setups:")
            for _, row in oversold.nsmallest(5, 'CCI').iterrows():
                time_str = row['timestamp'].strftime('%m-%d %H:%M')
                print(f"      {time_str}  Price: ${row['close']:>7.2f}  ADX: {row['ADX']:>5.1f}  CCI: {row['CCI']:>7.1f}")
    else:
        print(f"   ⚠️ No high-quality setups detected in MSTR_40")
        print(f"   This suggests filters are working TOO aggressively OR market conditions unfavorable")
    
    # ========================================
    # SECTION 9: COMPARISON TO PREVIOUS DATA
    # ========================================
    print(f"\n{'='*120}")
    print(f"📊 SECTION 9: PERFORMANCE DELTA (MSTR_40 vs MSTR_39)")
    print(f"{'='*120}\n")
    
    # Compare key metrics
    metrics = ['volatilityNormal', 'goodTradingTime', 'both_filters']
    
    print(f"{'Metric':<30} {'MSTR_39':<20} {'MSTR_40':<20} {'Delta'}")
    print("-" * 120)
    
    for metric in metrics:
        pct39 = df39[metric].sum() / len(df39) * 100
        pct40 = df40[metric].sum() / len(df40) * 100
        delta = pct40 - pct39
        
        print(f"{metric:<30} {pct39:>6.2f}%              {pct40:>6.2f}%              {delta:>+6.2f}%")
    
    # ADX comparison
    adx39 = df39['ADX'].mean()
    adx40 = df40['ADX'].mean()
    print(f"{'ADX average':<30} {adx39:>6.2f}              {adx40:>6.2f}              {adx40-adx39:>+6.2f}")
    
    # ========================================
    # SECTION 10: FINDINGS & RECOMMENDATIONS
    # ========================================
    print(f"\n{'='*120}")
    print(f"💡 SECTION 10: FINDINGS & RECOMMENDATIONS")
    print(f"{'='*120}\n")
    
    findings = []
    solutions = []
    
    # Finding 1: New data comparison
    if new_bars > 0:
        if new_pct < -5:
            findings.append(f"🔴 FINDING 1: MSTR_40 continues downtrend with {new_pct:+.1f}% move in new data")
            solutions.append("   → SHORT strategies (rally/breakdown/crossunder) should outperform")
            solutions.append("   → Consider disabling LONG strategies in strong downtrends")
        elif new_pct > 5:
            findings.append(f"🟢 FINDING 1: MSTR_40 shows reversal with {new_pct:+.1f}% rally in new data")
            solutions.append("   → LONG strategies (pullback/breakout/crossover) may start working")
            solutions.append("   → Re-enable BTC alignment for confirmation")
        else:
            findings.append(f"🟡 FINDING 1: MSTR_40 showing consolidation with {new_pct:+.1f}% move")
            solutions.append("   → Both LONG and SHORT strategies may struggle")
            solutions.append("   → Wait for regime change before increasing position size")
    
    # Finding 2: Signal quality
    total_signals = len(call_markers) + len(put_markers)
    if total_signals > 0:
        pass_rate = signals_pass_both / len(all_signals) * 100 if len(all_signals) > 0 else 0
        if pass_rate < 50:
            findings.append(f"🔴 FINDING 2: Only {pass_rate:.1f}% of TradingView signals pass filters")
            solutions.append(f"   → Filters working correctly - rejecting {100-pass_rate:.1f}% of low-quality signals")
            solutions.append(f"   → This is EXPECTED behavior - quality over quantity")
        else:
            findings.append(f"🟢 FINDING 2: {pass_rate:.1f}% of signals pass filters (good quality)")
            solutions.append(f"   → Filters balanced - not too strict, not too lenient")
    else:
        findings.append(f"⚪ FINDING 2: No TradingView signals in MSTR_40 data")
        solutions.append(f"   → Cannot evaluate signal quality without markers")
        solutions.append(f"   → Run backtest on TradingView to generate signal data")
    
    # Finding 3: Regime
    regime40_trending = len(df40[df40['regime'] == 'TRENDING']) / len(df40[df40['regime'] != 'LOADING']) * 100
    if regime40_trending > 80:
        findings.append(f"🔴 FINDING 3: MSTR_40 is {regime40_trending:.1f}% TRENDING (very directional)")
        solutions.append(f"   → Momentum strategies (breakout/breakdown) should excel")
        solutions.append(f"   → Reversal strategies (pullback/rally) may struggle")
        solutions.append(f"   → Hold winners longer - strong trends persist")
    elif regime40_trending < 40:
        findings.append(f"🔴 FINDING 3: MSTR_40 is only {regime40_trending:.1f}% TRENDING (choppy)")
        solutions.append(f"   → Reduce position size - higher risk of whipsaw")
        solutions.append(f"   → Consider tighter stops to exit quickly")
        solutions.append(f"   → May need to wait for clearer regime")
    
    # Finding 4: Filter effectiveness
    both_pass_pct = df40['both_filters'].sum() / len(df40) * 100
    if both_pass_pct < 10:
        findings.append(f"🔴 FINDING 4: Only {both_pass_pct:.1f}% of bars pass both filters (very restrictive)")
        solutions.append(f"   → Consider relaxing volatilityNormal threshold (1.15 → 1.20)")
        solutions.append(f"   → Or shorten goodTradingTime block windows")
    elif both_pass_pct > 25:
        findings.append(f"🟡 FINDING 4: {both_pass_pct:.1f}% of bars pass both filters (may be too lenient)")
        solutions.append(f"   → Consider tightening volatilityNormal (1.15 → 1.10)")
        solutions.append(f"   → Or add additional quality gates")
    else:
        findings.append(f"🟢 FINDING 4: {both_pass_pct:.1f}% of bars pass filters (good balance)")
        solutions.append(f"   → Current filter settings appear optimal")
    
    # Print findings
    for i, finding in enumerate(findings, 1):
        print(finding)
    
    print(f"\n🛠️  RECOMMENDED SOLUTIONS:\n")
    for solution in solutions:
        print(solution)
    
    print(f"\n{'='*120}")
    print(f"✅ DEEP ANALYSIS COMPLETE")
    print(f"{'='*120}\n")

if __name__ == "__main__":
    deep_analyze_mstr40()

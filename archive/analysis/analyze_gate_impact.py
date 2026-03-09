import pandas as pd
import numpy as np

# Load MSTR_40 with actual TradingView signals
df = pd.read_csv('BATS_MSTR, 1-40.csv')
df.columns = df.columns.str.strip()
df['timestamp'] = pd.to_datetime(df['time'], unit='s')

# Count actual signals from TradingView
call_signals = df[df['Buy Call marker'] == 1]
put_signals = df[df['Buy Put marker'] == 1]

print(f'\n{"="*100}')
print(f'ACTUAL TRADINGVIEW SIGNALS IN MSTR_40')
print(f'{"="*100}\n')
print(f'Total Signals: {len(call_signals) + len(put_signals)}')
print(f'  CALLs: {len(call_signals)}')
print(f'  PUTs:  {len(put_signals)}')
print()

# Analyze CALLs
if len(call_signals) > 0:
    calls_with_tech = call_signals.copy()
    
    # Strong downtrend conditions (use original column names)
    strong_down_v21 = (calls_with_tech['close'] < calls_with_tech['EMA 21']) & (calls_with_tech['ADX'] > 30) & (calls_with_tech['EMA 9'] < calls_with_tech['EMA 21'])
    strong_down_v22 = (calls_with_tech['close'] < calls_with_tech['EMA 21']) & (calls_with_tech['ADX'] > 35) & (calls_with_tech['EMA 9'] < calls_with_tech['EMA 21'])
    
    # Calculate RSI proxy from CCI
    calls_with_tech['rsi'] = 50 + (calls_with_tech['CCI'] / 4)
    
    # Extreme oversold
    extreme_os_v21 = (calls_with_tech['rsi'] < 25) | (calls_with_tech['CCI'] < -200)
    extreme_os_v22 = (calls_with_tech['rsi'] < 30) | (calls_with_tech['CCI'] < -150)
    
    # Quality proxies
    high_quality = calls_with_tech['CCI'].abs() > 120
    far_from_ema = (calls_with_tech['close'] - calls_with_tech['EMA 21']).abs() / calls_with_tech['ATR'] > 2.0
    
    print(f'{"="*100}')
    print(f'CALL SIGNALS - TREND ANALYSIS')
    print(f'{"="*100}\n')
    
    print(f'Market Conditions at CALL Entry:')
    print(f'  In Strong Downtrend (ADX>30): {strong_down_v21.sum()}/{len(call_signals)} ({strong_down_v21.sum()/len(call_signals)*100:.1f}%)')
    print(f'  In Strong Downtrend (ADX>35): {strong_down_v22.sum()}/{len(call_signals)} ({strong_down_v22.sum()/len(call_signals)*100:.1f}%)')
    print()
    
    print(f'Quality Indicators at Entry:')
    print(f'  Extreme Oversold (RSI<25 or CCI<-200): {extreme_os_v21.sum()}/{len(call_signals)} ({extreme_os_v21.sum()/len(call_signals)*100:.1f}%)')
    print(f'  Oversold (RSI<30 or CCI<-150): {extreme_os_v22.sum()}/{len(call_signals)} ({extreme_os_v22.sum()/len(call_signals)*100:.1f}%)')
    print(f'  High Quality (|CCI|>120): {high_quality.sum()}/{len(call_signals)} ({high_quality.sum()/len(call_signals)*100:.1f}%)')
    print(f'  Far from EMA (>2 ATRs): {far_from_ema.sum()}/{len(call_signals)} ({far_from_ema.sum()/len(call_signals)*100:.1f}%)')
    print()
    
    # Calculate what would be blocked
    print(f'{"="*100}')
    print(f'BLOCKING ANALYSIS')
    print(f'{"="*100}\n')
    
    # V2.1: Block if strong downtrend and not extreme oversold
    blocked_v21 = strong_down_v21 & ~extreme_os_v21
    
    # V2.2: Block if strong downtrend and none of the quality gates
    allowed_v22_by_quality = high_quality | extreme_os_v22 | far_from_ema
    blocked_v22 = strong_down_v22 & ~allowed_v22_by_quality
    
    print(f'V2.1 (Aggressive - ADX>30):')
    print(f'  Would BLOCK: {blocked_v21.sum()}/{len(call_signals)} signals ({blocked_v21.sum()/len(call_signals)*100:.1f}%)')
    print(f'  Would ALLOW: {len(call_signals) - blocked_v21.sum()}/{len(call_signals)} signals ({(len(call_signals)-blocked_v21.sum())/len(call_signals)*100:.1f}%)')
    print()
    
    print(f'V2.2 (Quality-Aware - ADX>35):')
    print(f'  Would BLOCK: {blocked_v22.sum()}/{len(call_signals)} signals ({blocked_v22.sum()/len(call_signals)*100:.1f}%)')
    print(f'  Would ALLOW: {len(call_signals) - blocked_v22.sum()}/{len(call_signals)} signals ({(len(call_signals)-blocked_v22.sum())/len(call_signals)*100:.1f}%)')
    print()
    
    # Breakdown of v2.2 allowed trades in downtrend
    allowed_in_downtrend = strong_down_v22 & allowed_v22_by_quality
    if allowed_in_downtrend.sum() > 0:
        print(f'V2.2 Allowed in Strong Downtrend ({allowed_in_downtrend.sum()} trades):')
        print(f'  By High Quality: {(strong_down_v22 & high_quality).sum()}')
        print(f'  By Oversold: {(strong_down_v22 & extreme_os_v22 & ~high_quality).sum()}')
        print(f'  By Extended (Far from EMA): {(strong_down_v22 & far_from_ema & ~high_quality & ~extreme_os_v22).sum()}')
    print()
    
    print(f'{"="*100}')
    print(f'PROJECTED TRADE COUNTS (Total: CALLs + PUTs)')
    print(f'{"="*100}\n')
    
    total_no_gate = len(call_signals) + len(put_signals)
    calls_v21 = len(call_signals) - blocked_v21.sum()
    calls_v22 = len(call_signals) - blocked_v22.sum()
    
    print(f'No Gate:     {total_no_gate} trades ({len(call_signals)} CALLs + {len(put_signals)} PUTs)')
    print(f'V2.1 Gate:   {calls_v21 + len(put_signals)} trades ({calls_v21} CALLs + {len(put_signals)} PUTs) [{(calls_v21+len(put_signals)-total_no_gate)/total_no_gate*100:+.1f}%]')
    print(f'V2.2 Gate:   {calls_v22 + len(put_signals)} trades ({calls_v22} CALLs + {len(put_signals)} PUTs) [{(calls_v22+len(put_signals)-total_no_gate)/total_no_gate*100:+.1f}%]')
    print()
    
    print(f'{"="*100}')
    print(f'✅ CONCLUSION: V2.2 preserves {blocked_v21.sum() - blocked_v22.sum()} more CALL trades than V2.1')
    print(f'{"="*100}\n')

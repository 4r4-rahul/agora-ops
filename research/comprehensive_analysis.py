"""
COMPREHENSIVE TRADING SYSTEM RESEARCH FRAMEWORK
================================================
Professional-grade analysis across 6 phases:
1. Overall Performance Summary
2. 2R+ Winners Analysis
3. Daily Regime Classification
4. Regime-Specific Strategy Design
5. Parameter Exploration & Simulation
6. System Wiring & Final Design
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional
import warnings
warnings.filterwarnings('ignore')

class TradingResearchFramework:
    """
    Comprehensive research framework for regime-aware trading system development
    """
    
    def __init__(self, mstr_file: str, btc_file: Optional[str] = None):
        """Load and prepare data"""
        print("=" * 100)
        print("LOADING DATA")
        print("=" * 100)
        
        # Load MSTR data
        self.mstr_df = pd.read_csv(mstr_file)
        self.mstr_df['timestamp'] = pd.to_datetime(self.mstr_df['time'], unit='s')
        self.mstr_df['date'] = self.mstr_df['timestamp'].dt.date
        
        print(f"\nMSTR Data:")
        print(f"  Period: {self.mstr_df['date'].iloc[0]} to {self.mstr_df['date'].iloc[-1]}")
        print(f"  Bars: {len(self.mstr_df):,}")
        print(f"  Price: ${self.mstr_df['close'].iloc[0]:.2f} → ${self.mstr_df['close'].iloc[-1]:.2f} ({(self.mstr_df['close'].iloc[-1]/self.mstr_df['close'].iloc[0]-1)*100:.1f}%)")
        
        # Load BTC data if available
        self.btc_df = None
        if btc_file:
            try:
                self.btc_df = pd.read_csv(btc_file)
                self.btc_df['timestamp'] = pd.to_datetime(self.btc_df['time'], unit='s')
                self.btc_df['date'] = self.btc_df['timestamp'].dt.date
                print(f"\nBTC Data:")
                print(f"  Period: {self.btc_df['date'].iloc[0]} to {self.btc_df['date'].iloc[-1]}")
                print(f"  Bars: {len(self.btc_df):,}")
            except:
                print(f"\nBTC Data: Not available or failed to load")
        
        # Extract signals
        self.call_signals = self.mstr_df[self.mstr_df['Buy Call marker'] == 1].copy()
        self.put_signals = self.mstr_df[self.mstr_df['Buy Put marker'] == 1].copy()
        self.call_exits = self.mstr_df[self.mstr_df['Sell Call marker'] == 1].copy()
        self.put_exits = self.mstr_df[self.mstr_df['Sell Put marker'] == 1].copy()
        
        print(f"\nSignals:")
        print(f"  CALLs: {len(self.call_signals)}")
        print(f"  PUTs: {len(self.put_signals)}")
        print(f"  Total: {len(self.call_signals) + len(self.put_signals)}")
        
        # Reconstruct trades
        self.trades = []
        self._reconstruct_trades()
        
    def _reconstruct_trades(self):
        """Reconstruct individual trades from entry/exit signals"""
        print("\n" + "=" * 100)
        print("RECONSTRUCTING TRADES")
        print("=" * 100)
        
        # Process CALL trades
        for _, entry in self.call_signals.iterrows():
            entry_idx = entry.name
            entry_time = entry['timestamp']
            entry_price = entry['close']
            
            # Find next exit after this entry
            future_exits = self.call_exits[self.call_exits.index > entry_idx]
            
            if len(future_exits) > 0:
                exit_row = future_exits.iloc[0]
                exit_idx = exit_row.name
                exit_time = exit_row['timestamp']
                exit_price = exit_row['close']
                
                # Calculate max excursion (highest high between entry and exit)
                bars_between = self.mstr_df.loc[entry_idx:exit_idx]
                max_high = bars_between['high'].max()
                min_low = bars_between['low'].min()
                
                # Calculate R (risk units) - assuming 1R = 1 ATR at entry
                atr_at_entry = entry['ATR']
                r_achieved = (exit_price - entry_price) / atr_at_entry
                max_r_excursion = (max_high - entry_price) / atr_at_entry
                min_r_excursion = (min_low - entry_price) / atr_at_entry
                
                holding_time = (exit_time - entry_time).total_seconds() / 60  # minutes
                
                trade = {
                    'side': 'CALL',
                    'entry_time': entry_time,
                    'exit_time': exit_time,
                    'entry_idx': entry_idx,
                    'exit_idx': exit_idx,
                    'entry_price': entry_price,
                    'exit_price': exit_price,
                    'atr_at_entry': atr_at_entry,
                    'r_achieved': r_achieved,
                    'max_r_excursion': max_r_excursion,
                    'min_r_excursion': min_r_excursion,
                    'holding_time_min': holding_time,
                    'pnl_pct': (exit_price - entry_price) / entry_price * 100,
                    'winner': r_achieved > 0,
                    'big_winner': max_r_excursion >= 2.0,
                    # Technical indicators at entry
                    'entry_adx': entry['ADX'],
                    'entry_cci': entry['CCI'],
                    'entry_ema9': entry['EMA 9'],
                    'entry_ema21': entry['EMA 21'],
                    'entry_vwap': entry['VWAP'],
                    'entry_hour': entry_time.hour,
                    'entry_minute': entry_time.minute,
                    'date': entry['date']
                }
                self.trades.append(trade)
        
        # Process PUT trades
        for _, entry in self.put_signals.iterrows():
            entry_idx = entry.name
            entry_time = entry['timestamp']
            entry_price = entry['close']
            
            future_exits = self.put_exits[self.put_exits.index > entry_idx]
            
            if len(future_exits) > 0:
                exit_row = future_exits.iloc[0]
                exit_idx = exit_row.name
                exit_time = exit_row['timestamp']
                exit_price = exit_row['close']
                
                bars_between = self.mstr_df.loc[entry_idx:exit_idx]
                max_high = bars_between['high'].max()
                min_low = bars_between['low'].min()
                
                atr_at_entry = entry['ATR']
                # For PUTs, profit when price goes down
                r_achieved = (entry_price - exit_price) / atr_at_entry
                max_r_excursion = (entry_price - min_low) / atr_at_entry
                min_r_excursion = (entry_price - max_high) / atr_at_entry
                
                holding_time = (exit_time - entry_time).total_seconds() / 60
                
                trade = {
                    'side': 'PUT',
                    'entry_time': entry_time,
                    'exit_time': exit_time,
                    'entry_idx': entry_idx,
                    'exit_idx': exit_idx,
                    'entry_price': entry_price,
                    'exit_price': exit_price,
                    'atr_at_entry': atr_at_entry,
                    'r_achieved': r_achieved,
                    'max_r_excursion': max_r_excursion,
                    'min_r_excursion': min_r_excursion,
                    'holding_time_min': holding_time,
                    'pnl_pct': (entry_price - exit_price) / entry_price * 100,
                    'winner': r_achieved > 0,
                    'big_winner': max_r_excursion >= 2.0,
                    'entry_adx': entry['ADX'],
                    'entry_cci': entry['CCI'],
                    'entry_ema9': entry['EMA 9'],
                    'entry_ema21': entry['EMA 21'],
                    'entry_vwap': entry['VWAP'],
                    'entry_hour': entry_time.hour,
                    'entry_minute': entry_time.minute,
                    'date': entry['date']
                }
                self.trades.append(trade)
        
        self.trades_df = pd.DataFrame(self.trades)
        
        print(f"\nReconstructed {len(self.trades)} completed trades")
        if len(self.trades) > 0:
            print(f"  Winners: {self.trades_df['winner'].sum()} ({self.trades_df['winner'].sum()/len(self.trades)*100:.1f}%)")
            print(f"  2R+ Excursions: {self.trades_df['big_winner'].sum()} ({self.trades_df['big_winner'].sum()/len(self.trades)*100:.1f}%)")
    
    def phase1_performance_summary(self):
        """Phase 1: Overall Performance Analysis"""
        print("\n" + "=" * 100)
        print("PHASE 1: OVERALL PERFORMANCE SUMMARY")
        print("=" * 100)
        
        if len(self.trades) == 0:
            print("\n⚠️  No completed trades found in dataset")
            return
        
        df = self.trades_df
        
        # Overall metrics
        total_trades = len(df)
        winners = df['winner'].sum()
        losers = total_trades - winners
        win_rate = winners / total_trades * 100
        
        net_r = df['r_achieved'].sum()
        avg_r = df['r_achieved'].mean()
        avg_winner_r = df[df['winner']]['r_achieved'].mean() if winners > 0 else 0
        avg_loser_r = df[~df['winner']]['r_achieved'].mean() if losers > 0 else 0
        
        # Calculate drawdown in R
        cumulative_r = df['r_achieved'].cumsum()
        running_max = cumulative_r.expanding().max()
        drawdown_r = cumulative_r - running_max
        max_drawdown_r = drawdown_r.min()
        
        # Profit factor
        gross_profit_r = df[df['winner']]['r_achieved'].sum() if winners > 0 else 0
        gross_loss_r = abs(df[~df['winner']]['r_achieved'].sum()) if losers > 0 else 1
        profit_factor = gross_profit_r / gross_loss_r if gross_loss_r > 0 else 0
        
        # Time metrics
        avg_holding_time = df['holding_time_min'].mean()
        
        print(f"\n📊 OVERALL METRICS")
        print(f"{'─' * 100}")
        print(f"Total Trades:        {total_trades}")
        print(f"Winners:             {winners} ({win_rate:.1f}%)")
        print(f"Losers:              {losers} ({100-win_rate:.1f}%)")
        print(f"")
        print(f"Net R:               {net_r:+.2f}R")
        print(f"Avg R per trade:     {avg_r:+.2f}R")
        print(f"Avg Winner:          {avg_winner_r:+.2f}R")
        print(f"Avg Loser:           {avg_loser_r:+.2f}R")
        print(f"")
        print(f"Max Drawdown:        {max_drawdown_r:.2f}R")
        print(f"Profit Factor:       {profit_factor:.2f}")
        print(f"Avg Holding Time:    {avg_holding_time:.0f} minutes ({avg_holding_time/60:.1f} hours)")
        
        # Daily PnL breakdown
        daily_pnl = df.groupby('date').agg({
            'r_achieved': ['sum', 'count'],
            'winner': 'sum'
        }).round(2)
        daily_pnl.columns = ['R_PnL', 'Trades', 'Winners']
        daily_pnl['Win_Rate_%'] = (daily_pnl['Winners'] / daily_pnl['Trades'] * 100).round(1)
        daily_pnl = daily_pnl.sort_index()
        
        print(f"\n📅 DAILY PNL BREAKDOWN")
        print(f"{'─' * 100}")
        print(daily_pnl.to_string())
        
        # Identify best and worst days
        best_days = daily_pnl.nlargest(5, 'R_PnL')
        worst_days = daily_pnl.nsmallest(5, 'R_PnL')
        
        print(f"\n🏆 BEST 5 DAYS")
        print(f"{'─' * 100}")
        print(best_days.to_string())
        
        print(f"\n💥 WORST 5 DAYS")
        print(f"{'─' * 100}")
        print(worst_days.to_string())
        
        # Side breakdown
        call_trades = df[df['side'] == 'CALL']
        put_trades = df[df['side'] == 'PUT']
        
        print(f"\n📈 SIDE BREAKDOWN")
        print(f"{'─' * 100}")
        if len(call_trades) > 0:
            print(f"CALLs:  {len(call_trades)} trades, {call_trades['winner'].sum()/len(call_trades)*100:.1f}% WR, {call_trades['r_achieved'].sum():+.2f}R total, {call_trades['r_achieved'].mean():+.2f}R avg")
        if len(put_trades) > 0:
            print(f"PUTs:   {len(put_trades)} trades, {put_trades['winner'].sum()/len(put_trades)*100:.1f}% WR, {put_trades['r_achieved'].sum():+.2f}R total, {put_trades['r_achieved'].mean():+.2f}R avg")
        
        return {
            'total_trades': total_trades,
            'win_rate': win_rate,
            'net_r': net_r,
            'avg_r': avg_r,
            'max_dd_r': max_drawdown_r,
            'profit_factor': profit_factor,
            'avg_holding_min': avg_holding_time,
            'daily_pnl': daily_pnl
        }
    
    def phase2_big_winners_analysis(self):
        """Phase 2: Analyze 2R+ Winners"""
        print("\n" + "=" * 100)
        print("PHASE 2: 2R+ WINNERS ANALYSIS")
        print("=" * 100)
        
        if len(self.trades) == 0:
            print("\n⚠️  No trades found")
            return
        
        df = self.trades_df
        big_winners = df[df['big_winner']].copy()
        
        if len(big_winners) == 0:
            print("\n⚠️  No 2R+ winners found in dataset")
            return
        
        print(f"\n🎯 IDENTIFIED {len(big_winners)} TRADES WITH 2R+ EXCURSION")
        print(f"   ({len(big_winners)/len(df)*100:.1f}% of all trades)")
        
        # Add time bucket
        def get_time_bucket(hour, minute):
            total_min = hour * 60 + minute
            if 570 <= total_min < 600:  # 9:30-10:00
                return 'OPEN'
            elif 600 <= total_min < 900:  # 10:00-15:00
                return 'MID'
            else:  # 15:00-16:00
                return 'CLOSE'
        
        big_winners['time_bucket'] = big_winners.apply(
            lambda x: get_time_bucket(x['entry_hour'], x['entry_minute']), axis=1
        )
        
        # Analyze entry conditions
        big_winners['ema_above_21'] = big_winners['entry_ema9'] > big_winners['entry_ema21']
        big_winners['price_above_vwap'] = big_winners['entry_price'] > big_winners['entry_vwap']
        big_winners['strong_adx'] = big_winners['entry_adx'] > 30
        big_winners['extreme_cci'] = big_winners['entry_cci'].abs() > 150
        
        # Summary table
        summary_cols = ['side', 'entry_time', 'max_r_excursion', 'r_achieved',
                       'time_bucket', 'entry_adx', 'entry_cci',
                       'ema_above_21', 'price_above_vwap']
        
        print(f"\n📋 2R+ WINNERS DETAILS")
        print(f"{'─' * 100}")
        print(big_winners[summary_cols].to_string(index=False))
        
        # Statistical analysis
        print(f"\n📊 COMMON CHARACTERISTICS")
        print(f"{'─' * 100}")
        print(f"Side Distribution:")
        print(f"  CALLs: {len(big_winners[big_winners['side']=='CALL'])} ({len(big_winners[big_winners['side']=='CALL'])/len(big_winners)*100:.1f}%)")
        print(f"  PUTs:  {len(big_winners[big_winners['side']=='PUT'])} ({len(big_winners[big_winners['side']=='PUT'])/len(big_winners)*100:.1f}%)")
        
        print(f"\nTime of Day:")
        for bucket in ['OPEN', 'MID', 'CLOSE']:
            count = len(big_winners[big_winners['time_bucket'] == bucket])
            if count > 0:
                print(f"  {bucket:5}: {count} ({count/len(big_winners)*100:.1f}%)")
        
        print(f"\nEntry Conditions (% of 2R+ winners):")
        print(f"  EMA9 > EMA21:       {big_winners['ema_above_21'].sum()}/{len(big_winners)} ({big_winners['ema_above_21'].sum()/len(big_winners)*100:.1f}%)")
        print(f"  Price > VWAP:       {big_winners['price_above_vwap'].sum()}/{len(big_winners)} ({big_winners['price_above_vwap'].sum()/len(big_winners)*100:.1f}%)")
        print(f"  ADX > 30:           {big_winners['strong_adx'].sum()}/{len(big_winners)} ({big_winners['strong_adx'].sum()/len(big_winners)*100:.1f}%)")
        print(f"  |CCI| > 150:        {big_winners['extreme_cci'].sum()}/{len(big_winners)} ({big_winners['extreme_cci'].sum()/len(big_winners)*100:.1f}%)")
        
        print(f"\nIndicator Ranges:")
        print(f"  ADX:  {big_winners['entry_adx'].min():.1f} - {big_winners['entry_adx'].max():.1f} (avg: {big_winners['entry_adx'].mean():.1f})")
        print(f"  CCI:  {big_winners['entry_cci'].min():.1f} - {big_winners['entry_cci'].max():.1f} (avg: {big_winners['entry_cci'].mean():.1f})")
        
        # Exit efficiency
        print(f"\n🎯 EXIT EFFICIENCY")
        print(f"{'─' * 100}")
        big_winners['exit_capture_%'] = (big_winners['r_achieved'] / big_winners['max_r_excursion'] * 100).clip(0, 100)
        print(f"Average capture of max excursion: {big_winners['exit_capture_%'].mean():.1f}%")
        print(f"Trades that captured >50% of max: {len(big_winners[big_winners['exit_capture_%'] > 50])} ({len(big_winners[big_winners['exit_capture_%'] > 50])/len(big_winners)*100:.1f}%)")
        
        print(f"\n✅ KEY FINDINGS:")
        print(f"{'─' * 100}")
        print(f"2R+ winners tend to:")
        
        # Determine dominant patterns
        if big_winners['ema_above_21'].sum() / len(big_winners) > 0.7:
            print(f"  ✓ Occur when EMA9 > EMA21 (bullish structure)")
        if big_winners['strong_adx'].sum() / len(big_winners) > 0.5:
            print(f"  ✓ Happen in strong trending conditions (ADX > 30)")
        if big_winners['extreme_cci'].sum() / len(big_winners) > 0.4:
            print(f"  ✓ Enter at extreme CCI readings (mean reversion)")
        
        dominant_time = big_winners['time_bucket'].value_counts().idxmax()
        print(f"  ✓ Cluster during {dominant_time} session")
        
        if big_winners['exit_capture_%'].mean() < 60:
            print(f"  ⚠  Exits are cutting winners too early (avg {big_winners['exit_capture_%'].mean():.1f}% capture)")
        
        return big_winners
    
    def save_results(self, phase1_results, big_winners):
        """Save analysis results"""
        # Save trades
        self.trades_df.to_csv('research/all_trades_analyzed.csv', index=False)
        print(f"\n💾 Saved: research/all_trades_analyzed.csv")
        
        # Save big winners
        if big_winners is not None and len(big_winners) > 0:
            big_winners.to_csv('research/2r_plus_winners.csv', index=False)
            print(f"💾 Saved: research/2r_plus_winners.csv")
        
        # Save daily PnL
        if phase1_results and 'daily_pnl' in phase1_results:
            phase1_results['daily_pnl'].to_csv('research/daily_pnl.csv')
            print(f"💾 Saved: research/daily_pnl.csv")


if __name__ == "__main__":
    print("\n" + "=" * 100)
    print("COMPREHENSIVE TRADING SYSTEM RESEARCH")
    print("=" * 100)
    print(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    # Initialize framework
    framework = TradingResearchFramework(
        mstr_file='BATS_MSTR, 1-41.csv',
        btc_file='CRYPTO_BTCUSD, 1-4.csv'
    )
    
    # Phase 1: Performance Summary
    phase1_results = framework.phase1_performance_summary()
    
    # Phase 2: Big Winners Analysis
    big_winners = framework.phase2_big_winners_analysis()
    
    # Save results
    framework.save_results(phase1_results, big_winners)
    
    print("\n" + "=" * 100)
    print("PHASE 1 & 2 COMPLETE - Continuing to Phases 3-6...")
    print("=" * 100)

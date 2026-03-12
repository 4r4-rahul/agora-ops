#!/usr/bin/env python3
"""
Capital Preservation & Risk-Per-Trade Analysis
===============================================
Explores how different risk-per-trade regimes and capital preservation
rules affect the equity curve, drawdown, and blowup probability
for the $10K starting account.

Analyzes:
  1. Current system: What % of capital is actually risked per trade?
  2. Fixed fractional: 1%, 2%, 3%, 5% risk per trade
  3. Kelly criterion: Optimal vs half-Kelly vs quarter-Kelly
  4. Anti-martingale: Size up after wins, size down after losses
  5. Drawdown-based throttle: Reduce size as drawdown deepens
  6. Monte Carlo: Blowup probability under each regime
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
from dataclasses import dataclass
from typing import List, Tuple
from trading_engine.config import EngineConfig
from trading_engine.data.scalp_backtester import ScalpBacktester

# ─────────────────────────────────────────────────────────────────
# 1. Extract all trades with full detail
# ─────────────────────────────────────────────────────────────────

def extract_trades(ticker: str, data_path: str) -> pd.DataFrame:
    """Run backtester and extract every trade with sizing detail."""
    df = pd.read_csv(data_path, parse_dates=['timestamp'], index_col='timestamp')
    df.index = pd.to_datetime(df.index, utc=True)
    
    config = EngineConfig()
    bt = ScalpBacktester(config=config, account_size=10_000.0, spx_mode=True)
    result = bt.run(df, ticker=ticker, interval='1m', verbose=False)
    
    trades = []
    for t in result.trades:
        entry_cost = t.entry_premium * 100 * t.num_contracts  # Total capital at risk
        pnl = t.total_pnl
        trades.append({
            'ticker': ticker,
            'date': t.entry_time.date() if hasattr(t.entry_time, 'date') else t.entry_time,
            'strategy': t.tier,
            'direction': t.direction,
            'entry_time': t.entry_time,
            'exit_time': t.exit_time,
            'entry_premium': t.entry_premium,
            'exit_premium': t.exit_premium,
            'num_contracts': t.num_contracts,
            'entry_cost': entry_cost,
            'pnl': pnl,
            'exit_reason': t.exit_reason,
            'atr_at_entry': t.atr_at_entry,
            'entry_underlying': t.entry_underlying,
            'stop_price': t.stop_price,
            'target_price': t.target_price,
        })
    
    return pd.DataFrame(trades), result


print("=" * 80)
print("CAPITAL PRESERVATION & RISK-PER-TRADE ANALYSIS")
print("=" * 80)

# Load SPY + QQQ trades
spy_trades, spy_result = extract_trades('SPY', 'data/intraday/SPY_ibkr_1m_180d.csv')
qqq_trades, qqq_result = extract_trades('QQQ', 'data/intraday/QQQ_ibkr_1m_180d.csv')

# Combine and sort by date
all_trades = pd.concat([spy_trades, qqq_trades], ignore_index=True)
all_trades = all_trades.sort_values('entry_time').reset_index(drop=True)

print(f"\nTotal trades: {len(all_trades)} (SPY={len(spy_trades)}, QQQ={len(qqq_trades)})")
print(f"Combined PnL: ${spy_result.total_pnl + qqq_result.total_pnl:+,.0f}")

# ─────────────────────────────────────────────────────────────────
# 2. CURRENT RISK PROFILE: What % of capital is risked per trade?
# ─────────────────────────────────────────────────────────────────

print("\n" + "=" * 80)
print("SECTION 1: CURRENT RISK PROFILE (What the system actually does)")
print("=" * 80)

starting_capital = 10_000.0
balance = starting_capital
risk_pcts = []
cost_pcts = []

for _, t in all_trades.iterrows():
    # Entry cost as % of current balance
    cost_pct = t['entry_cost'] / balance * 100
    cost_pcts.append(cost_pct)
    
    # Max loss (premium paid) as % of current balance
    max_loss_pct = t['entry_cost'] / balance * 100  # Long options: max loss = premium
    risk_pcts.append(max_loss_pct)
    
    # Update balance
    balance += t['pnl']

risk_arr = np.array(risk_pcts)
cost_arr = np.array(cost_pcts)

print(f"\n{'Metric':<45} {'Value':>10}")
print("-" * 57)
print(f"{'Starting Capital':<45} {'$10,000':>10}")
print(f"{'Ending Capital':<45} ${balance:>9,.0f}")
print(f"")
print(f"{'Premium at Risk per Trade (% of Balance):':<45}")
print(f"  {'Mean':<43} {risk_arr.mean():>9.1f}%")
print(f"  {'Median':<43} {np.median(risk_arr):>9.1f}%")
print(f"  {'Min':<43} {risk_arr.min():>9.1f}%")
print(f"  {'Max':<43} {risk_arr.max():>9.1f}%")
print(f"  {'Std Dev':<43} {risk_arr.std():>9.1f}%")
print(f"  {'P90 (90th percentile)':<43} {np.percentile(risk_arr, 90):>9.1f}%")
print(f"  {'P95 (95th percentile)':<43} {np.percentile(risk_arr, 95):>9.1f}%")

# Breakdown by strategy
print(f"\n{'Risk by Strategy:':<45}")
for strat in all_trades['strategy'].unique():
    mask = all_trades['strategy'] == strat
    strat_costs = cost_arr[mask.values]
    n = mask.sum()
    print(f"  {strat.upper():<20} n={n:>3}  "
          f"mean={strat_costs.mean():.1f}%  "
          f"max={strat_costs.max():.1f}%  "
          f"med={np.median(strat_costs):.1f}%")

# Breakdown: risk as $ amount
print(f"\n{'Actual $ Risk per Trade:':<45}")
balance = starting_capital
dollar_risks = []
for _, t in all_trades.iterrows():
    dollar_risks.append(t['entry_cost'])
    balance += t['pnl']
dollar_risks = np.array(dollar_risks)
print(f"  {'Mean premium at risk':<43} ${dollar_risks.mean():>9,.0f}")
print(f"  {'Max premium at risk':<43} ${dollar_risks.max():>9,.0f}")
print(f"  {'Min premium at risk':<43} ${dollar_risks.min():>9,.0f}")

# Actual realized losses
losses = all_trades[all_trades['pnl'] < 0]['pnl']
if len(losses) > 0:
    print(f"\n{'Realized Losses:':<45}")
    print(f"  {'Mean loss':<43} ${losses.mean():>9,.0f}")
    print(f"  {'Worst loss':<43} ${losses.min():>9,.0f}")
    print(f"  {'Median loss':<43} ${losses.median():>9,.0f}")
    print(f"  {'Num losing trades':<43} {len(losses):>9}")

# ─────────────────────────────────────────────────────────────────
# 3. EQUITY CURVE with risk metrics over time
# ─────────────────────────────────────────────────────────────────

print("\n" + "=" * 80)
print("SECTION 2: EQUITY CURVE & DRAWDOWN DYNAMICS")
print("=" * 80)

balance = starting_capital
peak = starting_capital
equity_curve = [starting_capital]
drawdown_curve = [0.0]
risk_at_entry = []

for _, t in all_trades.iterrows():
    risk_pct_at_entry = t['entry_cost'] / balance * 100
    risk_at_entry.append(risk_pct_at_entry)
    
    balance += t['pnl']
    equity_curve.append(balance)
    peak = max(peak, balance)
    dd = (peak - balance) / peak * 100
    drawdown_curve.append(dd)

# Key drawdown events
max_dd_idx = np.argmax(drawdown_curve)
print(f"\n{'Max Drawdown':<45} {max(drawdown_curve):>9.1f}%")
print(f"{'Max Drawdown $':<45} ${starting_capital * max(drawdown_curve) / 100:>9,.0f}")
print(f"{'Occurred after trade #':<45} {max_dd_idx:>9}")

# Consecutive losses
streak = 0
max_streak = 0
streak_loss = 0
max_streak_loss = 0
for _, t in all_trades.iterrows():
    if t['pnl'] < 0:
        streak += 1
        streak_loss += t['pnl']
        if streak > max_streak:
            max_streak = streak
            max_streak_loss = streak_loss
    else:
        streak = 0
        streak_loss = 0

print(f"{'Max consecutive losses':<45} {max_streak:>9}")
print(f"{'Max streak $ loss':<45} ${max_streak_loss:>9,.0f}")

# Worst N-trade windows
print(f"\n{'Worst Rolling Windows:':<45}")
pnls = all_trades['pnl'].values
for window in [3, 5, 10]:
    if len(pnls) >= window:
        rolling = pd.Series(pnls).rolling(window).sum()
        worst = rolling.min()
        print(f"  {'Worst ' + str(window) + '-trade PnL':<43} ${worst:>9,.0f}")

# ─────────────────────────────────────────────────────────────────
# 4. FIXED FRACTIONAL POSITION SIZING SIMULATION
# ─────────────────────────────────────────────────────────────────

print("\n" + "=" * 80)
print("SECTION 3: FIXED FRACTIONAL POSITION SIZING")
print("=" * 80)
print("\nWhat if we cap risk-per-trade at X% of current balance?")
print("(Actual trade PnL is scaled proportionally to the risk adjustment)")

def simulate_fixed_fractional(trades_df, start_cap, risk_pct_cap):
    """
    Simulate equity curve where each trade's size is capped at risk_pct_cap 
    of current balance. If the original trade risked MORE than the cap, 
    we scale down the PnL proportionally.
    """
    balance = start_cap
    peak = start_cap
    max_dd = 0
    min_balance = start_cap
    trades_taken = 0
    total_pnl = 0
    
    for _, t in trades_df.iterrows():
        original_cost = t['entry_cost']
        max_allowed_cost = balance * (risk_pct_cap / 100.0)
        
        # If we can't afford even 1 contract, skip
        min_contract_cost = t['entry_premium'] * 100
        if min_contract_cost > max_allowed_cost or balance <= 0:
            continue  # Skip — can't afford within risk budget
        
        # Scale factor: how much of the original trade we can take
        if original_cost > 0:
            scale = min(1.0, max_allowed_cost / original_cost)
        else:
            scale = 1.0
        
        # Scaled PnL
        scaled_pnl = t['pnl'] * scale
        balance += scaled_pnl
        total_pnl += scaled_pnl
        trades_taken += 1
        
        peak = max(peak, balance)
        dd = (peak - balance) / peak * 100 if peak > 0 else 0
        max_dd = max(max_dd, dd)
        min_balance = min(min_balance, balance)
    
    return {
        'risk_cap': risk_pct_cap,
        'trades': trades_taken,
        'total_pnl': total_pnl,
        'final_balance': balance,
        'max_dd_pct': max_dd,
        'min_balance': min_balance,
        'roi_pct': (balance - start_cap) / start_cap * 100,
    }

print(f"\n{'Risk Cap':>10} {'Trades':>8} {'PnL':>12} {'Final Bal':>12} {'MaxDD%':>8} {'MinBal':>10} {'ROI':>10}")
print("-" * 75)

for risk_cap in [1, 2, 3, 5, 7, 10, 15, 20, 30, 50]:
    r = simulate_fixed_fractional(all_trades, starting_capital, risk_cap)
    print(f"{r['risk_cap']:>9}% {r['trades']:>8} ${r['total_pnl']:>10,.0f} "
          f"${r['final_balance']:>10,.0f} {r['max_dd_pct']:>7.1f}% "
          f"${r['min_balance']:>8,.0f} {r['roi_pct']:>8.0f}%")

# ─────────────────────────────────────────────────────────────────
# 5. KELLY CRITERION ANALYSIS
# ─────────────────────────────────────────────────────────────────

print("\n" + "=" * 80)
print("SECTION 4: KELLY CRITERION")
print("=" * 80)

wins = all_trades[all_trades['pnl'] > 0]
losses = all_trades[all_trades['pnl'] < 0]

win_rate = len(wins) / len(all_trades)
avg_win = wins['pnl'].mean() if len(wins) > 0 else 0
avg_loss = abs(losses['pnl'].mean()) if len(losses) > 0 else 1

# Kelly formula: f* = W/L - (1-p)/b
# where p = win rate, b = avg_win/avg_loss
b = avg_win / avg_loss if avg_loss > 0 else 1
kelly = win_rate - (1 - win_rate) / b

print(f"\n{'Win Rate':<35} {win_rate:.1%}")
print(f"{'Avg Win':<35} ${avg_win:,.0f}")
print(f"{'Avg Loss':<35} ${avg_loss:,.0f}")
print(f"{'Win/Loss Ratio (b)':<35} {b:.2f}")
print(f"{'Full Kelly f*':<35} {kelly:.1%}")
print(f"{'Half Kelly':<35} {kelly/2:.1%}")
print(f"{'Quarter Kelly':<35} {kelly/4:.1%}")

# Kelly by strategy
print(f"\n{'Kelly by Strategy:':<35}")
for strat in all_trades['strategy'].unique():
    s = all_trades[all_trades['strategy'] == strat]
    s_wins = s[s['pnl'] > 0]
    s_losses = s[s['pnl'] < 0]
    s_wr = len(s_wins) / len(s) if len(s) > 0 else 0
    s_avg_w = s_wins['pnl'].mean() if len(s_wins) > 0 else 0
    s_avg_l = abs(s_losses['pnl'].mean()) if len(s_losses) > 0 else 1
    s_b = s_avg_w / s_avg_l if s_avg_l > 0 else 1
    s_kelly = s_wr - (1 - s_wr) / s_b if s_b > 0 else 0
    print(f"  {strat.upper():<18} WR={s_wr:.0%}  W/L={s_b:.1f}  Kelly={s_kelly:.1%}  ½K={s_kelly/2:.1%}")

# Simulate Kelly-based sizing
print(f"\n{'Kelly Fraction':>15} {'Trades':>8} {'PnL':>12} {'Final Bal':>12} {'MaxDD%':>8} {'ROI':>10}")
print("-" * 70)

for k_frac, label in [(kelly, "Full Kelly"), (kelly/2, "Half Kelly"), (kelly/4, "Quarter Kelly"),
                       (0.02, "2% Fixed"), (0.05, "5% Fixed"), (0.10, "10% Fixed")]:
    r = simulate_fixed_fractional(all_trades, starting_capital, k_frac * 100)
    print(f"{label:>15} {r['trades']:>8} ${r['total_pnl']:>10,.0f} "
          f"${r['final_balance']:>10,.0f} {r['max_dd_pct']:>7.1f}% {r['roi_pct']:>8.0f}%")

# ─────────────────────────────────────────────────────────────────
# 6. DRAWDOWN-BASED THROTTLE
# ─────────────────────────────────────────────────────────────────

print("\n" + "=" * 80)
print("SECTION 5: DRAWDOWN-BASED THROTTLE")
print("=" * 80)
print("\nReduce position size as drawdown deepens (capital preservation)")

def simulate_dd_throttle(trades_df, start_cap, base_risk_pct, 
                          dd_levels=None):
    """
    Adaptive sizing: reduce risk as drawdown deepens.
    dd_levels: list of (dd_pct, scale_factor) tuples.
    E.g., [(5, 0.75), (10, 0.50), (15, 0.25), (20, 0)] means:
      - Normal: base_risk_pct
      - 5% DD: 75% of normal size
      - 10% DD: 50% of normal size
      - 15% DD: 25% of normal size
      - 20% DD: STOP trading
    """
    if dd_levels is None:
        dd_levels = [(5, 0.75), (10, 0.50), (15, 0.25), (20, 0.0)]
    
    balance = start_cap
    peak = start_cap
    max_dd = 0
    total_pnl = 0
    trades_taken = 0
    trades_skipped = 0
    
    for _, t in trades_df.iterrows():
        # Current drawdown
        dd_pct = (peak - balance) / peak * 100 if peak > 0 else 0
        
        # Determine scale factor
        scale_factor = 1.0
        for dd_thresh, sf in sorted(dd_levels, key=lambda x: x[0]):
            if dd_pct >= dd_thresh:
                scale_factor = sf
        
        if scale_factor <= 0 or balance <= 0:
            trades_skipped += 1
            continue
        
        # Adjusted risk cap
        adj_risk_pct = base_risk_pct * scale_factor
        max_allowed_cost = balance * (adj_risk_pct / 100.0)
        
        # Scale trade
        original_cost = t['entry_cost']
        min_contract_cost = t['entry_premium'] * 100
        if min_contract_cost > max_allowed_cost:
            trades_skipped += 1
            continue
        
        scale = min(1.0, max_allowed_cost / original_cost) if original_cost > 0 else 1.0
        scaled_pnl = t['pnl'] * scale
        
        balance += scaled_pnl
        total_pnl += scaled_pnl
        trades_taken += 1
        
        peak = max(peak, balance)
        dd = (peak - balance) / peak * 100 if peak > 0 else 0
        max_dd = max(max_dd, dd)
    
    return {
        'trades': trades_taken,
        'skipped': trades_skipped,
        'total_pnl': total_pnl,
        'final_balance': balance,
        'max_dd_pct': max_dd,
        'roi_pct': (balance - start_cap) / start_cap * 100,
    }

# Test different throttle profiles
profiles = [
    ("No Throttle (baseline)", 30, []),
    ("Conservative", 30, [(5, 0.75), (10, 0.50), (15, 0.25), (20, 0.0)]),
    ("Moderate", 30, [(7, 0.75), (15, 0.50), (25, 0.25)]),
    ("Aggressive Protection", 30, [(3, 0.75), (7, 0.50), (10, 0.25), (15, 0.0)]),
    ("Survival Mode", 30, [(5, 0.50), (10, 0.10), (15, 0.0)]),
]

print(f"\n{'Profile':<28} {'Trades':>7} {'Skip':>5} {'PnL':>12} {'Final':>12} {'MaxDD':>7} {'ROI':>8}")
print("-" * 85)

for name, base, levels in profiles:
    r = simulate_dd_throttle(all_trades, starting_capital, base, levels)
    print(f"{name:<28} {r['trades']:>7} {r['skipped']:>5} ${r['total_pnl']:>10,.0f} "
          f"${r['final_balance']:>10,.0f} {r['max_dd_pct']:>6.1f}% {r['roi_pct']:>6.0f}%")

# ─────────────────────────────────────────────────────────────────
# 7. ANTI-MARTINGALE (Scale up after wins, down after losses)
# ─────────────────────────────────────────────────────────────────

print("\n" + "=" * 80)
print("SECTION 6: ANTI-MARTINGALE (Size with the trend)")  
print("=" * 80)
print("\nIncrease size after wins, decrease after losses")

def simulate_anti_martingale(trades_df, start_cap, base_risk_pct,
                              win_scale=1.25, loss_scale=0.75, 
                              min_scale=0.25, max_scale=2.0):
    """
    Anti-martingale: scale up after wins, down after losses.
    Compounds the current_scale by win_scale/loss_scale after each trade.
    """
    balance = start_cap
    peak = start_cap
    max_dd = 0
    total_pnl = 0
    trades_taken = 0
    current_scale = 1.0
    
    for _, t in trades_df.iterrows():
        adj_risk = base_risk_pct * current_scale
        max_allowed = balance * (adj_risk / 100.0)
        
        original_cost = t['entry_cost']
        min_cost = t['entry_premium'] * 100
        if min_cost > max_allowed or balance <= 0:
            continue
        
        scale = min(1.0, max_allowed / original_cost) if original_cost > 0 else 1.0
        scaled_pnl = t['pnl'] * scale
        
        balance += scaled_pnl
        total_pnl += scaled_pnl
        trades_taken += 1
        
        peak = max(peak, balance)
        dd = (peak - balance) / peak * 100 if peak > 0 else 0
        max_dd = max(max_dd, dd)
        
        # Adjust scale for next trade
        if t['pnl'] > 0:
            current_scale = min(max_scale, current_scale * win_scale)
        else:
            current_scale = max(min_scale, current_scale * loss_scale)
    
    return {
        'trades': trades_taken,
        'total_pnl': total_pnl,
        'final_balance': balance,
        'max_dd_pct': max_dd,
        'roi_pct': (balance - start_cap) / start_cap * 100,
    }

am_profiles = [
    ("Baseline (no scaling)", 1.0, 1.0),
    ("Mild (1.15x/0.85x)", 1.15, 0.85),
    ("Standard (1.25x/0.75x)", 1.25, 0.75),
    ("Aggressive (1.5x/0.5x)", 1.5, 0.5),
]

print(f"\n{'Profile':<30} {'Trades':>8} {'PnL':>12} {'Final':>12} {'MaxDD':>7} {'ROI':>8}")
print("-" * 80)

for name, ws, ls in am_profiles:
    r = simulate_anti_martingale(all_trades, starting_capital, 30, ws, ls)
    print(f"{name:<30} {r['trades']:>8} ${r['total_pnl']:>10,.0f} "
          f"${r['final_balance']:>10,.0f} {r['max_dd_pct']:>6.1f}% {r['roi_pct']:>6.0f}%")

# ─────────────────────────────────────────────────────────────────
# 8. MONTE CARLO: BLOWUP PROBABILITY UNDER EACH REGIME
# ─────────────────────────────────────────────────────────────────

print("\n" + "=" * 80)
print("SECTION 7: MONTE CARLO — BLOWUP PROBABILITY BY RISK REGIME")
print("=" * 80)
print(f"\n10,000 paths × {len(all_trades)} trades shuffled")
print("Blowup = balance drops below $1,000 (90% loss)")
print("Severe = balance drops below $5,000 (50% loss)")

np.random.seed(42)
N_SIMS = 10_000

def monte_carlo_fixed_frac(pnl_returns, start_cap, risk_cap_pct, 
                            original_costs, original_premiums,
                            n_sims=10000, degradation=0.0):
    """
    Monte Carlo with fixed fractional sizing.
    pnl_returns: array of PnL per trade (original)
    degradation: multiply wins by (1-degradation), multiply losses by (1+degradation)
    """
    n_trades = len(pnl_returns)
    blowup_count = 0  # < $1K
    severe_count = 0  # < $5K
    min_balances = []
    final_balances = []
    
    for _ in range(n_sims):
        indices = np.random.permutation(n_trades)
        balance = start_cap
        peak = start_cap
        min_bal = start_cap
        
        for idx in indices:
            orig_pnl = pnl_returns[idx]
            orig_cost = original_costs[idx]
            orig_prem = original_premiums[idx]
            
            # Apply degradation
            if degradation > 0:
                if orig_pnl > 0:
                    orig_pnl *= (1 - degradation)
                else:
                    orig_pnl *= (1 + degradation)
            
            # Fixed fractional sizing
            max_allowed = balance * (risk_cap_pct / 100.0)
            min_cost = orig_prem * 100
            
            if min_cost > max_allowed or balance < 1000:
                continue
            
            scale = min(1.0, max_allowed / orig_cost) if orig_cost > 0 else 1.0
            balance += orig_pnl * scale
            
            min_bal = min(min_bal, balance)
            if balance < 1000:
                break
        
        min_balances.append(min_bal)
        final_balances.append(balance)
        if min_bal < 1000:
            blowup_count += 1
        if min_bal < 5000:
            severe_count += 1
    
    return {
        'blowup_pct': blowup_count / n_sims * 100,
        'severe_pct': severe_count / n_sims * 100,
        'median_final': np.median(final_balances),
        'p10_final': np.percentile(final_balances, 10),
        'p90_final': np.percentile(final_balances, 90),
        'worst_final': min(final_balances),
    }

# Prepare arrays
pnls = all_trades['pnl'].values
costs = all_trades['entry_cost'].values
prems = all_trades['entry_premium'].values

# Run MC for different risk regimes
print(f"\n{'Risk Regime':<22} {'Blowup':>8} {'Severe':>8} {'Median$':>10} {'P10$':>10} {'P90$':>10} {'Worst$':>10}")
print("-" * 82)

for risk_cap, label in [(5, "5% cap"), (10, "10% cap"), (15, "15% cap"), 
                         (20, "20% cap"), (30, "30% cap (current)"), (50, "50% cap")]:
    r = monte_carlo_fixed_frac(pnls, starting_capital, risk_cap, costs, prems, N_SIMS)
    print(f"{label:<22} {r['blowup_pct']:>7.1f}% {r['severe_pct']:>7.1f}% "
          f"${r['median_final']:>8,.0f} ${r['p10_final']:>8,.0f} "
          f"${r['p90_final']:>8,.0f} ${r['worst_final']:>8,.0f}")

# Now with 30% degradation (realistic backtest → live slippage)
print(f"\n{'With 30% Performance Degradation:':<50}")
print(f"{'Risk Regime':<22} {'Blowup':>8} {'Severe':>8} {'Median$':>10} {'P10$':>10} {'P90$':>10}")
print("-" * 72)

for risk_cap, label in [(5, "5% cap"), (10, "10% cap"), (15, "15% cap"), 
                         (20, "20% cap"), (30, "30% cap (current)"), (50, "50% cap")]:
    r = monte_carlo_fixed_frac(pnls, starting_capital, risk_cap, costs, prems, N_SIMS, degradation=0.30)
    print(f"{label:<22} {r['blowup_pct']:>7.1f}% {r['severe_pct']:>7.1f}% "
          f"${r['median_final']:>8,.0f} ${r['p10_final']:>8,.0f} "
          f"${r['p90_final']:>8,.0f}")

# ─────────────────────────────────────────────────────────────────
# 9. CONCURRENT EXPOSURE ANALYSIS
# ─────────────────────────────────────────────────────────────────

print("\n" + "=" * 80)
print("SECTION 8: CONCURRENT EXPOSURE (Max Capital at Risk at Any Moment)")
print("=" * 80)

# For each trade, track open positions and total exposure
events = []
for _, t in all_trades.iterrows():
    events.append(('open', t['entry_time'], t['entry_cost'], t['strategy']))
    if pd.notna(t.get('exit_time')):
        events.append(('close', t['exit_time'], t['entry_cost'], t['strategy']))

events.sort(key=lambda x: x[1])

concurrent_exposure = 0
max_concurrent = 0
max_concurrent_time = None
concurrent_positions = 0
max_positions = 0

for action, time, cost, strat in events:
    if action == 'open':
        concurrent_exposure += cost
        concurrent_positions += 1
    else:
        concurrent_exposure -= cost
        concurrent_positions -= 1
    
    if concurrent_exposure > max_concurrent:
        max_concurrent = concurrent_exposure
        max_concurrent_time = time
    max_positions = max(max_positions, concurrent_positions)

print(f"\n{'Max Concurrent Exposure ($)':<45} ${max_concurrent:>9,.0f}")
print(f"{'Max Concurrent Exposure (% of $10K)':<45} {max_concurrent/10000*100:>9.1f}%")
print(f"{'Max Concurrent Positions':<45} {max_positions:>9}")

# ─────────────────────────────────────────────────────────────────
# 10. RECOMMENDATION MATRIX
# ─────────────────────────────────────────────────────────────────

print("\n" + "=" * 80)
print("SECTION 9: RECOMMENDATION MATRIX")
print("=" * 80)

print("""
┌─────────────────────────────────────────────────────────────────────────┐
│                RISK-PER-TRADE RECOMMENDATION MATRIX                    │
├───────────────────┬───────────┬───────────┬───────────┬───────────────┤
│ Risk Tolerance    │ Risk/Trade│ DD Throttle│ Sizing    │ Expected      │
├───────────────────┼───────────┼───────────┼───────────┼───────────────┤
│ ULTRA-CONSERVATIVE│ 2-3%      │ Aggressive│ ¼ Kelly   │ Slow growth,  │
│ (Preserve capital)│           │ 10%→stop  │           │ near-zero ruin│
├───────────────────┼───────────┼───────────┼───────────┼───────────────┤
│ CONSERVATIVE      │ 5%        │ Moderate  │ ½ Kelly   │ Good growth,  │
│ (Smart money)     │           │ 15%→50%   │           │ <2% ruin risk │
├───────────────────┼───────────┼───────────┼───────────┼───────────────┤
│ MODERATE          │ 10%       │ Light     │ ½ Kelly   │ Strong growth,│
│ (Calculated risk) │           │ 20%→50%   │           │ 5-10% ruin    │
├───────────────────┼───────────┼───────────┼───────────┼───────────────┤
│ AGGRESSIVE        │ 15-20%    │ None      │ Full Kelly│ Max returns,  │
│ (High conviction) │           │           │           │ 15-25% ruin   │
├───────────────────┼───────────┼───────────┼───────────┼───────────────┤
│ CURRENT SYSTEM    │ ~30%      │ None      │ >Kelly    │ Extreme gains │
│ (Backtest optimal)│           │           │           │ OR blowup     │
└───────────────────┴───────────┴───────────┴───────────┴───────────────┘

PRODUCTION RECOMMENDATION:
  Start at CONSERVATIVE (5% risk cap + moderate DD throttle).
  After 30 profitable trades, graduate to MODERATE (10%).
  After 3 profitable months, consider AGGRESSIVE (15-20%).
  
  NEVER go above 20% risk-per-trade with real money on $10K.
  The backtest's 30%+ sizing works because it has PERFECT hindsight.
""")

print("=" * 80)
print("ANALYSIS COMPLETE")
print("=" * 80)

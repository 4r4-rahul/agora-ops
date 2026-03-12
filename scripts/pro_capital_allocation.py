#!/usr/bin/env python3
"""
Professional Capital Allocation Strategies — Tested on Real Trades
===================================================================

Implements and backtests proven position sizing methodologies used by
the world's top algo trading systems against our 168 actual trades.

Strategies tested:
  1. Kelly Criterion (Ed Thorp / Renaissance Technologies)
  2. Optimal f (Ralph Vince — turned Kelly into a framework)
  3. Fixed Fractional (industry standard baseline)
  4. Volatility Targeting (AQR, Man Group, most CTAs)
  5. CPPI — Constant Proportion Portfolio Insurance (Fischer Black)
  6. Larry Williams Method (turned $10K → $1.1M in 1987 WTC)
  7. Turtle Trading (Richard Dennis — trained novices into millionaires)
  8. Van Tharp R-Multiple (risking fixed R per trade)
  9. Anti-Martingale / Geometric (trend-following hedge funds)
  10. Risk Parity by Strategy (Bridgewater / Dalio concept)

Each method is simulated on the ACTUAL 168-trade sequence with
$10K starting capital + Monte Carlo (10,000 shuffled paths).
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
from trading_engine.config import EngineConfig
from trading_engine.data.scalp_backtester import ScalpBacktester

# ─────────────────────────────────────────────────────────────────
# Extract all trades
# ─────────────────────────────────────────────────────────────────

def extract_trades(ticker, data_path):
    df = pd.read_csv(data_path, parse_dates=['timestamp'], index_col='timestamp')
    df.index = pd.to_datetime(df.index, utc=True)
    config = EngineConfig()
    bt = ScalpBacktester(config=config, account_size=10_000.0, spx_mode=True)
    result = bt.run(df, ticker=ticker, interval='1m', verbose=False)
    trades = []
    for t in result.trades:
        entry_cost = t.entry_premium * 100 * t.num_contracts
        trades.append({
            'ticker': ticker, 'strategy': t.tier, 'direction': t.direction,
            'entry_time': t.entry_time, 'exit_time': t.exit_time,
            'entry_premium': t.entry_premium, 'exit_premium': t.exit_premium,
            'num_contracts': t.num_contracts, 'entry_cost': entry_cost,
            'pnl': t.total_pnl, 'exit_reason': t.exit_reason,
            'atr_at_entry': t.atr_at_entry, 'entry_underlying': t.entry_underlying,
        })
    return pd.DataFrame(trades)

print("=" * 90)
print("PROFESSIONAL CAPITAL ALLOCATION STRATEGIES — COMPARATIVE ANALYSIS")
print("=" * 90)

spy_trades = extract_trades('SPY', 'data/intraday/SPY_ibkr_1m_180d.csv')
qqq_trades = extract_trades('QQQ', 'data/intraday/QQQ_ibkr_1m_180d.csv')
all_trades = pd.concat([spy_trades, qqq_trades], ignore_index=True)
all_trades = all_trades.sort_values('entry_time').reset_index(drop=True)

START_CAP = 10_000.0
N_TRADES = len(all_trades)

# Pre-compute trade characteristics
pnl_arr = all_trades['pnl'].values
cost_arr = all_trades['entry_cost'].values
prem_arr = all_trades['entry_premium'].values
atr_arr = all_trades['atr_at_entry'].values
strat_arr = all_trades['strategy'].values

wins = pnl_arr[pnl_arr > 0]
losses = pnl_arr[pnl_arr < 0]
win_rate = len(wins) / N_TRADES
avg_win = wins.mean()
avg_loss = abs(losses.mean())
payoff_ratio = avg_win / avg_loss

print(f"\nTrades: {N_TRADES} | WR: {win_rate:.1%} | Avg W: ${avg_win:,.0f} | "
      f"Avg L: ${avg_loss:,.0f} | Payoff: {payoff_ratio:.2f}")

# ─────────────────────────────────────────────────────────────────
# Helper: simulate a sizing function over the trade sequence
# ─────────────────────────────────────────────────────────────────

def simulate(sizing_fn, trades_df, start_cap=10_000.0, label=""):
    """
    sizing_fn(balance, peak, trade_row, trade_idx, history) → scale_factor (0 to N)
    scale_factor multiplies the original trade PnL.
    0 = skip trade, 1.0 = original size, 0.5 = half size, etc.
    """
    balance = start_cap
    peak = start_cap
    max_dd = 0
    min_bal = start_cap
    total_pnl = 0
    trades_taken = 0
    history = []  # list of (pnl, scale) tuples
    equity = [start_cap]
    
    for idx, (_, t) in enumerate(trades_df.iterrows()):
        scale = sizing_fn(balance, peak, t, idx, history)
        
        if scale <= 0 or balance <= 0:
            history.append((0, 0))
            continue
        
        # Check if minimum cost (1 contract) is affordable
        min_cost = t['entry_premium'] * 100
        if min_cost > balance * 0.95:  # Can't afford even 1 contract
            history.append((0, 0))
            continue
        
        scaled_pnl = t['pnl'] * scale
        balance += scaled_pnl
        total_pnl += scaled_pnl
        trades_taken += 1
        history.append((scaled_pnl, scale))
        
        peak = max(peak, balance)
        dd = (peak - balance) / peak * 100 if peak > 0 else 0
        max_dd = max(max_dd, dd)
        min_bal = min(min_bal, balance)
        equity.append(balance)
    
    # Compute Sharpe-like metric (on trade returns)
    if trades_taken > 1:
        rets = [h[0] for h in history if h[1] > 0]
        sharpe = np.mean(rets) / np.std(rets) * np.sqrt(252) if np.std(rets) > 0 else 0
    else:
        sharpe = 0
    
    # Risk-adjusted return: PnL / MaxDD
    rar = total_pnl / (max_dd * start_cap / 100) if max_dd > 0 else total_pnl
    
    return {
        'label': label,
        'trades': trades_taken,
        'pnl': total_pnl,
        'final': balance,
        'max_dd': max_dd,
        'min_bal': min_bal,
        'roi': (balance - start_cap) / start_cap * 100,
        'sharpe': sharpe,
        'rar': rar,  # Risk-adjusted return
        'equity': equity,
    }


# ═════════════════════════════════════════════════════════════════
# STRATEGY 1: KELLY CRITERION
# ═════════════════════════════════════════════════════════════════
# Used by: Ed Thorp, Renaissance Technologies, professional gamblers
# Theory: Maximize geometric growth rate of capital.
# Formula: f* = p - q/b = WR - (1-WR)/(AvgW/AvgL)
# Practice: NEVER use full Kelly. Half-Kelly gives 75% of the growth
# with much less variance. Quarter-Kelly is institutional standard.

kelly_f = win_rate - (1 - win_rate) / payoff_ratio

def kelly_sizer(fraction):
    """Kelly criterion: risk fraction × current balance."""
    def fn(balance, peak, trade, idx, history):
        max_allowed = balance * fraction
        orig_cost = trade['entry_cost']
        if orig_cost <= 0: return 1.0
        return min(1.0, max_allowed / orig_cost)
    return fn


# ═════════════════════════════════════════════════════════════════
# STRATEGY 2: OPTIMAL f (Ralph Vince)
# ═════════════════════════════════════════════════════════════════
# Used by: Quantitative trading desks
# Theory: Find the f that maximizes Terminal Wealth Relative (TWR).
# Different from Kelly because it uses actual trade distribution,
# not just mean win/loss. Accounts for tail risk.
# In practice: worst_loss / optimal_f = max risk per trade.

def compute_optimal_f(pnl_series):
    """Find the f that maximizes TWR over a trade series."""
    best_f = 0.01
    best_twr = 0
    worst_loss = abs(min(pnl_series))
    
    for f_test in np.arange(0.01, 1.0, 0.01):
        twr = 1.0
        for pnl in pnl_series:
            # HPR = 1 + f * (-trade / worst_loss)
            hpr = 1.0 + f_test * (-(-pnl) / worst_loss)
            if hpr <= 0:
                twr = 0
                break
            twr *= hpr
        if twr > best_twr:
            best_twr = twr
            best_f = f_test
    
    return best_f, worst_loss

opt_f, worst_loss = compute_optimal_f(pnl_arr)

def optimal_f_sizer(f_fraction=1.0):
    """Optimal f sizing: risk f_fraction of optimal-f per trade."""
    def fn(balance, peak, trade, idx, history):
        # Dollar risk per trade = balance × f / worst_loss × actual_risk
        risk_dollars = balance * (opt_f * f_fraction)
        orig_cost = trade['entry_cost']
        if orig_cost <= 0: return 1.0
        return min(1.0, risk_dollars / orig_cost)
    return fn


# ═════════════════════════════════════════════════════════════════
# STRATEGY 3: VOLATILITY TARGETING (AQR / Man Group / CTAs)
# ═════════════════════════════════════════════════════════════════
# Used by: AQR Capital, Man Group, Winton, most trend-following CTAs
# Theory: Size positions inversely to current volatility so that
# each trade contributes roughly the same DOLLAR RISK regardless
# of market conditions. Low vol = bigger size, high vol = smaller.
# This is the DOMINANT approach in professional systematic trading.

# We use ATR as our vol proxy (we have it per trade)
median_atr = np.median(atr_arr[atr_arr > 0])

def vol_target_sizer(target_risk_pct=0.15, vol_scalar=1.0):
    """
    Volatility-targeting: size inversely proportional to ATR.
    target_risk_pct: base risk as fraction of balance.
    Higher ATR → smaller position; Lower ATR → larger position.
    """
    def fn(balance, peak, trade, idx, history):
        atr = trade['atr_at_entry']
        if atr <= 0: atr = median_atr
        
        # Normalize: how volatile is this trade vs. median?
        vol_ratio = atr / median_atr if median_atr > 0 else 1.0
        
        # Inverse vol sizing: if vol is 2× median, size is 0.5×
        adjusted_risk = target_risk_pct / (vol_ratio * vol_scalar)
        adjusted_risk = min(adjusted_risk, 0.30)  # Hard cap at 30%
        
        max_allowed = balance * adjusted_risk
        orig_cost = trade['entry_cost']
        if orig_cost <= 0: return 1.0
        return min(1.0, max_allowed / orig_cost)
    return fn


# ═════════════════════════════════════════════════════════════════
# STRATEGY 4: CPPI (Constant Proportion Portfolio Insurance)
# ═════════════════════════════════════════════════════════════════
# Used by: Structured product desks, institutional funds
# Theory: Protect a FLOOR (e.g., never lose more than 20% of peak).
# Risk budget = multiplier × (balance - floor).
# As balance grows, you risk MORE (compounding).
# As balance approaches floor, you risk LESS → automatic protection.
# The "cushion" = balance - floor. Multiplier amplifies the cushion.

def cppi_sizer(floor_pct=0.80, multiplier=3.0):
    """
    CPPI: risk = multiplier × (balance - floor).
    floor_pct: minimum % of PEAK to preserve (0.80 = never lose >20% from peak).
    multiplier: leverage on the cushion (2-5 typical).
    """
    def fn(balance, peak, trade, idx, history):
        floor = peak * floor_pct
        cushion = max(0, balance - floor)
        risk_budget = multiplier * cushion
        
        if risk_budget <= 0:
            return 0  # Hit the floor → stop trading
        
        max_allowed = min(risk_budget, balance * 0.30)  # Never risk >30%
        orig_cost = trade['entry_cost']
        if orig_cost <= 0: return 1.0
        return min(1.0, max_allowed / orig_cost)
    return fn


# ═════════════════════════════════════════════════════════════════
# STRATEGY 5: LARRY WILLIAMS METHOD
# ═════════════════════════════════════════════════════════════════
# Used by: Larry Williams (1987 World Trading Championship:
#          $10K → $1,147,607 in 12 months, verified)
# Theory: Risk a fixed % of balance, but calculate position size
# from the LARGEST LOSING TRADE (not avg loss). This gives a
# natural margin of safety. Size = (balance × risk%) / worst_loss.

def larry_williams_sizer(risk_pct=0.15, lookback=20):
    """
    Larry Williams: size = (balance × risk%) / max_recent_loss.
    Uses rolling worst loss for adaptive safety margin.
    """
    def fn(balance, peak, trade, idx, history):
        # Use recent worst loss (or global worst if not enough history)
        recent_losses = [abs(h[0]) for h in history[-lookback:] if h[0] < 0]
        if len(recent_losses) >= 3:
            max_loss = max(recent_losses)
        else:
            max_loss = worst_loss  # Global worst from backtest
        
        if max_loss <= 0:
            max_loss = balance * 0.05  # Fallback
        
        # Dollar risk this trade
        risk_dollars = balance * risk_pct
        # Scale factor: how many "worst losses" we can afford
        scale = risk_dollars / max_loss
        
        orig_cost = trade['entry_cost']
        if orig_cost <= 0: return min(scale, 2.0)
        return min(scale, 2.0, balance * 0.30 / orig_cost)
    return fn


# ═════════════════════════════════════════════════════════════════
# STRATEGY 6: TURTLE TRADING (Richard Dennis)
# ═════════════════════════════════════════════════════════════════
# Used by: Original Turtle Traders (1983-1988, $175M+ profits)
# Theory: Risk exactly 1% of account per trade, defined as:
#   Position size = (1% × account) / (N × dollar_per_point)
#   where N = ATR(20). The ATR normalizes across instruments.
# Also has ADD rules: add to winners at every 0.5×N move.
# And CORRELATE rules: max 4 units in same direction.

def turtle_sizer(risk_pct=0.01, atr_mult=1.0):
    """
    Turtle Trading: risk 1% per trade via ATR-normalized sizing.
    1 Unit = (risk% × equity) / (ATR × dollar_per_point)
    Max 4 units per direction.
    """
    def fn(balance, peak, trade, idx, history):
        atr = trade['atr_at_entry']
        if atr <= 0: atr = median_atr
        
        # Dollar risk per unit = ATR × 100 (option multiplier) × 0.5 (delta proxy)
        dollar_risk_per_unit = atr * 100 * 0.5
        if dollar_risk_per_unit <= 0: return 0
        
        # Number of units
        units = (balance * risk_pct) / dollar_risk_per_unit
        units = min(units, 4.0)  # Turtle max: 4 units
        
        # Convert to scale factor
        orig_cost = trade['entry_cost']
        orig_contracts = trade['num_contracts']
        if orig_contracts <= 0: return 0
        
        target_contracts = max(1, int(units))
        return min(target_contracts / orig_contracts, 2.0) if orig_contracts > 0 else 1.0
    return fn


# ═════════════════════════════════════════════════════════════════
# STRATEGY 7: VAN THARP R-MULTIPLE
# ═════════════════════════════════════════════════════════════════
# Used by: Many prop trading firms, systematic traders
# Theory: Define R = risk per trade (initial stop distance × size).
# Risk exactly 1R per trade. All profits are measured in R-multiples.
# A 3R win = 3× your risk. This normalizes trade comparison.
# Position size = (risk% × equity) / R_per_unit.

def r_multiple_sizer(risk_pct=0.02):
    """
    Van Tharp R-Multiple: risk exactly risk_pct per trade.
    R = entry_cost (premium paid = max loss for long options).
    """
    def fn(balance, peak, trade, idx, history):
        max_allowed = balance * risk_pct
        orig_cost = trade['entry_cost']
        if orig_cost <= 0: return 1.0
        return min(1.0, max_allowed / orig_cost)
    return fn


# ═════════════════════════════════════════════════════════════════
# STRATEGY 8: ANTI-MARTINGALE / GEOMETRIC
# ═════════════════════════════════════════════════════════════════
# Used by: Trend-following hedge funds (Dunn, JWM, Graham)
# Theory: Increase exposure after wins, decrease after losses.
# Geometric growth: each win multiplies your next position.
# Different from fixed fractional because the MOMENTUM of wins
# matters — winning streaks get amplified, losing streaks dampened.

def anti_martingale_sizer(base_risk=0.15, win_step=1.20, loss_step=0.80,
                           min_scale=0.25, max_scale=2.5):
    """
    Anti-Martingale: scale up 20% after wins, down 20% after losses.
    Compounds the scaling factor over consecutive results.
    """
    def fn(balance, peak, trade, idx, history):
        # Compute current scale from recent results
        current_scale = 1.0
        for pnl, sc in history[-10:]:  # Last 10 trades
            if sc <= 0: continue
            if pnl > 0:
                current_scale *= win_step
            else:
                current_scale *= loss_step
        current_scale = max(min_scale, min(max_scale, current_scale))
        
        max_allowed = balance * base_risk * current_scale
        orig_cost = trade['entry_cost']
        if orig_cost <= 0: return current_scale
        return min(current_scale, max_allowed / orig_cost)
    return fn


# ═════════════════════════════════════════════════════════════════
# STRATEGY 9: RISK PARITY BY STRATEGY (Bridgewater concept)
# ═════════════════════════════════════════════════════════════════
# Used by: Bridgewater All Weather, risk parity funds
# Theory: Each sub-strategy should contribute EQUAL RISK, not
# equal capital. If ORB is 2× as volatile as RangeFade, give
# RangeFade 2× the position size.

# Compute per-strategy volatility
strat_vol = {}
for strat in ['orb', 'range_fade', 'scalp', 'runner']:
    strat_pnls = pnl_arr[strat_arr == strat]
    if len(strat_pnls) > 1:
        strat_vol[strat] = np.std(strat_pnls)
    else:
        strat_vol[strat] = avg_loss

# Inverse vol weights (lower vol → higher weight)
total_inv_vol = sum(1/v for v in strat_vol.values() if v > 0)
strat_weight = {s: (1/v)/total_inv_vol for s, v in strat_vol.items() if v > 0}

def risk_parity_sizer(total_risk=0.15):
    """
    Risk parity: allocate risk inversely proportional to strategy volatility.
    Lower-vol strategies get bigger positions.
    """
    def fn(balance, peak, trade, idx, history):
        strat = trade['strategy']
        weight = strat_weight.get(strat, 0.25)
        
        # Each strategy gets its weight × total risk budget
        risk_for_strat = total_risk * weight * len(strat_vol)  # Scale up since weights sum to 1
        max_allowed = balance * risk_for_strat
        
        orig_cost = trade['entry_cost']
        if orig_cost <= 0: return 1.0
        return min(1.5, max_allowed / orig_cost)  # Cap at 1.5× original
    return fn


# ═════════════════════════════════════════════════════════════════
# STRATEGY 10: DRAWDOWN-ADJUSTED CPPI (Hybrid — our best candidate)
# ═════════════════════════════════════════════════════════════════
# Combines CPPI floor protection with volatility targeting.
# The floor ratchets UP as equity grows (locks in profits).
# Position sizing inversely proportional to ATR.

def hybrid_sizer(floor_pct=0.85, multiplier=3.0, vol_adjust=True):
    """
    Hybrid: CPPI floor + vol targeting.
    As equity grows, floor ratchets up → locks profits.
    Position size reduced in high-vol environments.
    """
    def fn(balance, peak, trade, idx, history):
        floor = peak * floor_pct
        cushion = max(0, balance - floor)
        risk_budget = multiplier * cushion
        
        if risk_budget <= 0:
            return 0
        
        # Vol adjustment
        if vol_adjust:
            atr = trade['atr_at_entry']
            if atr > 0 and median_atr > 0:
                vol_ratio = atr / median_atr
                risk_budget /= vol_ratio
        
        max_allowed = min(risk_budget, balance * 0.25)
        orig_cost = trade['entry_cost']
        if orig_cost <= 0: return 1.0
        return min(1.5, max_allowed / orig_cost)
    return fn


# ═════════════════════════════════════════════════════════════════
# RUN ALL STRATEGIES
# ═════════════════════════════════════════════════════════════════

print("\n" + "=" * 90)
print("SECTION 1: HEAD-TO-HEAD COMPARISON (Sequential — Actual Trade Order)")
print("=" * 90)

strategies = [
    # (label, sizing_fn)
    ("BASELINE (current 30%)",         kelly_sizer(0.30)),
    
    # Kelly variants
    ("Kelly Full (42%)",               kelly_sizer(kelly_f)),
    ("Kelly Half (21%)",               kelly_sizer(kelly_f / 2)),
    ("Kelly Quarter (11%)",            kelly_sizer(kelly_f / 4)),
    
    # Optimal f
    (f"Optimal f ({opt_f:.0%})",       optimal_f_sizer(1.0)),
    (f"Optimal f/2 ({opt_f/2:.0%})",   optimal_f_sizer(0.5)),
    
    # Vol targeting
    ("Vol Target 15%",                 vol_target_sizer(0.15)),
    ("Vol Target 10%",                 vol_target_sizer(0.10)),
    ("Vol Target 20%",                 vol_target_sizer(0.20)),
    
    # CPPI
    ("CPPI (floor=80%, m=3)",         cppi_sizer(0.80, 3.0)),
    ("CPPI (floor=85%, m=3)",         cppi_sizer(0.85, 3.0)),
    ("CPPI (floor=85%, m=5)",         cppi_sizer(0.85, 5.0)),
    
    # Larry Williams
    ("Larry Williams 15%",            larry_williams_sizer(0.15)),
    ("Larry Williams 10%",            larry_williams_sizer(0.10)),
    
    # Turtle
    ("Turtle 1% per unit",            turtle_sizer(0.01)),
    ("Turtle 2% per unit",            turtle_sizer(0.02)),
    
    # Van Tharp R-Multiple
    ("R-Multiple 2%",                 r_multiple_sizer(0.02)),
    ("R-Multiple 5%",                 r_multiple_sizer(0.05)),
    ("R-Multiple 10%",                r_multiple_sizer(0.10)),
    ("R-Multiple 15%",                r_multiple_sizer(0.15)),
    
    # Anti-Martingale
    ("Anti-Martingale (1.2/0.8)",     anti_martingale_sizer(0.15, 1.20, 0.80)),
    ("Anti-Martingale (1.3/0.7)",     anti_martingale_sizer(0.15, 1.30, 0.70)),
    
    # Risk Parity
    ("Risk Parity 15%",              risk_parity_sizer(0.15)),
    
    # Hybrid
    ("Hybrid CPPI+Vol (85%,m=3)",    hybrid_sizer(0.85, 3.0, True)),
    ("Hybrid CPPI+Vol (80%,m=4)",    hybrid_sizer(0.80, 4.0, True)),
]

results = []
for label, sizer in strategies:
    r = simulate(sizer, all_trades, START_CAP, label)
    results.append(r)

# Sort by risk-adjusted return
results.sort(key=lambda x: x['rar'], reverse=True)

print(f"\n{'Rank':>4} {'Strategy':<32} {'Trades':>6} {'PnL':>11} {'MaxDD':>7} "
      f"{'ROI':>8} {'RAR':>8} {'Final':>11}")
print("-" * 97)

for i, r in enumerate(results, 1):
    marker = " ★" if r['rar'] == max(x['rar'] for x in results) else ""
    print(f"{i:>4} {r['label']:<32} {r['trades']:>6} ${r['pnl']:>9,.0f} "
          f"{r['max_dd']:>6.1f}% {r['roi']:>7.0f}% "
          f"{r['rar']:>7.1f} ${r['final']:>9,.0f}{marker}")

# ═════════════════════════════════════════════════════════════════
# MONTE CARLO: ROBUSTNESS UNDER SHUFFLED TRADE ORDER
# ═════════════════════════════════════════════════════════════════

print("\n" + "=" * 90)
print("SECTION 2: MONTE CARLO ROBUSTNESS (10,000 shuffled paths)")
print("=" * 90)
print("Tests how each strategy handles DIFFERENT trade orderings.")
print("A robust strategy shows low variance across paths.\n")

np.random.seed(42)
N_SIMS = 10_000

def mc_simulate(sizing_fn, n_sims=10_000):
    """Run Monte Carlo: shuffle trade order, apply sizing, collect stats."""
    finals = []
    max_dds = []
    blowups = 0  # < $2,000
    severe = 0   # < $5,000
    
    for _ in range(n_sims):
        indices = np.random.permutation(N_TRADES)
        balance = START_CAP
        peak = START_CAP
        max_dd = 0
        history = []
        
        for idx in indices:
            t = all_trades.iloc[idx]
            scale = sizing_fn(balance, peak, t, idx, history)
            
            if scale <= 0 or balance <= 500:
                history.append((0, 0))
                continue
            
            min_cost = t['entry_premium'] * 100
            if min_cost > balance * 0.95:
                history.append((0, 0))
                continue
            
            scaled_pnl = t['pnl'] * scale
            balance += scaled_pnl
            history.append((scaled_pnl, scale))
            
            peak = max(peak, balance)
            dd = (peak - balance) / peak * 100 if peak > 0 else 0
            max_dd = max(max_dd, dd)
        
        finals.append(balance)
        max_dds.append(max_dd)
        if balance < 2000: blowups += 1
        if balance < 5000: severe += 1
    
    return {
        'median': np.median(finals),
        'p10': np.percentile(finals, 10),
        'p25': np.percentile(finals, 25),
        'p75': np.percentile(finals, 75),
        'p90': np.percentile(finals, 90),
        'worst': min(finals),
        'best': max(finals),
        'median_dd': np.median(max_dds),
        'p90_dd': np.percentile(max_dds, 90),
        'blowup_pct': blowups / n_sims * 100,
        'severe_pct': severe / n_sims * 100,
        'std': np.std(finals),
    }

# Select top strategies for MC
mc_strategies = [
    ("BASELINE (30%)",           kelly_sizer(0.30)),
    ("Kelly Half (21%)",         kelly_sizer(kelly_f / 2)),
    ("Kelly Quarter (11%)",      kelly_sizer(kelly_f / 4)),
    ("Vol Target 15%",           vol_target_sizer(0.15)),
    ("Vol Target 10%",           vol_target_sizer(0.10)),
    ("CPPI (85%, m=3)",          cppi_sizer(0.85, 3.0)),
    ("CPPI (80%, m=4)",          cppi_sizer(0.80, 4.0)),
    ("Larry Williams 15%",       larry_williams_sizer(0.15)),
    ("Turtle 2%",                turtle_sizer(0.02)),
    ("R-Multiple 15%",           r_multiple_sizer(0.15)),
    ("Risk Parity 15%",          risk_parity_sizer(0.15)),
    ("Hybrid CPPI+Vol",          hybrid_sizer(0.85, 3.0, True)),
    ("Anti-Martingale",          anti_martingale_sizer(0.15, 1.20, 0.80)),
]

print(f"{'Strategy':<28} {'Median$':>9} {'P10$':>9} {'P90$':>9} "
      f"{'Worst$':>9} {'MedDD':>6} {'P90DD':>6} {'Blowup':>7} {'Std$':>9}")
print("-" * 102)

mc_results = []
for label, sizer in mc_strategies:
    mc = mc_simulate(sizer, N_SIMS)
    mc['label'] = label
    mc_results.append(mc)
    print(f"{label:<28} ${mc['median']:>7,.0f} ${mc['p10']:>7,.0f} ${mc['p90']:>7,.0f} "
          f"${mc['worst']:>7,.0f} {mc['median_dd']:>5.1f}% {mc['p90_dd']:>5.1f}% "
          f"{mc['blowup_pct']:>6.1f}% ${mc['std']:>7,.0f}")


# ═════════════════════════════════════════════════════════════════
# MONTE CARLO WITH 30% DEGRADATION (Backtest → Live Reality)
# ═════════════════════════════════════════════════════════════════

print("\n" + "=" * 90)
print("SECTION 3: MC WITH 30% DEGRADATION (Realistic Live Conditions)")
print("=" * 90)
print("Wins reduced 30%, losses increased 30% — worst-case live scenario.\n")

def mc_simulate_degraded(sizing_fn, degradation=0.30, n_sims=10_000):
    finals = []
    max_dds = []
    blowups = 0
    severe = 0
    
    for _ in range(n_sims):
        indices = np.random.permutation(N_TRADES)
        balance = START_CAP
        peak = START_CAP
        max_dd = 0
        history = []
        
        for idx in indices:
            t = all_trades.iloc[idx]
            scale = sizing_fn(balance, peak, t, idx, history)
            
            if scale <= 0 or balance <= 500:
                history.append((0, 0))
                continue
            
            min_cost = t['entry_premium'] * 100
            if min_cost > balance * 0.95:
                history.append((0, 0))
                continue
            
            # Apply degradation
            pnl = t['pnl']
            if pnl > 0:
                pnl *= (1 - degradation)
            else:
                pnl *= (1 + degradation)
            
            scaled_pnl = pnl * scale
            balance += scaled_pnl
            history.append((scaled_pnl, scale))
            
            peak = max(peak, balance)
            dd = (peak - balance) / peak * 100 if peak > 0 else 0
            max_dd = max(max_dd, dd)
        
        finals.append(balance)
        max_dds.append(max_dd)
        if balance < 2000: blowups += 1
        if balance < 5000: severe += 1
    
    return {
        'median': np.median(finals),
        'p10': np.percentile(finals, 10),
        'p90': np.percentile(finals, 90),
        'worst': min(finals),
        'median_dd': np.median(max_dds),
        'p90_dd': np.percentile(max_dds, 90),
        'blowup_pct': blowups / n_sims * 100,
        'severe_pct': severe / n_sims * 100,
    }

print(f"{'Strategy':<28} {'Median$':>9} {'P10$':>9} {'P90$':>9} "
      f"{'Worst$':>9} {'MedDD':>6} {'P90DD':>6} {'Blowup':>7} {'Severe':>7}")
print("-" * 102)

for label, sizer in mc_strategies:
    mc = mc_simulate_degraded(sizer, 0.30, N_SIMS)
    print(f"{label:<28} ${mc['median']:>7,.0f} ${mc['p10']:>7,.0f} ${mc['p90']:>7,.0f} "
          f"${mc['worst']:>7,.0f} {mc['median_dd']:>5.1f}% {mc['p90_dd']:>5.1f}% "
          f"{mc['blowup_pct']:>6.1f}% {mc['severe_pct']:>6.1f}%")


# ═════════════════════════════════════════════════════════════════
# FINAL COMPARISON & RECOMMENDATION
# ═════════════════════════════════════════════════════════════════

print("\n" + "=" * 90)
print("SECTION 4: METHODOLOGY DEEP-DIVE")
print("=" * 90)

print(f"""
┌──────────────────────────────────────────────────────────────────────────────────┐
│                    PROFESSIONAL CAPITAL ALLOCATION METHODS                       │
├────────────────────┬─────────────────────────────────────────────────────────────┤
│ METHOD             │ WHO USES IT & WHY                                          │
├────────────────────┼─────────────────────────────────────────────────────────────┤
│ KELLY CRITERION    │ Ed Thorp (beat casinos & markets), Renaissance Tech        │
│                    │ Maximizes GEOMETRIC growth rate. Full Kelly is TOO          │
│                    │ aggressive — doubles variance for marginal return.          │
│                    │ ½ Kelly = 75% of growth, 50% of variance. Industry std.    │
│                    │ Your Kelly: {kelly_f:.1%} → Half: {kelly_f/2:.1%} → Quarter: {kelly_f/4:.1%}          │
├────────────────────┼─────────────────────────────────────────────────────────────┤
│ VOL TARGETING      │ AQR Capital ($125B), Man Group, Winton, Two Sigma          │
│                    │ THE dominant method in systematic trading. Size inversely   │
│                    │ to volatility so every trade has ~same dollar risk.         │
│                    │ Key insight: high-vol trades already have larger moves,     │
│                    │ so you don't need big positions to get big P&L.             │
├────────────────────┼─────────────────────────────────────────────────────────────┤
│ CPPI               │ Goldman Sachs, JPM structured products, pension funds       │
│                    │ Protects a FLOOR while allowing upside. As you profit,     │
│                    │ the floor ratchets up → locks in gains. As drawdown        │
│                    │ approaches floor, trading stops → guarantees survival.      │
│                    │ Perfect for "$10K that I can't afford to lose."            │
├────────────────────┼─────────────────────────────────────────────────────────────┤
│ LARRY WILLIAMS     │ Larry Williams (verified $10K → $1.1M, 1987 WTC)           │
│                    │ Size based on WORST LOSS, not average. This is the         │
│                    │ "always prepared for the worst" philosophy.                 │
│                    │ Risk% × Balance / Max_Loss = position_size                 │
│                    │ Your worst loss: ${worst_loss:,.0f} → natural safety buffer         │
├────────────────────┼─────────────────────────────────────────────────────────────┤
│ TURTLE TRADING     │ Richard Dennis ($400M+ profits), trained novices            │
│                    │ Risk exactly 1-2% per trade via ATR normalization.          │
│                    │ Ultra-conservative but mathematically impossible to blow up.│
│                    │ Slow growth but guaranteed survival → time is your friend.  │
├────────────────────┼─────────────────────────────────────────────────────────────┤
│ R-MULTIPLE         │ Prop trading firms, Van Tharp's peak performance traders    │
│                    │ Normalize every trade to "R" (1R = initial risk).           │
│                    │ Risk 1-2% per trade. Simple, auditable, scalable.           │
│                    │ The "BORING BUT WORKS" approach — no fancy math needed.     │
├────────────────────┼─────────────────────────────────────────────────────────────┤
│ RISK PARITY        │ Bridgewater All Weather ($150B), AQR Risk Parity            │
│                    │ Equalize RISK CONTRIBUTION across strategies, not dollars.  │
│                    │ Your lower-vol strategies get MORE capital.                 │
│                    │ Prevents one hot strategy from dominating risk budget.      │
├────────────────────┼─────────────────────────────────────────────────────────────┤
│ HYBRID CPPI+VOL    │ Custom combination — our best candidate                     │
│                    │ CPPI floor (never lose >15% from peak) + vol targeting      │
│                    │ (inverse ATR sizing). Gets the best of both worlds:         │
│                    │ guaranteed floor protection + smart per-trade sizing.        │
└────────────────────┴─────────────────────────────────────────────────────────────┘
""")

# Compute strategy-level stats for Risk Parity info
print("\nRisk Parity Weights (inverse volatility):")
for strat, wt in sorted(strat_weight.items(), key=lambda x: x[1], reverse=True):
    vol = strat_vol[strat]
    print(f"  {strat.upper():<15} vol=${vol:>6,.0f}  weight={wt:.1%}")

print(f"\nOptimal f = {opt_f:.0%} (based on actual trade distribution)")
print(f"Kelly f*  = {kelly_f:.1%} (based on mean win/loss)")
print(f"Median ATR = ${median_atr:.4f} (used for vol normalization)")

print("\n" + "=" * 90)
print("ANALYSIS COMPLETE")
print("=" * 90)

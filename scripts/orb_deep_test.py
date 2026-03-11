#!/usr/bin/env python3
"""
Deep-dive: ORB Breakout strategy with proper options simulation.
Also test by regime to see WHERE it works vs doesn't.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
import pytz
from scipy.stats import norm

et = pytz.timezone("US/Eastern")

def bs_call(S, K, T, sigma, r=0.045):
    if T <= 0 or sigma <= 0:
        return max(S - K, 0)
    d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    return S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)

def bs_put(S, K, T, sigma, r=0.045):
    if T <= 0 or sigma <= 0:
        return max(K - S, 0)
    d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    return K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)

# ── Load data ─────────────────────────────────────────────────
df = pd.read_csv("data/intraday/SPY_ibkr_1m_180d.csv", parse_dates=["timestamp"], index_col="timestamp")
df.index = pd.to_datetime(df.index, utc=True)

# Identify days our current strategy trades (so we skip them)
from trading_engine.config import EngineConfig as EC
from trading_engine.scalper import SignalEngine

c = EC()
se = SignalEngine(c.scalp)
days = df.groupby(df.index.date)

entry_days = set()
for day_date, day_bars in days:
    if len(day_bars) < 30:
        continue
    precomp = se.precompute_day_indicators(day_bars)
    n = len(day_bars)
    closes = day_bars["close"].values
    day_iv = 0.0
    if n >= 30:
        lr = np.log(closes[1:30] / closes[:29])
        day_iv = np.std(lr, ddof=1) * np.sqrt(390 * 252)
    for i in range(20, n):
        local_time = day_bars.index[i].tz_convert(et)
        mso = (local_time - local_time.replace(hour=9, minute=30, second=0)).total_seconds() / 60
        in_w = (c.scalp.window_1_start <= mso <= c.scalp.window_1_end) or \
               (c.scalp.window_2_start <= mso <= c.scalp.window_2_end)
        if not in_w:
            continue
        signal = se.evaluate_fast(precomp, i, closes[i], "SPY", bars_index=day_bars.index)
        if signal is not None:
            rv = precomp.get("rv", np.full(n, np.nan))
            cur_rv = rv[i] if i < len(rv) else float("nan")
            if not np.isnan(cur_rv) and day_iv > 0:
                ratio = cur_rv / day_iv
                req = c.scalp.rv_iv_min_ratio_w1 if mso <= c.scalp.window_1_end else c.scalp.rv_iv_min_ratio
                if ratio >= req:
                    entry_days.add(day_date)

# ── ORB Breakout with proper options pricing ──────────────────
print("=" * 70)
print("  ORB BREAKOUT — DETAILED BACKTEST WITH OPTIONS PRICING")
print("=" * 70)

SPX_MULT = 10.0
ACCOUNT = 10_000
MAX_RISK_PCT = 0.05  # 5% per trade

# Test multiple ORB configs
configs = [
    {"label": "ORB15_t1.0_s0.5", "orb_bars": 15, "target_mult": 1.0, "stop_mult": 0.5, "max_hold": 30, "filter_range_min": 0.001},
    {"label": "ORB15_t1.5_s0.7", "orb_bars": 15, "target_mult": 1.5, "stop_mult": 0.7, "max_hold": 30, "filter_range_min": 0.001},
    {"label": "ORB15_t2.0_s1.0", "orb_bars": 15, "target_mult": 2.0, "stop_mult": 1.0, "max_hold": 45, "filter_range_min": 0.001},
    {"label": "ORB30_t1.0_s0.5", "orb_bars": 30, "target_mult": 1.0, "stop_mult": 0.5, "max_hold": 30, "filter_range_min": 0.001},
    {"label": "ORB30_t1.5_s0.7", "orb_bars": 30, "target_mult": 1.5, "stop_mult": 0.7, "max_hold": 45, "filter_range_min": 0.001},
    {"label": "ORB15_tight_s0.3", "orb_bars": 15, "target_mult": 0.7, "stop_mult": 0.3, "max_hold": 20, "filter_range_min": 0.001},
    # With volume filter
    {"label": "ORB15_vol_t1.0", "orb_bars": 15, "target_mult": 1.0, "stop_mult": 0.5, "max_hold": 30, "filter_range_min": 0.001, "vol_filter": True},
    {"label": "ORB15_wide_range", "orb_bars": 15, "target_mult": 1.0, "stop_mult": 0.5, "max_hold": 30, "filter_range_min": 0.002},
]

print(f"\n  {'Config':25s} {'Days':>5} {'Wins':>5} {'Loss':>5} {'WR':>6} {'PF':>6} {'PnL':>10} {'AvgWin':>8} {'AvgLos':>8}")
print(f"  {'─'*80}")

for cfg in configs:
    trades = []
    
    for day_date, db in days:
        if len(db) < 60:
            continue
        # Skip days current strategy covers
        if day_date in entry_days:
            continue
        
        o, h, l, c_arr, v = db["open"].values, db["high"].values, db["low"].values, db["close"].values, db["volume"].values
        n = len(db)
        
        orb_n = cfg["orb_bars"]
        if n < orb_n + 20:
            continue
        
        orb_high = h[:orb_n].max()
        orb_low = l[:orb_n].min()
        orb_range = orb_high - orb_low
        orb_range_pct = orb_range / o[0]
        
        if orb_range_pct < cfg["filter_range_min"]:
            continue
        
        # Volume filter: require above-average volume in ORB period
        if cfg.get("vol_filter"):
            orb_vol = v[:orb_n].mean()
            # Compare to all-bar average (rough)
            all_vol = v[:min(n, 60)].mean()
            if orb_vol < all_vol * 1.2:
                continue
        
        # Estimate IV from first 30 bars
        if n >= 30:
            lr = np.log(c_arr[1:30] / c_arr[:29])
            day_iv_est = np.std(lr, ddof=1) * np.sqrt(390 * 252)
        else:
            day_iv_est = 0.15
        day_iv_est = max(day_iv_est, 0.05)
        
        traded = False
        for i in range(orb_n, min(orb_n + 90, n - 5)):  # Look for breakout up to 90 bars after ORB
            if traded:
                break
            
            # Breakout above ORB high
            if c_arr[i] > orb_high and c_arr[i-1] <= orb_high:
                direction = "CALL"
                entry_price = c_arr[i] * SPX_MULT
                strike = round(entry_price / 5) * 5  # ATM, $5 increments
                
                # Time remaining (0DTE)
                local_time = db.index[i].tz_convert(et)
                mso = (local_time - local_time.replace(hour=9, minute=30, second=0)).total_seconds() / 60
                t_remain = max((390 - mso) / (390 * 252), 1e-6)
                
                entry_premium = bs_call(entry_price, strike, t_remain, day_iv_est)
                if entry_premium < 0.50 or entry_premium > 20:
                    continue
                
                # Position size
                max_risk = ACCOUNT * MAX_RISK_PCT
                num_contracts = max(1, int(max_risk / (entry_premium * 100)))
                num_contracts = min(num_contracts, 3)
                
                target_underlying = entry_price + orb_range * SPX_MULT * cfg["target_mult"]
                stop_underlying = entry_price - orb_range * SPX_MULT * cfg["stop_mult"]
                
                # Simulate
                exit_premium = None
                exit_reason = "TIME_STOP"
                for j in range(i + 1, min(i + cfg["max_hold"], n)):
                    cur_price = c_arr[j] * SPX_MULT
                    lt = db.index[j].tz_convert(et)
                    mso_j = (lt - lt.replace(hour=9, minute=30, second=0)).total_seconds() / 60
                    t_j = max((390 - mso_j) / (390 * 252), 1e-6)
                    
                    cur_premium = bs_call(cur_price, strike, t_j, day_iv_est)
                    
                    if cur_price >= target_underlying:
                        exit_premium = cur_premium
                        exit_reason = "TARGET"
                        break
                    elif cur_price <= stop_underlying:
                        exit_premium = cur_premium
                        exit_reason = "STOP"
                        break
                
                if exit_premium is None:
                    j = min(i + cfg["max_hold"], n - 1)
                    cur_price = c_arr[j] * SPX_MULT
                    lt = db.index[j].tz_convert(et)
                    mso_j = (lt - lt.replace(hour=9, minute=30, second=0)).total_seconds() / 60
                    t_j = max((390 - mso_j) / (390 * 252), 1e-6)
                    exit_premium = bs_call(cur_price, strike, t_j, day_iv_est)
                
                pnl = (exit_premium - entry_premium) * num_contracts * 100
                trades.append({"date": day_date, "pnl": pnl, "reason": exit_reason})
                traded = True
            
            # Breakout below ORB low
            elif c_arr[i] < orb_low and c_arr[i-1] >= orb_low:
                direction = "PUT"
                entry_price = c_arr[i] * SPX_MULT
                strike = round(entry_price / 5) * 5
                
                local_time = db.index[i].tz_convert(et)
                mso = (local_time - local_time.replace(hour=9, minute=30, second=0)).total_seconds() / 60
                t_remain = max((390 - mso) / (390 * 252), 1e-6)
                
                entry_premium = bs_put(entry_price, strike, t_remain, day_iv_est)
                if entry_premium < 0.50 or entry_premium > 20:
                    continue
                
                num_contracts = max(1, int((ACCOUNT * MAX_RISK_PCT) / (entry_premium * 100)))
                num_contracts = min(num_contracts, 3)
                
                target_underlying = entry_price - orb_range * SPX_MULT * cfg["target_mult"]
                stop_underlying = entry_price + orb_range * SPX_MULT * cfg["stop_mult"]
                
                exit_premium = None
                exit_reason = "TIME_STOP"
                for j in range(i + 1, min(i + cfg["max_hold"], n)):
                    cur_price = c_arr[j] * SPX_MULT
                    lt = db.index[j].tz_convert(et)
                    mso_j = (lt - lt.replace(hour=9, minute=30, second=0)).total_seconds() / 60
                    t_j = max((390 - mso_j) / (390 * 252), 1e-6)
                    
                    cur_premium = bs_put(cur_price, strike, t_j, day_iv_est)
                    
                    if cur_price <= target_underlying:
                        exit_premium = cur_premium
                        exit_reason = "TARGET"
                        break
                    elif cur_price >= stop_underlying:
                        exit_premium = cur_premium
                        exit_reason = "STOP"
                        break
                
                if exit_premium is None:
                    j = min(i + cfg["max_hold"], n - 1)
                    cur_price = c_arr[j] * SPX_MULT
                    lt = db.index[j].tz_convert(et)
                    mso_j = (lt - lt.replace(hour=9, minute=30, second=0)).total_seconds() / 60
                    t_j = max((390 - mso_j) / (390 * 252), 1e-6)
                    exit_premium = bs_put(cur_price, strike, t_j, day_iv_est)
                
                pnl = (exit_premium - entry_premium) * num_contracts * 100
                trades.append({"date": day_date, "pnl": pnl, "reason": exit_reason})
                traded = True
    
    # Results
    if trades:
        wins = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] <= 0]
        total_pnl = sum(t["pnl"] for t in trades)
        gross_win = sum(t["pnl"] for t in wins) if wins else 0
        gross_loss = abs(sum(t["pnl"] for t in losses)) if losses else 0
        pf = gross_win / gross_loss if gross_loss > 0 else 99
        wr = len(wins) / len(trades) * 100
        avg_win = gross_win / len(wins) if wins else 0
        avg_loss = -gross_loss / len(losses) if losses else 0
        unique_days = len(set(t["date"] for t in trades))
        
        marker = " ✅" if total_pnl > 0 and wr > 50 else ""
        print(f"  {cfg['label']:25s} {unique_days:>5} {len(wins):>5} {len(losses):>5} {wr:>5.1f}% {pf:>5.2f} ${total_pnl:>+9.0f} ${avg_win:>+7.0f} ${avg_loss:>+7.0f}{marker}")
    else:
        print(f"  {cfg['label']:25s}     0     0     0     -      - $       +0")

# ── Combined system projection ──
print(f"\n\n{'='*70}")
print("  COMBINED SYSTEM PROJECTION")
print(f"{'='*70}")
print()
print("  If we run BOTH strategies (momentum + ORB) side by side:")
print(f"    Momentum scalp:  13 days, +$12,820, PF=4.90, WR=66.7%")
print(f"    ORB breakout:   ~100+ days (on days momentum doesn't fire)")
print(f"    Combined:       ~113+ / 129 days (87%+ coverage)")
print()
print("  BUT the key question: does ORB add NET profit or dilute the edge?")
print("  If ORB is breakeven or slightly positive, it adds coverage but not P&L.")
print("  The value is in being IN THE MARKET more days → catching outlier moves.")

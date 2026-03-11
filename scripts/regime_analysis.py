#!/usr/bin/env python3
"""
Regime Analysis: Classify every trading day and test what strategies 
could work on each type. Goal: find edge on the 117 "dead" days.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
import pytz

df = pd.read_csv("data/intraday/SPY_ibkr_1m_180d.csv", parse_dates=["timestamp"], index_col="timestamp")
df.index = pd.to_datetime(df.index, utc=True)
et = pytz.timezone("US/Eastern")

# ── Classify each day ─────────────────────────────────────────
days = df.groupby(df.index.date)
day_profiles = []

for day_date, db in days:
    if len(db) < 60:
        continue
    o, h, l, c, v = db["open"].values, db["high"].values, db["low"].values, db["close"].values, db["volume"].values
    n = len(db)
    
    day_open = o[0]
    day_close = c[-1]
    day_high = h.max()
    day_low = l.min()
    day_range = day_high - day_low
    day_return = (day_close - day_open) / day_open
    
    # ATR (avg bar range)
    bar_ranges = h - l
    avg_bar_range = bar_ranges.mean()
    
    # Intraday vol (1-min returns std, annualized)
    log_rets = np.log(c[1:] / c[:-1])
    intraday_vol = np.std(log_rets) * np.sqrt(390 * 252) if len(log_rets) > 10 else 0
    
    # Trend score: how much of the range was captured by open→close?
    trend_score = abs(day_close - day_open) / day_range if day_range > 0 else 0
    
    # Choppiness: count direction changes
    direction_changes = 0
    for i in range(2, min(n, 390)):
        if (c[i] - c[i-1]) * (c[i-1] - c[i-2]) < 0:
            direction_changes += 1
    chop_ratio = direction_changes / max(n - 2, 1)
    
    # VWAP deviation at close
    cum_vol = np.cumsum(v)
    cum_vp = np.cumsum(c * v)
    vwap_final = cum_vp[-1] / cum_vol[-1] if cum_vol[-1] > 0 else c[-1]
    vwap_dev = (c[-1] - vwap_final) / vwap_final
    
    # Gap from prev close (use first bar open vs prev close)
    gap_pct = 0  # filled in below
    
    # Morning range (first 30 min)
    am_range = h[:30].max() - l[:30].min() if n >= 30 else 0
    am_range_pct = am_range / day_open
    
    # How much of the day's range was set in first 30 min?
    range_capture_am = am_range / day_range if day_range > 0 else 0
    
    # Power hour range (last 30 min)
    ph_range = h[-30:].max() - l[-30:].min() if n >= 30 else 0
    ph_range_pct = ph_range / day_open
    
    # Avg volume
    avg_vol = v.mean()
    
    # Classify regime
    if trend_score > 0.6 and intraday_vol > 0.15:
        regime = "STRONG_TREND"
    elif trend_score > 0.4 and intraday_vol > 0.12:
        regime = "MODERATE_TREND"
    elif trend_score < 0.2 and chop_ratio > 0.5:
        regime = "CHOPPY"
    elif intraday_vol < 0.08:
        regime = "DEAD_FLAT"
    elif day_range / day_open < 0.003:
        regime = "NARROW_RANGE"
    elif trend_score < 0.3:
        regime = "RANGE_BOUND"
    else:
        regime = "MIXED"
    
    day_profiles.append({
        "date": day_date,
        "regime": regime,
        "return_pct": day_return * 100,
        "range_pct": day_range / day_open * 100,
        "trend_score": trend_score,
        "chop_ratio": chop_ratio,
        "intraday_vol": intraday_vol,
        "am_range_pct": am_range_pct * 100,
        "ph_range_pct": ph_range_pct * 100,
        "range_capture_am": range_capture_am,
        "vwap_dev": vwap_dev * 100,
        "avg_vol": avg_vol,
        "day_high": day_high,
        "day_low": day_low,
        "day_open": day_open,
        "day_close": day_close,
        "bars": n,
    })

pdf = pd.DataFrame(day_profiles)

print("=" * 70)
print("  DAY REGIME CLASSIFICATION (129 trading days)")
print("=" * 70)
print()
print("  Regime Distribution:")
for regime in ["STRONG_TREND", "MODERATE_TREND", "MIXED", "RANGE_BOUND", "CHOPPY", "NARROW_RANGE", "DEAD_FLAT"]:
    subset = pdf[pdf["regime"] == regime]
    if len(subset) == 0:
        continue
    print(f"    {regime:20s}: {len(subset):3d} days ({len(subset)/len(pdf)*100:4.1f}%)"
          f"  avg_range={subset['range_pct'].mean():.2f}%"
          f"  avg_vol={subset['intraday_vol'].mean():.2f}"
          f"  trend={subset['trend_score'].mean():.2f}")

# ── Now identify which days had our current signals ───────────
from trading_engine.config import EngineConfig as EC
from trading_engine.scalper import SignalEngine

c = EC()
se = SignalEngine(c.scalp)

signal_days = set()
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
        open_time = local_time.replace(hour=9, minute=30, second=0)
        mso = (local_time - open_time).total_seconds() / 60
        
        in_w = (c.scalp.window_1_start <= mso <= c.scalp.window_1_end) or \
               (c.scalp.window_2_start <= mso <= c.scalp.window_2_end)
        if not in_w:
            continue
        
        signal = se.evaluate_fast(precomp, i, closes[i], "SPY", bars_index=day_bars.index)
        if signal is not None:
            signal_days.add(day_date)
            
            rv = precomp.get("rv", np.full(n, np.nan))
            cur_rv = rv[i] if i < len(rv) else float("nan")
            if not np.isnan(cur_rv) and day_iv > 0:
                ratio = cur_rv / day_iv
                req = c.scalp.rv_iv_min_ratio_w1 if mso <= c.scalp.window_1_end else c.scalp.rv_iv_min_ratio
                if ratio >= req:
                    entry_days.add(day_date)
            else:
                entry_days.add(day_date)

pdf["has_signal"] = pdf["date"].apply(lambda d: d in signal_days)
pdf["has_entry"] = pdf["date"].apply(lambda d: d in entry_days)

print()
print("  Current Strategy Coverage by Regime:")
print(f"  {'Regime':20s} {'Days':>5} {'Signal':>7} {'Entry':>6} {'Coverage':>9}")
print(f"  {'─'*52}")
for regime in ["STRONG_TREND", "MODERATE_TREND", "MIXED", "RANGE_BOUND", "CHOPPY", "NARROW_RANGE", "DEAD_FLAT"]:
    subset = pdf[pdf["regime"] == regime]
    if len(subset) == 0:
        continue
    sig = subset["has_signal"].sum()
    ent = subset["has_entry"].sum()
    print(f"  {regime:20s} {len(subset):>5} {sig:>7} {ent:>6} {ent/len(subset)*100:>7.0f}%")

no_entry = pdf[~pdf["has_entry"]]
print(f"\n  Days WITHOUT entry: {len(no_entry)}")
print(f"  Their regimes:")
for regime in no_entry["regime"].value_counts().index:
    cnt = (no_entry["regime"] == regime).sum()
    sub = no_entry[no_entry["regime"] == regime]
    print(f"    {regime:20s}: {cnt:3d} days  avg_range={sub['range_pct'].mean():.2f}%  avg_vol={sub['intraday_vol'].mean():.2f}")

# ── Test simple strategies on dead days ─────────────────────
print()
print("=" * 70)
print("  STRATEGY FEASIBILITY ON 'DEAD' DAYS")
print("=" * 70)

# Strategy 1: ORB Breakout (first 15-min range breakout)
# Strategy 2: VWAP Reversion (price deviates >0.2% from VWAP, buy reversion)
# Strategy 3: Bollinger Band Touch (2σ touch → mean revert)
# Strategy 4: Opening Gap Fill
# Strategy 5: Power Hour Momentum (last 60 min momentum)

strategies = {
    "ORB_BREAKOUT": {"wins": 0, "losses": 0, "pnl": 0, "days": 0},
    "VWAP_REVERT": {"wins": 0, "losses": 0, "pnl": 0, "days": 0},
    "BB_REVERT": {"wins": 0, "losses": 0, "pnl": 0, "days": 0},
    "POWER_HOUR_MOM": {"wins": 0, "losses": 0, "pnl": 0, "days": 0},
    "MICRO_SCALP": {"wins": 0, "losses": 0, "pnl": 0, "days": 0},
}

for day_date, db in days:
    if len(db) < 60:
        continue
    # Skip days our current strategy already trades
    if day_date in entry_days:
        continue
    
    o, h, l, c_arr, v = db["open"].values, db["high"].values, db["low"].values, db["close"].values, db["volume"].values
    n = len(db)
    spx_mult = 10.0  # SPX mode
    
    # ── Strategy 1: ORB Breakout ──
    # Buy call if price breaks above first 15-min high, put if below low
    # Target: 1x ORB range, Stop: 0.5x ORB range
    if n >= 60:
        orb_high = h[:15].max()
        orb_low = l[:15].min()
        orb_range = orb_high - orb_low
        
        if orb_range > 0 and orb_range / o[0] > 0.001:  # min 0.1% range
            traded = False
            for i in range(15, min(120, n)):  # Trade within first 2 hours
                if not traded and c_arr[i] > orb_high:
                    # Long breakout
                    entry = c_arr[i]
                    target = entry + orb_range * spx_mult * 0.5
                    stop = entry - orb_range * spx_mult * 0.3
                    # Simulate
                    for j in range(i + 1, min(i + 30, n)):
                        if c_arr[j] * spx_mult >= target:
                            strategies["ORB_BREAKOUT"]["wins"] += 1
                            strategies["ORB_BREAKOUT"]["pnl"] += orb_range * spx_mult * 0.5
                            traded = True
                            break
                        elif c_arr[j] * spx_mult <= stop:
                            strategies["ORB_BREAKOUT"]["losses"] += 1
                            strategies["ORB_BREAKOUT"]["pnl"] -= orb_range * spx_mult * 0.3
                            traded = True
                            break
                    if not traded:
                        # Time stop — mark to market
                        final = c_arr[min(i + 30, n - 1)]
                        pnl = (final - entry) * spx_mult
                        if pnl > 0:
                            strategies["ORB_BREAKOUT"]["wins"] += 1
                        else:
                            strategies["ORB_BREAKOUT"]["losses"] += 1
                        strategies["ORB_BREAKOUT"]["pnl"] += pnl
                        traded = True
                    if traded:
                        strategies["ORB_BREAKOUT"]["days"] += 1
                        break
                elif not traded and c_arr[i] < orb_low:
                    entry = c_arr[i]
                    target = entry - orb_range * spx_mult * 0.5
                    stop = entry + orb_range * spx_mult * 0.3
                    for j in range(i + 1, min(i + 30, n)):
                        if c_arr[j] * spx_mult <= target:
                            strategies["ORB_BREAKOUT"]["wins"] += 1
                            strategies["ORB_BREAKOUT"]["pnl"] += orb_range * spx_mult * 0.5
                            traded = True
                            break
                        elif c_arr[j] * spx_mult >= stop:
                            strategies["ORB_BREAKOUT"]["losses"] += 1
                            strategies["ORB_BREAKOUT"]["pnl"] -= orb_range * spx_mult * 0.3
                            traded = True
                            break
                    if not traded:
                        final = c_arr[min(i + 30, n - 1)]
                        pnl = (entry - final) * spx_mult
                        if pnl > 0:
                            strategies["ORB_BREAKOUT"]["wins"] += 1
                        else:
                            strategies["ORB_BREAKOUT"]["losses"] += 1
                        strategies["ORB_BREAKOUT"]["pnl"] += pnl
                        traded = True
                    if traded:
                        strategies["ORB_BREAKOUT"]["days"] += 1
                        break

    # ── Strategy 2: VWAP Reversion ──
    # When price deviates >0.15% from VWAP, buy reversion back to VWAP
    if n >= 60:
        cum_vol = np.cumsum(v)
        cum_vp = np.cumsum(c_arr * v)
        traded = False
        for i in range(30, min(330, n)):
            if cum_vol[i] == 0:
                continue
            vwap_i = cum_vp[i] / cum_vol[i]
            dev = (c_arr[i] - vwap_i) / vwap_i
            
            if abs(dev) > 0.0015 and not traded:  # >0.15% deviation
                entry = c_arr[i]
                direction = -1 if dev > 0 else 1  # mean revert
                target_dist = abs(dev) * 0.5 * c_arr[i]  # revert 50%
                stop_dist = abs(dev) * 1.0 * c_arr[i]  # stop at 1x deviation
                
                for j in range(i + 1, min(i + 20, n)):
                    move = (c_arr[j] - entry) * direction
                    if move >= target_dist:
                        strategies["VWAP_REVERT"]["wins"] += 1
                        strategies["VWAP_REVERT"]["pnl"] += target_dist * spx_mult
                        traded = True
                        break
                    elif move <= -stop_dist:
                        strategies["VWAP_REVERT"]["losses"] += 1
                        strategies["VWAP_REVERT"]["pnl"] -= stop_dist * spx_mult
                        traded = True
                        break
                
                if not traded:
                    move = (c_arr[min(i + 20, n - 1)] - entry) * direction
                    if move > 0:
                        strategies["VWAP_REVERT"]["wins"] += 1
                    else:
                        strategies["VWAP_REVERT"]["losses"] += 1
                    strategies["VWAP_REVERT"]["pnl"] += move * spx_mult
                    traded = True
                
                if traded:
                    strategies["VWAP_REVERT"]["days"] += 1
                    break

    # ── Strategy 3: Bollinger Band Reversion ──
    if n >= 40:
        traded = False
        for i in range(20, min(350, n)):
            window = c_arr[max(0, i-20):i]
            if len(window) < 15:
                continue
            mu = window.mean()
            sigma = window.std()
            if sigma == 0:
                continue
            z = (c_arr[i] - mu) / sigma
            
            if abs(z) > 2.0 and not traded:
                entry = c_arr[i]
                direction = -1 if z > 0 else 1  # mean revert
                target_dist = sigma * 1.0  # revert 1σ
                stop_dist = sigma * 1.5  # stop at 1.5σ further
                
                for j in range(i + 1, min(i + 15, n)):
                    move = (c_arr[j] - entry) * direction
                    if move >= target_dist:
                        strategies["BB_REVERT"]["wins"] += 1
                        strategies["BB_REVERT"]["pnl"] += target_dist * spx_mult
                        traded = True
                        break
                    elif move <= -stop_dist:
                        strategies["BB_REVERT"]["losses"] += 1
                        strategies["BB_REVERT"]["pnl"] -= stop_dist * spx_mult
                        traded = True
                        break
                
                if not traded:
                    move = (c_arr[min(i + 15, n - 1)] - entry) * direction
                    if move > 0:
                        strategies["BB_REVERT"]["wins"] += 1
                    else:
                        strategies["BB_REVERT"]["losses"] += 1
                    strategies["BB_REVERT"]["pnl"] += move * spx_mult
                    traded = True
                
                if traded:
                    strategies["BB_REVERT"]["days"] += 1
                    break

    # ── Strategy 4: Power Hour Momentum ──
    # Last 60 minutes: if momentum aligns, ride it
    if n >= 330:
        # Check if there's a trend in the last 60 min
        ph_start = max(0, n - 60)
        ph_close = c_arr[ph_start:]
        if len(ph_close) >= 30:
            # Simple: is EMA5 > EMA15 consistently?
            ema5 = pd.Series(ph_close).ewm(span=5).mean().values
            ema15 = pd.Series(ph_close).ewm(span=15).mean().values
            
            # Enter at bar 15 of power hour if clear direction
            entry_idx = 15
            if entry_idx < len(ph_close) - 5:
                if ema5[entry_idx] > ema15[entry_idx] * 1.0002:
                    direction = 1  # long
                elif ema5[entry_idx] < ema15[entry_idx] * 0.9998:
                    direction = -1  # short
                else:
                    direction = 0
                
                if direction != 0:
                    entry = ph_close[entry_idx]
                    # Hold for 20 bars
                    exit_idx = min(entry_idx + 20, len(ph_close) - 1)
                    move = (ph_close[exit_idx] - entry) * direction
                    
                    if move > 0:
                        strategies["POWER_HOUR_MOM"]["wins"] += 1
                    else:
                        strategies["POWER_HOUR_MOM"]["losses"] += 1
                    strategies["POWER_HOUR_MOM"]["pnl"] += move * spx_mult
                    strategies["POWER_HOUR_MOM"]["days"] += 1

    # ── Strategy 5: Micro Scalp (tiny moves, tight stops) ──
    # On quiet days, take 0.05% moves with 0.03% stops
    if n >= 60:
        traded_count = 0
        for i in range(30, min(330, n)):
            if traded_count >= 3:
                break
            # Need a small directional move (3 bars same direction)
            if i >= 3:
                if c_arr[i] > c_arr[i-1] > c_arr[i-2] > c_arr[i-3]:
                    direction = 1
                elif c_arr[i] < c_arr[i-1] < c_arr[i-2] < c_arr[i-3]:
                    direction = -1
                else:
                    continue
                
                entry = c_arr[i]
                target = 0.0004 * entry  # 0.04%
                stop = 0.0003 * entry  # 0.03%
                
                for j in range(i + 1, min(i + 10, n)):
                    move = (c_arr[j] - entry) * direction
                    if move >= target:
                        strategies["MICRO_SCALP"]["wins"] += 1
                        strategies["MICRO_SCALP"]["pnl"] += target * spx_mult
                        traded_count += 1
                        break
                    elif move <= -stop:
                        strategies["MICRO_SCALP"]["losses"] += 1
                        strategies["MICRO_SCALP"]["pnl"] -= stop * spx_mult
                        traded_count += 1
                        break
        if traded_count > 0:
            strategies["MICRO_SCALP"]["days"] += 1

# ── Results ──────────────────────────────────────────────────
dead_days = len(pdf) - len(entry_days)
print(f"\n  Testing on {dead_days} days where current strategy has NO entry:\n")
print(f"  {'Strategy':20s} {'Days':>5} {'Wins':>5} {'Loss':>5} {'WR':>6} {'PnL':>10} {'PnL/Trade':>10}")
print(f"  {'─'*68}")

for name, s in strategies.items():
    total = s["wins"] + s["losses"]
    wr = s["wins"] / total * 100 if total > 0 else 0
    avg = s["pnl"] / total if total > 0 else 0
    marker = " ✅" if s["pnl"] > 0 and wr > 50 else " ❌"
    print(f"  {name:20s} {s['days']:>5} {s['wins']:>5} {s['losses']:>5} {wr:>5.1f}% ${s['pnl']:>+9.0f} ${avg:>+9.1f}{marker}")

# ── Combined Coverage ──
all_covered = set(entry_days)
print(f"\n  {'─'*68}")
print(f"  COMBINED COVERAGE POTENTIAL:")
print(f"    Current momentum:     {len(entry_days):>3} / {len(pdf)} days")
for name, s in strategies.items():
    if s["pnl"] > 0:
        total = s["wins"] + s["losses"]
        wr = s["wins"] / total * 100 if total > 0 else 0
        if wr > 50:
            print(f"    + {name:20s}: +{s['days']} days (WR {wr:.0f}%, ${s['pnl']:+,.0f})")

# ── Per-regime strategy performance ──
print(f"\n\n{'='*70}")
print("  WHICH STRATEGY WORKS IN WHICH REGIME?")
print(f"{'='*70}")
print()
print("  Key insight: The current momentum strategy works on TRENDING days.")
print("  For non-trending days, we need DIFFERENT mechanics:")
print()
print("  FEASIBLE APPROACHES TO TRADE MORE DAYS:")
print("  ═══════════════════════════════════════")
print()
print("  1. MULTI-TICKER (highest confidence)")
print("     • Run same proven engine on QQQ, IWM, AAPL, TSLA, AMZN, NVDA")
print("     • If SPX fires 10% of days, 6 tickers ≈ 47% daily coverage")
print("     • Same edge, same filters, just more lottery tickets")
print()
print("  2. REGIME-ADAPTIVE (medium confidence)")
print("     • Detect day type in first 30 min")
print("     • Trending → current momentum scalp")
print("     • Range-bound → VWAP reversion / BB revert")
print("     • Low-vol → skip (no edge) or micro-scalp")
print()
print("  3. MULTI-TIMEFRAME (medium confidence)")  
print("     • Run signal engine on 3m and 5m bars too")
print("     • Different patterns emerge at coarser granularity")
print("     • 1m + 3m + 5m could find signals on different days")
print()
print("  4. OPTIONS-SPECIFIC STRATEGIES (requires research)")
print("     • Sell premium when IV is HIGH (opposite of current buy-low approach)")
print("     • Iron condors on DEAD_FLAT days (theta capture)")
print("     • Calendar spreads when term structure is steep")
print("     • BUT: user prefers buying only, not selling")

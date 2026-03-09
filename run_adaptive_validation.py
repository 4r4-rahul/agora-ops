#!/usr/bin/env python3
"""
Adaptive Regime Validation Test
=================================
Runs the OPTIMIZED adaptive backtester over all 180 days of IBKR data
to verify the regime-adaptive parameters produce profitable results
across GREEN, YELLOW, and RED regimes.

Compares: Fixed params (old) vs Adaptive params (new)

Usage:
    python run_adaptive_validation.py
"""

import sys
import os
import json
from datetime import date, timedelta
from collections import defaultdict
from typing import Dict, List

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from trading_engine.config import EngineConfig
from trading_engine.data.intraday import IntradayFetcher, IntradayBar, TradingDay
from trading_engine.data.intraday_backtester import IntradayBacktester, IntradayResults


# ─── Data loading ─────────────────────────────────────────────────

def load_trading_days() -> List[TradingDay]:
    cache_path = os.path.join("data", "intraday", "SPY_ibkr_5m_180d.csv")
    df = pd.read_csv(cache_path, index_col=0, parse_dates=True)
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, utc=True)

    days_dict = {}
    for ts, row in df.iterrows():
        d = ts.date()
        if d not in days_dict:
            days_dict[d] = []
        days_dict[d].append(IntradayBar(
            timestamp=pd.Timestamp(ts).to_pydatetime(),
            open=float(row.get("open", 0)),
            high=float(row.get("high", 0)),
            low=float(row.get("low", 0)),
            close=float(row.get("close", 0)),
            volume=int(row.get("volume", 0)),
            vwap=float(row.get("vwap", 0)) if "vwap" in row.index else 0,
        ))

    return [
        TradingDay(date=d, bars=bars, ticker="SPY")
        for d, bars in sorted(days_dict.items())
        if len(bars) >= 30
    ]


def classify_regime(vix: float) -> str:
    if vix < 18: return "GREEN"
    elif vix <= 25: return "YELLOW"
    return "RED"


def tag_with_regimes(trading_days: List[TradingDay]):
    """Tag each TradingDay with regime from VIX."""
    import yfinance as yf
    vix_df = yf.download("^VIX", period="300d", progress=False)
    if isinstance(vix_df.columns, pd.MultiIndex):
        vix_df.columns = vix_df.columns.get_level_values(0)
    vix_df.columns = [c.lower() for c in vix_df.columns]

    vix_dates = {d.date(): float(row.get("close", 18))
                 for d, row in vix_df.iterrows() if hasattr(d, 'date')}

    regime_map = {}
    for td in trading_days:
        vix_val = 18.0
        for offset in range(5):
            check = td.date - timedelta(days=offset)
            if check in vix_dates:
                vix_val = vix_dates[check]
                break
        td._regime = classify_regime(vix_val)
        td._vix = vix_val
        regime_map[str(td.date)] = td._regime

    return regime_map


def print_comparison(label: str, old: IntradayResults, new: IntradayResults):
    """Side-by-side comparison."""
    C_G = "\033[92m"
    C_R = "\033[91m"
    C_Y = "\033[93m"
    C_B = "\033[1m"
    C_X = "\033[0m"

    def delta_arrow(old_v, new_v, higher_is_better=True):
        diff = new_v - old_v
        if abs(diff) < 0.01:
            return "  →"
        if (diff > 0 and higher_is_better) or (diff < 0 and not higher_is_better):
            return f"{C_G}  ↑{C_X}"
        return f"{C_R}  ↓{C_X}"

    print(f"\n  {C_B}{label}{C_X}")
    print(f"  {'─' * 66}")
    print(f"  {'Metric':<25} {'FIXED (old)':>15} {'ADAPTIVE (new)':>15}  {'Δ':>5}")
    print(f"  {'─' * 66}")

    metrics = [
        ("Trades", old.total_trades, new.total_trades, False),
        ("Win Rate %", old.win_rate, new.win_rate, True),
        ("Total P&L $", old.total_pnl, new.total_pnl, True),
        ("Profit Factor", old.profit_factor, new.profit_factor, True),
        ("Max DD %", old.max_drawdown_pct, new.max_drawdown_pct, False),
        ("Sharpe Ratio", old.sharpe_ratio, new.sharpe_ratio, True),
        ("Gamma Exits", old.gamma_blow_ups, new.gamma_blow_ups, False),
    ]

    for name, ov, nv, higher_better in metrics:
        arr = delta_arrow(ov, nv, higher_better)
        if "P&L" in name:
            oc = C_G if ov > 0 else C_R
            nc = C_G if nv > 0 else C_R
            print(f"  {name:<25} {oc}${ov:>13,.2f}{C_X} {nc}${nv:>13,.2f}{C_X} {arr}")
        elif "%" in name:
            print(f"  {name:<25} {ov:>14.1f}% {nv:>14.1f}% {arr}")
        else:
            print(f"  {name:<25} {ov:>15.2f} {nv:>15.2f} {arr}")
    print(f"  {'─' * 66}")


def main():
    C_G = "\033[92m"
    C_R = "\033[91m"
    C_Y = "\033[93m"
    C_C = "\033[96m"
    C_B = "\033[1m"
    C_X = "\033[0m"

    print(f"\n{C_B}{'═' * 78}{C_X}")
    print(f"{C_B}  ADAPTIVE REGIME BACKTESTER — VALIDATION TEST{C_X}")
    print(f"{C_B}  Fixed Params vs Optimized Regime-Adaptive Params{C_X}")
    print(f"{C_B}{'═' * 78}{C_X}\n")

    # Load data
    print(f"  Loading 180 days of IBKR 5-min SPY data ...")
    trading_days = load_trading_days()
    print(f"  ✅ {len(trading_days)} trading days\n")

    # Tag regimes
    print(f"  Classifying regimes from VIX ...")
    regime_map = tag_with_regimes(trading_days)

    green = sum(1 for v in regime_map.values() if v == "GREEN")
    yellow = sum(1 for v in regime_map.values() if v == "YELLOW")
    red = sum(1 for v in regime_map.values() if v == "RED")
    print(f"  {C_G}■{C_X} GREEN: {green}  {C_Y}■{C_X} YELLOW: {yellow}  {C_R}■{C_X} RED: {red}\n")

    config = EngineConfig()

    # ─── Run A: FIXED params (old way) ───────────────────────────

    print(f"{C_C}TEST A: FIXED PARAMETERS (old — all strategies, all days){C_X}")
    print(f"{'─' * 60}")
    print(f"  Δ=0.15, stop=2.0×, TP=50%, w=$1, entry=9:45-10:30")

    bt_fixed = IntradayBacktester(config)
    fixed_results = {}
    for strat in ["put_credit", "call_credit", "iron_condor"]:
        r = bt_fixed.run(trading_days, strategies=[strat])
        fixed_results[strat] = r
        wr_c = C_G if r.win_rate >= 70 else C_Y if r.win_rate >= 55 else C_R
        pnl_c = C_G if r.total_pnl > 0 else C_R
        print(f"  {strat:<15} {wr_c}{r.win_rate:>5.1f}% WR{C_X} | "
              f"{pnl_c}${r.total_pnl:>+10,.2f}{C_X} | PF={r.profit_factor:.2f}")

    # Compute combined fixed
    combined_fixed_pnl = sum(r.total_pnl for r in fixed_results.values())
    print(f"\n  Combined fixed P&L: {C_G if combined_fixed_pnl > 0 else C_R}"
          f"${combined_fixed_pnl:>+,.2f}{C_X}")

    # ─── Run B: ADAPTIVE params (new way) ────────────────────────

    print(f"\n{C_C}TEST B: ADAPTIVE REGIME PARAMETERS (new — optimized per regime){C_X}")
    print(f"{'─' * 60}")
    print(f"  GREEN:  Δ=0.12, stop=3.0×, TP=75%, w=$2 → iron_condor")
    print(f"  YELLOW: Δ=0.18, stop=3.0×, TP=75%, w=$2 → call_credit")
    print(f"  RED:    NO TRADE\n")

    bt_adaptive = IntradayBacktester(config)
    adaptive_result = bt_adaptive.run(
        trading_days,
        adaptive=True,
        regime_map=regime_map,
    )

    wr_c = C_G if adaptive_result.win_rate >= 70 else C_Y if adaptive_result.win_rate >= 55 else C_R
    pnl_c = C_G if adaptive_result.total_pnl > 0 else C_R
    print(f"  ADAPTIVE:   {wr_c}{adaptive_result.win_rate:>5.1f}% WR{C_X} | "
          f"{pnl_c}${adaptive_result.total_pnl:>+10,.2f}{C_X} | "
          f"PF={adaptive_result.profit_factor:.2f} | "
          f"{adaptive_result.total_trades} trades")

    # ─── Comparison ──────────────────────────────────────────────

    # Use iron_condor (best fixed) as the fixed baseline
    best_fixed = max(fixed_results.values(), key=lambda r: r.total_pnl)
    best_fixed_name = [k for k, v in fixed_results.items() if v is best_fixed][0]

    print_comparison(
        f"FIXED ({best_fixed_name}) vs ADAPTIVE",
        best_fixed, adaptive_result
    )

    # ─── Per-regime breakdown ────────────────────────────────────

    print(f"\n  {C_B}PER-REGIME BREAKDOWN (Adaptive){C_X}")
    print(f"  {'─' * 60}")

    for regime, color in [("GREEN", C_G), ("YELLOW", C_Y), ("RED", C_R)]:
        regime_trades = [t for t in adaptive_result.trades
                         if regime_map.get(str(t.date)) == regime]
        if not regime_trades:
            print(f"  {color}■{C_X} {regime:6s}: NO TRADES (as designed)")
            continue

        wins = sum(1 for t in regime_trades if t.total_pnl > 0)
        total = len(regime_trades)
        pnl = sum(t.total_pnl for t in regime_trades)
        wr = wins / total * 100 if total else 0
        wins_pnl = sum(t.total_pnl for t in regime_trades if t.total_pnl > 0)
        loss_pnl = abs(sum(t.total_pnl for t in regime_trades if t.total_pnl <= 0))
        pf = wins_pnl / loss_pnl if loss_pnl > 0 else float('inf')

        pnl_c = C_G if pnl > 0 else C_R
        print(f"  {color}■{C_X} {regime:6s}: {total} trades | "
              f"{wr:.0f}% WR | {pnl_c}${pnl:>+,.2f}{C_X} | PF={pf:.2f}")

    # ─── Exit reason breakdown ───────────────────────────────────

    print(f"\n  {C_B}EXIT REASONS (Adaptive){C_X}")
    print(f"  {'─' * 60}")
    reasons = defaultdict(int)
    for t in adaptive_result.trades:
        reasons[t.exit_reason] += 1
    total = max(len(adaptive_result.trades), 1)
    for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
        pct = count / total * 100
        print(f"  {reason:<20} {count:>4} ({pct:>5.1f}%)")

    # ─── Monthly P&L ─────────────────────────────────────────────

    print(f"\n  {C_B}MONTHLY P&L (Adaptive){C_X}")
    print(f"  {'─' * 60}")
    if adaptive_result.monthly_pnl:
        max_m = max(abs(v) for v in adaptive_result.monthly_pnl.values()) or 1
        for month, mpnl in adaptive_result.monthly_pnl.items():
            color = C_G if mpnl >= 0 else C_R
            bar_len = int(abs(mpnl) / max_m * 25) if max_m > 0 else 0
            print(f"  {month}  {color}{'█' * bar_len:<25} ${mpnl:>+10,.2f}{C_X}")

    # ─── VERDICT ─────────────────────────────────────────────────

    print(f"\n{C_B}{'═' * 78}{C_X}")
    print(f"{C_B}  VERDICT{C_X}")
    print(f"{C_B}{'═' * 78}{C_X}\n")

    improvement = adaptive_result.total_pnl - best_fixed.total_pnl
    imp_c = C_G if improvement > 0 else C_R
    print(f"  P&L improvement:   {imp_c}${improvement:>+,.2f}{C_X} "
          f"(adaptive vs best fixed)")

    total_return = (adaptive_result.ending_balance - adaptive_result.starting_balance) / \
                   adaptive_result.starting_balance * 100
    ann_return = total_return * (252 / max(adaptive_result.total_days, 1))

    print(f"  Total return:      {C_G if total_return > 0 else C_R}"
          f"{total_return:+.2f}%{C_X}")
    print(f"  Annualized return: {C_G if ann_return > 0 else C_R}"
          f"{ann_return:+.1f}%{C_X}")
    print(f"  Max drawdown:      {adaptive_result.max_drawdown_pct:.2f}%")
    print(f"  Sharpe ratio:      {adaptive_result.sharpe_ratio:.2f}")
    print(f"  Win rate:          {adaptive_result.win_rate:.1f}%")
    print(f"  Profit factor:     {adaptive_result.profit_factor:.2f}")
    print(f"  RED days skipped:  {red} (avoided potential losses)\n")

    # Score
    score = 0
    if adaptive_result.profit_factor >= 2.0: score += 3
    elif adaptive_result.profit_factor >= 1.5: score += 2
    elif adaptive_result.profit_factor >= 1.0: score += 1
    if adaptive_result.win_rate >= 80: score += 2
    elif adaptive_result.win_rate >= 70: score += 1
    if adaptive_result.max_drawdown_pct < 5: score += 2
    elif adaptive_result.max_drawdown_pct < 10: score += 1
    if adaptive_result.sharpe_ratio > 1.5: score += 2
    elif adaptive_result.sharpe_ratio > 0.5: score += 1

    if score >= 8:
        verdict = f"{C_G}★★★ PRODUCTION READY{C_X}"
    elif score >= 5:
        verdict = f"{C_Y}★★☆ PAPER TRADE FIRST{C_X}"
    elif score >= 3:
        verdict = f"{C_Y}★☆☆ NEEDS MORE TUNING{C_X}"
    else:
        verdict = f"{C_R}☆☆☆ NOT READY{C_X}"

    print(f"  Rating: {verdict}  (score: {score}/10)")

    # Save
    output = {
        "test_date": str(date.today()),
        "adaptive": {
            "trades": adaptive_result.total_trades,
            "win_rate": adaptive_result.win_rate,
            "total_pnl": adaptive_result.total_pnl,
            "profit_factor": adaptive_result.profit_factor,
            "max_drawdown_pct": adaptive_result.max_drawdown_pct,
            "sharpe_ratio": adaptive_result.sharpe_ratio,
            "total_return_pct": round(total_return, 2),
            "annualized_return_pct": round(ann_return, 1),
            "monthly_pnl": adaptive_result.monthly_pnl,
        },
        "fixed_baseline": {
            "strategy": best_fixed_name,
            "total_pnl": best_fixed.total_pnl,
            "profit_factor": best_fixed.profit_factor,
            "win_rate": best_fixed.win_rate,
        },
        "improvement_dollars": round(improvement, 2),
        "regime_params": {
            "GREEN": {"strategy": "iron_condor", "delta": 0.12, "stop": 3.0,
                      "tp": 0.75, "width": 2.0, "entry": "30-60m"},
            "YELLOW": {"strategy": "call_credit", "delta": 0.18, "stop": 3.0,
                       "tp": 0.75, "width": 2.0, "entry": "30-60m"},
            "RED": {"strategy": "none", "action": "skip"},
        },
    }
    with open("adaptive_validation_results.json", "w") as f:
        json.dump(output, f, indent=2)
    print(f"\n  💾 Results saved to adaptive_validation_results.json")
    print(f"{C_B}{'═' * 78}{C_X}\n")


if __name__ == "__main__":
    main()

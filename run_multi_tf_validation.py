#!/usr/bin/env python3
"""
Multi-Timeframe Validation Test
===================================
Compares baseline (regime-adaptive only) vs multi-TF filtered backtester
on the same 180 days of IBKR 5-min SPY data.

This shows the REAL impact of D1/H1/M5/M1 signal gating:
  - How many entries get filtered out
  - Win rate improvement (or not)
  - P&L impact
  - Per-regime + per-trend breakdown

Usage:
    python run_multi_tf_validation.py
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
from trading_engine.data.multi_tf import MultiTFAnalyzer


# ─── Colors ──────────────────────────────────────────────────────

G = "\033[92m"
R = "\033[91m"
Y = "\033[93m"
B = "\033[1m"
C = "\033[96m"
X = "\033[0m"


# ─── Data loading ────────────────────────────────────────────────

def load_trading_days(ticker="SPY", interval="5m") -> List[TradingDay]:
    """Load cached IBKR intraday data."""
    cache_path = os.path.join("data", "intraday", f"{ticker}_ibkr_{interval}_180d.csv")
    if not os.path.exists(cache_path):
        print(f"  ❌ Missing {cache_path}")
        print(f"  Run the IBKR data fetcher first.")
        sys.exit(1)

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
        TradingDay(date=d, bars=bars, ticker=ticker)
        for d, bars in sorted(days_dict.items())
        if len(bars) >= 30
    ]


def classify_regime(vix: float) -> str:
    if vix < 18:
        return "GREEN"
    elif vix <= 25:
        return "YELLOW"
    return "RED"


def tag_with_regimes(trading_days: List[TradingDay]) -> Dict[str, str]:
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


# ─── Comparison ──────────────────────────────────────────────────

def run_comparison(ticker="SPY"):
    """Run baseline vs multi-TF and compare."""

    print(f"\n{C}{'═' * 78}{X}")
    print(f"  {B}MULTI-TIMEFRAME VALIDATION TEST{X}")
    print(f"  {ticker} | IBKR 5-min data | Baseline vs Multi-TF")
    print(f"{C}{'═' * 78}{X}\n")

    # Load data
    print(f"  📊 Loading {ticker} 5-min data...")
    trading_days = load_trading_days(ticker)
    print(f"     {len(trading_days)} trading days loaded")

    # Tag regimes
    print(f"  📈 Fetching VIX for regime classification...")
    regime_map = tag_with_regimes(trading_days)
    green = sum(1 for v in regime_map.values() if v == "GREEN")
    yellow = sum(1 for v in regime_map.values() if v == "YELLOW")
    red = sum(1 for v in regime_map.values() if v == "RED")
    print(f"     GREEN: {green}  YELLOW: {yellow}  RED: {red}")

    config = EngineConfig()
    bt = IntradayBacktester(config)

    # ── Run 1: Baseline (adaptive only, no multi-TF) ──
    print(f"\n  {B}▸ Run 1: Baseline (Adaptive Regime Only){X}")
    baseline = bt.run(
        trading_days,
        adaptive=True,
        regime_map=regime_map,
        multi_tf=False,
    )
    print(f"     Trades: {baseline.total_trades}  |  "
          f"WR: {baseline.win_rate:.1f}%  |  "
          f"P&L: {'$' if baseline.total_pnl >= 0 else '-$'}{abs(baseline.total_pnl):,.2f}  |  "
          f"PF: {baseline.profit_factor:.2f}  |  "
          f"Sharpe: {baseline.sharpe_ratio:.2f}")

    # ── Run 2: Multi-TF (adaptive + D1/H1/M5 gates + exits) ──
    print(f"\n  {B}▸ Run 2: Multi-TF (Adaptive + D1/H1/M5 Gates){X}")
    multi_tf = bt.run(
        trading_days,
        adaptive=True,
        regime_map=regime_map,
        multi_tf=True,
    )
    print(f"     Trades: {multi_tf.total_trades}  |  "
          f"WR: {multi_tf.win_rate:.1f}%  |  "
          f"P&L: {'$' if multi_tf.total_pnl >= 0 else '-$'}{abs(multi_tf.total_pnl):,.2f}  |  "
          f"PF: {multi_tf.profit_factor:.2f}  |  "
          f"Sharpe: {multi_tf.sharpe_ratio:.2f}")

    # ── Side-by-side comparison ──
    print(f"\n{C}{'─' * 78}{X}")
    print(f"  {B}COMPARISON: Baseline vs Multi-TF{X}")
    print(f"{C}{'─' * 78}{X}")

    def arrow(old, new, higher_is_better=True):
        diff = new - old
        if abs(diff) < 0.005:
            return "→"
        if (diff > 0 and higher_is_better) or (diff < 0 and not higher_is_better):
            return f"{G}↑{X}"
        return f"{R}↓{X}"

    metrics = [
        ("Total Trades", baseline.total_trades, multi_tf.total_trades, False),
        ("Win Rate %", baseline.win_rate, multi_tf.win_rate, True),
        ("Total P&L $", baseline.total_pnl, multi_tf.total_pnl, True),
        ("Profit Factor", baseline.profit_factor, multi_tf.profit_factor, True),
        ("Sharpe Ratio", baseline.sharpe_ratio, multi_tf.sharpe_ratio, True),
        ("Max Drawdown %", baseline.max_drawdown_pct, multi_tf.max_drawdown_pct, False),
        ("Avg Winner $", baseline.avg_winner, multi_tf.avg_winner, True),
        ("Avg Loser $", baseline.avg_loser, multi_tf.avg_loser, False),
        ("Gamma Blowups", baseline.gamma_blow_ups, multi_tf.gamma_blow_ups, False),
    ]

    print(f"\n  {'Metric':<20s} {'Baseline':>12s} {'Multi-TF':>12s}  {'Δ':>4s}")
    print(f"  {'─' * 55}")
    for name, bv, mv, hib in metrics:
        diff = mv - bv
        a = arrow(bv, mv, hib)
        if isinstance(bv, float):
            print(f"  {name:<20s} {bv:>12.2f} {mv:>12.2f}  {a} {diff:+.2f}")
        else:
            print(f"  {name:<20s} {bv:>12d} {mv:>12d}  {a} {diff:+d}")

    # ── Multi-TF filter stats ──
    if multi_tf.tf_entries_allowed > 0 or multi_tf.tf_entries_rejected > 0:
        total_eval = multi_tf.tf_entries_allowed + multi_tf.tf_entries_rejected
        filter_pct = multi_tf.tf_entries_rejected / total_eval * 100 if total_eval else 0

        print(f"\n{C}{'─' * 78}{X}")
        print(f"  {B}MULTI-TF FILTER ANALYSIS{X}")
        print(f"{C}{'─' * 78}{X}")
        print(f"  Days Evaluated:    {total_eval}")
        print(f"  Entries Allowed:   {G}{multi_tf.tf_entries_allowed}{X}")
        print(f"  Entries Rejected:  {R}{multi_tf.tf_entries_rejected}{X}")
        print(f"  Filter Rate:       {filter_pct:.1f}% of signals filtered out")
        print(f"  Avg Confidence:    {multi_tf.tf_avg_confidence:.0%}")

    # ── Per-exit-reason comparison ──
    print(f"\n{C}{'─' * 78}{X}")
    print(f"  {B}EXIT REASONS COMPARISON{X}")
    print(f"{C}{'─' * 78}{X}")

    all_reasons = set()
    base_reasons = {}
    mtf_reasons = {}
    for t in baseline.trades:
        base_reasons[t.exit_reason] = base_reasons.get(t.exit_reason, 0) + 1
        all_reasons.add(t.exit_reason)
    for t in multi_tf.trades:
        mtf_reasons[t.exit_reason] = mtf_reasons.get(t.exit_reason, 0) + 1
        all_reasons.add(t.exit_reason)

    print(f"\n  {'Reason':<25s} {'Baseline':>8s} {'Multi-TF':>8s}")
    print(f"  {'─' * 45}")
    for reason in sorted(all_reasons):
        bc = base_reasons.get(reason, 0)
        mc = mtf_reasons.get(reason, 0)
        print(f"  {reason:<25s} {bc:>8d} {mc:>8d}")

    # ── D1 Trend breakdown (multi-TF only) ──
    trend_trades = defaultdict(list)
    for t in multi_tf.trades:
        if t.tf_d1_trend:
            trend_trades[t.tf_d1_trend].append(t)

    if trend_trades:
        print(f"\n{C}{'─' * 78}{X}")
        print(f"  {B}D1 TREND → TRADE OUTCOMES (Multi-TF){X}")
        print(f"{C}{'─' * 78}{X}")
        for trend in sorted(trend_trades):
            trades = trend_trades[trend]
            n = len(trades)
            wr = sum(1 for t in trades if t.total_pnl > 0) / n * 100 if n else 0
            tp = sum(t.total_pnl for t in trades)
            avg_conf = np.mean([t.tf_entry_confidence for t in trades]) if trades else 0
            color = G if tp > 0 else R
            print(f"  {trend:10s}  {n:3d} trades  WR: {wr:.0f}%  "
                  f"{color}P&L: ${tp:+,.2f}{X}  AvgConf: {avg_conf:.0%}")

    # ── Position size distribution ──
    size_trades = defaultdict(list)
    for t in multi_tf.trades:
        if t.tf_size_mult == 1.0:
            size_trades["1.00× (full)"].append(t)
        elif t.tf_size_mult >= 0.75:
            size_trades["0.75× (reduced)"].append(t)
        elif t.tf_size_mult >= 0.5:
            size_trades["0.50× (minimum)"].append(t)
        else:
            size_trades["0.00× (rejected)"].append(t)

    if any(k != "1.00× (full)" for k in size_trades):
        print(f"\n  {B}Position Size Distribution:{X}")
        for sz in sorted(size_trades):
            trades = size_trades[sz]
            n = len(trades)
            wr = sum(1 for t in trades if t.total_pnl > 0) / n * 100 if n else 0
            tp = sum(t.total_pnl for t in trades)
            color = G if tp > 0 else R
            print(f"    {sz:20s}  {n:3d} trades  WR: {wr:.0f}%  "
                  f"{color}P&L: ${tp:+,.2f}{X}")

    # ── Direction bias breakdown ──
    dir_trades = defaultdict(list)
    for t in multi_tf.trades:
        if t.tf_direction_bias:
            dir_trades[t.tf_direction_bias].append(t)

    if dir_trades:
        print(f"\n  {B}Direction Bias → Outcomes:{X}")
        for d in sorted(dir_trades):
            trades = dir_trades[d]
            n = len(trades)
            wr = sum(1 for t in trades if t.total_pnl > 0) / n * 100 if n else 0
            tp = sum(t.total_pnl for t in trades)
            color = G if tp > 0 else R
            print(f"    {d:15s}  {n:3d} trades  WR: {wr:.0f}%  "
                  f"{color}P&L: ${tp:+,.2f}{X}")

    # ── Full reports ──
    print(f"\n\n{'='*78}")
    print(f"  {B}FULL BASELINE REPORT{X}")
    print(f"{'='*78}")
    bt.print_report(baseline)

    print(f"\n{'='*78}")
    print(f"  {B}FULL MULTI-TF REPORT{X}")
    print(f"{'='*78}")
    bt.print_report(multi_tf)

    # ── Verdict ──
    print(f"\n{C}{'═' * 78}{X}")
    print(f"  {B}VERDICT{X}")
    print(f"{C}{'═' * 78}{X}")

    improvements = 0
    if multi_tf.win_rate > baseline.win_rate:
        improvements += 1
        print(f"  {G}✓{X} Win rate improved: {baseline.win_rate:.1f}% → {multi_tf.win_rate:.1f}%")
    else:
        print(f"  {R}✗{X} Win rate decreased: {baseline.win_rate:.1f}% → {multi_tf.win_rate:.1f}%")

    if multi_tf.profit_factor > baseline.profit_factor:
        improvements += 1
        print(f"  {G}✓{X} Profit factor improved: {baseline.profit_factor:.2f} → {multi_tf.profit_factor:.2f}")
    else:
        print(f"  {R}✗{X} Profit factor decreased: {baseline.profit_factor:.2f} → {multi_tf.profit_factor:.2f}")

    if multi_tf.sharpe_ratio > baseline.sharpe_ratio:
        improvements += 1
        print(f"  {G}✓{X} Sharpe improved: {baseline.sharpe_ratio:.2f} → {multi_tf.sharpe_ratio:.2f}")
    else:
        print(f"  {R}✗{X} Sharpe decreased: {baseline.sharpe_ratio:.2f} → {multi_tf.sharpe_ratio:.2f}")

    if multi_tf.max_drawdown_pct < baseline.max_drawdown_pct:
        improvements += 1
        print(f"  {G}✓{X} Drawdown reduced: {baseline.max_drawdown_pct:.2f}% → {multi_tf.max_drawdown_pct:.2f}%")
    else:
        print(f"  {R}✗{X} Drawdown increased: {baseline.max_drawdown_pct:.2f}% → {multi_tf.max_drawdown_pct:.2f}%")

    # Per-trade profitability
    base_per_trade = baseline.total_pnl / baseline.total_trades if baseline.total_trades else 0
    mtf_per_trade = multi_tf.total_pnl / multi_tf.total_trades if multi_tf.total_trades else 0
    if mtf_per_trade > base_per_trade:
        improvements += 1
        print(f"  {G}✓{X} Per-trade P&L improved: ${base_per_trade:.2f} → ${mtf_per_trade:.2f}")
    else:
        print(f"  {R}✗{X} Per-trade P&L decreased: ${base_per_trade:.2f} → ${mtf_per_trade:.2f}")

    score = improvements
    if score >= 4:
        print(f"\n  {G}★★★ MULTI-TF ADDS CLEAR VALUE — {score}/5 metrics improved ★★★{X}")
    elif score >= 3:
        print(f"\n  {Y}★★ MULTI-TF SHOWS PROMISE — {score}/5 metrics improved ★★{X}")
    elif score >= 2:
        print(f"\n  {Y}★ MIXED RESULTS — {score}/5 metrics improved, needs tuning ★{X}")
    else:
        print(f"\n  {R}✗ MULTI-TF NOT ADDING VALUE — only {score}/5 improved{X}")
        print(f"    Consider loosening entry thresholds or adjusting gate weights")

    print(f"\n{C}{'═' * 78}{X}\n")


if __name__ == "__main__":
    run_comparison("SPY")

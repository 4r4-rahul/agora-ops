#!/usr/bin/env python3
"""
Multi-Timeframe Long Options Backtest
========================================
Tests the buy calls/puts strategy at multiple bar resolutions
using the SAME underlying 1-minute data (resampled).

This answers: "Which bar resolution gives the best edge for momentum detection?"

Approach:
  1. Load 1-minute bars (master data, highest fidelity)
  2. Resample to 2m, 3m, 5m using proper OHLCV aggregation
  3. Run the backtester at each timeframe
  4. Compare results side-by-side

The MomentumDetector auto-scales all lookback windows:
  - ORB window always covers 15 minutes (15 bars @ 1m, 5 bars @ 3m, 3 bars @ 5m)
  - EMA spans cover same time periods (9/21/50 min @ 1m, 9/21/50 min @ 5m via scaling)
  - VWAP lookback, volume surge, etc. all time-adjusted

Usage:
    python run_multi_tf_backtest.py                    # Default: SPY, all TFs
    python run_multi_tf_backtest.py --timeframes 1m 3m # Just 1m and 3m
    python run_multi_tf_backtest.py --days 60          # More history
    python run_multi_tf_backtest.py --verbose          # Per-trade detail
    python run_multi_tf_backtest.py --save             # Save all results
"""

import argparse
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
import numpy as np

from trading_engine.config import EngineConfig
from trading_engine.data.lotto_backtester import LottoBacktester


# ─────────────────────────────────────────────────────────────────
# Bar Resampling
# ─────────────────────────────────────────────────────────────────

def resample_bars(df: pd.DataFrame, target_minutes: int) -> pd.DataFrame:
    """
    Resample 1-minute OHLCV bars to a coarser timeframe.

    Uses proper OHLCV aggregation:
      - Open:   first open in window
      - High:   max high in window
      - Low:    min low in window
      - Close:  last close in window
      - Volume: sum of all volume in window

    Args:
        df: DataFrame with DatetimeIndex and columns [open, high, low, close, volume]
        target_minutes: Target bar size in minutes (2, 3, 5, 15, etc.)

    Returns:
        Resampled DataFrame with same columns.
    """
    if target_minutes <= 1:
        return df.copy()

    rule = f"{target_minutes}min"

    agg_dict = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }

    # Include vwap if present
    if "vwap" in df.columns:
        # Volume-weighted average of VWAP
        agg_dict["vwap"] = "mean"

    resampled = df.resample(rule, closed="left", label="left").agg(agg_dict)

    # Drop rows where open is NaN (gaps between days)
    resampled = resampled.dropna(subset=["open"])

    return resampled


def load_1m_data(data_dir: str, ticker: str) -> pd.DataFrame:
    """
    Load 1-minute bar data from cache.
    Tries IBKR data first (longest history), then Yahoo.
    """
    candidates = [
        os.path.join(data_dir, f"{ticker}_ibkr_1m_180d.csv"),
        os.path.join(data_dir, f"{ticker}_1m_7d.csv"),
        os.path.join(data_dir, f"{ticker}_1m_30d.csv"),
    ]

    for path in candidates:
        if os.path.exists(path):
            print(f"  📂 Loading 1m data from {os.path.basename(path)} ...")
            df = pd.read_csv(path, index_col="timestamp", parse_dates=True)
            if not isinstance(df.index, pd.DatetimeIndex):
                df.index = pd.to_datetime(df.index, utc=True)
            print(f"     {len(df)} bars, {df.index[0].date()} → {df.index[-1].date()}")
            return df

    return None


# ─────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Multi-timeframe backtest comparison for long options strategy"
    )
    parser.add_argument("--ticker", default="SPY",
                        help="Ticker to backtest (default: SPY)")
    parser.add_argument("--timeframes", nargs="+", default=["1m", "2m", "3m", "5m"],
                        help="Timeframes to test (default: 1m 2m 3m 5m)")
    parser.add_argument("--days", type=int, default=0,
                        help="Limit to last N calendar days (0 = all available)")
    parser.add_argument("--account", type=float, default=10000,
                        help="Account size (default: $10,000)")
    parser.add_argument("--max-trades", type=int, default=5,
                        help="Max trades per day (default: 5)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Print per-trade detail for each TF")
    parser.add_argument("--save", action="store_true",
                        help="Save results for each TF to JSON")
    args = parser.parse_args()

    print("\n" + "=" * 70)
    print("  MULTI-TIMEFRAME LONG OPTIONS BACKTEST")
    print("=" * 70)
    print(f"  Ticker:      {args.ticker}")
    print(f"  Timeframes:  {', '.join(args.timeframes)}")
    print(f"  Account:     ${args.account:,.0f}")
    print(f"  Max trades:  {args.max_trades}/day")
    if args.days:
        print(f"  Days limit:  Last {args.days} calendar days")
    print("=" * 70)

    # 1. Load 1-minute master data
    data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "intraday")
    master_df = load_1m_data(data_dir, args.ticker)

    if master_df is None or master_df.empty:
        print(f"\n  ❌ No 1-minute data found for {args.ticker}.")
        print(f"     First fetch it:  python run_lotto_backtest.py --ticker {args.ticker} --days 7 --interval 1m")
        print(f"     Or with IBKR:    python run_lotto_backtest.py --ticker {args.ticker} --days 180 --interval 1m --provider ibkr")
        sys.exit(1)

    # Optionally limit to last N days
    if args.days > 0:
        cutoff = master_df.index[-1] - pd.Timedelta(days=args.days)
        master_df = master_df[master_df.index >= cutoff]
        print(f"\n  📅 Filtered to last {args.days} days: {len(master_df)} bars")

    total_trading_days = master_df.index.normalize().nunique()
    print(f"\n  Total 1m bars: {len(master_df):,}")
    print(f"  Trading days:  {total_trading_days}")
    print(f"  Date range:    {master_df.index[0].date()} → {master_df.index[-1].date()}")

    # 2. Run backtest at each timeframe
    config = EngineConfig()
    config.account.account_size = args.account

    all_results = {}

    for tf in args.timeframes:
        tf_minutes = int(tf.replace("m", ""))

        print(f"\n{'─' * 70}")
        print(f"  ⏱️  TIMEFRAME: {tf} ({tf_minutes}-minute bars)")
        print(f"{'─' * 70}")

        # Resample
        if tf_minutes == 1:
            bars_df = master_df.copy()
        else:
            bars_df = resample_bars(master_df, tf_minutes)

        bars_per_day = 390 // tf_minutes
        print(f"  Bars: {len(bars_df):,} ({bars_per_day}/day × {total_trading_days} days)")

        # Run backtest
        t0 = time.time()
        bt = LottoBacktester(config=config, account_size=args.account)
        results = bt.run(
            bars_df,
            ticker=args.ticker,
            interval=tf,
            max_trades_per_day=args.max_trades,
            verbose=args.verbose,
        )
        elapsed = time.time() - t0
        print(f"  ⏱  Completed in {elapsed:.1f}s")

        all_results[tf] = results

        # Print individual report if verbose
        if args.verbose:
            bt.print_report(results)

        # Save if requested
        if args.save:
            path = f"lotto_backtest_{args.ticker}_{tf}.json"
            bt.save_results(results, path=path)

    # 3. Side-by-side comparison
    print_comparison(all_results, args.account)

    # 4. Per-trigger comparison across TFs
    print_trigger_comparison(all_results)

    print("\n  ✅ Multi-TF analysis complete.\n")


def print_comparison(all_results: dict, account_size: float):
    """Print side-by-side comparison table."""
    print("\n" + "=" * 90)
    print("  MULTI-TIMEFRAME COMPARISON")
    print("=" * 90)

    # Header
    tfs = list(all_results.keys())
    header_row = f"  {'Metric':<22}"
    for tf in tfs:
        header_row += f" {tf:>12}"
    print(header_row)
    print(f"  {'─' * (22 + 13 * len(tfs))}")

    # Rows
    metrics = [
        ("Total Trades", lambda r: f"{r.total_trades}"),
        ("Win Rate", lambda r: f"{r.win_rate:.1f}%"),
        ("Total P&L", lambda r: f"${r.total_pnl:+,.0f}"),
        ("Return %", lambda r: f"{r.total_pnl / account_size * 100:+.1f}%"),
        ("Profit Factor", lambda r: f"{r.profit_factor:.2f}"),
        ("Avg Winner", lambda r: f"${r.avg_win:+,.0f}"),
        ("Avg Loser", lambda r: f"${r.avg_loss:+,.0f}"),
        ("Avg Win Mult", lambda r: f"{r.avg_winner_mult:.2f}x"),
        ("Avg Lose Mult", lambda r: f"{r.avg_loser_mult:.2f}x"),
        ("Biggest Win", lambda r: f"${r.biggest_win:+,.0f}"),
        ("Biggest Loss", lambda r: f"${r.biggest_loss:+,.0f}"),
        ("Max Drawdown", lambda r: f"{r.max_drawdown_pct:.1%}"),
        ("Days Traded", lambda r: f"{r.days_traded}"),
        ("Total Spent", lambda r: f"${r.total_spent:,.0f}"),
    ]

    for label, fmt in metrics:
        row = f"  {label:<22}"
        values = []
        for tf in tfs:
            val = fmt(all_results[tf])
            row += f" {val:>12}"
            values.append(val)
        print(row)

    # Find best TF
    best_pf = max(tfs, key=lambda t: all_results[t].profit_factor)
    best_wr = max(tfs, key=lambda t: all_results[t].win_rate)
    best_pnl = max(tfs, key=lambda t: all_results[t].total_pnl)

    print(f"\n  {'─' * (22 + 13 * len(tfs))}")
    print(f"  🏆 Best Profit Factor:  {best_pf} ({all_results[best_pf].profit_factor:.2f})")
    print(f"  🏆 Best Win Rate:       {best_wr} ({all_results[best_wr].win_rate:.1f}%)")
    print(f"  🏆 Best P&L:            {best_pnl} (${all_results[best_pnl].total_pnl:+,.0f})")


def print_trigger_comparison(all_results: dict):
    """Print per-trigger breakdown across timeframes."""
    print("\n" + "=" * 90)
    print("  PER-TRIGGER BREAKDOWN ACROSS TIMEFRAMES")
    print("=" * 90)

    tfs = list(all_results.keys())

    # Collect all trigger types across all TFs
    all_triggers = set()
    for r in all_results.values():
        all_triggers.update(r.trigger_stats.keys())

    for trigger in sorted(all_triggers):
        print(f"\n  📊 {trigger}")
        header_row = f"    {'Metric':<16}"
        for tf in tfs:
            header_row += f" {tf:>10}"
        print(header_row)
        print(f"    {'─' * (16 + 11 * len(tfs))}")

        # Trades
        row = f"    {'Trades':<16}"
        for tf in tfs:
            s = all_results[tf].trigger_stats.get(trigger, {})
            row += f" {s.get('trades', 0):>10}"
        print(row)

        # Win Rate
        row = f"    {'Win Rate':<16}"
        for tf in tfs:
            s = all_results[tf].trigger_stats.get(trigger, {})
            wr = s.get('win_rate', 0)
            row += f" {wr:>9.1f}%"
        print(row)

        # Avg Multiplier
        row = f"    {'Avg Mult':<16}"
        for tf in tfs:
            s = all_results[tf].trigger_stats.get(trigger, {})
            m = s.get('avg_mult', 0)
            row += f" {m:>9.2f}x"
        print(row)

        # P&L
        row = f"    {'P&L':<16}"
        for tf in tfs:
            s = all_results[tf].trigger_stats.get(trigger, {})
            pnl = s.get('pnl', 0)
            row += f" ${pnl:>+9,.0f}"
        print(row)

    # Summary: which TF has any trigger with positive avg_mult?
    print(f"\n  {'─' * 60}")
    print(f"  POSITIVE EDGE HUNT (avg_mult > 1.0 = profitable):")
    print(f"  {'─' * 60}")
    found_positive = False
    for tf in tfs:
        for trigger, stats in all_results[tf].trigger_stats.items():
            if stats.get("avg_mult", 0) > 1.0:
                found_positive = True
                print(f"  ✅ {tf} × {trigger}: {stats['avg_mult']:.2f}x avg "
                      f"({stats['trades']} trades, {stats['win_rate']:.0f}% WR, "
                      f"${stats['pnl']:+,.0f})")

    if not found_positive:
        print(f"  ❌ No trigger at any timeframe has positive edge yet.")
        print(f"     Closest to breakeven:")
        best_combos = []
        for tf in tfs:
            for trigger, stats in all_results[tf].trigger_stats.items():
                if stats.get("trades", 0) >= 3:  # Need minimum sample
                    best_combos.append((tf, trigger, stats))
        best_combos.sort(key=lambda x: x[2].get("avg_mult", 0), reverse=True)
        for tf, trigger, stats in best_combos[:5]:
            print(f"     {tf} × {trigger}: {stats['avg_mult']:.2f}x "
                  f"({stats['trades']} trades, {stats['win_rate']:.0f}% WR)")


if __name__ == "__main__":
    main()

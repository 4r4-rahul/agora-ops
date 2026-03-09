#!/usr/bin/env python3
"""
Backtest Runner
================
One command to download free data, build snapshots, and run the backtest.

Usage:
    python run_backtest.py                          # Default: 2024-present, put credit spreads
    python run_backtest.py --start 2023-01-01       # Custom start date
    python run_backtest.py --strategy iron_condor    # Different strategy
    python run_backtest.py --strategy put_credit iron_condor   # Multiple strategies
    python run_backtest.py --account 100000          # Larger account
    python run_backtest.py --no-yellow               # Only trade GREEN days
    python run_backtest.py --force-download           # Re-download data

Strategies:
    put_credit    — 0DTE SPX put credit spreads (core strategy)
    call_credit   — 0DTE SPX call credit spreads
    iron_condor   — 0DTE SPX iron condors
    eod_scalp     — End-of-day theta scalps (last 90 min)
"""

import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from trading_engine.config import EngineConfig
from trading_engine.data import HistoricalDataFetcher
from trading_engine.data.backtester import Backtester


def main():
    parser = argparse.ArgumentParser(description="Run historical backtest with free data")
    parser.add_argument("--start", default="2024-01-01", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", default=None, help="End date (default: today)")
    parser.add_argument("--strategy", nargs="+", default=["put_credit"],
                        help="Strategies to test: put_credit, call_credit, iron_condor, eod_scalp")
    parser.add_argument("--account", type=float, default=50000, help="Account size")
    parser.add_argument("--no-yellow", action="store_true", help="Skip YELLOW regime days")
    parser.add_argument("--force-download", action="store_true", help="Force re-download data")
    parser.add_argument("--save", action="store_true", help="Save results to JSON")
    args = parser.parse_args()

    print("\n" + "=" * 60)
    print("  OPTIONS TRADING ENGINE — HISTORICAL BACKTEST")
    print("=" * 60)
    print(f"  Period:      {args.start} → {args.end or 'today'}")
    print(f"  Strategies:  {', '.join(args.strategy)}")
    print(f"  Account:     ${args.account:,.0f}")
    print(f"  Trade YELLOW: {'No' if args.no_yellow else 'Yes'}")
    print("=" * 60 + "\n")

    # 1. Download data
    print("STEP 1: Fetching free historical data (Yahoo Finance) ...\n")
    fetcher = HistoricalDataFetcher()
    fetcher.download(start=args.start, end=args.end, force=args.force_download)

    # 2. Build snapshots
    print("\nSTEP 2: Building MarketSnapshots ...\n")
    snapshots = fetcher.build_snapshots()
    fetcher.save_snapshots(snapshots)

    # 3. Run backtest
    print(f"\nSTEP 3: Running backtest ({len(snapshots)} days) ...\n")
    config = EngineConfig()
    config.account.account_size = args.account

    bt = Backtester(config)
    results = bt.run(
        snapshots,
        strategies=args.strategy,
        trade_yellow=not args.no_yellow,
    )

    # 4. Print results
    bt.print_report(results)

    # 5. Save if requested
    if args.save:
        bt.save_results(results)

    print("  Done. Run with --save to export detailed results to JSON.\n")


if __name__ == "__main__":
    main()

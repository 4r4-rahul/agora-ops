#!/usr/bin/env python3
"""
Long Options Backtest Runner
==============================
One command to download intraday data and backtest the buy-first strategy.

This backtests your PRIMARY strategy: buying calls/puts on momentum triggers.

Usage:
    python run_lotto_backtest.py                              # Default: SPY 5m, 30 days
    python run_lotto_backtest.py --ticker SPY --days 60       # SPY 60 days of 5m
    python run_lotto_backtest.py --days 7 --interval 1m       # 7 days of 1-min detail
    python run_lotto_backtest.py --verbose                    # Print every trade
    python run_lotto_backtest.py --save                       # Save results to JSON
    python run_lotto_backtest.py --account 10000              # Custom account size

Data Limits (Yahoo Finance free tier):
    1m bars:  max 7 days
    5m bars:  max 60 days
    15m bars: max 60 days

Strategy Being Tested:
    5 Momentum Triggers → Buy calls/puts → 3x target / 50% stop / trailing exit
    Triggers: ORB Breakout, Mean-Reversion, VWAP Reclaim, Volume Surge, Trend Continuation
"""

import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from trading_engine.config import EngineConfig
from trading_engine.data.intraday import IntradayFetcher
from trading_engine.data.lotto_backtester import LottoBacktester


def main():
    parser = argparse.ArgumentParser(
        description="Backtest the long options (buy calls/puts) strategy"
    )
    parser.add_argument("--ticker", default="SPY",
                        help="Ticker to backtest (default: SPY)")
    parser.add_argument("--days", type=int, default=30,
                        help="Days of history to test (default: 30)")
    parser.add_argument("--interval", default="5m",
                        help="Bar interval: 1m, 5m, 15m (default: 5m)")
    parser.add_argument("--account", type=float, default=10000,
                        help="Account size (default: $10,000)")
    parser.add_argument("--max-trades", type=int, default=5,
                        help="Max trades per day (default: 5)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Print per-trade detail")
    parser.add_argument("--save", action="store_true",
                        help="Save results to JSON")
    parser.add_argument("--force-download", action="store_true",
                        help="Force re-download data")
    parser.add_argument("--provider", default="auto",
                        choices=["auto", "yahoo", "ibkr", "polygon", "alpaca"],
                        help="Data provider (default: auto)")
    args = parser.parse_args()

    print("\n" + "=" * 60)
    print("  LONG OPTIONS BACKTEST — Buy Calls & Puts")
    print("=" * 60)
    print(f"  Ticker:      {args.ticker}")
    print(f"  Period:      {args.days} days of {args.interval} bars")
    print(f"  Account:     ${args.account:,.0f}")
    print(f"  Max trades:  {args.max_trades}/day")
    print(f"  Provider:    {args.provider}")
    print("=" * 60 + "\n")

    # 1. Fetch intraday data
    print("STEP 1: Fetching intraday bar data ...\n")
    fetcher = IntradayFetcher()

    try:
        trading_days = fetcher.fetch(
            ticker=args.ticker,
            days=args.days,
            interval=args.interval,
            provider=args.provider,
            force=args.force_download,
        )
    except Exception as e:
        print(f"  ❌ Failed to fetch data: {e}")
        print(f"\n  Suggestions:")
        print(f"    • For SPY 5m (free): python run_lotto_backtest.py --ticker SPY --days 60")
        print(f"    • For 1m detail:     python run_lotto_backtest.py --ticker SPY --days 7 --interval 1m")
        print(f"    • With IBKR:         python run_lotto_backtest.py --ticker SPX --provider ibkr")
        sys.exit(1)

    print(f"\n  ✅ Got {len(trading_days)} trading days")

    # 2. Convert TradingDay objects to a single DataFrame
    print("\nSTEP 2: Preparing bars for backtester ...\n")
    import pandas as pd

    all_rows = []
    for td in trading_days:
        for bar in td.bars:
            all_rows.append({
                "timestamp": bar.timestamp,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
            })

    if not all_rows:
        print("  ❌ No bars to backtest. Check data.")
        sys.exit(1)

    bars_df = pd.DataFrame(all_rows).set_index("timestamp")
    bars_df.sort_index(inplace=True)

    print(f"  Total bars:  {len(bars_df)}")
    print(f"  Date range:  {bars_df.index[0]} → {bars_df.index[-1]}")

    # 3. Run backtest
    print(f"\nSTEP 3: Running long options backtest ...\n")
    config = EngineConfig()
    config.account.account_size = args.account

    bt = LottoBacktester(config=config, account_size=args.account)
    results = bt.run(
        bars_df,
        ticker=args.ticker,
        interval=args.interval,
        max_trades_per_day=args.max_trades,
        verbose=args.verbose,
    )

    # 4. Print report
    bt.print_report(results)

    # 5. Save if requested
    if args.save:
        bt.save_results(results)

    print("  Done. Run with --verbose to see every trade, --save to export JSON.\n")


if __name__ == "__main__":
    main()

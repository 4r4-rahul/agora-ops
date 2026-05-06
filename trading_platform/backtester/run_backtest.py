"""
CLI entry point for the walk-forward backtester.

Usage:
    python -m trading_platform.backtester.run_backtest SPY --start 2024-01-01 --end 2024-12-31
    python -m trading_platform.backtester.run_backtest QQQ --start 2023-01-01 --end 2024-12-31 --balance 25000
    python -m trading_platform.backtester.run_backtest SPY QQQ --start 2024-01-01 --end 2024-06-30 --csv out/
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Load .env before importing settings
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


async def run_one(ticker: str, args: argparse.Namespace) -> int:
    from trading_platform.backtester.engine import BacktestEngine
    from trading_platform.backtester.report import export_csv, print_report

    engine = BacktestEngine(
        ticker=ticker,
        start=args.start,
        end=args.end,
        starting_balance=args.balance,
        trade_every_n_days=args.frequency,
        max_open_positions=args.max_positions,
    )

    print(f"\n  Running walk-forward backtest: {ticker}  {args.start} → {args.end}")
    result = await engine.run()
    print_report(result)

    if args.csv:
        csv_path = Path(args.csv) / f"backtest_{ticker}_{args.start}_{args.end}.csv"
        export_csv(result, csv_path)

    return 0 if result.total_pnl > 0 else 1


async def main_async(args: argparse.Namespace) -> None:
    # Run tickers sequentially — yfinance concurrent downloads collide
    for ticker in args.tickers:
        try:
            await run_one(ticker.upper(), args)
        except Exception as exc:
            print(f"  ERROR [{ticker}]: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Walk-forward backtester for the trading_platform agent pipeline"
    )
    parser.add_argument("tickers", nargs="+", help="Ticker symbols (e.g. SPY QQQ)")
    parser.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="End date YYYY-MM-DD")
    parser.add_argument("--balance", type=float, default=10_000.0, help="Starting balance (default: 10000)")
    parser.add_argument("--frequency", type=int, default=5, help="Trade every N days (default: 5)")
    parser.add_argument("--max-positions", type=int, default=3, help="Max concurrent positions (default: 3)")
    parser.add_argument("--csv", type=str, default="", help="Directory to write trade log CSV")
    parser.add_argument("--verbose", action="store_true", help="Show DEBUG logs")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()

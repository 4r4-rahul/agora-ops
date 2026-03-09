#!/usr/bin/env python3
"""
Intraday Greeks-Aware Backtest Runner
=======================================
Uses 5-minute bars to simulate 0DTE spreads with bar-by-bar
Black-Scholes re-pricing — captures gamma, theta, and vega effects.

Usage:
    python run_intraday_backtest.py                              # Yahoo, last 60 days, put credit
    python run_intraday_backtest.py --days 30                    # Last 30 days
    python run_intraday_backtest.py --strategy put_credit call_credit  # Both sides
    python run_intraday_backtest.py --provider ibkr              # Use IBKR TWS/Gateway (best quality)
    python run_intraday_backtest.py --provider polygon           # Use Polygon.io (needs API key)
    python run_intraday_backtest.py --ticker SPY                 # Default ticker
    python run_intraday_backtest.py --force                      # Re-download data

Data sources:
    auto     — Auto-select (IBKR if available, else Yahoo)
    ibkr     — IBKR TWS/Gateway (best quality, full history, real options data)
    yahoo    — FREE, last 60 days of 5-min data (default fallback)
    polygon  — FREE (delayed) or $29/mo (real-time), 2+ years
    alpaca   — FREE with account, 5+ years
    csv      — Load existing TradingView CSVs from workspace
"""

import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from trading_engine.config import EngineConfig
from trading_engine.data.intraday import IntradayFetcher
from trading_engine.data.intraday_backtester import IntradayBacktester


def main():
    parser = argparse.ArgumentParser(
        description="Run Greeks-aware intraday backtest with 5-min bars"
    )
    parser.add_argument("--ticker", default="SPY",
                        help="Ticker to backtest (default: SPY)")
    parser.add_argument("--days", type=int, default=60,
                        help="Days of history to fetch (default: 60)")
    parser.add_argument("--interval", default="5m",
                        help="Bar interval: 1m, 5m, 15m (default: 5m)")
    parser.add_argument("--strategy", nargs="+", default=["put_credit"],
                        help="Strategies: put_credit, call_credit, iron_condor")
    parser.add_argument("--provider", default="auto",
                        help="Data provider: yahoo, polygon, alpaca, csv, auto")
    parser.add_argument("--account", type=float, default=50000,
                        help="Account size (default: $50,000)")
    parser.add_argument("--iv", type=float, default=None,
                        help="Override IV (e.g., 0.20 for 20%%)")
    parser.add_argument("--force", action="store_true",
                        help="Force re-download data")
    parser.add_argument("--save", action="store_true",
                        help="Save results to JSON")
    args = parser.parse_args()

    print("\n" + "=" * 60)
    print("  INTRADAY GREEKS-AWARE BACKTEST")
    print("=" * 60)
    print(f"  Ticker:      {args.ticker}")
    print(f"  Days:        {args.days}")
    print(f"  Interval:    {args.interval}")
    print(f"  Strategies:  {', '.join(args.strategy)}")
    print(f"  Provider:    {args.provider}")
    print(f"  Account:     ${args.account:,.0f}")
    if args.iv:
        print(f"  IV Override: {args.iv:.1%}")
    print("=" * 60 + "\n")

    # 1. Fetch intraday data
    print("STEP 1: Fetching intraday 5-min bars ...\n")
    fetcher = IntradayFetcher()
    trading_days = fetcher.fetch(
        ticker=args.ticker,
        days=args.days,
        interval=args.interval,
        provider=args.provider,
        force=args.force,
    )

    if not trading_days:
        print("  ❌ No trading days found. Check your data source.")
        sys.exit(1)

    total_bars = sum(td.bar_count for td in trading_days)
    print(f"\n  ✅ {len(trading_days)} trading days, {total_bars} total bars")
    print(f"     {trading_days[0].date} → {trading_days[-1].date}\n")

    # 2. Run backtest
    print(f"STEP 2: Running Greeks-aware backtest ({len(trading_days)} days) ...\n")
    config = EngineConfig()
    config.account.account_size = args.account

    bt = IntradayBacktester(config)
    results = bt.run(
        trading_days,
        strategies=args.strategy,
        iv_override=args.iv,
    )

    # 3. Print report
    bt.print_report(results)

    # 4. Compare with daily backtest
    daily_results_path = os.path.join("data", "backtest_results.json")
    if os.path.exists(daily_results_path):
        import json
        with open(daily_results_path) as f:
            daily = json.load(f)

        print(f"\n  {'─' * 60}")
        print(f"  DAILY vs INTRADAY COMPARISON")
        print(f"  {'─' * 60}")
        print(f"  {'Metric':<25} {'Daily OHLC':>15} {'Intraday 5m':>15}")
        print(f"  {'─' * 60}")
        print(f"  {'Win Rate':<25} {daily['win_rate']:>14.1f}% {results.win_rate:>14.1f}%")
        print(f"  {'Profit Factor':<25} {daily['profit_factor']:>15.2f} {results.profit_factor:>15.2f}")
        print(f"  {'Max Drawdown':<25} {daily['max_drawdown_pct']:>14.2f}% {results.max_drawdown_pct:>14.2f}%")
        print(f"  {'Sharpe Ratio':<25} {daily['sharpe_ratio']:>15.2f} {results.sharpe_ratio:>15.2f}")
        print(f"  {'Gamma Exits':<25} {'N/A':>15} {results.gamma_blow_ups:>15}")
        print(f"  {'─' * 60}\n")

    # 5. Save
    if args.save:
        bt.save_results(results)

    print("  Done.\n")


if __name__ == "__main__":
    main()

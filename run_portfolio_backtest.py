#!/usr/bin/env python3
"""
Multi-Ticker Portfolio Backtester
==================================
Runs SPY/SPX + QQQ/SPX independently, then combines equity curves
and computes portfolio-level metrics (MaxDD, Sharpe, correlation).

Usage:
    python run_portfolio_backtest.py
    python run_portfolio_backtest.py --verbose
    python run_portfolio_backtest.py --tickers SPY
    python run_portfolio_backtest.py --tickers QQQ
"""

import sys
import os
import argparse
from dataclasses import dataclass, field
from typing import List, Dict, Optional
from datetime import date

sys.path.insert(0, os.path.dirname(__file__))

import numpy as np
import pandas as pd

from trading_engine.config import EngineConfig
from trading_engine.data.scalp_backtester import ScalpBacktester, ScalpBacktestResults


@dataclass
class TickerSpec:
    """Specification for a single ticker in the portfolio."""
    ticker: str                    # "SPY" or "QQQ"
    data_file: str                 # Path to 1m CSV
    config: EngineConfig           # Ticker-specific config
    account_size: float            # Per-ticker capital allocation
    spx_mode: bool = False         # Scale to SPX (SPY×10)
    ndx_mode: bool = False         # Scale to NDX (QQQ×40)


@dataclass
class PortfolioResults:
    """Combined portfolio backtest results."""
    ticker_results: Dict[str, ScalpBacktestResults] = field(default_factory=dict)

    # Portfolio-level metrics
    total_pnl: float = 0.0
    total_trades: int = 0
    total_capital: float = 0.0
    portfolio_pf: float = 0.0
    portfolio_wr: float = 0.0
    portfolio_max_dd_pct: float = 0.0
    portfolio_max_dd_dollars: float = 0.0
    portfolio_sharpe: float = 0.0
    correlation: float = 0.0

    # Per-day combined equity
    combined_equity: List[Dict] = field(default_factory=list)
    daily_returns_spy: List[float] = field(default_factory=list)
    daily_returns_qqq: List[float] = field(default_factory=list)


def run_portfolio(specs: List[TickerSpec], verbose: bool = False) -> PortfolioResults:
    """Run backtests for all tickers and combine into portfolio."""
    results = PortfolioResults()
    results.total_capital = sum(s.account_size for s in specs)

    all_daily_pnl = {}  # date -> {ticker: pnl}

    for spec in specs:
        print(f"\n{'='*60}")
        print(f"Running {spec.ticker} backtest...")
        print(f"{'='*60}")

        # Load data
        df = pd.read_csv(spec.data_file, parse_dates=["timestamp"], index_col="timestamp")
        df.index = pd.to_datetime(df.index, utc=True)

        # Create backtester
        bt = ScalpBacktester(
            config=spec.config,
            account_size=spec.account_size,
            spx_mode=spec.spx_mode,
            ndx_mode=spec.ndx_mode,
        )

        # Run
        r = bt.run(df, ticker=spec.ticker, interval="1m", verbose=verbose)
        results.ticker_results[spec.ticker] = r

        # Collect daily PnL
        for day_result in r.daily_results:
            d = day_result.date
            if d not in all_daily_pnl:
                all_daily_pnl[d] = {}
            all_daily_pnl[d][spec.ticker] = day_result.day_pnl

        # Print per-ticker summary
        print(f"\n  {spec.ticker} Results:")
        print(f"    Trades: {r.total_trades}")
        print(f"    Win Rate: {r.win_rate:.1%}")
        print(f"    Profit Factor: {r.profit_factor:.2f}")
        print(f"    Total PnL: ${r.total_pnl:+,.0f}")
        print(f"    MaxDD: {r.max_drawdown_pct:.1%}")
        print(f"    Scalp: ${r.scalp_pnl:+,.0f}  Runner: ${r.runner_pnl:+,.0f}  "
              f"ORB: ${r.orb_pnl:+,.0f}  RF: ${r.rf_pnl:+,.0f}")

    # ── Combine equity curves ─────────────────────────────────
    tickers = [s.ticker for s in specs]
    all_dates = sorted(all_daily_pnl.keys())

    # Per-ticker equity tracking
    equity = {t: specs[i].account_size for i, t in enumerate(tickers)}
    peaks = {t: specs[i].account_size for i, t in enumerate(tickers)}
    combined = sum(equity.values())
    combined_peak = combined
    worst_dd = 0.0
    worst_dd_dollars = 0.0

    # Track daily returns for correlation / Sharpe
    daily_rets_by_ticker = {t: [] for t in tickers}

    results.combined_equity.append({
        "date": str(all_dates[0]) if all_dates else "N/A",
        "combined": combined,
        **{t: equity[t] for t in tickers},
    })

    total_gross_profit = 0.0
    total_gross_loss = 0.0
    total_wins = 0
    total_count = 0

    for d in all_dates:
        day_pnl = 0.0
        for t in tickers:
            pnl = all_daily_pnl[d].get(t, 0.0)
            equity[t] += pnl
            day_pnl += pnl
            daily_rets_by_ticker[t].append(pnl)

        combined = sum(equity.values())
        combined_peak = max(combined_peak, combined)
        dd_dollars = combined_peak - combined
        dd_pct = dd_dollars / combined_peak if combined_peak > 0 else 0
        worst_dd = max(worst_dd, dd_pct)
        worst_dd_dollars = max(worst_dd_dollars, dd_dollars)

        results.combined_equity.append({
            "date": str(d),
            "combined": round(combined, 2),
            **{t: round(equity[t], 2) for t in tickers},
        })

    # ── Aggregate trade stats ─────────────────────────────────
    all_trades = []
    for t, r in results.ticker_results.items():
        all_trades.extend(r.trades)
        total_gross_profit += sum(t.total_pnl for t in r.trades if t.total_pnl > 0)
        total_gross_loss += abs(sum(t.total_pnl for t in r.trades if t.total_pnl < 0))
        total_wins += sum(1 for t in r.trades if t.total_pnl > 0)
        total_count += r.total_trades

    results.total_pnl = sum(r.total_pnl for r in results.ticker_results.values())
    results.total_trades = total_count
    results.portfolio_pf = (total_gross_profit / total_gross_loss
                            if total_gross_loss > 0 else float('inf'))
    results.portfolio_wr = total_wins / total_count if total_count > 0 else 0
    results.portfolio_max_dd_pct = worst_dd
    results.portfolio_max_dd_dollars = worst_dd_dollars

    # ── Sharpe ratio (daily) ──────────────────────────────────
    combined_daily = [sum(daily_rets_by_ticker[t][i] for t in tickers)
                      for i in range(len(all_dates))]
    if len(combined_daily) > 1:
        mean_daily = np.mean(combined_daily)
        std_daily = np.std(combined_daily, ddof=1)
        results.portfolio_sharpe = (mean_daily / std_daily * np.sqrt(252)
                                    if std_daily > 0 else 0)

    # ── Correlation between tickers ───────────────────────────
    if len(tickers) >= 2 and len(all_dates) > 5:
        t1_rets = np.array(daily_rets_by_ticker[tickers[0]])
        t2_rets = np.array(daily_rets_by_ticker[tickers[1]])
        # Only compute on days where at least one has non-zero
        mask = (t1_rets != 0) | (t2_rets != 0)
        if mask.sum() > 5:
            results.correlation = float(np.corrcoef(t1_rets[mask], t2_rets[mask])[0, 1])

    return results


def run_portfolio_shared(specs: List[TickerSpec], account_size: float = 10_000.0,
                         verbose: bool = False) -> PortfolioResults:
    """
    Run portfolio with SHARED balance — single account for all tickers.

    Unlike run_portfolio() which gives each ticker its own capital,
    this mode uses ONE balance shared across all tickers. Each day,
    both tickers see the same balance for position sizing decisions.

    This accurately models a $10K IBKR account trading SPY + QQQ.
    """
    results = PortfolioResults()
    results.total_capital = account_size

    # ── Prepare each backtester ───────────────────────────────
    backtests = {}  # ticker -> (ScalpBacktester, {date: day_bars})
    for spec in specs:
        print(f"  Preparing {spec.ticker}...")
        df = pd.read_csv(spec.data_file, parse_dates=["timestamp"], index_col="timestamp")
        df.index = pd.to_datetime(df.index, utc=True)

        bt = ScalpBacktester(
            config=spec.config,
            account_size=account_size,
            spx_mode=spec.spx_mode,
            ndx_mode=spec.ndx_mode,
        )
        days = bt.prepare(df, ticker=spec.ticker, interval="1m")
        day_dict = {d: bars for d, bars in days}
        backtests[spec.ticker] = (bt, day_dict)

    # ── Collect all trading dates ─────────────────────────────
    all_dates = sorted(set(
        d for _, (_, day_dict) in backtests.items() for d in day_dict.keys()
    ))

    # ── Day-by-day shared balance execution ───────────────────
    balance = account_size
    peak = balance
    worst_dd = 0.0
    worst_dd_dollars = 0.0
    all_daily_pnl = {}
    per_ticker_results = {t: [] for t in backtests}  # day results
    per_ticker_trades = {t: [] for t in backtests}

    for d in all_dates:
        day_combined_pnl = 0.0
        all_daily_pnl[d] = {}

        for ticker, (bt, day_dict) in backtests.items():
            if d in day_dict:
                day_result = bt.run_single_day(d, day_dict[d], balance, verbose)
                per_ticker_results[ticker].append(day_result)
                per_ticker_trades[ticker].extend(day_result._trades)
                day_pnl = day_result.day_pnl
            else:
                day_pnl = 0.0
            all_daily_pnl[d][ticker] = day_pnl
            day_combined_pnl += day_pnl

        balance += day_combined_pnl
        peak = max(peak, balance)
        dd_dollars = peak - balance
        dd_pct = dd_dollars / peak if peak > 0 else 0
        worst_dd = max(worst_dd, dd_pct)
        worst_dd_dollars = max(worst_dd_dollars, dd_dollars)

        results.combined_equity.append({
            "date": str(d), "combined": round(balance, 2)
        })

    # ── Build per-ticker ScalpBacktestResults ─────────────────
    tickers = [s.ticker for s in specs]
    total_gross_profit = 0.0
    total_gross_loss = 0.0
    total_wins = 0
    total_count = 0
    daily_rets_by_ticker = {t: [] for t in tickers}

    for spec in specs:
        t = spec.ticker
        r = ScalpBacktestResults(
            ticker=t, interval="1m", starting_balance=account_size,
        )
        r.daily_results = per_ticker_results[t]
        r.trades = per_ticker_trades[t]
        if all_dates:
            r.start_date = all_dates[0]
            r.end_date = all_dates[-1]
            r.total_days = len(all_dates)

        # Compute per-ticker balance trajectory for MaxDD
        t_balance = account_size
        t_peak = t_balance
        t_max_dd = 0.0
        for day_result in r.daily_results:
            t_balance += day_result.day_pnl
            t_peak = max(t_peak, t_balance)
            dd = (t_peak - t_balance) / t_peak if t_peak > 0 else 0
            t_max_dd = max(t_max_dd, dd)
            if day_result.trades_entered > 0:
                r.days_traded += 1

        r.max_drawdown_pct = t_max_dd
        r.ending_balance = round(t_balance, 2)

        # Use existing _compute_stats
        bt_instance = backtests[t][0]
        bt_instance._compute_stats(r)
        results.ticker_results[t] = r

        # Aggregate
        for trade in r.trades:
            if trade.total_pnl > 0:
                total_gross_profit += trade.total_pnl
                total_wins += 1
            else:
                total_gross_loss += abs(trade.total_pnl)
        total_count += r.total_trades

        # Daily returns for correlation
        for d in all_dates:
            pnl = all_daily_pnl[d].get(t, 0.0)
            daily_rets_by_ticker[t].append(pnl)

        print(f"\n  {t}: {r.total_trades} trades, WR={r.win_rate:.1f}%, "
              f"PF={r.profit_factor:.2f}, PnL=${r.total_pnl:+,.0f}")

    # ── Portfolio aggregation ─────────────────────────────────
    results.total_pnl = sum(r.total_pnl for r in results.ticker_results.values())
    results.total_trades = total_count
    results.portfolio_pf = (total_gross_profit / total_gross_loss
                            if total_gross_loss > 0 else float('inf'))
    results.portfolio_wr = total_wins / total_count if total_count > 0 else 0
    results.portfolio_max_dd_pct = worst_dd
    results.portfolio_max_dd_dollars = worst_dd_dollars

    combined_daily = [sum(daily_rets_by_ticker[t][i] for t in tickers)
                      for i in range(len(all_dates))]
    if len(combined_daily) > 1:
        mean_daily = np.mean(combined_daily)
        std_daily = np.std(combined_daily, ddof=1)
        results.portfolio_sharpe = (mean_daily / std_daily * np.sqrt(252)
                                    if std_daily > 0 else 0)

    if len(tickers) >= 2 and len(all_dates) > 5:
        t1_rets = np.array(daily_rets_by_ticker[tickers[0]])
        t2_rets = np.array(daily_rets_by_ticker[tickers[1]])
        mask = (t1_rets != 0) | (t2_rets != 0)
        if mask.sum() > 5:
            results.correlation = float(np.corrcoef(t1_rets[mask], t2_rets[mask])[0, 1])

    return results


def print_portfolio_report(results: PortfolioResults):
    """Print comprehensive portfolio report."""
    print("\n" + "=" * 70)
    print("PORTFOLIO BACKTEST REPORT")
    print("=" * 70)

    # Per-ticker summary
    print(f"\n{'Ticker':>8} {'Trades':>7} {'WR':>6} {'PF':>6} {'PnL':>12} {'MaxDD':>7} {'Return':>8}")
    print("-" * 60)
    for ticker, r in results.ticker_results.items():
        ret = r.total_pnl / r.starting_balance
        print(f"{ticker:>8} {r.total_trades:>7} {r.win_rate:>5.1f}% {r.profit_factor:>6.2f} "
              f"${r.total_pnl:>+10,.0f} {r.max_drawdown_pct:>7.1%} {ret:>8.0%}")

    # Portfolio summary
    print(f"\n{'PORTFOLIO':>8} {results.total_trades:>7} {results.portfolio_wr*100:>5.1f}% "
          f"{results.portfolio_pf:>6.2f} ${results.total_pnl:>+10,.0f} "
          f"{results.portfolio_max_dd_pct:>7.1%} "
          f"{results.total_pnl/results.total_capital:>8.0%}")

    # Additional metrics
    print(f"\n  Capital deployed: ${results.total_capital:,.0f}")
    print(f"  Sharpe ratio (daily, annualized): {results.portfolio_sharpe:.2f}")
    if results.correlation != 0:
        print(f"  Ticker correlation: {results.correlation:.3f}")
    print(f"  Max drawdown: ${results.portfolio_max_dd_dollars:,.0f} "
          f"({results.portfolio_max_dd_pct:.1%})")

    # Strategy breakdown across all tickers
    print(f"\n  Strategy Breakdown (all tickers):")
    total_scalp = sum(r.scalp_pnl for r in results.ticker_results.values())
    total_runner = sum(r.runner_pnl for r in results.ticker_results.values())
    total_orb = sum(r.orb_pnl for r in results.ticker_results.values())
    total_rf = sum(r.rf_pnl for r in results.ticker_results.values())
    print(f"    Scalp:  ${total_scalp:>+10,.0f}")
    print(f"    Runner: ${total_runner:>+10,.0f}")
    print(f"    ORB:    ${total_orb:>+10,.0f}")
    print(f"    RF:     ${total_rf:>+10,.0f}")

    # Unique trading days
    spy_days = set()
    qqq_days = set()
    for ticker, r in results.ticker_results.items():
        for dr in r.daily_results:
            if dr.trades_entered > 0:
                if ticker == "SPY":
                    spy_days.add(dr.date)
                elif ticker == "QQQ":
                    qqq_days.add(dr.date)
    if spy_days and qqq_days:
        both = spy_days & qqq_days
        spy_only = spy_days - qqq_days
        qqq_only = qqq_days - spy_days
        total_active = spy_days | qqq_days
        print(f"\n  Trading Day Coverage:")
        print(f"    SPY-only days: {len(spy_only)}")
        print(f"    QQQ-only days: {len(qqq_only)}")
        print(f"    Both traded: {len(both)}")
        print(f"    Total active days: {len(total_active)} / {len(results.combined_equity)-1}")


def main():
    parser = argparse.ArgumentParser(description="Multi-Ticker Portfolio Backtest")
    parser.add_argument("--tickers", nargs="+", default=["SPY", "QQQ"],
                        help="Tickers to backtest (default: SPY QQQ)")
    parser.add_argument("--account", type=float, default=10_000.0,
                        help="Account size (default: $10,000)")
    parser.add_argument("--shared", action="store_true",
                        help="Shared balance mode: single account for all tickers. "
                             "Both tickers see the same balance for position sizing.")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    data_dir = os.path.join(os.path.dirname(__file__), "data", "intraday")

    specs = []
    for ticker in args.tickers:
        if ticker.upper() == "SPY":
            specs.append(TickerSpec(
                ticker="SPY",
                data_file=os.path.join(data_dir, "SPY_ibkr_1m_180d.csv"),
                config=EngineConfig(),
                account_size=args.account,
                spx_mode=True,
            ))
        elif ticker.upper() == "QQQ":
            specs.append(TickerSpec(
                ticker="QQQ",
                data_file=os.path.join(data_dir, "QQQ_ibkr_1m_180d.csv"),
                config=EngineConfig.for_qqq(),
                account_size=args.account,
                spx_mode=True,  # Use SPX-like pricing for QQQ
            ))
        else:
            print(f"Unknown ticker: {ticker}. Supported: SPY, QQQ")
            sys.exit(1)

    if args.shared:
        print(f"\n{'='*60}")
        print(f"SHARED BALANCE MODE: ${args.account:,.0f} single account")
        print(f"{'='*60}")
        results = run_portfolio_shared(specs, account_size=args.account,
                                       verbose=args.verbose)
    else:
        results = run_portfolio(specs, verbose=args.verbose)
    print_portfolio_report(results)


if __name__ == "__main__":
    main()
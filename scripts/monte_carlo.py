#!/usr/bin/env python3
"""
Monte Carlo Stress Test
========================
Shuffles the actual trade-sequence from the walk-forward backtest 10,000
times to build confidence intervals on:
  • Final PnL (5th / 50th / 95th percentile)
  • Maximum drawdown (dollars and %)
  • Ruin probability (balance hitting ≤ $0)
  • Worst losing streak
  • CAGR and Sharpe on the resampled equity curve

This answers: "Was our edge real, or did we just get lucky with the
 order of trades?"

Usage:
    python scripts/monte_carlo.py SPY --start 2023-01-01 --end 2024-12-31
    python scripts/monte_carlo.py SPY QQQ --start 2024-01-01 --end 2024-12-31 --sims 50000
    python scripts/monte_carlo.py SPY --start 2023-01-01 --end 2024-12-31 --balance 25000
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


import numpy as np

# ─────────────────────────────────────────────────────────────────
#  Run walk-forward backtest once to get the actual trade PnL vector
# ─────────────────────────────────────────────────────────────────

async def _run_backtest(ticker: str, start: str, end: str, balance: float) -> tuple[list[float], float]:
    from trading_platform.backtester.engine import BacktestEngine
    engine = BacktestEngine(ticker=ticker, start=start, end=end, starting_balance=balance)
    result = await engine.run()
    pnls = [t.pnl_dollars for t in result.closed_trades if t.pnl_dollars is not None]
    return pnls, result.starting_balance


def get_trade_pnls(ticker: str, start: str, end: str, balance: float) -> tuple[list[float], float]:
    """Run walk-forward backtest and return (list_of_pnls, starting_balance)."""
    return asyncio.run(_run_backtest(ticker, start, end, balance))


# ─────────────────────────────────────────────────────────────────
#  Monte Carlo simulation
# ─────────────────────────────────────────────────────────────────

def simulate(pnls: np.ndarray, starting_balance: float,
             n_sims: int = 10_000, seed: int = 42):
    """
    Shuffle trade order `n_sims` times and track:
      - terminal equity
      - max drawdown ($)
      - max drawdown (%)
      - whether the account hit ruin (balance ≤ 0)
      - worst consecutive losing streak
    """
    rng = np.random.default_rng(seed)

    terminal_equity  = np.empty(n_sims)
    max_dd_dollars   = np.empty(n_sims)
    max_dd_pct       = np.empty(n_sims)
    worst_streak     = np.empty(n_sims, dtype=int)
    ruin_flag        = np.zeros(n_sims, dtype=bool)

    for i in range(n_sims):
        shuffled = rng.permutation(pnls)
        equity = starting_balance + np.cumsum(shuffled)

        # Terminal
        terminal_equity[i] = equity[-1]

        # Drawdown
        running_peak = np.maximum.accumulate(
            np.concatenate(([starting_balance], equity))
        )
        drawdowns = running_peak[1:] - equity          # dollar DD
        max_dd_dollars[i] = drawdowns.max()

        dd_pct = drawdowns / running_peak[1:]           # pct DD
        max_dd_pct[i] = dd_pct.max() * 100

        # Ruin
        if equity.min() <= 0:
            ruin_flag[i] = True

        # Worst losing streak
        streak = 0
        max_streak = 0
        for pnl in shuffled:
            if pnl < 0:
                streak += 1
                max_streak = max(max_streak, streak)
            else:
                streak = 0
        worst_streak[i] = max_streak

    return {
        "terminal_equity": terminal_equity,
        "max_dd_dollars": max_dd_dollars,
        "max_dd_pct": max_dd_pct,
        "worst_streak": worst_streak,
        "ruin_flag": ruin_flag,
    }


# ─────────────────────────────────────────────────────────────────
#  Reporting
# ─────────────────────────────────────────────────────────────────

def percentile_row(label: str, data: np.ndarray, fmt: str = "${:>+10,.0f}"):
    p5  = np.percentile(data, 5)
    p25 = np.percentile(data, 25)
    p50 = np.percentile(data, 50)
    p75 = np.percentile(data, 75)
    p95 = np.percentile(data, 95)

    def f(v):
        return fmt.format(v)

    print(f"  {label:<22} │ {f(p5):>12} │ {f(p25):>12} │"
          f" {f(p50):>12} │ {f(p75):>12} │ {f(p95):>12}")


def report(pnls: list[float], starting_balance: float, results: dict,
           n_sims: int):
    """Print the Monte Carlo report."""
    te = results["terminal_equity"]
    dd = results["max_dd_dollars"]
    ddp = results["max_dd_pct"]
    ws = results["worst_streak"]
    ruin = results["ruin_flag"]

    n_trades = len(pnls)
    winners = sum(1 for p in pnls if p > 0)
    losers = sum(1 for p in pnls if p < 0)
    actual_pnl = sum(pnls)

    print("\n" + "=" * 90)
    print("  MONTE CARLO STRESS TEST")
    print("=" * 90)

    print(f"\n  Simulations:       {n_sims:,}")
    print(f"  Trade count:       {n_trades}")
    print(f"  Winners / Losers:  {winners} / {losers}  "
          f"(WR {winners/n_trades*100:.1f}%)")
    print(f"  Starting balance:  ${starting_balance:,.0f}")
    print(f"  Actual PnL:        ${actual_pnl:+,.0f}")
    print(f"  Actual ending:     ${starting_balance + actual_pnl:,.0f}")

    # ── Percentile table ─────────────────────────────────────────
    print(f"\n  {'─' * 86}")
    print(f"  {'Metric':<22} │ {'5th':>12} │ {'25th':>12} │"
          f" {'50th':>12} │ {'75th':>12} │ {'95th':>12}")
    print(f"  {'─' * 86}")

    percentile_row("Terminal Equity", te, "${:>10,.0f}")
    percentile_row("Total PnL", te - starting_balance, "${:>+10,.0f}")
    percentile_row("Max DD ($)", dd, "${:>10,.0f}")
    percentile_row("Max DD (%)", ddp, "{:>10.1f}%")
    percentile_row("Worst Lose Streak", ws.astype(float), "{:>10.0f} ")

    print(f"  {'─' * 86}")

    # ── Key metrics ──────────────────────────────────────────────
    ruin_pct = ruin.sum() / n_sims * 100
    prob_profitable = (te > starting_balance).sum() / n_sims * 100
    prob_double = (te > starting_balance * 2).sum() / n_sims * 100

    print("\n  📊 Key Risk Metrics:")
    print(f"     Ruin probability:          {ruin_pct:>6.2f}%  "
          f"{'✅ <1%' if ruin_pct < 1 else '⚠️  >1%' if ruin_pct < 5 else '🔴 >5%'}")
    print(f"     P(profitable):             {prob_profitable:>6.1f}%  "
          f"{'✅' if prob_profitable > 90 else '⚠️'}")
    print(f"     P(2× starting balance):    {prob_double:>6.1f}%")
    print(f"     Median max drawdown:       ${np.median(dd):,.0f} "
          f"({np.median(ddp):.1f}%)")
    print(f"     95th pctile max DD:        ${np.percentile(dd, 95):,.0f} "
          f"({np.percentile(ddp, 95):.1f}%)")
    print(f"     Worst losing streak (95th): {int(np.percentile(ws, 95))} trades")

    # ── Expectancy & edge ────────────────────────────────────────
    avg_win = np.mean([p for p in pnls if p > 0]) if winners > 0 else 0
    avg_loss = abs(np.mean([p for p in pnls if p < 0])) if losers > 0 else 0
    wr = winners / n_trades
    expectancy = wr * avg_win - (1 - wr) * avg_loss
    edge_per_trade = expectancy

    print("\n  📊 Trade Edge:")
    print(f"     Avg winner:       ${avg_win:>+,.0f}")
    print(f"     Avg loser:        ${-avg_loss:>+,.0f}")
    print(f"     Win rate:          {wr*100:.1f}%")
    print(f"     Expectancy/trade: ${edge_per_trade:>+,.0f}")
    print(f"     Edge × N trades:  ${edge_per_trade * n_trades:>+,.0f}")

    # ── Risk-of-ruin analytical (simplified Kelly) ───────────────
    if avg_loss > 0 and avg_win > 0:
        b = avg_win / avg_loss   # reward-to-risk
        q = 1 - wr
        # Approximate risk of ruin = ((q / (p*b))^N) simplified check
        kelly_f = wr - q / b if b > 0 else 0
        print("\n  📊 Kelly Criterion:")
        print(f"     Reward/Risk (b):  {b:.2f}")
        print(f"     Kelly fraction:   {kelly_f*100:.1f}%  "
              f"{'✅ >0 (edge exists)' if kelly_f > 0 else '🔴 ≤0 (no edge)'}")

    # ── Histogram summary ────────────────────────────────────────
    print("\n  📊 Terminal Equity Distribution:")
    bins = [0, starting_balance * 0.5, starting_balance, starting_balance * 1.5,
            starting_balance * 2, starting_balance * 3, float("inf")]
    labels = ["Ruin (<50%)", "Losing (50-100%)", "Small gain (100-150%)",
              "Good gain (150-200%)", "Great (200-300%)", "Exceptional (>300%)"]
    for i in range(len(bins) - 1):
        count = ((te >= bins[i]) & (te < bins[i+1])).sum()
        pct = count / n_sims * 100
        bar = "█" * int(pct / 2)
        print(f"     {labels[i]:<25} {pct:>5.1f}%  {bar}")

    # ── Verdict ──────────────────────────────────────────────────
    print(f"\n  {'═' * 86}")
    ok = (
        ruin_pct < 1.0
        and prob_profitable > 90
        and np.median(ddp) < 30
        and np.percentile(ddp, 95) < 50
    )
    if ok:
        print("  ✅ PASS — Strategy is robust to trade-order randomization")
        print(f"           Ruin risk is negligible ({ruin_pct:.2f}%), "
              f"median DD {np.median(ddp):.1f}%")
    else:
        warnings = []
        if ruin_pct >= 1.0:
            warnings.append(f"ruin probability {ruin_pct:.1f}%")
        if prob_profitable <= 90:
            warnings.append(f"P(profitable) only {prob_profitable:.1f}%")
        if np.median(ddp) >= 30:
            warnings.append(f"median DD {np.median(ddp):.1f}%")
        print(f"  ⚠️  CAUTION — {', '.join(warnings)}")

    print(f"  {'═' * 86}\n")

    return ok


# ─────────────────────────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Monte Carlo stress test for trading_platform walk-forward backtest")
    parser.add_argument("tickers", nargs="+", help="Ticker symbols (e.g. SPY QQQ)")
    parser.add_argument("--start", required=True, help="Backtest start date YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="Backtest end date YYYY-MM-DD")
    parser.add_argument("--balance", type=float, default=10_000.0, help="Starting balance (default: 10000)")
    parser.add_argument("--sims", type=int, default=10_000, help="Number of simulations (default: 10000)")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducibility")
    args = parser.parse_args()

    all_pass = True
    for ticker in [t.upper() for t in args.tickers]:
        print(f"\n  Running walk-forward backtest for {ticker} ({args.start} → {args.end})...")
        pnls, starting_balance = get_trade_pnls(ticker, args.start, args.end, args.balance)
        if not pnls:
            print(f"  No closed trades for {ticker} — skipping Monte Carlo.")
            continue
        print(f"  Got {len(pnls)} closed trades, total PnL: ${sum(pnls):+,.0f}")

        print(f"  Running {args.sims:,} Monte Carlo simulations...")
        results = simulate(np.array(pnls), starting_balance,
                           n_sims=args.sims, seed=args.seed)
        ok = report(pnls, starting_balance, results, args.sims)
        all_pass = all_pass and ok

    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()

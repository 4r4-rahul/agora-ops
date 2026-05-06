"""
Backtest report printer and CSV exporter.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

from .models import BacktestResult


def print_report(result: BacktestResult) -> None:
    """Print a formatted backtest summary to stdout."""
    r = result
    sep = "─" * 72

    print(f"\n{'=' * 72}")
    print(f"  WALK-FORWARD BACKTEST — {r.ticker}  |  {r.start_date} → {r.end_date}")
    print(f"{'=' * 72}")

    print(f"\n  Starting balance:   ${r.starting_balance:>10,.0f}")
    print(f"  Ending balance:     ${r.ending_balance:>10,.0f}")
    print(f"  Total P&L:          ${r.total_pnl:>+10,.0f}  ({r.total_pnl / r.starting_balance * 100:+.1f}%)")
    print(f"  Max drawdown:       ${r.max_drawdown_dollars:>10,.0f}  ({r.max_drawdown_pct:.1f}%)")
    print(f"  Sharpe ratio:        {r.sharpe_ratio:>10.2f}")

    print(f"\n  {sep}")
    print(f"  {'Trades':>22}   Total: {r.total_trades}  |  Closed: {len(r.closed_trades)}")
    print(f"  {sep}")
    print(f"  Win rate:            {r.win_rate * 100:>9.1f}%")
    print(f"  Avg winner:         ${r.avg_win:>+10,.0f}")
    print(f"  Avg loser:          ${r.avg_loss:>+10,.0f}")
    print(f"  Profit factor:       {r.profit_factor:>10.2f}")
    print(f"  Expectancy/trade:   ${r.expectancy:>+10,.0f}")

    if r.closed_trades:
        wins = len(r.winning_trades)
        losses = len(r.losing_trades)
        print(f"  Winners / Losers:    {wins:>4} / {losses:<4}")

    # Strategy breakdown
    by_strategy: dict[str, list] = {}
    for t in r.closed_trades:
        by_strategy.setdefault(t.strategy, []).append(t.pnl_dollars or 0)

    if by_strategy:
        print(f"\n  {sep}")
        print(f"  {'Strategy':<25} {'Trades':>6} {'WR':>6} {'Avg PnL':>10} {'Total':>10}")
        print(f"  {sep}")
        for strat, pnls in sorted(by_strategy.items(), key=lambda x: -sum(x[1])):
            wins_s = sum(1 for p in pnls if p > 0)
            wr = wins_s / len(pnls) * 100 if pnls else 0
            avg = sum(pnls) / len(pnls) if pnls else 0
            total = sum(pnls)
            print(f"  {strat:<25} {len(pnls):>6} {wr:>5.0f}% {avg:>+10,.0f} {total:>+10,.0f}")

    # Verdict
    print(f"\n  {'=' * 72}")
    ok = (
        r.total_pnl > 0
        and r.win_rate >= 0.45
        and r.max_drawdown_pct < 30
        and r.sharpe_ratio > 0.5
    )
    if ok:
        print(f"  ✓  PASS — Strategy shows positive edge in walk-forward test")
    else:
        issues = []
        if r.total_pnl <= 0:
            issues.append(f"negative P&L ${r.total_pnl:+,.0f}")
        if r.win_rate < 0.45:
            issues.append(f"low win rate {r.win_rate*100:.0f}%")
        if r.max_drawdown_pct >= 30:
            issues.append(f"high drawdown {r.max_drawdown_pct:.1f}%")
        if r.sharpe_ratio <= 0.5:
            issues.append(f"low Sharpe {r.sharpe_ratio:.2f}")
        print(f"  ✗  CAUTION — {', '.join(issues)}")
    print(f"  {'=' * 72}\n")


def export_csv(result: BacktestResult, path: str | Path) -> None:
    """Export trade log to CSV."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    fields = [
        "date_opened", "date_closed", "ticker", "strategy", "direction",
        "entry_price", "exit_price", "contracts", "max_loss_dollars",
        "pnl_dollars", "reward_risk_ratio", "status",
    ]

    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for t in result.trades:
            writer.writerow({
                "date_opened": t.date_opened,
                "date_closed": t.date_closed or "",
                "ticker": t.ticker,
                "strategy": t.strategy,
                "direction": t.direction,
                "entry_price": f"{t.entry_price:.2f}",
                "exit_price": f"{t.exit_price:.2f}" if t.exit_price is not None else "",
                "contracts": t.contracts,
                "max_loss_dollars": f"{t.max_loss_dollars:.0f}",
                "pnl_dollars": f"{t.pnl_dollars:+.0f}" if t.pnl_dollars is not None else "",
                "reward_risk_ratio": f"{t.reward_risk_ratio:.2f}",
                "status": t.status.value,
            })
    print(f"  Trade log written → {path}")

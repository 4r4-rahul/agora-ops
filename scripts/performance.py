#!/usr/bin/env python3
"""
Trading Performance Analytics
==============================
Reads trade_journal.db and produces a full practitioner-grade report:

  • Overall edge metrics (win rate, expectancy, Kelly fraction)
  • P&L breakdown by strategy and ticker
  • Monthly P&L curve
  • Drawdown analysis
  • Best / worst trades
  • Open position summary

Usage:
    python scripts/performance.py
    python scripts/performance.py --db /path/to/trade_journal.db
    python scripts/performance.py --closed-only
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import os
from datetime import datetime
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ─────────────────────────────────────────────────────────────────
#  Data loading
# ─────────────────────────────────────────────────────────────────

def load_trades(db_path: str) -> tuple[list[dict], list[dict]]:
    """Return (closed_trades, open_trades) as list of dicts."""
    if not os.path.exists(db_path):
        print(f"  Database not found: {db_path}")
        sys.exit(1)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        closed = [dict(r) for r in conn.execute(
            "SELECT * FROM trade_journal WHERE status = 'closed' AND realized_pnl IS NOT NULL "
            "ORDER BY closed_at ASC"
        ).fetchall()]
        open_ = [dict(r) for r in conn.execute(
            "SELECT * FROM trade_journal WHERE status = 'open' ORDER BY opened_at ASC"
        ).fetchall()]

    return closed, open_


# ─────────────────────────────────────────────────────────────────
#  Analytics
# ─────────────────────────────────────────────────────────────────

def edge_metrics(trades: list[dict]) -> dict:
    if not trades:
        return {}

    pnls = [t["realized_pnl"] for t in trades]
    winners = [p for p in pnls if p > 0]
    losers  = [p for p in pnls if p < 0]
    scratch = [p for p in pnls if p == 0]

    n = len(pnls)
    win_rate = len(winners) / n if n else 0
    avg_win  = sum(winners) / len(winners) if winners else 0
    avg_loss = abs(sum(losers) / len(losers)) if losers else 0

    expectancy = win_rate * avg_win - (1 - win_rate) * avg_loss
    profit_factor = sum(winners) / abs(sum(losers)) if losers else float("inf")

    # Kelly fraction
    if avg_win > 0 and avg_loss > 0:
        b = avg_win / avg_loss
        kelly = win_rate - (1 - win_rate) / b
    else:
        kelly = 0.0

    # Max drawdown from equity curve
    balance = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        balance += p
        if balance > peak:
            peak = balance
        dd = peak - balance
        if dd > max_dd:
            max_dd = dd

    return {
        "total_trades": n,
        "winners": len(winners),
        "losers": len(losers),
        "scratch": len(scratch),
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "expectancy": expectancy,
        "profit_factor": profit_factor,
        "kelly": kelly,
        "total_pnl": sum(pnls),
        "max_drawdown": max_dd,
        "gross_profit": sum(winners),
        "gross_loss": abs(sum(losers)),
        "best_trade": max(pnls) if pnls else 0,
        "worst_trade": min(pnls) if pnls else 0,
    }


def by_strategy(trades: list[dict]) -> dict[str, dict]:
    groups: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        groups[t["strategy"]].append(t["realized_pnl"])
    return {s: edge_metrics([{"realized_pnl": p} for p in pnls]) for s, pnls in groups.items()}


def by_ticker(trades: list[dict]) -> dict[str, dict]:
    groups: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        groups[t["ticker"]].append(t["realized_pnl"])
    return {tk: edge_metrics([{"realized_pnl": p} for p in pnls]) for tk, pnls in groups.items()}


def by_exit_reason(trades: list[dict]) -> dict[str, dict]:
    groups: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        reason = t.get("exit_reason") or "unknown"
        groups[reason].append(t["realized_pnl"])
    return {r: edge_metrics([{"realized_pnl": p} for p in pnls]) for r, pnls in groups.items()}


def monthly_pnl(trades: list[dict]) -> dict[str, float]:
    months: dict[str, float] = defaultdict(float)
    for t in trades:
        closed_at = t.get("closed_at") or ""
        try:
            month = datetime.fromisoformat(closed_at).strftime("%Y-%m")
        except Exception:
            month = "unknown"
        months[month] += t["realized_pnl"]
    return dict(sorted(months.items()))


# ─────────────────────────────────────────────────────────────────
#  Formatting helpers
# ─────────────────────────────────────────────────────────────────

W = 90

def _bar(pnl: float, scale: float = 50.0) -> str:
    chars = int(abs(pnl) / scale)
    chars = min(chars, 40)
    return ("█" * chars) if pnl >= 0 else ("▓" * chars)


def _pnl_color(v: float) -> str:
    return f"${v:>+,.0f}"


def _hr(char: str = "─") -> str:
    return "  " + char * (W - 2)


def _section(title: str) -> None:
    print()
    print("  " + "═" * (W - 2))
    print(f"  {title}")
    print("  " + "═" * (W - 2))


# ─────────────────────────────────────────────────────────────────
#  Report
# ─────────────────────────────────────────────────────────────────

def report(closed: list[dict], open_: list[dict]) -> None:
    m = edge_metrics(closed)

    print()
    print("=" * W)
    print("  TRADING PERFORMANCE REPORT")
    print(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * W)

    if not closed:
        print("\n  No closed trades found — nothing to analyse yet.")
        print("  Run the scheduler and let some positions close first.\n")
        _open_summary(open_)
        return

    # ── Overall edge ──────────────────────────────────────────────
    _section("OVERALL EDGE")

    print(f"  Total closed trades:   {m['total_trades']}")
    print(f"  Winners / Losers:      {m['winners']} / {m['losers']}  "
          f"(scratch: {m['scratch']})")
    print(f"  Win rate:              {m['win_rate']:.1%}")
    print(f"  Avg winner:            {_pnl_color(m['avg_win'])}")
    print(f"  Avg loser:             {_pnl_color(-m['avg_loss'])}")
    print(f"  Expectancy / trade:    {_pnl_color(m['expectancy'])}")
    print(f"  Profit factor:         {m['profit_factor']:.2f}  "
          f"{'✅ >1.5' if m['profit_factor'] > 1.5 else '⚠️  <1.5'}")
    print(f"  Total P&L:             {_pnl_color(m['total_pnl'])}")
    print(f"  Gross profit:          {_pnl_color(m['gross_profit'])}")
    print(f"  Gross loss:            {_pnl_color(-m['gross_loss'])}")
    print(f"  Max drawdown:          ${m['max_drawdown']:,.0f}")
    print(f"  Best trade:            {_pnl_color(m['best_trade'])}")
    print(f"  Worst trade:           {_pnl_color(m['worst_trade'])}")

    # Kelly
    kelly_pct = m["kelly"] * 100
    kelly_half = kelly_pct / 2
    if kelly_pct > 0:
        kelly_label = f"✅ Edge exists — full Kelly {kelly_pct:.1f}%, use half-Kelly {kelly_half:.1f}%"
    else:
        kelly_label = "🔴 Negative Kelly — no statistical edge yet, reduce size"
    print(f"  Kelly fraction:        {kelly_pct:.1f}%  {kelly_label}")

    # ── By strategy ───────────────────────────────────────────────
    _section("P&L BY STRATEGY")
    strats = by_strategy(closed)
    print(f"  {'Strategy':<30} {'Trades':>6} {'WR':>6} {'PF':>6} {'Expect':>9} {'Total P&L':>12}")
    print(_hr())
    for strat, sm in sorted(strats.items(), key=lambda x: -x[1]["total_pnl"]):
        pf_str = f"{sm['profit_factor']:.2f}" if sm["profit_factor"] != float("inf") else "∞"
        print(f"  {strat:<30} {sm['total_trades']:>6} {sm['win_rate']:>5.0%} "
              f"{pf_str:>6} {_pnl_color(sm['expectancy']):>9} {_pnl_color(sm['total_pnl']):>12}")

    # ── By ticker ─────────────────────────────────────────────────
    _section("P&L BY TICKER")
    tickers = by_ticker(closed)
    print(f"  {'Ticker':<12} {'Trades':>6} {'WR':>6} {'Expect':>9} {'Total P&L':>12}")
    print(_hr())
    for ticker, tm in sorted(tickers.items(), key=lambda x: -x[1]["total_pnl"]):
        print(f"  {ticker:<12} {tm['total_trades']:>6} {tm['win_rate']:>5.0%} "
              f"{_pnl_color(tm['expectancy']):>9} {_pnl_color(tm['total_pnl']):>12}")

    # ── By exit reason ────────────────────────────────────────────
    _section("EXITS BY REASON")
    exits = by_exit_reason(closed)
    print(f"  {'Exit Reason':<20} {'Trades':>6} {'WR':>6} {'Avg P&L':>10} {'Total P&L':>12}")
    print(_hr())
    for reason, em in sorted(exits.items(), key=lambda x: -x[1]["total_pnl"]):
        print(f"  {reason:<20} {em['total_trades']:>6} {em['win_rate']:>5.0%} "
              f"{_pnl_color(em['expectancy']):>10} {_pnl_color(em['total_pnl']):>12}")

    # ── Monthly P&L ───────────────────────────────────────────────
    _section("MONTHLY P&L")
    monthly = monthly_pnl(closed)
    running = 0.0
    print(f"  {'Month':<10} {'P&L':>10}  {'Running':>10}  Chart")
    print(_hr())
    for month, pnl in monthly.items():
        running += pnl
        bar = _bar(pnl, scale=100)
        sign = "+" if pnl >= 0 else ""
        print(f"  {month:<10} {sign}${pnl:>8,.0f}  ${running:>+9,.0f}  {bar}")

    # ── Best / worst trades ────────────────────────────────────────
    _section("TOP 5 BEST TRADES")
    best = sorted(closed, key=lambda x: -x["realized_pnl"])[:5]
    _trade_table(best)

    _section("TOP 5 WORST TRADES")
    worst = sorted(closed, key=lambda x: x["realized_pnl"])[:5]
    _trade_table(worst)

    # ── Open positions ────────────────────────────────────────────
    _open_summary(open_)

    # ── Verdict ───────────────────────────────────────────────────
    _section("VERDICT")
    issues = []
    if m["win_rate"] < 0.50:
        issues.append(f"Win rate {m['win_rate']:.0%} < 50% — review entry criteria")
    if m["profit_factor"] < 1.0:
        issues.append(f"Profit factor {m['profit_factor']:.2f} < 1.0 — strategy has negative edge")
    if m["kelly"] < 0:
        issues.append("Negative Kelly — reduce position size until edge is proven")
    if m["total_trades"] < 20:
        issues.append(f"Only {m['total_trades']} closed trades — sample size too small for reliable conclusions")

    if not issues:
        print(f"  ✅ Strategy shows positive edge (PF={m['profit_factor']:.2f}, "
              f"WR={m['win_rate']:.0%}, Kelly={m['kelly']*100:.1f}%)")
        print(f"     Expectancy: {_pnl_color(m['expectancy'])} per trade")
        if m["kelly"] > 0:
            print(f"     Recommended sizing: half-Kelly = {m['kelly']*50:.1f}% of account per trade")
    else:
        print("  ⚠️  Areas requiring attention:")
        for issue in issues:
            print(f"     • {issue}")

    print()
    print("=" * W)
    print()


def _trade_table(trades: list[dict]) -> None:
    print(f"  {'Date':<12} {'Ticker':<8} {'Strategy':<28} {'P&L':>10}  Exit reason")
    print(_hr())
    for t in trades:
        closed_at = (t.get("closed_at") or "")[:10]
        pnl = t["realized_pnl"]
        print(f"  {closed_at:<12} {t['ticker']:<8} {t['strategy']:<28} "
              f"{_pnl_color(pnl):>10}  {t.get('exit_reason') or 'unknown'}")


def _open_summary(open_: list[dict]) -> None:
    if not open_:
        return
    _section(f"OPEN POSITIONS ({len(open_)})")
    print(f"  {'Opened':<12} {'Ticker':<8} {'Strategy':<28} {'Entry':>8} {'Stop':>8} {'Target':>8}  {'Size $':>8}")
    print(_hr())
    for t in open_:
        opened = (t.get("opened_at") or "")[:10]
        print(f"  {opened:<12} {t['ticker']:<8} {t['strategy']:<28} "
              f"${t['entry_price']:>6.2f}  ${t['stop_loss']:>5.2f}  ${t['profit_target']:>5.2f}  "
              f"${t['position_size_dollars']:>6,.0f}")


# ─────────────────────────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Trading performance analytics")
    parser.add_argument(
        "--db", default="./trade_journal.db",
        help="Path to trade_journal.db (default: ./trade_journal.db)",
    )
    parser.add_argument(
        "--closed-only", action="store_true",
        help="Only show closed trade analytics, skip open position table",
    )
    args = parser.parse_args()

    closed, open_ = load_trades(args.db)
    report(closed, [] if args.closed_only else open_)


if __name__ == "__main__":
    main()

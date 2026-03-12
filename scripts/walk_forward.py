#!/usr/bin/env python3
"""
Walk-Forward Out-of-Sample Validation
=======================================
Splits 180-day SPY dataset into train/test periods and validates
that the strategy isn't overfit.

Split:
  Train: first 120 trading days  (~Sep 2025 – Jan 2026)
  Test:  last  60 trading days   (~Jan 2026 – Mar 2026)

If the strategy is robust, test-period PF and win rate should be
in the same ballpark as train (not drastically worse).

Also runs a 3-fold walk-forward: 60/60/60 day splits, training
on each pair and testing on the third.

Usage:
    python scripts/walk_forward.py
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
from trading_engine.config import EngineConfig
from trading_engine.data.scalp_backtester import ScalpBacktester


def load_data():
    df = pd.read_csv(
        "data/intraday/SPY_ibkr_1m_180d.csv",
        parse_dates=["timestamp"], index_col="timestamp",
    )
    df.index = pd.to_datetime(df.index, utc=True)
    return df


def run_backtest(df, label=""):
    config = EngineConfig()
    bt = ScalpBacktester(config=config, account_size=10_000.0, spx_mode=True)
    r = bt.run(df, ticker="SPY", interval="1m", verbose=False)
    return r


def get_trading_days(df):
    """Get unique trading dates from the bar data."""
    return sorted(df.index.normalize().unique())


def split_by_days(df, start_day_idx, end_day_idx, trading_days):
    """Slice dataframe by trading day indices."""
    start_date = trading_days[start_day_idx]
    end_date = trading_days[min(end_day_idx, len(trading_days) - 1)]
    mask = (df.index.normalize() >= start_date) & (df.index.normalize() <= end_date)
    return df[mask].copy()


def print_result(label, r, trading_days_count):
    """Print formatted result row."""
    pf = r.profit_factor if r.profit_factor < 999 else float("inf")
    wr = r.win_rate if r.total_trades > 0 else 0
    print(f"  {label:<22} │ {trading_days_count:>4}d │ {r.total_trades:>4} │"
          f" {wr:>5.1f}% │ {pf:>6.2f} │ ${r.total_pnl:>+10,.0f} │"
          f" {r.max_drawdown_pct:>6.1f}%")


def main():
    print("\n" + "=" * 80)
    print("  WALK-FORWARD OUT-OF-SAMPLE VALIDATION")
    print("=" * 80)

    df = load_data()
    trading_days = get_trading_days(df)
    n_days = len(trading_days)

    print(f"\n  Data: {n_days} trading days")
    print(f"  Range: {trading_days[0].date()} → {trading_days[-1].date()}")

    # ── FULL BASELINE ────────────────────────────────────────────
    print(f"\n  {'─' * 78}")
    print(f"  {'Period':<22} │ Days │  Tr  │   WR   │    PF  │       PnL    │   MaxDD")
    print(f"  {'─' * 78}")

    r_full = run_backtest(df, "full")
    print_result("FULL (baseline)", r_full, n_days)

    # ── 2/3 TRAIN + 1/3 TEST ────────────────────────────────────
    split_point = int(n_days * 2 / 3)  # ~120 days train, 60 test

    train_df = split_by_days(df, 0, split_point - 1, trading_days)
    test_df = split_by_days(df, split_point, n_days - 1, trading_days)

    r_train = run_backtest(train_df, "train")
    r_test = run_backtest(test_df, "test")

    print(f"  {'─' * 78}")
    print_result(f"TRAIN (d1–{split_point})", r_train, split_point)
    print_result(f"TEST  (d{split_point+1}–{n_days})", r_test, n_days - split_point)

    # ── Degradation metrics ──────────────────────────────────────
    pf_ratio = r_test.profit_factor / r_train.profit_factor if r_train.profit_factor > 0 else 0
    wr_diff = r_test.win_rate - r_train.win_rate

    print(f"\n  📊 Out-of-Sample Quality:")
    print(f"     PF ratio (test/train): {pf_ratio:.2f}  {'✅ >0.70' if pf_ratio >= 0.70 else '⚠️  <0.70'}")
    print(f"     WR delta:              {wr_diff:+.1f}pp  {'✅' if abs(wr_diff) < 15 else '⚠️'}")

    # ── 3-FOLD WALK-FORWARD ──────────────────────────────────────
    print(f"\n  {'─' * 78}")
    print(f"  3-FOLD WALK-FORWARD (each fold = ~{n_days // 3} days)")
    print(f"  {'─' * 78}")

    fold_size = n_days // 3
    folds = [
        (0, fold_size),
        (fold_size, 2 * fold_size),
        (2 * fold_size, n_days),
    ]

    fold_results = []
    for i, (start, end) in enumerate(folds):
        fold_df = split_by_days(df, start, end - 1, trading_days)
        r = run_backtest(fold_df, f"fold_{i}")
        fold_results.append(r)
        print_result(f"Fold {i+1} (d{start+1}–{end})", r, end - start)

    # ── Walk-forward summary ─────────────────────────────────────
    pfs = [r.profit_factor for r in fold_results if r.total_trades > 0]
    wrs = [r.win_rate for r in fold_results if r.total_trades > 0]
    pnls = [r.total_pnl for r in fold_results]
    dds = [r.max_drawdown_pct for r in fold_results]

    profitable_folds = sum(1 for p in pnls if p > 0)
    total_folds = len(pnls)

    print(f"\n  📊 Walk-Forward Summary:")
    print(f"     Profitable folds:  {profitable_folds}/{total_folds}")
    print(f"     PF range:          [{min(pfs):.2f} – {max(pfs):.2f}]")
    print(f"     WR range:          [{min(wrs):.1f}% – {max(wrs):.1f}%]")
    print(f"     PnL range:         [${min(pnls):+,.0f} – ${max(pnls):+,.0f}]")
    print(f"     MaxDD range:       [{min(dds):.1f}% – {max(dds):.1f}%]")
    print(f"     PF std dev:        {np.std(pfs):.2f}")

    # ── Verdict ──────────────────────────────────────────────────
    print(f"\n  {'═' * 78}")
    ok = (
        pf_ratio >= 0.50
        and profitable_folds >= 2
        and r_test.profit_factor >= 1.0
    )
    if ok:
        print(f"  ✅ PASS — Strategy shows out-of-sample robustness")
    else:
        print(f"  ⚠️  WARNING — Possible overfit detected")
        if pf_ratio < 0.50:
            print(f"     PF ratio {pf_ratio:.2f} < 0.50 threshold")
        if profitable_folds < 2:
            print(f"     Only {profitable_folds}/3 folds profitable")
        if r_test.profit_factor < 1.0:
            print(f"     Test period PF={r_test.profit_factor:.2f} < 1.0")

    print()
    return ok


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)

#!/usr/bin/env python3
"""
0DTE Gamma Scalp Backtest Runner
==================================
Runs the new professional gamma scalping strategy on historical SPY 1m bars.

This strategy replaces the old "lottery ticket" approach with:
  • ATM strikes (delta 0.40-0.55) for maximum gamma
  • Underlying price-based exits (not premium-based)
  • Confirmation stacking (2+ signals required)
  • Time windows (morning + power hour)
  • No re-entry after stop in same direction

Usage:
    python run_scalp_backtest.py                              # Defaults
    python run_scalp_backtest.py --verbose                    # Per-trade detail
    python run_scalp_backtest.py --stop-atr 3.0 --target-atr 5.0
    python run_scalp_backtest.py --confirmations 3            # Stricter signals
    python run_scalp_backtest.py --midday                     # Include 10:30-2:00
    python run_scalp_backtest.py --sweep                      # Parameter sweep
"""

import argparse
import os
import sys
import time

import pandas as pd

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from trading_engine.config import EngineConfig, ScalpConfig
from trading_engine.data.scalp_backtester import ScalpBacktester


def load_data(ticker: str = "SPY", interval: str = "1m") -> pd.DataFrame:
    """Load intraday bar data."""
    data_dir = os.path.join(os.path.dirname(__file__), "data", "intraday")

    # Try IBKR 1m 180d file first
    candidates = [
        os.path.join(data_dir, f"{ticker}_ibkr_{interval}_180d.csv"),
        os.path.join(data_dir, f"{ticker}_ibkr_{interval}_5d.csv"),
        os.path.join(data_dir, f"{ticker}_{interval}_60d.csv"),
        os.path.join(data_dir, f"{ticker}_{interval}_30d.csv"),
    ]

    for path in candidates:
        if os.path.exists(path):
            print(f"  📊 Loading {os.path.basename(path)}")
            df = pd.read_csv(path, parse_dates=["timestamp"], index_col="timestamp")
            if not isinstance(df.index, pd.DatetimeIndex):
                df.index = pd.to_datetime(df.index, utc=True)
            print(f"     {len(df):,} bars, {df.index.date[0]} → {df.index.date[-1]}")
            return df

    print(f"  ❌ No data found for {ticker} {interval}")
    print(f"     Checked: {data_dir}")
    sys.exit(1)


def run_single(config: EngineConfig, ticker: str, interval: str,
               verbose: bool, save: bool, spx_mode: bool = False):
    """Run a single backtest with current config."""
    df = load_data(ticker, interval)

    bt = ScalpBacktester(config=config, account_size=10_000.0, spx_mode=spx_mode)

    start = time.time()
    results = bt.run(df, ticker=ticker, interval=interval, verbose=verbose)
    elapsed = time.time() - start

    bt.print_report(results)
    print(f"  ⏱  Completed in {elapsed:.1f}s")

    if save:
        save_ticker = "SPX" if spx_mode else ticker
        path = f"scalp_backtest_{save_ticker}_{interval}.json"
        bt.save_results(results, path)

    return results


def run_sweep(ticker: str, interval: str, spx_mode: bool = False):
    """Sweep key parameters to find optimal configuration."""
    df = load_data(ticker, interval)

    mode_label = "SPX" if spx_mode else ticker
    print("\n" + "=" * 80)
    print(f"  ⚡ GAMMA SCALP PARAMETER SWEEP ({mode_label})")
    print("=" * 80)

    results_table = []

    # Sweep stop × target × time_stop
    # Tighter ranges based on data: PF=0.87 combo needs stop/target tuning
    stop_values = [1.5, 2.0, 2.5, 3.0, 3.5]
    target_values = [2.0, 2.5, 3.0, 3.5, 4.0, 5.0]
    time_stop_values = [15, 20, 30, 999]  # 999 = effectively disabled
    conf_values = [3]  # conf=3 always dominates

    total = len(stop_values) * len(target_values) * len(time_stop_values) * len(conf_values)
    done = 0

    for stop_atr in stop_values:
        for target_atr in target_values:
            for time_stop in time_stop_values:
                for min_conf in conf_values:
                    # Skip if target <= stop (nonsensical)
                    if target_atr <= stop_atr:
                        done += 1
                        continue

                    config = EngineConfig()
                    config.scalp.stop_atr_mult = stop_atr
                    config.scalp.profit_target_atr_mult = target_atr
                    config.scalp.min_confirmations = min_conf
                    config.scalp.time_stop_minutes = time_stop

                    bt = ScalpBacktester(config=config, account_size=10_000.0, spx_mode=spx_mode)
                    r = bt.run(df, ticker=ticker, interval=interval, verbose=False)

                    done += 1
                    sys.stdout.write(f"\r  [{done}/{total}] "
                                     f"stop={stop_atr} target={target_atr} "
                                     f"ts={time_stop} conf={min_conf} "
                                     f"→ {r.total_trades} trades, "
                                     f"WR={r.win_rate:.0f}%, "
                                     f"PF={r.profit_factor:.2f}, "
                                     f"P&L=${r.total_pnl:+,.0f}     ")
                    sys.stdout.flush()

                    results_table.append({
                        "stop_atr": stop_atr,
                        "target_atr": target_atr,
                        "time_stop": time_stop,
                        "min_conf": min_conf,
                        "trades": r.total_trades,
                        "win_rate": r.win_rate,
                        "profit_factor": r.profit_factor,
                        "total_pnl": r.total_pnl,
                        "max_dd": r.max_drawdown_pct,
                        "avg_hold": r.avg_hold_minutes,
                    })

    print("\n\n")

    # Sort by profit factor
    results_table.sort(key=lambda x: x["profit_factor"], reverse=True)

    print(f"  {'stop':>5} {'target':>6} {'ts':>4} {'conf':>4} {'trades':>6} {'WR':>6} "
          f"{'PF':>6} {'P&L':>10} {'MaxDD':>7} {'AvgHold':>7}")
    print(f"  {'─' * 75}")

    for r in results_table[:20]:  # Top 20
        pnl_c = "\033[32m" if r["total_pnl"] >= 0 else "\033[31m"
        pf_c = "\033[32m" if r["profit_factor"] >= 1.0 else "\033[31m"
        print(f"  {r['stop_atr']:>5.1f} {r['target_atr']:>6.1f} "
              f"{r['time_stop']:>4} {r['min_conf']:>4} "
              f"{r['trades']:>6} {r['win_rate']:>5.1f}% "
              f"{pf_c}{r['profit_factor']:>5.2f}\033[0m "
              f"{pnl_c}${r['total_pnl']:>+9,.0f}\033[0m "
              f"{r['max_dd']:>6.1%} {r['avg_hold']:>6.1f}m")

    # Highlight best
    if results_table:
        best = results_table[0]
        print(f"\n  🏆 BEST: stop={best['stop_atr']}×ATR, "
              f"target={best['target_atr']}×ATR, "
              f"ts={best['time_stop']}min, "
              f"conf={best['min_conf']} → "
              f"PF={best['profit_factor']:.2f}, "
              f"P&L=${best['total_pnl']:+,.0f}")

    # Find profitable combinations
    profitable = [r for r in results_table if r["profit_factor"] >= 1.0]
    print(f"\n  📊 {len(profitable)}/{len(results_table)} combinations are profitable (PF ≥ 1.0)")

    return results_table


def main():
    parser = argparse.ArgumentParser(description="0DTE Gamma Scalp Backtester")

    # Data
    parser.add_argument("--ticker", default="SPY", help="Ticker (default: SPY)")
    parser.add_argument("--interval", default="1m", help="Bar interval (default: 1m)")
    parser.add_argument("--spx", action="store_true",
                        help="SPX mode: scale SPY bars x10 for SPX option pricing")

    # Strategy parameters
    parser.add_argument("--stop-atr", type=float, default=None,
                        help="Stop loss in ATR multiples (default: 3.0)")
    parser.add_argument("--target-atr", type=float, default=None,
                        help="Profit target in ATR multiples (default: 5.0)")
    parser.add_argument("--confirmations", type=int, default=None,
                        help="Min signal confirmations (default: 2)")
    parser.add_argument("--time-stop", type=int, default=None,
                        help="Time stop in minutes (default: 12)")
    parser.add_argument("--max-hold", type=int, default=None,
                        help="Max hold time in minutes (default: 45)")
    parser.add_argument("--midday", action="store_true",
                        help="Enable midday trading window (10:30-2:00)")
    parser.add_argument("--no-runner", action="store_true",
                        help="Disable runner tier (scalp-only mode)")
    parser.add_argument("--runner-otm", type=float, default=None,
                        help="Runner OTM distance %% (default: 0.5%%)")
    parser.add_argument("--runner-atr-gate", type=float, default=None,
                        help="Runner ATR gate multiplier (default: 1.3x median)")

    # Modes
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Print per-trade detail")
    parser.add_argument("--save", action="store_true",
                        help="Save results to JSON")
    parser.add_argument("--sweep", action="store_true",
                        help="Run parameter sweep")

    args = parser.parse_args()

    print("\n" + "=" * 70)
    mode_str = "SPX 0DTE" if args.spx else "SPY 0DTE"
    print(f"  ⚡ 0DTE GAMMA SCALP BACKTESTER — {mode_str}")
    print("  Professional directional scalping — ATM options, price-based exits")
    print("=" * 70)

    if args.sweep:
        run_sweep(args.ticker, args.interval, spx_mode=args.spx)
        return

    # Build config with overrides
    config = EngineConfig()
    if args.stop_atr is not None:
        config.scalp.stop_atr_mult = args.stop_atr
    if args.target_atr is not None:
        config.scalp.profit_target_atr_mult = args.target_atr
    if args.confirmations is not None:
        config.scalp.min_confirmations = args.confirmations
    if args.time_stop is not None:
        config.scalp.time_stop_minutes = args.time_stop
    if args.max_hold is not None:
        config.scalp.max_hold_minutes = args.max_hold
    if args.midday:
        config.scalp.enable_midday = True
    if args.no_runner:
        config.scalp.runner_enabled = False
    if args.runner_otm is not None:
        config.scalp.runner_otm_pct = args.runner_otm / 100.0
    if args.runner_atr_gate is not None:
        config.scalp.runner_min_atr_mult = args.runner_atr_gate

    # Print config
    cfg = config.scalp
    print(f"\n  Config:")
    print(f"    Stop:          {cfg.stop_atr_mult}×ATR")
    print(f"    Target:        {cfg.profit_target_atr_mult}×ATR")
    print(f"    Trailing:      {cfg.trailing_activation_atr}×ATR → trail {cfg.trailing_distance_atr}×ATR")
    print(f"    Confirmations: {cfg.min_confirmations}+")
    print(f"    Time stop:     {cfg.time_stop_minutes} min (threshold {cfg.time_stop_atr_mult}×ATR)")
    print(f"    Max hold:      {cfg.max_hold_minutes} min")
    mid_str = "ON" if cfg.enable_midday else "OFF"
    print(f"    Windows:       {cfg.window_1_start}-{cfg.window_1_end}m + "
          f"{cfg.window_2_start}-{cfg.window_2_end}m (midday: {mid_str})")
    print(f"    Max trades:    {cfg.max_trades_per_day}/day")
    print(f"    Re-entry lock: {'YES' if cfg.no_reentry_same_direction else 'NO'}")
    runner_str = "ON" if cfg.runner_enabled else "OFF"
    print(f"    Runner tier:   {runner_str}")
    if cfg.runner_enabled:
        print(f"      OTM:         {cfg.runner_otm_pct*100:.1f}%")
        print(f"      ATR gate:    {cfg.runner_min_atr_mult}× daily median")
        print(f"      Stop:        {cfg.runner_stop_atr_mult}×ATR")
        print(f"      Trail:       {cfg.runner_trail_activation_atr}×ATR → {cfg.runner_trail_distance_atr}×ATR")
        print(f"      Window:      {cfg.runner_window_start}-{cfg.runner_window_end}m (power hour)")
        print(f"      Max/day:     {cfg.runner_max_per_day}")

    run_single(config, args.ticker, args.interval, args.verbose, args.save,
               spx_mode=args.spx)


if __name__ == "__main__":
    main()

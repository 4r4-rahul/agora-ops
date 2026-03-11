#!/usr/bin/env python3
"""
Runner Tier Parameter Sweep
==============================
Sweeps OTM distance, ATR gate, stop width, and trail settings
to find optimal runner configuration alongside the proven scalp tier.

The scalp tier is LOCKED (PF=1.30, +$3,613). We only vary the runner.
"""
import os, sys, time
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from trading_engine.config import EngineConfig
from trading_engine.data.scalp_backtester import ScalpBacktester


def load_data():
    path = os.path.join("data", "intraday", "SPY_ibkr_1m_180d.csv")
    df = pd.read_csv(path, parse_dates=["timestamp"], index_col="timestamp")
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, utc=True)
    return df


def run_sweep():
    df = load_data()
    print(f"  📊 Loaded {len(df):,} bars\n")

    # ── Sweep dimensions ─────────────────────────────────────
    otm_pcts = [0.003, 0.005, 0.008, 0.010, 0.015]       # 0.3% to 1.5% OTM
    atr_gates = [1.0, 1.2, 1.3, 1.5, 2.0]                 # ATR multiplier gate
    stop_atrs = [3.0, 4.0, 5.0, 7.0, 10.0]                # Runner stop width
    trail_combos = [                                        # (activation, distance)
        (2.0, 1.0),
        (3.0, 1.5),
        (3.0, 2.0),
        (5.0, 2.5),
        (5.0, 3.0),
    ]

    # Also test: runner disabled (baseline)
    total = len(otm_pcts) * len(atr_gates) * len(stop_atrs) * len(trail_combos) + 1
    print(f"  🔄 Sweeping {total} combinations...\n")

    results = []

    # ── Baseline: no runner ──────────────────────────────────
    config = EngineConfig()
    config.scalp.runner_enabled = False
    bt = ScalpBacktester(config=config, account_size=10_000.0, spx_mode=True)
    r = bt.run(df, ticker="SPY", interval="1m", verbose=False)
    results.append({
        "otm_pct": 0, "atr_gate": 0, "stop_atr": 0,
        "trail_act": 0, "trail_dist": 0,
        "total_trades": r.total_trades, "win_rate": r.win_rate,
        "pf": r.profit_factor, "total_pnl": r.total_pnl,
        "max_dd": r.max_drawdown_pct,
        "scalp_pnl": r.scalp_pnl, "runner_pnl": r.runner_pnl,
        "runner_trades": r.runner_trades, "runner_wins": r.runner_wins,
        "runner_biggest": r.runner_biggest_win,
        "label": "NO RUNNER (baseline)",
    })
    print(f"  [baseline] No runner → PF={r.profit_factor:.2f}, "
          f"P&L=${r.total_pnl:+,.0f}")

    done = 1
    start = time.time()

    for otm in otm_pcts:
        for gate in atr_gates:
            for stop in stop_atrs:
                for trail_act, trail_dist in trail_combos:
                    config = EngineConfig()
                    cfg = config.scalp
                    cfg.runner_enabled = True
                    cfg.runner_otm_pct = otm
                    cfg.runner_min_atr_mult = gate
                    cfg.runner_stop_atr_mult = stop
                    cfg.runner_trail_activation_atr = trail_act
                    cfg.runner_trail_distance_atr = trail_dist

                    bt = ScalpBacktester(config=config, account_size=10_000.0, spx_mode=True)
                    r = bt.run(df, ticker="SPY", interval="1m", verbose=False)

                    done += 1
                    results.append({
                        "otm_pct": otm, "atr_gate": gate, "stop_atr": stop,
                        "trail_act": trail_act, "trail_dist": trail_dist,
                        "total_trades": r.total_trades, "win_rate": r.win_rate,
                        "pf": r.profit_factor, "total_pnl": r.total_pnl,
                        "max_dd": r.max_drawdown_pct,
                        "scalp_pnl": r.scalp_pnl, "runner_pnl": r.runner_pnl,
                        "runner_trades": r.runner_trades, "runner_wins": r.runner_wins,
                        "runner_biggest": r.runner_biggest_win,
                        "label": f"otm={otm*100:.1f}% gate={gate} stop={stop} "
                                 f"trail={trail_act}/{trail_dist}",
                    })

                    elapsed = time.time() - start
                    eta = elapsed / done * (total - done) if done > 0 else 0
                    sys.stdout.write(
                        f"\r  [{done}/{total}] "
                        f"otm={otm*100:.1f}% gate={gate:.1f} stop={stop:.0f} "
                        f"trail={trail_act:.0f}/{trail_dist:.1f} → "
                        f"R:{r.runner_trades}t/{r.runner_wins}w "
                        f"R_PnL=${r.runner_pnl:+,.0f} "
                        f"Total=${r.total_pnl:+,.0f} "
                        f"PF={r.profit_factor:.2f} "
                        f"[ETA {eta:.0f}s]     "
                    )
                    sys.stdout.flush()

    elapsed = time.time() - start
    print(f"\n\n  ✅ Sweep complete in {elapsed:.0f}s\n")

    # ── Sort by total P&L (runner added value) ───────────────
    results.sort(key=lambda x: x["total_pnl"], reverse=True)

    # ── Print top results ────────────────────────────────────
    print("=" * 100)
    print("  🏆 TOP 20 RUNNER CONFIGURATIONS (sorted by total P&L)")
    print("=" * 100)
    print(f"  {'OTM%':>5} {'Gate':>5} {'Stop':>5} {'Trail':>9} "
          f"{'Trades':>6} {'WR':>5} {'PF':>5} "
          f"{'Total P&L':>10} {'Scalp':>8} {'Runner':>8} "
          f"{'R_Trades':>8} {'R_Win':>5} {'R_Best':>8} {'MaxDD':>6}")
    print(f"  {'─' * 96}")

    for r in results[:20]:
        pc = "\033[32m" if r["total_pnl"] >= 0 else "\033[31m"
        rc = "\033[32m" if r["runner_pnl"] >= 0 else "\033[31m"
        if r["otm_pct"] == 0:
            print(f"  {'---':>5} {'---':>5} {'---':>5} {'---':>9} "
                  f"{r['total_trades']:>6} {r['win_rate']:>4.1f}% {r['pf']:>5.2f} "
                  f"{pc}${r['total_pnl']:>+9,.0f}\033[0m "
                  f"${r['scalp_pnl']:>+7,.0f} ${r['runner_pnl']:>+7,.0f} "
                  f"{'---':>8} {'---':>5} {'---':>8} "
                  f"{r['max_dd']:>5.1%}  ← BASELINE")
        else:
            print(f"  {r['otm_pct']*100:>4.1f}% {r['atr_gate']:>5.1f} "
                  f"{r['stop_atr']:>5.1f} "
                  f"{r['trail_act']:.0f}/{r['trail_dist']:.1f}     "
                  f"{r['total_trades']:>6} {r['win_rate']:>4.1f}% {r['pf']:>5.2f} "
                  f"{pc}${r['total_pnl']:>+9,.0f}\033[0m "
                  f"${r['scalp_pnl']:>+7,.0f} "
                  f"{rc}${r['runner_pnl']:>+7,.0f}\033[0m "
                  f"{r['runner_trades']:>8} {r['runner_wins']:>5} "
                  f"${r['runner_biggest']:>+7,.0f} "
                  f"{r['max_dd']:>5.1%}")

    # ── Print worst results ──────────────────────────────────
    print(f"\n  {'─' * 96}")
    print("  ⚠️  BOTTOM 5 (worst runners)")
    print(f"  {'─' * 96}")
    for r in results[-5:]:
        if r["otm_pct"] == 0:
            continue
        pc = "\033[32m" if r["total_pnl"] >= 0 else "\033[31m"
        rc = "\033[32m" if r["runner_pnl"] >= 0 else "\033[31m"
        print(f"  {r['otm_pct']*100:>4.1f}% {r['atr_gate']:>5.1f} "
              f"{r['stop_atr']:>5.1f} "
              f"{r['trail_act']:.0f}/{r['trail_dist']:.1f}     "
              f"{r['total_trades']:>6} {r['win_rate']:>4.1f}% {r['pf']:>5.2f} "
              f"{pc}${r['total_pnl']:>+9,.0f}\033[0m "
              f"${r['scalp_pnl']:>+7,.0f} "
              f"{rc}${r['runner_pnl']:>+7,.0f}\033[0m "
              f"{r['runner_trades']:>8} {r['runner_wins']:>5} "
              f"${r['runner_biggest']:>+7,.0f} "
              f"{r['max_dd']:>5.1%}")

    # ── Analysis: Runner value-add ───────────────────────────
    baseline_pnl = results[-1]["total_pnl"] if results[-1]["otm_pct"] == 0 else 0
    for r in results:
        if r["otm_pct"] == 0:
            baseline_pnl = r["total_pnl"]
            break

    positive_runners = [r for r in results if r["runner_pnl"] > 0 and r["otm_pct"] > 0]
    negative_runners = [r for r in results if r["runner_pnl"] < 0 and r["otm_pct"] > 0]
    beating_baseline = [r for r in results if r["total_pnl"] > baseline_pnl and r["otm_pct"] > 0]

    print(f"\n  {'=' * 60}")
    print(f"  RUNNER ANALYSIS")
    print(f"  {'=' * 60}")
    print(f"  Baseline (scalp only): ${baseline_pnl:+,.0f}")
    print(f"  Runner configs tested: {total - 1}")
    print(f"  Profitable runners:    {len(positive_runners)} ({len(positive_runners)/(total-1)*100:.0f}%)")
    print(f"  Beat baseline:         {len(beating_baseline)} ({len(beating_baseline)/(total-1)*100:.0f}%)")

    if beating_baseline:
        best = beating_baseline[0]
        added = best["total_pnl"] - baseline_pnl
        print(f"\n  🏆 BEST RUNNER CONFIG:")
        print(f"     OTM:        {best['otm_pct']*100:.1f}%")
        print(f"     ATR gate:   {best['atr_gate']}× median")
        print(f"     Stop:       {best['stop_atr']}×ATR")
        print(f"     Trail:      {best['trail_act']}×ATR activate → {best['trail_dist']}×ATR distance")
        print(f"     Runner P&L: ${best['runner_pnl']:+,.0f} ({best['runner_trades']} trades, {best['runner_wins']} wins)")
        print(f"     Biggest:    ${best['runner_biggest']:+,.0f}")
        print(f"     Total P&L:  ${best['total_pnl']:+,.0f} (+${added:,.0f} over baseline)")
        print(f"     PF:         {best['pf']:.2f}")

    # ── Dimensional analysis ─────────────────────────────────
    print(f"\n  {'─' * 60}")
    print(f"  DIMENSIONAL AVERAGES (runner P&L by parameter)")
    print(f"  {'─' * 60}")

    runner_results = [r for r in results if r["otm_pct"] > 0]
    rdf = pd.DataFrame(runner_results)

    print(f"\n  By OTM Distance:")
    for otm in otm_pcts:
        subset = rdf[rdf["otm_pct"] == otm]
        avg_rpnl = subset["runner_pnl"].mean()
        avg_rt = subset["runner_trades"].mean()
        pct_pos = (subset["runner_pnl"] > 0).mean() * 100
        c = "\033[32m" if avg_rpnl >= 0 else "\033[31m"
        print(f"    {otm*100:>4.1f}% OTM → avg runner P&L: {c}${avg_rpnl:>+7,.0f}\033[0m "
              f"| avg trades: {avg_rt:.1f} | {pct_pos:.0f}% positive")

    print(f"\n  By ATR Gate:")
    for gate in atr_gates:
        subset = rdf[rdf["atr_gate"] == gate]
        avg_rpnl = subset["runner_pnl"].mean()
        avg_rt = subset["runner_trades"].mean()
        pct_pos = (subset["runner_pnl"] > 0).mean() * 100
        c = "\033[32m" if avg_rpnl >= 0 else "\033[31m"
        print(f"    {gate:>4.1f}× gate → avg runner P&L: {c}${avg_rpnl:>+7,.0f}\033[0m "
              f"| avg trades: {avg_rt:.1f} | {pct_pos:.0f}% positive")

    print(f"\n  By Stop Width:")
    for stop in stop_atrs:
        subset = rdf[rdf["stop_atr"] == stop]
        avg_rpnl = subset["runner_pnl"].mean()
        avg_rt = subset["runner_trades"].mean()
        pct_pos = (subset["runner_pnl"] > 0).mean() * 100
        c = "\033[32m" if avg_rpnl >= 0 else "\033[31m"
        print(f"    {stop:>4.1f}× stop → avg runner P&L: {c}${avg_rpnl:>+7,.0f}\033[0m "
              f"| avg trades: {avg_rt:.1f} | {pct_pos:.0f}% positive")

    print(f"\n  By Trail Combo:")
    for trail_act, trail_dist in trail_combos:
        subset = rdf[(rdf["trail_act"] == trail_act) & (rdf["trail_dist"] == trail_dist)]
        avg_rpnl = subset["runner_pnl"].mean()
        avg_rt = subset["runner_trades"].mean()
        pct_pos = (subset["runner_pnl"] > 0).mean() * 100
        c = "\033[32m" if avg_rpnl >= 0 else "\033[31m"
        print(f"    {trail_act:.0f}/{trail_dist:.1f} trail → avg runner P&L: {c}${avg_rpnl:>+7,.0f}\033[0m "
              f"| avg trades: {avg_rt:.1f} | {pct_pos:.0f}% positive")

    print()


if __name__ == "__main__":
    run_sweep()

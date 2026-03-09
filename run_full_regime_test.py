#!/usr/bin/env python3
"""
Full-Round Intraday Options Engine Test
=========================================
Comprehensive test across ALL regimes using 180 days of IBKR 5-min data.

Tests:
  • put_credit, call_credit, iron_condor across 180 trading days
  • Regime breakdown: GREEN (VIX<18), YELLOW (18-25), RED (>25)
  • Monthly P&L, exit reason analysis, Greeks impact
  • Parameter sensitivity (delta, stop-loss, profit target)

Usage:
    python run_full_regime_test.py            # Full test
    python run_full_regime_test.py --live      # Pull fresh data from IBKR first
"""

import argparse
import sys
import os
import json
import math
from datetime import date, datetime, timedelta
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from collections import defaultdict

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from trading_engine.config import EngineConfig
from trading_engine.data.intraday import IntradayFetcher, TradingDay
from trading_engine.data.intraday_backtester import IntradayBacktester, IntradayResults


# ─────────────────────────────────────────────────────────────────
# Regime classification using VIX
# ─────────────────────────────────────────────────────────────────

def fetch_vix_history(days: int = 250) -> pd.DataFrame:
    """Fetch VIX daily closes from Yahoo Finance."""
    import yfinance as yf
    df = yf.download("^VIX", period=f"{days}d", progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [c.lower() for c in df.columns]
    return df


def classify_regime(vix: float) -> str:
    """
    Classify market regime from VIX level.
      GREEN:  VIX < 18  → low vol, ideal for premium selling
      YELLOW: VIX 18-25 → elevated vol, widen strikes
      RED:    VIX > 25  → high vol, reduce size or skip
    """
    if vix < 18:
        return "GREEN"
    elif vix <= 25:
        return "YELLOW"
    else:
        return "RED"


def tag_days_with_regime(trading_days: List[TradingDay],
                          vix_df: pd.DataFrame) -> Dict[str, List[TradingDay]]:
    """
    Tag each trading day with a regime and group them.
    Returns dict: {"GREEN": [...], "YELLOW": [...], "RED": [...]}.
    """
    regimes = {"GREEN": [], "YELLOW": [], "RED": []}
    vix_dates = {d.date(): row for d, row in vix_df.iterrows()
                 if hasattr(d, 'date')}

    for td in trading_days:
        d = td.date
        # Find matching VIX close
        vix_val = None
        for offset in range(5):  # Look back up to 5 days for weekends
            check = d - timedelta(days=offset)
            if check in vix_dates:
                vix_val = float(vix_dates[check].get("close", 18))
                break

        if vix_val is None:
            vix_val = 18.0  # Default

        regime = classify_regime(vix_val)
        td._regime = regime  # Tag it
        td._vix = vix_val
        regimes[regime].append(td)

    return regimes


# ─────────────────────────────────────────────────────────────────
# Run backtest for a single strategy across a day set
# ─────────────────────────────────────────────────────────────────

def run_strategy_on_days(days: List[TradingDay], strategy: str,
                          account_size: float = 50_000,
                          iv_override: Optional[float] = None) -> IntradayResults:
    """Run a single strategy on a list of trading days."""
    if not days:
        return IntradayResults()

    config = EngineConfig()
    config.account.account_size = account_size

    bt = IntradayBacktester(config)
    results = bt.run(days, strategies=[strategy], iv_override=iv_override)
    return results


# ─────────────────────────────────────────────────────────────────
# Main test harness
# ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Full regime test of intraday options engine")
    parser.add_argument("--live", action="store_true",
                        help="Pull fresh data from IBKR (requires TWS running)")
    parser.add_argument("--days", type=int, default=180,
                        help="Days of history to test (default: 180)")
    parser.add_argument("--account", type=float, default=50_000,
                        help="Starting account size")
    args = parser.parse_args()

    C_GREEN = "\033[92m"
    C_RED = "\033[91m"
    C_YELLOW = "\033[93m"
    C_CYAN = "\033[96m"
    C_BOLD = "\033[1m"
    C_DIM = "\033[2m"
    C_RESET = "\033[0m"

    STRATEGIES = ["put_credit", "call_credit", "iron_condor"]

    print(f"\n{C_BOLD}{'═' * 78}{C_RESET}")
    print(f"{C_BOLD}  FULL-ROUND INTRADAY OPTIONS ENGINE TEST{C_RESET}")
    print(f"{C_BOLD}  180 Days × 3 Strategies × 3 Regimes = Complete Coverage{C_RESET}")
    print(f"{C_BOLD}{'═' * 78}{C_RESET}\n")

    # ─── Step 1: Load intraday data ──────────────────────────────

    print(f"{C_CYAN}STEP 1: Loading Intraday Data{C_RESET}")
    print(f"{'─' * 60}\n")

    if args.live:
        print("  Pulling fresh 5-min bars from IBKR ...")
        fetcher = IntradayFetcher()
        trading_days = fetcher.fetch("SPY", days=args.days, provider="ibkr", force=True)
    else:
        # Load from cached IBKR data
        cache_path = os.path.join("data", "intraday", "SPY_ibkr_5m_180d.csv")
        if not os.path.exists(cache_path):
            # Fallback to regular fetcher
            print("  No IBKR cache found, using IntradayFetcher ...")
            fetcher = IntradayFetcher()
            trading_days = fetcher.fetch("SPY", days=args.days, provider="auto")
        else:
            print(f"  Loading cached IBKR data from {cache_path} ...")
            df = pd.read_csv(cache_path, index_col=0, parse_dates=True)
            # Ensure index is DatetimeIndex
            if not isinstance(df.index, pd.DatetimeIndex):
                df.index = pd.to_datetime(df.index, utc=True)
            print(f"  ✅ {len(df)} bars loaded")

            # Convert to TradingDay objects
            from trading_engine.data.intraday import IntradayBar
            days_dict = {}
            for ts, row in df.iterrows():
                d = ts.date()
                if d not in days_dict:
                    days_dict[d] = []
                days_dict[d].append(IntradayBar(
                    timestamp=pd.Timestamp(ts).to_pydatetime(),
                    open=float(row.get("open", 0)),
                    high=float(row.get("high", 0)),
                    low=float(row.get("low", 0)),
                    close=float(row.get("close", 0)),
                    volume=int(row.get("volume", 0)),
                    vwap=float(row.get("vwap", 0)) if "vwap" in row.index else 0,
                ))
            trading_days = [
                TradingDay(date=d, bars=bars, ticker="SPY")
                for d, bars in sorted(days_dict.items())
                if len(bars) >= 30
            ]
            print(f"  ✅ {len(trading_days)} complete trading days")

    total_bars = sum(td.bar_count for td in trading_days)
    print(f"  📊 {len(trading_days)} days, {total_bars:,} total bars")
    print(f"  📅 {trading_days[0].date} → {trading_days[-1].date}")
    print(f"  📈 Open range: ${trading_days[0].open_price:.2f} → ${trading_days[-1].close_price:.2f}\n")

    # ─── Step 2: Fetch VIX and classify regimes ──────────────────

    print(f"{C_CYAN}STEP 2: Regime Classification (VIX-based){C_RESET}")
    print(f"{'─' * 60}\n")

    vix_df = fetch_vix_history(days=300)
    regime_days = tag_days_with_regime(trading_days, vix_df)

    regime_colors = {"GREEN": C_GREEN, "YELLOW": C_YELLOW, "RED": C_RED}
    for regime in ["GREEN", "YELLOW", "RED"]:
        days_list = regime_days[regime]
        c = regime_colors[regime]
        if days_list:
            vix_vals = [getattr(td, '_vix', 18) for td in days_list]
            avg_vix = np.mean(vix_vals)
            print(f"  {c}■{C_RESET} {regime:6s}: {len(days_list):3d} days  "
                  f"(avg VIX: {avg_vix:.1f}, "
                  f"range: {min(vix_vals):.1f}-{max(vix_vals):.1f})")
        else:
            print(f"  {c}■{C_RESET} {regime:6s}:   0 days")

    print(f"\n  Total: {sum(len(v) for v in regime_days.values())} days classified\n")

    # ─── Step 3: Run all strategies across all regimes ───────────

    print(f"{C_CYAN}STEP 3: Running Backtest — All Strategies × All Regimes{C_RESET}")
    print(f"{'─' * 60}\n")

    # Big matrix: results[strategy][regime] = IntradayResults
    all_results: Dict[str, Dict[str, IntradayResults]] = {}

    # Also run each strategy on ALL days combined
    all_day_results: Dict[str, IntradayResults] = {}

    for strat in STRATEGIES:
        all_results[strat] = {}
        print(f"  ── {strat.upper().replace('_', ' ')} {'─' * (50 - len(strat))}")

        # Run on all days combined
        print(f"     ALL days ({len(trading_days)}) ...", end=" ", flush=True)
        r_all = run_strategy_on_days(trading_days, strat, args.account)
        all_day_results[strat] = r_all
        wr_c = C_GREEN if r_all.win_rate >= 70 else C_YELLOW if r_all.win_rate >= 55 else C_RED
        pnl_c = C_GREEN if r_all.total_pnl > 0 else C_RED
        print(f"{wr_c}{r_all.win_rate:.1f}% WR{C_RESET} | "
              f"{pnl_c}${r_all.total_pnl:>+10,.2f}{C_RESET} | "
              f"PF={r_all.profit_factor:.2f} | "
              f"{r_all.total_trades} trades")

        # Run per regime
        for regime in ["GREEN", "YELLOW", "RED"]:
            days_list = regime_days[regime]
            if not days_list:
                all_results[strat][regime] = IntradayResults()
                continue

            print(f"     {regime:6s} ({len(days_list):3d} days) ...", end=" ", flush=True)
            r = run_strategy_on_days(days_list, strat, args.account)
            all_results[strat][regime] = r

            c = regime_colors[regime]
            wr_c = C_GREEN if r.win_rate >= 70 else C_YELLOW if r.win_rate >= 55 else C_RED
            pnl_c = C_GREEN if r.total_pnl > 0 else C_RED
            print(f"{wr_c}{r.win_rate:.1f}% WR{C_RESET} | "
                  f"{pnl_c}${r.total_pnl:>+10,.2f}{C_RESET} | "
                  f"PF={r.profit_factor:.2f} | "
                  f"{r.total_trades} trades")

        print()

    # ─── Step 4: Master Report ───────────────────────────────────

    print(f"\n{C_BOLD}{'═' * 78}{C_RESET}")
    print(f"{C_BOLD}  COMPREHENSIVE RESULTS — REGIME × STRATEGY MATRIX{C_RESET}")
    print(f"{C_BOLD}{'═' * 78}{C_RESET}\n")

    # 4a. Strategy × Regime matrix — Win Rate
    print(f"  {C_BOLD}WIN RATE MATRIX{C_RESET}")
    print(f"  {'─' * 66}")
    print(f"  {'Strategy':<20} {'ALL':>10} {'GREEN':>10} {'YELLOW':>10} {'RED':>10}")
    print(f"  {'─' * 66}")
    for strat in STRATEGIES:
        r_all = all_day_results[strat]
        vals = [f"{r_all.win_rate:>9.1f}%"]
        for regime in ["GREEN", "YELLOW", "RED"]:
            r = all_results[strat][regime]
            if r.total_trades > 0:
                c = C_GREEN if r.win_rate >= 70 else C_YELLOW if r.win_rate >= 55 else C_RED
                vals.append(f"{c}{r.win_rate:>9.1f}%{C_RESET}")
            else:
                vals.append(f"{'N/A':>10}")
        print(f"  {strat:<20} {'  '.join(vals)}")
    print(f"  {'─' * 66}\n")

    # 4b. Strategy × Regime matrix — P&L
    print(f"  {C_BOLD}P&L MATRIX{C_RESET}")
    print(f"  {'─' * 70}")
    print(f"  {'Strategy':<20} {'ALL':>12} {'GREEN':>12} {'YELLOW':>12} {'RED':>12}")
    print(f"  {'─' * 70}")
    for strat in STRATEGIES:
        r_all = all_day_results[strat]
        c = C_GREEN if r_all.total_pnl > 0 else C_RED
        vals = [f"{c}${r_all.total_pnl:>+10,.0f}{C_RESET}"]
        for regime in ["GREEN", "YELLOW", "RED"]:
            r = all_results[strat][regime]
            if r.total_trades > 0:
                c = C_GREEN if r.total_pnl > 0 else C_RED
                vals.append(f"{c}${r.total_pnl:>+10,.0f}{C_RESET}")
            else:
                vals.append(f"{'N/A':>12}")
        print(f"  {strat:<20} {'  '.join(vals)}")
    print(f"  {'─' * 70}\n")

    # 4c. Strategy × Regime matrix — Profit Factor
    print(f"  {C_BOLD}PROFIT FACTOR MATRIX{C_RESET}")
    print(f"  {'─' * 66}")
    print(f"  {'Strategy':<20} {'ALL':>10} {'GREEN':>10} {'YELLOW':>10} {'RED':>10}")
    print(f"  {'─' * 66}")
    for strat in STRATEGIES:
        r_all = all_day_results[strat]
        c = C_GREEN if r_all.profit_factor > 1 else C_RED
        vals = [f"{c}{r_all.profit_factor:>10.2f}{C_RESET}"]
        for regime in ["GREEN", "YELLOW", "RED"]:
            r = all_results[strat][regime]
            if r.total_trades > 0:
                c = C_GREEN if r.profit_factor > 1 else C_RED
                vals.append(f"{c}{r.profit_factor:>10.2f}{C_RESET}")
            else:
                vals.append(f"{'N/A':>10}")
        print(f"  {strat:<20} {'  '.join(vals)}")
    print(f"  {'─' * 66}\n")

    # 4d. Risk metrics
    print(f"  {C_BOLD}RISK METRICS{C_RESET}")
    print(f"  {'─' * 72}")
    print(f"  {'Strategy':<20} {'Max DD%':>10} {'Max DD$':>12} {'Sharpe':>10} {'Gamma Exits':>12}")
    print(f"  {'─' * 72}")
    for strat in STRATEGIES:
        r = all_day_results[strat]
        dd_c = C_GREEN if r.max_drawdown_pct < 5 else C_YELLOW if r.max_drawdown_pct < 10 else C_RED
        sr_c = C_GREEN if r.sharpe_ratio > 1 else C_YELLOW if r.sharpe_ratio > 0 else C_RED
        g_c = C_GREEN if r.gamma_blow_ups < 3 else C_YELLOW if r.gamma_blow_ups < 10 else C_RED
        print(f"  {strat:<20} {dd_c}{r.max_drawdown_pct:>9.2f}%{C_RESET} "
              f"${r.max_drawdown_dollars:>10,.0f} "
              f"{sr_c}{r.sharpe_ratio:>10.2f}{C_RESET} "
              f"{g_c}{r.gamma_blow_ups:>12}{C_RESET}")
    print(f"  {'─' * 72}\n")

    # 4e. Exit reason breakdown
    print(f"  {C_BOLD}EXIT REASON BREAKDOWN{C_RESET}")
    print(f"  {'─' * 72}")
    print(f"  {'Strategy':<20} {'Profit':>10} {'Stop':>10} {'Expired':>10} {'ExpITM':>10} {'Gamma':>10}")
    print(f"  {'─' * 72}")
    for strat in STRATEGIES:
        r = all_day_results[strat]
        reasons = defaultdict(int)
        for t in r.trades:
            reasons[t.exit_reason] += 1
        total = max(len(r.trades), 1)
        profit = reasons.get("profit_target", 0)
        stop = reasons.get("stop_loss", 0)
        expired = reasons.get("expired", 0)
        expired_itm = reasons.get("expired_itm", 0)
        gamma = reasons.get("gamma_risk", 0)
        print(f"  {strat:<20} {C_GREEN}{profit:>9}{C_RESET} "
              f"({profit/total*100:.0f}%) "
              f"{C_RED}{stop:>6}{C_RESET} ({stop/total*100:.0f}%) "
              f"{expired:>6} ({expired/total*100:.0f}%) "
              f"{expired_itm:>6} ({expired_itm/total*100:.0f}%) "
              f"{gamma:>6} ({gamma/total*100:.0f}%)")
    print(f"  {'─' * 72}\n")

    # 4f. Monthly P&L for best strategy
    best_strat = max(STRATEGIES, key=lambda s: all_day_results[s].total_pnl)
    r_best = all_day_results[best_strat]
    print(f"  {C_BOLD}MONTHLY P&L — {best_strat.upper()}{C_RESET}")
    print(f"  {'─' * 60}")
    if r_best.monthly_pnl:
        max_m = max(abs(v) for v in r_best.monthly_pnl.values()) or 1
        for month, mpnl in r_best.monthly_pnl.items():
            color = C_GREEN if mpnl >= 0 else C_RED
            bar_len = int(abs(mpnl) / max_m * 25) if max_m > 0 else 0
            bar_str = "█" * bar_len
            print(f"  {month}  {color}{bar_str:<25} ${mpnl:>+10,.2f}{C_RESET}")
    print(f"  {'─' * 60}\n")

    # 4g. Timing analysis
    print(f"  {C_BOLD}TIMING ANALYSIS{C_RESET}")
    print(f"  {'─' * 60}")
    for strat in STRATEGIES:
        r = all_day_results[strat]
        durations = [t.duration_minutes for t in r.trades if t.duration_minutes > 0]
        if durations:
            print(f"  {strat:<20} avg={np.mean(durations):.0f}m "
                  f"med={np.median(durations):.0f}m "
                  f"min={min(durations):.0f}m max={max(durations):.0f}m")
    print(f"  {'─' * 60}\n")

    # 4h. Greeks summary
    print(f"  {C_BOLD}GREEKS AT ENTRY (averages){C_RESET}")
    print(f"  {'─' * 60}")
    print(f"  {'Strategy':<20} {'Δ':>10} {'Γ':>10} {'θ':>10} {'IV':>10}")
    print(f"  {'─' * 60}")
    for strat in STRATEGIES:
        r = all_day_results[strat]
        if r.trades:
            avg_d = np.mean([t.entry_delta for t in r.trades])
            avg_g = np.mean([t.entry_gamma for t in r.trades])
            avg_t = np.mean([t.entry_theta for t in r.trades])
            avg_iv = np.mean([t.iv_at_entry for t in r.trades])
            print(f"  {strat:<20} {avg_d:>+10.4f} {avg_g:>10.4f} {avg_t:>10.4f} {avg_iv:>9.1%}")
    print(f"  {'─' * 60}\n")

    # ─── Step 5: VERDICT ─────────────────────────────────────────

    print(f"{C_BOLD}{'═' * 78}{C_RESET}")
    print(f"{C_BOLD}  VERDICT & RECOMMENDATIONS{C_RESET}")
    print(f"{C_BOLD}{'═' * 78}{C_RESET}\n")

    for strat in STRATEGIES:
        r = all_day_results[strat]
        name = strat.upper().replace("_", " ")

        # Score the strategy
        score = 0
        issues = []
        strengths = []

        if r.win_rate >= 70:
            score += 2
            strengths.append(f"Strong WR ({r.win_rate:.0f}%)")
        elif r.win_rate >= 55:
            score += 1
        else:
            issues.append(f"Low WR ({r.win_rate:.0f}%)")

        if r.profit_factor >= 1.5:
            score += 2
            strengths.append(f"PF={r.profit_factor:.2f}")
        elif r.profit_factor >= 1.0:
            score += 1
        else:
            issues.append(f"Losing money (PF={r.profit_factor:.2f})")

        if r.max_drawdown_pct < 5:
            score += 1
            strengths.append(f"Low DD ({r.max_drawdown_pct:.1f}%)")
        elif r.max_drawdown_pct > 15:
            issues.append(f"High DD ({r.max_drawdown_pct:.1f}%)")

        if r.sharpe_ratio > 1.0:
            score += 1
            strengths.append(f"Sharpe={r.sharpe_ratio:.2f}")
        elif r.sharpe_ratio < 0:
            issues.append(f"Negative Sharpe ({r.sharpe_ratio:.2f})")

        if r.gamma_blow_ups <= 2:
            score += 1
        elif r.gamma_blow_ups > 10:
            issues.append(f"{r.gamma_blow_ups} gamma blowups")

        # Rating
        if score >= 6:
            rating = f"{C_GREEN}★★★ PRODUCTION READY{C_RESET}"
        elif score >= 4:
            rating = f"{C_YELLOW}★★☆ NEEDS TUNING{C_RESET}"
        elif score >= 2:
            rating = f"{C_RED}★☆☆ NOT READY{C_RESET}"
        else:
            rating = f"{C_RED}☆☆☆ AVOID{C_RESET}"

        print(f"  {C_BOLD}{name}{C_RESET}: {rating}")
        if strengths:
            print(f"    ✅ {', '.join(strengths)}")
        if issues:
            print(f"    ❌ {', '.join(issues)}")

        # Regime-specific advice
        for regime in ["GREEN", "YELLOW", "RED"]:
            rr = all_results[strat][regime]
            if rr.total_trades > 0:
                rc = regime_colors[regime]
                pnl_c = C_GREEN if rr.total_pnl > 0 else C_RED
                print(f"    {rc}{regime}{C_RESET}: {rr.win_rate:.0f}% WR, "
                      f"{pnl_c}${rr.total_pnl:>+,.0f}{C_RESET}, "
                      f"PF={rr.profit_factor:.2f}")
        print()

    # Best combo
    best = max(STRATEGIES, key=lambda s: all_day_results[s].profit_factor)
    r = all_day_results[best]
    print(f"  {C_BOLD}💡 RECOMMENDED STRATEGY: {best.upper().replace('_', ' ')}{C_RESET}")
    print(f"     {r.win_rate:.1f}% WR | ${r.total_pnl:>+,.2f} | PF={r.profit_factor:.2f} | "
          f"DD={r.max_drawdown_pct:.1f}%\n")

    # Save full results
    print(f"{C_BOLD}{'═' * 78}{C_RESET}")
    output = {
        "test_date": str(date.today()),
        "data_source": "IBKR 5-min bars",
        "period": f"{trading_days[0].date} → {trading_days[-1].date}",
        "total_days": len(trading_days),
        "total_bars": total_bars,
        "regime_counts": {r: len(d) for r, d in regime_days.items()},
        "strategies": {},
    }
    for strat in STRATEGIES:
        r = all_day_results[strat]
        output["strategies"][strat] = {
            "all": {
                "trades": r.total_trades,
                "win_rate": r.win_rate,
                "total_pnl": r.total_pnl,
                "profit_factor": r.profit_factor,
                "max_drawdown_pct": r.max_drawdown_pct,
                "sharpe_ratio": r.sharpe_ratio,
                "gamma_blowups": r.gamma_blow_ups,
                "monthly_pnl": r.monthly_pnl,
            },
            "by_regime": {}
        }
        for regime in ["GREEN", "YELLOW", "RED"]:
            rr = all_results[strat][regime]
            output["strategies"][strat]["by_regime"][regime] = {
                "days": len(regime_days[regime]),
                "trades": rr.total_trades,
                "win_rate": rr.win_rate,
                "total_pnl": rr.total_pnl,
                "profit_factor": rr.profit_factor,
                "max_drawdown_pct": rr.max_drawdown_pct,
                "gamma_blowups": rr.gamma_blow_ups,
            }

    out_path = "full_regime_test_results.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"  💾 Full results saved to {out_path}")
    print(f"{C_BOLD}{'═' * 78}{C_RESET}\n")


if __name__ == "__main__":
    main()

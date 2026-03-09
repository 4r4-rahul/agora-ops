#!/usr/bin/env python3
"""
Parameter Optimizer for Intraday Options Engine
=================================================
Sweeps delta, stop-loss, profit target, entry window, and width
across GREEN / YELLOW / RED regimes to find optimal parameters
for each market condition.

Usage:
    python optimize_params.py                  # Full sweep
    python optimize_params.py --quick          # Reduced grid (faster)
"""

import sys
import os
import json
import itertools
from datetime import date, timedelta
from collections import defaultdict
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from trading_engine.config import EngineConfig
from trading_engine.data.intraday import IntradayFetcher, IntradayBar, TradingDay
from trading_engine.data.intraday_backtester import IntradayBacktester, IntradayResults


# ─────────────────────────────────────────────────────────────────
# Parameter grid
# ─────────────────────────────────────────────────────────────────

@dataclass
class ParamSet:
    delta: float
    stop_mult: float
    profit_target: float
    entry_start_min: int   # Minutes after open
    entry_end_min: int     # Minutes after open
    width: float
    gamma_limit: float

    def label(self) -> str:
        return (f"Δ={self.delta:.2f} stop={self.stop_mult:.1f}x "
                f"TP={self.profit_target:.0%} w={self.width:.0f} "
                f"entry={self.entry_start_min}-{self.entry_end_min}m")


FULL_GRID = {
    "delta":          [0.08, 0.10, 0.12, 0.15, 0.18],
    "stop_mult":      [1.5, 2.0, 2.5, 3.0],
    "profit_target":  [0.30, 0.40, 0.50, 0.60, 0.75],
    "entry_window":   [(15, 60), (30, 60), (30, 90), (45, 90)],
    "width":          [1.0, 2.0],
    "gamma_limit":    [0.08, 0.10, 0.15],
}

QUICK_GRID = {
    "delta":          [0.08, 0.10, 0.12, 0.15],
    "stop_mult":      [1.5, 2.0, 3.0],
    "profit_target":  [0.40, 0.50, 0.75],
    "entry_window":   [(15, 60), (30, 90)],
    "width":          [1.0, 2.0],
    "gamma_limit":    [0.10],
}


def generate_param_sets(grid: dict) -> List[ParamSet]:
    combos = list(itertools.product(
        grid["delta"],
        grid["stop_mult"],
        grid["profit_target"],
        grid["entry_window"],
        grid["width"],
        grid["gamma_limit"],
    ))
    return [
        ParamSet(
            delta=c[0], stop_mult=c[1], profit_target=c[2],
            entry_start_min=c[3][0], entry_end_min=c[3][1],
            width=c[4], gamma_limit=c[5],
        )
        for c in combos
    ]


# ─────────────────────────────────────────────────────────────────
# Fast backtester (stripped down for speed in optimization)
# ─────────────────────────────────────────────────────────────────

from trading_engine.black_scholes import (
    bs_put_price, bs_call_price, bs_delta, bs_gamma,
    bs_all_greeks, strike_at_delta,
)

def fast_backtest_day(td: TradingDay, strategy: str, params: ParamSet,
                      iv: float, balance: float = 50_000) -> Optional[dict]:
    """
    Fast single-day backtest with custom parameters.
    Returns dict with pnl, exit_reason, etc. or None if no trade.
    """
    r = 0.045
    is_spy = td.ticker.upper() in ("SPY", "SPDR")
    strike_round = 1.0 if is_spy else 5.0
    width = params.width

    # Find entry bar
    entry_bar = None
    entry_idx = 0
    for i, b in enumerate(td.bars):
        mins = b.minutes_since_open
        if params.entry_start_min <= mins <= params.entry_end_min:
            entry_bar = b
            entry_idx = i
            break

    if entry_bar is None:
        return None

    S = entry_bar.close
    T = entry_bar.time_to_close_years
    slippage = 0.15

    # Open position
    if strategy == "put_credit":
        K_short = round(strike_at_delta(S, T, r, iv, params.delta, "put") / strike_round) * strike_round
        K_long = K_short - width
        raw_credit = bs_put_price(S, K_short, T, r, iv) - bs_put_price(S, K_long, T, r, iv)
        credit = max(raw_credit * (1 - slippage), 0.01)
        spread_type = "put"

    elif strategy == "call_credit":
        K_short = round(strike_at_delta(S, T, r, iv, params.delta, "call") / strike_round) * strike_round
        K_long = K_short + width
        raw_credit = bs_call_price(S, K_short, T, r, iv) - bs_call_price(S, K_long, T, r, iv)
        credit = max(raw_credit * (1 - slippage), 0.01)
        spread_type = "call"

    elif strategy == "iron_condor":
        K_put_short = round(strike_at_delta(S, T, r, iv, params.delta, "put") / strike_round) * strike_round
        K_put_long = K_put_short - width
        K_call_short = round(strike_at_delta(S, T, r, iv, params.delta, "call") / strike_round) * strike_round
        K_call_long = K_call_short + width
        put_raw = bs_put_price(S, K_put_short, T, r, iv) - bs_put_price(S, K_put_long, T, r, iv)
        call_raw = bs_call_price(S, K_call_short, T, r, iv) - bs_call_price(S, K_call_long, T, r, iv)
        credit = max((put_raw + call_raw) * (1 - slippage), 0.01)
        spread_type = "iron_condor"
    else:
        return None

    max_loss_per = width - credit
    if max_loss_per <= 0:
        return None

    max_risk = balance * 0.03
    contracts = max(1, min(10, int(max_risk / (max_loss_per * 100))))
    legs = 4 if strategy == "iron_condor" else 2
    commission = 0.65 * contracts * legs * 2

    stop_price = credit * params.stop_mult
    target_price = credit * (1 - params.profit_target)

    exit_reason = "expired"
    exit_price = 0.0

    # Bar-by-bar
    for b in td.bars[entry_idx + 1:]:
        T_now = b.time_to_close_years
        S_now = b.close

        # Price spread
        if spread_type == "put":
            mark = max(bs_put_price(S_now, K_short, T_now, r, iv) -
                       bs_put_price(S_now, K_long, T_now, r, iv), 0)
        elif spread_type == "call":
            mark = max(bs_call_price(S_now, K_short, T_now, r, iv) -
                       bs_call_price(S_now, K_long, T_now, r, iv), 0)
        else:  # iron_condor
            pv = bs_put_price(S_now, K_put_short, T_now, r, iv) - \
                 bs_put_price(S_now, K_put_long, T_now, r, iv)
            cv = bs_call_price(S_now, K_call_short, T_now, r, iv) - \
                 bs_call_price(S_now, K_call_long, T_now, r, iv)
            mark = max(pv + cv, 0)

        # Stop loss
        if mark >= stop_price:
            exit_reason = "stop_loss"
            exit_price = min(mark, width)
            break

        # Profit target
        if mark <= target_price:
            exit_reason = "profit_target"
            exit_price = mark
            break

        # Gamma protection
        if spread_type == "put":
            gamma = abs(bs_gamma(S_now, K_short, T_now, r, iv))
        elif spread_type == "call":
            gamma = abs(bs_gamma(S_now, K_short, T_now, r, iv))
        else:
            gamma = abs(bs_gamma(S_now, K_put_short, T_now, r, iv)) + \
                    abs(bs_gamma(S_now, K_call_short, T_now, r, iv))

        if gamma > params.gamma_limit and mark > credit * 1.3:
            exit_reason = "gamma_risk"
            exit_price = mark
            break

    # Expired — intrinsic value
    if exit_reason == "expired":
        S_final = td.bars[-1].close
        if spread_type == "put":
            exit_price = max(min(max(K_short - S_final, 0) - max(K_long - S_final, 0), width), 0)
        elif spread_type == "call":
            exit_price = max(min(max(S_final - K_short, 0) - max(S_final - K_long, 0), width), 0)
        else:
            pi = max(K_put_short - S_final, 0) - max(K_put_long - S_final, 0)
            ci = max(S_final - K_call_short, 0) - max(S_final - K_call_long, 0)
            exit_price = min(max(pi, 0) + max(ci, 0), width)
        if exit_price > 0.01:
            exit_reason = "expired_itm"

    pnl_per = credit - exit_price
    close_slippage = exit_price * 0.05
    if pnl_per < 0:
        close_slippage = -close_slippage
    pnl_per -= close_slippage

    total_pnl = round(pnl_per * contracts * 100 - commission, 2)

    return {
        "pnl": total_pnl,
        "exit_reason": exit_reason,
        "credit": credit,
        "contracts": contracts,
        "win": total_pnl > 0,
    }


def estimate_iv(td: TradingDay) -> float:
    """Quick IV estimate from first 30 min."""
    import math
    n = min(6, len(td.bars) - 1)
    if n < 3:
        return 0.18
    returns = [(td.bars[i].close - td.bars[i-1].close) / td.bars[i-1].close
               for i in range(1, n + 1)]
    iv = np.std(returns) * math.sqrt(78) * math.sqrt(252)
    return max(0.08, min(0.80, round(iv, 4)))


# ─────────────────────────────────────────────────────────────────
# Regime classification (same as full test)
# ─────────────────────────────────────────────────────────────────

def fetch_vix_history(days: int = 300) -> pd.DataFrame:
    import yfinance as yf
    df = yf.download("^VIX", period=f"{days}d", progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [c.lower() for c in df.columns]
    return df


def classify_regime(vix: float) -> str:
    if vix < 18:
        return "GREEN"
    elif vix <= 25:
        return "YELLOW"
    else:
        return "RED"


def tag_days(trading_days, vix_df):
    regimes = {"GREEN": [], "YELLOW": [], "RED": []}
    vix_dates = {d.date(): row for d, row in vix_df.iterrows() if hasattr(d, 'date')}
    for td in trading_days:
        vix_val = None
        for offset in range(5):
            check = td.date - timedelta(days=offset)
            if check in vix_dates:
                vix_val = float(vix_dates[check].get("close", 18))
                break
        if vix_val is None:
            vix_val = 18.0
        td._vix = vix_val
        td._regime = classify_regime(vix_val)
        regimes[td._regime].append(td)
    return regimes


# ─────────────────────────────────────────────────────────────────
# The optimizer
# ─────────────────────────────────────────────────────────────────

def optimize_regime(days: List[TradingDay], regime: str,
                    strategies: List[str], param_sets: List[ParamSet],
                    verbose: bool = True) -> Dict[str, Tuple[ParamSet, dict]]:
    """
    Sweep all parameter combinations for a regime.
    Returns best ParamSet per strategy.
    """
    best = {}  # strategy -> (ParamSet, stats)

    total_combos = len(strategies) * len(param_sets)
    if verbose:
        print(f"\n  Sweeping {len(param_sets)} param combos × {len(strategies)} strategies "
              f"× {len(days)} days = {total_combos * len(days):,} simulations")

    for strat in strategies:
        best_score = -999
        best_params = None
        best_stats = None

        for pi, ps in enumerate(param_sets):
            wins = 0
            losses = 0
            total_pnl = 0.0
            total_wins_pnl = 0.0
            total_loss_pnl = 0.0
            gamma_exits = 0

            for td in days:
                iv = estimate_iv(td)
                result = fast_backtest_day(td, strat, ps, iv)
                if result is None:
                    continue
                total_pnl += result["pnl"]
                if result["win"]:
                    wins += 1
                    total_wins_pnl += result["pnl"]
                else:
                    losses += 1
                    total_loss_pnl += result["pnl"]
                if result["exit_reason"] == "gamma_risk":
                    gamma_exits += 1

            total_trades = wins + losses
            if total_trades < 5:
                continue

            wr = wins / total_trades * 100
            pf = total_wins_pnl / abs(total_loss_pnl) if total_loss_pnl != 0 else 99
            avg_pnl = total_pnl / total_trades

            # Composite score: profit factor × win rate weight × positive P&L bonus
            # Penalize high drawdown scenarios
            score = 0
            if pf > 1.0:
                score += pf * 2
            else:
                score += pf - 1  # Negative for losing
            if wr >= 70:
                score += 2
            elif wr >= 60:
                score += 1
            if total_pnl > 0:
                score += min(total_pnl / 1000, 3)
            else:
                score += max(total_pnl / 1000, -3)
            if gamma_exits > total_trades * 0.1:
                score -= 1

            if score > best_score:
                best_score = score
                best_params = ps
                best_stats = {
                    "trades": total_trades,
                    "wins": wins,
                    "losses": losses,
                    "win_rate": round(wr, 1),
                    "total_pnl": round(total_pnl, 2),
                    "profit_factor": round(pf, 2),
                    "avg_pnl": round(avg_pnl, 2),
                    "gamma_exits": gamma_exits,
                    "score": round(score, 2),
                }

            if verbose and (pi + 1) % 50 == 0:
                print(f"    {strat}: {pi+1}/{len(param_sets)} tested "
                      f"(best so far: score={best_score:.1f})", end="\r")

        if best_params:
            best[strat] = (best_params, best_stats)
            if verbose:
                print(f"    {strat}: ✅ {best_params.label()}")
                print(f"      → {best_stats['win_rate']}% WR, "
                      f"PF={best_stats['profit_factor']:.2f}, "
                      f"${best_stats['total_pnl']:+,.2f}, "
                      f"score={best_stats['score']:.1f}")
        else:
            if verbose:
                print(f"    {strat}: ❌ No viable params found")

    return best


# ─────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Parameter optimizer")
    parser.add_argument("--quick", action="store_true", help="Reduced grid")
    args = parser.parse_args()

    C_G = "\033[92m"
    C_R = "\033[91m"
    C_Y = "\033[93m"
    C_C = "\033[96m"
    C_B = "\033[1m"
    C_X = "\033[0m"

    STRATEGIES = ["put_credit", "call_credit", "iron_condor"]

    print(f"\n{C_B}{'═' * 78}{C_X}")
    print(f"{C_B}  PARAMETER OPTIMIZER — Finding Best Params Per Regime{C_X}")
    print(f"{C_B}{'═' * 78}{C_X}\n")

    # Load data
    cache_path = os.path.join("data", "intraday", "SPY_ibkr_5m_180d.csv")
    if not os.path.exists(cache_path):
        print("  ❌ No cached data. Run: python run_full_regime_test.py first")
        sys.exit(1)

    print(f"  Loading IBKR 5-min data ...")
    df = pd.read_csv(cache_path, index_col=0, parse_dates=True)
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, utc=True)

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
    print(f"  ✅ {len(trading_days)} trading days loaded\n")

    # Classify regimes
    print(f"  Fetching VIX for regime classification ...")
    vix_df = fetch_vix_history()
    regime_days = tag_days(trading_days, vix_df)

    regime_colors = {"GREEN": C_G, "YELLOW": C_Y, "RED": C_R}
    for regime in ["GREEN", "YELLOW", "RED"]:
        c = regime_colors[regime]
        print(f"  {c}■{C_X} {regime}: {len(regime_days[regime])} days")
    print()

    # Generate parameter grid
    grid = QUICK_GRID if args.quick else FULL_GRID
    param_sets = generate_param_sets(grid)
    print(f"  Parameter grid: {len(param_sets)} combinations "
          f"({'quick' if args.quick else 'full'})\n")

    # Optimize per regime
    optimal = {}  # regime -> {strategy -> (ParamSet, stats)}

    for regime in ["GREEN", "YELLOW", "RED"]:
        days_list = regime_days[regime]
        c = regime_colors[regime]
        print(f"{C_B}{'─' * 78}{C_X}")
        print(f"  {c}■{C_X} {C_B}OPTIMIZING {regime} REGIME ({len(days_list)} days){C_X}")
        print(f"{C_B}{'─' * 78}{C_X}")

        if len(days_list) < 3:
            print(f"  ⚠️  Too few days for optimization. Using conservative defaults.")
            # Very conservative for RED: wide delta, tight stop
            conservative = ParamSet(
                delta=0.08, stop_mult=1.5, profit_target=0.40,
                entry_start_min=30, entry_end_min=60,
                width=1.0, gamma_limit=0.08,
            )
            optimal[regime] = {
                s: (conservative, {"trades": 0, "win_rate": 0, "total_pnl": 0,
                                   "profit_factor": 0, "score": 0, "note": "default_conservative"})
                for s in STRATEGIES
            }
            continue

        best = optimize_regime(days_list, regime, STRATEGIES, param_sets)
        optimal[regime] = best

    # ─── Print final results ─────────────────────────────────────

    print(f"\n{C_B}{'═' * 78}{C_X}")
    print(f"{C_B}  OPTIMAL PARAMETERS PER REGIME{C_X}")
    print(f"{C_B}{'═' * 78}{C_X}\n")

    output = {}
    for regime in ["GREEN", "YELLOW", "RED"]:
        c = regime_colors[regime]
        print(f"  {c}{'━' * 74}{C_X}")
        print(f"  {c}■ {regime} REGIME{C_X}")
        print(f"  {c}{'━' * 74}{C_X}")

        output[regime] = {}
        for strat in STRATEGIES:
            if strat in optimal.get(regime, {}):
                ps, stats = optimal[regime][strat]
                pnl_c = C_G if stats.get("total_pnl", 0) > 0 else C_R
                print(f"\n    {C_B}{strat}{C_X}")
                print(f"      Delta:         {ps.delta:.2f}")
                print(f"      Stop-Loss:     {ps.stop_mult:.1f}× credit")
                print(f"      Profit Target: {ps.profit_target:.0%}")
                print(f"      Width:         ${ps.width:.0f}")
                print(f"      Entry Window:  +{ps.entry_start_min}-{ps.entry_end_min}m after open")
                print(f"      Gamma Limit:   {ps.gamma_limit:.2f}")
                print(f"      ─────────────")
                print(f"      Win Rate:      {stats.get('win_rate', 0):.1f}%")
                print(f"      P&L:           {pnl_c}${stats.get('total_pnl', 0):>+,.2f}{C_X}")
                print(f"      Profit Factor: {stats.get('profit_factor', 0):.2f}")
                print(f"      Score:         {stats.get('score', 0):.1f}")

                output[regime][strat] = {
                    "delta": ps.delta,
                    "stop_mult": ps.stop_mult,
                    "profit_target": ps.profit_target,
                    "width": ps.width,
                    "entry_start_min": ps.entry_start_min,
                    "entry_end_min": ps.entry_end_min,
                    "gamma_limit": ps.gamma_limit,
                    "backtest": stats,
                }
            else:
                print(f"\n    {C_B}{strat}{C_X}: ❌ No profitable params found → SKIP in this regime")
                output[regime][strat] = {"action": "SKIP"}

    # Save
    out_path = "optimized_regime_params.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\n  💾 Saved to {out_path}")

    # ─── Summary recommendation ──────────────────────────────────

    print(f"\n{C_B}{'═' * 78}{C_X}")
    print(f"{C_B}  ADAPTIVE STRATEGY RECOMMENDATION{C_X}")
    print(f"{C_B}{'═' * 78}{C_X}\n")

    for regime in ["GREEN", "YELLOW", "RED"]:
        c = regime_colors[regime]
        best_strat = None
        best_pf = 0
        for strat in STRATEGIES:
            if strat in optimal.get(regime, {}):
                _, stats = optimal[regime][strat]
                pf = stats.get("profit_factor", 0)
                if pf > best_pf:
                    best_pf = pf
                    best_strat = strat

        if best_strat and best_pf > 1.0:
            ps, stats = optimal[regime][best_strat]
            print(f"  {c}■{C_X} {regime}: Trade {C_B}{best_strat}{C_X} "
                  f"(Δ={ps.delta:.2f}, stop={ps.stop_mult:.1f}×, "
                  f"TP={ps.profit_target:.0%}, w=${ps.width:.0f})")
            print(f"    → {stats['win_rate']}% WR, PF={best_pf:.2f}, "
                  f"${stats['total_pnl']:+,.2f}")
        elif best_strat:
            print(f"  {c}■{C_X} {regime}: Best is {best_strat} but PF={best_pf:.2f} < 1.0 "
                  f"→ {C_Y}REDUCE SIZE or SKIP{C_X}")
        else:
            print(f"  {c}■{C_X} {regime}: {C_R}NO TRADE — sit on hands{C_X}")

    print(f"\n{C_B}{'═' * 78}{C_X}\n")


if __name__ == "__main__":
    main()

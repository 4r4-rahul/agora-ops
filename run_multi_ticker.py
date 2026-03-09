#!/usr/bin/env python3
"""
Multi-Ticker Optimizer & Validator
====================================
Pulls intraday data from IBKR for any ticker, runs the full parameter
sweep, validates adaptive params, and compares results across tickers.

Usage:
    python run_multi_ticker.py QQQ                # Pull + optimize + validate QQQ
    python run_multi_ticker.py QQQ IWM            # Multiple tickers
    python run_multi_ticker.py QQQ --quick        # Reduced grid (faster)
    python run_multi_ticker.py QQQ --days 90      # Custom period
    python run_multi_ticker.py --compare           # Compare all cached tickers
"""

import sys
import os
import json
import math
import itertools
import argparse
import socket
from datetime import date, timedelta
from collections import defaultdict
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from trading_engine.config import EngineConfig, RegimeParams, AdaptiveConfig
from trading_engine.data.intraday import IntradayBar, TradingDay
from trading_engine.data.intraday_backtester import IntradayBacktester

# Reuse optimizer components
from optimize_params import (
    ParamSet, FULL_GRID, QUICK_GRID, generate_param_sets,
    fast_backtest_day, estimate_iv, fetch_vix_history, classify_regime,
)

# ─── Colors ───────────────────────────────────────────────────────
C_G = "\033[92m"
C_R = "\033[91m"
C_Y = "\033[93m"
C_C = "\033[96m"
C_B = "\033[1m"
C_X = "\033[0m"
C_W = "\033[97m"

STRATEGIES = ["put_credit", "call_credit", "iron_condor"]

# ─── Ticker properties ───────────────────────────────────────────

TICKER_INFO = {
    "SPY":  {"strike_round": 1.0, "min_width": 1.0, "has_0dte": True,  "desc": "S&P 500 ETF"},
    "QQQ":  {"strike_round": 1.0, "min_width": 1.0, "has_0dte": True,  "desc": "Nasdaq 100 ETF"},
    "IWM":  {"strike_round": 1.0, "min_width": 1.0, "has_0dte": True,  "desc": "Russell 2000 ETF"},
    "AAPL": {"strike_round": 2.5, "min_width": 2.5, "has_0dte": False, "desc": "Apple Inc"},
    "MSFT": {"strike_round": 2.5, "min_width": 2.5, "has_0dte": False, "desc": "Microsoft Corp"},
    "NVDA": {"strike_round": 1.0, "min_width": 1.0, "has_0dte": False, "desc": "NVIDIA Corp"},
    "TSLA": {"strike_round": 1.0, "min_width": 1.0, "has_0dte": False, "desc": "Tesla Inc"},
    "META": {"strike_round": 2.5, "min_width": 2.5, "has_0dte": False, "desc": "Meta Platforms"},
    "AMZN": {"strike_round": 1.0, "min_width": 1.0, "has_0dte": False, "desc": "Amazon.com"},
}


def get_ticker_info(ticker: str) -> dict:
    """Get strike spacing and properties for a ticker."""
    ticker = ticker.upper()
    if ticker in TICKER_INFO:
        return TICKER_INFO[ticker]
    # Default: assume $1 strikes, no 0DTE
    return {"strike_round": 1.0, "min_width": 1.0, "has_0dte": False, "desc": ticker}


# ─── Data fetching ────────────────────────────────────────────────

def fetch_ibkr_data(ticker: str, days: int = 180) -> pd.DataFrame:
    """Pull historical 5-min bars from IBKR."""
    from trading_engine.data.ibkr_provider import IBKRDataProvider

    # Check TWS
    s = socket.socket()
    s.settimeout(2)
    port = int(os.getenv("IBKR_PORT", "7497"))
    if s.connect_ex(("127.0.0.1", port)) != 0:
        s.close()
        raise ConnectionError(f"TWS not running on port {port}")
    s.close()

    provider = IBKRDataProvider(port=port, client_id=20)  # Separate client ID
    if not provider.connect():
        raise ConnectionError("Could not connect to IBKR")

    try:
        print(f"  Fetching {days} days of 5-min {ticker} bars from IBKR ...")
        df = provider.get_historical_bars(ticker, days=days, interval="5m")
        if df.empty:
            raise ValueError(f"No data returned for {ticker}")
        print(f"  ✅ {len(df)} bars: {df.index[0]} → {df.index[-1]}")
        print(f"     Price range: ${df['low'].min():.2f} → ${df['high'].max():.2f}")
        return df
    finally:
        provider.disconnect()


def load_or_fetch(ticker: str, days: int = 180, force: bool = False) -> pd.DataFrame:
    """Load from cache or fetch from IBKR."""
    cache_dir = os.path.join("data", "intraday")
    os.makedirs(cache_dir, exist_ok=True)
    cache_path = os.path.join(cache_dir, f"{ticker}_ibkr_5m_{days}d.csv")

    if os.path.exists(cache_path) and not force:
        print(f"  Loading cached data from {cache_path} ...")
        df = pd.read_csv(cache_path, index_col=0, parse_dates=True)
        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index, utc=True)
        print(f"  ✅ {len(df)} bars loaded from cache")
        return df

    df = fetch_ibkr_data(ticker, days)
    df.to_csv(cache_path)
    print(f"  💾 Cached to {cache_path}")
    return df


def df_to_trading_days(df: pd.DataFrame, ticker: str) -> List[TradingDay]:
    """Convert DataFrame to TradingDay list."""
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, utc=True)

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

    return [
        TradingDay(date=d, bars=bars, ticker=ticker.upper())
        for d, bars in sorted(days_dict.items())
        if len(bars) >= 30
    ]


def tag_regimes(trading_days: List[TradingDay]) -> Dict[str, str]:
    """Tag each day with VIX regime."""
    vix_df = fetch_vix_history()
    vix_dates = {d.date(): float(row.get("close", 18))
                 for d, row in vix_df.iterrows() if hasattr(d, 'date')}

    regime_map = {}
    for td in trading_days:
        vix_val = 18.0
        for offset in range(5):
            check = td.date - timedelta(days=offset)
            if check in vix_dates:
                vix_val = vix_dates[check]
                break
        td._regime = classify_regime(vix_val)
        td._vix = vix_val
        regime_map[str(td.date)] = td._regime

    return regime_map


# ─── Optimizer (ticker-aware) ────────────────────────────────────

def fast_backtest_day_ticker(td: TradingDay, strategy: str, params: ParamSet,
                              iv: float, balance: float = 50_000) -> Optional[dict]:
    """
    Fast single-day backtest — ticker-aware version.
    Adjusts strike rounding and width based on the ticker.
    """
    from trading_engine.black_scholes import (
        bs_put_price, bs_call_price, bs_gamma, strike_at_delta,
    )

    r = 0.045
    info = get_ticker_info(td.ticker)
    strike_round = info["strike_round"]
    width = max(params.width, info["min_width"])

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

    for b in td.bars[entry_idx + 1:]:
        T_now = b.time_to_close_years
        S_now = b.close

        if spread_type == "put":
            mark = max(bs_put_price(S_now, K_short, T_now, r, iv) -
                       bs_put_price(S_now, K_long, T_now, r, iv), 0)
        elif spread_type == "call":
            mark = max(bs_call_price(S_now, K_short, T_now, r, iv) -
                       bs_call_price(S_now, K_long, T_now, r, iv), 0)
        else:
            pv = bs_put_price(S_now, K_put_short, T_now, r, iv) - \
                 bs_put_price(S_now, K_put_long, T_now, r, iv)
            cv = bs_call_price(S_now, K_call_short, T_now, r, iv) - \
                 bs_call_price(S_now, K_call_long, T_now, r, iv)
            mark = max(pv + cv, 0)

        if mark >= stop_price:
            exit_reason = "stop_loss"
            exit_price = min(mark, width)
            break

        if mark <= target_price:
            exit_reason = "profit_target"
            exit_price = mark
            break

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


def optimize_ticker(trading_days: List[TradingDay], regime_days: Dict[str, List[TradingDay]],
                     param_sets: List[ParamSet]) -> Dict[str, Dict[str, Tuple[ParamSet, dict]]]:
    """Run full optimization for a ticker. Returns regime -> {strategy -> (best_params, stats)}."""
    optimal = {}

    for regime in ["GREEN", "YELLOW", "RED"]:
        days_list = regime_days.get(regime, [])
        c = {"GREEN": C_G, "YELLOW": C_Y, "RED": C_R}[regime]

        print(f"\n  {c}■{C_X} {C_B}{regime}{C_X} ({len(days_list)} days)")

        if len(days_list) < 3:
            print(f"    ⚠️  Too few days — skipping")
            optimal[regime] = {}
            continue

        best = {}
        total_combos = len(STRATEGIES) * len(param_sets)
        print(f"    Sweeping {len(param_sets)} params × {len(STRATEGIES)} strategies "
              f"× {len(days_list)} days = {total_combos * len(days_list):,} sims")

        for strat in STRATEGIES:
            best_score = -999
            best_params = None
            best_stats = None

            for pi, ps in enumerate(param_sets):
                wins = losses = 0
                total_pnl = wins_pnl = 0.0
                loss_pnl = 0.0
                gamma_exits = 0

                for td in days_list:
                    iv = estimate_iv(td)
                    result = fast_backtest_day_ticker(td, strat, ps, iv)
                    if result is None:
                        continue
                    total_pnl += result["pnl"]
                    if result["win"]:
                        wins += 1
                        wins_pnl += result["pnl"]
                    else:
                        losses += 1
                        loss_pnl += result["pnl"]
                    if result["exit_reason"] == "gamma_risk":
                        gamma_exits += 1

                total_trades = wins + losses
                if total_trades < 5:
                    continue

                wr = wins / total_trades * 100
                pf = wins_pnl / abs(loss_pnl) if loss_pnl != 0 else 99
                avg_pnl = total_pnl / total_trades

                score = 0
                if pf > 1.0:
                    score += pf * 2
                else:
                    score += pf - 1
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

                if (pi + 1) % 100 == 0:
                    print(f"      {strat}: {pi+1}/{len(param_sets)} "
                          f"(best: score={best_score:.1f})", end="\r")

            if best_params and best_stats and best_stats["profit_factor"] > 1.0:
                best[strat] = (best_params, best_stats)
                print(f"    ✅ {strat:<15} {best_params.label()}")
                print(f"       → {best_stats['win_rate']}% WR, "
                      f"PF={best_stats['profit_factor']:.2f}, "
                      f"${best_stats['total_pnl']:+,.2f}")
            else:
                print(f"    ❌ {strat:<15} no profitable params")

        optimal[regime] = best

    return optimal


def pick_best_strategy(optimal: Dict[str, Dict[str, Tuple[ParamSet, dict]]]) \
        -> Dict[str, Tuple[str, ParamSet, dict]]:
    """For each regime, pick the single best strategy."""
    picks = {}
    for regime in ["GREEN", "YELLOW", "RED"]:
        strats = optimal.get(regime, {})
        if not strats:
            picks[regime] = ("none", None, None)
            continue

        best_strat = max(strats.keys(),
                         key=lambda s: strats[s][1].get("score", 0))
        ps, stats = strats[best_strat]
        picks[regime] = (best_strat, ps, stats)

    return picks


def build_adaptive_config(picks: Dict[str, Tuple[str, Optional[ParamSet], Optional[dict]]]) \
        -> AdaptiveConfig:
    """Build AdaptiveConfig from optimizer picks."""
    def to_regime_params(strat: str, ps: Optional[ParamSet]) -> RegimeParams:
        if ps is None or strat == "none":
            return RegimeParams(trade_enabled=False, preferred_strategy="none",
                                position_size_mult=0.0)
        return RegimeParams(
            delta=ps.delta, stop_mult=ps.stop_mult,
            profit_target=ps.profit_target, width=ps.width,
            entry_start_min=ps.entry_start_min, entry_end_min=ps.entry_end_min,
            gamma_limit=ps.gamma_limit, preferred_strategy=strat,
            trade_enabled=True, position_size_mult=1.0 if "GREEN" else 0.5,
        )

    green_strat, green_ps, _ = picks.get("GREEN", ("none", None, None))
    yellow_strat, yellow_ps, _ = picks.get("YELLOW", ("none", None, None))
    red_strat, red_ps, _ = picks.get("RED", ("none", None, None))

    green_rp = to_regime_params(green_strat, green_ps)
    yellow_rp = to_regime_params(yellow_strat, yellow_ps)
    yellow_rp.position_size_mult = 0.5  # Always half size in yellow
    red_rp = to_regime_params(red_strat, red_ps)

    return AdaptiveConfig(green=green_rp, yellow=yellow_rp, red=red_rp)


def validate_adaptive(ticker: str, trading_days: List[TradingDay],
                       regime_map: Dict[str, str],
                       picks: Dict[str, Tuple[str, Optional[ParamSet], Optional[dict]]]):
    """Run full backtester validation with adaptive params."""
    config = EngineConfig()
    config.adaptive = build_adaptive_config(picks)

    print(f"\n  {C_B}Validation: Fixed vs Adaptive{C_X}")

    # Fixed baseline (iron_condor, default params)
    bt_fixed = IntradayBacktester(config)
    fixed = bt_fixed.run(trading_days, strategies=["iron_condor"])

    # Adaptive
    bt_adapt = IntradayBacktester(config)
    adaptive = bt_adapt.run(trading_days, adaptive=True, regime_map=regime_map)

    # Print comparison
    print(f"\n  {'Metric':<22} {'FIXED IC':>14} {'ADAPTIVE':>14}  {'':>3}")
    print(f"  {'─' * 56}")

    rows = [
        ("Trades", fixed.total_trades, adaptive.total_trades),
        ("Win Rate %", fixed.win_rate, adaptive.win_rate),
        ("Total P&L $", fixed.total_pnl, adaptive.total_pnl),
        ("Profit Factor", fixed.profit_factor, adaptive.profit_factor),
        ("Max DD %", fixed.max_drawdown_pct, adaptive.max_drawdown_pct),
        ("Sharpe Ratio", fixed.sharpe_ratio, adaptive.sharpe_ratio),
        ("Gamma Exits", fixed.gamma_blow_ups, adaptive.gamma_blow_ups),
    ]

    for name, fv, av in rows:
        if "P&L" in name:
            fc = C_G if fv > 0 else C_R
            ac = C_G if av > 0 else C_R
            arr = C_G + " ↑" + C_X if av > fv else C_R + " ↓" + C_X
            print(f"  {name:<22} {fc}${fv:>12,.2f}{C_X} {ac}${av:>12,.2f}{C_X} {arr}")
        elif "%" in name:
            arr = C_G + " ↑" + C_X if av > fv else (C_R + " ↓" + C_X if av < fv else "  →")
            if "DD" in name:
                arr = C_G + " ↑" + C_X if av < fv else C_R + " ↓" + C_X
            print(f"  {name:<22} {fv:>13.1f}% {av:>13.1f}% {arr}")
        else:
            arr = C_G + " ↑" + C_X if av > fv else (C_R + " ↓" + C_X if av < fv else "  →")
            if "Gamma" in name:
                arr = C_G + " ↑" + C_X if av < fv else C_R + " ↓" + C_X
            print(f"  {name:<22} {fv:>14.2f} {av:>14.2f} {arr}")

    # Per-regime breakdown
    print(f"\n  {C_B}Per-Regime (Adaptive){C_X}")
    for regime, color in [("GREEN", C_G), ("YELLOW", C_Y), ("RED", C_R)]:
        trades = [t for t in adaptive.trades if regime_map.get(str(t.date)) == regime]
        if not trades:
            print(f"  {color}■{C_X} {regime:6s}: NO TRADES")
            continue
        wins = sum(1 for t in trades if t.total_pnl > 0)
        pnl = sum(t.total_pnl for t in trades)
        wr = wins / len(trades) * 100
        win_pnl = sum(t.total_pnl for t in trades if t.total_pnl > 0)
        loss_pnl_abs = abs(sum(t.total_pnl for t in trades if t.total_pnl <= 0))
        pf = win_pnl / loss_pnl_abs if loss_pnl_abs > 0 else float('inf')
        pnl_c = C_G if pnl > 0 else C_R
        print(f"  {color}■{C_X} {regime:6s}: {len(trades)} trades | "
              f"{wr:.0f}% WR | {pnl_c}${pnl:>+,.2f}{C_X} | PF={pf:.2f}")

    # Monthly P&L
    print(f"\n  {C_B}Monthly P&L (Adaptive){C_X}")
    if adaptive.monthly_pnl:
        max_m = max(abs(v) for v in adaptive.monthly_pnl.values()) or 1
        for month, mpnl in adaptive.monthly_pnl.items():
            color = C_G if mpnl >= 0 else C_R
            bar_len = int(abs(mpnl) / max_m * 20) if max_m > 0 else 0
            print(f"  {month}  {color}{'█' * bar_len:<20} ${mpnl:>+10,.2f}{C_X}")

    return fixed, adaptive


# ─── Main ─────────────────────────────────────────────────────────

def process_ticker(ticker: str, days: int, quick: bool, force: bool):
    """Full pipeline for one ticker: fetch → optimize → validate."""
    ticker = ticker.upper()
    info = get_ticker_info(ticker)

    print(f"\n{C_B}{'═' * 78}{C_X}")
    print(f"{C_B}  {ticker} — {info['desc']}{C_X}")
    print(f"{C_B}  Strike spacing: ${info['strike_round']:.1f}  |  "
          f"0DTE: {'✅ Yes' if info['has_0dte'] else '❌ No (weekly expiry)'}  |  "
          f"Min width: ${info['min_width']:.1f}{C_X}")
    print(f"{C_B}{'═' * 78}{C_X}\n")

    # 1. Fetch data
    print(f"  {C_C}STEP 1: Data{C_X}")
    df = load_or_fetch(ticker, days, force)
    trading_days = df_to_trading_days(df, ticker)
    print(f"  ✅ {len(trading_days)} trading days\n")

    # 2. Classify regimes
    print(f"  {C_C}STEP 2: Regime Classification{C_X}")
    regime_map = tag_regimes(trading_days)
    regime_days = {"GREEN": [], "YELLOW": [], "RED": []}
    for td in trading_days:
        regime_days[td._regime].append(td)

    for regime, color in [("GREEN", C_G), ("YELLOW", C_Y), ("RED", C_R)]:
        print(f"  {color}■{C_X} {regime}: {len(regime_days[regime])} days")

    # 3. Optimize
    print(f"\n  {C_C}STEP 3: Parameter Optimization{C_X}")
    grid = QUICK_GRID if quick else FULL_GRID
    param_sets = generate_param_sets(grid)

    # Adjust widths for ticker
    adjusted = []
    for ps in param_sets:
        adj_width = max(ps.width, info["min_width"])
        adj_width = round(adj_width / info["strike_round"]) * info["strike_round"]
        if adj_width != ps.width:
            ps = ParamSet(delta=ps.delta, stop_mult=ps.stop_mult,
                         profit_target=ps.profit_target,
                         entry_start_min=ps.entry_start_min,
                         entry_end_min=ps.entry_end_min,
                         width=adj_width, gamma_limit=ps.gamma_limit)
        adjusted.append(ps)
    # Deduplicate
    seen = set()
    param_sets = []
    for ps in adjusted:
        key = (ps.delta, ps.stop_mult, ps.profit_target, ps.entry_start_min,
               ps.entry_end_min, ps.width, ps.gamma_limit)
        if key not in seen:
            seen.add(key)
            param_sets.append(ps)

    print(f"  Grid: {len(param_sets)} unique param combos ({'quick' if quick else 'full'})")

    optimal = optimize_ticker(trading_days, regime_days, param_sets)
    picks = pick_best_strategy(optimal)

    # Print picks
    print(f"\n  {C_B}BEST STRATEGY PER REGIME{C_X}")
    for regime, color in [("GREEN", C_G), ("YELLOW", C_Y), ("RED", C_R)]:
        strat, ps, stats = picks[regime]
        if ps and strat != "none":
            pnl_c = C_G if stats["total_pnl"] > 0 else C_R
            print(f"  {color}■{C_X} {regime}: {C_B}{strat}{C_X} "
                  f"(Δ={ps.delta:.2f}, stop={ps.stop_mult:.1f}×, "
                  f"TP={ps.profit_target:.0%}, w=${ps.width:.0f})")
            print(f"    → {stats['win_rate']}% WR, PF={stats['profit_factor']:.2f}, "
                  f"{pnl_c}${stats['total_pnl']:+,.2f}{C_X}")
        else:
            print(f"  {color}■{C_X} {regime}: {C_R}NO TRADE{C_X}")

    # 4. Validate
    print(f"\n  {C_C}STEP 4: Validation{C_X}")
    fixed, adaptive = validate_adaptive(ticker, trading_days, regime_map, picks)

    # 5. Summary
    total_return = (adaptive.ending_balance - adaptive.starting_balance) / \
                   adaptive.starting_balance * 100
    ann_return = total_return * (252 / max(adaptive.total_days, 1))

    print(f"\n{C_B}{'═' * 78}{C_X}")
    print(f"{C_B}  {ticker} SUMMARY{C_X}")
    print(f"{C_B}{'═' * 78}{C_X}\n")
    print(f"  Total Return:      {C_G if total_return > 0 else C_R}{total_return:+.2f}%{C_X}")
    print(f"  Annualized:        {C_G if ann_return > 0 else C_R}{ann_return:+.1f}%{C_X}")
    print(f"  Win Rate:          {adaptive.win_rate:.1f}%")
    print(f"  Profit Factor:     {adaptive.profit_factor:.2f}")
    print(f"  Max Drawdown:      {adaptive.max_drawdown_pct:.2f}%")
    print(f"  Sharpe Ratio:      {adaptive.sharpe_ratio:.2f}")

    improvement = adaptive.total_pnl - fixed.total_pnl
    print(f"  vs Fixed IC:       {C_G if improvement > 0 else C_R}"
          f"${improvement:>+,.2f}{C_X}")

    # Score
    score = 0
    if adaptive.profit_factor >= 2.0: score += 3
    elif adaptive.profit_factor >= 1.5: score += 2
    elif adaptive.profit_factor >= 1.0: score += 1
    if adaptive.win_rate >= 80: score += 2
    elif adaptive.win_rate >= 70: score += 1
    if adaptive.max_drawdown_pct < 5: score += 2
    elif adaptive.max_drawdown_pct < 10: score += 1
    if adaptive.sharpe_ratio > 1.5: score += 2
    elif adaptive.sharpe_ratio > 0.5: score += 1

    if score >= 8:
        verdict = f"{C_G}★★★ PRODUCTION READY{C_X}"
    elif score >= 5:
        verdict = f"{C_Y}★★☆ PAPER TRADE FIRST{C_X}"
    elif score >= 3:
        verdict = f"{C_Y}★☆☆ NEEDS TUNING{C_X}"
    else:
        verdict = f"{C_R}☆☆☆ NOT READY{C_X}"
    print(f"  Rating:            {verdict} ({score}/10)")

    # Save results
    result = {
        "ticker": ticker,
        "test_date": str(date.today()),
        "trading_days": len(trading_days),
        "fixed_ic": {
            "trades": fixed.total_trades, "win_rate": fixed.win_rate,
            "total_pnl": fixed.total_pnl, "profit_factor": fixed.profit_factor,
        },
        "adaptive": {
            "trades": adaptive.total_trades, "win_rate": adaptive.win_rate,
            "total_pnl": adaptive.total_pnl, "profit_factor": adaptive.profit_factor,
            "max_drawdown_pct": adaptive.max_drawdown_pct,
            "sharpe_ratio": adaptive.sharpe_ratio,
            "total_return_pct": round(total_return, 2),
            "annualized_return_pct": round(ann_return, 1),
            "monthly_pnl": adaptive.monthly_pnl,
        },
        "regime_picks": {
            regime: {
                "strategy": strat,
                "params": {
                    "delta": ps.delta, "stop_mult": ps.stop_mult,
                    "profit_target": ps.profit_target, "width": ps.width,
                    "entry_window": f"{ps.entry_start_min}-{ps.entry_end_min}m",
                    "gamma_limit": ps.gamma_limit,
                } if ps else None,
                "stats": stats,
            }
            for regime, (strat, ps, stats) in picks.items()
        },
        "score": score,
    }

    out_path = f"{ticker.lower()}_optimization_results.json"
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)
    print(f"\n  💾 Saved to {out_path}")

    return result


def compare_tickers():
    """Compare all optimized tickers."""
    import glob
    files = glob.glob("*_optimization_results.json")
    if not files:
        print("  No optimization results found. Run optimizer on tickers first.")
        return

    results = []
    for f in files:
        with open(f) as fp:
            results.append(json.load(fp))

    print(f"\n{C_B}{'═' * 90}{C_X}")
    print(f"{C_B}  MULTI-TICKER COMPARISON{C_X}")
    print(f"{C_B}{'═' * 90}{C_X}\n")

    print(f"  {'Ticker':<8} {'Days':>5} {'Trades':>7} {'WR%':>6} {'P&L':>12} "
          f"{'PF':>6} {'MaxDD%':>7} {'Sharpe':>7} {'Ann%':>7} {'Score':>6}")
    print(f"  {'─' * 85}")

    for r in sorted(results, key=lambda x: x["adaptive"]["total_pnl"], reverse=True):
        a = r["adaptive"]
        pnl_c = C_G if a["total_pnl"] > 0 else C_R
        print(f"  {r['ticker']:<8} {r['trading_days']:>5} {a['trades']:>7} "
              f"{a['win_rate']:>5.1f}% {pnl_c}${a['total_pnl']:>10,.2f}{C_X} "
              f"{a['profit_factor']:>6.2f} {a['max_drawdown_pct']:>6.2f}% "
              f"{a['sharpe_ratio']:>7.2f} {a['annualized_return_pct']:>6.1f}% "
              f"{r['score']:>5}/10")

    # Strategy breakdown
    print(f"\n  {C_B}OPTIMAL STRATEGY BY TICKER × REGIME{C_X}")
    print(f"  {'─' * 60}")
    print(f"  {'Ticker':<8} {'GREEN':^16} {'YELLOW':^16} {'RED':^16}")
    print(f"  {'─' * 60}")
    for r in results:
        parts = []
        for regime in ["GREEN", "YELLOW", "RED"]:
            pick = r["regime_picks"].get(regime, {})
            strat = pick.get("strategy", "none")
            if strat == "none" or pick.get("params") is None:
                parts.append(f"{C_R}{'skip':^14}{C_X}")
            else:
                abbrev = strat.replace("_credit", "").replace("_", " ")[:10]
                parts.append(f"{C_G}{abbrev:^14}{C_X}")
        print(f"  {r['ticker']:<8} {'  '.join(parts)}")

    print(f"\n{C_B}{'═' * 90}{C_X}\n")


def main():
    parser = argparse.ArgumentParser(description="Multi-ticker optimizer + validator")
    parser.add_argument("tickers", nargs="*", help="Tickers to process (e.g., QQQ IWM)")
    parser.add_argument("--days", type=int, default=180, help="Days of history (default 180)")
    parser.add_argument("--quick", action="store_true", help="Reduced parameter grid")
    parser.add_argument("--force", action="store_true", help="Re-fetch data even if cached")
    parser.add_argument("--compare", action="store_true", help="Compare all cached results")
    args = parser.parse_args()

    if args.compare:
        compare_tickers()
        return

    if not args.tickers:
        print("Usage: python run_multi_ticker.py QQQ [IWM] [--quick] [--days 90]")
        print("       python run_multi_ticker.py --compare")
        return

    all_results = []
    for ticker in args.tickers:
        result = process_ticker(ticker, args.days, args.quick, args.force)
        all_results.append(result)

    if len(all_results) > 1:
        compare_tickers()


if __name__ == "__main__":
    main()

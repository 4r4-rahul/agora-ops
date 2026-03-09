#!/usr/bin/env python3
"""
1-Minute Resolution Test
==========================
Pulls 1-min bars from IBKR, runs optimizer + validation, and compares
against 5-min results to quantify how much resolution matters for 0DTE.

IBKR 1-min data limits:
  - Max duration per request: ~30 days
  - Need to chunk 180 days into 30-day windows
  - Rate limit: ~10 requests/min (we add sleep between chunks)

This is the CRITICAL reality check:
  - 5-min bars can hide flash spikes (stop blowthrough)
  - 1-min bars give 5x more data points for stop/target execution
  - If P&L drops significantly, 5-min results were over-optimistic
"""

import sys
import os
import json
import time
import socket
from datetime import date, datetime, timedelta
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from trading_engine.config import EngineConfig, RegimeParams, AdaptiveConfig
from trading_engine.data.intraday import IntradayBar, TradingDay
from trading_engine.data.intraday_backtester import IntradayBacktester

from optimize_params import (
    ParamSet, FULL_GRID, QUICK_GRID, generate_param_sets,
    estimate_iv, fetch_vix_history, classify_regime,
)

# ─── Colors ───────────────────────────────────────────────────────
C_G = "\033[92m"
C_R = "\033[91m"
C_Y = "\033[93m"
C_C = "\033[96m"
C_B = "\033[1m"
C_X = "\033[0m"

STRATEGIES = ["put_credit", "call_credit", "iron_condor"]


# ─── Fetch 1-min bars (chunked for IBKR limits) ──────────────────

def fetch_1min_ibkr(ticker: str, total_days: int = 180) -> pd.DataFrame:
    """
    Fetch 1-min bars from IBKR in chunks (max ~30 days per request).
    Returns combined DataFrame.
    """
    from trading_engine.data.ibkr_provider import IBKRDataProvider

    port = int(os.getenv("IBKR_PORT", "7497"))
    provider = IBKRDataProvider(port=port, client_id=25)
    if not provider.connect():
        raise ConnectionError("Cannot connect to IBKR")

    try:
        import ib_insync
        ib = provider._ib

        contract = ib_insync.Stock(ticker.upper(), "SMART", "USD")
        ib.qualifyContracts(contract)

        # IBKR limit for 1-min bars: ~30 days per request
        chunk_days = 20  # Conservative to avoid pacing violations
        chunks = []
        end_date = datetime.now()

        num_chunks = (total_days + chunk_days - 1) // chunk_days
        print(f"  Fetching {total_days} days in {num_chunks} chunks of {chunk_days} days ...")

        for i in range(num_chunks):
            end_str = end_date.strftime("%Y%m%d %H:%M:%S")
            duration = f"{chunk_days} D"

            try:
                bars = ib.reqHistoricalData(
                    contract,
                    endDateTime=end_str,
                    durationStr=duration,
                    barSizeSetting="1 min",
                    whatToShow="TRADES",
                    useRTH=True,
                    formatDate=1,
                    keepUpToDate=False,
                )
            except Exception as e:
                print(f"    ⚠️  Chunk {i+1} error: {e}")
                time.sleep(15)
                continue

            if bars:
                records = [{
                    "timestamp": pd.Timestamp(b.date),
                    "open": b.open, "high": b.high,
                    "low": b.low, "close": b.close,
                    "volume": b.volume,
                    "vwap": getattr(b, "average", 0) or 0,
                } for b in bars]
                chunk_df = pd.DataFrame(records).set_index("timestamp")
                chunks.append(chunk_df)
                print(f"    Chunk {i+1}/{num_chunks}: {len(bars)} bars "
                      f"({chunk_df.index[0].strftime('%Y-%m-%d')} → "
                      f"{chunk_df.index[-1].strftime('%Y-%m-%d')})")

            # Move window back
            end_date -= timedelta(days=chunk_days)

            # IBKR pacing: max 60 requests in 10 min
            if i < num_chunks - 1:
                time.sleep(11)  # Be conservative

        if not chunks:
            raise RuntimeError(f"No 1-min data returned for {ticker}")

        df = pd.concat(chunks).sort_index()
        df = df[~df.index.duplicated(keep='first')]

        if df.index.tz is None:
            df.index = df.index.tz_localize("US/Eastern")

        print(f"  ✅ Total: {len(df)} 1-min bars "
              f"({df.index[0]} → {df.index[-1]})")
        return df

    finally:
        provider.disconnect()


def load_or_fetch_1min(ticker: str, days: int = 180, force: bool = False) -> pd.DataFrame:
    """Load from cache or fetch from IBKR."""
    cache_dir = os.path.join("data", "intraday")
    os.makedirs(cache_dir, exist_ok=True)
    cache_path = os.path.join(cache_dir, f"{ticker}_ibkr_1m_{days}d.csv")

    if os.path.exists(cache_path) and not force:
        print(f"  Loading cached 1-min data from {cache_path} ...")
        df = pd.read_csv(cache_path, index_col=0, parse_dates=True)
        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index, utc=True)
        print(f"  ✅ {len(df)} bars loaded from cache")
        return df

    df = fetch_1min_ibkr(ticker, days)
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

    tds = [
        TradingDay(date=d, bars=bars, ticker=ticker.upper())
        for d, bars in sorted(days_dict.items())
        if len(bars) >= 30  # Works for both 1-min and 5-min
    ]
    return tds


# ─── Fast backtester (1-min aware) ───────────────────────────────

def fast_backtest_day_1m(td: TradingDay, strategy: str, params: ParamSet,
                          iv: float, balance: float = 50_000) -> Optional[dict]:
    """
    Fast single-day backtest on 1-min bars.
    Same logic as 5-min but with realistic stop execution.
    """
    from trading_engine.black_scholes import (
        bs_put_price, bs_call_price, bs_gamma, strike_at_delta,
    )

    r = 0.045
    ticker = td.ticker.upper()
    strike_round = 1.0 if ticker in ("SPY", "QQQ", "IWM", "SPDR") else 5.0
    width = params.width

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

    if strategy == "put_credit":
        K_short = round(strike_at_delta(S, T, r, iv, params.delta, "put") / strike_round) * strike_round
        K_long = K_short - width
        raw = bs_put_price(S, K_short, T, r, iv) - bs_put_price(S, K_long, T, r, iv)
        credit = max(raw * (1 - slippage), 0.01)
        spread_type = "put"
    elif strategy == "call_credit":
        K_short = round(strike_at_delta(S, T, r, iv, params.delta, "call") / strike_round) * strike_round
        K_long = K_short + width
        raw = bs_call_price(S, K_short, T, r, iv) - bs_call_price(S, K_long, T, r, iv)
        credit = max(raw * (1 - slippage), 0.01)
        spread_type = "call"
    elif strategy == "iron_condor":
        K_ps = round(strike_at_delta(S, T, r, iv, params.delta, "put") / strike_round) * strike_round
        K_pl = K_ps - width
        K_cs = round(strike_at_delta(S, T, r, iv, params.delta, "call") / strike_round) * strike_round
        K_cl = K_cs + width
        praw = bs_put_price(S, K_ps, T, r, iv) - bs_put_price(S, K_pl, T, r, iv)
        craw = bs_call_price(S, K_cs, T, r, iv) - bs_call_price(S, K_cl, T, r, iv)
        credit = max((praw + craw) * (1 - slippage), 0.01)
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

    # Key difference: at 1-min, we also check the HIGH of the bar
    # for stop-loss execution (more realistic slippage model)
    for b in td.bars[entry_idx + 1:]:
        T_now = b.time_to_close_years
        S_now = b.close

        # Use bar HIGH for stop check (worst case within the bar)
        # and bar LOW for target check (best case within the bar)
        S_high = b.high
        S_low = b.low

        # Price spread at the close of the bar
        if spread_type == "put":
            mark = max(bs_put_price(S_now, K_short, T_now, r, iv) -
                       bs_put_price(S_now, K_long, T_now, r, iv), 0)
            # Worst case mark (for put credit, worst = when price drops to bar low)
            mark_worst = max(bs_put_price(S_low, K_short, T_now, r, iv) -
                            bs_put_price(S_low, K_long, T_now, r, iv), 0)
        elif spread_type == "call":
            mark = max(bs_call_price(S_now, K_short, T_now, r, iv) -
                       bs_call_price(S_now, K_long, T_now, r, iv), 0)
            mark_worst = max(bs_call_price(S_high, K_short, T_now, r, iv) -
                            bs_call_price(S_high, K_long, T_now, r, iv), 0)
        else:
            pv = bs_put_price(S_now, K_ps, T_now, r, iv) - bs_put_price(S_now, K_pl, T_now, r, iv)
            cv = bs_call_price(S_now, K_cs, T_now, r, iv) - bs_call_price(S_now, K_cl, T_now, r, iv)
            mark = max(pv + cv, 0)
            # Worst for IC: max of put side (at low) and call side (at high)
            pw = bs_put_price(S_low, K_ps, T_now, r, iv) - bs_put_price(S_low, K_pl, T_now, r, iv)
            cw = bs_call_price(S_high, K_cs, T_now, r, iv) - bs_call_price(S_high, K_cl, T_now, r, iv)
            mark_worst = max(max(pw, 0) + max(cv, 0), max(pv, 0) + max(cw, 0))

        # Stop-loss: check WORST mark (intra-bar adverse excursion)
        if mark_worst >= stop_price:
            exit_reason = "stop_loss"
            # Realistic fill: somewhere between stop_price and mark_worst
            exit_price = min(mark_worst * 1.05, width)  # 5% additional slippage
            break

        # Profit target: check at close (conservative — don't assume mid-bar fill)
        if mark <= target_price:
            exit_reason = "profit_target"
            exit_price = mark
            break

        # Gamma check
        if spread_type == "put":
            gamma = abs(bs_gamma(S_now, K_short, T_now, r, iv))
        elif spread_type == "call":
            gamma = abs(bs_gamma(S_now, K_short, T_now, r, iv))
        else:
            gamma = abs(bs_gamma(S_now, K_ps, T_now, r, iv)) + \
                    abs(bs_gamma(S_now, K_cs, T_now, r, iv))

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
            pi = max(K_ps - S_final, 0) - max(K_pl - S_final, 0)
            ci = max(S_final - K_cs, 0) - max(S_final - K_cl, 0)
            exit_price = min(max(pi, 0) + max(ci, 0), width)
        if exit_price > 0.01:
            exit_reason = "expired_itm"

    pnl_per = credit - exit_price
    close_slippage = exit_price * 0.05
    if pnl_per < 0:
        close_slippage = -close_slippage
    pnl_per -= close_slippage

    total_pnl = round(pnl_per * contracts * 100 - commission, 2)
    return {"pnl": total_pnl, "exit_reason": exit_reason, "credit": credit,
            "contracts": contracts, "win": total_pnl > 0}


# ─── Optimizer on 1-min data ─────────────────────────────────────

def optimize_1min(regime_days: Dict[str, List[TradingDay]],
                   param_sets: List[ParamSet]) -> Dict[str, Dict[str, Tuple[ParamSet, dict]]]:
    """Run parameter sweep on 1-min data with realistic stop model."""
    optimal = {}

    for regime in ["GREEN", "YELLOW", "RED"]:
        days_list = regime_days.get(regime, [])
        c = {"GREEN": C_G, "YELLOW": C_Y, "RED": C_R}[regime]

        print(f"\n  {c}■{C_X} {C_B}{regime}{C_X} ({len(days_list)} days)")

        if len(days_list) < 3:
            print(f"    ⚠️  Too few days — skip")
            optimal[regime] = {}
            continue

        total_sims = len(param_sets) * len(STRATEGIES) * len(days_list)
        print(f"    {len(param_sets)} params × {len(STRATEGIES)} strategies "
              f"× {len(days_list)} days = {total_sims:,} sims")

        best = {}
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
                    result = fast_backtest_day_1m(td, strat, ps, iv)
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
                if pf > 1.0: score += pf * 2
                else: score += pf - 1
                if wr >= 70: score += 2
                elif wr >= 60: score += 1
                if total_pnl > 0: score += min(total_pnl / 1000, 3)
                else: score += max(total_pnl / 1000, -3)
                if gamma_exits > total_trades * 0.1: score -= 1

                if score > best_score:
                    best_score = score
                    best_params = ps
                    best_stats = {
                        "trades": total_trades, "wins": wins, "losses": losses,
                        "win_rate": round(wr, 1), "total_pnl": round(total_pnl, 2),
                        "profit_factor": round(pf, 2), "avg_pnl": round(avg_pnl, 2),
                        "gamma_exits": gamma_exits, "score": round(score, 2),
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


# ─── Comparison engine ────────────────────────────────────────────

def run_comparison(ticker: str, days_5m: List[TradingDay], days_1m: List[TradingDay],
                    regime_map: Dict[str, str],
                    picks_5m: Dict[str, Tuple[str, Optional[ParamSet], Optional[dict]]],
                    picks_1m: Dict[str, Tuple[str, Optional[ParamSet], Optional[dict]]]):
    """Run full backtester on both resolutions and compare."""

    config = EngineConfig()

    # Build adaptive config from 5-min picks
    def build_config(picks):
        from run_multi_ticker import build_adaptive_config
        return build_adaptive_config(picks)

    # ── 5-min with 5-min optimized params ──
    config.adaptive = build_config(picks_5m)
    bt5 = IntradayBacktester(config)
    res_5m = bt5.run(days_5m, adaptive=True, regime_map=regime_map)

    # ── 1-min with 5-min optimized params (are 5-min params fragile?) ──
    bt1_old = IntradayBacktester(config)
    res_1m_old = bt1_old.run(days_1m, adaptive=True, regime_map=regime_map)

    # ── 1-min with 1-min optimized params ──
    config.adaptive = build_config(picks_1m)
    bt1_new = IntradayBacktester(config)
    res_1m_new = bt1_new.run(days_1m, adaptive=True, regime_map=regime_map)

    return res_5m, res_1m_old, res_1m_new


def print_triple_comparison(ticker: str, res_5m, res_1m_old, res_1m_new):
    """Side-by-side-by-side comparison."""
    print(f"\n{C_B}{'═' * 86}{C_X}")
    print(f"{C_B}  {ticker} — RESOLUTION COMPARISON{C_X}")
    print(f"{C_B}{'═' * 86}{C_X}\n")

    print(f"  A = 5-min bars + 5-min optimized params (CURRENT)")
    print(f"  B = 1-min bars + 5-min params (REALITY CHECK — are 5-min params fragile?)")
    print(f"  C = 1-min bars + 1-min optimized params (BEST for live trading)")
    print()

    print(f"  {'Metric':<22} {'A (5m/5m)':>14} {'B (1m/5m)':>14} {'C (1m/1m)':>14}")
    print(f"  {'─' * 68}")

    rows = [
        ("Trades",        res_5m.total_trades,     res_1m_old.total_trades,     res_1m_new.total_trades),
        ("Win Rate %",    res_5m.win_rate,         res_1m_old.win_rate,         res_1m_new.win_rate),
        ("Total P&L $",   res_5m.total_pnl,        res_1m_old.total_pnl,        res_1m_new.total_pnl),
        ("Profit Factor", res_5m.profit_factor,     res_1m_old.profit_factor,     res_1m_new.profit_factor),
        ("Max DD %",      res_5m.max_drawdown_pct,  res_1m_old.max_drawdown_pct,  res_1m_new.max_drawdown_pct),
        ("Sharpe Ratio",  res_5m.sharpe_ratio,      res_1m_old.sharpe_ratio,      res_1m_new.sharpe_ratio),
        ("Gamma Exits",   res_5m.gamma_blow_ups,    res_1m_old.gamma_blow_ups,    res_1m_new.gamma_blow_ups),
    ]

    for name, a, b, c in rows:
        if "P&L" in name:
            ac = C_G if a > 0 else C_R
            bc = C_G if b > 0 else C_R
            cc = C_G if c > 0 else C_R
            print(f"  {name:<22} {ac}${a:>12,.2f}{C_X} {bc}${b:>12,.2f}{C_X} {cc}${c:>12,.2f}{C_X}")
        elif "%" in name:
            print(f"  {name:<22} {a:>13.1f}% {b:>13.1f}% {c:>13.1f}%")
        else:
            print(f"  {name:<22} {a:>14.2f} {b:>14.2f} {c:>14.2f}")

    print(f"  {'─' * 68}")

    # Degradation analysis
    if res_5m.total_pnl > 0:
        degradation = (res_5m.total_pnl - res_1m_old.total_pnl) / res_5m.total_pnl * 100
        recovery = (res_1m_new.total_pnl - res_1m_old.total_pnl)
    else:
        degradation = 0
        recovery = 0

    print(f"\n  {C_B}ANALYSIS{C_X}")
    print(f"  {'─' * 68}")

    d_color = C_R if degradation > 20 else C_Y if degradation > 10 else C_G
    print(f"  5m→1m degradation (A→B): {d_color}{degradation:+.1f}%{C_X} P&L drop")
    print(f"  1m re-optimization lift (B→C): {C_G if recovery > 0 else C_R}"
          f"${recovery:>+,.2f}{C_X}")

    if degradation > 30:
        print(f"\n  ⚠️  {C_R}SIGNIFICANT DEGRADATION{C_X} — 5-min results were over-optimistic!")
        print(f"     The stops were getting blown through within 5-min bars.")
        print(f"     USE 1-MIN PARAMS FOR LIVE TRADING.")
    elif degradation > 15:
        print(f"\n  ⚡ {C_Y}MODERATE DEGRADATION{C_X} — some stop blowthrough detected.")
        print(f"     1-min params recommended for more realistic live performance.")
    else:
        print(f"\n  ✅ {C_G}MINIMAL DEGRADATION{C_X} — 5-min results are reasonably accurate.")
        print(f"     1-min data still recommended for precision, but 5-min params hold up.")


# ─── Main ─────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("tickers", nargs="*", default=["SPY", "QQQ"])
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    print(f"\n{C_B}{'═' * 86}{C_X}")
    print(f"{C_B}  1-MINUTE vs 5-MINUTE RESOLUTION TEST{C_X}")
    print(f"{C_B}  Quantifying how much bar resolution matters for 0DTE{C_X}")
    print(f"{C_B}{'═' * 86}{C_X}\n")

    for ticker in args.tickers:
        ticker = ticker.upper()
        print(f"\n{C_C}{'─' * 86}{C_X}")
        print(f"{C_C}  {ticker}{C_X}")
        print(f"{C_C}{'─' * 86}{C_X}\n")

        # Step 1: Load/fetch data
        print(f"  {C_B}STEP 1: Fetch 1-min data{C_X}")
        df_1m = load_or_fetch_1min(ticker, args.days, args.force)
        days_1m = df_to_trading_days(df_1m, ticker)
        print(f"  ✅ {len(days_1m)} trading days @ 1-min")
        avg_bars = np.mean([d.bar_count for d in days_1m])
        print(f"     Avg bars/day: {avg_bars:.0f} (vs ~78 at 5-min)")

        # Load 5-min too
        cache_5m = os.path.join("data", "intraday", f"{ticker}_ibkr_5m_{args.days}d.csv")
        if os.path.exists(cache_5m):
            df_5m = pd.read_csv(cache_5m, index_col=0, parse_dates=True)
            if not isinstance(df_5m.index, pd.DatetimeIndex):
                df_5m.index = pd.to_datetime(df_5m.index, utc=True)
            days_5m_all = df_to_trading_days(df_5m, ticker)

            # Filter 5-min days to only dates that exist in 1-min set
            dates_1m = {td.date for td in days_1m}
            days_5m = [td for td in days_5m_all if td.date in dates_1m]
            print(f"  ✅ {len(days_5m)} trading days @ 5-min (matched to 1-min date range)")
            if not days_5m:
                print(f"  ⚠️  No overlapping dates between 1-min and 5-min data.")
                print(f"     1-min range: {days_1m[0].date} → {days_1m[-1].date}")
                print(f"     5-min range: {days_5m_all[0].date} → {days_5m_all[-1].date}")
                continue
        else:
            print(f"  ⚠️  No 5-min cache for {ticker}. Run run_multi_ticker.py first.")
            continue

        # Step 2: Tag regimes
        print(f"\n  {C_B}STEP 2: Regime Classification{C_X}")
        regime_map = {}
        vix_df = fetch_vix_history()
        vix_dates = {d.date(): float(row.get("close", 18))
                     for d, row in vix_df.iterrows() if hasattr(d, 'date')}
        regime_days_1m = {"GREEN": [], "YELLOW": [], "RED": []}
        for td in days_1m:
            vix_val = 18.0
            for offset in range(5):
                check = td.date - timedelta(days=offset)
                if check in vix_dates:
                    vix_val = vix_dates[check]
                    break
            td._regime = classify_regime(vix_val)
            td._vix = vix_val
            regime_map[str(td.date)] = td._regime
            regime_days_1m[td._regime].append(td)

        # Also tag 5-min days and filter 1-min days to only overlapping dates
        dates_5m = {td.date for td in days_5m}
        days_1m = [td for td in days_1m if td.date in dates_5m]
        regime_days_1m = {"GREEN": [], "YELLOW": [], "RED": []}
        for td in days_1m:
            r = regime_map.get(str(td.date))
            if r:
                td._regime = r
                regime_days_1m[r].append(td)

        for td in days_5m:
            r = regime_map.get(str(td.date))
            if r:
                td._regime = r

        for regime, color in [("GREEN", C_G), ("YELLOW", C_Y), ("RED", C_R)]:
            print(f"  {color}■{C_X} {regime}: {len(regime_days_1m[regime])} days")

        # Step 3: Optimize on 1-min data
        print(f"\n  {C_B}STEP 3: Parameter Optimization on 1-min bars{C_X}")
        grid = QUICK_GRID if args.quick else FULL_GRID
        param_sets = generate_param_sets(grid)
        print(f"  Grid: {len(param_sets)} param combos ({'quick' if args.quick else 'full'})")

        optimal_1m = optimize_1min(regime_days_1m, param_sets)

        # Pick best per regime
        picks_1m = {}
        for regime in ["GREEN", "YELLOW", "RED"]:
            strats = optimal_1m.get(regime, {})
            if not strats:
                picks_1m[regime] = ("none", None, None)
                continue
            best_strat = max(strats.keys(), key=lambda s: strats[s][1].get("score", 0))
            ps, stats = strats[best_strat]
            picks_1m[regime] = (best_strat, ps, stats)

        print(f"\n  {C_B}1-MIN OPTIMAL PICKS{C_X}")
        for regime, color in [("GREEN", C_G), ("YELLOW", C_Y), ("RED", C_R)]:
            strat, ps, stats = picks_1m[regime]
            if ps and strat != "none":
                print(f"  {color}■{C_X} {regime}: {C_B}{strat}{C_X} "
                      f"(Δ={ps.delta:.2f}, stop={ps.stop_mult:.1f}×, "
                      f"TP={ps.profit_target:.0%}, w=${ps.width:.0f})")
            else:
                print(f"  {color}■{C_X} {regime}: {C_R}NO TRADE{C_X}")

        # Load 5-min picks from saved results
        picks_5m_file = f"{ticker.lower()}_optimization_results.json"
        if os.path.exists(picks_5m_file):
            with open(picks_5m_file) as f:
                saved = json.load(f)
            picks_5m = {}
            for regime in ["GREEN", "YELLOW", "RED"]:
                rp = saved.get("regime_picks", {}).get(regime, {})
                strat = rp.get("strategy", "none")
                params = rp.get("params")
                if params and strat != "none":
                    ps = ParamSet(
                        delta=params.get("delta", 0.12),
                        stop_mult=params.get("stop_mult", params.get("stop", 3.0)),
                        profit_target=params.get("profit_target", params.get("tp", 0.75)),
                        width=params.get("width", 2.0),
                        entry_start_min=params.get("entry_start_min", 30),
                        entry_end_min=params.get("entry_end_min", 60),
                        gamma_limit=params.get("gamma_limit", 0.15),
                    )
                    picks_5m[regime] = (strat, ps, rp.get("stats"))
                else:
                    picks_5m[regime] = ("none", None, None)
        else:
            print(f"\n  ⚠️  No 5-min results for {ticker}. Using defaults.")
            from run_multi_ticker import build_adaptive_config
            picks_5m = {
                "GREEN": ("iron_condor",
                           ParamSet(0.12, 3.0, 0.75, 30, 60, 2.0, 0.15), None),
                "YELLOW": ("call_credit",
                            ParamSet(0.18, 3.0, 0.75, 30, 60, 2.0, 0.15), None),
                "RED": ("none", None, None),
            }

        # Step 4: Full comparison
        print(f"\n  {C_B}STEP 4: Full Backtester Comparison{C_X}")
        res_5m, res_1m_old, res_1m_new = run_comparison(
            ticker, days_5m, days_1m, regime_map, picks_5m, picks_1m
        )

        print_triple_comparison(ticker, res_5m, res_1m_old, res_1m_new)

        # Save
        result = {
            "ticker": ticker,
            "test_date": str(date.today()),
            "bars_per_day_1m": round(avg_bars),
            "resolution_5m": {
                "trades": res_5m.total_trades, "win_rate": res_5m.win_rate,
                "total_pnl": res_5m.total_pnl, "profit_factor": res_5m.profit_factor,
                "max_drawdown": res_5m.max_drawdown_pct, "sharpe": res_5m.sharpe_ratio,
            },
            "resolution_1m_5m_params": {
                "trades": res_1m_old.total_trades, "win_rate": res_1m_old.win_rate,
                "total_pnl": res_1m_old.total_pnl, "profit_factor": res_1m_old.profit_factor,
                "max_drawdown": res_1m_old.max_drawdown_pct, "sharpe": res_1m_old.sharpe_ratio,
            },
            "resolution_1m_1m_params": {
                "trades": res_1m_new.total_trades, "win_rate": res_1m_new.win_rate,
                "total_pnl": res_1m_new.total_pnl, "profit_factor": res_1m_new.profit_factor,
                "max_drawdown": res_1m_new.max_drawdown_pct, "sharpe": res_1m_new.sharpe_ratio,
            },
            "picks_1m": {
                regime: {
                    "strategy": strat,
                    "params": {"delta": ps.delta, "stop_mult": ps.stop_mult,
                               "profit_target": ps.profit_target, "width": ps.width,
                               "entry_window": f"{ps.entry_start_min}-{ps.entry_end_min}m",
                               "gamma_limit": ps.gamma_limit} if ps else None,
                }
                for regime, (strat, ps, _) in picks_1m.items()
            },
        }
        out = f"{ticker.lower()}_1m_resolution_results.json"
        with open(out, "w") as f:
            json.dump(result, f, indent=2)
        print(f"\n  💾 Saved to {out}")

    print(f"\n{C_B}{'═' * 86}{C_X}\n")


if __name__ == "__main__":
    main()

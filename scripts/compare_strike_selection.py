#!/usr/bin/env python
"""
Validate _best_credit_per_delta_short() vs _nearest_delta_strike().

Builds a synthetic put chain (SPY-like skew, 45 DTE, VIX=18) and runs both
strike selection methods side-by-side. Shows which strike each picks, the net
spread credit (short_mid - long_mid), and the credit-per-delta ratio.

Usage:
    python scripts/compare_strike_selection.py
    python scripts/compare_strike_selection.py --spot 500 --iv 0.20 --dte 45
"""

from __future__ import annotations

import argparse
import math
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from agora.backtester.synthetic_pricing import bs_greeks, bs_price, skewed_sigma


def build_synthetic_chain(
    spot: float,
    sigma_atm: float,
    dte: int,
    ticker: str = "SPY",
    n_strikes: int = 30,
    opt_type: str = "put",
) -> pd.DataFrame:
    """
    Build a synthetic options chain DataFrame with realistic bid/ask spread.
    Columns: strike, mid, bid, ask, delta, impliedVolatility
    """
    T = dte / 365.0
    # Strike range: [spot * 0.80 .. spot * 0.99] for puts, [spot*1.01 .. spot*1.20] for calls
    if opt_type == "put":
        lo, hi = spot * 0.80, spot * 0.99
    else:
        lo, hi = spot * 1.01, spot * 1.20

    step = (hi - lo) / n_strikes
    rows = []
    for i in range(n_strikes + 1):
        K = round(lo + i * step, 1)
        iv = skewed_sigma(spot, K, sigma_atm, ticker)
        mid = bs_price(spot, K, T, 0.05, iv, opt_type)
        greeks = bs_greeks(spot, K, T, 0.05, iv, opt_type)
        half_spread = max(0.02, mid * 0.025)   # ~2.5% half-spread (realistic)
        bid = round(max(0.01, mid - half_spread), 2)
        ask = round(mid + half_spread, 2)
        rows.append({
            "strike":            K,
            "mid":               round(mid, 4),
            "bid":               bid,
            "ask":               ask,
            "delta":             greeks["delta"],
            "impliedVolatility": round(iv, 4),
        })
    return pd.DataFrame(rows)


def run_comparison(
    spot: float,
    sigma_atm: float,
    dte: int,
    ticker: str = "SPY",
    target_delta: float = 0.20,
    delta_band: float = 0.05,
) -> None:
    from agora.strategies.rules_engine import StrategyRulesEngine
    from unittest.mock import MagicMock

    settings = MagicMock()
    settings.short_delta_target = target_delta
    settings.long_delta_target  = 0.35
    settings.min_credit_spread_rr_ratio = 0.5
    settings.min_rr_ratio = 0.5

    engine = StrategyRulesEngine.__new__(StrategyRulesEngine)
    engine._settings = settings

    expiry = date.today() + timedelta(days=dte)
    chain  = build_synthetic_chain(spot, sigma_atm, dte, ticker, opt_type="put")
    otm    = chain[chain["strike"] < spot * 0.995].copy()

    if otm.empty:
        print("ERROR: No OTM puts in chain. Check parameters.")
        return

    # ── Method A: nearest-delta (legacy) ──────────────────────────────────────
    short_a = engine._nearest_delta_strike(otm, target_delta, "put", spot, expiry)

    # ── Method B: credit-per-delta (new) ──────────────────────────────────────
    short_b = engine._best_credit_per_delta_short(
        otm, target_delta, "put", wing_direction=-1, spot=spot, expiry=expiry, delta_band=delta_band
    )

    def spread_stats(short_strike: float | None, label: str) -> dict:
        if short_strike is None:
            print(f"  {label}: no strike selected")
            return {}
        long_strike = engine._spread_width_strike(otm, short_strike, "put", -1)
        if long_strike is None:
            print(f"  {label}: no long strike found")
            return {}

        short_row = chain[chain["strike"] == short_strike].iloc[0]
        long_row  = chain[chain["strike"] == long_strike].iloc[0]

        short_mid = (short_row["bid"] + short_row["ask"]) / 2
        long_mid  = (long_row["bid"]  + long_row["ask"])  / 2
        net_credit  = short_mid - long_mid
        delta_abs   = abs(short_row["delta"])
        cpd         = net_credit / delta_abs if delta_abs > 0 else 0.0
        width_pct   = (short_strike - long_strike) / spot * 100

        return {
            "label":        label,
            "short":        short_strike,
            "long":         long_strike,
            "short_mid":    round(short_mid,  4),
            "long_mid":     round(long_mid,   4),
            "net_credit":   round(net_credit, 4),
            "delta_abs":    round(delta_abs,  4),
            "credit/delta": round(cpd,        4),
            "width_pct":    round(width_pct,  2),
        }

    a = spread_stats(short_a, "Nearest-delta (legacy)")
    b = spread_stats(short_b, "Credit-per-delta (new) ")

    print(f"\n{'='*65}")
    print(f"  Strike Selection Comparison — {ticker}  |  S={spot}  σ={sigma_atm:.0%}  DTE={dte}")
    print(f"{'='*65}")
    print(f"  Target delta: {target_delta:.2f}  |  Search band: ±{delta_band:.2f}")
    print(f"{'─'*65}")
    header = f"  {'Method':<26} {'Short':>7} {'Long':>7} {'Credit':>8} {'Δ':>7} {'C/Δ':>8} {'Width%':>7}"
    print(header)
    print(f"{'─'*65}")

    for stats in [a, b]:
        if not stats:
            continue
        print(
            f"  {stats['label']:<26} "
            f"{stats['short']:>7.1f} "
            f"{stats['long']:>7.1f} "
            f"${stats['net_credit']:>7.4f} "
            f"{stats['delta_abs']:>7.4f} "
            f"{stats['credit/delta']:>8.4f} "
            f"{stats['width_pct']:>6.2f}%"
        )

    if a and b:
        cpd_delta = b["credit/delta"] - a["credit/delta"]
        credit_delta = b["net_credit"] - a["net_credit"]
        print(f"{'─'*65}")
        sign = "+" if cpd_delta >= 0 else ""
        print(f"  Credit/delta improvement : {sign}{cpd_delta:.4f}  ({sign}{cpd_delta/a['credit/delta']*100:.1f}%)")
        sign = "+" if credit_delta >= 0 else ""
        print(f"  Net credit improvement   : {sign}${credit_delta:.4f}/contract  ({sign}${credit_delta*100:.2f} per 100sh)")

    print(f"{'='*65}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare strike selection methods")
    parser.add_argument("--spot",   type=float, default=500.0,  help="Spot price")
    parser.add_argument("--iv",     type=float, default=0.18,   help="ATM IV (annualized, e.g. 0.18)")
    parser.add_argument("--dte",    type=int,   default=45,     help="Days to expiry")
    parser.add_argument("--ticker", type=str,   default="SPY",  help="Ticker (affects skew)")
    parser.add_argument("--delta",  type=float, default=0.20,   help="Target short delta")
    parser.add_argument("--band",   type=float, default=0.05,   help="Search band around target")
    args = parser.parse_args()

    # Run for requested params
    run_comparison(args.spot, args.iv, args.dte, args.ticker, args.delta, args.band)

    # Also run a low-vol scenario (tight spreads) for contrast
    if args.spot == 500.0 and args.iv == 0.18:
        print("Bonus scenario — high-vol (VIX=30, σ=0.30):")
        run_comparison(args.spot, 0.30, args.dte, args.ticker, args.delta, args.band)


if __name__ == "__main__":
    main()

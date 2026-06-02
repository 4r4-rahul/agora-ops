"""
agora/ops/skew_analyzer.py — Options IV Skew Analyzer

Analyzes the volatility skew of an options chain to classify the current
options market structure and provide strategy guidance.

Key concepts:
  • Normal put skew: puts carry higher IV than equidistant calls because
    equity markets have fat left tails (crash risk) → sellers demand premium.
  • Call skew: unusual; arises in meme/takeover names or supply-constrained
    borrow situations. Calls are expensive relative to puts.
  • Steep put skew (>10%): IV differential is very large; puts are "rich".
    Bullish traders should prefer selling puts (put credit spreads / cash
    secured puts) over buying calls — the put premium finances the trade
    at a favourable level.

Skew measurement:
  25-delta put IV  minus  25-delta call IV  (in IV percentage points)
  skew_pct = skew / call_25d_iv

Delta selection logic:
  1. If the chain dataframe has a `delta` column: find the strike whose
     |delta| is closest to 0.25 on each side.
  2. If no delta column: use Black-Scholes approximation to derive the
     25-delta strike from the ATM IV and spot price.

References:
  • Natenberg, "Option Volatility & Pricing" ch.17 — skewness and kurtosis
  • Gatheral, "The Volatility Surface" ch.1 — parametric skew models
  • Sinclair, "Volatility Trading" ch.7 — trading the skew
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


# ── Output dataclass ───────────────────────────────────────────────────────────
@dataclass
class SkewResult:
    call_25d_iv: float           # call-side 25Δ implied volatility (0-1 scale)
    put_25d_iv: float            # put-side  25Δ implied volatility (0-1 scale)
    skew: float                  # put_25d_iv - call_25d_iv (raw IV difference)
    skew_pct: float              # skew / call_25d_iv (normalised)
    skew_regime: str             # "call_skewed" | "flat" | "normal_put_skew" | "steep_put_skew"
    strategy_bias: str           # "prefer_put_credit" | "prefer_call_credit" | "neutral"
    long_put_penalty: int        # contracts to reduce (steep_put_skew → 1, else 0)
    conviction_adj: int          # +3 if steep put skew and bullish trade
    reason_str: str


# ── Black-Scholes helpers ──────────────────────────────────────────────────────

def _norm_cdf(x: float) -> float:
    """Standard normal CDF (Abramowitz & Stegun approximation)."""
    t = 1.0 / (1.0 + 0.2316419 * abs(x))
    poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))))
    prob = 1.0 - (1.0 / math.sqrt(2.0 * math.pi)) * math.exp(-0.5 * x * x) * poly
    return prob if x >= 0 else 1.0 - prob


def _bs_delta(spot: float, strike: float, iv: float, dte_years: float,
              is_call: bool, r: float = 0.05) -> float:
    """Black-Scholes delta for a European option."""
    if spot <= 0 or strike <= 0 or iv <= 0 or dte_years <= 0:
        return 0.0
    try:
        d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * dte_years) / (iv * math.sqrt(dte_years))
        if is_call:
            return _norm_cdf(d1)
        else:
            return _norm_cdf(d1) - 1.0   # put delta is negative
    except (ValueError, ZeroDivisionError):
        return 0.0


def _bs_implied_25delta_strike(spot: float, iv_atm: float, dte_years: float,
                                is_call: bool, r: float = 0.05) -> float:
    """
    Approximate the 25-delta strike by bisection.
    Returns 0.0 if computation fails.
    """
    if spot <= 0 or iv_atm <= 0 or dte_years <= 0:
        return 0.0
    try:
        target_delta = 0.25 if is_call else -0.25
        lo, hi = spot * 0.50, spot * 2.00
        for _ in range(40):
            mid = (lo + hi) / 2.0
            d = _bs_delta(spot, mid, iv_atm, dte_years, is_call, r)
            if is_call:
                if d > target_delta:
                    lo = mid
                else:
                    hi = mid
            else:
                if d < target_delta:
                    lo = mid
                else:
                    hi = mid
        return (lo + hi) / 2.0
    except Exception:
        return 0.0


# ── Nearest expiry to target DTE ──────────────────────────────────────────────

def _pick_expiry(options_chain: dict[str, Any], target_dte: int = 30) -> str | None:
    """
    Select the expiry string from options_chain whose DTE is closest to *target_dte*.
    Keys must be parseable as date strings (e.g. '2025-08-15').
    """
    today = datetime.now(tz=timezone.utc).date()
    best_key: str | None = None
    best_diff = 10_000

    for exp_str in options_chain:
        try:
            exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
            dte = (exp_date - today).days
            if dte < 0:
                continue
            diff = abs(dte - target_dte)
            if diff < best_diff:
                best_diff = diff
                best_key = exp_str
        except ValueError:
            continue

    return best_key


# ── Regime classification ─────────────────────────────────────────────────────

def _classify_regime(skew_pct: float) -> tuple[str, str, int, int]:
    """
    Returns (skew_regime, strategy_bias, long_put_penalty, conviction_adj).
    conviction_adj is applied when the trade is bullish (handled by caller).
    """
    if skew_pct < 0:
        return "call_skewed", "prefer_call_credit", 0, 0
    elif skew_pct < 0.03:
        return "flat", "neutral", 0, 0
    elif skew_pct < 0.10:
        return "normal_put_skew", "neutral", 0, 0
    else:
        # Steep put skew — puts are rich → selling puts is advantageous
        return "steep_put_skew", "prefer_put_credit", 1, +3


# ── Main analysis function ────────────────────────────────────────────────────

def analyze_skew(options_chain: dict[str, Any], spot: float) -> SkewResult | None:
    """
    Analyze the IV skew of *options_chain* near 30 DTE.

    Parameters
    ----------
    options_chain:
        Dict of {expiry_str: {"calls": pd.DataFrame, "puts": pd.DataFrame}}.
        DataFrames are expected to have 'strike' and 'impliedVolatility' columns;
        'delta' is optional but preferred for accurate 25Δ selection.
    spot:
        Current underlying price in USD.

    Returns
    -------
    SkewResult or None if insufficient data.
    """
    if not options_chain or spot <= 0:
        return None

    expiry = _pick_expiry(options_chain, target_dte=30)
    if expiry is None:
        logger.debug("Skew analyzer: no valid expiry found in chain")
        return None

    chain = options_chain[expiry]
    calls_df = chain.get("calls")
    puts_df  = chain.get("puts")

    if calls_df is None or puts_df is None:
        logger.debug("Skew analyzer: missing calls or puts for expiry %s", expiry)
        return None

    try:
        import pandas as pd
        calls_df = pd.DataFrame(calls_df) if not hasattr(calls_df, "columns") else calls_df
        puts_df  = pd.DataFrame(puts_df)  if not hasattr(puts_df,  "columns") else puts_df
    except Exception as exc:
        logger.debug("Skew analyzer: dataframe coercion failed: %s", exc)
        return None

    if "strike" not in calls_df.columns or "impliedVolatility" not in calls_df.columns:
        logger.debug("Skew analyzer: required columns missing from chain")
        return None

    # Parse DTE for B-S approx
    today = datetime.now(tz=timezone.utc).date()
    try:
        exp_date = datetime.strptime(expiry, "%Y-%m-%d").date()
        dte_days = max((exp_date - today).days, 1)
    except ValueError:
        dte_days = 30
    dte_years = dte_days / 365.0

    call_25d_iv: float | None = None
    put_25d_iv:  float | None = None

    # ── Try using delta column (preferred) ────────────────────────────────────
    if "delta" in calls_df.columns and "delta" in puts_df.columns:
        try:
            calls_clean = calls_df[["strike", "impliedVolatility", "delta"]].dropna()
            puts_clean  = puts_df[["strike",  "impliedVolatility", "delta"]].dropna()

            if len(calls_clean) >= 3:
                calls_clean = calls_clean.copy()
                calls_clean["_ddiff"] = (calls_clean["delta"].abs() - 0.25).abs()
                best_call = calls_clean.nsmallest(1, "_ddiff").iloc[0]
                call_25d_iv = float(best_call["impliedVolatility"])

            if len(puts_clean) >= 3:
                puts_clean = puts_clean.copy()
                puts_clean["_ddiff"] = (puts_clean["delta"].abs() - 0.25).abs()
                best_put = puts_clean.nsmallest(1, "_ddiff").iloc[0]
                put_25d_iv = float(best_put["impliedVolatility"])
        except Exception as exc:
            logger.debug("Skew analyzer: delta-based selection failed: %s", exc)

    # ── Fallback: Black-Scholes approximation ─────────────────────────────────
    if call_25d_iv is None or put_25d_iv is None:
        try:
            calls_clean = calls_df[["strike", "impliedVolatility"]].dropna()
            puts_clean  = puts_df[["strike",  "impliedVolatility"]].dropna()

            if len(calls_clean) < 3 or len(puts_clean) < 3:
                logger.debug("Skew analyzer: insufficient chain data")
                return None

            # ATM IV approximation from closest-to-spot call
            atm_calls = calls_clean.copy()
            atm_calls["_sdiff"] = (atm_calls["strike"] - spot).abs()
            atm_iv_row = atm_calls.nsmallest(1, "_sdiff").iloc[0]
            iv_atm = float(atm_iv_row["impliedVolatility"])

            if iv_atm <= 0:
                logger.debug("Skew analyzer: ATM IV is zero/negative")
                return None

            # 25Δ call strike (OTM call → strike > spot)
            call_25d_strike = _bs_implied_25delta_strike(spot, iv_atm, dte_years, is_call=True)
            if call_25d_strike > 0:
                calls_clean = calls_clean.copy()
                calls_clean["_sdiff"] = (calls_clean["strike"] - call_25d_strike).abs()
                best_call_row = calls_clean.nsmallest(1, "_sdiff").iloc[0]
                call_25d_iv = float(best_call_row["impliedVolatility"])

            # 25Δ put strike (OTM put → strike < spot)
            put_25d_strike = _bs_implied_25delta_strike(spot, iv_atm, dte_years, is_call=False)
            if put_25d_strike > 0:
                puts_clean = puts_clean.copy()
                puts_clean["_sdiff"] = (puts_clean["strike"] - put_25d_strike).abs()
                best_put_row = puts_clean.nsmallest(1, "_sdiff").iloc[0]
                put_25d_iv = float(best_put_row["impliedVolatility"])
        except Exception as exc:
            logger.debug("Skew analyzer: BS fallback failed: %s", exc)

    if call_25d_iv is None or put_25d_iv is None:
        logger.debug("Skew analyzer: could not determine 25Δ IVs for expiry %s", expiry)
        return None

    if call_25d_iv <= 0:
        logger.debug("Skew analyzer: call_25d_iv=%.4f is non-positive", call_25d_iv)
        return None

    skew     = put_25d_iv - call_25d_iv
    skew_pct = skew / call_25d_iv

    regime, strategy_bias, long_put_penalty, conviction_adj = _classify_regime(skew_pct)

    reason_str = (
        f"Expiry {expiry} ({dte_days}DTE) | "
        f"call_25Δ_IV={call_25d_iv:.1%} put_25Δ_IV={put_25d_iv:.1%} | "
        f"skew={skew:+.3f} ({skew_pct:+.1%}) → {regime}"
    )

    logger.debug(
        "Skew [%s] regime=%s bias=%s adj=%+d | %s",
        expiry, regime, strategy_bias, conviction_adj, reason_str,
    )

    return SkewResult(
        call_25d_iv=call_25d_iv,
        put_25d_iv=put_25d_iv,
        skew=skew,
        skew_pct=skew_pct,
        skew_regime=regime,
        strategy_bias=strategy_bias,
        long_put_penalty=long_put_penalty,
        conviction_adj=conviction_adj,
        reason_str=reason_str,
    )

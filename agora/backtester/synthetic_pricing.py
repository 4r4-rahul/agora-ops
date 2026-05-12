"""
Black-Scholes synthetic options pricing for backtesting — Level 2.

Level 2 additions vs Level 1:
  - IV skew model: log-linear strike skew calibrated per asset class
    (index ETFs carry pronounced put skew; commodities/rates are near-flat)
  - Term structure: variance-weighted interpolation using VIX9D / VIX / VIX3M
  - Bid-ask slippage: per-ticker half-spread applied at entry fills

The skew means OTM puts carry higher IV than OTM calls, so:
  - Bull_put_spread credits are slightly lower than flat-smile estimates
  - Iron condor put-leg credits are reduced (more realistic)
  - Bear_call_spread credits are slightly higher (calls are cheaper)

These changes tighten P&L estimates toward real-world execution quality.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# ── Level 2: Skew and slippage parameters ─────────────────────────────────────

# Log-linear skew slope per ticker.  iv(K) = atm_iv × exp(slope × ln(K/S))
# Negative slope → puts (K < S, ln(K/S) < 0) get HIGHER iv than calls.
# Calibrated to approximate typical 25-delta risk reversal for each asset class.
_SKEW_SLOPE: dict[str, float] = {
    # Equity index ETFs: pronounced put skew (~3-5 vol pts per 10% OTM)
    "SPY": -1.20, "QQQ": -1.30, "IWM": -1.00,
    "XLE": -0.90, "XLF": -0.80, "XLU": -0.60,
    # Rates ETFs: modest skew
    "TLT": -0.50, "IEF": -0.40, "AGG": -0.30,
    # Commodities: mild skew (gold puts slightly richer than calls)
    "GLD": -0.40, "GDX": -0.50, "SLV": -0.45,
}

# Half bid-ask spread as fraction of mid — applied at entry and exit.
# Liquid ETF options: SPY/QQQ tighter, smaller ETFs wider.
_BID_ASK_HALF_SPREAD: dict[str, float] = {
    "SPY": 0.020, "QQQ": 0.022, "IWM": 0.030,
    "GLD": 0.035, "GDX": 0.050, "SLV": 0.045,
    "TLT": 0.030, "IEF": 0.040, "AGG": 0.045,
    "XLE": 0.040, "XLF": 0.040, "XLU": 0.045,
}
_DEFAULT_HALF_SPREAD = 0.040   # fallback for unlisted tickers


@dataclass
class BSLeg:
    option_type: str    # "call" | "put"
    strike: float
    mid_price: float
    delta: float
    gamma: float
    theta: float
    vega: float


def bs_price(
    S: float,
    K: float,
    T: float,       # time to expiry in years
    r: float,       # risk-free rate (annual)
    sigma: float,   # annualized vol
    option_type: str = "call",
) -> float:
    """Black-Scholes option price. Returns 0 if inputs invalid."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        if option_type == "call":
            return max(0.0, S - K)
        return max(0.0, K - S)

    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    nd1, nd2 = _norm_cdf(d1), _norm_cdf(d2)

    if option_type == "call":
        return S * nd1 - K * math.exp(-r * T) * nd2
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def bs_greeks(
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    option_type: str = "call",
) -> dict[str, float]:
    """Returns delta, gamma, theta, vega."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}

    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    nd1 = _norm_pdf(d1)
    norm_d1 = _norm_cdf(d1)

    delta = norm_d1 if option_type == "call" else norm_d1 - 1.0
    gamma = nd1 / (S * sigma * math.sqrt(T))
    theta = (
        -(S * nd1 * sigma) / (2 * math.sqrt(T))
        - r * K * math.exp(-r * T) * _norm_cdf(d2 if option_type == "call" else -d2)
    ) / 365.0
    vega = S * nd1 * math.sqrt(T) / 100   # per 1% vol move

    return {
        "delta": round(delta, 4),
        "gamma": round(gamma, 6),
        "theta": round(theta, 4),
        "vega":  round(vega, 4),
    }


def strike_for_delta(
    S: float,
    T: float,
    sigma: float,
    target_delta: float,
    option_type: str = "call",
    r: float = 0.05,
) -> float:
    """
    Find the strike that gives approximately target_delta.
    Uses BS analytic inverse: K = S × exp(-d1 × σ√T + (r + 0.5σ²)T)
    where d1 = N_inv(target_delta) for calls, N_inv(target_delta + 1) for puts.
    """
    if T <= 0 or sigma <= 0:
        return S

    if option_type == "put":
        target_delta = 1.0 - target_delta  # puts: Δ_put = N(d1)-1; for |Δ|=0.20 → N(d1)=0.80

    target_delta = max(0.01, min(0.99, target_delta))
    d1 = _norm_inv(target_delta)
    K = S * math.exp(-d1 * sigma * math.sqrt(T) + (r + 0.5 * sigma ** 2) * T)
    return round(K, 2)


def skewed_sigma(S: float, K: float, sigma_atm: float, ticker: str = "") -> float:
    """
    IV at strike K via log-linear skew.  iv(K) = atm_iv × exp(slope × ln(K/S)).
    Returns atm_iv unchanged when ticker has no skew entry (slope=0).
    """
    slope = _SKEW_SLOPE.get(ticker, 0.0)
    if slope == 0.0 or K <= 0 or S <= 0:
        return sigma_atm
    return sigma_atm * math.exp(slope * math.log(K / S))


def term_structure_sigma(
    dte: int,
    vix: float,
    vix9d: float | None = None,
    vix3m: float | None = None,
) -> float:
    """
    Variance-weighted interpolation of forward vol for a given DTE.
    Inputs are VIX-scale (e.g. 20.5, not 0.205).  Returns annualised sigma.

    Anchor points used: VIX9D (9-day), VIX (30-day), VIX3M (90-day).
    Falls back gracefully when optional inputs are None.
    """
    v9  = vix9d  if vix9d  is not None else vix * 0.90
    v90 = vix3m  if vix3m  is not None else vix * 1.05

    if dte <= 9:
        return v9 / 100.0
    elif dte <= 30:
        # Interpolate between VIX9D and VIX in variance space
        var = (v9 ** 2 * 9 + vix ** 2 * (dte - 9)) / dte
    else:
        # Interpolate between VIX and VIX3M in variance space
        var = (vix ** 2 * 30 + v90 ** 2 * (dte - 30)) / dte

    return math.sqrt(max(1e-6, var)) / 100.0


def entry_slippage(ticker: str) -> float:
    """Half bid-ask spread at entry (fraction of mid).  Apply as: credit × (1 - slip)."""
    return _BID_ASK_HALF_SPREAD.get(ticker, _DEFAULT_HALF_SPREAD)


def build_spread(
    S: float,
    T_years: float,
    sigma: float,
    strategy: str,
    short_delta: float = 0.20,
    long_delta: float = 0.35,
    r: float = 0.05,
    ticker: str = "",      # Level 2: enables per-strike skew
) -> dict[str, float]:
    """
    Build a synthetic spread and return pricing/P&L parameters.

    Returns dict:
      entry_credit_debit: float    (positive = debit, negative = credit per share)
      short_strike, long_strike: float
      max_loss_dollars: float per contract (×100)
      max_gain_dollars: float per contract
      reward_risk_ratio: float
    """
    if strategy in ("bull_put_spread", "bear_call_spread", "iron_condor"):
        # Credit spreads — sell short_delta, buy wing
        if strategy in ("bull_put_spread", "iron_condor"):
            # Short put at short_delta below spot
            short_k = strike_for_delta(S, T_years, sigma, short_delta, "put", r)
            # Long put at 10-delta (further OTM)
            long_k  = strike_for_delta(S, T_years, sigma, 0.10, "put", r)
            short_k = max(short_k, long_k + 0.01)   # ensure ordering

            # Level 2: use per-strike skewed IV for realistic put-spread pricing
            short_price = bs_price(S, short_k, T_years, r, skewed_sigma(S, short_k, sigma, ticker), "put")
            long_price  = bs_price(S, long_k,  T_years, r, skewed_sigma(S, long_k,  sigma, ticker), "put")
            credit = short_price - long_price
            width  = short_k - long_k
            max_gain = max(0.01, credit * 100)
            max_loss = max(0.01, (width - credit) * 100)

            if strategy == "iron_condor":
                # Add bear call spread side (mirror)
                short_c = strike_for_delta(S, T_years, sigma, short_delta, "call", r)
                long_c  = strike_for_delta(S, T_years, sigma, 0.10, "call", r)
                short_c = min(short_c, long_c - 0.01)
                short_cp = bs_price(S, short_c, T_years, r, skewed_sigma(S, short_c, sigma, ticker), "call")
                long_cp  = bs_price(S, long_c,  T_years, r, skewed_sigma(S, long_c,  sigma, ticker), "call")
                call_credit = short_cp - long_cp
                credit += call_credit
                max_gain = credit * 100
                # max_loss = wider side minus total credit
                max_loss = max(0.01, (max(width, long_c - short_c) - credit) * 100)
                return {
                    "entry_credit_debit": -credit,
                    "short_strike":  short_k,
                    "long_strike":   long_k,
                    "call_short_strike": short_c,
                    "call_long_strike":  long_c,
                    "max_loss_dollars":  max_loss,
                    "max_gain_dollars":  max_gain,
                    "reward_risk_ratio": round(max_gain / max_loss, 3),
                    "option_type":   "put",
                }

        else:  # bear_call_spread
            short_k = strike_for_delta(S, T_years, sigma, short_delta, "call", r)
            long_k  = strike_for_delta(S, T_years, sigma, 0.10, "call", r)
            long_k  = max(long_k, short_k + 0.01)

            short_price = bs_price(S, short_k, T_years, r, skewed_sigma(S, short_k, sigma, ticker), "call")
            long_price  = bs_price(S, long_k,  T_years, r, skewed_sigma(S, long_k,  sigma, ticker), "call")
            credit = short_price - long_price
            width  = long_k - short_k
            max_gain = max(0.01, credit * 100)
            max_loss = max(0.01, (width - credit) * 100)

        return {
            "entry_credit_debit": -credit,  # negative = credit received
            "short_strike":  short_k,
            "long_strike":   long_k,
            "max_loss_dollars":  max_loss,
            "max_gain_dollars":  max_gain,
            "reward_risk_ratio": round(max_gain / max_loss, 3),
            "option_type":   "put" if strategy != "bear_call_spread" else "call",
        }

    else:
        # Debit spreads (bull_call_spread, bear_put_spread)
        if strategy == "bull_call_spread":
            long_k  = strike_for_delta(S, T_years, sigma, long_delta, "call", r)
            short_k = strike_for_delta(S, T_years, sigma, short_delta, "call", r)
            short_k = max(short_k, long_k + 0.01)

            long_p  = bs_price(S, long_k,  T_years, r, skewed_sigma(S, long_k,  sigma, ticker), "call")
            short_p = bs_price(S, short_k, T_years, r, skewed_sigma(S, short_k, sigma, ticker), "call")
        else:  # bear_put_spread
            long_k  = strike_for_delta(S, T_years, sigma, long_delta, "put", r)
            short_k = strike_for_delta(S, T_years, sigma, short_delta, "put", r)
            long_k  = max(long_k, short_k + 0.01)

            long_p  = bs_price(S, long_k,  T_years, r, skewed_sigma(S, long_k,  sigma, ticker), "put")
            short_p = bs_price(S, short_k, T_years, r, skewed_sigma(S, short_k, sigma, ticker), "put")

        debit  = long_p - short_p
        width  = abs(long_k - short_k)
        max_loss = max(0.01, debit * 100)
        max_gain = max(0.01, (width - debit) * 100)

        return {
            "entry_credit_debit": debit,
            "short_strike":  short_k,
            "long_strike":   long_k,
            "max_loss_dollars":  max_loss,
            "max_gain_dollars":  max_gain,
            "reward_risk_ratio": round(max_gain / max_loss, 3),
            "option_type":   "put" if strategy == "bear_put_spread" else "call",
        }


def mark_spread(
    S: float,
    T_years: float,
    sigma: float,
    short_strike: float,
    long_strike: float,
    option_type: str,  # "call" | "put"
    strategy: str,
    r: float = 0.05,
    ticker: str = "",   # Level 2: enables per-strike skew in mark
) -> float:
    """
    Re-price the spread at current S, T, sigma.
    Returns current spread value per share.
    """
    if T_years <= 0:
        # At expiry: pure intrinsic
        if option_type == "put":
            return max(0.0, short_strike - S) - max(0.0, long_strike - S)
        else:
            return max(0.0, S - short_strike) - max(0.0, S - long_strike)

    short_p = bs_price(S, short_strike, T_years, r, skewed_sigma(S, short_strike, sigma, ticker), option_type)
    long_p  = bs_price(S, long_strike,  T_years, r, skewed_sigma(S, long_strike,  sigma, ticker), option_type)

    if strategy in ("bull_put_spread", "iron_condor"):
        # For credit puts: position value = long_p - short_p (profit when spread narrows)
        return short_p - long_p   # net credit remaining
    elif strategy == "bear_call_spread":
        return short_p - long_p
    elif strategy == "bull_call_spread":
        return long_p - short_p   # debit spread: value of spread
    else:  # bear_put_spread
        return long_p - short_p


# ── Math helpers ───────────────────────────────────────────────────────────

def _norm_cdf(x: float) -> float:
    """Standard normal CDF — Abramowitz & Stegun approximation."""
    if x < 0:
        return 1.0 - _norm_cdf(-x)
    t = 1.0 / (1.0 + 0.2316419 * x)
    poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))))
    return 1.0 - _norm_pdf(x) * poly


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def _norm_inv(p: float) -> float:
    """Rational approximation for N_inv (Beasley-Springer-Moro)."""
    p = max(1e-8, min(1 - 1e-8, p))
    if p < 0.5:
        return -_norm_inv_pos(p)
    return _norm_inv_pos(1 - p)


def _norm_inv_pos(p: float) -> float:
    a = [2.515517, 0.802853, 0.010328]
    b = [1.432788, 0.189269, 0.001308]
    if p >= 0.5:
        p = 1 - p
    t = math.sqrt(-2 * math.log(p))
    num = a[0] + a[1] * t + a[2] * t * t
    den = 1 + b[0] * t + b[1] * t * t + b[2] * t * t * t
    return t - num / den

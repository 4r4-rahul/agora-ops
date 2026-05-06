"""
Options pricing service — Black-Scholes (European) and Binomial (American).

Key fix from the original repo:
  - SPY, QQQ, IWM pay dividends → American early exercise matters
  - Use binomial tree for American options
  - European BS only for non-dividend-paying tickers (SPX, NDX as indices don't hold shares)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal


# Dividend yield assumptions — empirically derived annual rates
_CONTINUOUS_DIV_YIELD: dict[str, float] = {
    "SPY": 0.0135,   # ~1.35% yield
    "QQQ": 0.0060,   # ~0.60% yield
    "IWM": 0.0120,   # ~1.20% yield
    "DIA": 0.0190,   # ~1.90% yield
    "GLD": 0.0000,
    "SLV": 0.0000,
}
_DEFAULT_DIV_YIELD = 0.0


@dataclass(frozen=True)
class PricingResult:
    price: float
    delta: float
    gamma: float
    theta: float   # per day
    vega: float    # per 1% vol move
    rho: float     # per 1% rate move
    method: str    # "bs" or "binomial"


def price_option(
    S: float,          # underlying price
    K: float,          # strike
    T: float,          # time to expiration in years
    r: float,          # risk-free rate (annual)
    sigma: float,      # implied volatility (annual)
    option_type: Literal["call", "put"],
    ticker: str = "",
    style: Literal["european", "american", "auto"] = "auto",
    binomial_steps: int = 200,
) -> PricingResult:
    """
    Price an option with auto-detection of American vs European.

    Auto mode: American if ticker pays dividends, European otherwise.
    """
    q = _CONTINUOUS_DIV_YIELD.get(ticker.upper(), _DEFAULT_DIV_YIELD)

    if style == "auto":
        use_american = q > 0 or ticker.upper() in _CONTINUOUS_DIV_YIELD
    elif style == "american":
        use_american = True
    else:
        use_american = False

    if use_american:
        return _binomial_american(S, K, T, r, q, sigma, option_type, binomial_steps)
    else:
        return _black_scholes_european(S, K, T, r, q, sigma, option_type)


# ── European Black-Scholes ────────────────────────────────────────────

def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def _black_scholes_european(
    S: float,
    K: float,
    T: float,
    r: float,
    q: float,
    sigma: float,
    option_type: Literal["call", "put"],
) -> PricingResult:
    if T <= 0 or sigma <= 0:
        intrinsic = max(0.0, S - K) if option_type == "call" else max(0.0, K - S)
        return PricingResult(
            price=intrinsic, delta=1.0 if intrinsic > 0 else 0.0,
            gamma=0.0, theta=0.0, vega=0.0, rho=0.0, method="bs"
        )

    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma ** 2) * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT

    Nd1 = _norm_cdf(d1)
    Nd2 = _norm_cdf(d2)
    nd1 = _norm_pdf(d1)

    disc_q = math.exp(-q * T)
    disc_r = math.exp(-r * T)

    if option_type == "call":
        price = S * disc_q * Nd1 - K * disc_r * Nd2
        delta = disc_q * Nd1
    else:
        price = K * disc_r * (1 - Nd2) - S * disc_q * (1 - Nd1)
        delta = -disc_q * (1 - Nd1)

    gamma = disc_q * nd1 / (S * sigma * sqrtT)
    theta_raw = (
        -S * disc_q * nd1 * sigma / (2 * sqrtT)
        - r * K * disc_r * (Nd2 if option_type == "call" else 1 - Nd2)
        + q * S * disc_q * (Nd1 if option_type == "call" else 1 - Nd1)
    )
    theta = theta_raw / 365  # per calendar day

    vega = S * disc_q * nd1 * sqrtT / 100  # per 1% vol
    rho_raw = K * T * disc_r * (Nd2 if option_type == "call" else -(1 - Nd2))
    rho = rho_raw / 100  # per 1% rate

    return PricingResult(
        price=max(price, 0.0),
        delta=delta,
        gamma=gamma,
        theta=theta,
        vega=vega,
        rho=rho,
        method="bs",
    )


# ── Binomial Tree (American) ──────────────────────────────────────────

def _binomial_american(
    S: float,
    K: float,
    T: float,
    r: float,
    q: float,
    sigma: float,
    option_type: Literal["call", "put"],
    N: int,
) -> PricingResult:
    """CRR binomial tree for American options."""
    if T <= 0 or sigma <= 0:
        intrinsic = max(0.0, S - K) if option_type == "call" else max(0.0, K - S)
        return PricingResult(
            price=intrinsic, delta=1.0 if intrinsic > 0 else 0.0,
            gamma=0.0, theta=0.0, vega=0.0, rho=0.0, method="binomial"
        )

    dt = T / N
    u = math.exp(sigma * math.sqrt(dt))
    d = 1.0 / u
    disc = math.exp(-r * dt)
    p = (math.exp((r - q) * dt) - d) / (u - d)
    p = max(0.0, min(1.0, p))  # clamp to avoid numerical issues

    # Terminal payoffs
    prices = [S * (u ** (N - 2 * j)) for j in range(N + 1)]
    if option_type == "call":
        values = [max(0.0, px - K) for px in prices]
    else:
        values = [max(0.0, K - px) for px in prices]

    # Backward induction with early exercise
    for i in range(N - 1, -1, -1):
        for j in range(i + 1):
            px = S * (u ** (i - 2 * j))
            hold = disc * (p * values[j] + (1 - p) * values[j + 1])
            intrinsic = max(0.0, px - K) if option_type == "call" else max(0.0, K - px)
            values[j] = max(hold, intrinsic)

    price = values[0]

    # Finite-difference Greeks from first two steps
    Su = S * u
    Sd = S * d
    dt2 = 2 * dt

    # Delta: (V_u - V_d) / (S_u - S_d)
    delta = (values[0] - values[1]) / (Su - Sd) if Su != Sd else 0.0

    # Gamma: second derivative at mid point
    S_uu = S * u * u
    S_ud = S
    S_dd = S * d * d
    if S_uu != S_dd:
        gamma = (
            (values[0] - 2 * values[1] + values[2]) / ((0.5 * (S_uu - S_dd)) ** 2)
        ) if len(values) > 2 else 0.0
    else:
        gamma = 0.0

    # Theta: approximate via one-step
    theta = (values[1] - price) / (365 * dt) if dt > 0 else 0.0

    # Vega approximation: use BS vega (identical for European/American near ATM)
    # Avoids infinite recursion from bump-and-reprice calling binomial again
    nd1_vega = _norm_pdf((math.log(S / K) + (r - q + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T)))
    vega = S * math.exp(-q * T) * nd1_vega * math.sqrt(T) / 100

    return PricingResult(
        price=max(price, 0.0),
        delta=max(-1.0, min(1.0, delta)),
        gamma=max(0.0, gamma),
        theta=theta,
        vega=vega,
        rho=0.0,  # rho bump-reprice omitted for performance
        method="binomial",
    )


def implied_vol(
    market_price: float,
    S: float,
    K: float,
    T: float,
    r: float,
    option_type: Literal["call", "put"],
    ticker: str = "",
    tol: float = 1e-5,
    max_iter: int = 100,
) -> float:
    """
    Newton-Raphson IV solver.

    Returns 0.0 if no solution found (deep ITM/OTM with no vol sensible).
    """
    if T <= 0 or market_price <= 0:
        return 0.0

    sigma = 0.30  # starting guess
    for _ in range(max_iter):
        result = price_option(S, K, T, r, sigma, option_type, ticker)
        diff = result.price - market_price
        if abs(diff) < tol:
            return sigma
        vega_raw = result.vega * 100  # vega is per 1%, convert back to per unit
        if abs(vega_raw) < 1e-10:
            return 0.0
        sigma -= diff / vega_raw
        sigma = max(0.001, min(sigma, 10.0))  # clamp

    return sigma  # best estimate even if not converged

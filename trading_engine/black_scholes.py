"""
Black-Scholes options pricing and probability calculations.
Core math used by all modules for strike selection, theta decay, and probability analysis.
"""

import math
from dataclasses import dataclass
from typing import Tuple

try:
    from scipy.stats import norm
except ImportError:
    # Fallback: simple normal CDF approximation if scipy not available
    class _NormFallback:
        @staticmethod
        def cdf(x: float) -> float:
            return 0.5 * (1 + math.erf(x / math.sqrt(2)))

        @staticmethod
        def pdf(x: float) -> float:
            return (1 / math.sqrt(2 * math.pi)) * math.exp(-0.5 * x * x)

    norm = _NormFallback()


# ─────────────────────────────────────────────────────────────────────
# Black-Scholes Pricing
# ─────────────────────────────────────────────────────────────────────

def d1(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Calculate d1 in Black-Scholes formula."""
    if T <= 0 or sigma <= 0:
        return 0.0
    return (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))


def d2(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Calculate d2 in Black-Scholes formula."""
    return d1(S, K, T, r, sigma) - sigma * math.sqrt(T)


def bs_call_price(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """
    Black-Scholes European call option price.

    Args:
        S: Current underlying price
        K: Strike price
        T: Time to expiration in years
        r: Risk-free rate (annualized)
        sigma: Implied volatility (annualized)
    """
    if T <= 0:
        return max(S - K, 0)
    _d1 = d1(S, K, T, r, sigma)
    _d2 = _d1 - sigma * math.sqrt(T)
    return S * norm.cdf(_d1) - K * math.exp(-r * T) * norm.cdf(_d2)


def bs_put_price(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Black-Scholes European put option price."""
    if T <= 0:
        return max(K - S, 0)
    _d1 = d1(S, K, T, r, sigma)
    _d2 = _d1 - sigma * math.sqrt(T)
    return K * math.exp(-r * T) * norm.cdf(-_d2) - S * norm.cdf(-_d1)


# ─────────────────────────────────────────────────────────────────────
# Greeks Calculations
# ─────────────────────────────────────────────────────────────────────

def bs_delta(S: float, K: float, T: float, r: float, sigma: float, option_type: str = "call") -> float:
    """Calculate delta."""
    if T <= 0 or sigma <= 0:
        if option_type == "call":
            return 1.0 if S > K else 0.0
        return -1.0 if S < K else 0.0
    _d1 = d1(S, K, T, r, sigma)
    if option_type == "call":
        return norm.cdf(_d1)
    return norm.cdf(_d1) - 1


def bs_gamma(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Calculate gamma (same for calls and puts)."""
    if T <= 0 or sigma <= 0:
        return 0.0
    _d1 = d1(S, K, T, r, sigma)
    return norm.pdf(_d1) / (S * sigma * math.sqrt(T))


def bs_theta(S: float, K: float, T: float, r: float, sigma: float, option_type: str = "call") -> float:
    """Calculate theta (per day)."""
    if T <= 0 or sigma <= 0:
        return 0.0
    _d1 = d1(S, K, T, r, sigma)
    _d2 = _d1 - sigma * math.sqrt(T)
    common = -(S * norm.pdf(_d1) * sigma) / (2 * math.sqrt(T))
    if option_type == "call":
        theta_annual = common - r * K * math.exp(-r * T) * norm.cdf(_d2)
    else:
        theta_annual = common + r * K * math.exp(-r * T) * norm.cdf(-_d2)
    return theta_annual / 365  # Per calendar day


def bs_vega(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Calculate vega (per 1% move in IV)."""
    if T <= 0 or sigma <= 0:
        return 0.0
    _d1 = d1(S, K, T, r, sigma)
    return S * math.sqrt(T) * norm.pdf(_d1) / 100


def bs_all_greeks(S: float, K: float, T: float, r: float, sigma: float,
                  option_type: str = "call") -> dict:
    """Calculate all greeks at once."""
    return {
        "delta": bs_delta(S, K, T, r, sigma, option_type),
        "gamma": bs_gamma(S, K, T, r, sigma),
        "theta": bs_theta(S, K, T, r, sigma, option_type),
        "vega": bs_vega(S, K, T, r, sigma),
        "iv": sigma,
    }


# ─────────────────────────────────────────────────────────────────────
# Probability Calculations
# ─────────────────────────────────────────────────────────────────────

def prob_otm(S: float, K: float, T: float, r: float, sigma: float,
             option_type: str = "put") -> float:
    """
    Probability of option expiring OTM (worthless).
    For put sellers: P(S > K at expiry)
    For call sellers: P(S < K at expiry)
    """
    if T <= 0:
        if option_type == "put":
            return 1.0 if S > K else 0.0
        return 1.0 if S < K else 0.0

    _d2 = d2(S, K, T, r, sigma)
    if option_type == "put":
        return norm.cdf(_d2)   # P(S_T > K)
    return norm.cdf(-_d2)      # P(S_T < K)


def prob_itm(S: float, K: float, T: float, r: float, sigma: float,
             option_type: str = "put") -> float:
    """Probability of option expiring ITM."""
    return 1 - prob_otm(S, K, T, r, sigma, option_type)


def expected_move(S: float, sigma: float, T: float) -> float:
    """
    Calculate expected move (1 standard deviation) for a given period.

    Args:
        S: Current price
        sigma: Annualized IV
        T: Time period in years (e.g., 1/252 for 1 day, 5/252 for 1 week)
    """
    return S * sigma * math.sqrt(T)


def expected_move_from_straddle(straddle_price: float) -> float:
    """
    Approximate expected move from ATM straddle price.
    Market convention: EM ≈ Straddle Price × 0.85
    """
    return straddle_price * 0.85


def strike_at_delta(S: float, T: float, r: float, sigma: float,
                    target_delta: float, option_type: str = "put") -> float:
    """
    Find the strike price corresponding to a target delta.
    Uses Newton's method for fast convergence.
    """
    if T <= 0:
        return S

    # Initial guess
    K = S
    for _ in range(50):
        current_delta = abs(bs_delta(S, K, T, r, sigma, option_type))
        if abs(current_delta - target_delta) < 0.001:
            break
        # Adjust strike
        gamma = bs_gamma(S, K, T, r, sigma)
        if gamma == 0:
            break
        if option_type == "put":
            # Lower delta → lower strike
            K -= (current_delta - target_delta) / gamma
        else:
            K += (current_delta - target_delta) / gamma

    return round(K)


def strike_at_std_dev(S: float, sigma: float, T: float,
                      num_std: float, side: str = "put") -> float:
    """
    Find strike at N standard deviations from current price.

    Args:
        num_std: Number of standard deviations (1.0, 1.5, 2.0, etc.)
        side: "put" for below, "call" for above
    """
    move = expected_move(S, sigma, T) * num_std
    if side == "put":
        return round(S - move)
    return round(S + move)


def prob_between(S: float, lower: float, upper: float, T: float,
                 r: float, sigma: float) -> float:
    """Probability that price stays between lower and upper bounds."""
    if T <= 0:
        return 1.0 if lower <= S <= upper else 0.0

    d2_lower = d2(S, lower, T, r, sigma)
    d2_upper = d2(S, upper, T, r, sigma)
    return norm.cdf(d2_upper) - norm.cdf(d2_lower)


# ─────────────────────────────────────────────────────────────────────
# Implied Volatility Solver
# ─────────────────────────────────────────────────────────────────────

def implied_vol(market_price: float, S: float, K: float, T: float,
                r: float, option_type: str = "call",
                tol: float = 1e-6, max_iter: int = 100) -> float:
    """
    Solve for implied volatility using Newton-Raphson method.
    """
    if T <= 0:
        return 0.0

    sigma = 0.25  # Initial guess
    for _ in range(max_iter):
        if option_type == "call":
            price = bs_call_price(S, K, T, r, sigma)
        else:
            price = bs_put_price(S, K, T, r, sigma)

        diff = price - market_price
        if abs(diff) < tol:
            return sigma

        vega = bs_vega(S, K, T, r, sigma) * 100  # Undo the /100 in bs_vega
        if vega < 1e-10:
            break
        sigma -= diff / vega
        sigma = max(sigma, 0.01)
        sigma = min(sigma, 5.0)

    return sigma


# ─────────────────────────────────────────────────────────────────────
# Theta Decay Curve (Intraday)
# ─────────────────────────────────────────────────────────────────────

def intraday_theta_curve(S: float, K: float, sigma: float, r: float,
                         option_type: str = "put",
                         market_open_hour: float = 9.5,
                         market_close_hour: float = 16.0) -> dict:
    """
    Calculate theta decay at each half-hour interval for a 0DTE option.
    Returns dict of {time_str: theta_value} showing acceleration into close.
    """
    total_hours = market_close_hour - market_open_hour
    curve = {}
    prev_price = None

    for minutes in range(0, int(total_hours * 60) + 1, 30):
        current_hour = market_open_hour + minutes / 60
        remaining_hours = market_close_hour - current_hour
        T = max(remaining_hours / (252 * 6.5), 1e-8)  # Convert to years

        if option_type == "put":
            price = bs_put_price(S, K, T, r, sigma)
        else:
            price = bs_call_price(S, K, T, r, sigma)

        hour_int = int(current_hour)
        minute_int = int((current_hour - hour_int) * 60)
        time_str = f"{hour_int:02d}:{minute_int:02d}"

        if prev_price is not None:
            decay = prev_price - price
            curve[time_str] = round(decay, 4)
        else:
            curve[time_str] = 0.0

        prev_price = price

    return curve

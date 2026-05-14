"""
Options chain intelligence service.

Provides:
  - max_pain_calculator: strike where most options expire worthless
  - unusual_flow_detector: flags volume > 2x 20-day average OI
  - options_chain_summary: ATM IV, skew, put/call ratio, term structure
  - get_gex: Gamma Exposure surface (dealer net gamma by strike)

All functions are synchronous-safe (wrap yfinance calls) and return
plain dicts so they can be logged / cached without Pydantic overhead.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

_GEX_CACHE: dict[str, tuple[float, dict]] = {}  # ticker → (timestamp, result)
_GEX_TTL = 300.0  # 5 minutes


def get_max_pain(ticker: str, expiry_str: str | None = None) -> dict[str, Any]:
    """
    Compute the max pain strike for a given options expiration.

    Max pain = the strike at which total dollar loss across all open interest
    is minimized (i.e., option writers profit most).

    Parameters
    ----------
    ticker:      Underlying symbol
    expiry_str:  Expiration date string (YYYY-MM-DD). If None, uses nearest weekly.

    Returns
    -------
    dict with keys: max_pain_strike, put_call_ratio, total_oi,
                    call_oi_by_strike, put_oi_by_strike
    """
    try:
        import yfinance as yf

        tk = yf.Ticker(ticker)
        if not tk.options:
            return {"error": "no options chain available", "ticker": ticker}

        # Select expiry
        if expiry_str:
            expiry = expiry_str
        else:
            # Nearest expiry ≥ 7 DTE
            today = date.today()
            expiry = None
            for exp in tk.options:
                exp_date = date.fromisoformat(exp)
                if (exp_date - today).days >= 7:
                    expiry = exp
                    break
            if not expiry:
                expiry = tk.options[0]

        chain = tk.option_chain(expiry)
        calls = chain.calls
        puts  = chain.puts

        if calls.empty or puts.empty:
            return {"error": "empty options chain", "ticker": ticker, "expiry": expiry}

        # Get all strikes from both sides
        strikes = sorted(set(calls["strike"].tolist()) | set(puts["strike"].tolist()))

        def _oi(v) -> int:
            try:
                f = float(v)
                return int(f) if f == f else 0  # f != f only for NaN
            except (TypeError, ValueError):
                return 0

        # Build OI maps
        call_oi = {float(r["strike"]): _oi(r["openInterest"]) for _, r in calls.iterrows()}
        put_oi  = {float(r["strike"]): _oi(r["openInterest"]) for _, r in puts.iterrows()}

        # Max pain: for each candidate strike, compute total dollar loss
        # Call loss at strike S: sum over all call strikes K < S of (S - K) * OI(K) * 100
        # Put loss at strike S:  sum over all put strikes K > S of (K - S) * OI(K) * 100
        min_loss = float("inf")
        max_pain_strike = strikes[len(strikes) // 2]  # default to ATM

        for candidate in strikes:
            call_loss = sum(
                max(0, candidate - k) * oi * 100
                for k, oi in call_oi.items()
            )
            put_loss = sum(
                max(0, k - candidate) * oi * 100
                for k, oi in put_oi.items()
            )
            total_loss = call_loss + put_loss
            if total_loss < min_loss:
                min_loss = total_loss
                max_pain_strike = candidate

        total_call_oi = sum(call_oi.values())
        total_put_oi  = sum(put_oi.values())
        put_call_ratio = total_put_oi / total_call_oi if total_call_oi > 0 else 1.0

        return {
            "ticker": ticker,
            "expiry": expiry,
            "max_pain_strike": max_pain_strike,
            "put_call_ratio": round(put_call_ratio, 2),
            "total_call_oi": total_call_oi,
            "total_put_oi": total_put_oi,
            "total_oi": total_call_oi + total_put_oi,
            "min_dollar_loss": min_loss,
        }

    except Exception as exc:
        logger.warning("max_pain failed for %s: %s", ticker, exc)
        return {"error": str(exc), "ticker": ticker}


_MIN_OI_FOR_FLOW = 500   # Below this, volume/OI ratio is meaningless noise
_SPOT_BAND_PCT   = 0.05  # Only count strikes within ±5% of spot as "concentrated"


def detect_unusual_flow(ticker: str, volume_multiplier: float = 2.0) -> dict[str, Any]:
    """
    Flag unusual options activity: volume > `volume_multiplier` × OI,
    with minimum OI floor and spot-concentration check to suppress false positives.

    Fixes applied vs prior version:
      - Minimum OI ≥ 500 per chain before computing ratios (kills Monday new-listing FP)
      - Strike concentration: unusual only if volume clustered within ±5% of spot
      - Absolute volume floor: call or put volume must be > 200 contracts total
    """
    try:
        import yfinance as yf

        tk = yf.Ticker(ticker)
        if not tk.options:
            return {"is_unusual": False, "ticker": ticker, "error": "no options"}

        expiry = tk.options[0]
        chain = tk.option_chain(expiry)
        calls = chain.calls
        puts  = chain.puts

        if calls.empty or puts.empty:
            return {"is_unusual": False, "ticker": ticker}

        def _safe_int(v) -> int:
            try:
                f = float(v)
                return int(f) if f == f else 0
            except (TypeError, ValueError):
                return 0

        call_oi = int(calls["openInterest"].apply(_safe_int).sum())
        put_oi  = int(puts["openInterest"].apply(_safe_int).sum())

        # Monday / new-listing false-positive guard: require meaningful OI
        if call_oi < _MIN_OI_FOR_FLOW and put_oi < _MIN_OI_FOR_FLOW:
            return {"is_unusual": False, "ticker": ticker, "reason": "insufficient_oi"}

        call_volume = int(calls["volume"].fillna(0).sum())
        put_volume  = int(puts["volume"].fillna(0).sum())

        # Absolute floor — eliminate micro-cap noise
        if call_volume < 200 and put_volume < 200:
            return {"is_unusual": False, "ticker": ticker, "reason": "low_volume"}

        # Spot-concentration check — is the volume clustered near the money?
        info = tk.info or {}
        spot = float(info.get("regularMarketPrice") or info.get("currentPrice") or 0)
        concentrated = False
        if spot > 0:
            lo, hi = spot * (1 - _SPOT_BAND_PCT), spot * (1 + _SPOT_BAND_PCT)
            near_calls = calls[calls["strike"].between(lo, hi)]
            near_puts  = puts[puts["strike"].between(lo, hi)]
            near_vol = (
                int(near_calls["volume"].fillna(0).sum())
                + int(near_puts["volume"].fillna(0).sum())
            )
            total_vol = call_volume + put_volume
            concentrated = total_vol > 0 and (near_vol / total_vol) >= 0.40

        call_vol_ratio = call_volume / call_oi if call_oi >= _MIN_OI_FOR_FLOW else 0
        put_vol_ratio  = put_volume  / put_oi  if put_oi  >= _MIN_OI_FOR_FLOW else 0

        is_unusual = (
            (call_vol_ratio > volume_multiplier or put_vol_ratio > volume_multiplier)
            and concentrated
        )

        max_ratio = max(call_vol_ratio, put_vol_ratio)
        if max_ratio > 5.0:
            urgency = "extreme"
        elif max_ratio > volume_multiplier * 1.5:
            urgency = "high"
        elif max_ratio > volume_multiplier:
            urgency = "moderate"
        else:
            urgency = "normal"

        if call_vol_ratio > put_vol_ratio * 1.5:
            dominant_side = "calls"
        elif put_vol_ratio > call_vol_ratio * 1.5:
            dominant_side = "puts"
        else:
            dominant_side = "neutral"

        return {
            "ticker":          ticker,
            "expiry":          expiry,
            "is_unusual":      is_unusual,
            "call_volume":     call_volume,
            "put_volume":      put_volume,
            "call_oi":         call_oi,
            "put_oi":          put_oi,
            "call_vol_ratio":  round(call_vol_ratio, 2),
            "put_vol_ratio":   round(put_vol_ratio, 2),
            "concentrated":    concentrated,
            "dominant_side":   dominant_side,
            "urgency":         urgency,
        }

    except Exception as exc:
        logger.warning("unusual_flow check failed for %s: %s", ticker, exc)
        return {"is_unusual": False, "ticker": ticker, "error": str(exc)}


def get_options_chain_summary(ticker: str, price: float | None = None) -> dict[str, Any]:
    """
    Compute ATM IV, skew (25-delta put IV - call IV), term structure,
    and put/call ratio for the nearest and next expiry.
    """
    try:
        import yfinance as yf

        tk = yf.Ticker(ticker)
        if not tk.options or len(tk.options) < 1:
            return {"error": "no options", "ticker": ticker}

        if price is None:
            info = tk.info
            price = float(info.get("regularMarketPrice", 0) or info.get("currentPrice", 0))

        results = {}
        for i, expiry in enumerate(tk.options[:2]):  # near + next term
            chain = tk.option_chain(expiry)
            calls = chain.calls
            puts  = chain.puts

            if calls.empty or puts.empty:
                continue

            # ATM strike
            atm_strike = min(calls["strike"], key=lambda s: abs(s - price))

            atm_call_iv = calls[calls["strike"] == atm_strike]["impliedVolatility"].values
            atm_put_iv  = puts[puts["strike"] == atm_strike]["impliedVolatility"].values

            atm_call_iv = float(atm_call_iv[0]) if len(atm_call_iv) else None
            atm_put_iv  = float(atm_put_iv[0])  if len(atm_put_iv)  else None

            # Skew: OTM put IV vs OTM call IV (approximate using 5% OTM)
            otm_call_strike = atm_strike * 1.05
            otm_put_strike  = atm_strike * 0.95
            otm_c_row = calls[(calls["strike"] - otm_call_strike).abs() < 2]
            otm_p_row = puts[(puts["strike"] - otm_put_strike).abs() < 2]
            otm_call_iv = float(otm_c_row["impliedVolatility"].iloc[0]) if not otm_c_row.empty else None
            otm_put_iv  = float(otm_p_row["impliedVolatility"].iloc[0]) if not otm_p_row.empty else None
            skew = round(otm_put_iv - otm_call_iv, 4) if (otm_put_iv and otm_call_iv) else None

            tag = "near" if i == 0 else "next"
            results[tag] = {
                "expiry": expiry,
                "atm_call_iv": round(atm_call_iv * 100, 1) if atm_call_iv else None,
                "atm_put_iv":  round(atm_put_iv * 100, 1) if atm_put_iv else None,
                "skew_pct": round(skew * 100, 1) if skew else None,
                "put_call_ratio": round(
                    puts["openInterest"].fillna(0).sum() / max(calls["openInterest"].fillna(0).sum(), 1), 2
                ),
            }

        # Term structure (near vs next ATM IV)
        term_structure = "flat"
        if "near" in results and "next" in results:
            near_iv = results["near"].get("atm_call_iv")
            next_iv = results["next"].get("atm_call_iv")
            if near_iv and next_iv:
                if next_iv > near_iv + 2:
                    term_structure = "contango"
                elif near_iv > next_iv + 2:
                    term_structure = "backwardation"

        return {
            "ticker": ticker,
            "price": price,
            "near": results.get("near", {}),
            "next": results.get("next", {}),
            "term_structure": term_structure,
        }

    except Exception as exc:
        logger.warning("options_chain_summary failed for %s: %s", ticker, exc)
        return {"error": str(exc), "ticker": ticker}


_GEX_MAX_DTE = 60   # expiries beyond 60 DTE contribute negligible gamma; skip them


def get_gex(ticker: str, price: float | None = None) -> dict[str, Any]:
    """
    Compute Gamma Exposure (GEX) surface across near-term expiries (0–60 DTE).

    GEX per strike = call_OI × gamma × spot² × 0.01
                   - put_OI × gamma × spot² × 0.01

    Aggregate GEX = sum across all strikes in the 0–60 DTE window.

    Interpretation:
      Positive aggregate GEX → dealers net long gamma → BUY dips / SELL rips
        → self-stabilising / mean-reversion regime.
      Negative aggregate GEX → dealers net short gamma → SELL dips / BUY rips
        → self-amplifying / trending regime.

    Returns:
      gex_total: aggregate signed GEX in dollar-gamma units
      gex_by_strike: list of {strike, gex} sorted by strike
      regime: "positive" | "negative" | "neutral"
      dominant_strike: strike with largest absolute GEX (potential price magnet)
      flip_level: strike where cumulative GEX crosses zero (vol trigger zone)
    """
    cached = _GEX_CACHE.get(ticker)
    if cached and (time.monotonic() - cached[0]) < _GEX_TTL:
        return cached[1]

    try:
        import math
        import yfinance as yf
        from datetime import date as _date

        tk = yf.Ticker(ticker)
        if not tk.options:
            return {"error": "no options", "ticker": ticker}

        if price is None:
            info = tk.info or {}
            price = float(
                info.get("regularMarketPrice")
                or info.get("currentPrice")
                or 0
            )
        if not price or price <= 0:
            return {"error": "no price", "ticker": ticker}

        def _safe(v, default=0.0) -> float:
            try:
                f = float(v)
                return f if f == f else default  # NaN guard
            except (TypeError, ValueError):
                return default

        today = _date.today()
        gex_by_strike: dict[float, float] = {}
        total_gex = 0.0

        for expiry in tk.options:
            exp_date = _date.fromisoformat(expiry)
            dte = (exp_date - today).days
            if dte > _GEX_MAX_DTE:
                break  # yfinance returns expiries in ascending date order
            dte_effective = max(dte, 0.5)  # 0DTE treated as 0.5 days to avoid gamma spike

            try:
                chain = tk.option_chain(expiry)
                calls, puts = chain.calls, chain.puts
                if calls.empty or puts.empty:
                    continue

                for _, row in calls.iterrows():
                    strike = _safe(row.get("strike"))
                    oi     = _safe(row.get("openInterest"))
                    gamma  = _safe(row.get("gamma"))
                    if strike <= 0 or oi < 1:
                        continue
                    if gamma <= 0:
                        iv = _safe(row.get("impliedVolatility"))
                        gamma = _approx_gamma(price, strike, iv, dte_effective) if iv > 0 else 0.0
                    contribution = oi * gamma * price * price * 0.01
                    gex_by_strike[strike] = gex_by_strike.get(strike, 0.0) + contribution
                    total_gex += contribution

                for _, row in puts.iterrows():
                    strike = _safe(row.get("strike"))
                    oi     = _safe(row.get("openInterest"))
                    gamma  = _safe(row.get("gamma"))
                    if strike <= 0 or oi < 1:
                        continue
                    if gamma <= 0:
                        iv = _safe(row.get("impliedVolatility"))
                        gamma = _approx_gamma(price, strike, iv, dte_effective) if iv > 0 else 0.0
                    contribution = oi * gamma * price * price * 0.01
                    gex_by_strike[strike] = gex_by_strike.get(strike, 0.0) - contribution
                    total_gex -= contribution

            except Exception:
                continue

        if not gex_by_strike:
            return {"error": "empty gex surface", "ticker": ticker}

        sorted_strikes = sorted(gex_by_strike.items())
        gex_list = [{"strike": s, "gex": round(g, 2)} for s, g in sorted_strikes]

        dominant_strike = max(gex_by_strike, key=lambda s: abs(gex_by_strike[s]))

        # Flip level = strike where cumulative GEX first crosses zero (potential vol regime boundary)
        flip_level: float | None = None
        cumulative = 0.0
        prev_sign = None
        for strike, gex in sorted_strikes:
            cumulative += gex
            sign = 1 if cumulative >= 0 else -1
            if prev_sign is not None and sign != prev_sign:
                flip_level = strike
                break
            prev_sign = sign

        if total_gex > 1e6:
            regime = "positive"
        elif total_gex < -1e6:
            regime = "negative"
        else:
            regime = "neutral"

        result = {
            "ticker":          ticker,
            "price":           price,
            "gex_total":       round(total_gex, 2),
            "regime":          regime,
            "dominant_strike": dominant_strike,
            "flip_level":      flip_level,
            "gex_by_strike":   gex_list,
        }
        _GEX_CACHE[ticker] = (time.monotonic(), result)
        return result

    except Exception as exc:
        logger.warning("get_gex failed for %s: %s", ticker, exc)
        return {"error": str(exc), "ticker": ticker}


def _approx_gamma(spot: float, strike: float, iv: float, dte_days: float = 30.0) -> float:
    """
    Black-Scholes gamma approximation for when yfinance returns gamma=0.
    dte_days must be the actual DTE for the expiry being processed.
    """
    import math
    if spot <= 0 or strike <= 0 or iv <= 0:
        return 0.0
    t = max(dte_days / 365.0, 1 / 365.0)
    try:
        d1 = (math.log(spot / strike) + 0.5 * iv * iv * t) / (iv * math.sqrt(t))
        phi = math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi)
        return phi / (spot * iv * math.sqrt(t))
    except (ValueError, ZeroDivisionError):
        return 0.0


def compute_bs_greeks(
    spot: float,
    strike: float,
    dte_days: float,
    iv: float,
    opt_type: str,
    risk_free: float = 0.05,
) -> dict[str, float]:
    """
    Full Black-Scholes greek set for a single option leg.

    Returns delta, gamma, theta (per calendar day), vega (per 1-pt IV move).
    Used as a fallback when yfinance options chain does not include greek columns,
    and for live greek refresh in the position lifecycle.

    Parameters
    ----------
    spot      : underlying price
    strike    : option strike
    dte_days  : calendar days to expiration (0.5 minimum to avoid singularity)
    iv        : implied volatility as a decimal (e.g. 0.25 for 25%)
    opt_type  : "call" or "put"
    risk_free : annualised risk-free rate (default 5%)
    """
    import math

    _zero = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    if spot <= 0 or strike <= 0 or iv <= 0:
        return _zero

    t = max(dte_days, 0.5) / 365.0
    sqrt_t = math.sqrt(t)

    try:
        d1 = (math.log(spot / strike) + (risk_free + 0.5 * iv * iv) * t) / (iv * sqrt_t)
        d2 = d1 - iv * sqrt_t

        def _nd(x: float) -> float:
            """Standard normal CDF via rational approximation (Abramowitz & Stegun)."""
            sign = 1 if x >= 0 else -1
            x = abs(x)
            t_ = 1 / (1 + 0.2316419 * x)
            poly = t_ * (0.319381530 + t_ * (-0.356563782 + t_ * (1.781477937
                   + t_ * (-1.821255978 + t_ * 1.330274429))))
            return 0.5 + sign * (0.5 - (math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)) * poly)

        def _npdf(x: float) -> float:
            return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)

        nd1 = _nd(d1)
        nd2 = _nd(d2)
        discount = math.exp(-risk_free * t)

        if opt_type.lower() == "call":
            delta = nd1
            theta = (
                -(spot * _npdf(d1) * iv) / (2 * sqrt_t)
                - risk_free * strike * discount * nd2
            ) / 365.0
        else:
            delta = nd1 - 1.0
            theta = (
                -(spot * _npdf(d1) * iv) / (2 * sqrt_t)
                + risk_free * strike * discount * (1 - nd2)
            ) / 365.0

        gamma = _npdf(d1) / (spot * iv * sqrt_t)
        vega  = spot * _npdf(d1) * sqrt_t / 100.0  # per 1% IV move

        return {
            "delta": round(delta, 4),
            "gamma": round(gamma, 6),
            "theta": round(theta, 4),
            "vega":  round(vega,  4),
        }

    except (ValueError, ZeroDivisionError):
        return _zero

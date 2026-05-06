"""Unit tests for the Black-Scholes and binomial pricing engine."""

from __future__ import annotations

import math
import pytest

from trading_platform.services.pricing.black_scholes import (
    implied_vol,
    price_option,
    PricingResult,
)


class TestBlackScholes:
    """Tests for European BS pricing."""

    def test_call_price_positive(self):
        result = price_option(100, 100, 0.25, 0.05, 0.20, "call", style="european")
        assert result.price > 0
        assert result.method == "bs"

    def test_put_call_parity(self):
        S, K, T, r, sigma = 100, 100, 0.25, 0.05, 0.20
        call = price_option(S, K, T, r, sigma, "call", style="european")
        put = price_option(S, K, T, r, sigma, "put", style="european")
        # C - P = S*e^(-qT) - K*e^(-rT)  (q=0 here)
        parity = S - K * math.exp(-r * T)
        assert abs((call.price - put.price) - parity) < 0.01

    def test_call_delta_between_0_and_1(self):
        result = price_option(100, 100, 0.25, 0.05, 0.20, "call", style="european")
        assert 0 < result.delta < 1

    def test_put_delta_between_minus1_and_0(self):
        result = price_option(100, 100, 0.25, 0.05, 0.20, "put", style="european")
        assert -1 < result.delta < 0

    def test_gamma_positive(self):
        result = price_option(100, 100, 0.25, 0.05, 0.20, "call", style="european")
        assert result.gamma > 0

    def test_theta_negative_for_long_call(self):
        result = price_option(100, 100, 0.25, 0.05, 0.20, "call", style="european")
        assert result.theta < 0

    def test_vega_positive(self):
        result = price_option(100, 100, 0.25, 0.05, 0.20, "call", style="european")
        assert result.vega > 0

    def test_zero_dte_returns_intrinsic(self):
        result = price_option(105, 100, 0.0, 0.05, 0.20, "call", style="european")
        assert abs(result.price - 5.0) < 0.01

    def test_deep_otm_near_zero(self):
        result = price_option(100, 200, 0.10, 0.05, 0.20, "call", style="european")
        assert result.price < 0.01


class TestBinomialAmerican:
    """Tests for American options via binomial tree."""

    def test_american_call_ge_european(self):
        """American call (no dividends) should roughly equal European."""
        am = price_option(100, 100, 0.25, 0.05, 0.20, "call", style="american")
        eu = price_option(100, 100, 0.25, 0.05, 0.20, "call", style="european")
        assert am.price >= eu.price - 0.10  # within 10 cents

    def test_spy_uses_binomial(self):
        """SPY pays dividends, so auto mode should use binomial."""
        result = price_option(
            500, 500, 0.25, 0.05, 0.15, "call", ticker="SPY", style="auto"
        )
        assert result.method == "binomial"

    def test_spx_uses_bs(self):
        """SPX (no ticker match) should use European BS."""
        result = price_option(
            5000, 5000, 0.25, 0.05, 0.15, "call", ticker="SPX", style="auto"
        )
        assert result.method == "bs"

    def test_american_put_early_exercise_premium(self):
        """Deep ITM American put should have higher value than European due to early exercise."""
        am = price_option(70, 100, 0.5, 0.05, 0.20, "put", style="american")
        eu = price_option(70, 100, 0.5, 0.05, 0.20, "put", style="european")
        assert am.price >= eu.price

    def test_binomial_call_delta_valid(self):
        result = price_option(100, 100, 0.25, 0.05, 0.20, "call", style="american")
        assert -1.0 <= result.delta <= 1.0


class TestImpliedVol:
    def test_iv_roundtrip(self):
        """Price an option, then recover IV from that price."""
        S, K, T, r, sigma = 100, 100, 0.25, 0.05, 0.20
        result = price_option(S, K, T, r, sigma, "call", style="european")
        recovered = implied_vol(result.price, S, K, T, r, "call")
        assert abs(recovered - sigma) < 0.005

    def test_zero_price_returns_zero(self):
        assert implied_vol(0.0, 100, 100, 0.25, 0.05, "call") == 0.0

    def test_zero_dte_returns_zero(self):
        assert implied_vol(5.0, 100, 100, 0.0, 0.05, "call") == 0.0

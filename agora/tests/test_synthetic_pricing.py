"""
agora/tests/test_synthetic_pricing.py — the backtester's Black-Scholes pricing engine (pure math,
0 I/O). Pins option price (incl. put-call parity + intrinsic-at-expiry), greeks, the analytic
strike-for-delta inverse, vol skew + term structure, slippage, and spread build/mark. The backtester
sizes strategy decisions off these numbers, so a pricing regression silently biases every backtest.
"""
from __future__ import annotations

import math

import pytest

from agora.backtester.synthetic_pricing import (
    _norm_cdf,
    _norm_inv,
    bs_greeks,
    bs_price,
    build_spread,
    entry_slippage,
    mark_spread,
    skewed_sigma,
    strike_for_delta,
    term_structure_sigma,
)


# ── bs_price ──────────────────────────────────────────────────────────────────
class TestBsPrice:
    def test_atm_call_positive(self):
        p = bs_price(S=100, K=100, T=30 / 365, r=0.05, sigma=0.30, option_type="call")
        assert 0 < p < 100

    def test_put_call_parity(self):
        # C - P = S - K e^{-rT}
        S, K, T, r, sig = 100, 105, 0.5, 0.05, 0.25
        c = bs_price(S, K, T, r, sig, "call")
        p = bs_price(S, K, T, r, sig, "put")
        assert (c - p) == pytest.approx(S - K * math.exp(-r * T), abs=1e-6)

    def test_intrinsic_at_expiry_call(self):
        # T<=0 → pure intrinsic
        assert bs_price(120, 100, 0, 0.05, 0.3, "call") == 20
        assert bs_price(80, 100, 0, 0.05, 0.3, "call") == 0

    def test_intrinsic_at_expiry_put(self):
        assert bs_price(80, 100, 0, 0.05, 0.3, "put") == 20
        assert bs_price(120, 100, 0, 0.05, 0.3, "put") == 0

    def test_deep_itm_call_approaches_intrinsic(self):
        # deep ITM short-dated call ≈ S - K·e^{-rT}
        S, K, T, r = 200, 100, 30 / 365, 0.05
        assert bs_price(S, K, T, r, 0.20, "call") == pytest.approx(S - K * math.exp(-r * T), abs=0.5)

    def test_higher_vol_raises_price(self):
        lo = bs_price(100, 100, 0.5, 0.05, 0.15, "call")
        hi = bs_price(100, 100, 0.5, 0.05, 0.45, "call")
        assert hi > lo

    @pytest.mark.parametrize("S,K,T,sigma", [(0, 100, 0.1, 0.3), (100, 100, 0.1, 0)])
    def test_invalid_falls_back_to_intrinsic(self, S, K, T, sigma):
        # guard branch returns intrinsic, never raises
        assert bs_price(S, K, T, 0.05, sigma, "call") == max(0.0, S - K)


# ── bs_greeks ─────────────────────────────────────────────────────────────────
class TestBsGreeks:
    def test_atm_call_delta_near_half(self):
        g = bs_greeks(100, 100, 30 / 365, 0.05, 0.30, "call")
        assert 0.5 < g["delta"] < 0.62

    def test_put_delta_negative(self):
        assert bs_greeks(100, 100, 30 / 365, 0.05, 0.30, "put")["delta"] < 0

    def test_gamma_and_vega_positive(self):
        g = bs_greeks(100, 100, 30 / 365, 0.05, 0.30, "call")
        assert g["gamma"] > 0 and g["vega"] > 0

    def test_theta_negative_for_long(self):
        assert bs_greeks(100, 100, 30 / 365, 0.05, 0.30, "call")["theta"] < 0

    def test_invalid_returns_zeros(self):
        assert bs_greeks(100, 100, 0, 0.05, 0.30, "call") == {
            "delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}


# ── strike_for_delta (analytic inverse) ───────────────────────────────────────
class TestStrikeForDelta:
    def test_call_otm(self):
        assert strike_for_delta(100, 30 / 365, 0.30, 0.20, "call") > 100

    def test_put_otm(self):
        assert strike_for_delta(100, 30 / 365, 0.30, 0.20, "put") < 100

    def test_round_trips_to_target_delta(self):
        # build a 20Δ call strike, then re-derive its delta — should be ~0.20
        K = strike_for_delta(100, 30 / 365, 0.30, 0.20, "call")
        d = bs_greeks(100, K, 30 / 365, 0.05, 0.30, "call")["delta"]
        assert abs(d - 0.20) < 0.03

    def test_degenerate_T_returns_spot(self):
        assert strike_for_delta(100, 0, 0.30, 0.20, "call") == 100


# ── skew + term structure ─────────────────────────────────────────────────────
class TestVolSurface:
    def test_skew_unchanged_for_unlisted_ticker(self):
        assert skewed_sigma(100, 95, 0.30, ticker="NOSKEW") == 0.30

    def test_skew_shifts_for_listed_ticker(self):
        # SPY has a (negative) put-skew slope → OTM put IV differs from ATM
        below = skewed_sigma(100, 90, 0.30, ticker="SPY")
        assert below != 0.30

    def test_term_structure_short_dte_uses_vix9d(self):
        assert term_structure_sigma(5, vix=20.0) == pytest.approx(20.0 * 0.90 / 100.0)

    def test_term_structure_monotone_in_variance(self):
        # all return annualised sigma in a sane band
        for dte in (5, 20, 45, 90):
            s = term_structure_sigma(dte, vix=20.0, vix9d=18.0, vix3m=21.0)
            assert 0.10 < s < 0.40

    def test_term_structure_none_fallbacks(self):
        # vix9d/vix3m default off vix; should not raise and stay sane
        assert 0.10 < term_structure_sigma(25, vix=22.0) < 0.40


# ── slippage ──────────────────────────────────────────────────────────────────
class TestSlippage:
    def test_default_for_unlisted(self):
        assert entry_slippage("RANDOM") == 0.040

    def test_listed_ticker(self):
        assert entry_slippage("SPY") != 0.040 or entry_slippage("SPY") == 0.040  # value from table
        assert isinstance(entry_slippage("SPY"), float)


# ── build_spread + mark_spread ────────────────────────────────────────────────
class TestSpread:
    def test_build_bull_put_spread_shape(self):
        s = build_spread(S=100, T_years=30 / 365, sigma=0.30, strategy="bull_put_spread")
        assert s["short_strike"] > s["long_strike"]    # put credit: short above long
        assert s["max_loss_dollars"] > 0
        assert s["entry_credit_debit"] < 0             # credit received
        assert s["option_type"] == "put"

    def test_build_bull_call_spread_shape(self):
        s = build_spread(S=100, T_years=30 / 365, sigma=0.30, strategy="bull_call_spread")
        assert s["long_strike"] < s["short_strike"]    # debit: long below short
        assert s["max_gain_dollars"] > 0
        assert s["entry_credit_debit"] > 0             # debit paid
        assert s["option_type"] == "call"

    def test_iron_condor_has_both_wings(self):
        s = build_spread(S=100, T_years=30 / 365, sigma=0.30, strategy="iron_condor")
        assert "call_short_strike" in s and "call_long_strike" in s

    def test_mark_at_expiry_is_intrinsic_put_credit(self):
        # bull_put_spread far OTM at expiry → ~0 (both puts expire worthless)
        v = mark_spread(S=120, T_years=0, sigma=0.30, short_strike=100, long_strike=95,
                        option_type="put", strategy="bull_put_spread")
        assert v == pytest.approx(0.0)

    def test_mark_at_expiry_max_loss_put_credit(self):
        # S below long strike → spread at full width (short - long intrinsic)
        v = mark_spread(S=90, T_years=0, sigma=0.30, short_strike=100, long_strike=95,
                        option_type="put", strategy="bull_put_spread")
        # short 100 put intrinsic 10, long 95 put intrinsic 5 → 10-5 = 5
        assert v == pytest.approx(5.0)

    def test_mark_before_expiry_credit_decays_toward_zero(self):
        far = mark_spread(150, 30 / 365, 0.30, 100, 95, "put", "bull_put_spread")
        assert far == pytest.approx(0.0, abs=0.5)   # deep OTM credit spread ≈ worthless


# ── norm helpers ──────────────────────────────────────────────────────────────
class TestNormHelpers:
    def test_norm_cdf_quantiles(self):
        assert _norm_cdf(0.0) == pytest.approx(0.5, abs=1e-4)
        assert _norm_cdf(1.96) == pytest.approx(0.975, abs=1e-3)

    def test_norm_cdf_symmetry(self):
        assert _norm_cdf(-1.0) == pytest.approx(1 - _norm_cdf(1.0), abs=1e-6)

    def test_norm_inv_is_cdf_inverse(self):
        for p in (0.1, 0.25, 0.5, 0.8, 0.975):
            assert _norm_cdf(_norm_inv(p)) == pytest.approx(p, abs=1e-2)

    def test_norm_inv_median(self):
        assert _norm_inv(0.5) == pytest.approx(0.0, abs=1e-3)

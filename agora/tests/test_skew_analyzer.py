"""
agora/tests/test_skew_analyzer.py — IV-skew math (drives put-credit bias + long-put sizing).

Pure deterministic math, so we pin exact numbers: standard-normal CDF, Black-Scholes delta and the
25Δ strike solver, expiry selection, regime classification, and the end-to-end analyze_skew on a
synthetic chain via BOTH the delta-column path and the Black-Scholes fallback. A regression here
would silently mis-bias entries toward the wrong side of the skew.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from agora.ops.skew_analyzer import (
    SkewResult,
    _bs_delta,
    _bs_implied_25delta_strike,
    _classify_regime,
    _norm_cdf,
    _pick_expiry,
    analyze_skew,
)


def _exp(dte: int) -> str:
    return (datetime.now(tz=timezone.utc).date() + timedelta(days=dte)).isoformat()


# ── standard normal CDF ───────────────────────────────────────────────────────
class TestNormCdf:
    def test_zero_is_half(self):
        assert abs(_norm_cdf(0.0) - 0.5) < 1e-4

    def test_symmetry(self):
        # Φ(-x) = 1 - Φ(x)
        for x in (0.3, 1.0, 1.96, 2.5):
            assert abs(_norm_cdf(-x) - (1.0 - _norm_cdf(x))) < 1e-4

    def test_known_quantiles(self):
        assert abs(_norm_cdf(1.0) - 0.8413) < 1e-3
        assert abs(_norm_cdf(1.96) - 0.9750) < 1e-3
        assert abs(_norm_cdf(-1.96) - 0.0250) < 1e-3

    def test_monotonic(self):
        xs = [-3, -1, 0, 1, 3]
        vals = [_norm_cdf(x) for x in xs]
        assert vals == sorted(vals)


# ── Black-Scholes delta ───────────────────────────────────────────────────────
class TestBsDelta:
    def test_atm_call_delta_near_half(self):
        d = _bs_delta(spot=100, strike=100, iv=0.30, dte_years=30 / 365, is_call=True)
        assert 0.5 < d < 0.62   # slightly >0.5 from drift + vega

    def test_atm_put_delta_negative(self):
        d = _bs_delta(spot=100, strike=100, iv=0.30, dte_years=30 / 365, is_call=False)
        assert -0.5 < d < 0.0

    def test_call_put_delta_parity(self):
        # call_delta - put_delta = 1 (no-dividend BS)
        c = _bs_delta(100, 105, 0.25, 0.25, is_call=True)
        p = _bs_delta(100, 105, 0.25, 0.25, is_call=False)
        assert abs((c - p) - 1.0) < 1e-6

    def test_deep_itm_call_delta_near_one(self):
        d = _bs_delta(spot=200, strike=100, iv=0.20, dte_years=30 / 365, is_call=True)
        assert d > 0.98

    def test_deep_otm_call_delta_near_zero(self):
        d = _bs_delta(spot=50, strike=100, iv=0.20, dte_years=30 / 365, is_call=True)
        assert d < 0.02

    @pytest.mark.parametrize("spot,strike,iv,dte", [
        (0, 100, 0.3, 0.1), (100, 0, 0.3, 0.1), (100, 100, 0, 0.1), (100, 100, 0.3, 0),
        (-5, 100, 0.3, 0.1),
    ])
    def test_invalid_inputs_return_zero(self, spot, strike, iv, dte):
        assert _bs_delta(spot, strike, iv, dte, is_call=True) == 0.0


# ── 25Δ strike solver ─────────────────────────────────────────────────────────
class TestImplied25DeltaStrike:
    def test_call_25d_strike_above_spot(self):
        k = _bs_implied_25delta_strike(spot=100, iv_atm=0.30, dte_years=30 / 365, is_call=True)
        assert k > 100   # 25Δ call is OTM

    def test_put_25d_strike_below_spot(self):
        k = _bs_implied_25delta_strike(spot=100, iv_atm=0.30, dte_years=30 / 365, is_call=False)
        assert 0 < k < 100   # 25Δ put is OTM

    def test_solved_strike_actually_has_25_delta(self):
        k = _bs_implied_25delta_strike(spot=100, iv_atm=0.30, dte_years=30 / 365, is_call=True)
        d = _bs_delta(100, k, 0.30, 30 / 365, is_call=True)
        assert abs(d - 0.25) < 0.01   # solver converged to ~0.25

    def test_higher_iv_widens_strike(self):
        lo = _bs_implied_25delta_strike(100, 0.20, 30 / 365, is_call=True)
        hi = _bs_implied_25delta_strike(100, 0.60, 30 / 365, is_call=True)
        assert hi > lo   # more vol → 25Δ strike further OTM

    @pytest.mark.parametrize("spot,iv,dte", [(0, 0.3, 0.1), (100, 0, 0.1), (100, 0.3, 0)])
    def test_invalid_inputs_return_zero(self, spot, iv, dte):
        assert _bs_implied_25delta_strike(spot, iv, dte, is_call=True) == 0.0


# ── expiry selection ──────────────────────────────────────────────────────────
class TestPickExpiry:
    def test_picks_closest_to_30dte(self):
        chain = {_exp(7): {}, _exp(28): {}, _exp(60): {}}
        assert _pick_expiry(chain, target_dte=30) == _exp(28)

    def test_skips_expired(self):
        chain = {_exp(-5): {}, _exp(45): {}}
        assert _pick_expiry(chain, target_dte=30) == _exp(45)

    def test_ignores_unparseable_keys(self):
        chain = {"not-a-date": {}, _exp(30): {}}
        assert _pick_expiry(chain, target_dte=30) == _exp(30)

    def test_none_when_all_expired_or_invalid(self):
        assert _pick_expiry({_exp(-1): {}, "junk": {}}, target_dte=30) is None

    def test_respects_custom_target(self):
        chain = {_exp(10): {}, _exp(50): {}}
        assert _pick_expiry(chain, target_dte=45) == _exp(50)


# ── regime classification ─────────────────────────────────────────────────────
class TestClassifyRegime:
    def test_call_skewed_when_negative(self):
        assert _classify_regime(-0.05) == ("call_skewed", "prefer_call_credit", 0, 0)

    def test_flat_band(self):
        assert _classify_regime(0.0)[0] == "flat"
        assert _classify_regime(0.029)[0] == "flat"

    def test_normal_put_skew_band(self):
        assert _classify_regime(0.03)[0] == "normal_put_skew"
        assert _classify_regime(0.099)[0] == "normal_put_skew"

    def test_steep_put_skew_triggers_penalty_and_adj(self):
        regime, bias, penalty, adj = _classify_regime(0.15)
        assert regime == "steep_put_skew"
        assert bias == "prefer_put_credit"
        assert penalty == 1
        assert adj == 3

    def test_boundary_010_is_steep(self):
        assert _classify_regime(0.10)[0] == "steep_put_skew"


# ── end-to-end analyze_skew ───────────────────────────────────────────────────
def _df(rows):
    return pd.DataFrame(rows)


class TestAnalyzeSkew:
    def test_none_on_empty_chain(self):
        assert analyze_skew({}, spot=100) is None

    def test_none_on_nonpositive_spot(self):
        assert analyze_skew({_exp(30): {}}, spot=0) is None

    def test_delta_path_normal_put_skew(self):
        # puts carry ~6% higher IV than calls at 25Δ → normal put skew
        calls = _df([
            {"strike": 105, "impliedVolatility": 0.30, "delta": 0.25},
            {"strike": 110, "impliedVolatility": 0.28, "delta": 0.15},
            {"strike": 100, "impliedVolatility": 0.33, "delta": 0.50},
        ])
        puts = _df([
            {"strike": 95, "impliedVolatility": 0.36, "delta": -0.25},
            {"strike": 90, "impliedVolatility": 0.40, "delta": -0.15},
            {"strike": 100, "impliedVolatility": 0.34, "delta": -0.50},
        ])
        res = analyze_skew({_exp(30): {"calls": calls, "puts": puts}}, spot=100)
        assert isinstance(res, SkewResult)
        assert res.call_25d_iv == pytest.approx(0.30)
        assert res.put_25d_iv == pytest.approx(0.36)
        assert res.skew == pytest.approx(0.06, abs=1e-9)
        assert res.skew_pct == pytest.approx(0.20, abs=1e-9)
        assert res.skew_regime == "steep_put_skew"      # 20% > 10%
        assert res.strategy_bias == "prefer_put_credit"
        assert res.conviction_adj == 3

    def test_delta_path_call_skew(self):
        calls = _df([
            {"strike": 105, "impliedVolatility": 0.40, "delta": 0.25},
            {"strike": 110, "impliedVolatility": 0.42, "delta": 0.15},
            {"strike": 100, "impliedVolatility": 0.38, "delta": 0.50},
        ])
        puts = _df([
            {"strike": 95, "impliedVolatility": 0.30, "delta": -0.25},
            {"strike": 90, "impliedVolatility": 0.28, "delta": -0.15},
            {"strike": 100, "impliedVolatility": 0.34, "delta": -0.50},
        ])
        res = analyze_skew({_exp(30): {"calls": calls, "puts": puts}}, spot=100)
        assert res.skew < 0
        assert res.skew_regime == "call_skewed"
        assert res.strategy_bias == "prefer_call_credit"

    def test_bs_fallback_when_no_delta_column(self):
        # No delta column → Black-Scholes 25Δ strike solver path. Strikes span both wings.
        strikes_iv = [(80, 0.42), (90, 0.37), (100, 0.33), (110, 0.30), (120, 0.31)]
        calls = _df([{"strike": s, "impliedVolatility": iv} for s, iv in strikes_iv])
        puts = _df([{"strike": s, "impliedVolatility": iv + 0.04} for s, iv in strikes_iv])
        res = analyze_skew({_exp(30): {"calls": calls, "puts": puts}}, spot=100)
        assert isinstance(res, SkewResult)
        assert res.call_25d_iv > 0
        assert res.put_25d_iv > res.call_25d_iv   # +0.04 put premium → put skew

    def test_none_when_missing_calls_or_puts(self):
        calls = _df([{"strike": 100, "impliedVolatility": 0.3, "delta": 0.5}])
        assert analyze_skew({_exp(30): {"calls": calls}}, spot=100) is None

    def test_none_when_required_columns_absent(self):
        bad = _df([{"strike": 100}])   # no impliedVolatility
        assert analyze_skew({_exp(30): {"calls": bad, "puts": bad}}, spot=100) is None

    def test_reason_str_populated(self):
        calls = _df([
            {"strike": 105, "impliedVolatility": 0.30, "delta": 0.25},
            {"strike": 110, "impliedVolatility": 0.28, "delta": 0.15},
            {"strike": 100, "impliedVolatility": 0.33, "delta": 0.50},
        ])
        puts = _df([
            {"strike": 95, "impliedVolatility": 0.31, "delta": -0.25},
            {"strike": 90, "impliedVolatility": 0.33, "delta": -0.15},
            {"strike": 100, "impliedVolatility": 0.34, "delta": -0.50},
        ])
        res = analyze_skew({_exp(30): {"calls": calls, "puts": puts}}, spot=100)
        assert "skew=" in res.reason_str and "DTE" in res.reason_str

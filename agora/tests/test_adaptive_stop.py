"""
agora/tests/test_adaptive_stop.py — the per-ticker volatility-normalized stop engine (pure).

Exhaustive edge coverage on every factor (vol/theta/regime/iv) + the composed stops + the
risk-budgeted size companion + invariants (clamping, monotonicity, grounding in our real HV data).
The whole module is pure, so this is the 99.99%-of-edge-cases surface — the IO wiring is tested
separately in test_position_surveillance / the position-manager integration tests.
"""
from __future__ import annotations

import pytest

from agora.ops.adaptive_stop import (
    DEFAULTS,
    AdaptiveStopConfig,
    adaptive_credit_stop_mult,
    adaptive_debit_stop_pct,
    explain,
    iv_factor,
    regime_factor,
    size_factor,
    theta_factor,
    vol_factor,
)


# ── vol_factor ────────────────────────────────────────────────────────────────────────
def test_vol_factor_baseline_is_one():
    assert vol_factor(DEFAULTS.base_hv) == pytest.approx(1.0)


def test_vol_factor_scales_linearly():
    assert vol_factor(0.60) == pytest.approx(2.0)        # 2× baseline (0.30)
    assert vol_factor(0.15) == pytest.approx(0.5)        # ½ baseline


def test_vol_factor_garbage_hv_is_neutral():
    assert vol_factor(None) == 1.0 and vol_factor(0.0) == 1.0 and vol_factor(-0.5) == 1.0


# ── theta_factor ──────────────────────────────────────────────────────────────────────
def test_theta_far_from_expiry_is_one():
    assert theta_factor(30) == 1.0 and theta_factor(DEFAULTS.near_dte) == 1.0


def test_theta_at_or_past_expiry_is_floor():
    assert theta_factor(0) == DEFAULTS.theta_floor
    assert theta_factor(-3) == DEFAULTS.theta_floor      # already expired → clamped to floor


def test_theta_ramps_monotonically_inside_window():
    vals = [theta_factor(d) for d in range(0, DEFAULTS.near_dte + 1)]
    assert vals == sorted(vals)                          # non-decreasing as DTE grows
    assert DEFAULTS.theta_floor < theta_factor(3) < 1.0  # interpolated strictly inside


def test_theta_none_dte_is_neutral():
    assert theta_factor(None) == 1.0


# ── regime_factor ─────────────────────────────────────────────────────────────────────
def test_regime_risk_off_tightens_case_insensitive():
    assert regime_factor("risk_off") == DEFAULTS.risk_off_tighten
    assert regime_factor("HIGH_VOLATILITY") == DEFAULTS.risk_off_tighten
    assert regime_factor("crisis") == DEFAULTS.risk_off_tighten


def test_regime_benign_and_garbage_is_one():
    assert regime_factor("neutral") == 1.0 and regime_factor("risk_on") == 1.0
    assert regime_factor(None) == 1.0 and regime_factor("") == 1.0


# ── iv_factor (the inert hook) ────────────────────────────────────────────────────────
def test_iv_factor_inert_by_default():
    assert iv_factor(None) == 1.0 and iv_factor(0.0) == 1.0 and iv_factor(-1.0) == 1.0
    assert iv_factor(1.0) == 1.0


def test_iv_factor_crush_widens_spike_tightens_bounded():
    assert iv_factor(0.5) > 1.0 and iv_factor(0.5) <= 1.15    # IV crush → widen (vega not direction)
    assert iv_factor(2.0) < 1.0 and iv_factor(2.0) >= 0.85    # IV spike → tighten
    assert iv_factor(100.0) == pytest.approx(0.85)            # clamped


# ── adaptive_debit_stop_pct (composition + clamps) ────────────────────────────────────
def test_debit_stop_grounded_in_real_hv():
    # TSLA HV ~0.58 far-dated neutral → hits the ceil (wide, needs room); KO HV ~0.16 → tight
    assert adaptive_debit_stop_pct(0.58, 30, "neutral") == DEFAULTS.stop_ceil
    ko = adaptive_debit_stop_pct(0.16, 30, "neutral")
    assert DEFAULTS.stop_floor <= ko < 0.35              # calm name → tight stop


def test_debit_stop_respects_floor_and_ceil():
    assert adaptive_debit_stop_pct(5.0, 30, "neutral") == DEFAULTS.stop_ceil       # absurd vol → ceil
    assert adaptive_debit_stop_pct(0.01, 30, "neutral") == DEFAULTS.stop_floor     # tiny vol → floor


def test_debit_stop_theta_tightens_near_expiry():
    far = adaptive_debit_stop_pct(0.30, 30, "neutral")
    near = adaptive_debit_stop_pct(0.30, 1, "neutral")
    assert near < far                                    # closer to expiry → tighter


def test_debit_stop_risk_off_tightens():
    assert adaptive_debit_stop_pct(0.30, 30, "risk_off") < adaptive_debit_stop_pct(0.30, 30, "neutral")


def test_debit_stop_never_raises_on_all_none():
    # fully degenerate inputs → falls back to base_stop within clamps, no exception
    v = adaptive_debit_stop_pct(None, None, None)
    assert DEFAULTS.stop_floor <= v <= DEFAULTS.stop_ceil


# ── adaptive_credit_stop_mult ─────────────────────────────────────────────────────────
def test_credit_mult_scales_and_clamps():
    assert adaptive_credit_stop_mult(0.16, 30, "neutral") < adaptive_credit_stop_mult(0.58, 30, "neutral")
    assert adaptive_credit_stop_mult(9.0, 30, "neutral") == DEFAULTS.credit_mult_ceil
    assert adaptive_credit_stop_mult(0.001, 30, "neutral") == DEFAULTS.credit_mult_floor


# ── size_factor (risk-budgeted companion) ─────────────────────────────────────────────
def test_size_factor_smaller_for_higher_vol():
    assert size_factor(DEFAULTS.base_hv) == 1.0          # baseline → full size
    assert size_factor(0.15) == 1.0                      # calmer than baseline → capped at 1.0 (down-only)
    assert size_factor(0.60) == pytest.approx(0.5)       # 2× vol → half size (constant $ risk)
    assert size_factor(5.0) == DEFAULTS.size_floor       # absurd vol → size floor


def test_size_factor_two_sided_ceil():
    # ceil > 1.0 → a calm name is sized UP (risk parity), bounded by the ceil
    assert size_factor(0.15, ceil=1.5) == 1.5            # 0.30/0.15 = 2.0 → clamped to ceil 1.5
    assert size_factor(0.20, ceil=1.5) == 1.5            # 0.30/0.20 = 1.5 → exactly the ceil
    assert size_factor(0.60, ceil=1.5) == pytest.approx(0.5)   # ceil never affects the DOWN side
    assert size_factor(0.30, ceil=2.5) == 1.0            # baseline vol stays 1.0 regardless of ceil


def test_size_factor_garbage_is_full():
    assert size_factor(None) == 1.0 and size_factor(0.0) == 1.0
    assert size_factor(None, ceil=2.0) == 1.0            # garbage → 1.0 even two-sided


def test_stop_and_size_are_inverse_for_risk_parity():
    # higher vol → WIDER stop but SMALLER size; the product (≈ $ risk proxy) compresses the spread
    wide, small = adaptive_debit_stop_pct(0.58, 30, "neutral"), size_factor(0.58)
    tight, big = adaptive_debit_stop_pct(0.16, 30, "neutral"), size_factor(0.16)
    assert wide > tight and small < big


# ── explain (UI / shadow payload) ─────────────────────────────────────────────────────
def test_explain_exposes_every_factor():
    e = explain("TSLA", 0.58, 5, "risk_off", iv_ratio=None)
    assert set(e) >= {"ticker", "hv", "dte", "regime", "debit_stop_pct", "credit_stop_mult",
                      "size_factor", "vol_factor", "theta_factor", "regime_factor", "iv_factor"}
    assert e["ticker"] == "TSLA" and e["iv_factor"] == 1.0     # inert hook reported, not hidden


def test_explain_handles_missing_hv():
    e = explain("XYZ", None, None, None)
    assert e["hv"] is None and DEFAULTS.stop_floor <= e["debit_stop_pct"] <= DEFAULTS.stop_ceil


# ── custom config plumbs through ──────────────────────────────────────────────────────
def test_custom_config_changes_bounds():
    cfg = AdaptiveStopConfig(stop_ceil=0.9, base_stop_pct=0.5)
    assert adaptive_debit_stop_pct(0.90, 30, "neutral", cfg=cfg) == 0.9   # higher ceil honored

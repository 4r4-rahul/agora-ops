"""
agora/tests/test_recalibration_20260624.py — the 3-part edge recalibration decided by the C-suite/
expert panel after the canonical _REAL_CLOSE ledger showed where the -$6,848 was leaking:

  R1  long directional debits bled -$5,711 (long_call -$4,062 @ 32% win) → raise the long-options
      signal-confluence floor 2 -> 3 (refuse marginal score-2 theta bets).
  R2  high-conviction (>=70) trades were 0% win over n=8 (-$2,008) — anti-predictive → suppress the
      1.5x size boost (high gate kept for tracking, size held at 1.0) unless explicitly re-enabled.
  R3  neutral-regime entries were 21% win / -$5,188 (n=43) vs risk_off 40% → raise the neutral
      conviction floor 60 -> 66; lean into the proven defensive edge.

All three are reversible (config flag / threshold). These tests lock the contract.
"""
from __future__ import annotations

import types

from agora.agents.disagreement_resolver import DisagreementResolver, SignalInput
from agora.core.config import get_settings


# ── R1: the long-options confluence floor was raised 2 -> 3 ──────────────────────────
def test_r1_long_options_min_conviction_floor_is_3():
    assert get_settings().long_options_min_conviction == 3


# ── R2: high-conviction 1.5x size boost is suppressed by default, restorable via flag ─
def _high_conviction_resolver(boost_enabled: bool) -> DisagreementResolver:
    r = DisagreementResolver()
    # Isolate from the global settings singleton — resolve() reads exactly these two fields.
    r._settings = types.SimpleNamespace(
        high_conviction_size_boost_enabled=boost_enabled,
        disagreement_resolver_floor=r._settings.disagreement_resolver_floor,
    )
    return r


def _resolve_high(r: DisagreementResolver) -> dict:
    # Two strongly-agreeing bullish signals + total_conviction>=70 → the 1.5x branch is reached.
    s1 = SignalInput(source="macro", direction="bullish", confidence=0.9, active=True)
    s2 = SignalInput(source="microstructure", direction="bullish", confidence=0.9, active=True)
    return r.resolve(s1, s2, None, regime="normal", total_conviction=75.0)


def test_r2_boost_suppressed_by_default():
    out = _resolve_high(_high_conviction_resolver(False))
    assert out["gate"] == "high"               # gate label kept for honest calibration tracking
    assert out["size_multiplier"] == 1.0       # but size is NOT amplified on a 0%-win cohort
    assert "suppressed" in out["reason"]


def test_r2_boost_restored_when_flag_enabled():
    out = _resolve_high(_high_conviction_resolver(True))
    assert out["gate"] == "high"
    assert out["size_multiplier"] == 1.5       # explicit opt-in restores the old behavior


def test_r2_low_conviction_unaffected():
    # total_conviction < 70 never reaches the boost branch regardless of the flag.
    r = _high_conviction_resolver(True)
    s1 = SignalInput(source="macro", direction="bullish", confidence=0.9, active=True)
    s2 = SignalInput(source="microstructure", direction="bullish", confidence=0.9, active=True)
    out = r.resolve(s1, s2, None, regime="normal", total_conviction=60.0)
    assert out["size_multiplier"] == 1.0 and out["gate"] == "standard"


# ── R3 was REJECTED by the C-suite cross-check (2026-06-24): it edited
# dynamic_params.min_conviction_score, but NOTHING consumes that value — the live entry gate
# (session.py) reads the static settings.min_conviction_score instead, which isn't regime-aware.
# The change was reverted as dead code. R3 (a regime-aware neutral-regime conviction bar) needs a
# proper rewire of the real gate and will land as a separate, verified change. No test here until
# it targets a consumed gate.

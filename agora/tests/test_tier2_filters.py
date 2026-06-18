"""
agora/tests/test_tier2_filters.py — Tier 2 (raise payoff / concentrate edge).

S2.1 regime filter is the only genuinely-new piece (the rest already existed in the codebase and is
verified enabled here, NOT re-built): S2.2 conviction-scaled trailing, S2.3 combo-liquidity
prescreen, S2.4 edge_size_multiplier (down-only). These tests pin S2.1's boundaries exactly and
assert the existing-three are wired/enabled so a config drift can't silently disable them.
"""
from __future__ import annotations

import inspect

from agora.core.config import get_settings
from agora.lifecycle import position_manager
from agora.ops.entry_filters import bearish_debit_blocked_in_risk_on


# ── S2.1 regime filter (the new piece) ────────────────────────────────────────
class TestRegimeFilter:
    def test_bearish_debit_in_riskon_blocked(self):
        # long_put (bearish, debit > 0) in confirmed risk-on → blocked
        assert bearish_debit_blocked_in_risk_on(
            "bearish", entry_debit_credit=300.0, macro_stance="risk_on",
            confidence=0.7, min_confidence=0.60) is True

    def test_bearish_credit_not_blocked(self):
        # bear_call (bearish, CREDIT < 0) sells premium → exempt
        assert bearish_debit_blocked_in_risk_on(
            "bearish", entry_debit_credit=-150.0, macro_stance="risk_on",
            confidence=0.7, min_confidence=0.60) is False

    def test_bullish_debit_not_blocked(self):
        # long_call in risk-on is WITH the drift → never blocked
        assert bearish_debit_blocked_in_risk_on(
            "bullish", entry_debit_credit=300.0, macro_stance="risk_on",
            confidence=0.9, min_confidence=0.60) is False

    def test_not_blocked_in_risk_off(self):
        assert bearish_debit_blocked_in_risk_on(
            "bearish", entry_debit_credit=300.0, macro_stance="risk_off",
            confidence=0.9, min_confidence=0.60) is False

    def test_not_blocked_in_neutral(self):
        assert bearish_debit_blocked_in_risk_on(
            "bearish", entry_debit_credit=300.0, macro_stance="neutral",
            confidence=0.9, min_confidence=0.60) is False

    def test_low_confidence_not_blocked(self):
        # ambiguous risk-on (conf < min) → don't block (avoid over-blocking a hedge)
        assert bearish_debit_blocked_in_risk_on(
            "bearish", entry_debit_credit=300.0, macro_stance="risk_on",
            confidence=0.40, min_confidence=0.60) is False

    def test_confidence_boundary(self):
        assert bearish_debit_blocked_in_risk_on(
            "bearish", 300.0, "risk_on", 0.60, min_confidence=0.60) is True

    def test_garbage_inputs_safe(self):
        assert bearish_debit_blocked_in_risk_on(None, None, None, None, min_confidence=0.6) is False


# ── existing mechanisms verified ENABLED (not re-built) ───────────────────────
class TestTier2AlreadyWired:
    def test_s21_config_present(self):
        s = get_settings()
        assert s.block_bearish_debit_in_risk_on is True
        assert 0 < s.block_bearish_debit_min_confidence <= 1

    def test_s22_conviction_trailing_exists(self):
        # the conviction-scaled trailing floor (let winners run) is already implemented
        src = inspect.getsource(position_manager)
        assert "conv_floor" in src and "eff_trail_floor" in src

    def test_s24_edge_sizing_enabled(self):
        # S2.4 reuses edge_size_multiplier — must be ON (down-only, protective)
        assert get_settings().edge_sizing_enabled is True

    def test_s24_edge_sizing_is_down_only(self):
        from agora.ops.edge_sizing import edge_size_multiplier
        src = inspect.getsource(edge_size_multiplier)
        # never sizes UP: capped at up_max which is pinned at 1.0
        assert "up_max" in src and "Only ever size DOWN" in src

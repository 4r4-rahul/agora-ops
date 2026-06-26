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


# ── R3 (REDESIGNED): the first R3 (raise neutral conviction bar) was rejected twice — dead code
# AND conceptually backwards (conviction is anti-predictive *within* neutral: high>=70 → 0% win, so
# raising the bar selects toward the worst trades). The expert panel re-derived R3 from the leak
# itself: long_call in neutral regime = -$4,419 (n=19), the single biggest hole — buying directional
# debits into a no-tailwind tape. R3 now requires CONFIRMED TREND (above/below both SMAs + 10d move)
# to take a long debit when macro has no directional bias. Wired through long_options_agent
# _score_direction (verified consumed, unlike the dead dynamic_params field).
from agora.agents.long_options_agent import LongOptionsAgent


def _score(stance: str, *, confirm_flag: bool, confirmed_up: bool):
    macro = types.SimpleNamespace(macro_stance=stance, iv_rank=50.0, vix=18.0, timestamp=None)
    flow  = types.SimpleNamespace(direction="bullish", sweeps=["sweep"])   # → bull += 2 (>= floor)
    mom = {"above_sma20": confirmed_up, "above_sma50": confirmed_up,
           "ret_10d": 0.05 if confirmed_up else 0.0, "rsi": 50.0}
    return LongOptionsAgent._score_direction(
        macro, flow, mom, "neutral", min_conviction=2, neutral_trend_confirm=confirm_flag)


def test_r3_blocks_unconfirmed_long_call_in_neutral():
    direction, strategy, stack, *_ = _score("neutral", confirm_flag=True, confirmed_up=False)
    assert direction is None and strategy is None       # the unconfirmed neutral debit is skipped
    assert "neutral_trend_gate" in stack


def test_r3_off_flag_lets_unconfirmed_neutral_debit_through():
    direction, strategy, *_ = _score("neutral", confirm_flag=False, confirmed_up=False)
    assert direction == "bullish" and strategy is not None   # flag off → old behavior


def test_r3_not_applied_when_macro_has_directional_bias():
    # risk_on = bullish tailwind → no confirmation required even unconfirmed
    direction, strategy, *_ = _score("risk_on", confirm_flag=True, confirmed_up=False)
    assert direction == "bullish" and strategy is not None


def test_r3_allows_confirmed_uptrend_in_neutral():
    direction, strategy, *_ = _score("neutral", confirm_flag=True, confirmed_up=True)
    assert direction == "bullish" and strategy is not None   # confirmed trend → debit proceeds


# ── #4 hard per-trade risk cap: positions >=$400 risk had -$134 EV vs +$13 for <$400. The cap
# trims contracts to the risk budget and returns 0 (caller skips) for a structure too wide to fit
# even one contract. Tests the pure _size_contracts logic (self is unused). ─────────────────────
from agora.strategies.rules_engine import StrategyRulesEngine


def _settings(cap: float):
    return types.SimpleNamespace(
        risk_per_trade_dollars=150.0, max_contracts_per_trade=10, max_risk_per_trade_dollars=cap)


def _sized(size_mult, max_loss_per_contract, cap=400.0):
    return StrategyRulesEngine._size_contracts(None, size_mult, max_loss_per_contract, _settings(cap))


def test_r4_normal_size_within_budget_unchanged():
    assert _sized(1.0, 50.0) == 3            # 150/50 = 3 contracts, $150 total — under the $400 cap


def test_r4_trims_to_risk_cap():
    # many cheap contracts would risk past the cap → trimmed to int(400/50)=8
    assert _sized(5.0, 50.0) == 8


def test_r4_skips_when_single_contract_exceeds_cap():
    # one $450 contract > $400 cap → returns 0 so the caller skips (the oversized-position leak)
    assert _sized(1.0, 450.0) == 0


def test_r4_disabled_restores_uncapped_floor():
    # cap=0 → old behavior: max(1,...) floors a too-wide single contract at 1 (no skip)
    assert _sized(1.0, 450.0, cap=0.0) == 1


# ── PAPER operational-effectiveness sizing: floor + multiplier, bypasses the live #4 cap ──────
def _paper_settings(floor=2, mult=2.0):
    return types.SimpleNamespace(
        trading_mode="paper", risk_per_trade_dollars=150.0, max_contracts_per_trade=10,
        max_risk_per_trade_dollars=400.0, paper_min_contracts=floor, paper_contract_multiplier=mult)


def _paper_sized(max_loss, floor=2, mult=2.0, size_mult=1.0):
    return StrategyRulesEngine._size_contracts(None, size_mult, max_loss, _paper_settings(floor, mult))


def test_paper_floors_and_scales_contracts():
    # REDESIGNED 2026-06-26: float-resolution paper sizing — sized = round(base_f × conviction × vol ×
    # mult), floored at paper_min_contracts (no flat floor that would invert risk parity). base_f =
    # 150/max_loss. A wide $779 structure: base 0.19 × mult 2 ≈ 0.39 → floored to 2.
    assert _paper_sized(779.0) == 2
    # a cheap $50 structure: base 3.0 × mult 2 = 6 (multi-contract management exercised, proportionally)
    assert _paper_sized(50.0) == 6


def test_paper_bypasses_the_live_risk_cap():
    # a $450 single contract is SKIPPED in live (exceeds $400 cap) but in paper it is sized up, not 0
    assert _sized(1.0, 450.0) == 0                 # live: skip
    assert _paper_sized(450.0) >= 2                # paper: multi-contract, cap bypassed


def test_paper_respects_max_contracts_hard_cap():
    # base 3.0 ($50 max_loss) × mult 4 = 12 → clamped to max_contracts_per_trade (10)
    assert _paper_sized(50.0, floor=1, mult=4.0) == 10


def test_live_mode_unchanged_when_mode_absent_or_live():
    # the existing #4 tests (no trading_mode) and explicit live both keep the cap
    s = types.SimpleNamespace(trading_mode="live", risk_per_trade_dollars=150.0,
                              max_contracts_per_trade=10, max_risk_per_trade_dollars=400.0)
    assert StrategyRulesEngine._size_contracts(None, 1.0, 450.0, s) == 0   # live cap still skips

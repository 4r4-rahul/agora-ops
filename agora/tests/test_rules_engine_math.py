"""
agora/tests/test_rules_engine_math.py — the deterministic risk/reward + structure math of the rules
engine (complements test_rules_engine.py, which covers build_recommendation/reprice end-to-end):
dynamic R/R floor, structure width (incl. iron-condor wider-wing), max-loss / max-gain per strategy
family, breakevens, position sizing, and the direction/strategy selectors. These define the dollar
risk, the R/R gate, and the contract count of every spread — a regression mis-sizes or mis-prices.
"""
from __future__ import annotations

import types

import pytest

from agora.core.models import StrategyPillar, StrategyType
from agora.strategies.rules_engine import StrategyRulesEngine


def _leg(action, strike, mid_price=0.0):
    return types.SimpleNamespace(action=action, strike=strike, mid_price=mid_price)


def _self(block_bull_put_in_risk_off=True):
    return types.SimpleNamespace(
        _settings=types.SimpleNamespace(block_bull_put_in_risk_off=block_bull_put_in_risk_off))


def _conv(pillar):
    return types.SimpleNamespace(pillar=pillar)


def _gex(regime_value):
    return types.SimpleNamespace(regime=types.SimpleNamespace(value=regime_value))


# ── _dynamic_rr_floor (IVR-scaled R/R gate) ───────────────────────────────────
class TestDynamicRrFloor:
    @pytest.mark.parametrize("ivr,expected", [
        (90, 0.15), (80, 0.15),
        (70, 0.12), (55, 0.12),
        (40, 0.10), (30, 0.10),
        (20, 0.08), (0, 0.08),
    ])
    def test_bands(self, ivr, expected):
        assert StrategyRulesEngine._dynamic_rr_floor(ivr, vix=None) == expected

    def test_none_ivr_defaults_to_50_band(self):
        assert StrategyRulesEngine._dynamic_rr_floor(None, None) == 0.10   # 50 → [30,55) band


# ── _structure_width (defines max risk) ───────────────────────────────────────
class TestStructureWidth:
    def test_two_leg_gap(self):
        legs = [_leg("buy", 100), _leg("sell", 110)]
        assert StrategyRulesEngine._structure_width(StrategyType.BULL_CALL_SPREAD, legs) == 10

    def test_iron_condor_uses_wider_wing(self):
        # put wing 100-95=5, call wing 120-130=10 → defining width is the WIDER (10)
        legs = [_leg("sell", 100), _leg("buy", 95), _leg("sell", 120), _leg("buy", 130)]
        assert StrategyRulesEngine._structure_width(StrategyType.IRON_CONDOR, legs) == 10

    def test_iron_condor_symmetric(self):
        legs = [_leg("sell", 100), _leg("buy", 95), _leg("sell", 120), _leg("buy", 125)]
        assert StrategyRulesEngine._structure_width(StrategyType.IRON_CONDOR, legs) == 5

    def test_under_two_legs_zero(self):
        assert StrategyRulesEngine._structure_width(StrategyType.BULL_CALL_SPREAD, [_leg("buy", 100)]) == 0.0


# ── _max_loss / _max_gain (P&L envelope per family) ───────────────────────────
class TestMaxLossGain:
    # debit spreads: pay debit; loss = debit, gain = width*100 - debit
    def test_debit_spread_loss_is_debit(self):
        ml = StrategyRulesEngine._max_loss(None, StrategyType.BULL_CALL_SPREAD, debit_credit=300.0, width=10)
        assert ml == 300.0

    def test_debit_spread_gain_is_width_minus_debit(self):
        mg = StrategyRulesEngine._max_gain(None, StrategyType.BULL_CALL_SPREAD, debit_credit=300.0, width=10)
        assert mg == 10 * 100 - 300.0   # 700

    # credit spreads / IC: receive credit; loss = width*100 - credit, gain = credit
    def test_credit_spread_loss_is_width_minus_credit(self):
        ml = StrategyRulesEngine._max_loss(None, StrategyType.BULL_PUT_SPREAD, debit_credit=-150.0, width=5)
        assert ml == 5 * 100 - 150.0    # 350

    def test_credit_spread_gain_is_credit(self):
        mg = StrategyRulesEngine._max_gain(None, StrategyType.BEAR_CALL_SPREAD, debit_credit=-150.0, width=5)
        assert mg == 150.0

    def test_iron_condor_uses_credit_math(self):
        assert StrategyRulesEngine._max_loss(None, StrategyType.IRON_CONDOR, -200.0, 10) == 10 * 100 - 200.0
        assert StrategyRulesEngine._max_gain(None, StrategyType.IRON_CONDOR, -200.0, 10) == 200.0

    def test_loss_plus_gain_equals_width_for_verticals(self):
        # invariant: for a vertical, max_loss + max_gain == width*100
        for strat, dc in [(StrategyType.BULL_CALL_SPREAD, 300.0), (StrategyType.BULL_PUT_SPREAD, -300.0)]:
            ml = StrategyRulesEngine._max_loss(None, strat, dc, 10)
            mg = StrategyRulesEngine._max_gain(None, strat, dc, 10)
            assert ml + mg == pytest.approx(10 * 100)


# ── _breakeven ────────────────────────────────────────────────────────────────
class TestBreakeven:
    def test_bull_call_spread_be(self):
        # buy 100 @2.0, sell 110 @0.8 → net debit 1.2 → BE = 100 + 1.2
        legs = [_leg("buy", 100, 2.0), _leg("sell", 110, 0.8)]
        assert StrategyRulesEngine._breakeven(None, StrategyType.BULL_CALL_SPREAD, legs) == 101.2

    def test_bull_put_spread_be(self):
        # sell 100 @2.0, buy 95 @0.8 → net credit 1.2 → BE = 100 - 1.2
        legs = [_leg("sell", 100, 2.0), _leg("buy", 95, 0.8)]
        assert StrategyRulesEngine._breakeven(None, StrategyType.BULL_PUT_SPREAD, legs) == 98.8

    def test_bear_put_spread_be(self):
        # buy 100 @2.0, sell 90 @0.8 → net debit 1.2 → BE = 100 - 1.2 (win below)
        legs = [_leg("buy", 100, 2.0), _leg("sell", 90, 0.8)]
        assert StrategyRulesEngine._breakeven(None, StrategyType.BEAR_PUT_SPREAD, legs) == 98.8

    def test_bear_call_spread_be(self):
        # sell 100 @2.0, buy 110 @0.8 → net credit 1.2 → BE = 100 + 1.2
        legs = [_leg("sell", 100, 2.0), _leg("buy", 110, 0.8)]
        assert StrategyRulesEngine._breakeven(None, StrategyType.BEAR_CALL_SPREAD, legs) == 101.2

    def test_empty_legs_none(self):
        assert StrategyRulesEngine._breakeven(None, StrategyType.BULL_CALL_SPREAD, []) is None


# ── _size_contracts (position sizing) ─────────────────────────────────────────
class TestSizeContracts:
    def _settings(self, risk=500.0, cap=10):
        return types.SimpleNamespace(risk_per_trade_dollars=risk, max_contracts_per_trade=cap)

    def test_base_sizing_from_risk_budget(self):
        # risk 500 / max_loss 100 = 5 contracts at full size
        n = StrategyRulesEngine._size_contracts(None, 1.0, 100.0, self._settings())
        assert n == 5

    def test_multiplier_scales(self):
        # base=5, ×0.5=2.5 → Python banker's rounding → 2
        assert StrategyRulesEngine._size_contracts(None, 0.5, 100.0, self._settings()) == 2
        # ×0.8 = 4.0 → 4 (unambiguous)
        assert StrategyRulesEngine._size_contracts(None, 0.8, 100.0, self._settings()) == 4

    def test_capped_at_max(self):
        # risk 500 / max_loss 10 = 50 contracts → capped at 10
        n = StrategyRulesEngine._size_contracts(None, 1.0, 10.0, self._settings(cap=10))
        assert n == 10

    def test_zero_max_loss_returns_one(self):
        assert StrategyRulesEngine._size_contracts(None, 1.0, 0.0, self._settings()) == 1

    def test_never_below_one(self):
        # tiny multiplier still yields at least 1 contract
        n = StrategyRulesEngine._size_contracts(None, 0.01, 100.0, self._settings())
        assert n >= 1

    # ── per-ticker adaptive entry sizing (down-only vol_size_factor) ──────────────
    def test_vol_factor_one_is_unchanged(self):
        # factor 1.0 (calm/disabled) → identical to base sizing (5)
        assert StrategyRulesEngine._size_contracts(None, 1.0, 100.0, self._settings(),
                                                   vol_size_factor=1.0) == 5

    def test_vol_factor_shrinks_volatile(self):
        # base 5 × 0.5 = 2.5 → 2 ; × 0.4 = 2.0 → 2 (a volatile name takes a smaller position)
        assert StrategyRulesEngine._size_contracts(None, 1.0, 100.0, self._settings(),
                                                   vol_size_factor=0.5) == 2
        assert StrategyRulesEngine._size_contracts(None, 1.0, 100.0, self._settings(),
                                                   vol_size_factor=0.4) == 2

    def test_vol_factor_two_sided_sizes_up(self):
        # factor > 1.0 (a calm name) enlarges the position — base 5 × 1.5 = 7.5 → 8
        assert StrategyRulesEngine._size_contracts(None, 1.0, 100.0, self._settings(),
                                                   vol_size_factor=1.5) == 8

    def test_vol_factor_up_is_capped_by_max_contracts(self):
        # the hard max_contracts ceiling bounds any up-size — 5 × 3.0 = 15 → capped at 10
        assert StrategyRulesEngine._size_contracts(None, 1.0, 100.0, self._settings(cap=10),
                                                   vol_size_factor=3.0) == 10

    def test_vol_factor_floors_at_one_contract(self):
        # factor 0.0 → 5×0 = 0 → max(1,...) keeps a tradeable 1 contract
        assert StrategyRulesEngine._size_contracts(None, 1.0, 100.0, self._settings(),
                                                   vol_size_factor=0.0) == 1


# ── _vol_size_factor (per-ticker resolver) ────────────────────────────────────
class TestVolSizeFactor:
    def _engine(self, *, enabled=True, profiles=(("TSLA", 0.58), ("KO", 0.16))):
        import sqlite3
        import tempfile
        db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        c = sqlite3.connect(db)
        c.execute("CREATE TABLE ticker_profiles (ticker TEXT, hv_annual REAL)")
        c.executemany("INSERT INTO ticker_profiles VALUES (?,?)", list(profiles))
        c.commit(); c.close()
        s = types.SimpleNamespace(db_path=db, adaptive_entry_sizing_enabled=enabled)
        return StrategyRulesEngine(settings=s)

    def test_volatile_ticker_sizes_down(self):
        # PURE vol-math, no floor: TSLA HV 0.58 → 0.30/0.58 ≈ 0.52
        assert self._engine()._vol_size_factor("TSLA") == pytest.approx(0.52, abs=0.01)

    def test_calm_ticker_sizes_up_uncapped(self):
        # PURE vol-math, no ceil: calm KO (HV 0.16 → 0.30/0.16 = 1.875) sized UP, machine decides
        assert self._engine()._vol_size_factor("KO") == pytest.approx(1.875, abs=0.01)

    def test_unprofiled_ticker_is_full_size(self):
        assert self._engine()._vol_size_factor("NOPROFILE") == 1.0

    def test_disabled_flag_is_full_size(self):
        assert self._engine(enabled=False)._vol_size_factor("TSLA") == 1.0   # gate off → 1.0

    def test_cached_after_first_lookup(self):
        eng = self._engine()
        eng._vol_size_factor("TSLA")
        assert "TSLA" in eng._hv_cache


# ── _infer_direction (pillar → direction) ─────────────────────────────────────
class TestInferDirection:
    def test_vol_premium_neutral_by_default(self):
        d = StrategyRulesEngine._infer_direction(_self(), _conv(StrategyPillar.VOL_PREMIUM), None, "neutral")
        assert d == "neutral"

    def test_vol_premium_risk_off_biases_bearish(self):
        d = StrategyRulesEngine._infer_direction(_self(True), _conv(StrategyPillar.VOL_PREMIUM), None, "risk_off")
        assert d == "bearish"

    def test_vol_premium_risk_off_gate_off_stays_neutral(self):
        d = StrategyRulesEngine._infer_direction(_self(False), _conv(StrategyPillar.VOL_PREMIUM), None, "risk_off")
        assert d == "neutral"

    def test_catalyst_is_bullish(self):
        d = StrategyRulesEngine._infer_direction(_self(), _conv(StrategyPillar.CATALYST), None)
        assert d == "bullish"

    def test_negative_gex_follows_momentum_bullish(self):
        d = StrategyRulesEngine._infer_direction(_self(), _conv(StrategyPillar.DIRECTIONAL), _gex("negative"))
        assert d == "bullish"


# ── _select_strategy (pillar + direction → structure, dte) ────────────────────
class TestSelectStrategy:
    def test_cpi_is_iron_condor(self):
        assert StrategyRulesEngine._select_strategy(None, _conv(StrategyPillar.EVENT_CPI), "neutral", None) \
            == (StrategyType.IRON_CONDOR, 7)

    def test_fomc_is_bull_call(self):
        assert StrategyRulesEngine._select_strategy(None, _conv(StrategyPillar.EVENT_FOMC), "bullish", None) \
            == (StrategyType.BULL_CALL_SPREAD, 7)

    def test_post_earnings_respects_direction(self):
        f = StrategyRulesEngine._select_strategy
        assert f(None, _conv(StrategyPillar.POST_EARNINGS), "bearish", None) == (StrategyType.BEAR_CALL_SPREAD, 21)
        assert f(None, _conv(StrategyPillar.POST_EARNINGS), "neutral", None) == (StrategyType.IRON_CONDOR, 21)
        assert f(None, _conv(StrategyPillar.POST_EARNINGS), "bullish", None) == (StrategyType.BULL_PUT_SPREAD, 21)

    def test_directional_bull_vs_bear(self):
        f = StrategyRulesEngine._select_strategy
        assert f(None, _conv(StrategyPillar.DIRECTIONAL), "bullish", None) == (StrategyType.BULL_CALL_SPREAD, 30)
        assert f(None, _conv(StrategyPillar.DIRECTIONAL), "bearish", None) == (StrategyType.BEAR_PUT_SPREAD, 30)

    def test_vol_premium_credit_side(self):
        f = StrategyRulesEngine._select_strategy
        assert f(None, _conv(StrategyPillar.VOL_PREMIUM), "neutral", None) == (StrategyType.BULL_PUT_SPREAD, 30)
        assert f(None, _conv(StrategyPillar.VOL_PREMIUM), "bearish", None) == (StrategyType.BEAR_CALL_SPREAD, 30)

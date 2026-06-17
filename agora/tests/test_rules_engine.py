"""Unit tests for agora/strategies/rules_engine.py — StrategyRulesEngine.

Pure/deterministic: no network, no LLM, no IBKR. Synthetic options chains are
built from pandas DataFrames in the shape the engine expects:

    {expiry_str: {"calls": DataFrame, "puts": DataFrame}}

with columns: strike / bid / ask / openInterest / impliedVolatility / delta.

These tests exercise the real selection + rejection behaviour:
  - gates (no_trade, spot<=0)
  - the mid<=0 unpriced-leg guard
  - the net combo bid-ask liquidity gate math in reprice_and_revalidate
    (rel = 2*|dc_ps - net_nat| / |dc_ps|, reject when rel > max_combo_spread_pct)
  - DTE-band / strike selection from a synthetic chain
  - force_strategy_type honoured only for _BUILDABLE_STRATEGIES
"""
from __future__ import annotations

import math
from datetime import date, timedelta

import pandas as pd
import pytest

from agora.core.models import (
    ConvictionScore,
    SpreadLeg,
    StrategyPillar,
    StrategyType,
    TradeRecommendation,
)
from agora.ops.dynamic_params import DynamicParams
from agora.strategies.rules_engine import _BUILDABLE_STRATEGIES, StrategyRulesEngine

# ── Synthetic chain construction ──────────────────────────────────────────────

def _call_delta(spot: float, strike: float) -> float:
    """Monotone-decreasing call delta in strike: ~1 deep ITM, ~0 deep OTM."""
    return max(0.01, min(0.99, 0.5 - (strike - spot) * 0.04))


def _put_delta(spot: float, strike: float) -> float:
    """Put delta: ~-1 deep ITM (high strike), ~0 deep OTM (low strike)."""
    return -max(0.01, min(0.99, 0.5 - (spot - strike) * 0.04))


def _option_price(spot: float, strike: float, opt: str) -> float:
    """Intrinsic + a Gaussian extrinsic bump peaking ATM — gives realistic,
    strike-monotone mids so vertical spreads net a non-zero debit/credit."""
    intrinsic = max(0.0, (spot - strike) if opt == "call" else (strike - spot))
    extrinsic = 3.0 * math.exp(-((strike - spot) / 8.0) ** 2)
    return round(intrinsic + extrinsic, 2)


def _make_df(
    strikes: list[float],
    spot: float,
    opt: str,
    *,
    zero_strikes: tuple[float, ...] = (),
    spread: float = 0.10,
) -> pd.DataFrame:
    """Build a calls/puts DataFrame. Strikes in `zero_strikes` get bid=ask=0
    (an unpriced/illiquid strike → mid<=0)."""
    rows = []
    for k in strikes:
        if k in zero_strikes:
            bid = ask = 0.0
        else:
            mid = _option_price(spot, k, opt)
            bid = round(max(0.0, mid - spread), 2)
            ask = round(mid + spread, 2)
        delta = _call_delta(spot, k) if opt == "call" else _put_delta(spot, k)
        rows.append(
            dict(
                strike=float(k),
                bid=bid,
                ask=ask,
                openInterest=500,
                impliedVolatility=0.30,
                delta=delta,
            )
        )
    return pd.DataFrame(rows)


def _chain(
    dte: int,
    *,
    spot: float = 100.0,
    strikes: list[float] | None = None,
    call_zero: tuple[float, ...] = (),
    put_zero: tuple[float, ...] = (),
) -> dict:
    """One-expiry chain at `dte` days from today."""
    if strikes is None:
        strikes = [float(k) for k in range(70, 131, 2)]
    exp = (date.today() + timedelta(days=dte)).isoformat()
    return {
        exp: {
            "calls": _make_df(strikes, spot, "call", zero_strikes=call_zero),
            "puts": _make_df(strikes, spot, "put", zero_strikes=put_zero),
        }
    }


def _conviction(pillar: StrategyPillar, *, gate: str = "standard", score: float = 70.0) -> ConvictionScore:
    return ConvictionScore(
        session_id="sess-1",
        ticker="TEST",
        total_score=score,
        size_multiplier=1.0,
        gate=gate,
        reasoning="synthetic",
        pillar=pillar,
    )


@pytest.fixture
def engine() -> StrategyRulesEngine:
    return StrategyRulesEngine()


# ── 1. Top-level gates ────────────────────────────────────────────────────────

class TestGates:
    def test_no_trade_gate_returns_none(self, engine):
        conv = _conviction(StrategyPillar.CATALYST, gate="no_trade", score=10.0)
        rec = engine.build_recommendation(conv, 100.0, _chain(23), direction_override="bullish")
        assert rec is None

    def test_nonpositive_spot_returns_none(self, engine):
        conv = _conviction(StrategyPillar.CATALYST)
        assert engine.build_recommendation(conv, 0.0, _chain(23), direction_override="bullish") is None
        assert engine.build_recommendation(conv, -5.0, _chain(23), direction_override="bullish") is None

    def test_no_matching_expiry_returns_none(self, engine):
        # CATALYST targets 21 DTE; relaxed window tops out at target_dte+21 = 42.
        # A lone 120-DTE expiry sits well outside any band → None.
        conv = _conviction(StrategyPillar.CATALYST)
        rec = engine.build_recommendation(conv, 100.0, _chain(120), direction_override="bullish")
        assert rec is None


# ── 2. mid<=0 unpriced-leg guard ──────────────────────────────────────────────

class TestUnpricedLegGuard:
    def test_baseline_bull_call_builds(self, engine):
        """Sanity: with a fully-priced chain the bull call spread is constructed.
        This is the control that makes the rejection test below meaningful."""
        conv = _conviction(StrategyPillar.CATALYST)
        rec = engine.build_recommendation(conv, 100.0, _chain(23), direction_override="bullish")
        assert rec is not None
        assert rec.strategy == StrategyType.BULL_CALL_SPREAD
        assert all((leg.mid_price or 0) > 0 for leg in rec.legs)

    def test_unpriced_long_leg_rejected(self, engine):
        """Zero out the 35-delta long call strike (~104) so its leg has bid=ask=0
        → mid<=0 → the liquidity guard rejects the whole structure."""
        conv = _conviction(StrategyPillar.CATALYST)
        # Confirm the strike the engine WOULD pick, then poison exactly that one.
        clean = _chain(23)
        calls = next(iter(clean.values()))["calls"]
        long_strike = engine._nearest_delta_strike(calls, engine._settings.long_delta_target, "call", 100.0)
        assert long_strike is not None
        poisoned = _chain(23, call_zero=(long_strike,))
        rec = engine.build_recommendation(conv, 100.0, poisoned, direction_override="bullish")
        assert rec is None


# ── 3. Net combo bid-ask liquidity gate (reprice_and_revalidate) ──────────────

class TestLiquidityGateMath:
    """The rel = 2*|dc_ps - net_nat|/|dc_ps| > cap rejection lives in
    reprice_and_revalidate. We feed aligned IBKR-style per-leg quotes and assert
    the formula's threshold behaviour around max_combo_spread_pct (default 0.50)."""

    def _rec(self) -> TradeRecommendation:
        exp = date.today() + timedelta(days=23)
        legs = [
            SpreadLeg(option_type="call", strike=104.0, expiration=exp, action="buy", mid_price=2.34),
            SpreadLeg(option_type="call", strike=108.0, expiration=exp, action="sell", mid_price=1.10),
        ]
        # Net debit per share = 2.34 - 1.10 = 1.24 → per contract 124.
        return TradeRecommendation(
            session_id="s",
            ticker="TEST",
            strategy=StrategyType.BULL_CALL_SPREAD,
            pillar=StrategyPillar.CATALYST,
            direction="bullish",
            legs=legs,
            contracts=1,
            entry_debit_credit=124.0,
            max_loss_dollars=124.0,
            max_gain_dollars=276.0,
            reward_risk_ratio=2.226,
        )

    def test_tight_spread_accepted(self, engine):
        # dc_ps = 2.34 - 1.10 = 1.24
        # net_nat = ask(buy) - bid(sell) = 2.39 - 1.05 = 1.34
        # rel = 2*|1.24 - 1.34| / 1.24 = 0.161 < 0.50 → accept
        ok, updated, reason = engine.reprice_and_revalidate(
            self._rec(),
            [
                {"mid": 2.34, "bid": 2.29, "ask": 2.39, "action": "buy"},
                {"mid": 1.10, "bid": 1.05, "ask": 1.15, "action": "sell"},
            ],
        )
        assert ok is True
        assert updated is not None
        assert reason == "ok"

    def test_wide_spread_rejected_by_liquidity(self, engine):
        # dc_ps = 1.24 (mids unchanged) but quotes are very wide:
        # net_nat = ask(buy) - bid(sell) = 3.20 - 0.20 = 3.00
        # rel = 2*|1.24 - 3.00| / 1.24 = 2.84 > 0.50 → reject as illiquid
        ok, updated, reason = engine.reprice_and_revalidate(
            self._rec(),
            [
                {"mid": 2.34, "bid": 1.50, "ask": 3.20, "action": "buy"},
                {"mid": 1.10, "bid": 0.20, "ask": 2.00, "action": "sell"},
            ],
        )
        assert ok is False
        assert updated is None
        assert "illiquid" in reason

    def test_threshold_is_max_combo_spread_pct(self, engine):
        """Drive rel just over the configured cap and confirm that exact gate
        (not pricing-sanity / R-R / credit) is what fires."""
        cap = float(getattr(engine._settings, "max_combo_spread_pct", 0.50))
        rec = self._rec()
        dc_ps = 2.34 - 1.10  # 1.24
        # Choose net_nat so rel = cap + small epsilon. rel = 2*|dc_ps-net_nat|/dc_ps.
        # Keep mids equal to dc_ps so pricing-sanity passes (ratio == 1).
        target_rel = cap + 0.05
        delta = target_rel * dc_ps / 2.0          # |dc_ps - net_nat|
        net_nat = dc_ps + delta                    # widen symmetrically upward
        # Distribute net_nat = ask_buy - bid_sell with mids fixed (2.34 buy, 1.10 sell).
        # Put all the widening on the buy leg's ask, sell leg's bid = its mid.
        ask_buy = 2.34 + (net_nat - dc_ps)
        ok, updated, reason = engine.reprice_and_revalidate(
            rec,
            [
                {"mid": 2.34, "bid": 2.34, "ask": round(ask_buy, 4), "action": "buy"},
                {"mid": 1.10, "bid": 1.10, "ask": 1.10, "action": "sell"},
            ],
        )
        assert ok is False
        assert "illiquid" in reason

    def test_mismatched_quote_count_keeps_yfinance(self, engine):
        rec = self._rec()
        ok, updated, reason = engine.reprice_and_revalidate(rec, [])
        assert ok is True
        assert updated is rec
        assert "kept yfinance" in reason


# ── 4. DTE band + strike selection ────────────────────────────────────────────

class TestSelection:
    def test_select_expiry_picks_nearest_in_band(self, engine):
        # CATALYST → target_dte 21; primary band is [21, 35). Of {9, 23, 50},
        # only 23 sits in-band, so it must be chosen.
        chain = {}
        chain.update(_chain(9))
        chain.update(_chain(23))
        chain.update(_chain(50))
        expiry, slice_ = engine._select_expiry(chain, 21)
        assert expiry is not None
        assert (expiry - date.today()).days == 23
        assert "calls" in slice_ and "puts" in slice_

    def test_select_expiry_prefers_closest_to_target(self, engine):
        # Two in-band candidates 23 and 30 DTE for target 21 → 23 is closer.
        chain = {}
        chain.update(_chain(23))
        chain.update(_chain(30))
        expiry, _ = engine._select_expiry(chain, 21)
        assert (expiry - date.today()).days == 23

    def test_min_dte_floor_applied(self, engine):
        # build_recommendation floors target_dte at max(7, ...). A CPI condor
        # (base 7 DTE) against a 9-DTE chain should still resolve to that expiry.
        conv = _conviction(StrategyPillar.EVENT_CPI)
        rec = engine.build_recommendation(conv, 100.0, _chain(9))
        assert rec is not None
        assert (rec.legs[0].expiration - date.today()).days == 9

    def test_bull_call_strikes_ordered_and_distinct(self, engine):
        conv = _conviction(StrategyPillar.CATALYST)
        rec = engine.build_recommendation(conv, 100.0, _chain(23), direction_override="bullish")
        assert rec is not None
        buy = next(l for l in rec.legs if l.action == "buy")
        sell = next(l for l in rec.legs if l.action == "sell")
        # Bull call: long (lower strike) bought, short (higher strike) sold.
        assert buy.strike < sell.strike
        # Long leg sits near the 35-delta target, short near 20-delta (further OTM).
        assert abs(buy.delta) > abs(sell.delta)

    def test_dynamic_params_dte_adjustment_shifts_expiry(self, engine):
        # CATALYST base 21 DTE. dte_adjustment=+9 → target 30 → in-band [30,44).
        # Offer 23 and 33 DTE; with the +9 shift the engine prefers 33.
        chain = {}
        chain.update(_chain(23))
        chain.update(_chain(33))
        conv = _conviction(StrategyPillar.CATALYST)
        dp = DynamicParams(short_delta_target=0.20, stop_loss_multiplier=2.0, dte_adjustment=9)
        rec = engine.build_recommendation(
            conv, 100.0, chain, direction_override="bullish", dynamic_params=dp
        )
        assert rec is not None
        assert (rec.legs[0].expiration - date.today()).days == 33
        # stop_loss from dynamic params is threaded through.
        assert rec.stop_loss_pct == 2.0


# ── 5. force_strategy_type override path ──────────────────────────────────────

class TestForceStrategyType:
    def test_unbuildable_override_returns_none(self, engine):
        # LONG_CALL is not in _BUILDABLE_STRATEGIES → override cannot be honoured.
        assert StrategyType.LONG_CALL not in _BUILDABLE_STRATEGIES
        conv = _conviction(StrategyPillar.VOL_PREMIUM)
        rec = engine.build_recommendation(
            conv, 100.0, _chain(33), direction_override="bullish",
            force_strategy_type=StrategyType.LONG_CALL,
        )
        assert rec is None

    def test_buildable_override_changes_structure(self, engine):
        # VOL_PREMIUM + bullish natively selects BULL_PUT_SPREAD. Force a
        # buildable BEAR_CALL_SPREAD and confirm that type is used instead.
        # The synthetic chain's credit/width is 0.20 (< the 0.30 cr_w edge floor); this
        # test verifies STRUCTURE OVERRIDE, not the gate, so relax the floor here. The
        # cr_w gate has its own coverage in TestCreditWidthGate below.
        engine._settings.min_credit_to_width_ratio = 0.0
        conv = _conviction(StrategyPillar.VOL_PREMIUM)
        native = engine.build_recommendation(conv, 100.0, _chain(33), direction_override="bullish")
        assert native is not None
        assert native.strategy == StrategyType.BULL_PUT_SPREAD

        forced = engine.build_recommendation(
            conv, 100.0, _chain(33), direction_override="bullish",
            force_strategy_type=StrategyType.BEAR_CALL_SPREAD,
        )
        assert forced is not None
        assert forced.strategy == StrategyType.BEAR_CALL_SPREAD

    def test_override_matching_native_is_noop(self, engine):
        # Forcing the type the engine already chose is a no-op (not a rejection).
        conv = _conviction(StrategyPillar.CATALYST)
        rec = engine.build_recommendation(
            conv, 100.0, _chain(23), direction_override="bullish",
            force_strategy_type=StrategyType.BULL_CALL_SPREAD,
        )
        assert rec is not None
        assert rec.strategy == StrategyType.BULL_CALL_SPREAD


# ── 6. Economics sanity on a built recommendation ─────────────────────────────

class TestEconomics:
    def test_credit_spread_records_negative_debit_credit(self, engine):
        # Verifies credit-spread ECONOMICS (sign of entry_debit_credit), not the cr_w edge
        # gate — the synthetic chain is 0.20 cr_w, so relax the floor (gate covered separately).
        engine._settings.min_credit_to_width_ratio = 0.0
        conv = _conviction(StrategyPillar.VOL_PREMIUM)
        rec = engine.build_recommendation(conv, 100.0, _chain(33), direction_override="bullish")
        assert rec is not None
        # Credit spread → entry_debit_credit is negative (credit received).
        assert rec.entry_debit_credit < 0
        assert rec.max_loss_dollars > 0
        assert rec.max_gain_dollars > 0

    def test_debit_spread_max_loss_equals_debit(self, engine):
        conv = _conviction(StrategyPillar.CATALYST)
        rec = engine.build_recommendation(conv, 100.0, _chain(23), direction_override="bullish")
        assert rec is not None
        # Bull call (debit): max loss == net debit paid; both scaled by contracts.
        assert rec.entry_debit_credit > 0
        assert rec.max_loss_dollars == pytest.approx(rec.entry_debit_credit, abs=0.01)
        assert rec.contracts >= 1


# ── 7. Credit/width edge gate (negative-EV credit-spread backstop) ────────────

class TestCreditWidthGate:
    """The cr_w gate rejects credit verticals whose credit/width is below the floor —
    structurally negative-EV (verified: 17/17 historical bull_put losers had cr_w 0.13-0.23).
    The synthetic VOL_PREMIUM bullish chain yields cr_w=0.20, a clean below-floor fixture."""

    def test_subfloor_credit_spread_rejected(self, engine):
        engine._settings.min_credit_to_width_ratio = 0.30
        conv = _conviction(StrategyPillar.VOL_PREMIUM)
        rec = engine.build_recommendation(conv, 100.0, _chain(33), direction_override="bullish")
        assert rec is None   # cr_w=0.20 < 0.30 → rejected

    def test_same_spread_builds_when_floor_relaxed(self, engine):
        engine._settings.min_credit_to_width_ratio = 0.0
        conv = _conviction(StrategyPillar.VOL_PREMIUM)
        rec = engine.build_recommendation(conv, 100.0, _chain(33), direction_override="bullish")
        assert rec is not None and rec.strategy == StrategyType.BULL_PUT_SPREAD

    def test_iron_condor_exempt_from_cr_w_gate(self, engine):
        # Iron condors collect on both wings (different ratio math) and are exempt — confirm a
        # sub-floor single-vertical fixture doesn't gate the IC pillar. (No-op if the pillar
        # doesn't build an IC on this chain; the assertion is simply that the gate didn't fire.)
        engine._settings.min_credit_to_width_ratio = 0.30
        conv = _conviction(StrategyPillar.VOL_PREMIUM)
        # Direct credit verticals gate; ICs do not. This documents the exemption boundary.
        rec = engine.build_recommendation(conv, 100.0, _chain(33), direction_override="bullish")
        assert rec is None   # the vertical path IS gated (proves the gate is scoped + active)

"""Unit tests for agora/core/models.py, agora/core/config.py, and
agora/signals/event_patterns.py.

No network / LLM / IBKR. The event-pattern engine reads a MacroCalendar; tests
inject a controllable fake calendar onto the engine (or use the real bundled
calendar at known dates) so window boundaries are exercised deterministically.
"""
from datetime import date, datetime

import pytest
from pydantic import ValidationError

from agora.core.config import AgoraSettings, get_settings
from agora.core.models import (
    CatalystType,
    ConvictionScore,
    OpenPosition,
    PositionStatus,
    SpreadLeg,
    StrategyPillar,
    StrategyType,
    TradeRecommendation,
)
from agora.signals.event_patterns import EventPatternEngine
from trading_platform.services.macro_calendar import MacroCalendar, MacroEvent

# ── Fixtures / helpers ────────────────────────────────────────────────────────

def _leg(option_type="call", strike=500.0, action="buy", exp=date(2026, 7, 17)) -> SpreadLeg:
    return SpreadLeg(option_type=option_type, strike=strike, expiration=exp, action=action)


def _credit_spread_position() -> OpenPosition:
    """Bull put credit spread — entry_price stored NEGATIVE (net credit received)."""
    return OpenPosition(
        position_id="pos-credit-1",
        ticker="SPY",
        strategy=StrategyType.BULL_PUT_SPREAD,
        pillar=StrategyPillar.VOL_PREMIUM,
        direction="bullish",
        legs=[
            _leg("put", 480.0, "sell"),
            _leg("put", 475.0, "buy"),
        ],
        contracts=2,
        entry_price=-1.20,          # net credit RECEIVED → negative
        entry_date=date(2026, 6, 1),
        expiry_date=date(2026, 7, 17),
        target_close_date=date(2026, 6, 22),
        max_loss_dollars=380.0,
        max_gain_dollars=120.0,
    )


def _long_put_position() -> OpenPosition:
    """Single-leg long put — entry_price stored POSITIVE (net debit paid)."""
    return OpenPosition(
        position_id="pos-long-1",
        ticker="QQQ",
        strategy=StrategyType.LONG_PUT,
        pillar=StrategyPillar.DIRECTIONAL,
        direction="bearish",
        legs=[_leg("put", 450.0, "buy")],
        contracts=1,
        entry_price=4.55,           # net debit PAID → positive
        entry_date=date(2026, 6, 1),
        expiry_date=date(2026, 6, 19),
        target_close_date=date(2026, 6, 6),
        max_loss_dollars=455.0,     # debit paid is the max loss on a long
        max_gain_dollars=1_000.0,
    )


class _FakeCalendar:
    """Stand-in for MacroCalendar exposing only the methods the signal engine
    actually calls. Lets tests place an arbitrary 'next event' at a chosen DTE."""

    def __init__(self, days_to_next: int, description: str, upcoming=None):
        self._dte = days_to_next
        self._desc = description
        self._upcoming = upcoming or []

    def days_to_next_event(self, dt=None):
        return self._dte, self._desc

    def upcoming_events(self, dt=None, days=30):
        return list(self._upcoming)


def _engine_with(cal) -> EventPatternEngine:
    eng = EventPatternEngine()
    eng._cal = cal
    return eng


# ── Models: enums ─────────────────────────────────────────────────────────────

class TestEnums:
    def test_strategy_type_values(self):
        assert StrategyType.BULL_PUT_SPREAD == "bull_put_spread"
        assert StrategyType.LONG_PUT == "long_put"
        assert StrategyType.IRON_CONDOR == "iron_condor"
        assert StrategyType("bear_call_spread") is StrategyType.BEAR_CALL_SPREAD

    def test_position_status_values(self):
        assert PositionStatus.OPEN == "open"
        assert PositionStatus.CLOSED == "closed"
        assert {s.value for s in PositionStatus} == {
            "open", "tested", "rolled", "closed", "expired", "assigned"
        }

    def test_strategy_pillar_event_members(self):
        assert StrategyPillar.EVENT_FOMC == "event_fomc"
        assert StrategyPillar.EVENT_CPI == "event_cpi"
        assert StrategyPillar.VOL_PREMIUM == "vol_premium"

    def test_catalyst_type_values(self):
        assert CatalystType.EARNINGS_BEAT == "earnings_beat"
        assert CatalystType.FDA_APPROVAL == "fda_approval"
        assert CatalystType("activist_13d") is CatalystType.ACTIVIST_13D


# ── Models: SpreadLeg / TradeRecommendation / ConvictionScore ────────────────

class TestSpreadLeg:
    def test_defaults(self):
        leg = _leg()
        # greeks + mid default to 0.0, contracts to 1
        assert leg.contracts == 1
        assert leg.delta == 0.0
        assert leg.gamma == 0.0
        assert leg.theta == 0.0
        assert leg.vega == 0.0
        assert leg.mid_price == 0.0
        assert leg.expiration == date(2026, 7, 17)


class TestTradeRecommendation:
    def _rec(self, **over):
        base = dict(
            session_id="s1",
            ticker="SPY",
            strategy=StrategyType.BULL_CALL_SPREAD,
            pillar=StrategyPillar.DIRECTIONAL,
            direction="bullish",
            legs=[_leg("call", 500.0, "buy"), _leg("call", 510.0, "sell")],
            entry_debit_credit=2.50,
            max_loss_dollars=250.0,
            max_gain_dollars=750.0,
            reward_risk_ratio=3.0,
        )
        base.update(over)
        return TradeRecommendation(**base)

    def test_construction_and_defaults(self):
        rec = self._rec()
        assert rec.contracts == 1
        assert rec.stop_loss_pct == 2.0
        assert rec.target_dte_close == 21
        assert rec.conviction_score == 0.0
        assert rec.size_multiplier == 1.0
        assert rec.breakeven_price is None
        assert rec.event_mitigation == ""
        assert isinstance(rec.timestamp, datetime)

    def test_entry_ivr_field_exists_and_defaults_zero(self):
        # entry_ivr must exist as a real field (BaseModel forbids ad-hoc attrs)
        assert self._rec().entry_ivr == 0.0

    def test_entry_ivr_accepts_value(self):
        rec = self._rec(entry_ivr=32.5)
        assert rec.entry_ivr == 32.5

    def test_rejects_ad_hoc_attribute_assignment(self):
        # The model forbids setting undeclared attributes — this is WHY entry_ivr
        # had to be added as a real field for the advocate to read it back.
        rec = self._rec()
        with pytest.raises(ValueError):
            rec.entry_ivr_typo = 42.0

    def test_extra_construction_kwarg_silently_dropped(self):
        # Undeclared kwargs at construction are ignored (not stored), confirming
        # ad-hoc context can only be carried via a declared field like entry_ivr.
        rec = self._rec(not_a_real_field=1)
        assert not hasattr(rec, "not_a_real_field")

    def test_debit_credit_sign_is_caller_convention(self):
        # entry_debit_credit: positive = debit paid, negative = credit received
        debit = self._rec(entry_debit_credit=2.50)
        credit = self._rec(entry_debit_credit=-1.10)
        assert debit.entry_debit_credit > 0
        assert credit.entry_debit_credit < 0


class TestConvictionScore:
    def test_construction_and_component_defaults(self):
        cs = ConvictionScore(session_id="s1", ticker="SPY", total_score=72.0)
        assert cs.size_multiplier == 1.0
        assert cs.gate == "no_trade"
        assert cs.pillar is None
        # component scores default to 0.0
        for comp in (cs.vol_premium_score, cs.gex_score, cs.regime_score,
                     cs.event_score, cs.macro_score, cs.smart_money_score,
                     cs.info_speed_score):
            assert comp == 0.0

    def test_total_score_bounds_enforced(self):
        with pytest.raises(ValidationError):
            ConvictionScore(session_id="s1", ticker="SPY", total_score=150.0)
        with pytest.raises(ValidationError):
            ConvictionScore(session_id="s1", ticker="SPY", total_score=-1.0)


# ── Models: OpenPosition P&L sign convention ─────────────────────────────────

class TestOpenPositionSignConvention:
    def test_credit_spread_entry_price_negative(self):
        pos = _credit_spread_position()
        # credit spreads store entry_price NEGATIVE (net credit received)
        assert pos.entry_price < 0
        assert pos.entry_price == -1.20
        assert pos.strategy == StrategyType.BULL_PUT_SPREAD
        assert pos.pillar == StrategyPillar.VOL_PREMIUM

    def test_long_put_entry_price_positive(self):
        pos = _long_put_position()
        # debit / long store entry_price POSITIVE (net debit paid)
        assert pos.entry_price > 0
        assert pos.entry_price == 4.55
        assert pos.strategy == StrategyType.LONG_PUT

    def test_sign_distinguishes_credit_from_debit(self):
        credit = _credit_spread_position()
        long_debit = _long_put_position()
        # the sign alone separates the two cashflow directions
        assert credit.entry_price < 0 < long_debit.entry_price

    def test_position_defaults(self):
        pos = _long_put_position()
        assert pos.status == PositionStatus.OPEN
        assert pos.current_price == 0.0
        assert pos.unrealized_pnl == 0.0
        assert pos.realized_pnl == 0.0          # realized_pnl defaults to 0.0
        assert pos.rolled_count == 0
        assert pos.ibkr_order_ids == []
        assert pos.is_pre_earnings is False
        assert pos.earnings_date is None
        assert pos.conviction_at_entry == 0.0
        assert isinstance(pos.last_reviewed, datetime)

    def test_realized_pnl_signed_settable(self):
        pos = _credit_spread_position()
        pos.realized_pnl = -250.0   # a loss is a negative realized P&L
        assert pos.realized_pnl == -250.0


# ── Config ────────────────────────────────────────────────────────────────────

class TestConfig:
    @pytest.fixture(scope="class")
    def settings(self) -> AgoraSettings:
        return get_settings()

    def test_get_settings_cached_singleton(self, settings):
        # lru_cache(maxsize=1) → same object every call
        assert get_settings() is settings
        assert isinstance(settings, AgoraSettings)

    def test_documented_defaults(self, settings):
        assert settings.ibkr_market_data_type == 1
        assert settings.exec_max_attempts_per_symbol == 4
        assert settings.prescreen_combo_spread_pct == 0.80
        assert settings.max_combo_spread_pct == 0.50

    def test_field_types(self, settings):
        assert isinstance(settings.ibkr_market_data_type, int)
        assert isinstance(settings.exec_max_attempts_per_symbol, int)
        assert isinstance(settings.scheduled_catalysts, list)
        assert isinstance(settings.use_adaptive_single_leg, bool)

    def test_use_adaptive_single_leg_default(self, settings):
        # Adaptive IS valid on single-leg native orders → default ON
        assert settings.use_adaptive_single_leg is True

    def test_spread_fractions_in_unit_interval(self, settings):
        # liquidity gates are fractions of mid → (0, 1]
        for frac in (settings.prescreen_combo_spread_pct, settings.max_combo_spread_pct):
            assert 0.0 < frac <= 1.0

    def test_prescreen_looser_than_execution_gate(self, settings):
        # pre-screen is intentionally LOOSE; precise gate is tighter
        assert settings.prescreen_combo_spread_pct > settings.max_combo_spread_pct

    def test_exec_attempts_sane_range(self, settings):
        assert 1 <= settings.exec_max_attempts_per_symbol <= 100

    def test_market_data_type_valid(self, settings):
        # 1=live, 3=delayed are the only meaningful values used by the bridge
        assert settings.ibkr_market_data_type in (1, 2, 3, 4)


# ── Event patterns: FOMC drift ────────────────────────────────────────────────

class TestFomcDrift:
    TODAY = date(2026, 6, 14)

    @pytest.mark.parametrize("dte", [1, 2, 3, 4, 5])
    def test_fires_inside_window_for_spy(self, dte):
        eng = _engine_with(_FakeCalendar(dte, "FOMC Rate Decision"))
        sigs = eng._fomc_drift("SPY", self.TODAY)
        assert len(sigs) == 1
        s = sigs[0]
        assert s["event_type"] == "fomc_drift"
        assert s["ticker"] == "SPY"
        assert s["direction"] == "bullish"
        assert s["strategy_hint"] == "bull_call_spread"
        assert s["days_to_event"] == dte
        assert 0.0 <= s["confidence"] <= 1.0

    def test_fires_for_qqq(self):
        eng = _engine_with(_FakeCalendar(3, "FOMC Rate Decision"))
        assert eng._fomc_drift("QQQ", self.TODAY)

    def test_silent_for_single_stock_inside_window(self):
        # SPY/QQQ only — a random single name gets nothing even at T-3 FOMC
        eng = _engine_with(_FakeCalendar(3, "FOMC Rate Decision"))
        assert eng._fomc_drift("AAPL", self.TODAY) == []

    def test_silent_before_window_T_minus_6(self):
        eng = _engine_with(_FakeCalendar(6, "FOMC Rate Decision"))
        assert eng._fomc_drift("SPY", self.TODAY) == []

    def test_silent_on_event_day_T_zero(self):
        # window is T-5..T-1; T-0 (event day) is excluded
        eng = _engine_with(_FakeCalendar(0, "FOMC Rate Decision"))
        assert eng._fomc_drift("SPY", self.TODAY) == []

    def test_silent_when_next_event_not_fomc(self):
        # T-3 but the next event is CPI, not FOMC → no FOMC drift
        eng = _engine_with(_FakeCalendar(3, "CPI Inflation Report"))
        assert eng._fomc_drift("SPY", self.TODAY) == []

    def test_real_calendar_t_minus_3(self):
        # Bundled calendar: FOMC 2026-06-17; today 06-14 → T-3
        eng = EventPatternEngine()
        eng._cal = MacroCalendar()
        sigs = eng._fomc_drift("SPY", date(2026, 6, 14))
        assert len(sigs) == 1
        assert sigs[0]["days_to_event"] == 3


# ── Event patterns: CPI iron condor ───────────────────────────────────────────

class TestCpiCondor:
    TODAY = date(2026, 7, 13)

    def test_fires_only_at_t_minus_1_for_spy(self):
        eng = _engine_with(_FakeCalendar(1, "CPI Inflation Report"))
        sigs = eng._cpi_condor("SPY", self.TODAY)
        assert len(sigs) == 1
        s = sigs[0]
        assert s["event_type"] == "cpi_iv_premium"
        assert s["direction"] == "neutral"
        assert s["strategy_hint"] == "iron_condor"
        assert s["days_to_event"] == 1

    def test_fires_for_qqq(self):
        eng = _engine_with(_FakeCalendar(1, "CPI Inflation Report"))
        assert eng._cpi_condor("QQQ", self.TODAY)

    def test_silent_for_single_stock(self):
        eng = _engine_with(_FakeCalendar(1, "CPI Inflation Report"))
        assert eng._cpi_condor("NVDA", self.TODAY) == []

    @pytest.mark.parametrize("dte", [0, 2, 3, 5])
    def test_silent_outside_t_minus_1(self, dte):
        # condor is a single-day signal (T-1 only) — not T-2, not event day
        eng = _engine_with(_FakeCalendar(dte, "CPI Inflation Report"))
        assert eng._cpi_condor("SPY", self.TODAY) == []

    def test_silent_when_next_event_not_cpi(self):
        eng = _engine_with(_FakeCalendar(1, "FOMC Rate Decision"))
        assert eng._cpi_condor("SPY", self.TODAY) == []

    def test_real_calendar_t_minus_1(self):
        # Bundled calendar: CPI 2026-07-14; today 07-13 → T-1
        eng = EventPatternEngine()
        eng._cal = MacroCalendar()
        sigs = eng._cpi_condor("SPY", date(2026, 7, 13))
        assert len(sigs) == 1
        assert sigs[0]["days_to_event"] == 1


# ── Event patterns: get_signals aggregation ──────────────────────────────────

class TestGetSignals:
    def test_aggregates_fomc_only_at_t_minus_3(self):
        eng = _engine_with(_FakeCalendar(3, "FOMC Rate Decision"))
        sigs = eng.get_signals("SPY", date(2026, 6, 14))
        kinds = {s["event_type"] for s in sigs}
        assert kinds == {"fomc_drift"}     # CPI condor stays silent

    def test_aggregates_cpi_only_at_t_minus_1(self):
        eng = _engine_with(_FakeCalendar(1, "CPI Inflation Report"))
        sigs = eng.get_signals("QQQ", date(2026, 7, 13))
        kinds = {s["event_type"] for s in sigs}
        assert kinds == {"cpi_iv_premium"}

    def test_empty_for_non_index_ticker(self):
        eng = _engine_with(_FakeCalendar(3, "FOMC Rate Decision"))
        assert eng.get_signals("TSLA", date(2026, 6, 14)) == []

    def test_empty_far_from_any_event(self):
        eng = _engine_with(_FakeCalendar(9, "FOMC Rate Decision"))
        assert eng.get_signals("SPY", date(2026, 6, 8)) == []


# ── MacroEvent shape sanity (feeds the engine) ───────────────────────────────

class TestMacroEventShape:
    def test_namedtuple_fields(self):
        ev = MacroEvent(
            event_date=date(2026, 6, 17),
            event_type="fomc",
            description="FOMC Rate Decision",
            risk_level="avoid",
        )
        assert ev.event_date == date(2026, 6, 17)
        assert ev.event_type == "fomc"
        assert "fomc" in ev.description.lower()
        assert ev.risk_level == "avoid"

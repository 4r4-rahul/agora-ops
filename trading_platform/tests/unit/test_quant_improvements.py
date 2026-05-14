"""
Tests for the quant-driven improvements from the Phase 1 audit:

  - Conviction: regime confidence penalty (-10 when confidence < 0.5)
  - Conviction: signal stack bonus (+10 when regime+technical+news all align)
  - Conviction: IV rank pre-classification when strategy unknown
  - RiskBudget: account drawdown circuit breaker (halt at 25% DD)
  - RiskBudget: PDT day trade counter (block at 3/week on <$25K accounts)
  - PositionSizing: Kelly-based dynamic sizing
  - MonitorAgent: trailing stop ratchet logic
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ── ConvictionAgent improvements ─────────────────────────────────────────────

def _make_conviction_agent():
    from trading_platform.agents.conviction import ConvictionAgent
    bus = MagicMock()
    state = MagicMock()
    settings = MagicMock()
    settings.account_size = 10_000.0
    settings.account_tier = "starter"
    agent = ConvictionAgent.__new__(ConvictionAgent)
    agent._bus = bus
    agent._state = state
    agent._settings = settings
    agent._log = MagicMock()
    agent._learned_weights = {}
    return agent


def _make_session(
    regime="bull_trend", regime_confidence=0.85,
    signal="bullish", signal_strength=0.80,
    news_sentiment="positive", news_score_raw=0.6,
    has_earnings=False, iv_rank=35.0,
):
    session = MagicMock()
    session.ticker = "SPY"
    session.regime_result = {
        "regime": regime,
        "confidence": regime_confidence,
    }
    session.technical_result = {
        "signal": signal,
        "signal_strength": signal_strength,
    }
    session.news_result = {
        "sentiment": news_sentiment,
        "sentiment_score": news_score_raw,
        "has_earnings_risk": has_earnings,
        "catalyst_type": "none",
    }
    session.market_snapshot = {"iv_rank": iv_rank}
    session.conviction_score = None
    return session


class TestConvictionRegimeConfidencePenalty:

    def test_low_confidence_reduces_regime_score(self):
        agent = _make_conviction_agent()
        # High confidence baseline
        s_high = _make_session(regime_confidence=0.85)
        score_high = agent._compute_score("sess", s_high)

        # Low confidence — should get -10 penalty
        s_low = _make_session(regime_confidence=0.35)
        score_low = agent._compute_score("sess", s_low)

        assert score_high.total_score > score_low.total_score
        # Penalty is ~10 pts on regime score
        assert score_high.total_score - score_low.total_score >= 8.0

    def test_confidence_below_threshold_noted_in_reasoning(self):
        agent = _make_conviction_agent()
        s = _make_session(regime_confidence=0.40)
        score = agent._compute_score("sess", s)
        assert "regime_conf_penalty" in score.reasoning or score.regime_score < 10.0

    def test_high_confidence_no_penalty(self):
        agent = _make_conviction_agent()
        s = _make_session(regime_confidence=0.80)
        score = agent._compute_score("sess", s)
        # At 0.80 confidence, bull_trend × 1.1 × 25 = 22 pts (no penalty)
        assert score.regime_score >= 20.0

    def test_crisis_regime_not_double_penalised(self):
        agent = _make_conviction_agent()
        s = _make_session(regime="crisis", regime_confidence=0.30)
        score = agent._compute_score("sess", s)
        # Crisis already zeroes regime_score — no further penalty needed
        assert score.regime_score == 0.0


class TestConvictionStackBonus:

    def test_all_aligned_bullish_gives_bonus(self):
        agent = _make_conviction_agent()
        # No alignment
        s_no = _make_session(
            regime="bull_trend", signal="neutral", news_score_raw=0.0
        )
        score_no = agent._compute_score("sess", s_no)

        # Full alignment: bull_trend + bullish signal + positive news
        s_yes = _make_session(
            regime="bull_trend", signal="bullish", news_score_raw=0.5
        )
        score_yes = agent._compute_score("sess", s_yes)
        assert score_yes.total_score > score_no.total_score

    def test_stack_bonus_noted_in_reasoning(self):
        agent = _make_conviction_agent()
        s = _make_session(regime="bull_trend", signal="bullish", news_score_raw=0.6)
        score = agent._compute_score("sess", s)
        assert "stack_bonus" in score.reasoning

    def test_no_bonus_on_earnings(self):
        agent = _make_conviction_agent()
        s_no_earnings = _make_session(
            regime="bull_trend", signal="bullish", news_score_raw=0.6, has_earnings=False
        )
        s_earnings = _make_session(
            regime="bull_trend", signal="bullish", news_score_raw=0.6, has_earnings=True
        )
        score_clean = agent._compute_score("sess", s_no_earnings)
        score_earn  = agent._compute_score("sess", s_earnings)
        # Earnings kills news score AND blocks stack bonus
        assert score_clean.total_score > score_earn.total_score

    def test_bearish_alignment_also_bonuses(self):
        agent = _make_conviction_agent()
        s = _make_session(
            regime="bear_trend", regime_confidence=0.85,
            signal="bearish", news_score_raw=-0.5,
        )
        score = agent._compute_score("sess", s)
        assert "stack_bonus" in score.reasoning


class TestConvictionIVPreclassification:

    def test_trending_low_iv_scores_well_as_buying(self):
        agent = _make_conviction_agent()
        # bull_trend + IVR 30 → likely debit spread (buying) → IV sweet spot
        s = _make_session(regime="bull_trend", iv_rank=30.0)
        score = agent._compute_score("sess", s)
        assert score.iv_score >= 12.0  # buying sweet spot

    def test_high_ivr_in_ranging_scores_well_as_selling(self):
        agent = _make_conviction_agent()
        s = _make_session(regime="ranging", iv_rank=60.0)
        score = agent._compute_score("sess", s)
        assert score.iv_score >= 12.0  # selling sweet spot


# ── RiskBudgetTracker improvements ───────────────────────────────────────────

def _make_settings(account_size=10_000.0):
    s = MagicMock()
    s.account_size = account_size
    s.daily_loss_limit_dollars = account_size * 0.03
    s.weekly_loss_limit_dollars = account_size * 0.06
    s.pdt_exempt = False  # explicit False so MagicMock doesn't shadow the check
    return s


def _make_tracker_with_db(db_path: Path, account_size=10_000.0):
    from trading_platform.services.risk_budget import RiskBudgetTracker
    settings = _make_settings(account_size)
    tracker = RiskBudgetTracker.__new__(RiskBudgetTracker)
    tracker._s = settings
    tracker._cooldown_until = None
    # Patch DB path
    import trading_platform.services.risk_budget as rb_module
    rb_module._DB_PATH = db_path
    return tracker


def _setup_db(db_path: Path, trades: list[dict]):
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trade_journal (
                id TEXT PRIMARY KEY,
                opened_at TEXT,
                closed_at TEXT,
                status TEXT,
                realized_pnl REAL
            )
        """)
        for t in trades:
            conn.execute(
                "INSERT INTO trade_journal VALUES (?,?,?,?,?)",
                (t["id"], t["opened_at"], t["closed_at"], t["status"], t.get("pnl")),
            )
        conn.commit()


class TestAccountDrawdownCircuitBreaker:

    def test_no_drawdown_allows_trading(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Path(tmpdir) / "test.db"
            _setup_db(db, [
                {"id": "t1", "opened_at": "2026-04-01T10:00:00", "closed_at": "2026-04-02T10:00:00", "status": "closed", "pnl": 500.0},
            ])
            tracker = _make_tracker_with_db(db)
            # Should not raise
            tracker._check_account_drawdown()

    def test_25pct_drawdown_blocks_trading(self):
        from trading_platform.services.risk_budget import CircuitBreakerTripped
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Path(tmpdir) / "test.db"
            # Starts at $10K, peaked at $12K (after +$2K), then lost $3.5K → now $8.5K
            # DD = (12K - 8.5K) / 12K = 29% → should trigger
            _setup_db(db, [
                {"id": "t1", "opened_at": "2026-04-01T10:00:00", "closed_at": "2026-04-02T10:00:00", "status": "closed", "pnl": 2000.0},
                {"id": "t2", "opened_at": "2026-04-03T10:00:00", "closed_at": "2026-04-04T10:00:00", "status": "closed", "pnl": -3500.0},
            ])
            tracker = _make_tracker_with_db(db)
            with pytest.raises(CircuitBreakerTripped, match="drawdown"):
                tracker._check_account_drawdown()

    def test_exactly_24pct_drawdown_does_not_block(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Path(tmpdir) / "test.db"
            # Peak = $11K (+$1K), current = $8.36K (-$2.64K from $11K = 24% DD)
            _setup_db(db, [
                {"id": "t1", "opened_at": "2026-04-01T10:00:00", "closed_at": "2026-04-02T10:00:00", "status": "closed", "pnl": 1000.0},
                {"id": "t2", "opened_at": "2026-04-03T10:00:00", "closed_at": "2026-04-04T10:00:00", "status": "closed", "pnl": -2640.0},
            ])
            tracker = _make_tracker_with_db(db)
            # 24% < 25% threshold — should NOT raise
            tracker._check_account_drawdown()

    def test_peak_balance_computation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Path(tmpdir) / "test.db"
            _setup_db(db, [
                {"id": "t1", "opened_at": "2026-04-01T10:00:00", "closed_at": "2026-04-02T10:00:00", "status": "closed", "pnl": 500.0},   # balance 10500
                {"id": "t2", "opened_at": "2026-04-03T10:00:00", "closed_at": "2026-04-04T10:00:00", "status": "closed", "pnl": 200.0},   # balance 10700 ← peak
                {"id": "t3", "opened_at": "2026-04-05T10:00:00", "closed_at": "2026-04-06T10:00:00", "status": "closed", "pnl": -100.0},  # balance 10600
            ])
            tracker = _make_tracker_with_db(db)
            peak = tracker._get_peak_balance()
            assert peak == pytest.approx(10_700.0)
            current = tracker._get_current_balance()
            assert current == pytest.approx(10_600.0)


class TestPDTCircuitBreaker:

    def _today_open_close(self, id: str) -> dict:
        today = date.today().isoformat()
        return {"id": id, "opened_at": f"{today}T09:35:00", "closed_at": f"{today}T14:00:00", "status": "closed", "pnl": 100.0}

    def test_pdt_allows_if_no_day_trades(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Path(tmpdir) / "test.db"
            _setup_db(db, [])
            tracker = _make_tracker_with_db(db, account_size=10_000.0)
            tracker._check_pdt_limit()  # should not raise

    def test_pdt_blocks_at_3_day_trades(self):
        from trading_platform.services.risk_budget import CircuitBreakerTripped
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Path(tmpdir) / "test.db"
            trades = [self._today_open_close(f"dt{i}") for i in range(3)]
            _setup_db(db, trades)
            tracker = _make_tracker_with_db(db, account_size=10_000.0)
            with pytest.raises(CircuitBreakerTripped, match="PDT"):
                tracker._check_pdt_limit()

    def test_pdt_not_active_above_25k(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Path(tmpdir) / "test.db"
            trades = [self._today_open_close(f"dt{i}") for i in range(5)]
            _setup_db(db, trades)
            tracker = _make_tracker_with_db(db, account_size=30_000.0)
            # PDT doesn't apply above $25K — should not raise regardless of day trades
            tracker._check_pdt_limit()

    def test_overnight_holds_not_counted_as_day_trades(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Path(tmpdir) / "test.db"
            today = date.today()
            yesterday = (today - timedelta(days=1)).isoformat()
            today_str = today.isoformat()
            trades = [
                # Opened yesterday, closed today = NOT a day trade
                {"id": "ov1", "opened_at": f"{yesterday}T10:00:00", "closed_at": f"{today_str}T14:00:00", "status": "closed", "pnl": 200.0},
                {"id": "ov2", "opened_at": f"{yesterday}T11:00:00", "closed_at": f"{today_str}T15:00:00", "status": "closed", "pnl": 150.0},
                {"id": "ov3", "opened_at": f"{yesterday}T12:00:00", "closed_at": f"{today_str}T15:30:00", "status": "closed", "pnl": 100.0},
            ]
            _setup_db(db, trades)
            tracker = _make_tracker_with_db(db, account_size=10_000.0)
            # Overnight holds don't count → 0 day trades → should not raise
            count = tracker._get_week_day_trades()
            assert count == 0
            tracker._check_pdt_limit()


# ── PositionSizing Kelly ──────────────────────────────────────────────────────

class TestKellyPositionSizing:

    def test_no_kelly_uses_flat_pct(self):
        from trading_platform.core.models.risk import PositionSizing
        sizing = PositionSizing.calculate(
            ticker="SPY",
            max_loss_per_contract=200.0,
            account_size=10_000.0,
            max_position_pct=0.05,
            kelly_fraction=0.0,
        )
        assert sizing.max_position_dollars == pytest.approx(500.0)  # 5% of 10K

    def test_kelly_scales_position_down_when_low(self):
        from trading_platform.core.models.risk import PositionSizing
        # Kelly=0.10 → half=0.05 → same as flat max (capped)
        sizing_flat = PositionSizing.calculate("SPY", 200.0, 10_000.0, 0.05, kelly_fraction=0.0)
        # Kelly=0.04 → half=0.02 → below flat max → should be smaller
        sizing_kelly = PositionSizing.calculate("SPY", 200.0, 10_000.0, 0.05, kelly_fraction=0.04)
        assert sizing_kelly.max_position_dollars < sizing_flat.max_position_dollars

    def test_kelly_capped_at_max_position_pct(self):
        from trading_platform.core.models.risk import PositionSizing
        # Kelly=0.80 → half=0.40 → BUT capped at max_position_pct=0.05
        sizing = PositionSizing.calculate("SPY", 200.0, 10_000.0, 0.05, kelly_fraction=0.80)
        assert sizing.max_position_dollars == pytest.approx(500.0)  # capped at 5%

    def test_kelly_floor_at_1pct(self):
        from trading_platform.core.models.risk import PositionSizing
        # Even with very low kelly (near zero), floor at 1% prevents $0 positions
        sizing = PositionSizing.calculate("SPY", 200.0, 10_000.0, 0.05, kelly_fraction=0.005)
        assert sizing.max_position_dollars >= 100.0  # at least 1% of $10K

    def test_kelly_note_in_output(self):
        from trading_platform.core.models.risk import PositionSizing
        sizing = PositionSizing.calculate("SPY", 200.0, 10_000.0, 0.05, kelly_fraction=0.20)
        assert "Kelly" in sizing.notes or "kelly" in sizing.notes.lower()


# ── MonitorAgent trailing stop ────────────────────────────────────────────────

def _make_open_position(
    entry_price=2.20, stop_loss=1.10, profit_target=4.00,
    dte=21, days_elapsed=5,
) -> Any:
    from trading_platform.agents.monitor import OpenPosition
    expiration = date.today() + timedelta(days=dte - days_elapsed)
    return OpenPosition({
        "id": "test-pos-123",
        "session_id": "sess-001",
        "ticker": "SPY",
        "strategy": "bull_call_spread",
        "direction": "bullish",
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "profit_target": profit_target,
        "contracts": 1,
        "max_loss_dollars": entry_price * 100,
        "underlying_at_entry": 500.0,
        "expiration_date": expiration.isoformat(),
        "original_dte": dte,
        "raw_recommendation": json.dumps({"legs": [
            {"option_type": "call", "strike": 500, "expiration_dte": dte, "action": "buy", "quantity": 1},
            {"option_type": "call", "strike": 510, "expiration_dte": dte, "action": "sell", "quantity": 1},
        ]}),
    })


def _make_monitor_agent():
    from trading_platform.agents.monitor import MonitorAgent
    agent = MonitorAgent.__new__(MonitorAgent)
    import logging
    agent._log = logging.getLogger("test.monitor")
    return agent


class TestTrailingStop:

    def test_trailing_stop_not_active_below_30pct_profit(self):
        agent = _make_monitor_agent()
        pos = _make_open_position(entry_price=2.20, profit_target=4.00)
        # At 25% of max profit: (2.75 - 2.20) / (4.00 - 2.20) = 0.55/1.80 = 30.6%
        # Just below: option_price = 2.70 → (2.70-2.20)/1.80 = 27.8%
        agent._update_trailing_stop(pos, 2.70)
        assert pos.trailing_stop_price is None

    def test_trailing_stop_activates_at_30pct_profit(self):
        agent = _make_monitor_agent()
        pos = _make_open_position(entry_price=2.20, profit_target=4.00)
        # At 30%: entry + 0.30 * 1.80 = 2.20 + 0.54 = 2.74
        agent._update_trailing_stop(pos, 2.74)
        assert pos.trailing_stop_price is not None
        assert pos.trailing_stop_price == pytest.approx(pos.entry_price, abs=0.01)  # breakeven

    def test_trailing_stop_ratchets_to_breakeven_plus_at_50pct(self):
        agent = _make_monitor_agent()
        pos = _make_open_position(entry_price=2.20, profit_target=4.00)
        # At 50%: entry + 0.50 * 1.80 = 2.20 + 0.90 = 3.10
        agent._update_trailing_stop(pos, 3.10)
        max_profit = 4.00 - 2.20
        expected_trail = 2.20 + max_profit * 0.10  # 10% of max profit locked
        assert pos.trailing_stop_price == pytest.approx(expected_trail, abs=0.01)

    def test_trailing_stop_locks_50pct_at_70pct_profit(self):
        agent = _make_monitor_agent()
        pos = _make_open_position(entry_price=2.20, profit_target=4.00)
        # At 70%: entry + 0.70 * 1.80 = 2.20 + 1.26 = 3.46
        agent._update_trailing_stop(pos, 3.46)
        max_profit = 4.00 - 2.20
        expected_trail = 2.20 + max_profit * 0.50
        assert pos.trailing_stop_price == pytest.approx(expected_trail, abs=0.01)

    def test_trailing_stop_never_decreases(self):
        agent = _make_monitor_agent()
        pos = _make_open_position(entry_price=2.20, profit_target=4.00)
        # Activate at 70% profit
        agent._update_trailing_stop(pos, 3.46)
        high_trail = pos.trailing_stop_price

        # Price drops back to 50% profit level — trailing stop should NOT decrease
        agent._update_trailing_stop(pos, 3.10)
        assert pos.trailing_stop_price == pytest.approx(high_trail, abs=0.01)

    def test_trailing_stop_triggers_exit(self):
        agent = _make_monitor_agent()
        pos = _make_open_position(entry_price=2.20, stop_loss=1.10, profit_target=4.00)
        # Activate trailing stop at 70% profit
        agent._update_trailing_stop(pos, 3.46)
        trail = pos.trailing_stop_price
        assert trail is not None

        # Price reverses to just below trailing stop
        result = agent._check_exit_condition(pos, trail - 0.05)
        assert result == "trailing_stop"

    def test_hard_stop_still_works_before_trailing_activates(self):
        agent = _make_monitor_agent()
        pos = _make_open_position(entry_price=2.20, stop_loss=1.10, profit_target=4.00)
        # No trailing stop yet (position never profited)
        result = agent._check_exit_condition(pos, 1.05)
        assert result == "stop_loss"


# ── Black-Scholes Greeks ───────────────────────────────────────────────────────

class TestBlackScholesGreeks:

    def test_call_delta_between_0_and_1(self):
        from trading_platform.core.models.risk import compute_spread_greeks
        legs = [{"action": "buy", "strike": 500, "option_type": "call"}]
        g = compute_spread_greeks(underlying=500.0, legs=legs, implied_vol=0.20, dte_remaining=30)
        assert 0.0 < g.delta < 1.0

    def test_put_delta_negative(self):
        from trading_platform.core.models.risk import compute_spread_greeks
        legs = [{"action": "buy", "strike": 500, "option_type": "put"}]
        g = compute_spread_greeks(underlying=500.0, legs=legs, implied_vol=0.20, dte_remaining=30)
        assert -1.0 < g.delta < 0.0

    def test_sell_call_inverts_delta(self):
        from trading_platform.core.models.risk import compute_spread_greeks
        legs = [{"action": "sell", "strike": 500, "option_type": "call"}]
        g = compute_spread_greeks(underlying=500.0, legs=legs, implied_vol=0.20, dte_remaining=30)
        assert g.delta < 0.0  # short call = negative delta

    def test_bull_call_spread_delta_positive_less_than_1(self):
        from trading_platform.core.models.risk import compute_spread_greeks
        # Buy 490 call, sell 500 call → net positive delta < 0.5
        legs = [
            {"action": "buy", "strike": 490, "option_type": "call"},
            {"action": "sell", "strike": 500, "option_type": "call"},
        ]
        g = compute_spread_greeks(underlying=495.0, legs=legs, implied_vol=0.20, dte_remaining=21)
        assert 0.0 < g.delta < 1.0

    def test_theta_is_negative_for_long_option(self):
        from trading_platform.core.models.risk import compute_spread_greeks
        legs = [{"action": "buy", "strike": 500, "option_type": "call"}]
        g = compute_spread_greeks(underlying=500.0, legs=legs, implied_vol=0.20, dte_remaining=21)
        assert g.theta < 0.0  # long options decay daily

    def test_vega_positive_for_long_option(self):
        from trading_platform.core.models.risk import compute_spread_greeks
        legs = [{"action": "buy", "strike": 500, "option_type": "call"}]
        g = compute_spread_greeks(underlying=500.0, legs=legs, implied_vol=0.20, dte_remaining=21)
        assert g.vega > 0.0

    def test_zero_dte_returns_zero_greeks(self):
        from trading_platform.core.models.risk import compute_spread_greeks
        legs = [{"action": "buy", "strike": 500, "option_type": "call"}]
        g = compute_spread_greeks(underlying=500.0, legs=legs, implied_vol=0.20, dte_remaining=0)
        assert g.delta == 0.0 and g.theta == 0.0 and g.vega == 0.0


# ── IV crush detection in monitor ────────────────────────────────────────────

class TestIVCrush:

    def _make_pos_with_iv_crush(self, iv_crush_risk: str, days_elapsed: int = 10, dte: int = 21):
        from trading_platform.agents.monitor import OpenPosition
        expiration = date.today() + timedelta(days=dte - days_elapsed)
        return OpenPosition({
            "id": "iv-crush-pos",
            "session_id": "sess-002",
            "ticker": "AAPL",
            "strategy": "bull_call_spread",
            "direction": "bullish",
            "entry_price": 2.50,
            "stop_loss": 1.25,
            "profit_target": 5.00,
            "contracts": 1,
            "max_loss_dollars": 250.0,
            "underlying_at_entry": 200.0,
            "expiration_date": expiration.isoformat(),
            "original_dte": dte,
            "raw_recommendation": json.dumps({
                "legs": [
                    {"option_type": "call", "strike": 198, "action": "buy"},
                    {"option_type": "call", "strike": 203, "action": "sell"},
                ],
                "iv_crush_risk": iv_crush_risk,
                "iv_rank": 65,
            }),
        })

    def test_no_iv_crush_does_not_reduce_time_value(self):
        from trading_platform.agents.monitor import _estimate_option_price
        pos = self._make_pos_with_iv_crush("none", days_elapsed=12)
        price_no_crush = _estimate_option_price(pos, 200.5)  # near ATM

        # A position with "none" crush risk should not be penalised
        pos_crush = self._make_pos_with_iv_crush("extreme", days_elapsed=12)
        price_with_crush = _estimate_option_price(pos_crush, 200.5)

        # extreme crush should produce lower time value → lower estimated price (at same intrinsic)
        assert price_with_crush <= price_no_crush

    def test_extreme_iv_crush_materially_reduces_time_value(self):
        from trading_platform.agents.monitor import _estimate_option_price
        # Use underlying just below lower strike (196 on 198/203 spread) so
        # moneyness_scale > 0 and time value is non-zero — allows crush to act on it.
        # distance_otm = 2, spread_width = 5 → moneyness_scale = 1 - 2/10 = 0.8
        pos_none = self._make_pos_with_iv_crush("none", days_elapsed=12)
        pos_extreme = self._make_pos_with_iv_crush("extreme", days_elapsed=12)

        price_none = _estimate_option_price(pos_none, 196.0)
        price_extreme = _estimate_option_price(pos_extreme, 196.0)

        # Both should be non-zero (moneyness scale > 0)
        assert price_none > 0
        # Extreme crush should reduce estimate by at least 20%
        assert price_extreme < price_none * 0.80

    def test_iv_crush_not_applied_before_20pct_dte_used(self):
        from trading_platform.agents.monitor import _estimate_option_price
        # Only 2 days elapsed out of 21 = 9.5% DTE used — below 20% threshold
        pos_none = self._make_pos_with_iv_crush("none", days_elapsed=2)
        pos_extreme = self._make_pos_with_iv_crush("extreme", days_elapsed=2)

        price_none = _estimate_option_price(pos_none, 180.0)
        price_extreme = _estimate_option_price(pos_extreme, 180.0)

        # Before 20% DTE threshold, crush factor should not apply
        assert price_none == pytest.approx(price_extreme, rel=0.01)

    def test_iv_rank_converts_to_sigma(self):
        from trading_platform.agents.monitor import OpenPosition
        expiration = date.today() + timedelta(days=14)
        pos = OpenPosition({
            "id": "iv-rank-test",
            "session_id": "s1",
            "ticker": "AAPL",
            "strategy": "bull_call_spread",
            "direction": "bullish",
            "entry_price": 2.0,
            "stop_loss": 1.0,
            "profit_target": 4.0,
            "contracts": 1,
            "max_loss_dollars": 200.0,
            "underlying_at_entry": 200.0,
            "expiration_date": expiration.isoformat(),
            "original_dte": 21,
            "raw_recommendation": json.dumps({"legs": [], "iv_rank": 50, "iv_crush_risk": "none"}),
        })
        # iv_rank=50 → σ = 0.10 + 50/100 * 0.30 = 0.25
        assert pos.iv_at_entry == pytest.approx(0.25, rel=0.01)


# ── Sector correlation tracking ───────────────────────────────────────────────

class TestSectorCorrelation:

    def _make_tracker(self):
        from trading_platform.services.risk_budget import RiskBudgetTracker
        from unittest.mock import MagicMock
        s = MagicMock()
        s.account_size = 50_000.0
        s.daily_loss_limit_dollars = 1_500.0
        s.weekly_loss_limit_dollars = 3_000.0
        tracker = RiskBudgetTracker.__new__(RiskBudgetTracker)
        tracker._s = s
        tracker._cooldown_until = None
        import trading_platform.services.risk_budget as rb_module
        # Point to non-existent DB so balance/PDT checks return clean state
        import tempfile
        tmpdir = tempfile.mkdtemp()
        rb_module._DB_PATH = Path(tmpdir) / "sector_test.db"
        return tracker

    def test_allows_first_tech_position(self):
        tracker = self._make_tracker()
        open_positions = []
        # Should not raise
        tracker._check_sector_concentration("AAPL", open_positions)

    def test_allows_second_tech_position(self):
        tracker = self._make_tracker()
        open_positions = [{"ticker": "MSFT", "direction": "bullish"}]
        tracker._check_sector_concentration("AAPL", open_positions)

    def test_blocks_third_tech_position(self):
        from trading_platform.services.risk_budget import CircuitBreakerTripped
        tracker = self._make_tracker()
        open_positions = [
            {"ticker": "MSFT", "direction": "bullish"},
            {"ticker": "GOOGL", "direction": "bullish"},
        ]
        with pytest.raises(CircuitBreakerTripped, match="tech"):
            tracker._check_sector_concentration("AAPL", open_positions)

    def test_broad_market_etfs_not_blocked(self):
        tracker = self._make_tracker()
        open_positions = [
            {"ticker": "SPY", "direction": "bullish"},
            {"ticker": "QQQ", "direction": "bullish"},
        ]
        # broad_market is exempt — should not raise
        tracker._check_sector_concentration("IWM", open_positions)

    def test_unknown_ticker_not_blocked(self):
        tracker = self._make_tracker()
        open_positions = [
            {"ticker": "UNKNOWN1", "direction": "bullish"},
            {"ticker": "UNKNOWN2", "direction": "bullish"},
        ]
        # Tickers not in sector map → no sector rule applies
        tracker._check_sector_concentration("UNKNOWN3", open_positions)

    def test_different_sectors_not_blocked(self):
        tracker = self._make_tracker()
        open_positions = [
            {"ticker": "MSFT", "direction": "bullish"},  # tech
            {"ticker": "GOOGL", "direction": "bullish"}, # tech
        ]
        # JPM is financials — different sector, should pass
        tracker._check_sector_concentration("JPM", open_positions)

    def test_check_can_trade_passes_ticker(self):
        from trading_platform.services.risk_budget import CircuitBreakerTripped
        tracker = self._make_tracker()
        open_positions = [
            {"ticker": "MSFT", "direction": "bullish"},
            {"ticker": "GOOGL", "direction": "bullish"},
        ]
        with pytest.raises(CircuitBreakerTripped, match="tech"):
            tracker.check_can_trade("bullish", open_positions, ticker="AAPL")

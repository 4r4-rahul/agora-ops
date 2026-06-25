"""
Unit tests for the AGORA money guards — the synchronous LAST line of defense
against catastrophic loss.

Covered:
  agora/risk/compliance.py       — ComplianceAgent.check_trade and helpers
  agora/risk/circuit_breaker.py  — CircuitBreakerAgent daily-loss / realized-P&L

These are pure synchronous guards: NO LLM, NO network, NO IBKR. Every test that
touches sqlite uses a pytest tmp_path DB via settings override — the real
.agora/agora.db is never opened. Inputs are built from the REAL pydantic models
(agora.core.models) and real settings (get_settings().model_copy(...)).
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pytest

from agora.core.config import get_settings
from agora.core.models import (
    OpenPosition,
    PositionStatus,
    SpreadLeg,
    StrategyPillar,
    StrategyType,
    TradeRecommendation,
)
from agora.risk.circuit_breaker import CircuitBreakerAgent
from agora.risk.compliance import ComplianceAgent
from agora.risk.risk_council import RiskCouncil

# ── Fixtures / builders ───────────────────────────────────────────────────────

@pytest.fixture
def settings(tmp_path):
    """Real settings with db_path redirected into tmp — NEVER touch the live DB.

    Pin account_size=10_000 so the concentration limit (20% = $2,000) and the
    daily-loss breaker ($2,000) are deterministic regardless of .env.
    """
    s = get_settings()
    return s.model_copy(update={
        "db_path": tmp_path / "test_agora.db",
        "account_size": 10_000.0,
        "daily_loss_limit_pct": 0.20,   # → daily_loss_limit_dollars = $2,000
    })


def _expiry(days: int = 45) -> date:
    return date.today() + timedelta(days=days)


def _leg(option_type="call", action="buy", strike=100.0, contracts=1) -> SpreadLeg:
    return SpreadLeg(
        option_type=option_type,
        strike=strike,
        expiration=_expiry(),
        action=action,
        contracts=contracts,
    )


def _debit_spread_rec(
    ticker="SPY",
    contracts=1,
    max_loss_dollars=300.0,
    max_gain_dollars=700.0,
    entry_debit_credit=3.00,
    conviction=70.0,
) -> TradeRecommendation:
    """A clean, compliant bull call (debit) spread — two legs, one buy one sell."""
    return TradeRecommendation(
        session_id="t-sess",
        ticker=ticker,
        strategy=StrategyType.BULL_CALL_SPREAD,
        pillar=StrategyPillar.DIRECTIONAL,
        direction="bullish",
        legs=[
            _leg("call", "buy", 100.0, contracts),
            _leg("call", "sell", 110.0, contracts),
        ],
        contracts=contracts,
        entry_debit_credit=entry_debit_credit,
        max_loss_dollars=max_loss_dollars,
        max_gain_dollars=max_gain_dollars,
        reward_risk_ratio=max_gain_dollars / max(max_loss_dollars, 1),
        conviction_score=conviction,
    )


def _naked_call_rec(ticker="SPY") -> TradeRecommendation:
    return TradeRecommendation(
        session_id="t-sess",
        ticker=ticker,
        strategy=StrategyType.NAKED_CALL,
        pillar=StrategyPillar.VOL_PREMIUM,
        direction="bearish",
        legs=[_leg("call", "sell", 110.0)],
        contracts=1,
        entry_debit_credit=-1.50,
        max_loss_dollars=5_000.0,
        max_gain_dollars=150.0,
        reward_risk_ratio=0.03,
        conviction_score=90.0,
    )


def _naked_put_rec(ticker="SPY", conviction=70.0) -> TradeRecommendation:
    return TradeRecommendation(
        session_id="t-sess",
        ticker=ticker,
        strategy=StrategyType.NAKED_PUT,
        pillar=StrategyPillar.VOL_PREMIUM,
        direction="bullish",
        legs=[_leg("put", "sell", 90.0)],
        contracts=1,
        entry_debit_credit=-2.00,
        max_loss_dollars=9_000.0,
        max_gain_dollars=200.0,
        reward_risk_ratio=0.02,
        conviction_score=conviction,
    )


def _open_position(ticker="SPY", max_loss_dollars=1_000.0, contracts=1) -> OpenPosition:
    return OpenPosition(
        position_id=f"pos-{ticker}-{max_loss_dollars}",
        ticker=ticker,
        strategy=StrategyType.BULL_CALL_SPREAD,
        pillar=StrategyPillar.DIRECTIONAL,
        direction="bullish",
        status=PositionStatus.OPEN,
        legs=[_leg("call", "buy", 100.0), _leg("call", "sell", 110.0)],
        contracts=contracts,
        entry_price=3.0,
        entry_date=date.today(),
        expiry_date=_expiry(),
        target_close_date=date.today() + timedelta(days=21),
        max_loss_dollars=max_loss_dollars,
        max_gain_dollars=2_000.0,
    )


# ── ComplianceAgent: clean trade allowed ──────────────────────────────────────

class TestComplianceClean:
    def test_clean_trade_allowed(self, settings):
        agent = ComplianceAgent(settings=settings)
        rec = _debit_spread_rec(max_loss_dollars=300.0, contracts=1)
        result = agent.check_trade(rec, open_positions=[], account_buying_power=10_000.0)
        assert result["compliant"] is True
        assert isinstance(result["warnings"], list)

    def test_clean_trade_no_buying_power_data_still_allowed(self, settings):
        # buying_power=None → BP check is skipped (can't check), trade still allowed.
        agent = ComplianceAgent(settings=settings)
        rec = _debit_spread_rec()
        result = agent.check_trade(rec, open_positions=[], account_buying_power=None)
        assert result["compliant"] is True


# ── ComplianceAgent: concentration ────────────────────────────────────────────

class TestComplianceConcentration:
    def test_concentration_blocks_over_20pct(self, settings):
        # account_size=$10k → max single-ticker risk = $2,000.
        # New trade risk = max_loss × contracts = $2,500 × 1 > $2,000 → BLOCK.
        agent = ComplianceAgent(settings=settings)
        rec = _debit_spread_rec(ticker="NVDA", max_loss_dollars=2_500.0, contracts=1)
        result = agent.check_trade(rec, open_positions=[], account_buying_power=50_000.0)
        assert result["compliant"] is False
        assert "Concentration" in result["reason"]

    def test_concentration_allows_under_20pct(self, settings):
        # $1,500 risk < $2,000 cap → allowed.
        agent = ComplianceAgent(settings=settings)
        rec = _debit_spread_rec(ticker="NVDA", max_loss_dollars=1_500.0, contracts=1)
        result = agent.check_trade(rec, open_positions=[], account_buying_power=50_000.0)
        assert result["compliant"] is True

    def test_concentration_counts_existing_same_ticker(self, settings):
        # Existing $1,500 in NVDA + new $1,000 = $2,500 > $2,000 → BLOCK.
        # The same new $1,000 trade with NO existing position would pass — proving
        # the block comes from the AGGREGATE, not the single trade.
        agent = ComplianceAgent(settings=settings)
        existing = [_open_position(ticker="NVDA", max_loss_dollars=1_500.0, contracts=1)]
        rec = _debit_spread_rec(ticker="NVDA", max_loss_dollars=1_000.0, contracts=1)

        blocked = agent.check_trade(rec, open_positions=existing, account_buying_power=50_000.0)
        assert blocked["compliant"] is False
        assert "Concentration" in blocked["reason"]

        allowed = agent.check_trade(rec, open_positions=[], account_buying_power=50_000.0)
        assert allowed["compliant"] is True

    def test_concentration_other_ticker_does_not_count(self, settings):
        # Existing risk is in AAPL; new trade is NVDA — AAPL must not count against NVDA.
        agent = ComplianceAgent(settings=settings)
        existing = [_open_position(ticker="AAPL", max_loss_dollars=1_900.0, contracts=1)]
        rec = _debit_spread_rec(ticker="NVDA", max_loss_dollars=1_500.0, contracts=1)
        result = agent.check_trade(rec, open_positions=existing, account_buying_power=50_000.0)
        assert result["compliant"] is True

    def test_concentration_boundary_at_exactly_20pct_allowed(self, settings):
        # Exactly $2,000 == cap; block fires only when total > cap, so == is allowed.
        agent = ComplianceAgent(settings=settings)
        rec = _debit_spread_rec(ticker="NVDA", max_loss_dollars=2_000.0, contracts=1)
        result = agent.check_trade(rec, open_positions=[], account_buying_power=50_000.0)
        assert result["compliant"] is True

    def test_concentration_just_over_boundary_blocked(self, settings):
        agent = ComplianceAgent(settings=settings)
        rec = _debit_spread_rec(ticker="NVDA", max_loss_dollars=2_000.01, contracts=1)
        result = agent.check_trade(rec, open_positions=[], account_buying_power=50_000.0)
        assert result["compliant"] is False


# ── ComplianceAgent: wash sale (advisory — warns, does not block) ─────────────

class TestComplianceWashSale:
    def test_recent_loss_flags_wash_sale_warning(self, settings):
        agent = ComplianceAgent(settings=settings)
        # Record a loss close today → within the 30-day window.
        agent.record_close("SPY", realized_pnl=-250.0, strategy="bull_call_spread")

        ok, msg = agent._check_wash_sale("SPY")
        assert ok is False
        assert "wash sale" in msg.lower()

        # check_trade is advisory: it must still ALLOW but surface a warning.
        rec = _debit_spread_rec(ticker="SPY", max_loss_dollars=300.0)
        result = agent.check_trade(rec, open_positions=[], account_buying_power=10_000.0)
        assert result["compliant"] is True
        assert any("WASH SALE" in w for w in result["warnings"])

    def test_winning_close_does_not_flag(self, settings):
        agent = ComplianceAgent(settings=settings)
        agent.record_close("SPY", realized_pnl=+400.0, strategy="bull_call_spread")
        ok, _ = agent._check_wash_sale("SPY")
        assert ok is True

    def test_loss_outside_30day_window_does_not_flag(self, settings):
        agent = ComplianceAgent(settings=settings)
        # Insert a loss close 40 days ago directly (record_close always stamps today).
        old = (date.today() - timedelta(days=40)).isoformat()
        agent._db.execute(
            "INSERT INTO wash_sale_log (ticker, close_date, realized_pnl, strategy, flagged) "
            "VALUES (?, ?, ?, ?, ?)",
            ("SPY", old, -250.0, "bull_call_spread", 1),
        )
        agent._db.commit()
        ok, _ = agent._check_wash_sale("SPY")
        assert ok is True

    def test_loss_in_other_ticker_does_not_flag(self, settings):
        agent = ComplianceAgent(settings=settings)
        agent.record_close("AAPL", realized_pnl=-250.0)
        ok, _ = agent._check_wash_sale("SPY")
        assert ok is True


# ── ComplianceAgent: Reg T buying power (advisory warning, not a hard block) ───

class TestComplianceBuyingPower:
    def test_buying_power_within_budget(self, settings):
        agent = ComplianceAgent(settings=settings)
        # Debit cost = entry_debit_credit × contracts × 100 = 3.00 × 1 × 100 = $300.
        ok, msg = agent._check_buying_power(_debit_spread_rec(entry_debit_credit=3.00), buying_power=10_000.0)
        assert ok is True

    def test_buying_power_exceeds_90pct_blocked(self, settings):
        agent = ComplianceAgent(settings=settings)
        # Cost = 9.50 × 1 × 100 = $950 vs BP $1,000 → 95% > 90% → not OK.
        rec = _debit_spread_rec(entry_debit_credit=9.50, contracts=1)
        ok, msg = agent._check_buying_power(rec, buying_power=1_000.0)
        assert ok is False
        assert "buying power" in msg.lower()

    def test_buying_power_none_skips_check(self, settings):
        agent = ComplianceAgent(settings=settings)
        ok, _ = agent._check_buying_power(_debit_spread_rec(), buying_power=None)
        assert ok is True

    def test_buying_power_credit_spread_uses_max_loss_collateral(self, settings):
        # Credit spread: entry_debit_credit <= 0 → collateral = max_loss × contracts.
        agent = ComplianceAgent(settings=settings)
        rec = _debit_spread_rec(entry_debit_credit=-1.00, max_loss_dollars=950.0, contracts=1)
        ok, _ = agent._check_buying_power(rec, buying_power=1_000.0)  # $950/$1000 = 95% → block
        assert ok is False

    def test_buying_power_warning_does_not_block_full_check(self, settings):
        # Over-BP is a WARNING in check_trade, not a hard block (only concentration /
        # naked-call hard-block). Concentration here is fine ($300 < $2k).
        agent = ComplianceAgent(settings=settings)
        rec = _debit_spread_rec(ticker="SPY", entry_debit_credit=9.50, max_loss_dollars=300.0)
        result = agent.check_trade(rec, open_positions=[], account_buying_power=1_000.0)
        assert result["compliant"] is True
        assert any("BUYING POWER" in w for w in result["warnings"])


# ── ComplianceAgent: naked-strategy hard blocks ───────────────────────────────

class TestComplianceStrategyLevel:
    def test_naked_short_call_always_blocked(self, settings):
        agent = ComplianceAgent(settings=settings)
        result = agent.check_trade(_naked_call_rec(), open_positions=[], account_buying_power=50_000.0)
        assert result["compliant"] is False
        assert "Naked short CALL" in result["reason"]

    def test_naked_short_put_blocked_below_conviction_floor(self, settings):
        agent = ComplianceAgent(settings=settings)
        result = agent.check_trade(
            _naked_put_rec(conviction=70.0), open_positions=[], account_buying_power=500_000.0
        )
        assert result["compliant"] is False
        assert "Naked short PUT" in result["reason"]

    def test_naked_short_put_allowed_at_high_conviction(self, settings):
        # conviction ≥ 80 → cash-secured put allowed (defined max loss).
        # Give it huge buying power so the BP check passes and concentration is fine.
        agent = ComplianceAgent(settings=settings)
        rec = _naked_put_rec(conviction=85.0)
        rec = rec.model_copy(update={"max_loss_dollars": 1_500.0})  # < $2k concentration cap
        result = agent.check_trade(rec, open_positions=[], account_buying_power=500_000.0)
        assert result["compliant"] is True


# ── CircuitBreaker: today's realized P&L summation (tmp sqlite) ────────────────

def _seed_trade_records(db_path, rows):
    """Create a minimal trade_records table and insert (realized_pnl, close_date) rows."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS trade_records ("
        "  trade_id TEXT, realized_pnl REAL, close_date TEXT"
        ")"
    )
    conn.executemany(
        "INSERT INTO trade_records (trade_id, realized_pnl, close_date) VALUES (?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def _seed_positions(db_path, rows, *, close_source="lifecycle"):
    """Seed the positions table with REAL-CLOSE rows the breaker actually reads.

    _get_todays_realized_pnl was repointed off trade_records (model marks that disagree in
    SIGN with the fill) onto positions/_REAL_CLOSE — the single trustworthy P&L source. The
    breaker tests must therefore seed positions, not trade_records. Each row is
    (position_id, realized_pnl, close_date); close_source defaults to a trusted provenance so
    the _REAL_CLOSE predicate counts it."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS positions ("
        "  position_id TEXT, realized_pnl REAL, close_date TEXT, "
        "  status TEXT, close_source TEXT, regime_at_entry TEXT DEFAULT 'neutral'"
        ")"
    )
    conn.executemany(
        "INSERT INTO positions (position_id, realized_pnl, close_date, status, close_source) "
        "VALUES (?, ?, ?, 'closed', ?)",
        [(pid, pnl, cd, close_source) for (pid, pnl, cd) in rows],
    )
    conn.commit()
    conn.close()


class TestCircuitBreakerRealizedPnl:
    def test_sums_only_todays_closes(self, settings):
        today = date.today().isoformat()
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        _seed_positions(settings.db_path, [
            ("a", -500.0, today),
            ("b", -300.0, today),
            ("c", +200.0, today),
            ("d", -9_999.0, yesterday),   # different day — must be excluded
        ])
        cb = CircuitBreakerAgent(settings=settings)
        # -500 -300 +200 = -600 ; yesterday's -9,999 excluded.
        assert cb._get_todays_realized_pnl() == pytest.approx(-600.0)

    def test_no_records_returns_zero(self, settings):
        _seed_trade_records(settings.db_path, [])
        cb = CircuitBreakerAgent(settings=settings)
        assert cb._get_todays_realized_pnl() == 0.0

    def test_missing_table_returns_zero(self, settings):
        # No trade_records table at all — guard must swallow the error and return 0.0.
        cb = CircuitBreakerAgent(settings=settings)
        assert cb._get_todays_realized_pnl() == 0.0


# ── CircuitBreaker: daily-loss trip decision at the $2k threshold ──────────────

class _StubPos:
    def __init__(self, unrealized_pnl):
        self.unrealized_pnl = unrealized_pnl


class _StubPositionMgr:
    def __init__(self, positions):
        self._positions = positions

    def get_open_positions(self):
        return list(self._positions)


class TestCircuitBreakerDailyLossThreshold:
    """The trip rule is `daily_pnl < -limit` where
    daily_pnl = (total_unrealized - baseline) + realized_today and
    limit = settings.daily_loss_limit_dollars ($2,000 at account_size=$10k, pct=0.20).

    The full evaluation lives in the async _check_cycle (which also calls yfinance /
    RiskCouncil). We test the deterministic money decision directly against the SAME
    real settings threshold, with the realized component sourced from the tmp DB.
    """

    def _daily_pnl(self, cb, total_unrealized, baseline):
        return (total_unrealized - baseline) + cb._get_todays_realized_pnl()

    def test_trips_when_loss_exceeds_limit(self, settings):
        assert settings.daily_loss_limit_dollars == pytest.approx(2_000.0)
        # Realized -$1,500 today + unrealized drop of -$600 = -$2,100 < -$2,000 → TRIP.
        _seed_positions(settings.db_path, [("x", -1_500.0, date.today().isoformat())])
        cb = CircuitBreakerAgent(settings=settings)
        daily_pnl = self._daily_pnl(cb, total_unrealized=-600.0, baseline=0.0)
        assert daily_pnl == pytest.approx(-2_100.0)
        assert daily_pnl < -settings.daily_loss_limit_dollars   # breaker would trip

    def test_does_not_trip_when_loss_within_limit(self, settings):
        # Realized -$500 + unrealized -$1,000 = -$1,500 > -$2,000 → NO trip.
        _seed_positions(settings.db_path, [("x", -500.0, date.today().isoformat())])
        cb = CircuitBreakerAgent(settings=settings)
        daily_pnl = self._daily_pnl(cb, total_unrealized=-1_000.0, baseline=0.0)
        assert daily_pnl == pytest.approx(-1_500.0)
        assert daily_pnl >= -settings.daily_loss_limit_dollars  # breaker holds

    def test_baseline_absorbs_prior_session_loss(self, settings):
        # If the baseline already reflects a -$3,000 carried-over mark, only TODAY's
        # additional movement counts: mark -$3,200 vs baseline -$3,000 = -$200 today.
        _seed_trade_records(settings.db_path, [])
        cb = CircuitBreakerAgent(settings=settings)
        daily_pnl = self._daily_pnl(cb, total_unrealized=-3_200.0, baseline=-3_000.0)
        assert daily_pnl == pytest.approx(-200.0)
        assert daily_pnl >= -settings.daily_loss_limit_dollars  # prior loss NOT re-counted


# ── CircuitBreaker: rebaseline_daily_loss re-anchors to current unrealized ─────

class TestCircuitBreakerRebaseline:
    def test_rebaseline_anchors_to_current_unrealized(self, settings):
        mgr = _StubPositionMgr([_StubPos(-1_200.0), _StubPos(-800.0)])  # total -$2,000
        cb = CircuitBreakerAgent(settings=settings, position_mgr=mgr)
        cb.rebaseline_daily_loss(reason="operator reset")

        assert cb._daily_unrealized_baseline == pytest.approx(-2_000.0)
        assert cb._baseline_date == date.today().isoformat()
        assert cb._last_unrealized == pytest.approx(-2_000.0)

        # After re-anchor, a fresh daily_pnl with the SAME marks is ~0 → would not re-trip.
        _seed_trade_records(settings.db_path, [])
        daily_pnl = (-2_000.0 - cb._daily_unrealized_baseline) + cb._get_todays_realized_pnl()
        assert daily_pnl == pytest.approx(0.0)

    def test_rebaseline_persists_to_disk(self, settings):
        mgr = _StubPositionMgr([_StubPos(-500.0)])
        cb = CircuitBreakerAgent(settings=settings, position_mgr=mgr)
        cb.rebaseline_daily_loss()
        # A fresh agent loads the same baseline from the persisted cb_baseline.json.
        cb2 = CircuitBreakerAgent(settings=settings, position_mgr=mgr)
        assert cb2._daily_unrealized_baseline == pytest.approx(-500.0)

    def test_rebaseline_with_no_position_mgr_is_safe(self, settings):
        cb = CircuitBreakerAgent(settings=settings, position_mgr=None)
        cb.rebaseline_daily_loss()  # no positions → baseline 0.0
        assert cb._daily_unrealized_baseline == pytest.approx(0.0)


# ── RiskCouncil.record_daily_pnl: paper breaker-off must NOT auto-trip ─────────
# Regression for the bug that halted the paper data-collection engine on 2026-06-23:
# record_daily_pnl() auto-tripped the kill switch on a loss breach WITHOUT honouring
# paper_disable_loss_breakers, while every other trip path (RiskCouncil._check_daily_loss
# and CircuitBreakerAgent) did honour it. So the one path meant to be silent in paper
# mode was the one that fired. The ledger must keep updating either way.
class TestRecordDailyPnlPaperBreakerOff:
    def _paper(self, settings, breakers_disabled: bool):
        return settings.model_copy(update={
            "trading_mode": "paper",
            "paper_disable_loss_breakers": breakers_disabled,
        })

    def test_no_trip_when_paper_breakers_disabled(self, settings):
        rc = RiskCouncil(settings=self._paper(settings, breakers_disabled=True))
        # -$3,000 < -$2,000 limit — would trip if the flag were ignored (the bug).
        rc.record_daily_pnl(realized=-3_000.0, unrealized=0.0, trades=5)
        assert rc.is_kill_switch_active() is False
        # …but the ledger MUST still record the loss (bookkeeping never gated).
        row = rc._db.execute(
            "SELECT realized_pnl FROM daily_pnl WHERE record_date=?",
            (date.today().isoformat(),),
        ).fetchone()
        assert row is not None and row[0] == pytest.approx(-3_000.0)

    def test_trips_when_paper_breakers_enabled(self, settings):
        rc = RiskCouncil(settings=self._paper(settings, breakers_disabled=False))
        rc.record_daily_pnl(realized=-3_000.0, unrealized=0.0, trades=5)
        assert rc.is_kill_switch_active() is True

    def test_no_trip_when_loss_within_limit(self, settings):
        rc = RiskCouncil(settings=self._paper(settings, breakers_disabled=False))
        rc.record_daily_pnl(realized=-500.0, unrealized=-1_000.0, trades=3)  # -$1,500 > -$2,000
        assert rc.is_kill_switch_active() is False

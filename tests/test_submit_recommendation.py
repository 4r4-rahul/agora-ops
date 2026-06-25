"""
Integration tests for AgoraSession._submit_recommendation().

Tests the full 7-gate sequence:
  1. Entry timing gate         — outside 10:00–15:30 ET → block
  1b. Macro calendar gate      — FOMC avoid day → block
  2. VIX stress mode           — size reduction applied
  3. Compliance gate           — wash sale / concentration → block
  4. Risk council              — StrategyHealth pause, Greeks limits → block
  4c. Execution cooldown       — recently timed-out ticker → block
  4b. Open combo order limit   — too many GTC brackets → block
  4e. DevilsAdvocate           — 5-check checklist → block
  PASS → submit_trade() called exactly once

All external I/O is mocked: no network, no IBKR, no Claude API, no real SQLite writes.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_leg(expiry_days: int = 30, strike: float = 100.0, opt_type: str = "put") -> Any:
    leg = MagicMock()
    leg.expiration = date.today() + timedelta(days=expiry_days)
    leg.strike     = strike
    leg.option_type = opt_type
    leg.action     = "sell"
    leg.delta      = -0.20
    leg.gamma      = 0.01
    leg.theta      = -0.05
    leg.vega       = 0.10
    return leg


def _make_recommendation(
    ticker: str = "AAPL",
    pillar: str = "vol_premium",
    direction: str = "neutral",
    conviction: float = 65.0,
    contracts: int = 1,
    entry_debit_credit: float = -150.0,   # negative = credit received
    max_loss: float = 350.0,
    max_gain: float = 150.0,
    expiry_days: int = 30,
) -> Any:
    rec = MagicMock()
    rec.ticker            = ticker
    rec.pillar            = MagicMock()
    rec.pillar.value      = pillar
    rec.direction         = direction
    rec.conviction_score  = conviction
    rec.contracts         = contracts
    rec.entry_debit_credit = entry_debit_credit
    rec.max_loss_dollars  = max_loss
    rec.max_gain_dollars  = max_gain
    rec.reward_risk_ratio = max_gain / max_loss
    rec.stop_loss_pct     = 2.0
    rec.size_multiplier   = 1.0
    rec.strategy          = MagicMock()
    rec.strategy.value    = "bull_put_spread"
    rec.legs              = [_make_leg(expiry_days)]
    return rec


def _make_session() -> Any:
    """
    Build a minimal AgoraSession-like object with all dependencies mocked.
    Uses __new__ to skip __init__ so we control every attribute.
    """
    from agora.session import AgoraSession

    s = AgoraSession.__new__(AgoraSession)

    # Basic session state
    s._session_id       = "TEST-SESSION"
    s._running          = True
    s._macro_context    = MagicMock()
    s._macro_context.macro_stance   = "neutral"
    s._macro_context.vol_selling_ok = True
    s._macro_context.confidence     = 0.7
    s._exec_cooldowns   = {}
    s._inflight_tickers = set()   # M1 idempotency wrapper (added after this fixture was written)
    s._error_201_blocked = set()  # per-session Error-201 block set (reject path)

    # Settings
    s._settings = MagicMock()
    s._settings.gtc_max_open_combo_orders = 5
    s._settings.discord_bot_token         = None   # disable Discord gate
    s._settings.discord_approval_user_id  = None
    s._settings.high_conviction_score     = 80.0
    s._settings.db_path                   = Path(tempfile.NamedTemporaryFile(suffix=".db").name)
    s._settings.max_open_positions        = 10
    s._settings.max_contracts_per_trade   = 10   # hard contract-cap guard (spreads)
    s._settings.long_options_max_contracts = 5   # hard contract-cap guard (long options)
    s._settings.max_portfolio_delta       = 100.0
    s._settings.max_portfolio_vega        = 5000.0
    s._settings.max_daily_theta_dollars   = 500.0
    s._settings.max_per_correlation_group = 2
    s._settings.min_rr_ratio              = 0.3
    s._settings.min_credit_spread_rr_ratio = 0.3
    # Global exposure ceiling (gate #3, added after this fixture was written) — non-binding here.
    s._settings.max_total_open_positions       = 1000
    s._settings.max_total_capital_deployed_pct = 1.0    # 1.0 = capital check skipped
    s._settings.account_size                   = 10_000.0

    # Entry timing — PERMIT by default
    s._entry_timing = MagicMock()
    s._entry_timing.is_entry_permitted.return_value = (True, "")

    # Circuit breaker — no stress mode by default
    s._circuit_breaker = MagicMock()
    s._circuit_breaker.vix_stress_mode          = False
    s._circuit_breaker.size_multiplier_override = 0.5

    # Position manager — no open positions, zero greeks by default
    s._position_mgr = MagicMock()
    s._position_mgr.get_open_positions.return_value = []
    s._position_mgr.get_portfolio_greeks.return_value = {
        "delta": 0.0, "vega": 0.0, "theta": 0.0, "positions": 0,
    }

    # Compliance — approved by default
    s._compliance = MagicMock()
    s._compliance.check_trade.return_value = {"compliant": True, "reason": "", "warnings": []}

    # Risk council — approved by default
    s._risk = MagicMock()
    s._risk.approve_trade.return_value = {"approved": True, "reason": "All checks passed", "checks": {}}
    s._risk.is_kill_switch_active.return_value = False   # _entry_gate kill-switch check (added later)

    # Correlation monitor — no correlated exposure by default (gate added to _entry_gate later)
    s._correlation_monitor = MagicMock()
    _corr = MagicMock()
    _corr.risk_level = "none"
    _corr.conviction_adj = 0
    _corr.block_reason = ""
    s._correlation_monitor.check.return_value = _corr

    # Event-risk surgical gate — allow by default (added to _entry_gate later)
    s._event_risk_assessment = MagicMock(return_value=("allow", ""))

    # Exec quality — records silently; never skips by default (re-submission storm guard added later)
    s._exec_quality = MagicMock()
    s._exec_quality.should_skip_symbol.return_value = (False, "")

    # Orphan reconciler — needed when Error 201 triggers reconcile_now()
    s._orphan_reconciler = MagicMock()
    s._orphan_reconciler.reconcile_now = AsyncMock()

    # Execution cooldown window (set in __init__ in real session)
    s._EXEC_COOLDOWN_SECS = 7200

    # Phase 5/6 intelligence agents — disabled by default in tests
    s._strategy_selector = None
    s._advocate = None
    s._exit_agent = None
    s._defender = None          # debate gate's defender (added after this fixture was written)
    s._price_target = None      # post-fill price-target enrichment (added later) → skipped

    return s


# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _no_ibkr_reprice():
    """reprice_legs() (added to the submit path after this suite was written) makes a real IBKR
    connection. Stub it to None everywhere so the gate-sequence tests stay hermetic — None means
    'keep the yfinance economics', which is the no-op path these tests assume."""
    with patch("agora.session.reprice_legs", new_callable=AsyncMock, return_value=None):
        yield


class TestSubmitRecommendationGateSequence:
    """Each test flips exactly one gate and verifies submit_trade is NOT called."""

    @pytest.mark.asyncio
    async def test_happy_path_calls_submit_trade(self):
        """All gates pass → submit_trade() called exactly once."""
        s   = _make_session()
        rec = _make_recommendation()
        order = {"status": "Filled", "order_id": 1, "fills": [{"price": 1.50}]}

        s._record_position = MagicMock()
        with patch("agora.session.get_macro_calendar") as mock_cal, \
             patch("agora.session.submit_trade", new_callable=AsyncMock, return_value=order) as mock_submit, \
             patch("agora.session._log_chain", return_value="chain-001"):
            mock_cal.return_value.should_trade.return_value           = (True, "")
            mock_cal.return_value.position_size_multiplier.return_value = 1.0
            await s._submit_recommendation(rec, "AAPL", 150.0)

        mock_submit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_timing_gate_blocks(self):
        """Entry timing gate → submit_trade never called."""
        s   = _make_session()
        rec = _make_recommendation()
        s._entry_timing.is_entry_permitted.return_value = (False, "After hours")

        with patch("agora.session.submit_trade", new_callable=AsyncMock) as mock_submit:
            await s._submit_recommendation(rec, "AAPL", 150.0)

        mock_submit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_macro_calendar_blocks(self):
        """Macro calendar avoid day → submit_trade never called."""
        s   = _make_session()
        rec = _make_recommendation()

        with patch("agora.session.get_macro_calendar") as mock_cal, \
             patch("agora.session.submit_trade", new_callable=AsyncMock) as mock_submit:
            mock_cal.return_value.should_trade.return_value = (False, "FOMC avoid day")
            # The pre-gate sizing step reads position_size_multiplier() before the avoid-day
            # block fires inside _entry_gate — give it a real number (1.0 = no resize).
            mock_cal.return_value.position_size_multiplier.return_value = 1.0
            await s._submit_recommendation(rec, "AAPL", 150.0)

        mock_submit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_macro_calendar_reduces_size(self):
        """Macro calendar caution day → contracts scaled down, trade still proceeds."""
        s   = _make_session()
        rec = _make_recommendation(contracts=4)
        order = {"status": "Filled", "order_id": 2, "fills": [{"price": 1.5}]}

        s._record_position = MagicMock()
        with patch("agora.session.get_macro_calendar") as mock_cal, \
             patch("agora.session.submit_trade", new_callable=AsyncMock, return_value=order) as mock_submit, \
             patch("agora.session._log_chain", return_value="chain-002"):
            mock_cal.return_value.should_trade.return_value           = (True, "")
            mock_cal.return_value.position_size_multiplier.return_value = 0.5
            await s._submit_recommendation(rec, "AAPL", 150.0)

        # Contracts should have been halved (4 × 0.5 = 2)
        assert rec.contracts == 2
        mock_submit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_vix_stress_mode_reduces_size(self):
        """VIX stress mode → contracts reduced but trade still reaches submit."""
        s   = _make_session()
        rec = _make_recommendation(contracts=4)
        s._circuit_breaker.vix_stress_mode          = True
        s._circuit_breaker.size_multiplier_override = 0.5
        order = {"status": "Filled", "order_id": 3, "fills": [{"price": 1.5}]}

        s._record_position = MagicMock()
        with patch("agora.session.get_macro_calendar") as mock_cal, \
             patch("agora.session.submit_trade", new_callable=AsyncMock, return_value=order) as mock_submit, \
             patch("agora.session._log_chain", return_value="chain-003"):
            mock_cal.return_value.should_trade.return_value           = (True, "")
            mock_cal.return_value.position_size_multiplier.return_value = 1.0
            await s._submit_recommendation(rec, "AAPL", 150.0)

        assert rec.contracts == 2
        mock_submit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_hard_contract_cap_guard_clamps_oversized(self):
        """An oversized recommendation (any builder/override) is clamped to max_contracts_per_trade
        before submit — the defense-in-depth guard for the DIA-21/NOK-12 cap breach."""
        s   = _make_session()
        rec = _make_recommendation(contracts=21)   # bull_put_spread → spread cap (10)
        order = {"status": "Filled", "order_id": 9, "fills": [{"price": 1.5}]}
        s._record_position = MagicMock()
        with patch("agora.session.get_macro_calendar") as mock_cal, \
             patch("agora.session.submit_trade", new_callable=AsyncMock, return_value=order) as mock_submit, \
             patch("agora.session._log_chain", return_value="chain-009"):
            mock_cal.return_value.should_trade.return_value           = (True, "")
            mock_cal.return_value.position_size_multiplier.return_value = 1.0
            await s._submit_recommendation(rec, "AAPL", 150.0)

        assert rec.contracts == 10        # clamped from 21 to the spread cap
        mock_submit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_compliance_blocks(self):
        """Compliance gate failure → submit_trade never called."""
        s   = _make_session()
        rec = _make_recommendation()
        s._compliance.check_trade.return_value = {
            "compliant": False,
            "reason": "Wash sale — sold AAPL within 30 days",
            "warnings": [],
        }

        with patch("agora.session.get_macro_calendar") as mock_cal, \
             patch("agora.session.submit_trade", new_callable=AsyncMock) as mock_submit:
            mock_cal.return_value.should_trade.return_value           = (True, "")
            mock_cal.return_value.position_size_multiplier.return_value = 1.0
            await s._submit_recommendation(rec, "AAPL", 150.0)

        mock_submit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_risk_council_blocks(self):
        """Risk council rejection → submit_trade never called."""
        s   = _make_session()
        rec = _make_recommendation()
        s._risk.approve_trade.return_value = {
            "approved": False,
            "reason":   "Delta limit breach",
            "checks":   {"portfolio_delta": False},
        }

        with patch("agora.session.get_macro_calendar") as mock_cal, \
             patch("agora.session.submit_trade", new_callable=AsyncMock) as mock_submit:
            mock_cal.return_value.should_trade.return_value           = (True, "")
            mock_cal.return_value.position_size_multiplier.return_value = 1.0
            await s._submit_recommendation(rec, "AAPL", 150.0)

        mock_submit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_execution_cooldown_blocks(self):
        """Ticker under execution cooldown → submit_trade never called."""
        s   = _make_session()
        rec = _make_recommendation()
        # Set cooldown to 90 minutes from now
        s._exec_cooldowns["AAPL"] = datetime.now(tz=UTC) + timedelta(minutes=90)

        with patch("agora.session.get_macro_calendar") as mock_cal, \
             patch("agora.session.submit_trade", new_callable=AsyncMock) as mock_submit:
            mock_cal.return_value.should_trade.return_value           = (True, "")
            mock_cal.return_value.position_size_multiplier.return_value = 1.0
            await s._submit_recommendation(rec, "AAPL", 150.0)

        mock_submit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_expired_cooldown_passes(self):
        """Expired execution cooldown → gate passes, submit_trade called."""
        s   = _make_session()
        rec = _make_recommendation()
        # Cooldown expired 1 minute ago
        s._exec_cooldowns["AAPL"] = datetime.now(tz=UTC) - timedelta(minutes=1)
        order = {"status": "Filled", "order_id": 4, "fills": [{"price": 1.5}]}

        s._record_position = MagicMock()
        with patch("agora.session.get_macro_calendar") as mock_cal, \
             patch("agora.session.submit_trade", new_callable=AsyncMock, return_value=order) as mock_submit, \
             patch("agora.session._log_chain", return_value="chain-004"):
            mock_cal.return_value.should_trade.return_value           = (True, "")
            mock_cal.return_value.position_size_multiplier.return_value = 1.0
            await s._submit_recommendation(rec, "AAPL", 150.0)

        mock_submit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_open_combo_limit_blocks(self):
        """At or above GTC combo order limit → submit_trade never called."""
        s   = _make_session()
        rec = _make_recommendation()
        s._settings.gtc_max_open_combo_orders = 3
        # Simulate 3 open positions (at the limit)
        s._position_mgr.get_open_positions.return_value = [MagicMock(ticker="X")] * 3

        with patch("agora.session.get_macro_calendar") as mock_cal, \
             patch("agora.session.submit_trade", new_callable=AsyncMock) as mock_submit:
            mock_cal.return_value.should_trade.return_value           = (True, "")
            mock_cal.return_value.position_size_multiplier.return_value = 1.0
            await s._submit_recommendation(rec, "AAPL", 150.0)

        mock_submit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_devils_advocate_duplicate_ticker_blocks(self):
        """DevilsAdvocate duplicate ticker check → submit_trade never called."""
        s   = _make_session()
        rec = _make_recommendation(ticker="AAPL")

        # Open position in the same ticker
        existing = MagicMock()
        existing.ticker      = "AAPL"
        existing.position_id = "pos-existing"
        s._position_mgr.get_open_positions.return_value = [existing]

        with patch("agora.session.get_macro_calendar") as mock_cal, \
             patch("agora.session.submit_trade", new_callable=AsyncMock) as mock_submit:
            mock_cal.return_value.should_trade.return_value           = (True, "")
            mock_cal.return_value.position_size_multiplier.return_value = 1.0
            await s._submit_recommendation(rec, "AAPL", 150.0)

        mock_submit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_devils_advocate_conviction_floor_blocks(self):
        """DevilsAdvocate conviction floor < 30 → submit_trade never called."""
        s   = _make_session()
        rec = _make_recommendation(conviction=15.0)  # below floor of 30

        with patch("agora.session.get_macro_calendar") as mock_cal, \
             patch("agora.session.submit_trade", new_callable=AsyncMock) as mock_submit:
            mock_cal.return_value.should_trade.return_value           = (True, "")
            mock_cal.return_value.position_size_multiplier.return_value = 1.0
            await s._submit_recommendation(rec, "AAPL", 150.0)

        mock_submit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_devils_advocate_macro_opposing_blocks(self):
        """DevilsAdvocate risk_off + bullish vol_premium → blocked."""
        s   = _make_session()
        rec = _make_recommendation(pillar="vol_premium", direction="bullish", conviction=65.0)
        s._macro_context.macro_stance = "risk_off"
        s._macro_context.confidence   = 0.80

        with patch("agora.session.get_macro_calendar") as mock_cal, \
             patch("agora.session.submit_trade", new_callable=AsyncMock) as mock_submit:
            mock_cal.return_value.should_trade.return_value           = (True, "")
            mock_cal.return_value.position_size_multiplier.return_value = 1.0
            await s._submit_recommendation(rec, "AAPL", 150.0)

        mock_submit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_order_rejected_sets_cooldown(self):
        """
        When IBKR rejects with 'price steps' in reason, execution cooldown
        is set for the ticker — future submissions are blocked.
        """
        s   = _make_session()
        rec = _make_recommendation()
        order = {
            "status":     "Cancelled",
            "order_id":   99,
            "error_code": "timeout",
            "reason":     "Unfilled after all price steps exhausted",
            "fills":      [],
        }

        with patch("agora.session.get_macro_calendar") as mock_cal, \
             patch("agora.session.submit_trade", new_callable=AsyncMock, return_value=order), \
             patch("agora.session._log_chain", return_value="chain-099"):
            mock_cal.return_value.should_trade.return_value           = (True, "")
            mock_cal.return_value.position_size_multiplier.return_value = 1.0
            await s._submit_recommendation(rec, "AAPL", 150.0)

        # Cooldown should now be set
        assert "AAPL" in s._exec_cooldowns
        assert s._exec_cooldowns["AAPL"] > datetime.now(tz=UTC)

    @pytest.mark.asyncio
    async def test_gate_ordering_timing_before_compliance(self):
        """
        Timing gate fires BEFORE compliance is even checked.
        Verifies gate ordering: an after-hours submission should never
        reach the compliance check.
        """
        s   = _make_session()
        rec = _make_recommendation()
        s._entry_timing.is_entry_permitted.return_value = (False, "After hours")

        with patch("agora.session.submit_trade", new_callable=AsyncMock):
            await s._submit_recommendation(rec, "AAPL", 150.0)

        # Compliance should never have been called
        s._compliance.check_trade.assert_not_called()

    @pytest.mark.asyncio
    async def test_gate_ordering_compliance_before_risk(self):
        """
        Compliance gate fires BEFORE risk council.
        A wash sale block should prevent the risk council from being called.
        """
        s   = _make_session()
        rec = _make_recommendation()
        s._compliance.check_trade.return_value = {
            "compliant": False,
            "reason":    "Wash sale",
            "warnings": [],
        }

        with patch("agora.session.get_macro_calendar") as mock_cal, \
             patch("agora.session.submit_trade", new_callable=AsyncMock):
            mock_cal.return_value.should_trade.return_value           = (True, "")
            mock_cal.return_value.position_size_multiplier.return_value = 1.0
            await s._submit_recommendation(rec, "AAPL", 150.0)

        s._risk.approve_trade.assert_not_called()

    @pytest.mark.asyncio
    async def test_decision_chain_logged_on_fill(self):
        """On a successful fill, _log_chain is called with 'filled' outcome."""
        s   = _make_session()
        rec = _make_recommendation()
        order = {"status": "Filled", "order_id": 10, "fills": [{"price": 1.5}]}

        s._record_position = MagicMock()
        with patch("agora.session.get_macro_calendar") as mock_cal, \
             patch("agora.session.submit_trade", new_callable=AsyncMock, return_value=order), \
             patch("agora.session._log_chain", return_value="chain-010") as mock_chain:
            mock_cal.return_value.should_trade.return_value           = (True, "")
            mock_cal.return_value.position_size_multiplier.return_value = 1.0
            await s._submit_recommendation(rec, "AAPL", 150.0, triggered_by="catalyst")

        mock_chain.assert_called_once()
        call_kwargs = mock_chain.call_args
        assert call_kwargs[0][2] == "catalyst"   # triggered_by positional arg
        assert call_kwargs[0][3] == "filled"      # outcome positional arg

    @pytest.mark.asyncio
    async def test_decision_chain_logged_on_reject(self):
        """On an IBKR rejection, _log_chain is called with 'rejected' outcome."""
        s   = _make_session()
        rec = _make_recommendation()
        order = {
            "status":     "Cancelled",
            "order_id":   20,
            "error_code": "201",
            "reason":     "Max combination orders",
            "fills":      [],
        }

        with patch("agora.session.get_macro_calendar") as mock_cal, \
             patch("agora.session.submit_trade", new_callable=AsyncMock, return_value=order), \
             patch("agora.session._log_chain", return_value="chain-020") as mock_chain, \
             patch("agora.session.asyncio.create_task"):  # suppress orphan reconciler task
            mock_cal.return_value.should_trade.return_value           = (True, "")
            mock_cal.return_value.position_size_multiplier.return_value = 1.0
            await s._submit_recommendation(rec, "AAPL", 150.0)

        mock_chain.assert_called_once()
        call_kwargs = mock_chain.call_args
        assert call_kwargs[0][3] == "rejected"

    @pytest.mark.asyncio
    async def test_gates_passed_list_includes_devils_advocate(self):
        """On fill, gates_passed must include 'devils_advocate'."""
        s   = _make_session()
        rec = _make_recommendation()
        order = {"status": "Filled", "order_id": 30, "fills": [{"price": 1.5}]}

        s._record_position = MagicMock()
        with patch("agora.session.get_macro_calendar") as mock_cal, \
             patch("agora.session.submit_trade", new_callable=AsyncMock, return_value=order), \
             patch("agora.session._log_chain", return_value="chain-030") as mock_chain:
            mock_cal.return_value.should_trade.return_value           = (True, "")
            mock_cal.return_value.position_size_multiplier.return_value = 1.0
            await s._submit_recommendation(rec, "AAPL", 150.0)

        call_kwargs = mock_chain.call_args
        gates = call_kwargs[1].get("gates_passed") or call_kwargs[0][-1]
        assert "devils_advocate" in gates

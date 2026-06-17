"""
Unit tests for agora ops agents added in the Week 5-8 build:

  - StrategyHealthAgent  — rolling Sharpe, auto-pause / auto-unpause logic
  - DevilsAdvocate       — 5-check deterministic pre-IBKR checklist
  - ConvictionWeightCalibrator — quarterly SQL analysis + weight proposals

All tests are pure-unit: SQLite uses temp files, no network, no Claude API.
"""

from __future__ import annotations

import json
import math
import sqlite3
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ══════════════════════════════════════════════════════════════════════════════
# StrategyHealthAgent
# ══════════════════════════════════════════════════════════════════════════════

from agora.ops.strategy_health import (
    _compute_sharpe,
    compute_health,
    ensure_table,
    get_paused_cells,
    is_pillar_paused,
    MIN_TRADES,
    SHARPE_PAUSE_THRESH,
    SHARPE_RECOVERY,
    StrategyHealthAgent,
)


class TestComputeSharpe:

    def test_returns_none_for_single_sample(self):
        assert _compute_sharpe([1.0]) is None

    def test_returns_none_for_empty(self):
        assert _compute_sharpe([]) is None

    def test_positive_pnls_positive_sharpe(self):
        pnls = [10.0, 20.0, 15.0, 18.0, 12.0]
        s = _compute_sharpe(pnls)
        assert s is not None and s > 0

    def test_all_losses_negative_sharpe(self):
        pnls = [-10.0, -20.0, -15.0, -5.0, -8.0]
        s = _compute_sharpe(pnls)
        assert s is not None and s < 0

    def test_zero_std_all_same_positive(self):
        # All identical positive → std=0, mean>0 → return 1.0
        assert _compute_sharpe([5.0, 5.0, 5.0]) == 1.0

    def test_zero_std_all_same_negative(self):
        assert _compute_sharpe([-5.0, -5.0, -5.0]) == -1.0

    def test_zero_std_zero_mean(self):
        assert _compute_sharpe([0.0, 0.0, 0.0]) == 0.0

    def test_two_samples(self):
        s = _compute_sharpe([10.0, -10.0])
        assert s is not None and s == 0.0  # mean=0

    def test_sharpe_below_pause_threshold(self):
        # Consistently losing → Sharpe clearly below -0.5
        pnls = [-100.0, -50.0, -80.0, -60.0, -90.0] * 5
        s = _compute_sharpe(pnls)
        assert s is not None and s < SHARPE_PAUSE_THRESH


def _make_trade_db(pnl_rows: list[tuple]) -> str:
    """
    Create a temp SQLite DB with a `positions` table — the REAL-fills source compute_health now
    reads (was trade_records model marks, which logged fiction Sharpe). Each row is inserted as a
    genuine agent-driven close (status='closed', close_source='thesis_exit') so it satisfies the
    _REAL_CLOSE predicate the query gates on.
    pnl_rows: list of (pillar, regime, realized_pnl, days_ago)
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = tmp.name
    tmp.close()
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE positions (
                position_id  INTEGER PRIMARY KEY,
                pillar       TEXT,
                regime_at_entry TEXT,
                realized_pnl REAL,
                close_date   TEXT,
                status       TEXT DEFAULT 'closed',
                close_source TEXT DEFAULT 'thesis_exit'
            )
        """)
        for pillar, regime, pnl, days_ago in pnl_rows:
            close_date = (date.today() - timedelta(days=days_ago)).isoformat()
            conn.execute(
                """INSERT INTO positions (pillar, regime_at_entry, realized_pnl, close_date,
                   status, close_source) VALUES (?,?,?,?, 'closed', 'thesis_exit')""",
                (pillar, regime, pnl, close_date),
            )
        conn.commit()
    return db_path


class TestComputeHealth:

    def test_empty_db_returns_empty(self):
        db = _make_trade_db([])
        ensure_table(db)
        result = compute_health(db)
        assert result == {}

    def test_groups_by_pillar_regime(self):
        rows = [
            ("vol_premium", "risk_on", 50.0, 5),
            ("vol_premium", "risk_on", 30.0, 10),
            ("catalyst",    "neutral", -20.0, 3),
            ("catalyst",    "neutral", -10.0, 7),
        ]
        db = _make_trade_db(rows)
        ensure_table(db)
        result = compute_health(db)
        assert "vol_premium:risk_on" in result
        assert "catalyst:neutral" in result
        assert result["vol_premium:risk_on"]["count"] == 2
        assert result["catalyst:neutral"]["count"] == 2

    def test_excludes_old_trades_outside_window(self):
        rows = [
            ("vol_premium", "neutral", 100.0, 5),    # inside 30d window
            ("vol_premium", "neutral", -100.0, 35),  # outside 30d window
        ]
        db = _make_trade_db(rows)
        ensure_table(db)
        result = compute_health(db)
        # Only the 5-day-old trade counts
        assert result["vol_premium:neutral"]["count"] == 1
        assert result["vol_premium:neutral"]["mean_pnl"] == 100.0

    def test_excludes_open_positions(self):
        # Trades with close_date=NULL should be excluded
        db = _make_trade_db([])
        ensure_table(db)
        with sqlite3.connect(db) as conn:
            conn.execute(
                """INSERT INTO positions (pillar, regime_at_entry, realized_pnl, close_date,
                   status, close_source) VALUES (?,?,?,NULL,'open',NULL)""",
                ("vol_premium", "neutral", 500.0),
            )
        result = compute_health(db)
        assert result == {}

    def test_sharpe_computed_correctly(self):
        pnls = [10.0, 20.0, 15.0, 25.0, 5.0]
        rows = [("vol_premium", "neutral", p, i) for i, p in enumerate(pnls, start=1)]
        db = _make_trade_db(rows)
        ensure_table(db)
        result = compute_health(db)
        cell = result["vol_premium:neutral"]
        expected_sharpe = _compute_sharpe(pnls)
        assert cell["sharpe"] == round(expected_sharpe, 3)


class TestIsPillarPaused:

    def _paused_db(self, pillar: str, regime: str, reason: str = "test") -> str:
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = tmp.name
        tmp.close()
        with sqlite3.connect(db_path) as conn:
            conn.execute("""
                CREATE TABLE pillar_pauses (
                    pillar TEXT NOT NULL,
                    regime TEXT NOT NULL DEFAULT 'all',
                    paused_at TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    sharpe_at_pause REAL,
                    trade_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (pillar, regime)
                )
            """)
            conn.execute(
                "INSERT INTO pillar_pauses (pillar, regime, paused_at, reason) VALUES (?,?,?,?)",
                (pillar, regime, "2026-01-01", reason),
            )
        return db_path

    def test_not_paused_when_no_row(self):
        db = _make_trade_db([])
        ensure_table(db)
        paused, reason = is_pillar_paused(db, "vol_premium", "neutral")
        assert not paused
        assert reason == ""

    def test_paused_on_exact_match(self):
        db = self._paused_db("vol_premium", "risk_on", "bad Sharpe")
        paused, reason = is_pillar_paused(db, "vol_premium", "risk_on")
        assert paused
        assert "bad Sharpe" in reason

    def test_paused_via_all_regime_row(self):
        # "all" regime blocks any regime query
        db = self._paused_db("catalyst", "all", "all regimes blocked")
        paused, reason = is_pillar_paused(db, "catalyst", "neutral")
        assert paused

    def test_not_paused_different_pillar(self):
        db = self._paused_db("catalyst", "neutral")
        paused, _ = is_pillar_paused(db, "vol_premium", "neutral")
        assert not paused

    def test_not_paused_different_regime(self):
        db = self._paused_db("vol_premium", "risk_on")
        paused, _ = is_pillar_paused(db, "vol_premium", "risk_off")
        assert not paused


class TestStrategyHealthPatrol:
    """Tests for StrategyHealthAgent._patrol() using a real temp SQLite DB."""

    def _make_agent(self, db_path: str) -> StrategyHealthAgent:
        settings = MagicMock()
        settings.db_path = Path(db_path)
        agent = StrategyHealthAgent.__new__(StrategyHealthAgent)
        agent._settings = settings
        agent._ceo = None
        agent._running = False
        ensure_table(db_path)
        # positions (real fills) must exist for compute_health() to query — it was migrated off
        # trade_records (model marks) to honest, fill-sourced P&L.
        with sqlite3.connect(db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS positions (
                    position_id  INTEGER PRIMARY KEY,
                    pillar       TEXT,
                    regime_at_entry TEXT,
                    realized_pnl REAL,
                    close_date   TEXT,
                    status       TEXT DEFAULT 'closed',
                    close_source TEXT DEFAULT 'thesis_exit'
                )
            """)
        return agent

    def _insert_trades(
        self, db_path: str, pillar: str, regime: str, pnls: list[float]
    ) -> None:
        with sqlite3.connect(db_path) as conn:
            for i, pnl in enumerate(pnls):
                close_date = (date.today() - timedelta(days=i)).isoformat()
                conn.execute(
                    """INSERT INTO positions
                       (pillar, regime_at_entry, realized_pnl, close_date, status, close_source)
                       VALUES (?,?,?,?, 'closed', 'thesis_exit')""",
                    (pillar, regime, pnl, close_date),
                )

    @pytest.mark.asyncio
    async def test_does_not_pause_below_min_trades(self):
        """Fewer than MIN_TRADES — no pause even with terrible Sharpe."""
        db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        agent = self._make_agent(db)
        losing = [-100.0] * (MIN_TRADES - 1)
        self._insert_trades(db, "vol_premium", "neutral", losing)
        await agent._patrol()
        paused, _ = is_pillar_paused(db, "vol_premium", "neutral")
        assert not paused

    @pytest.mark.asyncio
    async def test_pauses_on_bad_sharpe_with_enough_trades(self):
        """MIN_TRADES or more with Sharpe < SHARPE_PAUSE_THRESH → auto-pause."""
        db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        agent = self._make_agent(db)
        # Consistent losses → deeply negative Sharpe
        losing = [-50.0] * MIN_TRADES
        self._insert_trades(db, "catalyst", "risk_off", losing)
        await agent._patrol()
        paused, reason = is_pillar_paused(db, "catalyst", "risk_off")
        assert paused
        assert "Sharpe=" in reason

    @pytest.mark.asyncio
    async def test_does_not_pause_when_sharpe_ok(self):
        """Healthy Sharpe → no pause."""
        db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        agent = self._make_agent(db)
        winning = [50.0] * MIN_TRADES
        self._insert_trades(db, "vol_premium", "neutral", winning)
        await agent._patrol()
        paused, _ = is_pillar_paused(db, "vol_premium", "neutral")
        assert not paused

    @pytest.mark.asyncio
    async def test_unpauses_when_sharpe_recovers(self):
        """Cell is currently paused but Sharpe has now recovered → unpause."""
        db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        agent = self._make_agent(db)
        # Manually insert a pause row
        with sqlite3.connect(db) as conn:
            conn.execute(
                """INSERT INTO pillar_pauses
                   (pillar, regime, paused_at, reason, sharpe_at_pause, trade_count)
                   VALUES (?,?,?,?,?,?)""",
                ("vol_premium", "neutral", "2026-01-01", "old bad run", -0.8, 25),
            )
        # Insert recent winning trades (Sharpe > SHARPE_RECOVERY)
        winning = [40.0] * MIN_TRADES
        self._insert_trades(db, "vol_premium", "neutral", winning)
        await agent._patrol()
        paused, _ = is_pillar_paused(db, "vol_premium", "neutral")
        assert not paused

    @pytest.mark.asyncio
    async def test_escalates_to_ceo_on_pause(self):
        """CEO should receive a critical alert when a cell gets paused."""
        db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        ceo = AsyncMock()
        agent = self._make_agent(db)
        agent._ceo = ceo
        losing = [-60.0] * MIN_TRADES
        self._insert_trades(db, "catalyst", "neutral", losing)
        await agent._patrol()
        ceo.dispatch_alert.assert_awaited_once()
        call_args = ceo.dispatch_alert.call_args
        assert call_args[0][0] == "critical"


# ══════════════════════════════════════════════════════════════════════════════
# DevilsAdvocate
# ══════════════════════════════════════════════════════════════════════════════

from agora.ops.devils_advocate import (
    _check_conviction_floor,
    _check_duplicate_ticker,
    _check_earnings_spans_expiry,
    _check_macro_opposing,
    _check_vol_selling_ok,
    _CONVICTION_FLOOR,
    run as da_run,
)


def _make_leg(expiration: date, strike: float = 100.0) -> Any:
    leg = MagicMock()
    leg.expiration = expiration
    leg.strike = strike
    return leg


def _make_rec(
    pillar: str = "vol_premium",
    direction: str = "neutral",
    conviction: float = 60.0,
    expiry_days: int = 30,
    strategy: str | None = None,
) -> Any:
    rec = MagicMock()
    rec.pillar = MagicMock()
    rec.pillar.value = pillar
    # The earnings-span and vol-selling checks key on the STRATEGY TYPE (credit spreads), not the
    # pillar. Map the legacy pillar intent to a representative strategy so those checks fire:
    # vol_premium → a credit spread, anything else → a non-credit long.
    if strategy is None:
        strategy = "bull_put_spread" if pillar == "vol_premium" else "long_call"
    rec.strategy = MagicMock()
    rec.strategy.value = strategy
    rec.ticker = "AAPL"
    rec.direction = direction
    rec.conviction_score = conviction
    rec.legs = [_make_leg(date.today() + timedelta(days=expiry_days))]
    return rec


def _make_macro(stance: str = "neutral", confidence: float = 0.7, vol_ok: bool = True) -> Any:
    ctx = MagicMock()
    ctx.macro_stance = stance
    ctx.confidence = confidence
    ctx.vol_selling_ok = vol_ok
    return ctx


def _make_position(ticker: str, position_id: str = "pos1") -> Any:
    p = MagicMock()
    p.ticker = ticker
    p.position_id = position_id
    return p


class TestDevilsAdvocateEarningsCheck:

    def test_passes_when_no_earnings_date(self):
        rec = _make_rec("vol_premium", expiry_days=30)
        ok, reason = _check_earnings_spans_expiry(rec, None, False)
        assert ok

    def test_passes_when_is_pre_earnings(self):
        rec = _make_rec("vol_premium", expiry_days=30)
        earnings = date.today() + timedelta(days=10)
        ok, reason = _check_earnings_spans_expiry(rec, earnings, True)
        assert ok  # pre-earnings plays are exempt

    def test_passes_for_non_credit_pillar(self):
        rec = _make_rec("catalyst", expiry_days=30)
        earnings = date.today() + timedelta(days=10)
        ok, reason = _check_earnings_spans_expiry(rec, earnings, False)
        assert ok  # only vol_premium is blocked

    def test_blocks_credit_spanning_earnings(self):
        rec = _make_rec("vol_premium", expiry_days=30)
        earnings = date.today() + timedelta(days=15)  # earnings before expiry
        ok, reason = _check_earnings_spans_expiry(rec, earnings, False)
        assert not ok
        assert "gamma risk" in reason

    def test_passes_when_earnings_after_expiry(self):
        rec = _make_rec("vol_premium", expiry_days=15)
        earnings = date.today() + timedelta(days=20)  # earnings after expiry
        ok, reason = _check_earnings_spans_expiry(rec, earnings, False)
        assert ok


class TestDevilsAdvocateDuplicateTicker:

    def test_passes_when_no_existing_positions(self):
        rec = _make_rec()
        ok, _ = _check_duplicate_ticker(rec, [])
        assert ok

    def test_passes_when_different_ticker(self):
        rec = _make_rec()
        positions = [_make_position("TSLA")]
        ok, _ = _check_duplicate_ticker(rec, positions)
        assert ok

    def test_blocks_same_ticker(self):
        rec = _make_rec()
        positions = [_make_position("AAPL")]
        ok, reason = _check_duplicate_ticker(rec, positions)
        assert not ok
        assert "AAPL" in reason

    def test_blocks_first_matching_ticker(self):
        rec = _make_rec()
        positions = [_make_position("TSLA"), _make_position("AAPL", "pos2")]
        ok, reason = _check_duplicate_ticker(rec, positions)
        assert not ok


class TestDevilsAdvocateMacroOpposing:

    def test_passes_when_no_macro_context(self):
        rec = _make_rec(direction="bullish")
        ok, _ = _check_macro_opposing(rec, None)
        assert ok

    def test_passes_risk_off_and_neutral_direction(self):
        rec = _make_rec(direction="neutral")
        ctx = _make_macro(stance="risk_off")
        ok, _ = _check_macro_opposing(rec, ctx)
        assert ok

    def test_passes_risk_on_and_bullish(self):
        rec = _make_rec(direction="bullish")
        ctx = _make_macro(stance="risk_on")
        ok, _ = _check_macro_opposing(rec, ctx)
        assert ok

    def test_blocks_risk_off_plus_bullish(self):
        rec = _make_rec(pillar="vol_premium", direction="bullish")
        ctx = _make_macro(stance="risk_off", confidence=0.80)
        ok, reason = _check_macro_opposing(rec, ctx)
        assert not ok
        assert "risk_off" in reason

    def test_passes_event_pillar_ignores_macro(self):
        """Event pillars (FOMC, CPI) are exempt from macro-direction gate."""
        rec = _make_rec(pillar="event_fomc", direction="bullish")
        ctx = _make_macro(stance="risk_off")
        ok, _ = _check_macro_opposing(rec, ctx)
        assert ok


class TestDevilsAdvocateVolSelling:

    @pytest.fixture(autouse=True)
    def _no_force_vol_selling(self):
        # Free-paper mode sets force_vol_selling_ok=True, which makes the gate fall open. Pin it
        # off so the block path is exercised. The check reads get_settings() lazily inside the fn.
        fake = MagicMock()
        fake.force_vol_selling_ok = False
        with patch("agora.core.config.get_settings", return_value=fake):
            yield

    def test_passes_for_non_credit_pillar(self):
        rec = _make_rec(pillar="catalyst")
        ctx = _make_macro(vol_ok=False)
        ok, _ = _check_vol_selling_ok(rec, ctx)
        assert ok  # only vol_premium is gated

    def test_passes_when_vol_ok(self):
        rec = _make_rec(pillar="vol_premium")
        ctx = _make_macro(vol_ok=True)
        ok, _ = _check_vol_selling_ok(rec, ctx)
        assert ok

    def test_blocks_when_vol_not_ok(self):
        rec = _make_rec(pillar="vol_premium")
        ctx = _make_macro(vol_ok=False)
        ok, reason = _check_vol_selling_ok(rec, ctx)
        assert not ok
        assert "vol_selling_ok=False" in reason

    def test_passes_when_no_macro_context(self):
        rec = _make_rec(pillar="vol_premium")
        ok, _ = _check_vol_selling_ok(rec, None)
        assert ok


class TestDevilsAdvocateConvictionFloor:

    @pytest.fixture(autouse=True)
    def _fixed_floor(self):
        # The runtime floor is now dynamic (settings.disagreement_resolver_floor, lowered in
        # paper mode). Pin it to the documented reference value so these boundary tests exercise
        # the gate LOGIC (below→block, at/above→pass) independent of the live config.
        with patch("agora.ops.devils_advocate._get_conviction_floor", return_value=_CONVICTION_FLOOR):
            yield

    def test_passes_above_floor(self):
        rec = _make_rec(conviction=_CONVICTION_FLOOR + 1)
        ok, _ = _check_conviction_floor(rec)
        assert ok

    def test_blocks_below_floor(self):
        rec = _make_rec(conviction=_CONVICTION_FLOOR - 1)
        ok, reason = _check_conviction_floor(rec)
        assert not ok
        assert "floor" in reason

    def test_blocks_exactly_at_floor_boundary(self):
        # Strictly less than floor → block
        rec = _make_rec(conviction=_CONVICTION_FLOOR - 0.01)
        ok, _ = _check_conviction_floor(rec)
        assert not ok

    def test_passes_at_zero_conviction_when_field_missing(self):
        # If no conviction_score field, defaults to 100.0 (safe fallback)
        rec = MagicMock(spec=[])  # no conviction_score attribute
        ok, _ = _check_conviction_floor(rec)
        assert ok


class TestDevilsAdvocateRun:

    def _good_rec(self) -> Any:
        return _make_rec(pillar="vol_premium", direction="neutral", conviction=70.0, expiry_days=30)

    def test_all_pass_returns_true(self):
        rec = self._good_rec()
        ok, reason, results = da_run(rec, [], _make_macro(), None, False)
        assert ok
        assert reason == ""
        assert all(r[1] for r in results)

    def test_stops_at_first_failure(self):
        rec = _make_rec(pillar="vol_premium", direction="neutral", conviction=70.0, expiry_days=30)
        # Trigger duplicate ticker (check #2)
        positions = [_make_position("AAPL")]
        ok, reason, results = da_run(rec, positions, _make_macro(), None, False)
        assert not ok
        assert "AAPL" in reason
        # Should have stopped after check #2 — only 2 results appended
        assert len(results) == 2

    def test_conviction_floor_blocks(self):
        rec = _make_rec(conviction=10.0)
        ok, reason, results = da_run(rec, [], _make_macro(), None, False)
        assert not ok
        assert "floor" in reason

    def test_macro_opposing_blocks_before_conviction_floor(self):
        # Both macro and conviction are bad — macro check (#3) fires first
        rec = _make_rec(pillar="vol_premium", direction="bullish", conviction=5.0)
        ctx = _make_macro(stance="risk_off")
        ok, reason, results = da_run(rec, [], ctx, None, False)
        assert not ok
        assert "risk_off" in reason
        assert len(results) == 3  # stopped at check #3


# ══════════════════════════════════════════════════════════════════════════════
# ConvictionWeightCalibrator
# ══════════════════════════════════════════════════════════════════════════════

from agora.ops.conviction_calibrator import (
    _profit_factor,
    _cell_stats,
    _conviction_quintile_analysis,
    _per_pillar_analysis,
    _propose_weights,
    _CURRENT_WEIGHTS,
    calibrate,
    MIN_TRADES_FOR_PROPOSAL,
)


class TestProfitFactor:

    def test_all_wins(self):
        assert _profit_factor([10.0, 20.0, 30.0]) == float("inf")

    def test_all_losses(self):
        pf = _profit_factor([-10.0, -20.0])
        assert pf == 0.0  # gross_profit=0, gross_loss>0 → 0/loss = 0

    def test_mixed(self):
        pf = _profit_factor([30.0, -10.0])  # 30 profit / 10 loss = 3.0
        assert pf == 3.0

    def test_no_losses_no_wins(self):
        # All zeros → returns 1.0
        assert _profit_factor([0.0, 0.0]) == 1.0


class TestCellStats:

    def test_empty_returns_none_fields(self):
        s = _cell_stats([])
        assert s["count"] == 0
        assert s["win_rate"] is None

    def test_all_winning(self):
        s = _cell_stats([10.0, 20.0, 30.0])
        assert s["count"] == 3
        assert s["win_rate"] == 1.0
        assert s["avg_pnl"] == 20.0

    def test_half_wins(self):
        s = _cell_stats([10.0, -10.0])
        assert s["win_rate"] == 0.5


class TestConvictionQuintileAnalysis:

    def test_empty_chains(self):
        assert _conviction_quintile_analysis([]) == {}

    def test_single_trade(self):
        chains = [{"conviction": 50.0, "strategy": "bull_put", "pnl": 100.0}]
        result = _conviction_quintile_analysis(chains)
        assert len(result) >= 1

    def test_quintile_count(self):
        chains = [
            {"conviction": float(i * 10), "strategy": "s", "pnl": float(i - 5) * 10}
            for i in range(50)
        ]
        result = _conviction_quintile_analysis(chains)
        assert len(result) == 5  # 5 quintiles

    def test_q5_label_has_higher_range_than_q1(self):
        chains = [
            {"conviction": float(i), "strategy": "s", "pnl": 10.0}
            for i in range(50)
        ]
        result = _conviction_quintile_analysis(chains)
        labels = sorted(result.keys())
        # Q1 label should come before Q5 label lexicographically
        assert labels[0].startswith("Q1")
        assert labels[-1].startswith("Q5")


def _make_calibrator_db(trade_rows: list[tuple], chain_rows: list[tuple]) -> str:
    """
    trade_rows: (pillar, regime, conviction, pnl, days_ago)
    chain_rows: (conviction, strategy, pnl, days_ago)

    Both calibration sources read positions/_REAL_CLOSE now (was trade_records model marks and
    decision_chains.realized_pnl, which flip sign vs the fills). So we build a `positions` table of
    genuinely-closed trades, and link each decision_chains row to one of those SAME positions —
    chains are the conviction-quintile VIEW of the closed trades, not a separate population (so
    they must not inflate total_closed_trades, which counts positions).
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = tmp.name
    tmp.close()
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE positions (
                position_id  TEXT PRIMARY KEY,
                pillar       TEXT,
                regime_at_entry TEXT,
                conviction_at_entry REAL,
                realized_pnl REAL,
                close_date   TEXT,
                status       TEXT DEFAULT 'closed',
                close_source TEXT DEFAULT 'thesis_exit'
            )
        """)
        conn.execute("""
            CREATE TABLE decision_chains (
                chain_id    TEXT,
                position_id TEXT,
                conviction  REAL,
                strategy    TEXT,
                realized_pnl REAL,
                started_at  TEXT,
                outcome     TEXT
            )
        """)
        pids: list[str] = []
        for i, (pillar, regime, conviction, pnl, days_ago) in enumerate(trade_rows):
            pid = f"pos-{i}"
            conn.execute(
                """INSERT INTO positions (position_id, pillar, regime_at_entry, conviction_at_entry,
                   realized_pnl, close_date, status, close_source)
                   VALUES (?,?,?,?,?,?, 'closed','thesis_exit')""",
                (pid, pillar, regime, conviction, pnl,
                 (date.today() - timedelta(days=days_ago)).isoformat()),
            )
            pids.append(pid)
        for j, (conviction, strategy, pnl, days_ago) in enumerate(chain_rows):
            started = (date.today() - timedelta(days=days_ago)).isoformat()
            if pids:
                pid = pids[j % len(pids)]   # reuse a real closed position (no count inflation)
            else:
                # No trades — a chain still needs a position to join to.
                pid = f"cpos-{j}"
                conn.execute(
                    """INSERT INTO positions (position_id, pillar, regime_at_entry,
                       conviction_at_entry, realized_pnl, close_date, status, close_source)
                       VALUES (?,?,?,?,?,?, 'closed','thesis_exit')""",
                    (pid, "vol_premium", "neutral", conviction, pnl, started),
                )
            conn.execute(
                """INSERT INTO decision_chains (chain_id, position_id, conviction, strategy,
                   realized_pnl, started_at, outcome) VALUES (?,?,?,?,?,?, 'filled')""",
                (f"chain-{j}", pid, conviction, strategy, pnl, started),
            )
    return db_path


class TestProposeWeights:

    def test_no_proposals_below_min_trades(self):
        regime_stats = {}
        pillar_stats = {}
        proposed, notes = _propose_weights(regime_stats, pillar_stats, MIN_TRADES_FOR_PROPOSAL - 1)
        assert any("Insufficient" in n for n in notes)
        assert proposed == _CURRENT_WEIGHTS

    def test_proposals_above_min_trades_returns_dict(self):
        regime_stats = {"neutral": {"win_rate": 0.55, "count": 25, "profit_factor": 1.3}}
        pillar_stats = {"vol_premium": {"win_rate": 0.60, "count": 20, "profit_factor": 1.5}}
        proposed, notes = _propose_weights(regime_stats, pillar_stats, MIN_TRADES_FOR_PROPOSAL)
        # Should return a dict with same keys as current weights
        assert set(proposed.keys()) == set(_CURRENT_WEIGHTS.keys())

    def test_weights_sum_to_one(self):
        regime_stats = {}
        pillar_stats = {"catalyst": {"win_rate": 0.70, "count": 15, "profit_factor": 2.0}}
        proposed, _ = _propose_weights(regime_stats, pillar_stats, MIN_TRADES_FOR_PROPOSAL + 10)
        for regime, weights in proposed.items():
            total = sum(weights.values())
            assert abs(total - 1.0) < 0.01, f"{regime} weights sum={total}"

    def test_catalyst_boost_proposed_when_win_rate_high(self):
        """High catalyst win_rate + enough trades → catalyst weight bumped in calm regimes."""
        regime_stats = {}
        pillar_stats = {
            "catalyst": {"win_rate": 0.65, "count": 12, "profit_factor": 1.8, "avg_pnl": 50.0, "sharpe": 0.8}
        }
        proposed, notes = _propose_weights(regime_stats, pillar_stats, MIN_TRADES_FOR_PROPOSAL + 5)
        # At least one note about catalyst
        catalyst_notes = [n for n in notes if "Catalyst" in n or "catalyst" in n]
        assert len(catalyst_notes) > 0


class TestCalibrateE2E:

    def test_empty_db_produces_valid_output(self):
        db = _make_calibrator_db([], [])
        out_path = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
        result = calibrate(db, out_path)
        assert result["total_closed_trades"] == 0
        assert "per_pillar" in result
        assert "proposed_weights" in result
        assert "action_required" in result
        # File should be written
        with open(out_path) as f:
            written = json.load(f)
        assert written["total_closed_trades"] == 0

    def test_insufficient_data_note_present(self):
        db = _make_calibrator_db([], [])
        out_path = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
        result = calibrate(db, out_path)
        notes = result["proposal_notes"]
        assert any("Insufficient" in n for n in notes)

    def test_with_sufficient_trades(self):
        trade_rows = [
            ("vol_premium", "neutral", 60.0, 50.0, i) for i in range(1, 26)
        ]
        chain_rows = [
            (60.0, "bull_put_spread", 50.0, i) for i in range(1, 26)
        ]
        db = _make_calibrator_db(trade_rows, chain_rows)
        out_path = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
        result = calibrate(db, out_path)
        assert result["total_closed_trades"] == 25
        assert "vol_premium" in result["per_pillar"]
        assert result["per_pillar"]["vol_premium"]["win_rate"] == 1.0

    def test_per_pillar_regime_cross_cell(self):
        trade_rows = [
            ("catalyst", "risk_on", 55.0, 20.0, i) for i in range(1, 6)
        ] + [
            ("vol_premium", "neutral", 45.0, -10.0, i + 5) for i in range(1, 4)
        ]
        db = _make_calibrator_db(trade_rows, [])
        out_path = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
        result = calibrate(db, out_path)
        assert "catalyst:risk_on" in result["per_pillar_regime_cell"]
        assert "vol_premium:neutral" in result["per_pillar_regime_cell"]

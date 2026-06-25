"""Unit tests for agora/ops/execution_quality.py and agora/ops/outcome_attributor.py

Covers:
  • ExecutionQualityAgent — per-symbol skip cap, fill exemption, fill-rate math,
    and the Error-201 policy-reject exclusion from the effective denominator.
  • outcome_attributor._REAL_CLOSE — the SQL filter that attributes ONLY genuine
    agent-driven closes and EXCLUDES quarantined / broker-sync artifact rows.

All tests use a temp sqlite DB (tmp_path) — never the real .agora/agora.db.
No LLM, no network: only the pure-Python record/stats methods and the literal
_REAL_CLOSE SQL string are exercised.
"""
from __future__ import annotations

import sqlite3

import pytest

from agora.core.config import get_settings
from agora.ops.execution_quality import ExecutionQualityAgent
from agora.ops.outcome_attributor import _REAL_CLOSE

# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture
def temp_settings(tmp_path):
    """Real settings object with db_path redirected to a temp file."""
    db = tmp_path / "exec_quality.db"
    return get_settings().model_copy(update={"db_path": db})


@pytest.fixture
def agent(temp_settings) -> ExecutionQualityAgent:
    return ExecutionQualityAgent(settings=temp_settings)


# ── ExecutionQualityAgent: per-symbol skip cap ─────────────────────────────────

class TestShouldSkipSymbol:
    def test_not_skipped_below_cap(self, agent):
        cap = agent._settings.exec_max_attempts_per_symbol
        for _ in range(cap - 1):
            agent.record_attempt("SMH", "debit_spread", 1.20)
        skip, reason = agent.should_skip_symbol("SMH")
        assert skip is False
        assert reason == ""

    def test_skipped_at_cap_with_zero_fills(self, agent):
        cap = agent._settings.exec_max_attempts_per_symbol
        for _ in range(cap):
            agent.record_attempt("SMH", "debit_spread", 1.20)
        skip, reason = agent.should_skip_symbol("SMH")
        assert skip is True
        assert str(cap) in reason

    def test_unknown_symbol_never_skipped(self, agent):
        skip, reason = agent.should_skip_symbol("NEVER_SEEN")
        assert skip is False
        assert reason == ""

    def test_a_single_fill_exempts_symbol_forever(self, agent):
        cap = agent._settings.exec_max_attempts_per_symbol
        # Far exceed the cap in attempts...
        for _ in range(cap * 3):
            agent.record_attempt("AAPL", "debit_spread", 2.00)
        # ...but one fill flips it to never-skip.
        agent.record_fill("AAPL", fill_price=2.01, mid_price=2.00, strategy="debit_spread")
        skip, reason = agent.should_skip_symbol("AAPL")
        assert skip is False
        assert reason == ""

    def test_skip_is_per_symbol(self, agent):
        cap = agent._settings.exec_max_attempts_per_symbol
        for _ in range(cap):
            agent.record_attempt("VECO", "debit_spread", 0.90)
        # VECO capped, but a different unfilled name below cap is untouched.
        agent.record_attempt("QQQ", "credit_spread", 1.10)
        assert agent.should_skip_symbol("VECO")[0] is True
        assert agent.should_skip_symbol("QQQ")[0] is False


# ── ExecutionQualityAgent: session fill-rate math ──────────────────────────────

class TestSessionStats:
    def test_fill_rate_basic(self, agent):
        for _ in range(4):
            agent.record_attempt("SPY", "debit_spread", 1.00)
        agent.record_fill("SPY", fill_price=1.00, mid_price=1.00, strategy="debit_spread")
        stats = agent.get_session_stats()
        assert stats["attempts"] == 4
        assert stats["fills"] == 1
        assert stats["fill_rate"] == pytest.approx(0.25)

    def test_empty_session_is_zero_not_division_error(self, agent):
        stats = agent.get_session_stats()
        assert stats["attempts"] == 0
        assert stats["fill_rate"] == 0.0
        assert stats["effective_fill_rate"] == 0.0

    def test_error_201_excluded_from_effective_denominator(self, agent):
        # 4 attempts: 1 fills, 2 are Error-201 policy rejects, 1 is a real (non-201) reject.
        for _ in range(4):
            agent.record_attempt("IWM", "iron_condor", 1.50)
        agent.record_fill("IWM", fill_price=1.50, mid_price=1.50, strategy="iron_condor")
        agent.record_reject("IWM", "201", "riskless combo limit", "iron_condor")
        agent.record_reject("IWM", "201", "riskless combo limit", "iron_condor")
        agent.record_reject("IWM", "202", "order cancelled", "iron_condor")

        stats = agent.get_session_stats()
        assert stats["attempts"] == 4
        assert stats["policy_rejects"] == 2
        # effective denominator strips the 2 policy rejects: 4 - 2 = 2
        assert stats["effective_attempts"] == 2
        # raw fill rate dilutes over all 4; effective lifts the 201s out.
        assert stats["fill_rate"] == pytest.approx(1 / 4)
        assert stats["effective_fill_rate"] == pytest.approx(1 / 2)

    def test_only_201_counts_as_policy_reject(self, agent):
        agent.record_attempt("DIA", "debit_spread", 1.00)
        agent.record_reject("DIA", "202", "cancelled", "debit_spread")
        stats = agent.get_session_stats()
        assert stats["policy_rejects"] == 0
        assert stats["effective_attempts"] == 1

    def test_201_storm_flag_trips_at_five(self, agent):
        for _ in range(5):
            agent.record_attempt("XLF", "iron_condor", 0.80)
            agent.record_reject("XLF", "201", "riskless combo limit", "iron_condor")
        stats = agent.get_session_stats()
        assert stats["reject_reasons"].get("201") == 5
        assert stats["error_201_storm"] is True


# ── ExecutionQualityAgent: DB persistence ──────────────────────────────────────

class TestDbWrites:
    def test_attempt_then_fill_updates_same_row(self, agent, temp_settings):
        agent.record_attempt("MSFT", "debit_spread", 2.00)
        agent.record_fill("MSFT", fill_price=2.05, mid_price=2.00, strategy="debit_spread")
        # Verify directly against the on-disk DB (independent connection).
        conn = sqlite3.connect(str(temp_settings.db_path))
        rows = conn.execute(
            "SELECT outcome, fill_price FROM execution_quality WHERE ticker='MSFT'"
        ).fetchall()
        conn.close()
        # Exactly one row, flipped pending -> fill (not a duplicate insert).
        assert rows == [("fill", 2.05)]

    def test_fill_without_attempt_inserts_row(self, agent, temp_settings):
        # Long-options path: a fill can arrive with no preceding attempt row.
        agent.record_fill("NVDA", fill_price=3.10, mid_price=3.00, strategy="long_call")
        conn = sqlite3.connect(str(temp_settings.db_path))
        row = conn.execute(
            "SELECT outcome, fill_price FROM execution_quality WHERE ticker='NVDA'"
        ).fetchone()
        conn.close()
        assert row == ("fill", 3.10)


# ── outcome_attributor._REAL_CLOSE SQL filter ──────────────────────────────────

class TestRealCloseFilter:
    """Drive the literal _REAL_CLOSE string against a temp positions table and
    confirm exactly the genuine agent-driven closes pass — the quarantined and
    broker-sync artifact sources are excluded."""

    @staticmethod
    def _build_positions_db(db_path: str) -> None:
        conn = sqlite3.connect(db_path)
        conn.execute(
            """CREATE TABLE positions (
                   position_id  TEXT PRIMARY KEY,
                   status       TEXT,
                   close_date   TEXT,
                   close_source TEXT,
                   realized_pnl REAL NOT NULL DEFAULT 0,
                   regime_at_entry TEXT DEFAULT 'neutral'
               )"""
        )
        rows = [
            # (position_id, status, close_date, close_source) — expected pass?
            ("p_lifecycle",   "closed", "2026-06-10", "lifecycle"),        # PASS
            ("p_thesis",      "closed", "2026-06-10", "thesis_exit"),      # PASS
            ("p_trail",       "closed", "2026-06-10", "trailing_stop"),    # PASS
            ("p_stop",        "closed", "2026-06-10", "stop_loss"),        # PASS
            ("p_session",     "closed", "2026-06-10", "session:cto_close"),# PASS (LIKE 'session:%')
            ("p_fabricated",  "closed", "2026-06-10", "fabricated_unfilled"),  # EXCLUDE (quarantine)
            ("p_dupe",        "closed", "2026-06-10", "duplicate_void"),       # EXCLUDE (quarantine)
            ("p_tws_sync",    "closed", "2026-06-10", "tws_startup_sync"),     # EXCLUDE (broker artifact)
            ("p_open",        "open",   None,         "lifecycle"),            # EXCLUDE (not closed)
            ("p_no_date",     "closed", "",           "lifecycle"),            # EXCLUDE (empty close_date)
        ]
        conn.executemany(
            "INSERT INTO positions (position_id, status, close_date, close_source, realized_pnl) "
            "VALUES (?,?,?,?,0)",
            rows,
        )
        conn.commit()
        conn.close()

    @pytest.fixture
    def positions_db(self, tmp_path):
        db = tmp_path / "attrib.db"
        self._build_positions_db(str(db))
        return str(db)

    def _selected(self, db_path: str) -> set[str]:
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                f"SELECT p.position_id FROM positions p WHERE {_REAL_CLOSE}"
            ).fetchall()
        finally:
            conn.close()
        return {r[0] for r in rows}

    def test_genuine_closes_selected(self, positions_db):
        selected = self._selected(positions_db)
        assert selected == {
            "p_lifecycle", "p_thesis", "p_trail", "p_stop", "p_session",
        }

    def test_quarantined_sources_excluded(self, positions_db):
        selected = self._selected(positions_db)
        # The P&L-restatement quarantine rows must never feed agent learning.
        assert "p_fabricated" not in selected
        assert "p_dupe" not in selected

    def test_broker_sync_artifact_excluded(self, positions_db):
        selected = self._selected(positions_db)
        assert "p_tws_sync" not in selected

    def test_open_and_unclosed_excluded(self, positions_db):
        selected = self._selected(positions_db)
        assert "p_open" not in selected
        assert "p_no_date" not in selected

    def test_session_prefix_match_is_anchored(self, tmp_path):
        # 'session:' must be a prefix (LIKE 'session:%'); a non-session source that
        # merely contains the word must NOT slip through.
        db = tmp_path / "anchored.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE positions (position_id TEXT, status TEXT, close_date TEXT, "
            "close_source TEXT, realized_pnl REAL NOT NULL DEFAULT 0, "
            "regime_at_entry TEXT DEFAULT 'neutral')"
        )
        conn.executemany(
            "INSERT INTO positions (position_id, status, close_date, close_source, realized_pnl) "
            "VALUES (?,?,?,?,0)",
            [
                ("good", "closed", "2026-06-10", "session:ceo_flatten"),
                ("bad",  "closed", "2026-06-10", "ended_session:foo"),
            ],
        )
        conn.commit()
        conn.close()
        assert self._selected(str(db)) == {"good"}

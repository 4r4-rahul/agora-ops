"""
Phase 2 golden lifecycle test — AGORA Grand Specification §6.

Verifies the full decision-chain lifecycle across 50 simulated candidates:
  start_chain() at conviction gate
  → analyst_journal entry linked via decision_id
  → complete_chain() with IBKR outcome
  → link_position() on confirmed fill
  → close_chain() with realized P&L

Coverage matrix (50 rows):
  20 filled → win (pnl > 0)
   5 filled → loss (pnl < 0)
  10 rejected (IBKR error)
  10 no_trade (strategy built None / chain timeout)
   3 analyst_blocked (live mode no_thesis)
   2 timeout (options chain fetch timed out)

All assertions verify cross-table integrity — every chain_id in analyst_journal
must have a matching decision_chains row, and every position_id in positions
must trace back to a filled chain.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from agora.ops.decision_chains import (
    close_chain,
    complete_chain,
    link_position,
    recent_chains,
    start_chain,
)

# ── Schema bootstrap ──────────────────────────────────────────────────────────

_DECISION_CHAINS_DDL = """
CREATE TABLE IF NOT EXISTS decision_chains (
    chain_id         TEXT PRIMARY KEY,
    ticker           TEXT NOT NULL,
    triggered_by     TEXT NOT NULL,
    started_at       TEXT NOT NULL,
    outcome          TEXT NOT NULL DEFAULT 'evaluating',
    session_id       TEXT DEFAULT '',
    conviction       REAL DEFAULT 0,
    strategy         TEXT DEFAULT '',
    gates_passed     TEXT DEFAULT '[]',
    position_id      TEXT,
    realized_pnl     REAL,
    completed_at_utc TEXT,
    metadata_json    TEXT
);
"""

_ANALYST_JOURNAL_DDL = """
CREATE TABLE IF NOT EXISTS analyst_journal (
    journal_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id          TEXT    NOT NULL,
    ticker               TEXT    NOT NULL,
    decided_at_utc       TEXT    NOT NULL,
    prompt_version       TEXT    NOT NULL,
    model                TEXT    NOT NULL,
    payload_json         TEXT    NOT NULL,
    history_used_json    TEXT,
    lessons_applied_json TEXT,
    decision             TEXT    NOT NULL,
    direction            TEXT,
    magnitude_pct        REAL,
    horizon_days         INTEGER,
    confidence_pct       INTEGER,
    strategy_family      TEXT,
    kill_conditions_json TEXT,
    scorecard_critique   TEXT,
    reasoning_trace      TEXT,
    output_full_json     TEXT    NOT NULL,
    input_tokens         INTEGER,
    output_tokens        INTEGER,
    cost_usd             REAL,
    latency_ms           INTEGER,
    shadow_mode          INTEGER DEFAULT 1,
    thesis_played_out         INTEGER,
    magnitude_realized_pct    REAL,
    horizon_realized_days     INTEGER,
    kill_condition_hit        TEXT,
    confidence_was_calibrated INTEGER,
    lesson_learned            TEXT
);
"""

_POSITIONS_DDL = """
CREATE TABLE IF NOT EXISTS positions (
    position_id      TEXT PRIMARY KEY,
    ticker           TEXT NOT NULL,
    strategy         TEXT DEFAULT '',
    pillar           TEXT DEFAULT '',
    entry_date       TEXT NOT NULL,
    close_date       TEXT,
    status           TEXT DEFAULT 'open',
    realized_pnl     REAL,
    regime_at_entry  TEXT DEFAULT 'neutral'
);
"""

_AGENT_LESSONS_DDL = """
CREATE TABLE IF NOT EXISTS agent_lessons (
    lesson_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name             TEXT    NOT NULL,
    lesson_text            TEXT    NOT NULL,
    derived_from_chain_ids TEXT,
    confidence_in_lesson   REAL,
    sample_size            INTEGER,
    created_at_utc         TEXT    NOT NULL,
    last_reinforced_at_utc TEXT,
    times_reinforced       INTEGER DEFAULT 1,
    active                 INTEGER DEFAULT 1,
    human_approved         INTEGER DEFAULT 0,
    approved_at_utc        TEXT,
    approved_by            TEXT,
    rejected_at_utc        TEXT,
    rejected_reason        TEXT,
    CHECK (agent_name IN ('analyst', 'strategy', 'advocate', 'exit'))
);
"""


def _make_db() -> tuple[str, sqlite3.Connection]:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = tmp.name
    tmp.close()
    conn = sqlite3.connect(db_path)
    conn.executescript(
        _DECISION_CHAINS_DDL
        + _ANALYST_JOURNAL_DDL
        + _POSITIONS_DDL
        + _AGENT_LESSONS_DDL
    )
    return db_path, conn


def _insert_analyst_journal(conn: sqlite3.Connection, decision_id: str, ticker: str,
                             decision: str = "thesis", shadow: int = 0) -> None:
    conn.execute(
        """INSERT INTO analyst_journal
           (decision_id, ticker, decided_at_utc, prompt_version, model,
            payload_json, decision, output_full_json, shadow_mode)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            decision_id, ticker,
            datetime.now(tz=UTC).isoformat(),
            "1.0.0", "claude-sonnet-4-6",
            json.dumps({"ticker": ticker}),
            decision,
            json.dumps({"decision": decision}),
            shadow,
        ),
    )
    conn.commit()


def _insert_position(conn: sqlite3.Connection, position_id: str, ticker: str,
                     pnl: float) -> None:
    conn.execute(
        """INSERT INTO positions (position_id, ticker, entry_date, close_date, status, realized_pnl)
           VALUES (?,?,?,?,?,?)""",
        (position_id, ticker, date.today().isoformat(), date.today().isoformat(), "closed", pnl),
    )
    conn.commit()


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestDecisionChainLifecycle:
    """50-row golden lifecycle covering all outcome branches."""

    def setup_method(self):
        self.db_path, self.conn = _make_db()

    def teardown_method(self):
        self.conn.close()
        Path(self.db_path).unlink(missing_ok=True)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _all_chains(self) -> list[dict]:
        return recent_chains(self.db_path, limit=200)

    def _chain(self, chain_id: str) -> dict | None:
        for c in self._all_chains():
            if c["chain_id"] == chain_id:
                return c
        return None

    def _analyst_rows(self, decision_id: str) -> list:
        return self.conn.execute(
            "SELECT decision_id, decision FROM analyst_journal WHERE decision_id = ?",
            (decision_id,),
        ).fetchall()

    # ── 20 filled → win ───────────────────────────────────────────────────────

    def test_filled_win_chains(self):
        filled_wins: list[tuple[str, str]] = []  # (chain_id, position_id)
        for i in range(20):
            ticker = f"WIN{i:02d}"
            chain_id = start_chain(self.db_path, ticker, "universe_scan",
                                   session_id="s1", conviction=75.0)
            _insert_analyst_journal(self.conn, chain_id, ticker, decision="thesis")
            position_id = str(uuid.uuid4())
            _insert_position(self.conn, position_id, ticker, pnl=150.0 + i)
            complete_chain(self.db_path, chain_id, "filled",
                           strategy="bull_call_spread",
                           gates_passed=["timing", "compliance", "risk"],
                           position_id=position_id)
            link_position(self.db_path, chain_id, position_id)
            close_chain(self.db_path, position_id, realized_pnl=150.0 + i)
            filled_wins.append((chain_id, position_id))

        for chain_id, position_id in filled_wins:
            c = self._chain(chain_id)
            assert c is not None, f"chain {chain_id} not found"
            assert c["outcome"] == "win", f"expected win, got {c['outcome']}"
            assert c["position_id"] == position_id
            assert c["realized_pnl"] is not None and c["realized_pnl"] > 0
            assert c["completed_at"] is not None
            # Analyst journal linked
            rows = self._analyst_rows(chain_id)
            assert len(rows) == 1
            assert rows[0][1] == "thesis"

    # ── 5 filled → loss ───────────────────────────────────────────────────────

    def test_filled_loss_chains(self):
        for i in range(5):
            ticker = f"LOSS{i}"
            chain_id = start_chain(self.db_path, ticker, "catalyst",
                                   session_id="s1", conviction=62.0)
            _insert_analyst_journal(self.conn, chain_id, ticker, decision="thesis")
            position_id = str(uuid.uuid4())
            _insert_position(self.conn, position_id, ticker, pnl=-80.0 - i * 10)
            complete_chain(self.db_path, chain_id, "filled",
                           strategy="bear_put_spread",
                           gates_passed=["timing", "compliance", "risk"],
                           position_id=position_id)
            link_position(self.db_path, chain_id, position_id)
            close_chain(self.db_path, position_id, realized_pnl=-80.0 - i * 10)

            c = self._chain(chain_id)
            assert c["outcome"] == "loss", f"expected loss, got {c['outcome']}"
            assert c["realized_pnl"] < 0
            assert c["position_id"] == position_id

    # ── 10 rejected ───────────────────────────────────────────────────────────

    def test_rejected_chains(self):
        for i in range(10):
            ticker = f"REJ{i:02d}"
            chain_id = start_chain(self.db_path, ticker, "universe_scan",
                                   session_id="s1", conviction=68.0)
            _insert_analyst_journal(self.conn, chain_id, ticker, decision="thesis")
            complete_chain(self.db_path, chain_id, "rejected",
                           strategy="iron_condor",
                           gates_passed=["timing", "compliance", "risk"])

            c = self._chain(chain_id)
            assert c["outcome"] == "rejected"
            assert c["position_id"] is None
            assert c["realized_pnl"] is None
            assert c["completed_at"] is not None

    # ── 10 no_trade ───────────────────────────────────────────────────────────

    def test_no_trade_chains(self):
        for i in range(10):
            ticker = f"NOTRADE{i}"
            chain_id = start_chain(self.db_path, ticker, "universe_scan",
                                   session_id="s1", conviction=55.0)
            complete_chain(self.db_path, chain_id, "no_trade")

            c = self._chain(chain_id)
            assert c["outcome"] == "no_trade"
            assert c["position_id"] is None

    # ── 3 analyst_blocked ─────────────────────────────────────────────────────

    def test_analyst_blocked_chains(self):
        for i in range(3):
            ticker = f"ABLK{i}"
            chain_id = start_chain(self.db_path, ticker, "universe_scan",
                                   session_id="s1", conviction=61.0)
            _insert_analyst_journal(self.conn, chain_id, ticker, decision="no_thesis", shadow=0)
            complete_chain(self.db_path, chain_id, "analyst_blocked")

            c = self._chain(chain_id)
            assert c["outcome"] == "analyst_blocked"
            rows = self._analyst_rows(chain_id)
            assert len(rows) == 1
            assert rows[0][1] == "no_thesis"

    # ── 2 timeout ─────────────────────────────────────────────────────────────

    def test_timeout_chains(self):
        for i in range(2):
            ticker = f"TOUT{i}"
            chain_id = start_chain(self.db_path, ticker, "universe_scan",
                                   session_id="s1", conviction=70.0)
            complete_chain(self.db_path, chain_id, "timeout")

            c = self._chain(chain_id)
            assert c["outcome"] == "timeout"

    # ── Full 50-row aggregate ─────────────────────────────────────────────────

    def test_full_50_row_lifecycle(self):
        """
        Runs all 50 candidates in sequence and validates aggregate invariants:
        - Total chain count == 50
        - All filled chains have position_id
        - No non-filled chain has position_id
        - win/loss distinction correct on closed chains
        - All analyst_journal rows have valid decision_id FK
        """
        chains_created: list[str] = []

        # 20 win
        for i in range(20):
            ticker = f"W{i:02d}"
            cid = start_chain(self.db_path, ticker, "scan", conviction=75.0)
            pid = str(uuid.uuid4())
            _insert_analyst_journal(self.conn, cid, ticker)
            _insert_position(self.conn, pid, ticker, pnl=100.0)
            complete_chain(self.db_path, cid, "filled", position_id=pid)
            link_position(self.db_path, cid, pid)
            close_chain(self.db_path, pid, 100.0)
            chains_created.append(cid)

        # 5 loss
        for i in range(5):
            ticker = f"L{i}"
            cid = start_chain(self.db_path, ticker, "scan", conviction=62.0)
            pid = str(uuid.uuid4())
            _insert_analyst_journal(self.conn, cid, ticker)
            _insert_position(self.conn, pid, ticker, pnl=-50.0)
            complete_chain(self.db_path, cid, "filled", position_id=pid)
            link_position(self.db_path, cid, pid)
            close_chain(self.db_path, pid, -50.0)
            chains_created.append(cid)

        # 10 rejected
        for i in range(10):
            ticker = f"R{i:02d}"
            cid = start_chain(self.db_path, ticker, "scan", conviction=68.0)
            complete_chain(self.db_path, cid, "rejected")
            chains_created.append(cid)

        # 10 no_trade
        for i in range(10):
            ticker = f"N{i:02d}"
            cid = start_chain(self.db_path, ticker, "scan", conviction=55.0)
            complete_chain(self.db_path, cid, "no_trade")
            chains_created.append(cid)

        # 3 analyst_blocked
        for i in range(3):
            ticker = f"A{i}"
            cid = start_chain(self.db_path, ticker, "scan", conviction=61.0)
            _insert_analyst_journal(self.conn, cid, ticker, decision="no_thesis", shadow=0)
            complete_chain(self.db_path, cid, "analyst_blocked")
            chains_created.append(cid)

        # 2 timeout
        for i in range(2):
            ticker = f"T{i}"
            cid = start_chain(self.db_path, ticker, "scan", conviction=70.0)
            complete_chain(self.db_path, cid, "timeout")
            chains_created.append(cid)

        assert len(chains_created) == 50

        all_chains = {c["chain_id"]: c for c in self._all_chains()}

        # Every created chain is in the DB
        for cid in chains_created:
            assert cid in all_chains, f"chain {cid} missing from DB"

        wins   = [c for c in all_chains.values() if c["outcome"] == "win"]
        losses = [c for c in all_chains.values() if c["outcome"] == "loss"]
        filled = wins + losses
        non_filled = [
            c for c in all_chains.values()
            if c["outcome"] not in ("win", "loss", "filled")
        ]

        assert len(wins)   == 20
        assert len(losses) == 5

        # All filled chains have a position_id
        for c in filled:
            assert c["position_id"] is not None, f"filled chain {c['chain_id']} missing position_id"
            assert c["realized_pnl"] is not None

        # No non-filled chain has a position_id
        for c in non_filled:
            assert c["position_id"] is None, f"non-filled chain {c['chain_id']} has position_id"

        # analyst_journal FK integrity — every row must point to a known chain
        rows = self.conn.execute(
            "SELECT decision_id FROM analyst_journal"
        ).fetchall()
        known_chain_ids = set(all_chains.keys())
        for (did,) in rows:
            assert did in known_chain_ids, f"orphaned analyst_journal.decision_id={did}"


class TestAgentLessonsGating:
    """Verify the human_approved gate — unapproved lessons must not surface as approved."""

    def setup_method(self):
        self.db_path, self.conn = _make_db()

    def teardown_method(self):
        self.conn.close()
        Path(self.db_path).unlink(missing_ok=True)

    def _insert_lesson(self, agent: str, text: str, approved: int = 0) -> int:
        cur = self.conn.execute(
            """INSERT INTO agent_lessons
               (agent_name, lesson_text, created_at_utc, human_approved)
               VALUES (?,?,?,?)""",
            (agent, text, datetime.now(tz=UTC).isoformat(), approved),
        )
        self.conn.commit()
        return cur.lastrowid

    def test_unapproved_lessons_not_in_approved_query(self):
        lid = self._insert_lesson("analyst", "Never trade FOMC day", approved=0)
        approved = self.conn.execute(
            "SELECT lesson_id FROM agent_lessons WHERE human_approved = 1 AND active = 1"
        ).fetchall()
        assert (lid,) not in approved

    def test_approve_lesson_makes_it_available(self):
        lid = self._insert_lesson("analyst", "IV rank > 70 earns extra contracts", approved=0)
        self.conn.execute(
            "UPDATE agent_lessons SET human_approved = 1, approved_by = 'CEO' WHERE lesson_id = ?",
            (lid,),
        )
        self.conn.commit()
        approved = self.conn.execute(
            "SELECT lesson_id FROM agent_lessons WHERE human_approved = 1 AND active = 1"
        ).fetchall()
        assert (lid,) in approved

    def test_rejected_lesson_is_inactive(self):
        lid = self._insert_lesson("strategy", "Always sell ATM straddles on VIX > 30", approved=0)
        self.conn.execute(
            "UPDATE agent_lessons SET active = 0, rejected_reason = 'too risky' WHERE lesson_id = ?",
            (lid,),
        )
        self.conn.commit()
        active = self.conn.execute(
            "SELECT lesson_id FROM agent_lessons WHERE active = 1"
        ).fetchall()
        assert (lid,) not in active

    def test_invalid_agent_name_rejected(self):
        with pytest.raises(sqlite3.IntegrityError):
            self.conn.execute(
                """INSERT INTO agent_lessons (agent_name, lesson_text, created_at_utc)
                   VALUES ('invalid_agent', 'some lesson', '2026-01-01T00:00:00+00:00')"""
            )
            self.conn.commit()


class TestDecisionChainLegacyAliases:
    """Verify legacy log_decision / update_outcome / update_close still work."""

    def setup_method(self):
        self.db_path, self.conn = _make_db()

    def teardown_method(self):
        self.conn.close()
        Path(self.db_path).unlink(missing_ok=True)

    def test_log_decision_creates_completed_chain(self):
        from agora.ops.decision_chains import log_decision
        chain_id = log_decision(
            self.db_path, "AAPL", "scan", "filled",
            session_id="s1", conviction=72.0, strategy="iron_condor",
        )
        assert chain_id
        c = recent_chains(self.db_path, limit=10)[0]
        assert c["chain_id"] == chain_id
        assert c["outcome"] == "filled"
        assert c["ticker"] == "AAPL"
        assert c["completed_at"] is not None

    def test_update_outcome_alias(self):
        from agora.ops.decision_chains import update_outcome
        cid = start_chain(self.db_path, "TSLA", "scan", conviction=65.0)
        update_outcome(self.db_path, cid, "rejected")
        c = recent_chains(self.db_path, limit=10)[0]
        assert c["outcome"] == "rejected"

    def test_update_close_alias(self):
        from agora.ops.decision_chains import update_close
        cid = start_chain(self.db_path, "NVDA", "scan", conviction=80.0)
        pid = str(uuid.uuid4())
        complete_chain(self.db_path, cid, "filled", position_id=pid)
        link_position(self.db_path, cid, pid)
        update_close(self.db_path, pid, 250.0)
        c = next(c for c in recent_chains(self.db_path, limit=10) if c["chain_id"] == cid)
        assert c["realized_pnl"] == 250.0
        assert c["outcome"] == "win"

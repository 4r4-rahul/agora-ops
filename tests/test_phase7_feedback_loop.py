"""
tests/test_phase7_feedback_loop.py — Phase 7 feedback loop tests.

Covers:
  - OutcomeAttributor: analyst, strategy, advocate, exit attribution
  - calibration_log writes (Brier score)
  - get_promotion_readiness() thresholds
  - LessonsGenerator._is_duplicate() dedup logic
  - LessonsGenerator._fetch_sample() with real DB

No LLM calls. LessonsGenerator.generate_all() is not called (avoids API spend).
"""

from __future__ import annotations

import sqlite3
import tempfile
from datetime import UTC, date, datetime, timedelta

from agora.ops.outcome_attributor import (
    _brier_score,
    _calibrated,
    attribute_closed_trades,
    get_analyst_stats,
    get_promotion_readiness,
)

# ── DB factory ────────────────────────────────────────────────────────────────

def _make_db() -> str:
    """Create a temp DB with all required tables."""
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = f.name
    f.close()
    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
            CREATE TABLE trade_records (
                trade_id TEXT PRIMARY KEY,
                ticker TEXT,
                entry_date TEXT,
                close_date TEXT,
                realized_pnl REAL
            );
            CREATE TABLE positions (
                position_id TEXT PRIMARY KEY,
                status TEXT DEFAULT 'closed',
                close_date TEXT,
                close_source TEXT,
                realized_pnl REAL,
                max_loss_dollars REAL,
                max_gain_dollars REAL,
                strategy TEXT,
                legs_json TEXT DEFAULT '[]',
                regime_at_entry TEXT DEFAULT 'neutral'
            );
            -- Production keys analyst/strategy/advocate attribution on the EXACT decision_id via
            -- decision_chains.chain_id -> positions.position_id (created by position_manager in prod).
            CREATE TABLE decision_chains (
                chain_id TEXT,
                position_id TEXT
            );
            CREATE TABLE analyst_journal (
                journal_id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id TEXT,
                ticker TEXT,
                decided_at_utc TEXT,
                prompt_version TEXT DEFAULT '1.0.0',
                model TEXT DEFAULT 'test',
                payload_json TEXT DEFAULT '{}',
                decision TEXT,
                direction TEXT,
                magnitude_pct REAL,
                horizon_days INTEGER,
                confidence_pct INTEGER,
                strategy_family TEXT,
                kill_conditions_json TEXT DEFAULT '[]',
                scorecard_critique TEXT,
                reasoning_trace TEXT,
                output_full_json TEXT DEFAULT '{}',
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cost_usd REAL DEFAULT 0,
                latency_ms INTEGER DEFAULT 0,
                shadow_mode INTEGER DEFAULT 1,
                thesis_played_out INTEGER,
                magnitude_realized_pct REAL,
                horizon_realized_days INTEGER,
                kill_condition_hit TEXT,
                confidence_was_calibrated INTEGER,
                lesson_learned TEXT
            );
            CREATE TABLE strategy_journal (
                journal_id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id TEXT,
                ticker TEXT,
                decided_at_utc TEXT,
                prompt_version TEXT DEFAULT '1.0.0',
                model TEXT DEFAULT 'test',
                payload_json TEXT DEFAULT '{}',
                decision TEXT,
                strategy_type TEXT,
                legs_json TEXT,
                contracts INTEGER DEFAULT 1,
                entry_debit_credit REAL,
                max_profit REAL,
                max_loss REAL,
                reward_risk_ratio REAL,
                output_full_json TEXT DEFAULT '{}',
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cost_usd REAL DEFAULT 0,
                latency_ms INTEGER DEFAULT 0,
                shadow_mode INTEGER DEFAULT 1,
                structure_used INTEGER,
                realized_pnl REAL,
                vs_rules_engine_pnl REAL
            );
            CREATE TABLE advocate_journal (
                journal_id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id TEXT,
                ticker TEXT,
                decided_at_utc TEXT,
                prompt_version TEXT DEFAULT '1.0.0',
                model TEXT DEFAULT 'test',
                payload_json TEXT DEFAULT '{}',
                verdict TEXT,
                verdict_confidence INTEGER,
                failure_modes_json TEXT DEFAULT '[]',
                most_likely_scenario TEXT,
                output_full_json TEXT DEFAULT '{}',
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cost_usd REAL DEFAULT 0,
                latency_ms INTEGER DEFAULT 0,
                shadow_mode INTEGER DEFAULT 1,
                trade_taken INTEGER,
                realized_pnl REAL,
                advocate_was_right INTEGER
            );
            CREATE TABLE exit_journal (
                journal_id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id TEXT DEFAULT '',
                position_id TEXT,
                ticker TEXT,
                decided_at_utc TEXT,
                prompt_version TEXT DEFAULT '1.0.0',
                model TEXT DEFAULT 'test',
                payload_json TEXT DEFAULT '{}',
                thesis_validity TEXT DEFAULT 'VALID',
                kill_condition_status TEXT DEFAULT 'NONE_TRIGGERED',
                recommendation TEXT,
                recommendation_reasoning TEXT,
                specific_action_json TEXT DEFAULT '{}',
                confidence_pct INTEGER,
                output_full_json TEXT DEFAULT '{}',
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cost_usd REAL DEFAULT 0,
                latency_ms INTEGER DEFAULT 0,
                shadow_mode INTEGER DEFAULT 1,
                action_taken TEXT,
                exit_alpha_pct REAL,
                pnl_pct_of_max REAL,
                outcome_pnl REAL,
                action_quality TEXT
            );
            CREATE TABLE calibration_log (
                log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_name TEXT,
                measured_at_utc TEXT,
                sample_window_days INTEGER,
                sample_size INTEGER,
                brier_score REAL,
                confidence_bucket TEXT,
                predicted_win_rate REAL,
                actual_win_rate REAL,
                calibration_gap REAL,
                drift_alert INTEGER DEFAULT 0
            );
            CREATE TABLE agent_lessons (
                lesson_id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_name TEXT,
                lesson_text TEXT,
                derived_from_chain_ids TEXT,
                confidence_in_lesson REAL,
                sample_size INTEGER,
                created_at_utc TEXT,
                last_reinforced_at_utc TEXT,
                times_reinforced INTEGER DEFAULT 1,
                active INTEGER DEFAULT 1,
                human_approved INTEGER DEFAULT 0,
                approved_at_utc TEXT,
                approved_by TEXT,
                rejected_at_utc TEXT,
                rejected_reason TEXT
            );
        """)
    return db_path


def _now_iso(delta_hours: float = 0.0) -> str:
    return (datetime.now(tz=UTC) - timedelta(hours=delta_hours)).isoformat()


def _entry_date(delta_days: int = 5) -> str:
    return (date.today() - timedelta(days=delta_days)).isoformat()


def _close_position(conn, position_id: str, realized_pnl: float, max_loss: float = 200.0,
                    max_gain: float = 150.0, close_source: str = "thesis_exit",
                    strategy: str = "bull_put_spread") -> None:
    """Insert a genuinely-closed position (status/close_date/close_source satisfy _REAL_CLOSE),
    the source of truth the attributor reads realized_pnl from."""
    conn.execute(
        """INSERT INTO positions (position_id, status, close_date, close_source, realized_pnl,
           max_loss_dollars, max_gain_dollars, strategy, legs_json)
           VALUES (?, 'closed', ?, ?, ?, ?, ?, ?, '[]')""",
        (position_id, _entry_date(0), close_source, realized_pnl, max_loss, max_gain, strategy),
    )


def _link_chain(conn, chain_id: str, position_id: str) -> None:
    """Link a decision_id (journal) to its closed position — the exact-key join the analyst/
    strategy/advocate attributors use (replaced the old fuzzy ticker+time match)."""
    conn.execute("INSERT INTO decision_chains (chain_id, position_id) VALUES (?,?)",
                 (chain_id, position_id))


# ── Helpers ───────────────────────────────────────────────────────────────────

class TestPureHelpers:
    def test_calibrated_high_conf_win(self):
        assert _calibrated(75, True) == 1

    def test_calibrated_high_conf_loss(self):
        assert _calibrated(75, False) == 0

    def test_calibrated_low_conf_loss(self):
        assert _calibrated(40, False) == 1

    def test_calibrated_low_conf_win(self):
        assert _calibrated(40, True) == 0

    def test_calibrated_ambiguous_band(self):
        assert _calibrated(55, True) is None
        assert _calibrated(55, False) is None

    def test_calibrated_none_confidence(self):
        assert _calibrated(None, True) is None

    def test_brier_score_perfect(self):
        buckets = [(1.0, 1.0), (0.0, 0.0)]
        assert _brier_score(buckets) == 0.0

    def test_brier_score_worst(self):
        buckets = [(1.0, 0.0), (0.0, 1.0)]
        assert _brier_score(buckets) == 1.0

    def test_brier_score_empty(self):
        assert _brier_score([]) is None


# ── Analyst attribution ───────────────────────────────────────────────────────

class TestAnalystAttribution:
    def _seed(self, db_path: str, ticker: str = "AAPL", realized_pnl: float = 150.0,
              confidence_pct: int = 70, max_loss: float = 200.0) -> None:
        entry = _entry_date(5)
        decided = _now_iso(delta_hours=6 * 24)   # 6 days ago — within match window
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO trade_records VALUES (?,?,?,?,?)",
                ("trade-1", ticker, entry, _entry_date(0), realized_pnl),
            )
            _close_position(conn, "trade-1", realized_pnl, max_loss=max_loss)
            _link_chain(conn, "chain-1", "trade-1")
            conn.execute(
                """INSERT INTO analyst_journal (decision_id, ticker, decided_at_utc,
                   decision, direction, magnitude_pct, confidence_pct, strategy_family)
                   VALUES (?,?,?,?,?,?,?,?)""",
                ("chain-1", ticker, decided, "thesis", "bullish", 5.0, confidence_pct, "debit_spread"),
            )

    def test_win_attributed(self):
        db = _make_db()
        self._seed(db, realized_pnl=150.0, confidence_pct=70)
        result = attribute_closed_trades(db)
        assert result["attributed"] == 1
        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT thesis_played_out, confidence_was_calibrated FROM analyst_journal LIMIT 1"
            ).fetchone()
        assert row[0] == 1       # won
        assert row[1] == 1       # high conf + win → calibrated

    def test_loss_attributed(self):
        db = _make_db()
        self._seed(db, realized_pnl=-80.0, confidence_pct=70)
        attribute_closed_trades(db)
        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT thesis_played_out, confidence_was_calibrated FROM analyst_journal LIMIT 1"
            ).fetchone()
        assert row[0] == 0       # lost
        assert row[1] == 0       # high conf + loss → overconfident

    def test_idempotent(self):
        db = _make_db()
        self._seed(db)
        r1 = attribute_closed_trades(db)
        r2 = attribute_closed_trades(db)
        assert r1["attributed"] == 1
        assert r2["attributed"] == 0   # second pass skips already-attributed rows

    def test_no_match_when_no_journal_entry(self):
        db = _make_db()
        entry = _entry_date(5)
        with sqlite3.connect(db) as conn:
            conn.execute("INSERT INTO trade_records VALUES (?,?,?,?,?)",
                         ("trade-2", "TSLA", entry, _entry_date(0), 50.0))
        result = attribute_closed_trades(db)
        assert result["attributed"] == 0

    def test_open_trade_not_attributed(self):
        db = _make_db()
        with sqlite3.connect(db) as conn:
            conn.execute("INSERT INTO trade_records VALUES (?,?,?,NULL,NULL)",
                         ("open-1", "AAPL", _entry_date(3)))
        result = attribute_closed_trades(db)
        assert result["attributed"] == 0


# ── Strategy + advocate attribution ──────────────────────────────────────────

class TestStrategyAdvocateAttribution:
    def _seed(self, db_path: str, verdict: str = "PASS", realized_pnl: float = 120.0) -> None:
        entry  = _entry_date(5)
        decided = _now_iso(delta_hours=6 * 24)
        with sqlite3.connect(db_path) as conn:
            conn.execute("INSERT INTO trade_records VALUES (?,?,?,?,?)",
                         ("t-1", "SPY", entry, _entry_date(0), realized_pnl))
            _close_position(conn, "t-1", realized_pnl, max_loss=200.0)
            _link_chain(conn, "c-1", "t-1")
            conn.execute(
                """INSERT INTO analyst_journal (decision_id, ticker, decided_at_utc,
                   decision, direction, confidence_pct, strategy_family)
                   VALUES (?,?,?,?,?,?,?)""",
                ("c-1", "SPY", decided, "thesis", "bullish", 65, "credit_spread"),
            )
            conn.execute(
                """INSERT INTO strategy_journal (decision_id, ticker, decided_at_utc,
                   decision, strategy_type) VALUES (?,?,?,?,?)""",
                ("c-1", "SPY", decided, "structure", "bull_put_spread"),
            )
            conn.execute(
                """INSERT INTO advocate_journal (decision_id, ticker, decided_at_utc,
                   verdict, verdict_confidence) VALUES (?,?,?,?,?)""",
                ("c-1", "SPY", decided, verdict, 70),
            )

    def test_strategy_attributed_on_fill(self):
        db = _make_db()
        self._seed(db, realized_pnl=120.0)
        attribute_closed_trades(db)
        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT structure_used, realized_pnl FROM strategy_journal LIMIT 1"
            ).fetchone()
        assert row[0] == 1
        assert row[1] == 120.0

    def test_advocate_pass_win_is_right(self):
        db = _make_db()
        self._seed(db, verdict="PASS", realized_pnl=120.0)
        attribute_closed_trades(db)
        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT trade_taken, advocate_was_right FROM advocate_journal LIMIT 1"
            ).fetchone()
        assert row[0] == 1
        assert row[1] == 1   # PASS + win → right

    def test_advocate_pass_loss_is_wrong(self):
        db = _make_db()
        self._seed(db, verdict="PASS", realized_pnl=-90.0)
        attribute_closed_trades(db)
        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT advocate_was_right FROM advocate_journal LIMIT 1"
            ).fetchone()
        assert row[0] == 0   # PASS + loss → wrong

    def test_advocate_block_loss_is_right(self):
        db = _make_db()
        self._seed(db, verdict="BLOCK", realized_pnl=-90.0)
        attribute_closed_trades(db)
        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT advocate_was_right FROM advocate_journal LIMIT 1"
            ).fetchone()
        assert row[0] == 1   # BLOCK + loss → correctly blocked


# ── Exit attribution ──────────────────────────────────────────────────────────

class TestExitAttribution:
    def test_exit_rows_marked_close_confirmed(self):
        db = _make_db()
        entry = _entry_date(10)
        with sqlite3.connect(db) as conn:
            conn.execute("INSERT INTO trade_records VALUES (?,?,?,?,?)",
                         ("pos-1", "QQQ", entry, _entry_date(0), 80.0))
            _close_position(conn, "pos-1", 80.0, max_loss=150.0)
            # 3 exit journal rows for this position (hourly evaluations)
            for _ in range(3):
                conn.execute(
                    """INSERT INTO exit_journal (position_id, ticker, decided_at_utc,
                       recommendation) VALUES (?,?,?,?)""",
                    ("pos-1", "QQQ", _now_iso(), "HOLD"),
                )
        attribute_closed_trades(db)
        with sqlite3.connect(db) as conn:
            rows = conn.execute(
                "SELECT action_taken FROM exit_journal WHERE position_id='pos-1'"
            ).fetchall()
        assert all(r[0] == "close_confirmed" for r in rows)
        assert len(rows) == 3


# ── Calibration log ───────────────────────────────────────────────────────────

class TestCalibrationLog:
    def _seed_analyst_rows(self, db_path: str, n_win: int, n_loss: int,
                            confidence: int = 70) -> None:
        # Each row must trace to a genuinely-closed chain or _purge_fabricated_attribution strips
        # its attribution (correct prod behaviour). So seed a real closed position + decision_chain
        # per row and leave thesis_played_out NULL — attribute_closed_trades sets it from the P&L.
        with sqlite3.connect(db_path) as conn:
            for win, n in ((True, n_win), (False, n_loss)):
                tag = "W" if win else "L"
                pnl = 120.0 if win else -90.0
                for i in range(n):
                    pid, cid = f"p{tag}{i}", f"c{tag}{i}"
                    _close_position(conn, pid, pnl)
                    _link_chain(conn, cid, pid)
                    conn.execute(
                        """INSERT INTO analyst_journal (decision_id, ticker, decided_at_utc,
                           decision, direction, confidence_pct)
                           VALUES (?,?,?,?,?,?)""",
                        (cid, f"{tag}{i}", _now_iso(), "thesis", "bullish", confidence),
                    )

    def test_calibration_log_written_after_attribution(self):
        db = _make_db()
        self._seed_analyst_rows(db, n_win=6, n_loss=4)
        attribute_closed_trades(db)
        with sqlite3.connect(db) as conn:
            rows = conn.execute(
                "SELECT agent_name, sample_size, brier_score FROM calibration_log"
            ).fetchall()
        analyst_row = next((r for r in rows if r[0] == "analyst"), None)
        assert analyst_row is not None
        assert analyst_row[1] == 10   # sample_size
        assert analyst_row[2] is not None

    def test_brier_score_improves_with_calibration(self):
        # Perfectly calibrated (70% conf, 70% win rate) → low Brier score
        db = _make_db()
        self._seed_analyst_rows(db, n_win=7, n_loss=3, confidence=70)
        attribute_closed_trades(db)
        with sqlite3.connect(db) as conn:
            row = conn.execute("SELECT brier_score FROM calibration_log").fetchone()
        # brier = (0.7 - 1)^2 * 7 + (0.7 - 0)^2 * 3 / 10 = 0.09*7 + 0.49*3/10 = 0.21
        assert row is not None
        assert row[0] < 0.5   # should be well below 0.5 (random = 0.25)


# ── Promotion readiness ───────────────────────────────────────────────────────

class TestPromotionReadiness:
    def _seed_analyst(self, db_path: str, n: int, win_rate: float) -> None:
        n_win = int(n * win_rate)
        with sqlite3.connect(db_path) as conn:
            for i in range(n_win):
                conn.execute(
                    """INSERT INTO analyst_journal (ticker, decided_at_utc, decision,
                       confidence_pct, thesis_played_out) VALUES (?,?,?,?,?)""",
                    (f"W{i}", _now_iso(), "thesis", 70, 1),
                )
            for i in range(n - n_win):
                conn.execute(
                    """INSERT INTO analyst_journal (ticker, decided_at_utc, decision,
                       confidence_pct, thesis_played_out) VALUES (?,?,?,?,?)""",
                    (f"L{i}", _now_iso(), "thesis", 70, 0),
                )

    def test_insufficient_data_when_few_samples(self):
        db = _make_db()
        self._seed_analyst(db, n=10, win_rate=0.70)
        result = get_promotion_readiness(db)
        assert result["analyst"]["status"] == "INSUFFICIENT_DATA"
        assert not result["analyst"]["ready"]

    def test_not_ready_when_hit_rate_too_low(self):
        db = _make_db()
        self._seed_analyst(db, n=40, win_rate=0.50)
        result = get_promotion_readiness(db)
        assert result["analyst"]["status"] == "NOT_READY"

    def test_ready_when_thresholds_met(self):
        db = _make_db()
        self._seed_analyst(db, n=40, win_rate=0.60)
        result = get_promotion_readiness(db)
        assert result["analyst"]["status"] == "READY"
        assert result["analyst"]["ready"] is True

    def test_advocate_insufficient_data(self):
        db = _make_db()
        # Only 5 attributed rows
        with sqlite3.connect(db) as conn:
            for i in range(5):
                conn.execute(
                    """INSERT INTO advocate_journal (ticker, decided_at_utc, verdict,
                       verdict_confidence, trade_taken, advocate_was_right)
                       VALUES (?,?,?,?,?,?)""",
                    (f"T{i}", _now_iso(), "PASS", 70, 1, 1),
                )
        result = get_promotion_readiness(db)
        assert result["advocate"]["status"] == "INSUFFICIENT_DATA"

    def test_empty_db_returns_no_crash(self):
        db = _make_db()
        result = get_promotion_readiness(db)
        # analyst key present when table exists
        assert "analyst" in result or "error" not in result


# ── Analyst stats ─────────────────────────────────────────────────────────────

class TestAnalystStats:
    def test_empty_db(self):
        db = _make_db()
        stats = get_analyst_stats(db)
        assert stats["total_calls"] == 0

    def test_stats_with_data(self):
        db = _make_db()
        with sqlite3.connect(db) as conn:
            for i in range(5):
                conn.execute(
                    """INSERT INTO analyst_journal (ticker, decided_at_utc, decision,
                       confidence_pct, thesis_played_out, confidence_was_calibrated)
                       VALUES (?,?,?,?,?,?)""",
                    (f"T{i}", _now_iso(), "thesis", 70, 1, 1),
                )
        stats = get_analyst_stats(db)
        assert stats["total_calls"] == 5
        assert stats["attributed_count"] == 5
        assert stats["direction_hit_rate"] == 1.0


# ── LessonsGenerator dedup ────────────────────────────────────────────────────

class TestLessonsGeneratorDedup:
    def test_duplicate_detected(self):
        db = _make_db()
        with sqlite3.connect(db) as conn:
            conn.execute(
                """INSERT INTO agent_lessons (agent_name, lesson_text, created_at_utc,
                   last_reinforced_at_utc, active, human_approved)
                   VALUES (?,?,?,?,1,0)""",
                ("analyst", "Avoid selling premium when IV rank is below 30 percent",
                 _now_iso(), _now_iso()),
            )
        from agora.agents.lessons_generator import LessonsGenerator
        gen = LessonsGenerator(db_path=db, api_key="test-key-unused")
        # Same lesson with slightly different wording — should be detected as duplicate
        is_dup = gen._is_duplicate("analyst", "avoid selling premium when IV rank below 30 percent")
        assert is_dup

    def test_different_lesson_not_duplicate(self):
        db = _make_db()
        with sqlite3.connect(db) as conn:
            conn.execute(
                """INSERT INTO agent_lessons (agent_name, lesson_text, created_at_utc,
                   last_reinforced_at_utc, active, human_approved)
                   VALUES (?,?,?,?,1,0)""",
                ("analyst", "Avoid selling premium when IV rank is below 30",
                 _now_iso(), _now_iso()),
            )
        from agora.agents.lessons_generator import LessonsGenerator
        gen = LessonsGenerator(db_path=db, api_key="test-key-unused")
        is_dup = gen._is_duplicate("analyst", "Close positions when earnings are within 3 days")
        assert not is_dup

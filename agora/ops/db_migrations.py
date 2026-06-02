"""
agora/ops/db_migrations.py — Idempotent schema migrations.

Called once at session startup before any agent opens the DB.
Each migration is idempotent: safe to run multiple times.
Add new migrations at the bottom; never edit or remove existing ones.
"""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)


def run_all(db_path: str) -> None:
    """Run all pending migrations in order. Fast no-op if already applied."""
    for fn in [
        _m001_expand_agent_lessons_check,
        _m002_uw_alerts_table,
        _m003_naked_journal,
        _m004_long_journal,
        _m005_signal_stats,
    ]:
        try:
            fn(db_path)
        except Exception as exc:
            logger.error("Migration %s failed: %s", fn.__name__, exc)


def _m001_expand_agent_lessons_check(db_path: str) -> None:
    """
    Expand agent_lessons CHECK constraint to include swing_judge, defender, system.
    Original constraint only allowed analyst|strategy|advocate|exit.
    Also changes human_approved default from 0 to NULL (pending = NULL, not 0).
    """
    with sqlite3.connect(db_path, timeout=15) as conn:
        conn.execute("PRAGMA journal_mode=WAL")

        # Check if migration already applied by inspecting current constraint
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='agent_lessons'"
        ).fetchone()
        if row is None:
            return  # Table doesn't exist yet — will be created correctly by first run

        if "swing_judge" in (row[0] or ""):
            return  # Already migrated

        logger.info("Applying m001: expanding agent_lessons CHECK constraint")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS agent_lessons_m001 (
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
                human_approved         INTEGER,
                approved_at_utc        TEXT,
                approved_by            TEXT,
                rejected_at_utc        TEXT,
                rejected_reason        TEXT,
                CHECK (agent_name IN (
                    'analyst', 'strategy', 'advocate', 'exit',
                    'swing_judge', 'defender', 'system'
                ))
            );
            INSERT INTO agent_lessons_m001 SELECT * FROM agent_lessons;
            DROP TABLE agent_lessons;
            ALTER TABLE agent_lessons_m001 RENAME TO agent_lessons;
        """)
        logger.info("m001 applied: agent_lessons now accepts swing_judge/defender/system")


def _m002_uw_alerts_table(db_path: str) -> None:
    """
    Create uw_alerts table for UWMarketIntelAgent.
    Stores all raw Discord messages from #uw-alerts channel for classification,
    realtime flagging, and scheduled summaries.
    """
    with sqlite3.connect(db_path, timeout=15) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='uw_alerts'"
        ).fetchone()
        if row is not None:
            return  # already exists

        logger.info("Applying m002: creating uw_alerts table")
        conn.execute("""
            CREATE TABLE uw_alerts (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_msg_id       TEXT    UNIQUE NOT NULL,
                received_at_utc      TEXT    NOT NULL,
                author               TEXT,
                topic_type           TEXT,
                tickers              TEXT,
                content              TEXT,
                embeds_json          TEXT,
                flagged              INTEGER DEFAULT 0,
                flag_reason          TEXT,
                realtime_sent        INTEGER DEFAULT 0,
                included_in_summary  TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS ix_uw_alerts_received ON uw_alerts(received_at_utc)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_uw_alerts_topic    ON uw_alerts(topic_type)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_uw_alerts_flagged  ON uw_alerts(flagged, realtime_sent)")
        logger.info("m002 applied: uw_alerts table created")


def _m003_naked_journal(db_path: str) -> None:
    """
    Create naked_journal table for NakedOptionsAgent.
    Tracks every naked put/call decision: entry signals, strike selection,
    DTE reasoning, and outcome linkage for calibration.
    """
    with sqlite3.connect(db_path, timeout=15) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='naked_journal'"
        ).fetchone()
        if row is not None:
            return

        logger.info("Applying m003: creating naked_journal table")
        conn.execute("""
            CREATE TABLE naked_journal (
                journal_id       INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id      TEXT,
                ticker           TEXT    NOT NULL,
                strategy         TEXT    NOT NULL,     -- naked_put | naked_call
                direction        TEXT    NOT NULL,     -- bullish | bearish
                decided_at_utc   TEXT    NOT NULL,
                spot             REAL,
                strike           REAL,
                expiry           TEXT,
                dte              INTEGER,
                delta_approx     REAL,
                premium_per_sh   REAL,
                ivr              REAL,
                vix              REAL,
                regime           TEXT,
                flow_direction   TEXT,
                dte_reason       TEXT,
                outcome          TEXT,                 -- filled | skipped | blocked | error
                block_reason     TEXT,
                max_loss_dollars REAL,
                max_gain_dollars REAL,
                contracts        INTEGER DEFAULT 1
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS ix_naked_ticker   ON naked_journal(ticker)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_naked_strategy ON naked_journal(strategy, decided_at_utc)")
        logger.info("m003 applied: naked_journal table created")


def _m004_long_journal(db_path: str) -> None:
    """
    Create long_journal table for LongOptionsAgent.
    Tracks every long call/put decision: signal stack at entry, strike selection,
    conviction score, and outcome linkage for calibration.
    """
    with sqlite3.connect(db_path, timeout=15) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='long_journal'"
        ).fetchone()
        if row is not None:
            return

        logger.info("Applying m004: creating long_journal table")
        conn.execute("""
            CREATE TABLE long_journal (
                journal_id       INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id      TEXT,
                ticker           TEXT    NOT NULL,
                strategy         TEXT    NOT NULL,   -- long_call | long_put
                direction        TEXT    NOT NULL,   -- bullish | bearish
                decided_at_utc   TEXT    NOT NULL,
                strike           REAL,
                expiry           TEXT,
                dte              INTEGER,
                delta_approx     REAL,
                premium_per_sh   REAL,
                ivr              REAL,
                vix              REAL,
                regime           TEXT,
                flow_direction   TEXT,
                momentum_score   REAL,
                conviction_score INTEGER,
                signal_stack     TEXT,               -- JSON: which signals fired and weights
                outcome          TEXT,               -- proceed | skipped | blocked | error
                block_reason     TEXT,
                max_loss_dollars REAL,               -- premium paid × 100
                max_gain_dollars REAL,
                contracts        INTEGER DEFAULT 1
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS ix_long_ticker   ON long_journal(ticker)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_long_strategy ON long_journal(strategy, decided_at_utc)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_long_outcome  ON long_journal(outcome, decided_at_utc)")
        logger.info("m004 applied: long_journal table created")


def _m005_signal_stats(db_path: str) -> None:
    """
    Create signal_stats table for signal calibration loop.
    Tracks per-signal win rate and P&L for LongOptionsAgent self-calibration.
    Updated on every position close via update_signal_stats().
    """
    with sqlite3.connect(db_path, timeout=15) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='signal_stats'"
        ).fetchone()
        if row is not None:
            return

        logger.info("Applying m005: creating signal_stats table")
        conn.execute("""
            CREATE TABLE signal_stats (
                signal_name      TEXT    NOT NULL,   -- e.g. 'flow', 'momentum', 'vol_surge'
                direction        TEXT    NOT NULL,   -- bullish | bearish
                total_trades     INTEGER DEFAULT 0,
                wins             INTEGER DEFAULT 0,
                losses           INTEGER DEFAULT 0,
                total_pnl        REAL    DEFAULT 0.0,
                avg_pnl          REAL    DEFAULT 0.0,
                win_rate         REAL    DEFAULT 0.0,
                last_updated_utc TEXT,
                PRIMARY KEY (signal_name, direction)
            )
        """)
        logger.info("m005 applied: signal_stats table created")

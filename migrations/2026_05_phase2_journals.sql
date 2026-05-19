-- ============================================================
-- Phase 2 — Journaling Foundation
-- AGORA Grand Specification §6 + §14
-- Applied: 2026-05-18
-- Rollback: migrations/2026_05_phase2_journals_down.sql
-- ============================================================

-- ── Extend decision_chains to full spec schema ────────────────
-- Add missing columns (idempotent ALTER TABLE approach for SQLite)
-- SQLite cannot add FK constraints after creation; columns added only.

ALTER TABLE decision_chains ADD COLUMN completed_at_utc TEXT;
ALTER TABLE decision_chains ADD COLUMN metadata_json TEXT;

-- Backfill: mark existing filled chains as completed
UPDATE decision_chains
SET completed_at_utc = started_at
WHERE outcome IN ('filled', 'rejected') AND completed_at_utc IS NULL;

-- ── analyst_journal ───────────────────────────────────────────
-- Matches §14 schema exactly. decision_id links every journal
-- entry to the chain that spawned it, enabling full correlation.
CREATE TABLE IF NOT EXISTS analyst_journal (
    journal_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id          TEXT    NOT NULL,   -- FK → decision_chains.chain_id
    ticker               TEXT    NOT NULL,
    decided_at_utc       TEXT    NOT NULL,
    prompt_version       TEXT    NOT NULL,
    model                TEXT    NOT NULL,

    -- Input context
    payload_json         TEXT    NOT NULL,
    history_used_json    TEXT,
    lessons_applied_json TEXT,

    -- Output
    decision             TEXT    NOT NULL,   -- 'thesis' | 'no_thesis'
    direction            TEXT,
    magnitude_pct        REAL,
    horizon_days         INTEGER,
    confidence_pct       INTEGER,
    strategy_family      TEXT,
    kill_conditions_json TEXT,
    scorecard_critique   TEXT,
    reasoning_trace      TEXT,
    output_full_json     TEXT    NOT NULL,

    -- Cost / perf metadata
    input_tokens         INTEGER,
    output_tokens        INTEGER,
    cost_usd             REAL,
    latency_ms           INTEGER,
    shadow_mode          INTEGER DEFAULT 1,

    -- Post-outcome attribution (filled by OutcomeAttributor)
    thesis_played_out         INTEGER,  -- 0/1
    magnitude_realized_pct    REAL,
    horizon_realized_days     INTEGER,
    kill_condition_hit        TEXT,
    confidence_was_calibrated INTEGER,  -- 0/1/NULL
    lesson_learned            TEXT
);
CREATE INDEX IF NOT EXISTS idx_analyst_ticker     ON analyst_journal(ticker, decided_at_utc);
CREATE INDEX IF NOT EXISTS idx_analyst_decision   ON analyst_journal(decision_id);
CREATE INDEX IF NOT EXISTS idx_analyst_outcome    ON analyst_journal(decision, decided_at_utc);

-- ── strategy_journal (Phase 6 — schema deployed now, empty) ──
CREATE TABLE IF NOT EXISTS strategy_journal (
    journal_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id       TEXT    NOT NULL,
    ticker            TEXT    NOT NULL,
    decided_at_utc    TEXT    NOT NULL,
    prompt_version    TEXT    NOT NULL,
    model             TEXT    NOT NULL,
    payload_json      TEXT    NOT NULL,
    decision          TEXT    NOT NULL,   -- 'structure' | 'no_structure'
    strategy_type     TEXT,
    legs_json         TEXT,
    contracts         INTEGER,
    entry_debit_credit REAL,
    max_profit        REAL,
    max_loss          REAL,
    reward_risk_ratio REAL,
    output_full_json  TEXT    NOT NULL,
    input_tokens      INTEGER,
    output_tokens     INTEGER,
    cost_usd          REAL,
    latency_ms        INTEGER,
    shadow_mode       INTEGER DEFAULT 1,
    -- attribution
    structure_used    INTEGER,           -- 0/1: was this structure actually submitted?
    realized_pnl      REAL,
    vs_rules_engine_pnl REAL            -- counterfactual: what rules engine would have done
);
CREATE INDEX IF NOT EXISTS idx_strategy_decision ON strategy_journal(decision_id);
CREATE INDEX IF NOT EXISTS idx_strategy_ticker   ON strategy_journal(ticker, decided_at_utc);

-- ── advocate_journal (Phase 5 — schema deployed now, empty) ──
CREATE TABLE IF NOT EXISTS advocate_journal (
    journal_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id          TEXT    NOT NULL,
    ticker               TEXT    NOT NULL,
    decided_at_utc       TEXT    NOT NULL,
    prompt_version       TEXT    NOT NULL,
    model                TEXT    NOT NULL,
    payload_json         TEXT    NOT NULL,
    verdict              TEXT    NOT NULL,   -- 'PASS' | 'CAUTION' | 'BLOCK'
    verdict_confidence   INTEGER,
    failure_modes_json   TEXT    NOT NULL,
    most_likely_scenario TEXT,
    output_full_json     TEXT    NOT NULL,
    input_tokens         INTEGER,
    output_tokens        INTEGER,
    cost_usd             REAL,
    latency_ms           INTEGER,
    shadow_mode          INTEGER DEFAULT 1,
    -- attribution: was advocate right?
    trade_taken          INTEGER,           -- 0/1
    realized_pnl         REAL,
    advocate_was_right   INTEGER            -- 0/1: BLOCK→loss or PASS→win
);
CREATE INDEX IF NOT EXISTS idx_advocate_decision ON advocate_journal(decision_id);
CREATE INDEX IF NOT EXISTS idx_advocate_ticker   ON advocate_journal(ticker, decided_at_utc);
CREATE INDEX IF NOT EXISTS idx_advocate_verdict  ON advocate_journal(verdict, decided_at_utc);

-- ── exit_journal (Phase 6 — schema deployed now, empty) ──────
CREATE TABLE IF NOT EXISTS exit_journal (
    journal_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id          TEXT    NOT NULL,
    position_id          TEXT    NOT NULL,
    ticker               TEXT    NOT NULL,
    decided_at_utc       TEXT    NOT NULL,
    prompt_version       TEXT    NOT NULL,
    model                TEXT    NOT NULL,
    payload_json         TEXT    NOT NULL,
    thesis_validity      TEXT    NOT NULL,   -- 'VALID' | 'WEAKENING' | 'INVALIDATED'
    kill_condition_status TEXT   NOT NULL,
    recommendation       TEXT    NOT NULL,   -- 'HOLD' | 'TIGHTEN_STOP' | 'TAKE_PARTIAL' | 'CLOSE_NOW' | 'ROLL'
    recommendation_reasoning TEXT,
    specific_action_json TEXT,
    confidence_pct       INTEGER,
    output_full_json     TEXT    NOT NULL,
    input_tokens         INTEGER,
    output_tokens        INTEGER,
    cost_usd             REAL,
    latency_ms           INTEGER,
    shadow_mode          INTEGER DEFAULT 1,
    -- attribution
    action_taken         TEXT,              -- what actually happened
    exit_alpha_pct       REAL,             -- counterfactual P&L improvement vs floors-only
    pnl_pct_of_max       REAL,             -- P&L % of max_gain at recommendation time
    outcome_pnl          REAL,             -- actual realized P&L when position closed
    action_quality       TEXT              -- EARLY_EXIT_CORRECT | EARLY_EXIT_WRONG | HOLD_CORRECT | HOLD_WRONG
);
CREATE INDEX IF NOT EXISTS idx_exit_position ON exit_journal(position_id);
CREATE INDEX IF NOT EXISTS idx_exit_decision ON exit_journal(decision_id);

-- ── agent_lessons (Phase 2 foundation; §6.2 + §8.4) ─────────
CREATE TABLE IF NOT EXISTS agent_lessons (
    lesson_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name             TEXT    NOT NULL,   -- 'analyst'|'strategy'|'advocate'|'exit'
    lesson_text            TEXT    NOT NULL,
    derived_from_chain_ids TEXT,              -- JSON array of decision_ids
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
CREATE INDEX IF NOT EXISTS idx_lessons_agent   ON agent_lessons(agent_name, active, human_approved);
CREATE INDEX IF NOT EXISTS idx_lessons_pending ON agent_lessons(human_approved, active);

-- ── calibration_log (Phase 7 — schema deployed now, empty) ───
CREATE TABLE IF NOT EXISTS calibration_log (
    log_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name         TEXT    NOT NULL,
    measured_at_utc    TEXT    NOT NULL,
    sample_window_days INTEGER,
    sample_size        INTEGER,
    brier_score        REAL,
    confidence_bucket  TEXT,
    predicted_win_rate REAL,
    actual_win_rate    REAL,
    calibration_gap    REAL,
    drift_alert        INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_calibration_agent ON calibration_log(agent_name, measured_at_utc);

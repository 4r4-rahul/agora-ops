-- ============================================================
-- Phase 2 — Rollback
-- DROP tables added in 2026_05_phase2_journals.sql
-- WARNING: data loss — backup first
-- ============================================================

DROP TABLE IF EXISTS calibration_log;
DROP TABLE IF EXISTS agent_lessons;
DROP TABLE IF EXISTS exit_journal;
DROP TABLE IF EXISTS advocate_journal;
DROP TABLE IF EXISTS strategy_journal;
DROP TABLE IF EXISTS analyst_journal;

-- Note: decision_chains columns (completed_at_utc, metadata_json) cannot be
-- dropped in SQLite without rebuilding the table. They remain but are benign.

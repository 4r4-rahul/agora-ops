"""
agora/tests/test_attributor_internals.py — two precision-critical internals of the learning loop:

  • _attribute_exit: classifies each exit decision's action_quality (EARLY_EXIT_CORRECT/WRONG,
    HOLD_CORRECT/WRONG) from the realized P&L vs the P&L at recommendation time. This feeds the
    exit-intelligence promotion gate, so a misclassification trains the exit brain on a lie.
  • _purge_fabricated_attribution: NULLs any attribution that does NOT trace to a genuine
    agent-driven close — the continuous regression guard that keeps fiction out of calibration.

Both operate on a sqlite connection, so we drive them with a small in-memory schema.
"""
from __future__ import annotations

import sqlite3

import pytest

from agora.ops.outcome_attributor import _attribute_exit, _purge_fabricated_attribution


# ── _attribute_exit ───────────────────────────────────────────────────────────
def _exit_db():
    c = sqlite3.connect(":memory:")
    c.execute("""CREATE TABLE exit_journal (
        journal_id INTEGER PRIMARY KEY AUTOINCREMENT,
        position_id TEXT, recommendation TEXT, pnl_pct_of_max REAL,
        action_taken TEXT, outcome_pnl REAL, action_quality TEXT)""")
    return c


def _add_exit(c, position_id, recommendation, pnl_pct_of_max=0.0, action_taken=None):
    c.execute(
        "INSERT INTO exit_journal (position_id, recommendation, pnl_pct_of_max, action_taken) "
        "VALUES (?,?,?,?)",
        (position_id, recommendation, pnl_pct_of_max, action_taken),
    )


def _quality(c, position_id):
    return c.execute(
        "SELECT action_quality, outcome_pnl, action_taken FROM exit_journal WHERE position_id=?",
        (position_id,),
    ).fetchone()


class TestAttributeExit:
    def test_close_now_correct_when_pnl_dropped(self):
        # rec at +50% of max_gain (0.5*1000=500); realized only 200 (<500) → exiting was RIGHT
        c = _exit_db()
        _add_exit(c, "p1", "CLOSE_NOW", pnl_pct_of_max=0.5)
        n = _attribute_exit(c, [("p1", "AAPL", "2026-01-01", 200.0, 700.0, 1000.0)])
        q = _quality(c, "p1")
        assert n == 1
        assert q[0] == "EARLY_EXIT_CORRECT"
        assert q[1] == 200.0 and q[2] == "close_confirmed"

    def test_close_now_wrong_when_left_money(self):
        # realized 600 >= 0.5*1000=500 → closing early LEFT money on the table → WRONG
        c = _exit_db()
        _add_exit(c, "p2", "CLOSE_NOW", pnl_pct_of_max=0.5)
        _attribute_exit(c, [("p2", "AAPL", "2026-01-01", 600.0, 700.0, 1000.0)])
        assert _quality(c, "p2")[0] == "EARLY_EXIT_WRONG"

    def test_hold_correct_when_profitable(self):
        c = _exit_db()
        _add_exit(c, "p3", "HOLD")
        _attribute_exit(c, [("p3", "AAPL", "2026-01-01", 150.0, 700.0, 1000.0)])
        assert _quality(c, "p3")[0] == "HOLD_CORRECT"

    def test_hold_wrong_when_loss(self):
        c = _exit_db()
        _add_exit(c, "p4", "HOLD")
        _attribute_exit(c, [("p4", "AAPL", "2026-01-01", -80.0, 700.0, 1000.0)])
        assert _quality(c, "p4")[0] == "HOLD_WRONG"

    @pytest.mark.parametrize("rec", ["TIGHTEN_STOP", "TAKE_PARTIAL", "ROLL"])
    def test_other_recs_treated_as_hold(self, rec):
        c = _exit_db()
        _add_exit(c, "p5", rec)
        _attribute_exit(c, [("p5", "AAPL", "2026-01-01", 50.0, 700.0, 1000.0)])
        assert _quality(c, "p5")[0] == "HOLD_CORRECT"   # profitable → HOLD_CORRECT

    def test_already_attributed_rows_skipped(self):
        c = _exit_db()
        _add_exit(c, "p6", "HOLD", action_taken="close_confirmed")   # already done
        n = _attribute_exit(c, [("p6", "AAPL", "2026-01-01", 100.0, 700.0, 1000.0)])
        assert n == 0   # nothing re-attributed

    def test_multiple_rows_same_position(self):
        c = _exit_db()
        _add_exit(c, "p7", "HOLD")
        _add_exit(c, "p7", "HOLD")
        n = _attribute_exit(c, [("p7", "AAPL", "2026-01-01", 10.0, 700.0, 1000.0)])
        assert n == 2

    def test_none_realized_pnl_writes_null_outcome(self):
        c = _exit_db()
        _add_exit(c, "p8", "HOLD")
        _attribute_exit(c, [("p8", "AAPL", "2026-01-01", None, 700.0, 1000.0)])
        q = _quality(c, "p8")
        assert q[1] is None and q[0] == "HOLD_WRONG"   # None → treated as <=0


# ── _purge_fabricated_attribution ─────────────────────────────────────────────
def _purge_db(with_shadow=False):
    c = sqlite3.connect(":memory:")
    c.execute("""CREATE TABLE analyst_journal (
        decision_id TEXT, thesis_played_out INTEGER,
        magnitude_realized_pct REAL, confidence_was_calibrated INTEGER)""")
    c.execute("CREATE TABLE strategy_journal (decision_id TEXT, structure_used INTEGER, realized_pnl REAL)")
    c.execute("""CREATE TABLE advocate_journal (
        decision_id TEXT, trade_taken INTEGER, realized_pnl REAL, advocate_was_right INTEGER)""")
    c.execute("CREATE TABLE decision_chains (chain_id TEXT, position_id TEXT)")
    c.execute("""CREATE TABLE positions (
        position_id TEXT, status TEXT, close_date TEXT, close_source TEXT,
        regime_at_entry TEXT DEFAULT 'neutral')""")
    if with_shadow:
        c.execute("""CREATE TABLE shadow_book (
            decision_id TEXT, evaluated INTEGER, hypothetical_win INTEGER)""")
    return c


def _real_close(c, chain_id, position_id):
    """Link a decision_id to a genuinely-closed position so _REAL_CLOSED_CHAINS includes it."""
    c.execute("INSERT INTO decision_chains VALUES (?,?)", (chain_id, position_id))
    c.execute("INSERT INTO positions (position_id, status, close_date, close_source) "
              "VALUES (?, 'closed', '2026-01-02', 'thesis_exit')",
              (position_id,))


class TestPurgeFabricatedAttribution:
    def test_real_close_attribution_is_kept(self):
        c = _purge_db()
        _real_close(c, "chain-1", "pos-1")
        c.execute("INSERT INTO analyst_journal VALUES ('chain-1', 1, 12.0, 1)")
        purged = _purge_fabricated_attribution(c)
        assert purged["analyst"] == 0
        # the attribution survives
        assert c.execute("SELECT thesis_played_out FROM analyst_journal").fetchone()[0] == 1

    def test_untraced_attribution_is_purged(self):
        c = _purge_db()
        # attributed but decision_id traces to NO real closed chain → fabricated
        c.execute("INSERT INTO analyst_journal VALUES ('ghost', 1, 9.0, 1)")
        c.execute("INSERT INTO strategy_journal VALUES ('ghost', 1, 120.0)")
        purged = _purge_fabricated_attribution(c)
        assert purged["analyst"] == 1 and purged["strategy"] == 1
        assert c.execute("SELECT thesis_played_out FROM analyst_journal").fetchone()[0] is None
        assert c.execute("SELECT structure_used FROM strategy_journal").fetchone()[0] is None

    def test_open_position_does_not_count_as_real_close(self):
        c = _purge_db()
        # chain links to a position that is OPEN (not a real close) → attribution must be purged
        c.execute("INSERT INTO decision_chains VALUES ('c2', 'pos-open')")
        c.execute("INSERT INTO positions (position_id, status, close_date, close_source) "
                  "VALUES ('pos-open', 'open', NULL, NULL)")
        c.execute("INSERT INTO analyst_journal VALUES ('c2', 1, 5.0, 0)")
        assert _purge_fabricated_attribution(c)["analyst"] == 1

    def test_sync_artifact_not_a_real_close(self):
        c = _purge_db()
        c.execute("INSERT INTO decision_chains VALUES ('c3', 'pos-sync')")
        c.execute("INSERT INTO positions (position_id, status, close_date, close_source) "
                  "VALUES ('pos-sync', 'closed', '2026-01-02', 'tws_startup_sync')")
        c.execute("INSERT INTO analyst_journal VALUES ('c3', 1, 5.0, 0)")
        assert _purge_fabricated_attribution(c)["analyst"] == 1

    def test_idempotent_second_pass_purges_nothing(self):
        c = _purge_db()
        c.execute("INSERT INTO analyst_journal VALUES ('ghost', 1, 9.0, 1)")
        _purge_fabricated_attribution(c)
        assert _purge_fabricated_attribution(c)["analyst"] == 0   # already clean

    def test_advocate_shadow_block_is_spared(self):
        c = _purge_db(with_shadow=True)
        # A BLOCK scored from the shadow book has NO real position by construction — must be kept.
        c.execute("INSERT INTO advocate_journal VALUES ('blk', 1, -90.0, 1)")
        c.execute("INSERT INTO shadow_book VALUES ('blk', 1, 0)")
        purged = _purge_fabricated_attribution(c)
        assert purged["advocate"] == 0
        assert c.execute("SELECT trade_taken FROM advocate_journal").fetchone()[0] == 1

    def test_advocate_untraced_without_shadow_is_purged(self):
        c = _purge_db(with_shadow=True)
        c.execute("INSERT INTO advocate_journal VALUES ('ghost-adv', 1, 50.0, 0)")
        assert _purge_fabricated_attribution(c)["advocate"] == 1

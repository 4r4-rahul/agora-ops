"""
test_long_thesis_recovery.py — entry-journal recovery for long positions.

Root issue: only ~1% of long_journal 'proceed' rows ever get position_id linked (the fill-record
write rarely runs; orphan-then-adopted positions never carry it), so the exit brain + signal-stats
loop — both keyed on position_id — silently fail for ~99% of longs (e.g. the unmanaged TSM -$479).
The thesis EXISTS in long_journal; find_entry_journal recovers it by ticker+strike+expiry.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from agora.ops.db_migrations import run_all
from agora.agents.long_options_agent import LongOptionsAgent as L


def _seed(db, *, position_id="", ticker="TSM", strike=460.0, expiry="2026-07-10",
          direction="bullish", signals='{"flow":"bullish+2","momentum":"bullish"}'):
    run_all(db)
    c = sqlite3.connect(db)
    c.execute(
        """INSERT INTO long_journal (position_id,ticker,strategy,direction,decided_at_utc,strike,
              expiry,dte,delta_approx,premium_per_sh,ivr,vix,regime,flow_direction,momentum_score,
              conviction_score,signal_stack,outcome,block_reason,max_loss_dollars,max_gain_dollars,
              contracts,is_itm,dte_reason)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'proceed','',?,?,?,?,?)""",
        (position_id, ticker, "long_call", direction, "2026-06-15T12:00:00Z", strike, expiry,
         25, 0.55, 14.0, 40.0, 18.0, "risk_on", "bullish", 0.6, 3, signals, 1400.0, 700.0, 1, 0, "4factor"),
    )
    c.commit(); c.close()


def test_fallback_recovers_unlinked_thesis(tmp_path):
    """position_id='' (the 99% case) → fast path misses, structure match recovers it."""
    db = str(tmp_path / "j.db")
    _seed(db, position_id="")               # unlinked, like an orphan-adopted position
    # fast path by a (wrong/adopt) position_id finds nothing on its own...
    assert L.find_entry_journal(db, "adopt-xyz") is None
    # ...but the structure fallback recovers the real thesis
    row = L.find_entry_journal(db, "adopt-xyz", "TSM", 460.0, "2026-07-10")
    assert row is not None and row["direction"] == "bullish" and row["conviction_score"] == 3


def test_fast_path_by_position_id(tmp_path):
    db = str(tmp_path / "j.db")
    _seed(db, position_id="pos-1")
    row = L.find_entry_journal(db, "pos-1")
    assert row is not None and row["ticker"] == "TSM"


def test_link_position_id_updates_not_duplicates(tmp_path):
    """link_position_id stamps the id onto the existing row (no second/duplicate row)."""
    db = str(tmp_path / "j.db")
    _seed(db, position_id="")
    assert L.link_position_id(db, "pos-9", "TSM", 460.0, "2026-07-10") is True
    c = sqlite3.connect(db)
    n = c.execute("SELECT COUNT(*) FROM long_journal WHERE ticker='TSM' AND outcome='proceed'").fetchone()[0]
    linked = c.execute("SELECT position_id FROM long_journal WHERE ticker='TSM'").fetchone()[0]
    assert n == 1                            # UPDATE, not a duplicate INSERT
    assert linked == "pos-9"
    # now the fast path resolves it
    assert L.find_entry_journal(db, "pos-9") is not None


def test_update_signal_stats_via_structure(tmp_path):
    """signal-stats learning loop recovers the signal_stack by structure too (not just pos_id)."""
    db = str(tmp_path / "j.db")
    _seed(db, position_id="")               # unlinked
    L.update_signal_stats(db, "adopt-xyz", realized_pnl=250.0,
                          ticker="TSM", strike=460.0, expiry="2026-07-10")
    c = sqlite3.connect(db)
    rows = dict(c.execute("SELECT signal_name, wins FROM signal_stats").fetchall())
    assert rows.get("flow") == 1 and rows.get("momentum") == 1   # win recorded for each signal


def test_no_entry_returns_none(tmp_path):
    db = str(tmp_path / "j.db")
    run_all(db)
    assert L.find_entry_journal(db, "nope", "ZZZ", 1.0, "2026-01-01") is None

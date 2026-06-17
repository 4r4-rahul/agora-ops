"""
test_signal_attribution.py — signal_stats must DIFFERENTIATE signals.

Learning-loop bug (lesson-confirmed): the stack carries all 6 signals every trade, and
update_signal_stats credited every key — so all six ended up byte-for-byte identical
(n=21 8W/13L) and the calibration loop learned nothing. Now only signals that genuinely
FIRED in the trade's direction are credited.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile

import pytest

from agora.ops.db_migrations import run_all
from agora.agents.long_options_agent import LongOptionsAgent as L, _signal_fired_in_dir as fired


def test_fired_detection():
    assert fired("bullish+1(sweep)", "bullish") is True
    assert fired("outperform+1", "bullish") is True
    assert fired("surge", "bullish") is True
    assert fired("neutral(RSI=48)", "bullish") is False    # didn't fire
    assert fired("none", "bullish") is False
    assert fired("normal", "bullish") is False
    assert fired("bearish(RSI=29)", "bullish") is False     # counter-signal, not credited
    assert fired("bearish+1", "bearish") is True
    assert fired("bullish+1", "bearish") is False           # counter on a bearish trade


def _seed(db, stack: dict, direction="bullish"):
    run_all(db)
    c = sqlite3.connect(db)
    c.execute(
        "INSERT INTO long_journal (position_id,ticker,strategy,direction,decided_at_utc,strike,"
        "expiry,signal_stack,outcome,is_itm,dte_reason,conviction_score) "
        "VALUES ('p1','NVDA','long_call',?,'2026-06-17',100,'2026-07-17',?,'proceed',0,'x',3)",
        (direction, json.dumps(stack)))
    c.commit()


def test_only_firing_signals_credited(tmp_path):
    db = str(tmp_path / "t.db")
    _seed(db, {"flow": "bullish+1(sweep)", "momentum": "neutral(RSI=48)", "rel_strength": "neutral",
               "vol_surge": "normal", "news": "none", "gex": "neutral", "macro": "neutral"})
    L.update_signal_stats(db, "p1", realized_pnl=200.0, ticker="NVDA", strike=100.0, expiry="2026-07-17")
    rows = sqlite3.connect(db).execute(
        "SELECT signal_name, wins, losses FROM signal_stats ORDER BY signal_name").fetchall()
    assert rows == [("flow", 1, 0)]            # ONLY the signal that fired


def test_multiple_fired_signals_each_credited(tmp_path):
    db = str(tmp_path / "t.db")
    _seed(db, {"flow": "bullish+1", "momentum": "bullish(RSI=63)", "news": "none",
               "vol_surge": "surge", "gex": "neutral"})
    L.update_signal_stats(db, "p1", realized_pnl=-50.0, ticker="NVDA", strike=100.0, expiry="2026-07-17")
    rows = dict((n, (w, l)) for n, w, l in sqlite3.connect(db).execute(
        "SELECT signal_name, wins, losses FROM signal_stats").fetchall())
    assert set(rows) == {"flow", "momentum", "vol_surge"}      # the 3 that fired; news/gex skipped
    assert rows["flow"] == (0, 1)                              # a loss recorded

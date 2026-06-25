"""
agora/tests/test_entry_funnel.py — per-ticker entry-funnel observability (read-only).

Mock-DB coverage: monitor-eval upsert/accumulation (counts, MAX move/vol, promotion), gate-outcome
upsert, the derived funnel stage + diagnostic ordering (the AAOI symptom surfaces first), and
error-safety (every IO path never raises). All against a temp sqlite — no engine, no network.
"""
from __future__ import annotations

import sqlite3
import tempfile

from agora.ops.entry_funnel import funnel_summary, record_gate_outcome, record_monitor_evals

DAY = "2026-06-25"


def _db():
    return tempfile.NamedTemporaryFile(suffix=".db", delete=False).name


def _row(db, ticker):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    r = c.execute("SELECT * FROM entry_funnel WHERE ticker=?", (ticker,)).fetchone()
    c.close()
    return dict(r) if r else None


# ── monitor evals: upsert + accumulation ───────────────────────────────────────────────
def test_records_eval_and_counts():
    db = _db()
    n = record_monitor_evals(db, [
        {"ticker": "AAOI", "move_pct": 1.2, "vol_ratio": 1.5, "promoted": False},
        {"ticker": "SPY", "move_pct": 0.3, "vol_ratio": 1.1, "promoted": False},
    ], day=DAY)
    assert n == 2
    a = _row(db, "AAOI")
    assert a["evaluated_n"] == 1 and a["max_move_pct"] == 1.2 and a["promoted_n"] == 0


def test_accumulates_across_cycles_keeping_max():
    db = _db()
    record_monitor_evals(db, [{"ticker": "AAOI", "move_pct": 1.2, "vol_ratio": 1.5, "promoted": False}], day=DAY)
    record_monitor_evals(db, [{"ticker": "AAOI", "move_pct": 0.6, "vol_ratio": 3.0, "promoted": False}], day=DAY)
    a = _row(db, "AAOI")
    assert a["evaluated_n"] == 2                       # two cycles
    assert a["max_move_pct"] == 1.2                    # keeps the LARGER move (not the latest)
    assert a["max_vol_ratio"] == 3.0                   # keeps the larger vol ratio


def test_promotion_bumps_and_records_trigger():
    db = _db()
    record_monitor_evals(db, [{"ticker": "TSLA", "move_pct": 2.1, "vol_ratio": 1.2,
                               "promoted": True, "trigger": "move +2.1% in 30m"}], day=DAY)
    t = _row(db, "TSLA")
    assert t["promoted_n"] == 1 and t["last_trigger"] == "move +2.1% in 30m"


def test_move_pct_abs_valued():
    db = _db()
    record_monitor_evals(db, [{"ticker": "X", "move_pct": -3.4, "vol_ratio": 1.0, "promoted": False}], day=DAY)
    assert _row(db, "X")["max_move_pct"] == 3.4        # magnitude, not signed


def test_blank_and_garbage_tickers_skipped():
    db = _db()
    n = record_monitor_evals(db, [
        {"ticker": "", "move_pct": 5.0, "promoted": False},
        {"ticker": None, "move_pct": 5.0, "promoted": False},
        {"ticker": "ok", "move_pct": 1.0, "promoted": False},
    ], day=DAY)
    assert n == 1 and _row(db, "OK") is not None       # only the valid one, upper-cased


# ── gate outcomes ───────────────────────────────────────────────────────────────────────
def test_gate_outcome_upserts_onto_existing_row():
    db = _db()
    record_monitor_evals(db, [{"ticker": "NVDA", "move_pct": 1.8, "vol_ratio": 1.0,
                               "promoted": True, "trigger": "t"}], day=DAY)
    assert record_gate_outcome(db, "NVDA", "rejected: R/R 0.8<1.0", day=DAY)
    r = _row(db, "NVDA")
    assert r["gate_n"] == 1 and "R/R" in r["gate_outcome"] and r["promoted_n"] == 1  # preserves prior


def test_gate_outcome_creates_row_if_absent():
    db = _db()
    record_gate_outcome(db, "ZZZ", "passed->order", day=DAY)
    assert _row(db, "ZZZ")["gate_outcome"] == "passed->order"


# ── funnel summary: derived stage + diagnostic ordering ─────────────────────────────────
def test_summary_stages_and_ordering():
    db = _db()
    # AAOI: big move, never promoted (the symptom) ; NVDA: promoted + gated ; KO: promoted only
    record_monitor_evals(db, [
        {"ticker": "AAOI", "move_pct": 4.0, "vol_ratio": 1.0, "promoted": False},
        {"ticker": "NVDA", "move_pct": 2.0, "vol_ratio": 1.0, "promoted": True, "trigger": "t"},
        {"ticker": "KO", "move_pct": 1.6, "vol_ratio": 1.0, "promoted": True, "trigger": "t"},
    ], day=DAY)
    record_gate_outcome(db, "NVDA", "rejected: low conviction", day=DAY)
    s = funnel_summary(db, day=DAY)
    by = {r["ticker"]: r for r in s}
    assert by["AAOI"]["stage"] == "never_promoted"
    assert by["KO"]["stage"] == "promoted_only"
    assert by["NVDA"]["stage"] == "reached_gates"
    # diagnostic ordering: the high-move never-promoted name (AAOI) comes first
    assert s[0]["ticker"] == "AAOI"


def test_summary_limit_and_day_isolation():
    db = _db()
    record_monitor_evals(db, [{"ticker": "A", "move_pct": 1.0, "vol_ratio": 1.0, "promoted": False}], day="2026-06-24")
    record_monitor_evals(db, [{"ticker": "B", "move_pct": 1.0, "vol_ratio": 1.0, "promoted": False}], day=DAY)
    assert [r["ticker"] for r in funnel_summary(db, day=DAY)] == ["B"]   # only today's bucket
    assert len(funnel_summary(db, day=DAY, limit=0)) == 0


# ── error safety ────────────────────────────────────────────────────────────────────────
def test_all_paths_error_safe():
    assert record_monitor_evals("/no/such/x.db", [{"ticker": "A", "move_pct": 1.0}]) == 0
    assert record_gate_outcome("/no/such/x.db", "A", "x") is False
    assert funnel_summary("/no/such/x.db") == []
    assert record_monitor_evals(_db(), []) == 0        # empty batch is a no-op, not an error

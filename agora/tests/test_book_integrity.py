"""
agora/tests/test_book_integrity.py — smoke + mock tests that keep FICTION out of the book for good.

Two fiction sources were found on 2026-06-25 and fixed at the source:
  1. the reconciler's contracts² units bug (adopted entry_price carried the contract count) — fixed so
     _adopt_group writes a PER-SHARE entry_price;
  2. bad close fills booking out-of-bounds realized — clamped at the _close_position booking guard.

These tests lock both down:
  • SMOKE  — scan_impossible_pnl is empty on a clean book, catches a planted impossible row, and the live
    .agora DB (if present) carries no fiction.
  • MOCK   — the reconciler, fed a 59-contract IBKR leg, can NEVER reproduce the contracts² blow-up; the
    resulting position's worst-case close is bounded by its own defined risk.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import types

import pytest

from agora.core.pnl import pnl_within_bounds, realized_pnl
from agora.ops.book_integrity import clean_book_fiction, scan_impossible_pnl


def _book(rows):
    """rows: (position_id, status, realized_pnl, max_loss, max_gain, regime, contracts)."""
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    c = sqlite3.connect(db)
    c.execute(
        """CREATE TABLE positions (position_id TEXT, ticker TEXT DEFAULT 'TST', status TEXT,
           realized_pnl REAL NOT NULL DEFAULT 0, unrealized_pnl REAL NOT NULL DEFAULT 0, close_price REAL,
           peak_unrealized_pnl REAL, trough_unrealized_pnl REAL, entry_price REAL NOT NULL DEFAULT 1.0,
           max_loss_dollars REAL, max_gain_dollars REAL, regime_at_entry TEXT, contracts INTEGER)"""
    )
    c.executemany(
        "INSERT INTO positions (position_id, status, realized_pnl, max_loss_dollars, max_gain_dollars, "
        "regime_at_entry, contracts) VALUES (?,?,?,?,?,?,?)", rows)
    c.commit(); c.close()
    return db


# ── SMOKE: the "no fiction in the book" invariant ────────────────────────────────────────
def test_scan_catches_impossible_then_clean_makes_book_scan_empty():
    db = _book([
        ("eng-ok",   "closed",   -200.0,    500.0,  150.0, "neutral", 1),    # within bounds
        ("eng-bad",  "closed",  -5000.0,    500.0,  150.0, "risk_off", 1),   # impossible loss (bad fill)
        ("win-ok",   "closed",    140.0,    500.0,  150.0, "neutral", 1),    # big but legit winner
        ("adopt-x",  "closed", -808831.0, 11549.0, 34647.0, "adopted", 59),  # adopted contracts² fiction
    ])
    flagged = {b["position_id"] for b in scan_impossible_pnl(db)}
    assert flagged == {"eng-bad", "adopt-x"}            # only true bounds-violations, not the legit winner

    res = clean_book_fiction(db)
    assert res["adopted_cleaned"] == 1 and res["engine_clamped"] == 1
    assert scan_impossible_pnl(db) == []               # SMOKE: book is clean after restatement (idempotent)
    assert scan_impossible_pnl(db) == []               # second run still clean

    c = sqlite3.connect(db)
    assert c.execute("SELECT realized_pnl FROM positions WHERE position_id='eng-bad'").fetchone()[0] == -500.0
    assert c.execute("SELECT realized_pnl FROM positions WHERE position_id='adopt-x'").fetchone()[0] == 0.0
    # adopted entry_price recovered to per-share = 11549 / 100 / 59 ≈ 1.9575 (NOT 115.49)
    ep = c.execute("SELECT entry_price FROM positions WHERE position_id='adopt-x'").fetchone()[0]
    assert abs(ep - 1.9575) < 0.01


def test_legit_winner_is_never_clamped():
    db = _book([("win", "closed", 1085.0, 345.0, 1155.0, "risk_off", 1)])   # within max_gain → not fiction
    assert scan_impossible_pnl(db) == []
    assert clean_book_fiction(db)["engine_clamped"] == 0


def test_live_db_carries_no_fiction():
    """True smoke test against the live book (skips when absent, e.g. in CI)."""
    db = os.path.join(".agora", "agora.db")
    if not os.path.exists(db):
        pytest.skip("no live .agora/agora.db")
    fiction = scan_impossible_pnl(db)
    assert fiction == [], f"the live book has {len(fiction)} impossible-P&L rows: {fiction[:3]}"


# ── MOCK: the reconciler can never reproduce the contracts² corruption ───────────────────
def test_adopt_writes_per_share_entry_so_pnl_is_bounded():
    from agora.core.models import (
        OpenPosition,
        PositionStatus,
        SpreadLeg,
        StrategyPillar,
        StrategyType,
    )
    from agora.ops.position_reconciler import _adopt_group

    captured: dict = {}
    pm = types.SimpleNamespace(
        add_position=lambda p: captured.__setitem__("pos", p),
        _settings=types.SimpleNamespace(max_contracts_per_trade=10),
    )
    # A 59-contract long DIA put. IBKR avgCost is per-share×100, so 195.75 → $1.9575/share.
    legs = [{"right": "P", "strike": 440.0, "ibkr_qty": 59}]
    detailed = {("DIA", "P", 440.0, "20260717"): (59, 195.75)}

    ok = _adopt_group(pm, "DIA", "20260717", legs, detailed,
                      OpenPosition, SpreadLeg, StrategyType, StrategyPillar, PositionStatus)
    assert ok
    pos = captured["pos"]

    # entry_price is PER-SHARE (~$1.96) — NOT 1.9575×59 = 115.49 (the contracts² bug)
    assert abs(pos.entry_price - 1.9575) < 0.02
    assert pos.contracts == 59
    assert abs(pos.max_loss_dollars - 11549.25) < 1.0          # = 1.9575 × 100 × 59

    # The decisive property: realized P&L for ANY close is bounded by the position's own defined risk,
    # so pnl_within_bounds can never trip. Worst case = closes worthless.
    worst = realized_pnl(pos.entry_price, 0.0, pos.contracts)
    assert pnl_within_bounds(worst, pos.max_loss_dollars, pos.max_gain_dollars)
    assert abs(worst) < 12_000                                 # ≈ −$11.5k, NOT −$808k


# ── INTEGRITY GUARD: no REAL trade silently DROPPED (the 2026-06-29 CBOE +$660 'time_stop' bug) ──────
def _book_with_sources(rows):
    """rows: (status, close_source, regime, realized_pnl). Minimal schema for canonical_book."""
    import tempfile as _tf
    db = _tf.NamedTemporaryFile(suffix=".db", delete=False).name
    c = sqlite3.connect(db)
    c.execute("""CREATE TABLE positions (position_id TEXT DEFAULT 'p', ticker TEXT DEFAULT 'TST',
        status TEXT, close_source TEXT, close_date TEXT, regime_at_entry TEXT,
        realized_pnl REAL NOT NULL DEFAULT 0, unrealized_pnl REAL NOT NULL DEFAULT 0)""")
    c.executemany("INSERT INTO positions (status, close_source, close_date, regime_at_entry, realized_pnl) "
                  "VALUES (?,?,'2026-06-29',?,?)", rows)
    c.commit(); c.close()
    return db


def test_integrity_ok_when_every_source_classified():
    from agora.ops.book_manager import canonical_book
    db = _book_with_sources([
        ("closed", "time_stop",     "risk_off", 660.0),   # real (the bug source — must classify)
        ("closed", "profit_target", "risk_off", 326.0),   # real
        ("closed", "lifecycle",     "neutral",  100.0),   # real
        ("closed", "reconcile_ghost","neutral",   0.0),   # known fiction
        ("closed", "fabricated_unfilled","neutral",0.0),  # known fiction
        ("closed", "lifecycle",     "adopted",  999.0),   # adopted (known)
    ])
    b = canonical_book(db)
    assert b["integrity"]["ok"] is True
    assert b["integrity"]["unclassified_sources"] == []
    os.unlink(db)


def test_integrity_flags_unclassified_source_loudly():
    """A close_source that is neither real nor known-fiction must flip integrity.ok=False and be named —
    so a future 'time_stop'-style omission can NEVER hide a real trade in 'other_excluded' again."""
    from agora.ops.book_manager import canonical_book
    db = _book_with_sources([
        ("closed", "lifecycle",   "neutral", 100.0),   # real
        ("closed", "mystery_stop","risk_off", 540.0),  # UNCLASSIFIED — likely a real trade being dropped
    ])
    b = canonical_book(db)
    assert b["integrity"]["ok"] is False
    srcs = {u["close_source"] for u in b["integrity"]["unclassified_sources"]}
    assert "mystery_stop" in srcs
    assert "mystery_stop" in b["integrity"]["message"]
    os.unlink(db)


# ── EXPANDED INTEGRITY GUARD (SME review 2026-06-29): both directions + corruption signals ───────────
def _full_book(rows):
    """rows: dicts with keys status, close_source, regime, realized_pnl, unrealized_pnl,
    max_loss_dollars, max_gain_dollars. Full schema for canonical_book()._integrity."""
    import tempfile as _tf
    db = _tf.NamedTemporaryFile(suffix=".db", delete=False).name
    c = sqlite3.connect(db)
    c.execute("""CREATE TABLE positions (position_id TEXT DEFAULT 'p', ticker TEXT DEFAULT 'TST',
        status TEXT, close_source TEXT, close_date TEXT, regime_at_entry TEXT, contracts INTEGER DEFAULT 1,
        realized_pnl REAL NOT NULL DEFAULT 0, unrealized_pnl REAL NOT NULL DEFAULT 0,
        max_loss_dollars REAL, max_gain_dollars REAL)""")
    for i, r in enumerate(rows):
        c.execute("INSERT INTO positions (position_id,status,close_source,close_date,regime_at_entry,"
                  "realized_pnl,unrealized_pnl,max_loss_dollars,max_gain_dollars) VALUES (?,?,?,?,?,?,?,?,?)",
                  (f"p{i}", r["status"], r.get("close_source"),
                   "2026-06-29" if r["status"] == "closed" else None, r.get("regime"),
                   r.get("realized_pnl", 0.0), r.get("unrealized_pnl", 0.0),
                   r.get("max_loss_dollars", 500.0), r.get("max_gain_dollars", 500.0)))
    c.commit(); c.close()
    return db


def test_integrity_flags_impossible_pnl_as_corruption():
    """A closed row whose realized P&L is outside [-max_loss,+max_gain]*1.2 is the −$808k contracts²
    signature — must flip integrity.ok False even under a KNOWN (real) close_source."""
    from agora.ops.book_manager import canonical_book
    db = _full_book([
        {"status": "closed", "close_source": "lifecycle", "regime": "neutral",
         "realized_pnl": -8000.0, "max_loss_dollars": 500.0, "max_gain_dollars": 500.0},  # impossible loss
    ])
    b = canonical_book(db)
    assert b["integrity"]["ok"] is False
    assert b["integrity"]["impossible_pnl"], "the −8000 vs 500 max-loss row must be flagged"
    os.unlink(db)


def test_integrity_nonzero_fiction_is_visibility_not_failure():
    """A within-bounds fiction-labeled close (tws_startup_sync −$50) is surfaced for visibility but must
    NOT flip ok — known/quarantined artifacts can't cry wolf forever (the −$373 live case)."""
    from agora.ops.book_manager import canonical_book
    db = _full_book([
        {"status": "closed", "close_source": "lifecycle", "regime": "neutral", "realized_pnl": 100.0},
        {"status": "closed", "close_source": "tws_startup_sync", "regime": "", "realized_pnl": -50.0},
    ])
    b = canonical_book(db)
    assert b["integrity"]["ok"] is True
    assert b["integrity"]["nonzero_fiction"]["n"] == 1 and b["integrity"]["nonzero_fiction"]["sum"] == -50.0
    os.unlink(db)


def test_adopted_open_excluded_from_real_open_unrealized():
    """P0-2: an adopted OPEN must NOT inflate real_strategy.open_unrealized (reconstructed basis)."""
    from agora.ops.book_manager import canonical_book
    db = _full_book([
        {"status": "open", "regime": "neutral", "unrealized_pnl": -300.0},
        {"status": "open", "regime": "adopted", "unrealized_pnl": 216.16},   # must be excluded
    ])
    b = canonical_book(db)
    assert b["real_strategy"]["open_unrealized"] == -300.0       # NOT -83.84
    assert b["real_strategy"]["open_positions"] == 1            # adopted not counted
    assert b["integrity"]["adopted_open_unrealized"]["sum"] == 216.16   # surfaced separately
    os.unlink(db)


def test_adopted_with_real_source_buckets_as_adopted_not_real():
    """P1-3: a row that is adopted AND carries a real close_source must land in adopted_legacy, never
    in real strategy P&L (the load-bearing CASE order / _REAL_CLOSE adopted-exclusion)."""
    from agora.ops.book_manager import canonical_book
    db = _full_book([
        {"status": "closed", "close_source": "lifecycle", "regime": "adopted", "realized_pnl": 999.0},
    ])
    b = canonical_book(db)
    assert b["real_strategy"]["net_realized"] == 0.0           # the +999 is NOT real
    assert b["excluded"]["adopted_legacy"]["pnl"] == 999.0     # it is adopted_legacy
    assert b["partition_ok"] is True
    os.unlink(db)

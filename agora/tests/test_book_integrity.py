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

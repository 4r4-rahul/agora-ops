"""
agora/ops/book_integrity.py — the "no fiction in the book" invariant: scan for, and clean, P&L that is
mathematically impossible for a defined-risk position.

The invariant (agora.core.pnl.pnl_within_bounds): a defined-risk position's realized P&L can NEVER exceed
its own max-loss (down) or max-gain (up). Anything outside is FICTION, and we have seen two sources:
  1. corrupted cost basis — the 2026-06-25 adopted-position contracts² units bug (entry_price carried the
     contract count, so realized_pnl scaled by contracts TWICE → DIA 59x booked −$808k on $11.5k risk);
  2. a bad close fill — a leg mispriced at close (AAPL 1x booked +$1,085 on a $345-risk spread).

`scan_impossible_pnl` is the canonical check the smoke test asserts stays empty (and an ops job can alert
on). `clean_book_fiction` restates existing corruption IN PLACE. New corruption is blocked at the source:
position_reconciler now writes a per-share entry_price, and _close_position clamps any out-of-bounds
realized via pnl_within_bounds. Read-only except clean_book_fiction; neither raises on an absent column.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from agora.core.pnl import pnl_within_bounds


def scan_impossible_pnl(db_path: str, *, tol: float = 1.2) -> list[dict[str, Any]]:
    """Every CLOSED position whose booked realized_pnl violates its own defined-risk bounds.

    Empty list == the book is clean. NULL realized (untrustworthy/uncomputed) is not counted as fiction.
    """
    out: list[dict[str, Any]] = []
    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT position_id, ticker, contracts, realized_pnl, max_loss_dollars, "
                "max_gain_dollars, COALESCE(regime_at_entry,'') AS regime "
                "FROM positions WHERE status='closed' AND realized_pnl IS NOT NULL"
            ).fetchall()
    except Exception as exc:   # pragma: no cover - defensive
        return [{"error": str(exc)}]
    for r in rows:
        ml = abs(r["max_loss_dollars"] or 0.0)
        mg = abs(r["max_gain_dollars"] or 0.0)
        if (ml or mg) and not pnl_within_bounds(r["realized_pnl"], ml, mg, tol=tol):
            out.append({k: r[k] for k in r.keys()})
    return out


def clean_book_fiction(db_path: str) -> dict[str, Any]:
    """Restate existing fiction IN PLACE (idempotent). Run the feature-store rebuild AFTER this.

      • ADOPTED positions (broker artifacts the engine never priced): recover a per-share entry_price from
        the dollar risk (max_loss / 100 / contracts) so the row is internally consistent, then zero/NULL
        the money marks. Their true P&L is unknowable, but realized_pnl/unrealized_pnl are NOT NULL columns
        so they get 0.0 (a neutral, non-fictional placeholder) while the nullable close/peak/trough get
        NULL. The _REAL_CLOSE predicate already keeps adopted out of the engine books and ML, so this 0.0
        is never read by any P&L/ML consumer — it exists only so the raw row carries no impossible number.
      • NON-ADOPTED engine closes with impossible realized (a bad close fill): the entry is trustworthy, so
        clamp realized to the violated defined-risk bound (+max_gain / −max_loss). If that bound is unknown
        (0/NULL on the violated side), fall back to 0.0 rather than fabricate a clamp.

    Returns {adopted_cleaned, engine_clamped, engine_zeroed, details:[...]}.
    """
    summary: dict[str, Any] = {"adopted_cleaned": 0, "engine_clamped": 0, "engine_zeroed": 0, "details": []}
    with sqlite3.connect(db_path, timeout=10) as conn:
        conn.row_factory = sqlite3.Row

        # 1. adopted — per-share entry_price + neutralised money marks (NOT NULL cols → 0.0)
        for r in conn.execute(
            "SELECT position_id, contracts, max_loss_dollars FROM positions "
            "WHERE COALESCE(regime_at_entry,'')='adopted'"
        ).fetchall():
            ctr = max(1, int(r["contracts"] or 1))
            ml = abs(r["max_loss_dollars"] or 0.0)
            per_share = round(ml / (100.0 * ctr), 4) if ml else 0.01
            conn.execute(
                "UPDATE positions SET entry_price=?, realized_pnl=0.0, unrealized_pnl=0.0, "
                "close_price=NULL, peak_unrealized_pnl=NULL, trough_unrealized_pnl=NULL "
                "WHERE position_id=?",
                (per_share, r["position_id"]),
            )
            summary["adopted_cleaned"] += 1

        # 2. non-adopted engine closes with impossible realized — clamp to the violated bound (or 0.0)
        for r in conn.execute(
            "SELECT position_id, ticker, realized_pnl, max_loss_dollars, max_gain_dollars FROM positions "
            "WHERE status='closed' AND realized_pnl IS NOT NULL "
            "AND COALESCE(regime_at_entry,'') <> 'adopted'"
        ).fetchall():
            ml = abs(r["max_loss_dollars"] or 0.0)
            mg = abs(r["max_gain_dollars"] or 0.0)
            rp = r["realized_pnl"]
            if not (ml or mg) or pnl_within_bounds(rp, ml, mg):
                continue
            if rp > 0 and mg > 0:
                new = round(mg, 2)
                summary["engine_clamped"] += 1
            elif rp < 0 and ml > 0:
                new = round(-ml, 2)
                summary["engine_clamped"] += 1
            else:
                new = 0.0   # the violated bound is unknown — neutralise rather than fabricate a clamp
                summary["engine_zeroed"] += 1
            conn.execute("UPDATE positions SET realized_pnl=? WHERE position_id=?", (new, r["position_id"]))
            summary["details"].append({"position_id": r["position_id"], "ticker": r["ticker"],
                                       "was": rp, "now": new})
        conn.commit()
    return summary

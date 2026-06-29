#!/usr/bin/env python3
"""
Dedupe double-booked ADOPTED positions (2026-06-29).

ROOT CAUSE (now prevented in position_reconciler.heal via the orphan in-flight guard): during
leg-by-leg spread entry the long leg fills SECONDS before the short, and the spread's DB row is
written only AFTER both legs fill. In that race window the lone filled long leg looked like an orphan
and the heal ADOPTED it as a standalone long_call/long_put — so once the spread row landed, the SAME
broker contract was booked twice (e.g. JPM 340C / NVDA 205C: db_qty=2 vs broker=1).

This tool removes ONLY the duplicate adopted rows, defined precisely and conservatively:
    an adopted position (regime_at_entry='adopted', status open/tested/rolled) is a duplicate iff
    EVERY one of its legs (ticker,right,strike,expiry) is ALSO carried by some OTHER open
    NON-adopted position (the real strategy row that the broker leg actually belongs to).

A genuine standalone adoption (SMCI 29P, SPY 771C — no competing real position) has at least one leg
NOT covered elsewhere, so it is NEVER touched. Deleting those would create real untracked broker risk.

Closes via PositionManager.mark_position_closed(source='reconcile_dedupe', realized_pnl=0): adopted
rows book $0 under the _REAL_CLOSE guard, so this injects no P&L fiction. Dry-run by default; pass
--apply to execute. Run with the engine STOPPED to avoid SQLite write contention.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict

LIVE = ("open", "tested", "rolled")


def _legs_of(legs_json: str) -> set[tuple]:
    out: set[tuple] = set()
    try:
        for lg in json.loads(legs_json or "[]"):
            right = "C" if str(lg.get("option_type", "")).lower().startswith("c") else "P"
            out.add((right, float(lg.get("strike", 0) or 0), str(lg.get("expiration", ""))))
    except Exception:
        pass
    return out


def find_duplicates(db) -> list[dict]:
    rows = db.execute(
        "SELECT position_id, ticker, strategy, regime_at_entry, legs_json "
        f"FROM positions WHERE status IN {LIVE}"
    ).fetchall()
    # legs carried by NON-adopted open positions, per ticker
    real_legs: dict[str, set[tuple]] = defaultdict(set)
    for pid, ticker, strat, regime, lj in rows:
        if (regime or "") != "adopted":
            real_legs[ticker] |= _legs_of(lj)
    dupes = []
    for pid, ticker, strat, regime, lj in rows:
        if (regime or "") != "adopted":
            continue
        legs = _legs_of(lj)
        if legs and legs <= real_legs.get(ticker, set()):   # every adopted leg covered by a real row
            dupes.append({"position_id": pid, "ticker": ticker, "strategy": strat, "legs": sorted(legs)})
    return dupes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="execute the closes (default: dry-run)")
    args = ap.parse_args()

    from agora.lifecycle.position_manager import PositionManager
    pm = PositionManager()
    db = pm._db

    dupes = find_duplicates(db)
    if not dupes:
        print("No double-booked adopted positions found — nothing to do.")
        return 0

    print(f"{'APPLYING' if args.apply else 'DRY-RUN'} — {len(dupes)} duplicate adopted position(s):")
    for d in dupes:
        print(f"  {d['ticker']:6} {d['strategy']:12} {d['position_id']}  legs={d['legs']}")

    if not args.apply:
        print("\nRe-run with --apply (engine STOPPED) to close these. Genuine standalone adoptions "
              "(no competing real row) are intentionally left untouched.")
        return 0

    closed = 0
    for d in dupes:
        if pm.mark_position_closed(d["position_id"], realized_pnl=0.0, close_price=0.0,
                                   source="reconcile_dedupe"):
            closed += 1
            print(f"  closed {d['position_id']} (books $0 — adopted)")
    print(f"\nClosed {closed}/{len(dupes)}. Now restart the engine and confirm /agora/reconcile is clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

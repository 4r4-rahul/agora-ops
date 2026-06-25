#!/usr/bin/env python3
"""
scripts/clean_book.py — one-shot, idempotent book-fiction cleanup.

Scans the AGORA book for mathematically-impossible P&L (realized outside a position's own defined-risk
bounds), restates it in place (adopted → per-share entry + neutralised marks; bad engine fills → clamped
to bound), and rebuilds the feature store so ML reads the clean book. Safe to re-run — a clean book is a
no-op. New corruption is blocked at the source (reconciler per-share entry + the _close_position guard);
this only heals pre-existing rows.

Usage: python scripts/clean_book.py [path/to/agora.db]   (default: .agora/agora.db)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agora.ops.book_integrity import clean_book_fiction, scan_impossible_pnl  # noqa: E402
from agora.ops.feature_store import build_feature_store  # noqa: E402


def main() -> int:
    db = sys.argv[1] if len(sys.argv) > 1 else ".agora/agora.db"
    before = scan_impossible_pnl(db)
    print(f"scan: {len(before)} impossible-P&L rows before clean")
    if before:
        res = clean_book_fiction(db)
        print(f"clean: adopted={res['adopted_cleaned']} engine_clamped={res['engine_clamped']} "
              f"engine_zeroed={res['engine_zeroed']}")
        for d in res["details"]:
            print(f"  restated {d['ticker']} {d['position_id']}: {d['was']} -> {d['now']}")
    fs = build_feature_store(db)
    print(f"feature store rebuilt: labeled={fs.get('labeled')}")
    after = scan_impossible_pnl(db)
    print(f"scan: {len(after)} impossible-P&L rows after clean", "OK CLEAN" if not after else after)
    return 0 if not after else 1


if __name__ == "__main__":
    raise SystemExit(main())

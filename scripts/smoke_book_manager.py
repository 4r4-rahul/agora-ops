#!/usr/bin/env python3
"""
smoke_book_manager.py — prove the Book Manager (single source of truth) end-to-end, on demand.

Two halves:
  PART A (mock) — build a realistic DB (real closes + adopted + reconcile + fabricated fiction + a
                  DRIFTED daily_pnl ledger) and assert every invariant: exact partition, reconcile
                  → $0 after rebuild, real ≠ fiction, the bug ledger separates them.
  PART B (live) — run canonical_book against .agora/agora.db and assert the partition + reconciliation
                  invariants hold on the REAL book (read-only).

Run:  python scripts/smoke_book_manager.py
Exit: 0 if every invariant holds, 1 otherwise.
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agora.ops.book_manager import (  # noqa: E402
    canonical_book,
    execution_bug_ledger,
    rebuild_daily_pnl,
    reconcile,
)

GREEN, RED, BOLD, DIM, RESET = "\033[92m", "\033[91m", "\033[1m", "\033[2m", "\033[0m"
_results: list[tuple[str, bool]] = []


def _check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, ok))
    tag = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
    print(f"   {tag}  {name}{('  — ' + detail) if detail else ''}")


def _mock_db() -> str:
    p = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE positions (status TEXT, close_date TEXT, close_source TEXT, "
              "regime_at_entry TEXT DEFAULT 'neutral', realized_pnl REAL, unrealized_pnl REAL DEFAULT 0)")
    rows = [
        ("closed", "2026-06-20", "lifecycle", "neutral", 300.0, 0.0),     # real win
        ("closed", "2026-06-20", "thesis_exit", "neutral", -120.0, 0.0),  # real loss
        ("closed", "2026-06-21", "stop_loss", "neutral", -80.0, 0.0),     # real loss
        ("closed", "2026-06-19", "lifecycle", "adopted", -50000.0, 0.0),  # ADOPTED fiction (impossible loss)
        ("closed", "2026-06-19", "reconcile_ghost", "neutral", -373.0, 0.0),  # over-fill cleanup
        ("closed", "2026-06-19", "fabricated_smoke", "neutral", 999.0, 0.0),  # fabricated fiction
        ("closed", "2026-06-19", "weird_src", "neutral", 7.0, 0.0),       # other-excluded
        ("open", None, "", "neutral", 0.0, 88.0),                          # open (ignored in realized)
    ]
    c.executemany("INSERT INTO positions (status,close_date,close_source,regime_at_entry,realized_pnl,"
                  "unrealized_pnl) VALUES (?,?,?,?,?,?)", rows)
    c.execute("CREATE TABLE daily_pnl (record_date TEXT PRIMARY KEY, realized_pnl REAL, "
              "unrealized_pnl REAL DEFAULT 0, trades_count INTEGER DEFAULT 0)")
    # DRIFTED ledger: wrong values + a fiction day (06-18) with no real close behind it
    c.executemany("INSERT INTO daily_pnl VALUES (?,?,0,0)",
                  [("2026-06-20", 9999.0), ("2026-06-21", -80.0), ("2026-06-18", 314.0)])
    c.commit(); c.close()
    return p


def part_a_mock() -> None:
    print(f"\n{BOLD}PART A — mock book (real + adopted + reconcile + fabricated + drifted ledger){RESET}")
    db = _mock_db()
    b = canonical_book(db)
    rs = b["real_strategy"]
    # real strategy = 300 - 120 - 80 = 100, over 3 closes (fiction excluded)
    _check("real strategy P&L excludes ALL fiction", rs["net_realized"] == 100.0 and rs["n_closed"] == 3,
           f"net={rs['net_realized']} n={rs['n_closed']}")
    # every-penny-accounted: real + Σexcluded == naïve all-closed, to the penny
    _check("partition exact (every penny accounted)", b["partition_ok"] and b["partition_residual"] == 0.0,
           f"residual={b['partition_residual']}")
    # the −$50k adopted fiction is quarantined, NOT in real strategy
    _check("adopted fiction quarantined", b["excluded"]["adopted_legacy"]["pnl"] == -50000.0)
    _check("reconcile/over-fill quarantined", b["excluded"]["reconcile_artifact"]["pnl"] == -373.0)
    _check("fabricated quarantined", b["excluded"]["fabricated_test"]["pnl"] == 999.0)
    # reconciliation: drifted → rebuild → $0
    _check("ledger starts DRIFTED", reconcile(db)["reconciled"] is False)
    out = rebuild_daily_pnl(db)
    _check("rebuild drives drift → $0", out["drift_after"] == 0.0 and out["reconciled_after"],
           f"drift_after={out['drift_after']}, fiction_days_zeroed={out['fiction_days_zeroed']}")
    _check("ledger now equals real book", reconcile(db)["ledger_realized"] == 100.0)
    # bug ledger separates real from fiction
    led = execution_bug_ledger(db)
    _check("bug ledger: real ≠ fiction", led["real_strategy_pnl"] == 100.0 and led["total_excluded_fiction"] != 0,
           f"real={led['real_strategy_pnl']} fiction={led['total_excluded_fiction']}")
    _check("bug ledger: every episode tagged", all(e["date"] and e["root_cause"] and e["fix_commit"]
                                                    for e in led["episodes"]))


def part_b_live() -> None:
    print(f"\n{BOLD}PART B — LIVE book (.agora/agora.db, read-only invariants){RESET}")
    db = ".agora/agora.db"
    if not Path(db).exists():
        print(f"{DIM}   no .agora/agora.db (e.g. CI) — live half SKIPPED (not a failure){RESET}")
        return
    b = canonical_book(db)
    if "error" in b:
        _check("live canonical_book reads", False, b["error"])
        return
    rs = b["real_strategy"]
    print(f"{DIM}   live real strategy P&L = {rs['net_realized']} over {rs['n_closed']} closes "
          f"(win {rs['win_rate']:.1%}, exp {rs['expectancy']}); excluded fiction = "
          f"{b['excluded']['total']}{RESET}")
    _check("live partition exact (every penny accounted)", b["partition_ok"] and b["partition_residual"] == 0.0,
           f"residual={b['partition_residual']}")
    _check("live ledger reconciled (drift ≈ $0)", b["reconciliation"]["reconciled"],
           f"drift={b['reconciliation']['drift']}")
    led = execution_bug_ledger(db)
    _check("live bug ledger computes", "summary" in led, led.get("summary", "")[:80])


def main() -> int:
    print(f"{BOLD}╔══════════════════════════════════════════════════════════════════╗{RESET}")
    print(f"{BOLD}║  SMOKE TEST — Book Manager: one honest number, every surface       ║{RESET}")
    print(f"{BOLD}╚══════════════════════════════════════════════════════════════════╝{RESET}")
    part_a_mock()
    part_b_live()
    passed = sum(1 for _, ok in _results if ok)
    total = len(_results)
    all_ok = passed == total
    color = GREEN if all_ok else RED
    print(f"\n{color}{BOLD}{passed}/{total} invariants held — "
          f"{'BOOK MANAGER VALIDATED ✅' if all_ok else 'AN INVARIANT FAILED ❌'}{RESET}\n")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())

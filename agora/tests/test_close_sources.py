"""
agora/tests/test_close_sources.py — locks the SINGLE source of truth for "real" closes.

Regression guard for the 2026-06-29 bug: time_stop/profit_target/stale_model_stop were missing from the
copy-pasted whitelists, silently dropping real trades (CBOE +$660) from the P&L book. These tests fail
loudly if a legitimate engine close-source is ever dropped again, or if the predicate copies drift.
"""
from __future__ import annotations

import sqlite3

from agora.ops.close_sources import (
    FICTION_PATTERNS,
    REAL_CLOSE_SOURCES,
    real_close_predicate,
)


class TestRealCloseSources:
    def test_previously_missing_sources_are_included(self):
        # the exact ones that caused the bug
        for s in ("time_stop", "profit_target", "stale_model_stop"):
            assert s in REAL_CLOSE_SOURCES, f"{s} must count as a real engine close"

    def test_core_engine_sources_present(self):
        for s in ("lifecycle", "thesis_exit", "trailing_stop", "stop_loss", "pre_earnings"):
            assert s in REAL_CLOSE_SOURCES

    def test_predicate_lists_every_source_and_session_family(self):
        sql = real_close_predicate()
        for s in REAL_CLOSE_SOURCES:
            assert f"'{s}'" in sql
        assert "LIKE 'session:%'" in sql

    def test_predicate_excludes_fiction_and_adopted(self):
        sql = real_close_predicate()
        for pat in FICTION_PATTERNS:
            assert f"NOT LIKE '%{pat}%'" in sql
        assert "regime_at_entry,'') <> 'adopted'" in sql
        assert "status<>'reset'" in sql

    def test_prefix_qualifies_all_columns(self):
        sql = real_close_predicate(prefix="p")
        assert "p.status='closed'" in sql and "p.close_source" in sql
        assert "p.regime_at_entry" in sql
        # no bare (unqualified) column leaked through
        assert "p.close_date" in sql

    def test_require_pnl_adds_guard(self):
        assert "realized_pnl IS NOT NULL" in real_close_predicate(require_pnl=True)
        assert "realized_pnl IS NOT NULL" not in real_close_predicate(require_pnl=False)


class TestRealCloseSemantics:
    """Functional: build a tiny positions table and assert exactly the right rows count."""

    def _db(self):
        c = sqlite3.connect(":memory:")
        c.execute("CREATE TABLE positions (ticker TEXT, status TEXT, close_date TEXT, "
                  "close_source TEXT, regime_at_entry TEXT, realized_pnl REAL)")
        rows = [
            ("CBOE", "closed", "2026-06-29", "time_stop",        "risk_off", 660.0),   # real ✓
            ("TSLA", "closed", "2026-06-29", "profit_target",    "risk_off", 326.0),   # real ✓
            ("AMZN", "closed", "2026-06-29", "stale_model_stop", "neutral", -75.0),    # real ✓ (loss counts)
            ("AAPL", "closed", "2026-06-29", "lifecycle",        "neutral", 100.0),    # real ✓
            ("SPY",  "closed", "2026-06-29", "session:ceo_plan", "risk_on",  50.0),    # real ✓
            ("DIA",  "closed", "2026-06-29", "reconcile_ghost",  "neutral",  0.0),     # fiction ✗
            ("NOK",  "closed", "2026-06-29", "fabricated_unfilled", "neutral", 0.0),   # fiction ✗
            ("JPM",  "closed", "2026-06-29", "tws_startup_sync", "neutral", -373.0),   # artifact ✗
            ("NVDA", "closed", "2026-06-29", "lifecycle",        "adopted",  999.0),   # adopted ✗
            ("MSFT", "open",   None,         None,               "neutral",  0.0),     # not closed ✗
        ]
        c.executemany("INSERT INTO positions VALUES (?,?,?,?,?,?)", rows)
        return c

    def test_counts_real_excludes_fiction_adopted_open(self):
        c = self._db()
        n, total = c.execute(
            f"SELECT COUNT(*), COALESCE(SUM(realized_pnl),0) FROM positions WHERE {real_close_predicate()}"
        ).fetchone()
        assert n == 5, "the 5 real closes (incl time_stop/profit_target/stale_model_stop) must count"
        assert total == 660 + 326 - 75 + 100 + 50  # = 1061
        c.close()

    def test_adopted_winner_never_inflates(self):
        # the NVDA adopted +999 must NOT be counted (reconstructed cost basis)
        c = self._db()
        n, total = c.execute(
            f"SELECT COUNT(*), COALESCE(SUM(realized_pnl),0) FROM positions "
            f"WHERE ticker='NVDA' AND {real_close_predicate()}"
        ).fetchone()
        assert n == 0 and total == 0
        c.close()


# ── DRIFT GUARD: the allowlist may exist in EXACTLY ONE place ────────────────────────────────────
import pathlib  # noqa: E402
import re  # noqa: E402


def test_no_close_source_allowlist_literal_outside_close_sources_py():
    """The CBOE +$711 drop, and its live recurrence in routes.py, both happened because the close-source
    allowlist was copy-pasted and drifted. After centralization, NO module outside close_sources.py may
    hand-spell it. This FAILS the moment a quoted allowlist literal reappears anywhere else — making the
    centralization self-enforcing rather than a hope."""
    agora_root = pathlib.Path(__file__).resolve().parents[1]
    # a real allowlist co-locates these two QUOTED on one line; prose ('a/b/c', comments) won't match
    pat = re.compile(r"""['"]thesis_exit['"]""")
    pat2 = re.compile(r"""['"]trailing_stop['"]""")
    offenders = []
    for py in agora_root.rglob("*.py"):
        if py.name == "close_sources.py" or "/tests/" in str(py).replace("\\", "/"):
            continue
        for i, line in enumerate(py.read_text().splitlines(), 1):
            if pat.search(line) and pat2.search(line):
                offenders.append(f"{py.relative_to(agora_root)}:{i}")
    assert offenders == [], (
        "close-source allowlist literal found OUTSIDE close_sources.py — it must be the single source "
        f"of truth (import REAL_CLOSE_SOURCES / real_close_predicate / is_real_close_row): {offenders}"
    )


def test_is_real_close_row_matches_the_allowlist_exactly():
    from agora.ops.close_sources import REAL_CLOSE_SOURCES, is_real_close_row
    for s in REAL_CLOSE_SOURCES:
        assert is_real_close_row(s, "neutral") is True, f"{s} must be real"
    assert is_real_close_row("session:ceo_plan", "risk_on") is True
    # the exact three that were dropped
    for s in ("time_stop", "profit_target", "stale_model_stop"):
        assert is_real_close_row(s, "risk_off") is True
    # fiction / adopted / non-closed / null-pnl all excluded
    assert is_real_close_row("reconcile_ghost", "neutral") is False
    assert is_real_close_row("tws_startup_sync", "") is False
    assert is_real_close_row("lifecycle", "adopted") is False           # adopted wins
    assert is_real_close_row("lifecycle", "neutral", status="open") is False
    assert is_real_close_row("lifecycle", "neutral", realized_pnl=None) is False

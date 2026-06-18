"""
agora/tests/test_expectancy_meter.py — the North-Star meter (S0.4 + expectancy_meter). Seeds a
real-close ledger split across the legacy cutoff and pins: the post-fix expectancy view excludes
pre-fix churn, progress is measured baseline→target, period buckets, the projection, and the
UNVALIDATED flag at low n. Pure + read-only over a temp DB.
"""
from __future__ import annotations

import sqlite3
import tempfile
from datetime import date, timedelta

from agora.ops.expectancy_meter import build_meter
from agora.ops.performance_metrics import compute_metrics


def _db(rows):
    """rows: (close_date 'YYYY-MM-DD', realized_pnl)."""
    p = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    c = sqlite3.connect(p)
    c.execute("""CREATE TABLE positions (strategy TEXT, status TEXT, close_date TEXT,
                 close_source TEXT, realized_pnl REAL)""")
    c.executemany(
        "INSERT INTO positions VALUES ('bull_put_spread','closed',?,'thesis_exit',?)", rows)
    # the metrics ledger also reads daily_pnl in reconcile (not used here) — keep schema minimal
    c.commit(); c.close()
    return p


_CUT = "2026-06-12"


# ── S0.4 legacy cutoff in performance_metrics ─────────────────────────────────
class TestLegacyCutoff:
    def test_post_fix_excludes_legacy(self):
        # pre-cutoff: two −$200 churn losers; post-cutoff: two +$50 winners
        db = _db([("2026-06-01", -200.0), ("2026-06-05", -200.0),
                  ("2026-06-15", 50.0), ("2026-06-16", 50.0)])
        m = compute_metrics(db, legacy_cutoff=_CUT)
        assert m["overall"]["n"] == 4
        assert m["post_fix"]["n"] == 2                 # only post-cutoff
        assert m["post_fix"]["expectancy"] == 50.0     # both winners
        assert m["overall"]["expectancy"] < 0          # legacy drags all-time negative
        assert m["legacy_cutoff"] == _CUT

    def test_no_cutoff_no_post_fix_key(self):
        db = _db([("2026-06-15", 50.0)])
        assert "post_fix" not in compute_metrics(db)


# ── the meter ─────────────────────────────────────────────────────────────────
class TestMeter:
    def test_progress_baseline_to_target(self):
        # all-time (baseline) ≈ −50; post-fix (current) = +5; target +25 → progress between 0-100
        db = _db([("2026-06-01", -150.0), ("2026-06-02", -150.0),
                  ("2026-06-20", 10.0), ("2026-06-20", 0.0)])
        m = build_meter(db, target_per_trade=25.0, target_date="2026-09-30", legacy_cutoff=_CUT)
        assert m["current"]["expectancy"] == 5.0
        assert m["baseline_expectancy"] < 0
        # progress = (current - baseline)/(target - baseline)*100, between baseline(0) and target(100)
        assert 0 < m["progress_pct"] < 100
        assert m["gap_to_target"] == 20.0

    def test_target_locked_and_countdown(self):
        db = _db([("2026-06-20", 30.0)])
        m = build_meter(db, target_per_trade=25.0, target_date="2026-09-30", legacy_cutoff=_CUT)
        assert m["target"]["locked"] is True
        assert m["target"]["per_trade"] == 25.0 and m["target"]["date"] == "2026-09-30"
        assert isinstance(m["target"]["days_remaining"], int)

    def test_at_target_status(self):
        # need >= MIN_SAMPLE to be validated; many winners above target
        db = _db([("2026-06-20", 40.0)] * 25)
        m = build_meter(db, target_per_trade=25.0, legacy_cutoff=_CUT)
        assert m["current"]["expectancy"] == 40.0
        assert m["validated"] is True
        assert m["status"] == "AT_TARGET"

    def test_unvalidated_at_low_n(self):
        db = _db([("2026-06-20", 40.0)])   # n=1 post-fix
        m = build_meter(db, legacy_cutoff=_CUT)
        assert m["validated"] is False and m["status"] == "UNVALIDATED"

    def test_period_buckets(self):
        today = date.today()
        db = _db([(today.isoformat(), 12.0),
                  ((today - timedelta(days=2)).isoformat(), -8.0),
                  ("2026-06-13", 5.0)])
        m = build_meter(db, legacy_cutoff=_CUT)
        assert m["periods"]["today"]["expectancy"] == 12.0
        assert m["periods"]["today"]["n"] == 1
        # all_post_fix mirrors current
        assert m["periods"]["all_post_fix"]["expectancy"] == m["current"]["expectancy"]

    def test_projection_scales_by_cadence(self):
        db = _db([("2026-06-20", 25.0)] * 4)
        m = build_meter(db, target_per_trade=25.0, legacy_cutoff=_CUT)
        proj = m["projection"]
        assert proj["trades_per_day"] > 0
        # target per-day = target_per_trade * trades_per_day
        assert proj["target"]["per_day"] == round(25.0 * proj["trades_per_day"], 2)

    def test_error_safe_on_bad_db(self):
        m = build_meter("/nonexistent/path.db", target_per_trade=25.0)
        # never raises — returns a shell with the target still present
        assert m["target"]["per_trade"] == 25.0
        assert m["current"]["expectancy"] is None or "error" in m

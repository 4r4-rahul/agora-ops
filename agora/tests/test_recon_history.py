"""
recon_history — daily reconciliation-health history backing the HONESTY proof-gate
(90 consecutive clean days). Locks: idempotent per-day record, and the consecutive-'ok' streak that
breaks on the first non-'ok' day.
"""
import sqlite3

from agora.ops.recon_history import _DDL, consecutive_clean_days


def _seed(db, rows):
    # rows: list of (date, status) most-recent LAST
    with sqlite3.connect(db) as c:
        c.execute(_DDL)
        for d, s in rows:
            c.execute("INSERT OR REPLACE INTO recon_history (snapshot_date,status,real_pnl,recorded_at) "
                      "VALUES (?,?,?,?)", (d, s, 0.0, d))
        c.commit()


class TestConsecutiveCleanDays:
    def test_empty_is_zero(self, tmp_path):
        assert consecutive_clean_days(str(tmp_path / "e.db")) == 0

    def test_counts_trailing_ok_streak(self, tmp_path):
        db = str(tmp_path / "h.db")
        _seed(db, [("2026-06-25", "ok"), ("2026-06-26", "ok"), ("2026-06-27", "ok")])
        assert consecutive_clean_days(db) == 3

    def test_breaks_on_most_recent_non_ok(self, tmp_path):
        db = str(tmp_path / "h.db")
        _seed(db, [("2026-06-25", "ok"), ("2026-06-26", "ok"), ("2026-06-27", "warn")])
        assert consecutive_clean_days(db) == 0   # latest is warn → streak broken

    def test_breaks_at_interior_non_ok(self, tmp_path):
        db = str(tmp_path / "h.db")
        _seed(db, [("2026-06-24", "ok"), ("2026-06-25", "critical"),
                   ("2026-06-26", "ok"), ("2026-06-27", "ok")])
        assert consecutive_clean_days(db) == 2   # only the last two ok days count


class TestHonestyGateUsesStreak:
    def test_gate_green_at_90(self, tmp_path):
        db = str(tmp_path / "g.db")
        _seed(db, [(f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}", "ok") for i in range(95)])
        from agora.ops.proof_gates import GREEN, _honesty_gate
        g = _honesty_gate(db)
        assert g["current"]["consecutive_clean_days"] >= 90
        assert g["status"] == GREEN

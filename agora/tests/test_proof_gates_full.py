"""
Full mock + smoke coverage for the proof-gate build-out (HONESTY/DISASTER/AGENTIC/EDGE).

- Mock tests fill the remaining branches (record paths, ready A/B, defensive excepts, status()).
- The SMOKE builds a fully-GREEN mock DB end-to-end → proves overall=green/sellable=True (positive
  control), complementing the live RED (negative control). So both directions of "are we sellable?"
  are proven, and a disabled/false claim can never silently slip through.
"""
import sqlite3
from datetime import UTC, datetime, timedelta

from agora.ops import incident_log, recon_history
from agora.ops.agentic_ab import _median, agentic_value, arm_comparison
from agora.ops.edge_readout import _band
from agora.ops.proof_gates import GREEN, RED, proof_gates


# ── helpers ──────────────────────────────────────────────────────────────────
def _seed_positions_with_arms(db, n_per_arm=45):
    """_REAL_CLOSE-valid closes stamped with decision_arm: agentic wins, rules_only loses."""
    with sqlite3.connect(db) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS positions (
            status TEXT, close_date TEXT, close_source TEXT, regime_at_entry TEXT,
            realized_pnl REAL, decision_arm TEXT, strategy TEXT, max_loss_dollars REAL,
            config_version_at_entry INT)""")
        for i in range(n_per_arm):
            c.execute("INSERT INTO positions VALUES ('closed','2026-06-27','lifecycle','neutral',?,?,"
                      "'long_put',300,5)", (60.0, "agentic"))
            c.execute("INSERT INTO positions VALUES ('closed','2026-06-27','lifecycle','neutral',?,?,"
                      "'long_put',300,5)", (-40.0, "rules_only"))
        c.commit()


def _seed_recon_clean(db, days):
    with sqlite3.connect(db) as c:
        c.execute(recon_history._DDL)
        for i in range(days):
            d = (datetime.now(UTC) - timedelta(days=i)).date().isoformat()
            c.execute("INSERT OR REPLACE INTO recon_history VALUES (?,?,?,?)", (d, "ok", 0.0, d))
        c.commit()


def _seed_watch(db, days_ago):
    ts = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    with sqlite3.connect(db) as c:
        c.execute(incident_log._DDL)
        c.execute("INSERT INTO safety_incidents (occurred_at, kind, detail) VALUES (?,?,?)",
                  (ts, "__watch_start__", "start"))
        c.commit()


# ── mock tests: fill the remaining branches ──────────────────────────────────
class TestReconHistoryRecord:
    def test_record_writes_and_is_idempotent(self, tmp_path, monkeypatch):
        db = str(tmp_path / "r.db")
        monkeypatch.setattr("agora.ops.book_manager.reconciliation_health",
                            lambda p: {"status": "ok", "real_strategy_pnl": -10.0})
        recon_history.record_recon_snapshot(db, on_date="2026-06-27")
        recon_history.record_recon_snapshot(db, on_date="2026-06-27")   # same day → upsert, no dup
        with sqlite3.connect(db) as c:
            rows = c.execute("SELECT status FROM recon_history").fetchall()
        assert rows == [("ok",)]

    def test_record_error_is_swallowed(self):
        assert "error" in recon_history.record_recon_snapshot("/nonexistent/dir/x.db", on_date="2026-06-27")

    def test_consecutive_on_bad_path_is_zero(self):
        assert recon_history.consecutive_clean_days("/nonexistent/dir/x.db") == 0


class TestIncidentLogDefensive:
    def test_status_combines(self, tmp_path):
        db = str(tmp_path / "s.db")
        _seed_watch(db, 10)
        st = incident_log.status(db)
        assert st["incident_free_days"] == 10 and st["incident_count"] == 0

    def test_record_and_free_days_on_bad_path_are_safe(self):
        incident_log.record_incident("/nonexistent/dir/x.db", "k", "d")   # no raise
        assert incident_log.incident_free_days("/nonexistent/dir/x.db") == 0
        assert incident_log.incident_count("/nonexistent/dir/x.db") == 0


class TestAgenticReadyPath:
    def test_median_empty(self):
        assert _median([]) is None

    def test_arm_comparison_ready_and_agentic_beats(self, tmp_path):
        db = str(tmp_path / "a.db")
        _seed_positions_with_arms(db, n_per_arm=45)
        ab = arm_comparison(db)
        assert ab["status"] == "ready"
        assert ab["agentic_beats_baseline"] is True
        assert ab["expectancy_delta"] == 100.0   # +60 vs -40

    def test_arm_comparison_insufficient_when_thin(self, tmp_path):
        db = str(tmp_path / "t.db")
        _seed_positions_with_arms(db, n_per_arm=5)   # < 40/arm
        assert arm_comparison(db)["status"] == "insufficient_data"

    def test_agentic_value_has_both_parts(self, tmp_path):
        db = str(tmp_path / "v.db")
        _seed_positions_with_arms(db, n_per_arm=45)
        av = agentic_value(db)
        assert "arm_comparison" in av and "signal_predictiveness" in av


class TestEdgeBandEdges:
    def test_band_over_max_and_negative(self):
        assert _band(20_000_000) == "unknown"
        assert _band(-5) == "unknown"


class TestProofGatesDefensive:
    def test_failing_gate_reports_red_not_crash(self, monkeypatch, tmp_path):
        # force the edge gate to raise → board must still return, that gate RED-with-error
        monkeypatch.setattr("agora.ops.proof_gates._edge_gate",
                            lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
        board = proof_gates(str(tmp_path / "x.db"))
        assert len(board["gates"]) == 4                       # board complete, no crash
        errored = [g for g in board["gates"] if "error" in g]
        assert errored and errored[0]["status"] == RED        # failing gate is RED-with-error
        assert board["sellable"] is False


# ── SMOKE: end-to-end positive + negative controls ───────────────────────────
class TestSmokeProofGates:
    def test_negative_control_empty_db_not_sellable(self, tmp_path):
        board = proof_gates(str(tmp_path / "empty.db"))
        assert board["overall"] in (RED, "amber") and board["sellable"] is False

    def test_positive_control_all_green_is_sellable(self, tmp_path, monkeypatch):
        db = str(tmp_path / "green.db")
        _seed_recon_clean(db, 95)             # HONESTY: 95 consecutive clean days
        _seed_watch(db, 95)                   # DISASTER: 95 incident-free days, no incidents
        _seed_positions_with_arms(db, 45)     # AGENTIC: 45/arm, agentic beats baseline
        # EDGE: seed canonical_book green (seeding its full input is heavy; the gate's real green-branch
        # logic still runs against this green input).
        monkeypatch.setattr("agora.ops.book_manager.canonical_book",
                            lambda p: {"real_strategy": {"n_closed": 200, "expectancy": 25.0,
                                                         "win_rate": 0.6, "profit_factor": 1.8,
                                                         "net_realized": 5000.0}})
        board = proof_gates(db)
        by = {g["pillar"]: g["status"] for g in board["gates"]}
        assert by == {"EDGE": GREEN, "HONESTY": GREEN, "DISASTER": GREEN, "AGENTIC": GREEN}
        assert board["overall"] == GREEN and board["sellable"] is True


# ── final fail-open branch coverage (the defensive guards must never raise) ────
class TestFailOpenGuards:
    def test_ensure_watching_bad_path_no_raise(self):
        incident_log.ensure_watching("/nonexistent/dir/x.db")   # must not raise

    def test_incident_free_days_malformed_date(self, tmp_path):
        db = str(tmp_path / "m.db")
        with sqlite3.connect(db) as c:
            c.execute(incident_log._DDL)
            c.execute("INSERT INTO safety_incidents (occurred_at, kind, detail) VALUES "
                      "('not-a-date','__watch_start__','x')")
            c.commit()
        assert incident_log.incident_free_days(db) == 0   # unparseable → 0, no raise

    def test_edge_gate_survives_readout_failure(self, monkeypatch, tmp_path):
        from agora.ops import proof_gates as pg
        monkeypatch.setattr("agora.ops.edge_readout.by_config_version",
                            lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
        monkeypatch.setattr("agora.ops.book_manager.canonical_book",
                            lambda p: {"real_strategy": {"n_closed": 1, "expectancy": -5.0}})
        g = pg._edge_gate(str(tmp_path / "x.db"))   # by_config_version raises → caught → cv_trend=[]
        assert g["current"]["by_config_version"] == []

    def test_arm_comparison_query_error_is_caught(self, tmp_path):
        # positions has decision_arm but is missing columns _REAL_CLOSE needs → query raises → caught
        db = str(tmp_path / "broken.db")
        with sqlite3.connect(db) as c:
            c.execute("CREATE TABLE positions (decision_arm TEXT, realized_pnl REAL)")
            c.commit()
        res = arm_comparison(db)
        assert res["status"] in ("error", "insufficient_data")   # never raises

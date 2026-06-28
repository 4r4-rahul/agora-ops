"""is_cell_blocked — suppress ONLY data-condemned cells (n>=min AND expectancy<min); fail-open else."""
import agora.ops.cell_gate as cg
from agora.ops.cell_gate import is_cell_blocked


def _patch(monkeypatch, stats):
    monkeypatch.setattr(cg, "cell_stats", lambda db, cutoff: stats)


class TestIsCellBlocked:
    def test_blocked_when_proven_negative(self, monkeypatch):
        _patch(monkeypatch, {("long_call", "DIRECTIONAL"): {"n": 40, "expectancy": -150.0}})
        blocked, reason = is_cell_blocked("x", "long_call", "DIRECTIONAL",
                                          cutoff="2026-01-01", min_samples=20, min_expectancy=-50)
        assert blocked is True and "negative" in reason

    def test_not_blocked_when_thin(self, monkeypatch):
        _patch(monkeypatch, {("long_call", "DIRECTIONAL"): {"n": 5, "expectancy": -150.0}})
        assert is_cell_blocked("x", "long_call", "DIRECTIONAL",
                               cutoff="c", min_samples=20, min_expectancy=-50)[0] is False

    def test_not_blocked_when_positive(self, monkeypatch):
        _patch(monkeypatch, {("long_call", "DIRECTIONAL"): {"n": 40, "expectancy": 10.0}})
        assert is_cell_blocked("x", "long_call", "DIRECTIONAL",
                               cutoff="c", min_samples=20, min_expectancy=-50)[0] is False

    def test_fail_open_on_error(self, monkeypatch):
        monkeypatch.setattr(cg, "cell_stats", lambda db, cutoff: (_ for _ in ()).throw(RuntimeError("x")))
        assert is_cell_blocked("x", "s", "p", cutoff="c", min_samples=20, min_expectancy=-50)[0] is False


class TestCellStatsAndBlockedCells:
    def _seed(self, tmp_path):
        import sqlite3
        db = str(tmp_path / "c.db")
        with sqlite3.connect(db) as c:
            c.execute("CREATE TABLE positions (strategy TEXT, pillar TEXT, realized_pnl REAL, "
                      "status TEXT, close_date TEXT, close_source TEXT, regime_at_entry TEXT)")
            for pnl in (-200, -180, -160):   # long_call/DIRECTIONAL: clearly negative
                c.execute("INSERT INTO positions VALUES ('long_call','DIRECTIONAL',?,'closed',"
                          "'2026-06-27','lifecycle','neutral')", (pnl,))
            c.execute("INSERT INTO positions VALUES ('long_put','DIRECTIONAL',50,'closed',"
                      "'2026-06-27','lifecycle','neutral')")
            c.commit()
        return db

    def test_cell_stats_aggregates_real_closes(self, tmp_path):
        from agora.ops.cell_gate import cell_stats
        st = cell_stats(self._seed(tmp_path), "2026-01-01")
        assert st[("long_call", "DIRECTIONAL")]["n"] == 3
        assert st[("long_call", "DIRECTIONAL")]["expectancy"] == -180.0

    def test_cell_stats_bad_path_returns_empty(self):
        from agora.ops.cell_gate import cell_stats
        assert cell_stats("/nonexistent/dir/x.db", "2026-01-01") == {}

    def test_blocked_cells_reporting(self, tmp_path):
        from agora.ops.cell_gate import blocked_cells
        out = blocked_cells(self._seed(tmp_path), cutoff="2026-01-01", min_samples=3, min_expectancy=-50)
        assert "long_call/DIRECTIONAL" in out["blocked"]       # proven-negative, benched
        assert "long_put/DIRECTIONAL" not in out["blocked"]    # positive, not benched
        assert "all_cells" in out

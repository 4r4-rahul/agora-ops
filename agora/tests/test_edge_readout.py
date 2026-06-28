"""
edge_readout — per-cell expectancy to calibrate toward a proven edge.
Locks: size-banding, credible-cell threshold (thin cells omitted), config-version segmentation, and
outlier-robust reporting (median alongside mean).
"""
import sqlite3

from agora.ops.edge_readout import _band, by_cell, by_config_version, edge_readout


def _db(tmp_path, rows):
    # rows: (config_version, regime, strategy, max_loss, pnl)
    db = str(tmp_path / "e.db")
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE positions (config_version_at_entry INT, regime_at_entry TEXT, "
                  "strategy TEXT, max_loss_dollars REAL, realized_pnl REAL, status TEXT, "
                  "close_date TEXT, close_source TEXT, regime_at_entry2 TEXT)")
        for cv, reg, strat, ml, pnl in rows:
            c.execute("INSERT INTO positions (config_version_at_entry,regime_at_entry,strategy,"
                      "max_loss_dollars,realized_pnl,status,close_date,close_source) "
                      "VALUES (?,?,?,?,?, 'closed','2026-06-27','lifecycle')", (cv, reg, strat, ml, pnl))
        c.commit()
    return db


class TestBanding:
    def test_bands(self):
        assert _band(150) == "$0-200"
        assert _band(500) == "$400-600"
        assert _band(1200) == "$800+"
        assert _band(None) == "unknown"


class TestReadout:
    def test_by_config_version_segments(self, tmp_path):
        db = _db(tmp_path, [(4, "neutral", "long_put", 300, -50),
                            (5, "neutral", "long_put", 300, 100),
                            (5, "neutral", "long_put", 300, 60)])
        cv = {r["config_version"]: r for r in by_config_version(db)}
        assert cv[4]["expectancy"] == -50.0
        assert cv[5]["n"] == 2 and cv[5]["expectancy"] == 80.0   # post-fix version positive

    def test_thin_cells_omitted(self, tmp_path):
        db = _db(tmp_path, [(5, "neutral", "long_put", 300, 10)] * 3)   # n=3 < min_n=5
        assert by_cell(db, min_n=5) == []
        assert len(by_cell(db, min_n=2)) == 1

    def test_outlier_robust_median_reported(self, tmp_path):
        # 4 small losers + 1 huge winner: mean positive, median negative — both must be shown
        db = _db(tmp_path, [(5, "n", "s", 300, -20)] * 4 + [(5, "n", "s", 300, 1000)])
        cell = by_cell(db, min_n=5)[0]
        assert cell["median_pnl"] == -20.0 and cell["expectancy"] > 0   # the lesson: read both

    def test_full_readout_shape(self, tmp_path):
        db = _db(tmp_path, [(5, "neutral", "long_put", 300, 10)] * 6)
        out = edge_readout(db, min_n=5)
        assert out["total_real_closes"] == 6 and len(out["credible_cells"]) == 1

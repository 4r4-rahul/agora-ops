"""
ConvictionWeightCalibrator — data-driven diagnostics must be OUTLIER-ROBUST.

Lesson 2026-06-27: an early mean-based check (and a PF-based one) wrongly called conviction "healthy"
because 4 lucky bear_put_spread winners (+$3,334) landed in the top quintile. The real signal: every
quintile loses on MEDIAN. These tests lock in that a few big winners cannot fake an edge.
"""
from agora.ops.conviction_calibrator import _diagnostics, _median


def _q(win, median, avg, pf=1.0):
    return {"count": 22, "win_rate": win, "median_pnl": median, "avg_pnl": avg, "profit_factor": pf}


class TestMedian:
    def test_odd(self):
        assert _median([1.0, 5.0, 2.0]) == 2.0

    def test_even(self):
        assert _median([1.0, 2.0, 3.0, 4.0]) == 2.5

    def test_empty(self):
        assert _median([]) is None


class TestConvictionPredictiveness:
    def _verdict(self, quintiles):
        d = [x for x in _diagnostics(quintiles, {}, {}) if x["kind"] == "conviction_predictiveness"]
        return d[0]

    def test_outliers_do_not_fake_an_edge(self):
        # Top quintile: positive MEAN + PF>1 from a few big winners, but NEGATIVE median + still loses.
        quintiles = {
            "Q1": _q(0.227, -66.5, -77.36, pf=0.4),
            "Q5": _q(0.409, -54.5, 50.27, pf=1.35),   # mean/PF look good, median says loser
        }
        v = self._verdict(quintiles)
        assert v["severity"] == "critical"
        assert v["outlier_driven_mean"] is True
        assert "NOT ACTIONABLE" in v["finding"]

    def test_genuine_edge_is_actionable(self):
        # Top quintile actually profitable on the robust metric (positive median) and beats bottom.
        quintiles = {
            "Q1": _q(0.30, -40.0, -30.0, pf=0.6),
            "Q5": _q(0.62, 120.0, 150.0, pf=2.0),
        }
        v = self._verdict(quintiles)
        assert v["severity"] == "ok"
        assert "ACTIONABLE" in v["finding"] and "NOT ACTIONABLE" not in v["finding"]


class TestRegimeAndCellLeaks:
    def test_regime_leak_flagged(self):
        regime = {"neutral": {"count": 74, "profit_factor": 0.386, "win_rate": 0.257}}
        leaks = [x for x in _diagnostics({}, regime, {}) if x["kind"] == "regime_leak"]
        assert leaks and leaks[0]["regime"] == "neutral"

    def test_healthy_regime_not_flagged(self):
        regime = {"risk_off": {"count": 36, "profit_factor": 0.966, "win_rate": 0.528}}
        leaks = [x for x in _diagnostics({}, regime, {}) if x["kind"] == "regime_leak"]
        assert not leaks

    def test_cell_leak_flagged(self):
        cells = {"directional:neutral": {"count": 30, "avg_pnl": -140.93, "win_rate": 0.367}}
        leaks = [x for x in _diagnostics({}, {}, cells) if x["kind"] == "cell_leak"]
        assert leaks and leaks[0]["cell"] == "directional:neutral"

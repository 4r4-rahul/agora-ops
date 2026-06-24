"""
agora/tests/test_ledger_calibration.py — ledger-derived calibration maps (SHADOW recalibration).

The headline contract: fitting a binned map on an INVERTED predictor (high predicted → low actual)
must LOWER the Brier score — i.e. the recalibration provably corrects the bias. Plus pure-math edge
cases (empty bins, out-of-range lookup, identity) and store/shadow safety.
"""
from __future__ import annotations

import tempfile

from agora.ops.ledger_calibration import (
    _apply_map,
    _build_map,
    _calibrated_brier,
    fit_calibration,
    get_calibration_maps,
    run_ledger_recalibration,
    store_calibration_map,
)
from agora.ops.prediction_ledger import record_prediction, score_predictions


def _db():
    return tempfile.NamedTemporaryFile(suffix=".db", delete=False).name


# ── pure map math ─────────────────────────────────────────────────────────────────────
def test_build_map_has_numeric_edges_and_rates():
    pairs = [(0.1, 0), (0.15, 0), (0.85, 1), (0.9, 1)]
    bins = _build_map(pairs, n_bins=5)
    assert bins[0]["lo"] == 0.0 and bins[0]["actual_rate"] == 0.0
    assert bins[-1]["actual_rate"] == 1.0


def test_apply_map_in_bin_out_of_bin_and_identity():
    bins = [{"lo": 0.0, "hi": 0.2, "actual_rate": 0.05, "n": 4},
            {"lo": 0.8, "hi": 1.0, "actual_rate": 0.9, "n": 4}]
    assert _apply_map(0.1, bins) == 0.05            # in-bin
    assert _apply_map(0.85, bins) == 0.9            # in-bin
    assert _apply_map(0.5, bins) in (0.05, 0.9)     # out-of-bin → nearest populated
    assert _apply_map(0.5, []) == 0.5               # no bins → identity


def test_calibrated_brier_beats_raw_on_inverted_data():
    # INVERTED: high predicted → low actual. The remap must reduce Brier.
    from agora.ops.prediction_ledger import brier
    pairs = [(0.9, 0), (0.85, 0), (0.8, 0), (0.2, 1), (0.25, 1), (0.3, 1)] * 3
    bins = _build_map(pairs, n_bins=5)
    raw = sum(brier(p, a) for p, a in pairs) / len(pairs)
    calib = _calibrated_brier(pairs, bins)
    assert calib < raw          # recalibration provably corrects the inverted predictor


# ── fit / store / run (IO, shadow) ────────────────────────────────────────────────────
def _seed(db, source, pairs):
    for i, (p, a) in enumerate(pairs):
        record_prediction(db, source=source, target_key=f"{source}{i}", target_type="trade", predicted=p)
    score_predictions(db, source=source,
                      actual_resolver=lambda k, _m={f"{source}{i}": a for i, (p, a) in enumerate(pairs)}: _m.get(k))


def test_fit_below_min_n_is_none():
    db = _db()
    _seed(db, "thin", [(0.5, 1)] * 5)
    assert fit_calibration(db, "thin", min_n=20) is None


def test_fit_detects_inverted_and_improves_brier():
    db = _db()
    _seed(db, "conviction", [(0.9, 0), (0.85, 0), (0.8, 0), (0.2, 1), (0.25, 1), (0.3, 1)] * 5)
    fit = fit_calibration(db, "conviction", min_n=20)
    assert fit and fit["inverted"] is True
    assert fit["brier_calibrated"] < fit["brier_raw"]      # the map helps


def test_store_is_shadow_and_run_summarizes():
    db = _db()
    _seed(db, "conviction", [(0.9, 0), (0.2, 1)] * 15)
    r = run_ledger_recalibration(db, ["conviction"], min_n=20)
    assert r["status"] == "ok" and r["fitted"]
    maps = get_calibration_maps(db)
    assert maps and maps[0]["active"] == 0          # SHADOW — never applied
    assert maps[0]["inverted"] == 1


def test_error_safe():
    assert fit_calibration("/nonexistent/x.db", "s") is None
    assert store_calibration_map("/nonexistent/x.db", {"source": "s", "bins": [], "n": 1, "inverted": False}) is False
    assert get_calibration_maps("/nonexistent/x.db") == []
    assert run_ledger_recalibration("/nonexistent/x.db")["status"] == "ok"

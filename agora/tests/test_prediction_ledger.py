"""
agora/tests/test_prediction_ledger.py — the unified predicted-vs-actual ledger.

Heavy coverage: pure scoring (brier clamping/boundaries, signed bias, reliability bins), record
idempotency (update-while-unscored, frozen-after-scored), horizon-gated scoring (resolver None =
pending), calibration aggregation (min-n gate, brier, the INVERTED-predictor flag), error-safety.
"""
from __future__ import annotations

import tempfile

from agora.ops.prediction_ledger import (
    bias,
    brier,
    calibration_summary,
    recent_scored,
    record_prediction,
    reliability_bins,
    score_predictions,
)


def _db():
    return tempfile.NamedTemporaryFile(suffix=".db", delete=False).name


# ── pure scoring ──────────────────────────────────────────────────────────────────────
def test_brier_perfect_and_worst():
    assert brier(1.0, 1) == 0.0 and brier(0.0, 0) == 0.0       # perfect
    assert brier(1.0, 0) == 1.0 and brier(0.0, 1) == 1.0       # worst


def test_brier_clamps_out_of_range():
    assert brier(1.5, 1) == 0.0 and brier(-0.5, 0) == 0.0      # clamped to [0,1]


def test_bias_signed():
    assert bias(0.3, 1.0) == 0.7        # under-predicted
    assert bias(0.8, 0.0) == -0.8       # over-predicted


def test_reliability_bins_empty_and_rates():
    assert reliability_bins([]) == []
    pairs = [(0.1, 0), (0.15, 0), (0.85, 1), (0.9, 1)]   # low bin 0% actual, high bin 100%
    bins = reliability_bins(pairs, n_bins=5)
    lo = next(b for b in bins if b["bin"].startswith("0%"))
    hi = next(b for b in bins if b["mean_predicted"] > 0.5)
    assert lo["actual_rate"] == 0.0 and hi["actual_rate"] == 1.0


# ── record idempotency ────────────────────────────────────────────────────────────────
def test_record_then_update_while_unscored():
    db = _db()
    record_prediction(db, source="conviction", target_key="p1", target_type="trade", predicted=0.7)
    record_prediction(db, source="conviction", target_key="p1", target_type="trade", predicted=0.4)  # re-record
    import sqlite3
    c = sqlite3.connect(db)
    rows = c.execute("SELECT predicted FROM prediction_ledger WHERE target_key='p1'").fetchall()
    assert len(rows) == 1 and rows[0][0] == 0.4    # single row, updated (not duplicated)


def test_record_frozen_after_scored():
    db = _db()
    record_prediction(db, source="s", target_key="p1", target_type="trade", predicted=0.7)
    score_predictions(db, source="s", actual_resolver=lambda k: 1.0)
    record_prediction(db, source="s", target_key="p1", target_type="trade", predicted=0.1)  # must NOT change
    import sqlite3
    c = sqlite3.connect(db)
    pred = c.execute("SELECT predicted FROM prediction_ledger WHERE target_key='p1'").fetchone()[0]
    assert pred == 0.7    # frozen after scoring


# ── scoring (horizon-gated) ───────────────────────────────────────────────────────────
def test_score_pending_when_actual_unknown():
    db = _db()
    record_prediction(db, source="s", target_key="p1", target_type="trade", predicted=0.7)
    r = score_predictions(db, source="s", actual_resolver=lambda k: None)   # actual not known yet
    assert r["scored"] == 0 and r["pending"] == 1


def test_score_records_gap():
    db = _db()
    record_prediction(db, source="s", target_key="p1", target_type="trade", predicted=0.3)
    r = score_predictions(db, source="s", actual_resolver=lambda k: 1.0)
    assert r["scored"] == 1
    sc = recent_scored(db)[0]
    assert sc["actual"] == 1.0 and sc["gap"] == 0.7     # actual − predicted


# ── calibration aggregation + the inverted flag ───────────────────────────────────────
def _seed(db, source, pairs):
    for i, (p, a) in enumerate(pairs):
        record_prediction(db, source=source, target_key=f"{source}{i}", target_type="trade", predicted=p)
    score_predictions(db, source=source, actual_resolver=lambda k, _m={f"{source}{i}": a for i, (p, a) in enumerate(pairs)}: _m.get(k))


def test_calibration_below_min_n_excluded():
    db = _db()
    _seed(db, "thin", [(0.5, 1)] * 3)
    assert calibration_summary(db, min_n=10) == []


def test_calibration_flags_inverted_predictor():
    db = _db()
    # high predicted → LOW actual (anti-predictive, like our conviction signal)
    _seed(db, "conviction", [(0.9, 0), (0.85, 0), (0.8, 0), (0.3, 1), (0.25, 1), (0.2, 1)] * 2)
    summ = calibration_summary(db, min_n=10)
    conv = next(s for s in summ if s["source"] == "conviction")
    assert conv["inverted"] is True and conv["brier"] > 0


def test_calibration_well_calibrated_not_inverted():
    db = _db()
    _seed(db, "good", [(0.9, 1), (0.85, 1), (0.8, 1), (0.2, 0), (0.25, 0), (0.3, 0)] * 2)
    good = next(s for s in calibration_summary(db, min_n=10) if s["source"] == "good")
    assert good["inverted"] is False and good["brier"] < 0.2


def test_error_safe():
    assert record_prediction("/nonexistent/x.db", source="s", target_key="k", target_type="t",
                             predicted=0.5) is False
    assert calibration_summary("/nonexistent/x.db") == []
    assert recent_scored("/nonexistent/x.db") == []
    assert score_predictions("/nonexistent/x.db", source="s", actual_resolver=lambda k: 1.0)["status"] in ("ok", "error")

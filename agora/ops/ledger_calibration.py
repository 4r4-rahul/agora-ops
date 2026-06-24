"""
agora/ops/ledger_calibration.py — fit a calibration correction from the prediction ledger (SHADOW).

(Distinct from agora/ops/recalibrate.py, which recalibrates the advocate agent.) Once the prediction
ledger has scored enough predicted-vs-actual pairs for a source, we fit a BINNED calibration map (raw
predicted prob → empirical actual rate) that corrects a biased/inverted predictor — e.g. conviction,
which the ledger shows is anti-predictive. We measure Brier before vs after the remap to prove it
helps, and store the map SHADOW (logged, never applied) until promoted.

Pure fit math (_build_map / _apply_map / _calibrated_brier) is isolated for edge-case testing. IO never
raises. This is the "recalibrate" half of the predicted→actual→recalibrate loop — observational until
a human/board promotes a map to actually rescale the live signal.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from agora.ops.prediction_ledger import brier, reliability_bins

_DDL = """
CREATE TABLE IF NOT EXISTS calibration_maps (
    source      TEXT PRIMARY KEY,
    bins_json   TEXT NOT NULL,     -- the fitted [{lo, hi, actual_rate, n}] remap
    n           INTEGER NOT NULL,
    inverted    INTEGER NOT NULL,
    brier_raw   REAL,
    brier_calib REAL,
    active      INTEGER NOT NULL DEFAULT 0,   -- 0 = shadow (logged, NOT applied), 1 = live
    updated_at  TEXT NOT NULL
);
"""


def _build_map(pairs: list[tuple[float, float]], n_bins: int = 5) -> list[dict]:
    """PURE: a binned remap from reliability bins — predicted in [lo,hi) → the bin's empirical actual
    rate. Empty bins are skipped; lookup falls back to the nearest populated bin."""
    out = []
    for b in reliability_bins(pairs, n_bins=n_bins):
        lo_s, hi_s = b["bin"].split("-")
        out.append({"lo": int(lo_s.rstrip("%")) / 100, "hi": int(hi_s.rstrip("%")) / 100,
                    "actual_rate": b["actual_rate"], "n": b["n"]})
    return out


def _apply_map(predicted: float, bins: list[dict]) -> float:
    """PURE: map a raw predicted prob through the fitted bins → calibrated prob. Falls back to the
    nearest bin by midpoint when the value lands outside the populated bins. Identity if no bins."""
    if not bins:
        return max(0.0, min(1.0, float(predicted)))
    p = max(0.0, min(0.999999, float(predicted)))
    for b in bins:
        if b["lo"] <= p < b["hi"]:
            return b["actual_rate"]
    nearest = min(bins, key=lambda b: abs((b["lo"] + b["hi"]) / 2 - p))
    return nearest["actual_rate"]


def _calibrated_brier(pairs: list[tuple[float, float]], bins: list[dict]) -> float | None:
    """PURE: mean Brier after applying the remap to each predicted value."""
    if not pairs:
        return None
    return round(sum(brier(_apply_map(p, bins), a) for p, a in pairs) / len(pairs), 4)


def fit_calibration(db_path: Any, source: str, *, min_n: int = 20, n_bins: int = 5) -> dict | None:
    """Fit a calibration map for `source` from its scored ledger pairs (with before/after Brier).
    None if too few samples. Never raises."""
    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        conn.executescript(_DDL)
        rows = conn.execute(
            "SELECT predicted, actual FROM prediction_ledger "
            "WHERE source=? AND scored=1 AND actual IS NOT NULL", (source,)).fetchall()
        conn.close()
    except Exception:
        return None
    pairs = [(float(p), float(a)) for p, a in rows]
    if len(pairs) < min_n:
        return None
    bins = _build_map(pairs, n_bins=n_bins)
    n = len(pairs)
    mean_pred = sum(p for p, _ in pairs) / n
    hi = [a for p, a in pairs if p >= mean_pred]
    lo = [a for p, a in pairs if p < mean_pred]
    inverted = bool(hi and lo and (sum(hi) / len(hi)) < (sum(lo) / len(lo)))
    return {
        "source": source, "n": n, "bins": bins, "inverted": inverted,
        "brier_raw": round(sum(brier(p, a) for p, a in pairs) / n, 4),
        "brier_calibrated": _calibrated_brier(pairs, bins),
    }


def store_calibration_map(db_path: Any, fit: dict) -> bool:
    """Persist a fitted map SHADOW (active=0 → logged, never applied). Never raises."""
    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        conn.executescript(_DDL)
        conn.execute(
            "INSERT INTO calibration_maps (source, bins_json, n, inverted, brier_raw, brier_calib, "
            "active, updated_at) VALUES (?,?,?,?,?,?,0,?) "
            "ON CONFLICT(source) DO UPDATE SET bins_json=excluded.bins_json, n=excluded.n, "
            "inverted=excluded.inverted, brier_raw=excluded.brier_raw, brier_calib=excluded.brier_calib, "
            "updated_at=excluded.updated_at",
            (fit["source"], json.dumps(fit["bins"]), fit["n"], 1 if fit["inverted"] else 0,
             fit.get("brier_raw"), fit.get("brier_calibrated"), datetime.now(UTC).isoformat()))
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False


def run_ledger_recalibration(db_path: Any, sources: list[str] | None = None, *, min_n: int = 20) -> dict:
    """Fit + store (SHADOW) a calibration map for each source. Never raises."""
    srcs = sources or ["conviction"]
    fitted = []
    for s in srcs:
        fit = fit_calibration(db_path, s, min_n=min_n)
        if fit and store_calibration_map(db_path, fit):
            fitted.append({"source": s, "n": fit["n"], "inverted": fit["inverted"],
                           "brier_raw": fit["brier_raw"], "brier_calibrated": fit["brier_calibrated"]})
    return {"status": "ok", "fitted": fitted,
            "summary": "; ".join(f"{f['source']}: Brier {f['brier_raw']}→{f['brier_calibrated']}"
                                 f"{' (INVERTED)' if f['inverted'] else ''}" for f in fitted)
                       or "no source had enough scored predictions yet"}


def get_calibration_maps(db_path: Any) -> list[dict]:
    """All fitted maps (for the dashboard). Never raises."""
    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        conn.executescript(_DDL)
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT source, n, inverted, brier_raw, brier_calib, active, updated_at FROM calibration_maps")]
        conn.close()
        return rows
    except Exception:
        return []

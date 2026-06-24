"""
agora/ops/prediction_ledger.py — the unified predicted-vs-actual ledger (the meaningfulness loop).

Every prediction the engine makes (conviction→win, news direction→move, model scores, per-ticker
would-be settings, surveillance verdicts) lands HERE with the settings regime it was made under. When
the actual outcome arrives (trade closes, horizon elapses), we record it and compute the GAP. Rolling
calibration per source × regime then tells us which predictors are honest and which are biased — and
the recalibration engine (built separately, shadow-first) fits a correction from this ledger.

This is out-of-sample by CONSTRUCTION (the actual is real future data the predictor never saw), so —
unlike backtesting — it cannot overfit. It is the engine grading itself against reality, every day.

Pure scoring (brier/bias/reliability bins) is isolated for exhaustive edge-case testing. IO never
raises. Recording and scoring are observational — they never affect a live trade.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

_DDL = """
CREATE TABLE IF NOT EXISTS prediction_ledger (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source         TEXT NOT NULL,      -- 'conviction' | 'news' | 'surveillance' | 'model:m4' | ...
    target_key     TEXT NOT NULL,      -- position_id / ticker / event_id
    target_type    TEXT NOT NULL,      -- 'trade' | 'news' | ...
    predicted      REAL NOT NULL,      -- predicted value, normalized to the actual's space (e.g. P(win))
    is_binary      INTEGER NOT NULL DEFAULT 1,   -- 1 = predicted is a probability scored vs {0,1}
    confidence     REAL,
    config_version INTEGER,
    made_at        TEXT NOT NULL,
    actual         REAL,               -- NULL until scored
    gap            REAL,               -- actual - predicted (signed error)
    scored         INTEGER NOT NULL DEFAULT 0,
    scored_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_pl_unscored ON prediction_ledger(scored);
CREATE INDEX IF NOT EXISTS idx_pl_source   ON prediction_ledger(source);
"""


# ── pure scoring ──────────────────────────────────────────────────────────────────────
def brier(predicted_prob: float, actual_binary: float) -> float:
    """Squared error of a probabilistic prediction vs a {0,1} outcome. 0 = perfect, 1 = worst."""
    p = max(0.0, min(1.0, float(predicted_prob)))
    return round((p - (1.0 if actual_binary else 0.0)) ** 2, 6)


def bias(predicted: float, actual: float) -> float:
    """Signed calibration error: actual − predicted. >0 = under-predicted, <0 = over-predicted."""
    return round(float(actual) - float(predicted), 6)


def reliability_bins(pairs: list[tuple[float, float]], n_bins: int = 5) -> list[dict]:
    """Reliability curve for binary predictions: bin by predicted prob, report the empirical actual
    rate per bin. A well-calibrated predictor has actual_rate ≈ bin midpoint. Pure; never raises."""
    if not pairs or n_bins < 1:
        return []
    buckets: dict[int, list[tuple[float, float]]] = {}
    for p, a in pairs:
        p = max(0.0, min(0.999999, float(p)))
        buckets.setdefault(min(n_bins - 1, int(p * n_bins)), []).append((p, a))
    out = []
    for b in sorted(buckets):
        ps = buckets[b]
        out.append({
            "bin": f"{b / n_bins:.0%}-{(b + 1) / n_bins:.0%}",
            "n": len(ps),
            "mean_predicted": round(sum(p for p, _ in ps) / len(ps), 3),
            "actual_rate": round(sum(1 for _, a in ps if a > 0) / len(ps), 3),
        })
    return out


# ── IO: record / score / summarize ────────────────────────────────────────────────────
def record_prediction(db_path: Any, *, source: str, target_key: str, target_type: str,
                      predicted: float, is_binary: bool = True, confidence: float | None = None,
                      config_version: int | None = None) -> bool:
    """Log a prediction at the moment it is made (point-in-time). Idempotent per (source, target_key):
    a re-record updates the prediction only while still unscored. Returns True on success; never raises."""
    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        conn.executescript(_DDL)
        existing = conn.execute(
            "SELECT id, scored FROM prediction_ledger WHERE source=? AND target_key=?",
            (source, str(target_key))).fetchone()
        if existing and existing[1] == 0:
            conn.execute("UPDATE prediction_ledger SET predicted=?, confidence=?, config_version=?, "
                         "made_at=? WHERE id=?",
                         (float(predicted), confidence, config_version,
                          datetime.now(UTC).isoformat(), existing[0]))
        elif not existing:
            conn.execute(
                "INSERT INTO prediction_ledger (source, target_key, target_type, predicted, is_binary, "
                "confidence, config_version, made_at) VALUES (?,?,?,?,?,?,?,?)",
                (source, str(target_key), target_type, float(predicted), 1 if is_binary else 0,
                 confidence, config_version, datetime.now(UTC).isoformat()))
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False


def score_predictions(db_path: Any, *, source: str,
                      actual_resolver: Callable[[str], float | None]) -> dict:
    """Resolve the actual outcome for unscored predictions of `source` (actual_resolver(target_key) →
    actual or None if not yet known) and record the gap. Never raises."""
    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        conn.executescript(_DDL)
        rows = conn.execute(
            "SELECT id, target_key, predicted FROM prediction_ledger WHERE scored=0 AND source=?",
            (source,)).fetchall()
        scored = 0
        for pid, tkey, predicted in rows:
            try:
                actual = actual_resolver(tkey)
            except Exception:
                actual = None
            if actual is None:
                continue
            conn.execute("UPDATE prediction_ledger SET actual=?, gap=?, scored=1, scored_at=? WHERE id=?",
                         (float(actual), bias(predicted, actual), datetime.now(UTC).isoformat(), pid))
            scored += 1
        conn.commit()
        conn.close()
        return {"status": "ok", "scored": scored, "pending": len(rows) - scored}
    except Exception:
        return {"status": "error"}


def calibration_summary(db_path: Any, *, min_n: int = 10) -> list[dict]:
    """Per-source rolling calibration over SCORED predictions: n, mean Brier (binary), mean bias, and
    the actual win-rate vs mean predicted. Only sources with ≥ min_n scored predictions. Never raises."""
    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        conn.executescript(_DDL)
        rows = conn.execute(
            "SELECT source, predicted, actual, is_binary FROM prediction_ledger "
            "WHERE scored=1 AND actual IS NOT NULL").fetchall()
        conn.close()
    except Exception:
        return []
    by_src: dict[str, list[tuple[float, float, int]]] = {}
    for src, pred, act, isb in rows:
        by_src.setdefault(src, []).append((pred, act, isb))
    out = []
    for src, recs in by_src.items():
        if len(recs) < min_n:
            continue
        n = len(recs)
        binary = all(b for _, _, b in recs)
        mean_pred = sum(p for p, _, _ in recs) / n
        mean_act = sum(a for _, a, _ in recs) / n
        mean_bias = round(mean_act - mean_pred, 4)
        row = {"source": src, "n": n, "mean_predicted": round(mean_pred, 3),
               "actual_rate": round(mean_act, 3), "mean_bias": mean_bias}
        if binary:
            row["brier"] = round(sum(brier(p, a) for p, a, _ in recs) / n, 4)
            # anti-predictive flag: predictor is INVERTED if higher predicted → lower actual
            hi = [a for p, a, _ in recs if p >= mean_pred]
            lo = [a for p, a, _ in recs if p < mean_pred]
            if hi and lo:
                row["inverted"] = (sum(hi) / len(hi)) < (sum(lo) / len(lo))
        out.append(row)
    out.sort(key=lambda d: -d.get("brier", 0))
    return out


def recent_scored(db_path: Any, limit: int = 30) -> list[dict]:
    """Most recently scored predictions (for the dashboard). Never raises."""
    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        conn.executescript(_DDL)
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT source, target_key, predicted, actual, gap, config_version, scored_at "
            "FROM prediction_ledger WHERE scored=1 ORDER BY id DESC LIMIT ?", (limit,))]
        conn.close()
        return rows
    except Exception:
        return []

"""
agora/ops/model_runner.py — Phase 0c: the scheduled model-runner the whole fleet plugs into.

A model is a pure function `fn(db_path) -> dict` with a name + a cadence (days). The runner:
  • runs each model when due (persisted last-run in `model_runs`, so cadence survives restarts —
    the same wall-clock discipline as the weekly markers, NOT monotonic time);
  • logs every run to `model_runs` (status, n_samples, readiness, metrics_json, summary);
  • offers `write_scores()` so models persist per-entity outputs into `model_scores`
    (e.g. M1 fill-prob per order, M2 regime per ticker) for the engine/UI to read.

Guarantees: READ-ONLY on trading data, writes ONLY to model_runs / model_scores, never raises,
isolated try/except per model → a model failure can never disturb the scheduler or execution.
Models that need labels self-report readiness (BOOTSTRAP/EMERGING/TRAINABLE) and refuse to emit
live-actionable scores below their n-gate — so nothing ever trains/acts on too-few samples.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS model_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    model_name    TEXT NOT NULL,
    run_ts_utc    TEXT NOT NULL,
    status        TEXT,                -- ok | error | skipped
    n_samples     INTEGER,
    readiness     TEXT,                -- BOOTSTRAP | EMERGING | TRAINABLE | N/A
    metrics_json  TEXT,
    summary       TEXT
);
CREATE INDEX IF NOT EXISTS idx_mr_name ON model_runs(model_name);

CREATE TABLE IF NOT EXISTS model_scores (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    model_name   TEXT NOT NULL,
    entity_type  TEXT,                 -- 'position' | 'ticker' | 'order' | 'global'
    entity_id    TEXT,
    score        REAL,
    score_date   TEXT,
    meta_json    TEXT,
    UNIQUE(model_name, entity_type, entity_id, score_date)
);
CREATE INDEX IF NOT EXISTS idx_ms_name ON model_scores(model_name);
"""

# Readiness thresholds on LABELED real-close samples (shared with the feature store).
BOOTSTRAP_MAX = 20
EMERGING_MAX = 50


def readiness_for(n_labeled: int) -> str:
    if n_labeled < BOOTSTRAP_MAX:
        return "BOOTSTRAP"
    if n_labeled < EMERGING_MAX:
        return "EMERGING"
    return "TRAINABLE"


def _ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)


def write_scores(db_path: str, model_name: str, rows: list[dict[str, Any]], score_date: str | None = None) -> int:
    """Persist per-entity scores. rows = [{entity_type, entity_id, score, meta}]. Idempotent/day."""
    sd = score_date or datetime.now(UTC).date().isoformat()
    n = 0
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        _ensure(conn)
        for r in rows:
            conn.execute(
                """INSERT OR REPLACE INTO model_scores
                   (model_name, entity_type, entity_id, score, score_date, meta_json)
                   VALUES (?,?,?,?,?,?)""",
                (model_name, r.get("entity_type", "global"), str(r.get("entity_id", "")),
                 r.get("score"), sd, json.dumps(r.get("meta", {}), default=str)),
            )
            n += 1
        conn.commit(); conn.close()
    except Exception as exc:
        logger.debug("write_scores(%s) failed: %s", model_name, exc)
    return n


def _last_run_age_days(conn: sqlite3.Connection, model_name: str) -> float | None:
    row = conn.execute(
        "SELECT MAX(run_ts_utc) FROM model_runs WHERE model_name=? AND status='ok'", (model_name,)
    ).fetchone()
    if not row or not row[0]:
        return None
    try:
        return (datetime.now(UTC) - datetime.fromisoformat(row[0])).total_seconds() / 86400.0
    except Exception:
        return None


# ── A model spec ──────────────────────────────────────────────────────────────
# {"name": str, "cadence_days": float, "fn": Callable[[str], dict]}
# fn returns {status, n_samples, readiness, metrics(dict), summary(str), scores(optional list)}

def _model_dataset_health(db_path: str) -> dict[str, Any]:
    """Built-in model that exercises the pipeline + monitors training-set readiness from the feature
    store. Pure read; the fleet's 'are we ready to train' gauge."""
    conn = sqlite3.connect(db_path, timeout=8); conn.row_factory = sqlite3.Row
    try:
        has = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='trade_features'").fetchone()[0]
        if not has:
            return {"status": "skipped", "n_samples": 0, "readiness": "BOOTSTRAP",
                    "metrics": {}, "summary": "feature store not built yet"}
        rows = conn.execute("SELECT COUNT(*) FROM trade_features").fetchone()[0]
        lab = conn.execute("SELECT COUNT(*) FROM trade_features WHERE win IS NOT NULL").fetchone()[0]
        wins = conn.execute("SELECT COUNT(*) FROM trade_features WHERE win=1").fetchone()[0]
        path = conn.execute("SELECT COUNT(*) FROM trade_features WHERE n_frames IS NOT NULL").fetchone()[0]
        by_cls = {r["structure_class"]: r["lab"] for r in conn.execute(
            "SELECT structure_class, SUM(win IS NOT NULL) lab FROM trade_features GROUP BY structure_class")}
        rd = readiness_for(lab)
        return {
            "status": "ok", "n_samples": lab, "readiness": rd,
            "metrics": {"total_rows": rows, "labeled": lab, "wins": wins, "losses": lab - wins,
                        "win_rate": round(wins / lab, 3) if lab else None,
                        "path_coverage": path, "labeled_by_structure": by_cls},
            "summary": f"{lab} labeled / {rows} rows · {rd} · need {max(0, EMERGING_MAX - lab)} more for EMERGING",
        }
    finally:
        conn.close()


MODEL_REGISTRY: list[dict[str, Any]] = [
    {"name": "dataset_health", "cadence_days": 0.0, "fn": _model_dataset_health},  # every cycle
]


def register_model(name: str, cadence_days: float, fn: Callable[[str], dict]) -> None:
    """Append a model to the registry (used by M1..M8 modules at import)."""
    if not any(m["name"] == name for m in MODEL_REGISTRY):
        MODEL_REGISTRY.append({"name": name, "cadence_days": cadence_days, "fn": fn})


def run_due_models(db_path: str) -> dict[str, Any]:
    """Run every model whose cadence is due; log each to model_runs. Never raises."""
    ran: list[str] = []
    try:
        # Load the fleet (M1..M8 self-register on import; idempotent via register_model dedupe).
        try:
            import agora.ops.ml_models  # noqa: F401
        except Exception as _impexc:
            logger.debug("ml_models import skipped: %s", _impexc)
        conn = sqlite3.connect(db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        _ensure(conn)
        for spec in list(MODEL_REGISTRY):
            name = spec["name"]
            try:
                age = _last_run_age_days(conn, name)
                if age is not None and age < spec.get("cadence_days", 0.0):
                    continue  # not due yet
                res = spec["fn"](db_path)
                conn.execute(
                    """INSERT INTO model_runs (model_name, run_ts_utc, status, n_samples, readiness,
                       metrics_json, summary) VALUES (?,?,?,?,?,?,?)""",
                    (name, datetime.now(UTC).isoformat(timespec="seconds"),
                     res.get("status", "ok"), res.get("n_samples"), res.get("readiness"),
                     json.dumps(res.get("metrics", {}), default=str), res.get("summary", "")),
                )
                conn.commit()
                if res.get("scores"):
                    write_scores(db_path, name, res["scores"])
                ran.append(name)
            except Exception as mexc:
                logger.debug("model %s failed: %s", name, mexc)
                try:
                    conn.execute(
                        "INSERT INTO model_runs (model_name, run_ts_utc, status, summary) VALUES (?,?,?,?)",
                        (name, datetime.now(UTC).isoformat(timespec="seconds"), "error", str(mexc)))
                    conn.commit()
                except Exception:
                    pass
        conn.close()
        return {"ran": ran, "registered": len(MODEL_REGISTRY)}
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("run_due_models failed: %s", exc)
        return {"ran": ran, "error": str(exc)}

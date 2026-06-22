"""
agora/tests/test_model_runner.py — Phase 0c model-runner. Verifies cadence gating (persisted),
score persistence, readiness thresholds, and that a failing model is isolated (logged error, never
raises, other models still run). Pure over a temp DB.
"""
from __future__ import annotations

import sqlite3
import tempfile

from agora.ops import model_runner as MR


def _db():
    return tempfile.NamedTemporaryFile(suffix=".db", delete=False).name


def test_readiness_thresholds():
    assert MR.readiness_for(0) == "BOOTSTRAP"
    assert MR.readiness_for(19) == "BOOTSTRAP"
    assert MR.readiness_for(20) == "EMERGING"
    assert MR.readiness_for(49) == "EMERGING"
    assert MR.readiness_for(50) == "TRAINABLE"


def test_runs_and_logs(monkeypatch):
    db = _db()
    calls = {"n": 0}

    def _m(dbp):
        calls["n"] += 1
        return {"status": "ok", "n_samples": 7, "readiness": "BOOTSTRAP", "metrics": {"x": 1},
                "summary": "hi", "scores": [{"entity_type": "ticker", "entity_id": "AAA", "score": 0.5}]}

    monkeypatch.setattr(MR, "MODEL_REGISTRY", [{"name": "t1", "cadence_days": 1.0, "fn": _m}])
    r = MR.run_due_models(db)
    assert r["ran"] == ["t1"] and calls["n"] == 1
    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM model_runs WHERE model_name='t1' AND status='ok'").fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM model_scores WHERE model_name='t1'").fetchone()[0] == 1
    # cadence: a second immediate run is NOT due (1-day cadence, just ran)
    MR.run_due_models(db)
    assert calls["n"] == 1  # not re-run


def test_cadence_zero_always_runs(monkeypatch):
    db = _db()
    calls = {"n": 0}
    def _m(dbp):
        calls["n"] += 1
        return {"status": "ok", "n_samples": 0, "readiness": "BOOTSTRAP", "metrics": {}, "summary": ""}
    monkeypatch.setattr(MR, "MODEL_REGISTRY", [{"name": "t0", "cadence_days": 0.0, "fn": _m}])
    MR.run_due_models(db); MR.run_due_models(db)
    assert calls["n"] == 2  # cadence 0 → runs every time


def test_failing_model_isolated(monkeypatch):
    db = _db()
    def _bad(dbp):
        raise RuntimeError("boom")
    def _good(dbp):
        return {"status": "ok", "n_samples": 1, "readiness": "BOOTSTRAP", "metrics": {}, "summary": "ok"}
    monkeypatch.setattr(MR, "MODEL_REGISTRY",
                        [{"name": "bad", "cadence_days": 0.0, "fn": _bad},
                         {"name": "good", "cadence_days": 0.0, "fn": _good}])
    r = MR.run_due_models(db)              # must NOT raise
    assert "good" in r["ran"]              # good still ran despite bad failing
    c = sqlite3.connect(db)
    assert c.execute("SELECT status FROM model_runs WHERE model_name='bad'").fetchone()[0] == "error"


def test_register_model_dedupes():
    before = len(MR.MODEL_REGISTRY)
    MR.register_model("dataset_health", 1.0, lambda d: {})   # already registered
    assert len(MR.MODEL_REGISTRY) == before

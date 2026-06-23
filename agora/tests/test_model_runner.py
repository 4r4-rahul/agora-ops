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
    # run_due_models lazily `import agora.ops.ml_models`, whose M1..M8 self-register into the
    # registry — repopulating our isolated [t1] (order-dependent: only latent if ml_models
    # wasn't already imported). Neutralize registration so this test stays hermetic.
    monkeypatch.setattr(MR, "register_model", lambda *a, **k: None)
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


# ── next_tier_gap: the readiness progress message must target the CORRECT next tier ──
def test_next_tier_gap_targets_correct_tier():
    # BOOTSTRAP → counts toward EMERGING (at BOOTSTRAP_MAX), not the old always-EMERGING_MAX bug.
    assert MR.next_tier_gap(0)  == ("EMERGING", MR.BOOTSTRAP_MAX)
    assert MR.next_tier_gap(8)  == ("EMERGING", MR.BOOTSTRAP_MAX - 8)
    # EMERGING → counts toward TRAINABLE (the bug mislabeled this "for EMERGING").
    assert MR.next_tier_gap(20) == ("TRAINABLE", MR.EMERGING_MAX - 20)
    assert MR.next_tier_gap(49) == ("TRAINABLE", 1)
    # TRAINABLE → nothing more needed.
    assert MR.next_tier_gap(50)  == (None, 0)
    assert MR.next_tier_gap(500) == (None, 0)


def test_next_tier_gap_consistent_with_readiness():
    for n in (0, 19, 20, 49, 50, 200):
        nxt, _ = MR.next_tier_gap(n)
        if MR.readiness_for(n) == "TRAINABLE":
            assert nxt is None
        else:
            assert nxt == ("EMERGING" if MR.readiness_for(n) == "BOOTSTRAP" else "TRAINABLE")

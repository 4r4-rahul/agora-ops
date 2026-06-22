"""
agora/ops/model_analyst.py — the Model Analyst: turns the model fleet's scores into RANKED, ACTIONABLE
system-improvement recommendations. This is the loop-closer: models → analyst → "here's what to change".

DETERMINISTIC by design — every recommendation is a rule grounded in a concrete model score with the
evidence attached (no LLM, no hallucination; today's lesson). READ-ONLY on model_scores/model_runs,
writes only model_recommendations. Never raises. Cannot affect execution — recommendations are advisory
(surfaced to UI + C-suite); a human/board decides whether to act, exactly like every gate change today.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS model_recommendations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_date  TEXT NOT NULL,
    category      TEXT,             -- execution | universe | data | edge
    severity      TEXT,             -- high | medium | low | info
    source_model  TEXT,
    finding       TEXT,
    recommendation TEXT,
    evidence_json TEXT,
    status        TEXT DEFAULT 'open',
    UNIQUE(created_date, category, finding)
);
CREATE INDEX IF NOT EXISTS idx_rec_date ON model_recommendations(created_date);
"""

_SEV_RANK = {"high": 0, "medium": 1, "low": 2, "info": 3}

# thresholds
_FILL_POOR = 0.05        # strategy fill rate below this is "poor"
_FILL_MIN_N = 40         # need enough attempts before flagging a structure
_DEAD_MIN_N = 15         # need enough attempts before calling a ticker "dead"


def _latest_scores(conn: sqlite3.Connection, model: str) -> list[dict[str, Any]]:
    """Latest score per entity for a model (most recent score_date)."""
    rows = conn.execute(
        """SELECT s.entity_id, s.score, s.meta_json FROM model_scores s
           WHERE s.model_name=? AND s.score_date=(SELECT MAX(score_date) FROM model_scores WHERE model_name=?)""",
        (model, model)).fetchall()
    out = []
    for r in rows:
        try:
            meta = json.loads(r["meta_json"]) if r["meta_json"] else {}
        except Exception:
            meta = {}
        out.append({"entity_id": r["entity_id"], "score": r["score"], "meta": meta})
    return out


def analyze_models(db_path: str) -> dict[str, Any]:
    """Build today's recommendations from the latest model scores. Idempotent per day. Never raises."""
    today = datetime.now(UTC).date().isoformat()
    recs: list[dict[str, Any]] = []
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_DDL)
        conn.row_factory = sqlite3.Row
        if not conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='model_scores'").fetchone()[0]:
            conn.close()
            return {"recommendations": 0, "note": "no model scores yet"}

        # ── execution: poorly-filling structures (M1) ──
        for s in _latest_scores(conn, "fill_model"):
            m = s["meta"]
            sc = s["score"]  # NB: must check `is not None` — 0.0 is falsy, so `sc or 1` would mask 0%
            if (m.get("n") or 0) >= _FILL_MIN_N and sc is not None and sc < _FILL_POOR:
                rej = (m.get("top_reject") or "").lower()
                if "201" in rej:
                    action = "Reject is the historical Error-201 combo block (fixed 2026-06-08) — verify it is not recurring; no live change."
                elif "protective long leg" in rej or "unfilled" in rej:
                    action = "Paper-sim fill latency — the 4-min paper window helps; do NOT widen the cross (slippage). Validate against live fills before tuning."
                else:
                    action = "Investigate routing/limit aggressiveness for this structure; consider down-weighting it in paper until fills improve."
                recs.append({"category": "execution", "severity": "high", "source_model": "fill_model",
                             "finding": f"{s['entity_id']} fills at {s['score']*100:.0f}% (n={m.get('n')}, top reject: {m.get('top_reject')})",
                             "recommendation": action, "evidence": m})

        # ── universe: tickers that never fill (M3) ──
        dead = [s for s in _latest_scores(conn, "liquidity_model")
                if (s["meta"].get("n") or 0) >= _DEAD_MIN_N and s["score"] is not None and s["score"] == 0.0]
        if dead:
            names = ", ".join(sorted(s["entity_id"] for s in dead)[:12])
            recs.append({"category": "universe", "severity": "medium", "source_model": "liquidity_model",
                         "finding": f"{len(dead)} ticker(s) never filled over their recent attempts: {names}",
                         "recommendation": "Down-weight or pause these names in the active universe until fills improve — spending discovery/LLM on un-fillable names is wasted effort.",
                         "evidence": {"dead_tickers": [s["entity_id"] for s in dead]}})

        # ── data: training-set readiness (dataset_health) ──
        dh = conn.execute(
            "SELECT n_samples, readiness FROM model_runs WHERE model_name='dataset_health' "
            "AND status='ok' ORDER BY id DESC LIMIT 1").fetchone()
        if dh and dh["readiness"] == "BOOTSTRAP":
            recs.append({"category": "data", "severity": "info", "source_model": "dataset_health",
                         "finding": f"Training set is BOOTSTRAP ({dh['n_samples']} labeled real closes).",
                         "recommendation": "Keep accumulating outcomes; do NOT enable any ML-based GATE until EMERGING (20+ labeled). Predictive models stay shadow-only.",
                         "evidence": {"labeled": dh["n_samples"]}})

        # ── edge: current vol regime → credit-vs-debit bias (M2) ──
        cf = next((s for s in _latest_scores(conn, "regime_model")
                   if s["entity_id"] == "credit_favorability"), None)
        if cf and cf["score"] is not None:
            m = cf["meta"]
            if cf["score"] >= 0.6:
                recs.append({"category": "edge", "severity": "medium", "source_model": "regime_model",
                             "finding": f"Vol regime is {m.get('vol_regime')} (VIX {m.get('vix')}, {m.get('term_state')}) — premium is rich.",
                             "recommendation": "Favor CREDIT spreads; down-weight new directional DEBITS (IV-crush risk).",
                             "evidence": m})
            elif cf["score"] <= 0.35:
                recs.append({"category": "edge", "severity": "low", "source_model": "regime_model",
                             "finding": f"Vol regime is {m.get('vol_regime')} (VIX {m.get('vix')}) — premium is cheap.",
                             "recommendation": "Directional DEBITS are acceptable here; credit spreads collect little premium.",
                             "evidence": m})

        # persist (idempotent per day) + rank
        for r in recs:
            conn.execute(
                """INSERT OR IGNORE INTO model_recommendations
                   (created_date, category, severity, source_model, finding, recommendation, evidence_json)
                   VALUES (?,?,?,?,?,?,?)""",
                (today, r["category"], r["severity"], r["source_model"], r["finding"],
                 r["recommendation"], json.dumps(r.get("evidence", {}), default=str)))
        conn.commit()
        conn.close()
        recs.sort(key=lambda r: _SEV_RANK.get(r["severity"], 9))
        return {"recommendations": len(recs), "by_severity": {
            sev: sum(1 for r in recs if r["severity"] == sev) for sev in ("high", "medium", "low", "info")}}
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("analyze_models failed: %s", exc)
        return {"recommendations": len(recs), "error": str(exc)}

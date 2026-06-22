"""
agora/tests/test_model_analyst.py — the Model Analyst. Verifies it turns model scores into the right
deterministic recommendations (poor-fill structures, dead tickers, bootstrap data), is idempotent per
day, and never raises. Pure over a temp DB.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
from datetime import UTC, datetime

from agora.ops.model_analyst import analyze_models


def _db():
    p = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    c = sqlite3.connect(p)
    c.execute("""CREATE TABLE model_scores (id INTEGER PRIMARY KEY, model_name TEXT, entity_type TEXT,
        entity_id TEXT, score REAL, score_date TEXT, meta_json TEXT)""")
    c.execute("""CREATE TABLE model_runs (id INTEGER PRIMARY KEY, model_name TEXT, run_ts_utc TEXT,
        status TEXT, n_samples INTEGER, readiness TEXT, metrics_json TEXT, summary TEXT)""")
    sd = datetime.now(UTC).date().isoformat()
    # M1 fill scores: one poor (n big), one fine
    c.execute("INSERT INTO model_scores (model_name,entity_type,entity_id,score,score_date,meta_json) VALUES (?,?,?,?,?,?)",
              ("fill_model", "strategy", "bull_call_spread", 0.01, sd,
               json.dumps({"n": 467, "top_reject": "Unfilled after walking mid"})))
    c.execute("INSERT INTO model_scores (model_name,entity_type,entity_id,score,score_date,meta_json) VALUES (?,?,?,?,?,?)",
              ("fill_model", "strategy", "long_call", 0.10, sd, json.dumps({"n": 191, "top_reject": None})))
    # M3 liquidity: a dead ticker (0%, n>=15) and a live one
    c.execute("INSERT INTO model_scores (model_name,entity_type,entity_id,score,score_date,meta_json) VALUES (?,?,?,?,?,?)",
              ("liquidity_model", "ticker", "XLE", 0.0, sd, json.dumps({"n": 20})))
    c.execute("INSERT INTO model_scores (model_name,entity_type,entity_id,score,score_date,meta_json) VALUES (?,?,?,?,?,?)",
              ("liquidity_model", "ticker", "TSLA", 0.12, sd, json.dumps({"n": 36})))
    # dataset_health BOOTSTRAP
    c.execute("INSERT INTO model_runs (model_name,run_ts_utc,status,n_samples,readiness) VALUES (?,?,?,?,?)",
              ("dataset_health", datetime.now(UTC).isoformat(), "ok", 13, "BOOTSTRAP"))
    c.commit(); c.close()
    return p


def test_generates_grounded_recs():
    db = _db()
    r = analyze_models(db)
    assert r["recommendations"] == 3   # poor-fill + dead-ticker + bootstrap
    c = sqlite3.connect(db); c.row_factory = sqlite3.Row
    recs = {(x["category"], x["source_model"]): x for x in c.execute("SELECT * FROM model_recommendations")}
    # poor fill → execution/high
    ex = recs[("execution", "fill_model")]
    assert ex["severity"] == "high" and "bull_call_spread fills at 1%" in ex["finding"]
    # dead ticker → universe/medium, XLE named
    un = recs[("universe", "liquidity_model")]
    assert un["severity"] == "medium" and "XLE" in un["finding"]
    # bootstrap → data/info
    da = recs[("data", "dataset_health")]
    assert da["severity"] == "info" and "BOOTSTRAP" in da["finding"]
    # long_call (10% fill, fine) does NOT generate a poor-fill rec
    assert not any("long_call fills" in x["finding"] for x in recs.values())


def test_idempotent_per_day():
    db = _db()
    analyze_models(db); analyze_models(db)   # re-run same day
    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM model_recommendations").fetchone()[0] == 3  # no dupes


def test_error_safe():
    assert analyze_models("/nonexistent/x.db").get("recommendations") == 0

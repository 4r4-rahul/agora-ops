"""
agora/tests/test_m1_fill.py — M1 fill model. Verifies it computes per-strategy fill rates from
execution_quality, windows to recent attempts, emits scores, ranks hardest/easiest, and self-registers.
Pure over a temp DB.
"""
from __future__ import annotations

import sqlite3
import tempfile
from datetime import date, timedelta

from agora.ops.ml_models.m1_fill import fill_model


def _db(rows):
    p = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    c = sqlite3.connect(p)
    c.execute("""CREATE TABLE execution_quality (id INTEGER PRIMARY KEY, attempt_date TEXT, ticker TEXT,
        strategy TEXT, mid_price REAL, outcome TEXT, fill_price REAL, slippage_ticks REAL,
        reject_code TEXT, reject_reason TEXT)""")
    c.executemany(
        "INSERT INTO execution_quality (attempt_date,strategy,outcome,slippage_ticks,reject_reason) VALUES (?,?,?,?,?)",
        rows)
    c.commit(); c.close()
    return p


def test_per_strategy_fill_rate_and_ranking():
    today = date.today().isoformat()
    rows = []
    # long_call: 12 attempts, 6 fills → 50%
    rows += [(today, "long_call", "fill", -0.3, None)] * 6 + [(today, "long_call", "timeout", None, None)] * 6
    # bull_put_spread: 20 attempts, 1 fill → 5% (hardest)
    rows += [(today, "bull_put_spread", "fill", -2.0, None)] * 1 + \
            [(today, "bull_put_spread", "reject", None, "Error 201 riskless")] * 5 + \
            [(today, "bull_put_spread", "timeout", None, None)] * 14
    db = _db(rows)
    r = fill_model(db)
    assert r["status"] == "ok"
    bys = r["metrics"]["by_strategy"]
    assert bys["long_call"]["fill_rate"] == 0.5
    assert bys["bull_put_spread"]["fill_rate"] == 0.05
    assert bys["bull_put_spread"]["top_reject"].startswith("Error 201")
    assert "hardest bull_put_spread" in r["summary"]
    # scores emitted per strategy
    assert {s["entity_id"] for s in r["scores"]} == {"long_call", "bull_put_spread"}


def test_min_per_strategy_filter():
    today = date.today().isoformat()
    rows = [(today, "rare_strat", "fill", 0.0, None)] * 3   # below _MIN_PER_STRAT (10)
    rows += [(today, "long_call", "fill", 0.0, None)] * 15
    r = fill_model(_db(rows))
    assert "rare_strat" not in r["metrics"]["by_strategy"]   # filtered (too few)
    assert "long_call" in r["metrics"]["by_strategy"]


def test_falls_back_to_all_time_when_recent_sparse():
    old = (date.today() - timedelta(days=60)).isoformat()
    rows = [(old, "long_call", "fill", 0.0, None)] * 15      # all old → recent window empty
    r = fill_model(_db(rows))
    assert r["metrics"]["window"] == "all-time"
    assert r["metrics"]["by_strategy"]["long_call"]["fill_rate"] == 1.0


def test_registered():
    import agora.ops.ml_models  # noqa: F401
    from agora.ops.model_runner import MODEL_REGISTRY
    assert any(m["name"] == "fill_model" for m in MODEL_REGISTRY)


def test_error_safe():
    r = fill_model("/nonexistent/x.db")
    assert r.get("status") in ("skipped", None) or "error" in str(r)

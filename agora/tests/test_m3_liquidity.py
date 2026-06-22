"""
agora/tests/test_m3_liquidity.py — M3 liquidity model: per-ticker fill rate, price-bucket effect,
ranking, registration. Pure over a temp DB.
"""
from __future__ import annotations

import sqlite3
import tempfile
from datetime import date

from agora.ops.ml_models.m3_liquidity import liquidity_model


def _db(rows):
    p = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    c = sqlite3.connect(p)
    c.execute("""CREATE TABLE execution_quality (id INTEGER PRIMARY KEY, attempt_date TEXT, ticker TEXT,
        strategy TEXT, mid_price REAL, outcome TEXT, fill_price REAL, slippage_ticks REAL,
        reject_code TEXT, reject_reason TEXT)""")
    c.executemany(
        "INSERT INTO execution_quality (attempt_date,ticker,mid_price,outcome,slippage_ticks) VALUES (?,?,?,?,?)",
        rows)
    c.commit(); c.close()
    return p


def test_per_ticker_fill_and_ranking():
    t = date.today().isoformat()
    rows = []
    # AAA: 20 attempts, 10 fill → 50% (most liquid)
    rows += [(t, "AAA", 1.0, "fill", 0.0)] * 10 + [(t, "AAA", 1.0, "timeout", None)] * 10
    # BBB: 20 attempts, 1 fill → 5% (least)
    rows += [(t, "BBB", 1.0, "fill", 0.0)] * 1 + [(t, "BBB", 1.0, "timeout", None)] * 19
    r = liquidity_model(_db(rows))
    assert r["status"] == "ok"
    bt = r["metrics"]["by_ticker"]
    assert bt["AAA"]["fill_rate"] == 0.5 and bt["BBB"]["fill_rate"] == 0.05
    assert "most-liquid AAA" in r["summary"] and "least BBB" in r["summary"]
    assert {s["entity_id"] for s in r["scores"]} == {"AAA", "BBB"}


def test_min_per_ticker_filter():
    t = date.today().isoformat()
    rows = [(t, "RARE", 1.0, "fill", 0.0)] * 5      # below _MIN_PER_TICKER (12)
    rows += [(t, "AAA", 1.0, "fill", 0.0)] * 15
    r = liquidity_model(_db(rows))
    assert "RARE" not in r["metrics"]["by_ticker"]
    assert "AAA" in r["metrics"]["by_ticker"]


def test_price_buckets_present():
    t = date.today().isoformat()
    # 60 attempts across a price range → tertile buckets computed
    rows = [(t, "AAA", float(i % 30) + 0.5, "fill" if i % 3 == 0 else "timeout", 0.0) for i in range(60)]
    r = liquidity_model(_db(rows))
    pb = r["metrics"]["fill_by_price_bucket"]
    assert set(pb.keys()) == {"cheap", "mid", "expensive"}


def test_registered():
    import agora.ops.ml_models  # noqa: F401
    from agora.ops.model_runner import MODEL_REGISTRY
    assert any(m["name"] == "liquidity_model" for m in MODEL_REGISTRY)


def test_error_safe():
    assert liquidity_model("/nonexistent/x.db").get("status") in ("error", "skipped")

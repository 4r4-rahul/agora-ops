"""
agora/tests/test_feature_store.py — Phase 0b unified feature table. Verifies it materializes one row
per position, labels ONLY real post-cutoff closes (no fabricated/legacy), derives structure_class +
path features, and is idempotent. Pure + read-only over a temp DB.
"""
from __future__ import annotations

import sqlite3
import tempfile

from agora.ops.feature_store import _structure_class, build_feature_store


def _db():
    p = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    c = sqlite3.connect(p)
    c.execute("""CREATE TABLE positions (position_id TEXT, ticker TEXT, strategy TEXT, pillar TEXT,
        direction TEXT, status TEXT, entry_price REAL, max_loss_dollars REAL, max_gain_dollars REAL,
        entry_date TEXT, expiry_date TEXT, conviction_at_entry REAL, regime_at_entry TEXT,
        realized_pnl REAL, close_date TEXT, close_source TEXT)""")
    c.execute("CREATE TABLE decision_chains (chain_id TEXT, position_id TEXT, triggered_by TEXT, gates_passed TEXT, started_at TEXT)")
    c.execute("""CREATE TABLE lifecycle_snapshots (position_id TEXT, snapshot_date TEXT, days_held INTEGER,
        unrealized_pct_risk REAL, captured_pct_gain REAL, net_delta REAL, net_theta REAL)""")
    c.executemany("INSERT INTO positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
        # real win post-cutoff (labeled win=1)
        ("w1", "AAA", "bull_put_spread", "vol_premium", "bullish", "closed", -1.5, 350, 150,
         "2026-06-13", "2026-07-17", 55.0, "neutral", 120.0, "2026-06-20", "thesis_exit"),
        # real loss post-cutoff (labeled win=0)
        ("l1", "BBB", "long_call", "directional", "bullish", "closed", 5.0, 500, 1500,
         "2026-06-14", "2026-07-17", 70.0, "neutral", -200.0, "2026-06-21", "stop_loss"),
        # FABRICATED close → must NOT be labeled
        ("f1", "CCC", "long_put", "directional", "bearish", "closed", 3.0, 300, 900,
         "2026-06-15", "2026-07-17", 40.0, "neutral", 0.0, "2026-06-20", "fabricated_unfilled"),
        # PRE-cutoff close → must NOT be labeled
        ("p1", "DDD", "bull_call_spread", "directional", "bullish", "closed", 2.0, 200, 300,
         "2026-06-01", "2026-06-15", 60.0, "neutral", -50.0, "2026-06-10", "thesis_exit"),
        # open → no label
        ("o1", "EEE", "bear_call_spread", "vol_premium", "bearish", "open", -2.0, 800, 200,
         "2026-06-19", "2026-07-17", 45.0, "neutral", None, "", ""),
    ])
    c.execute("INSERT INTO decision_chains VALUES ('ch_w1','w1','catalyst','[\"timing\",\"risk\",\"liquidity\"]','2026-06-13')")
    c.executemany("INSERT INTO lifecycle_snapshots VALUES (?,?,?,?,?,?,?)", [
        ("w1", "2026-06-14", 1, -0.10, 0.20, 0.05, 0.01),
        ("w1", "2026-06-18", 5, -0.40, 0.65, 0.03, 0.02),   # max adverse -0.40, max favorable 0.65
    ])
    c.commit(); c.close()
    return p


def test_structure_class():
    assert _structure_class("bull_put_spread", True) == "credit_spread"
    assert _structure_class("bear_put_spread", False) == "debit_spread"
    assert _structure_class("long_call", False) == "long_option"


def test_labels_only_real_postcutoff_closes():
    db = _db()
    r = build_feature_store(db, legacy_cutoff="2026-06-12")
    assert r["rows"] == 5 and r["labeled"] == 2          # w1 + l1 only
    c = sqlite3.connect(db); c.row_factory = sqlite3.Row
    tf = {x["position_id"]: x for x in c.execute("SELECT * FROM trade_features")}
    assert tf["w1"]["win"] == 1 and tf["l1"]["win"] == 0
    assert tf["f1"]["win"] is None   # fabricated excluded
    assert tf["p1"]["win"] is None   # pre-cutoff excluded
    assert tf["o1"]["win"] is None   # open


def test_derived_and_path_features():
    db = _db()
    build_feature_store(db, legacy_cutoff="2026-06-12")
    c = sqlite3.connect(db); c.row_factory = sqlite3.Row
    w1 = c.execute("SELECT * FROM trade_features WHERE position_id='w1'").fetchone()
    assert w1["structure_class"] == "credit_spread" and w1["is_credit"] == 1
    assert w1["triggered_by"] == "catalyst" and w1["gates_passed_n"] == 3
    assert w1["max_adverse_pct"] == -0.40 and w1["max_favorable_pct"] == 0.65 and w1["days_held"] == 5
    assert w1["return_on_risk"] == round(120.0 / 350, 4)


def test_idempotent():
    db = _db()
    build_feature_store(db); build_feature_store(db)   # re-run
    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM trade_features").fetchone()[0] == 5  # no dupes


def test_error_safe():
    assert build_feature_store("/nonexistent/x.db").get("rows") == 0

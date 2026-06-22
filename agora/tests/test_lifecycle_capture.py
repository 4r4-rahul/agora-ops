"""
agora/tests/test_lifecycle_capture.py — the M8 daily "film" capture. Verifies it snapshots open
positions, computes net greeks from legs, captures the closing frame, and is idempotent per day.
Pure + read-only over a temp DB; no execution path touched.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile

from agora.ops.lifecycle_capture import _net_greeks, capture_lifecycle_snapshots


def _db():
    p = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    c = sqlite3.connect(p)
    c.execute("""CREATE TABLE positions (
        position_id TEXT, ticker TEXT, strategy TEXT, pillar TEXT, direction TEXT, status TEXT,
        legs_json TEXT, entry_price REAL, current_price REAL, unrealized_pnl REAL, realized_pnl REAL,
        max_loss_dollars REAL, max_gain_dollars REAL, entry_date TEXT, expiry_date TEXT,
        conviction_at_entry REAL, regime_at_entry TEXT, close_date TEXT)""")
    legs = json.dumps([{"action": "buy", "option_type": "call", "strike": 50, "contracts": 1,
                        "delta": 0.49, "gamma": 0.17, "theta": -0.02, "vega": 0.05}])
    c.executemany("INSERT INTO positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
        ("p1", "AAA", "long_call", "directional", "bullish", "open", legs, 1.0, 0.8, -20.0, None,
         100.0, 300.0, "2026-06-10", "2026-07-17", 65.0, "neutral", ""),
        # closed TODAY → should get a closing frame with realized_pnl
        ("p2", "BBB", "bull_put_spread", "vol_premium", "bullish", "closed", legs, -1.0, 0.5, 0.0, 80.0,
         400.0, 100.0, "2026-06-12", "2026-07-17", 50.0, "neutral", "2026-06-22"),
        # closed on a DIFFERENT day → must NOT be captured
        ("p3", "CCC", "long_put", "directional", "bearish", "closed", legs, 2.0, 0.0, 0.0, -200.0,
         200.0, 600.0, "2026-06-01", "2026-06-15", 40.0, "neutral", "2026-06-15"),
    ])
    c.commit(); c.close()
    return p


def test_net_greeks_signed_sum():
    legs = json.dumps([{"action": "buy", "contracts": 2, "delta": 0.4, "theta": -0.02},
                       {"action": "sell", "contracts": 2, "delta": 0.2, "theta": -0.01}])
    g = _net_greeks(legs)
    assert abs(g["net_delta"] - (2 * 0.4 - 2 * 0.2)) < 1e-6   # +0.4
    assert abs(g["net_theta"] - (2 * -0.02 - 2 * -0.01)) < 1e-6
    assert _net_greeks(None)["net_delta"] is None


def test_captures_open_and_closing_frame_only_today():
    db = _db()
    r = capture_lifecycle_snapshots(db, as_of="2026-06-22")
    assert r["captured"] == 2                       # p1 (open) + p2 (closed today); p3 excluded
    c = sqlite3.connect(db); c.row_factory = sqlite3.Row
    rows = {x["position_id"]: x for x in c.execute("SELECT * FROM lifecycle_snapshots")}
    assert set(rows) == {"p1", "p2"}
    assert rows["p1"]["status"] == "open" and rows["p1"]["realized_pnl"] is None
    assert rows["p2"]["status"] == "closed" and rows["p2"]["realized_pnl"] == 80.0
    # features computed
    assert rows["p1"]["days_held"] == 12            # 2026-06-10 → 06-22
    assert rows["p1"]["unrealized_pct_risk"] == round(-20.0 / 100.0, 4)
    assert rows["p1"]["net_delta"] == 0.49


def test_idempotent_same_day():
    db = _db()
    capture_lifecycle_snapshots(db, as_of="2026-06-22")
    capture_lifecycle_snapshots(db, as_of="2026-06-22")   # re-run
    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM lifecycle_snapshots").fetchone()[0] == 2  # no dupes


def test_error_safe_on_bad_db():
    r = capture_lifecycle_snapshots("/nonexistent/path.db")
    assert "error" in r or r.get("captured") == 0

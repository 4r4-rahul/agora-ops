"""Tests for the paper-account clean-slate tool — its core logic (which legs to KEEP, which to
flatten) is pure and must be correct before it ever touches the broker: it KEEPS real engine
positions and flattens ONLY orphans + adopted junk."""
import importlib.util
import json
import sqlite3
from pathlib import Path

_M = Path(__file__).resolve().parent.parent.parent / "scripts" / "flatten_paper_orphans.py"
_spec = importlib.util.spec_from_file_location("flatten_paper_orphans", _M)
fpo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fpo)


def _db(tmp_path, rows):
    p = str(tmp_path / "f.db")
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE positions (ticker TEXT, status TEXT, contracts INTEGER, "
              "legs_json TEXT, regime_at_entry TEXT DEFAULT 'neutral')")
    c.executemany("INSERT INTO positions (ticker,status,contracts,legs_json,regime_at_entry) "
                  "VALUES (?,?,?,?,?)", rows)
    c.commit(); c.close()
    return p


def _legs(*specs):
    return json.dumps([{"option_type": ot, "strike": s, "expiration": "2026-07-17",
                        "action": a, "contracts": 1} for ot, s, a in specs])


def test_keep_set_excludes_adopted(tmp_path):
    db = _db(tmp_path, [
        ("META", "open", 1, _legs(("put", 540, "buy"), ("put", 505, "sell")), "neutral"),  # REAL
        ("AMD", "open", 8, _legs(("put", 470, "sell")), "adopted"),                          # JUNK (adopted)
    ])
    keep = fpo.real_engine_legs(db)
    assert ("META", "P", 540.0, "20260717") in keep      # real kept
    assert ("META", "P", 505.0, "20260717") in keep
    assert not any(k[0] == "AMD" for k in keep)           # adopted NOT in keep set


def test_flatten_plan_keeps_real_flattens_junk():
    keep = {("META", "P", 540.0, "20260717"): 1, ("META", "P", 505.0, "20260717"): -1}
    broker = dict(keep)
    broker[("AMD", "P", 470.0, "20260717")] = -8     # adopted naked junk at broker
    broker[("ZZZ", "C", 100.0, "20260101")] = 3      # pure orphan
    plan = fpo.flatten_plan(broker, keep)
    flattened = {(p["symbol"], p["action"], p["qty"]) for p in plan}
    assert flattened == {("AMD", "BUY", 8), ("ZZZ", "SELL", 3)}   # ONLY junk; real META untouched
    assert all(p["keep_qty"] == 0 for p in plan)


def test_overfilled_real_leg_trimmed_to_book(tmp_path):
    # a REAL leg the broker holds MORE of than the book → trim the excess, keep the real size
    keep = {("DIA", "P", 505.0, "20260717"): -59}
    broker = {("DIA", "P", 505.0, "20260717"): -631}     # over-filled
    plan = fpo.flatten_plan(broker, keep)
    assert len(plan) == 1 and plan[0]["action"] == "BUY" and plan[0]["qty"] == 572   # 631-59
    assert plan[0]["keep_qty"] == -59


def test_nothing_to_flatten_when_broker_equals_book():
    keep = {("SPY", "C", 600.0, "20260717"): 5}
    assert fpo.flatten_plan(dict(keep), keep) == []

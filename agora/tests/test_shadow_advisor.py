"""
agora/tests/test_shadow_advisor.py — Rung 2 shadow advisor. Verifies it records what models would
advise on real trades, matches M2's bias thresholds (neutral at mid-vol, down-weight debits only in
rich-IV), produces the right would-be verdict, is idempotent, and never affects trading. Pure temp DB.
"""
from __future__ import annotations

import sqlite3
import tempfile
from datetime import UTC, datetime

from agora.ops.shadow_advisor import run_shadow_advisor


def _db(credit_favor, trades, ticker_ivr=None):
    """trades: (position_id, ticker, structure_class, is_credit, status, realized_pnl, win).
    ticker_ivr: optional {ticker: per_ticker_favor} to test the M2 per-ticker IVR path."""
    p = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    c = sqlite3.connect(p)
    c.execute("""CREATE TABLE trade_features (position_id TEXT, ticker TEXT, structure_class TEXT,
        is_credit INTEGER, status TEXT, realized_pnl REAL, win INTEGER)""")
    c.execute("CREATE TABLE lifecycle_snapshots (position_id TEXT, snapshot_date TEXT, unrealized_pnl REAL)")
    c.execute("""CREATE TABLE model_scores (model_name TEXT, entity_id TEXT, score REAL, score_date TEXT, meta_json TEXT)""")
    c.executemany("INSERT INTO trade_features VALUES (?,?,?,?,?,?,?)", trades)
    sd = datetime.now(UTC).date().isoformat()
    c.execute("INSERT INTO model_scores VALUES ('regime_model','credit_favorability',?,?,'{}')", (credit_favor, sd))
    for tk, fav in (ticker_ivr or {}).items():
        c.execute("INSERT INTO model_scores VALUES ('regime_model',?,?,?,'{}')", (tk, fav, sd))
    c.commit(); c.close()
    return p


def test_neutral_when_low_favor():
    # global cf=0.5, no per-ticker scores → M2 neutral, does NOT flag debits
    db = _db(0.5, [("d1", "AAA", "long_option", 0, "closed", -100.0, 0),
                   ("c1", "BBB", "credit_spread", 1, "closed", 50.0, 1)])
    r = run_shadow_advisor(db)
    assert r["m2_would_downweight"] == 0
    assert r["m2_verdict"] == "m2_neutral"


def test_flags_debit_via_market_fallback():
    # no per-ticker score → falls back to global cf=0.8 → flags the debit
    db = _db(0.8, [("d1", "AAA", "long_option", 0, "closed", -200.0, 0),
                   ("c1", "BBB", "credit_spread", 1, "closed", 80.0, 1)])
    r = run_shadow_advisor(db)
    assert r["m2_would_downweight"] == 1
    c = sqlite3.connect(db)
    adv = c.execute("SELECT would_advise FROM shadow_model_decisions WHERE position_id='d1' AND model='regime_model'").fetchone()[0]
    assert adv == "downweight_debit_high_ivr"


def test_per_ticker_ivr_flags_debit_even_at_mid_market_vol():
    # market cf=0.5 (mid) BUT ticker AAA has high per-ticker IVR favor 0.8 → flags AAA's debit anyway
    db = _db(0.5, [("d1", "AAA", "long_option", 0, "closed", -300.0, 0),
                   ("d2", "ZZZ", "long_option", 0, "closed", -50.0, 0)],
             ticker_ivr={"AAA": 0.8})  # only AAA is high-IVR
    r = run_shadow_advisor(db)
    c = sqlite3.connect(db)
    aaa = c.execute("SELECT would_advise FROM shadow_model_decisions WHERE position_id='d1' AND model='regime_model'").fetchone()[0]
    zzz = c.execute("SELECT would_advise FROM shadow_model_decisions WHERE position_id='d2' AND model='regime_model'").fetchone()[0]
    assert aaa == "downweight_debit_high_ivr"   # high per-ticker IVR → flagged
    assert zzz == "neutral"                     # low IVR (falls to global 0.5) → not flagged


def test_idempotent():
    db = _db(0.5, [("d1", "AAA", "long_option", 0, "closed", -100.0, 0)])
    run_shadow_advisor(db); run_shadow_advisor(db)
    c = sqlite3.connect(db)
    # 1 trade × 1 model (regime; no fill/liq scores) = 1 row, no dupes
    assert c.execute("SELECT COUNT(*) FROM shadow_model_decisions").fetchone()[0] == 1


def test_records_open_trades_via_unrealized():
    db = _db(0.5, [("o1", "AAA", "long_option", 0, "open", None, None)])
    c = sqlite3.connect(db)
    c.execute("INSERT INTO lifecycle_snapshots VALUES ('o1','2026-06-22',-75.0)"); c.commit(); c.close()
    run_shadow_advisor(db)
    c = sqlite3.connect(db)
    pnl = c.execute("SELECT trade_pnl FROM shadow_model_decisions WHERE position_id='o1' AND model='regime_model'").fetchone()[0]
    assert pnl == -75.0   # used the unrealized mark


def test_error_safe():
    assert run_shadow_advisor("/nonexistent/x.db").get("recorded") == 0

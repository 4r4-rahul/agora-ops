"""
agora/tests/test_market_regime.py — Phase 0d market capture + M2 regime model. Verifies VIX → vol
regime + term-structure classification, the credit-vs-debit favorability mapping, idempotency, and
network-failure safety. Uses an injected fetcher (no network). Pure over a temp DB.
"""
from __future__ import annotations

import sqlite3
import tempfile

from agora.ops.market_capture import _term_state, _vol_regime, capture_market_snapshot
from agora.ops.ml_models.m2_regime import regime_model


def _hi():
    return {"VIX": {"price": 26.0, "change_pct": 5.0}, "VIX9D": {"price": 27.5},
            "VIX3M": {"price": 25.0}, "SPY": {"price": 500, "change_pct": -1.2},
            "QQQ": {"price": 430, "change_pct": -1.5}}


def _lo():
    return {"VIX": {"price": 12.0, "change_pct": -1.0}, "VIX9D": {"price": 11.5},
            "VIX3M": {"price": 13.0}, "SPY": {"price": 500, "change_pct": 0.8},
            "QQQ": {"price": 430, "change_pct": 1.0}}


def test_classifiers():
    assert _vol_regime(12) == "low" and _vol_regime(18) == "elevated"
    assert _vol_regime(24) == "high" and _vol_regime(32) == "extreme"
    assert _term_state(1.05) == "backwardation" and _term_state(0.95) == "contango"
    assert _term_state(1.0) == "flat"


def test_capture_and_idempotent():
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    r = capture_market_snapshot(db, as_of="2026-06-22", fetcher=_hi)
    assert r["captured"] == 1 and r["vol_regime"] == "high" and r["term_state"] == "backwardation"
    # idempotent
    r2 = capture_market_snapshot(db, as_of="2026-06-22", fetcher=_hi)
    assert r2.get("skipped") == 1
    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0] == 1


def test_m2_high_vol_favors_credit():
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    capture_market_snapshot(db, as_of="2026-06-22", fetcher=_hi)
    r = regime_model(db)
    assert r["status"] == "ok"
    assert r["metrics"]["credit_favorability"] >= 0.6   # high vol → favor credit
    assert "SELL premium" in r["metrics"]["bias"]
    cf = next(s for s in r["scores"] if s["entity_id"] == "credit_favorability")
    assert cf["score"] >= 0.6


def test_m2_low_vol_favors_debit():
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    capture_market_snapshot(db, as_of="2026-06-22", fetcher=_lo)
    r = regime_model(db)
    assert r["metrics"]["credit_favorability"] <= 0.35   # low vol → debits ok
    assert "BUY premium" in r["metrics"]["bias"]


def test_m2_no_snapshot_skips():
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    assert regime_model(db)["status"] == "skipped"


def test_capture_no_vix_safe():
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    r = capture_market_snapshot(db, as_of="2026-06-22", fetcher=lambda: {"VIX": {"price": None}})
    assert r.get("skipped") == 1

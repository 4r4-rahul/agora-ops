"""
agora/tests/test_phase2_models.py — M4 (win/EV), M5 (conviction calibration), M8 (lifecycle
attribution). The key contract: at BOOTSTRAP n they refuse to act (actionable=False / verdict=
insufficient_n) and only assert edge/predictiveness once EMERGING. Pure over a temp DB.
"""
from __future__ import annotations

import sqlite3
import tempfile

from agora.ops.ml_models.m4_winrate import win_ev_model
from agora.ops.ml_models.m5_conviction import conviction_calibration_model
from agora.ops.ml_models.m8_lifecycle import lifecycle_attribution_model


def _db(rows):
    """rows: (structure_class, conviction, regime, n_frames, mae, mfe, days, win, pnl)."""
    p = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    c = sqlite3.connect(p)
    c.execute("""CREATE TABLE trade_features (structure_class TEXT, conviction_at_entry REAL,
        regime_at_entry TEXT, n_frames INTEGER, max_adverse_pct REAL, max_favorable_pct REAL,
        days_held INTEGER, win INTEGER, realized_pnl REAL)""")
    c.executemany("INSERT INTO trade_features VALUES (?,?,?,?,?,?,?,?,?)", rows)
    c.commit(); c.close()
    return p


def _mk(n_high_win, n_high_loss, n_low_win, n_low_loss):
    rows = []
    rows += [("credit_spread", 75, "neutral", 3, -0.2, 0.6, 5, 1, 100.0)] * n_high_win
    rows += [("credit_spread", 75, "neutral", 3, -0.8, 0.1, 4, 0, -200.0)] * n_high_loss
    rows += [("long_option", 50, "neutral", 3, -0.5, 0.3, 6, 1, 50.0)] * n_low_win
    rows += [("long_option", 50, "neutral", 3, -0.9, 0.05, 7, 0, -150.0)] * n_low_loss
    return rows


def test_m4_bootstrap_is_shadow_only():
    db = _db(_mk(3, 2, 2, 2))   # n=9 < 20 → BOOTSTRAP
    r = win_ev_model(db)
    assert r["readiness"] == "BOOTSTRAP" and r["metrics"]["actionable"] is False
    assert all(s["meta"]["actionable"] is False for s in r["scores"])
    assert "shadow-only" in r["summary"]


def test_m4_emerging_is_actionable():
    db = _db(_mk(8, 4, 5, 5))   # n=22 >= 20 → EMERGING
    r = win_ev_model(db)
    assert r["readiness"] == "EMERGING" and r["metrics"]["actionable"] is True
    # segments carry Wilson lower bound
    assert "win_rate_wilson_lb" in r["metrics"]["by_structure"]["credit_spread"]


def test_m5_refuses_verdict_at_bootstrap():
    db = _db(_mk(3, 2, 2, 2))   # BOOTSTRAP
    r = conviction_calibration_model(db)
    assert r["readiness"] == "BOOTSTRAP"
    assert r["metrics"]["verdict"] == "insufficient_n"   # the honest no-assert (not 'inverted')


def test_m5_predictive_verdict_when_emerging():
    # high band wins more than low band, n>=20 → 'predictive'
    db = _db(_mk(10, 2, 4, 8))
    r = conviction_calibration_model(db)
    assert r["readiness"] == "EMERGING"
    assert r["metrics"]["verdict"] in ("predictive", "flat", "inverted")
    # high band avg_pnl > low band → predictive
    assert r["metrics"]["verdict"] == "predictive"


def test_m8_path_profile_and_gating():
    db = _db(_mk(3, 2, 2, 2))   # BOOTSTRAP, but path features present
    r = lifecycle_attribution_model(db)
    assert r["readiness"] == "BOOTSTRAP" and r["metrics"]["actionable"] is False
    prof = r["metrics"]["path_profile"]
    assert prof["winners"]["n"] == 5 and prof["losers"]["n"] == 4
    # winners' avg max-favorable computed
    assert prof["winners"]["avg_max_favorable"] is not None
    assert r["metrics"]["signals"] == []   # no signals asserted at bootstrap


def test_error_safe():
    for fn in (win_ev_model, conviction_calibration_model, lifecycle_attribution_model):
        assert fn("/nonexistent/x.db").get("status") in ("error", "skipped")


# ── M2 regime: a stateless heuristic must report readiness N/A, never a train tier ──
from agora.ops.ml_models.m2_regime import regime_model


def _regime_db(n_rows):
    p = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    c = sqlite3.connect(p)
    if n_rows:
        c.execute("CREATE TABLE market_snapshots "
                  "(snapshot_date TEXT, vix REAL, vol_regime TEXT, term_state TEXT)")
        c.executemany("INSERT INTO market_snapshots VALUES (?,?,?,?)",
                      [(f"2026-06-{10+i:02d}", 18.0, "elevated", "contango") for i in range(n_rows)])
    c.commit(); c.close()
    return p


def test_m2_regime_readiness_is_na_not_trainable():
    # It scores off a fixed lookup on the latest snapshot — identical at n=1 or n=1000 — so the
    # train tiers don't apply. Was wrongly reporting TRAINABLE on a single snapshot.
    r1 = regime_model(_regime_db(1))
    assert r1["status"] == "ok" and r1["readiness"] == "N/A"
    r5 = regime_model(_regime_db(5))
    assert r5["readiness"] == "N/A"           # never escalates with more snapshots


def test_m2_regime_no_snapshot_is_na():
    assert regime_model(_regime_db(0))["readiness"] == "N/A"

"""
agora/tests/test_ticker_adapter.py — per-ticker adaptive job (Phase 2, shadow).

Locks the safety contract: Bayesian shrinkage toward the global prior, n-gated, down-only, and every
override written SHADOW (active=0 → never applied by the resolver).
"""
from __future__ import annotations

import sqlite3
import tempfile

from agora.ops.ticker_adapter import _MIN_N, _shrink, run_ticker_adapter
from agora.ops.ticker_settings import TickerSettingsResolver, get_overrides


def test_shrink_pulls_thin_samples_toward_global():
    # n=4 (< prior strength 20) → estimate sits mostly on the global prior
    near_global = _shrink(ticker_stat=-500.0, global_stat=-50.0, n=4)
    assert -150.0 < near_global < -50.0      # pulled hard toward -50, far from -500
    # n=200 → estimate sits mostly on the ticker's own number
    near_ticker = _shrink(ticker_stat=-500.0, global_stat=-50.0, n=200)
    assert near_ticker < -400.0


def _db(rows):
    """rows: (ticker, realized_pnl). Builds a minimal trade_features with labeled real closes."""
    p = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE trade_features (ticker TEXT, realized_pnl REAL, is_real_close INTEGER)")
    c.executemany("INSERT INTO trade_features VALUES (?,?,1)", rows)
    c.commit(); c.close()
    return p


def test_below_min_n_gets_no_override():
    # BAD ticker but only _MIN_N-1 closes → must NOT earn an override
    rows = [("BAD", -300.0)] * (_MIN_N - 1) + [("FILL", 10.0)] * 30
    db = _db(rows)
    r = run_ticker_adapter(db)
    assert r["status"] == "ok"
    assert not any(o["ticker"] == "BAD" for o in get_overrides(db))


def test_proven_loser_gets_shadow_tighter_cap():
    # BAD: many big losses (shrunk EV stays clearly negative even after pooling) → shadow cap override
    rows = [("BAD", -400.0)] * 25 + [("OK", 50.0)] * 25
    db = _db(rows)
    run_ticker_adapter(db)
    ov = [o for o in get_overrides(db) if o["ticker"] == "BAD"]
    assert len(ov) == 1
    o = ov[0]
    assert o["setting_key"] == "max_risk_per_trade_dollars"
    assert o["value"] == 200      # 400 * down_factor 0.5
    assert o["active"] == 0       # SHADOW — never applied
    assert "shrunk_EV" in o["rationale"]
    # the resolver must NOT apply it (shadow)
    assert TickerSettingsResolver(db).resolve("BAD", "max_risk_per_trade_dollars", 400.0) == 400.0


def test_winner_gets_no_override_down_only():
    # profitable ticker → down-only means NO tightening
    rows = [("WIN", 120.0)] * 20 + [("X", -10.0)] * 20
    db = _db(rows)
    run_ticker_adapter(db)
    assert not any(o["ticker"] == "WIN" for o in get_overrides(db))


def test_vol_profile_alone_tightens_cap_even_without_edge():
    # A high-vol ticker (cap_factor 0.6) with NO trades of its own still earns a vol-scaled shadow
    # cap from its price-history profile — the Phase-A characterization path.
    db = _db([("OK", 50.0)] * 20)               # global prior only; HOT has no closes
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE ticker_profiles (ticker TEXT PRIMARY KEY, hv_annual REAL, cap_factor REAL)")
    c.execute("INSERT INTO ticker_profiles (ticker, hv_annual, cap_factor) VALUES ('HOT', 0.5, 0.6)")
    c.commit(); c.close()
    run_ticker_adapter(db)
    ov = [o for o in get_overrides(db) if o["ticker"] == "HOT"]
    assert len(ov) == 1 and ov[0]["value"] == 240 and ov[0]["active"] == 0   # 400*0.6, shadow
    assert "vol×0.6" in ov[0]["rationale"]


def test_vol_and_edge_take_the_tighter_min():
    # BAD is a proven loser (edge→0.5) AND high-vol (vol→0.6) → cap uses the MIN (0.5 → 200).
    db = _db([("BAD", -400.0)] * 25 + [("OK", 50.0)] * 25)
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE ticker_profiles (ticker TEXT PRIMARY KEY, hv_annual REAL, cap_factor REAL)")
    c.execute("INSERT INTO ticker_profiles (ticker, hv_annual, cap_factor) VALUES ('BAD', 0.5, 0.6)")
    c.commit(); c.close()
    run_ticker_adapter(db)
    bad = [o for o in get_overrides(db) if o["ticker"] == "BAD"][0]
    assert bad["value"] == 200    # min(0.5 edge, 0.6 vol) → 0.5 → 400*0.5


def test_error_safe():
    assert run_ticker_adapter("/nonexistent/x.db")["status"] in ("skipped", "error")

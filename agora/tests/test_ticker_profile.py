"""
agora/tests/test_ticker_profile.py — per-ticker characterization (Phase A).

Heavy edge-case coverage on the PURE math (insufficient/garbage/constant data, missing high-low-SPY,
boundary vols) and the IO paths (fetch/store/upsert, error-safety). characterize() must never guess
on bad data — it returns None below the minimum sample.
"""
from __future__ import annotations

import math
import sqlite3
import tempfile

from agora.ops.ticker_profile import (
    _MIN_DAYS,
    all_profiles,
    build_profiles,
    characterize,
    get_profile,
    vol_scaled_cap_factor,
)


def _series(n: int, amp: float = 0.01, base: float = 100.0) -> list[float]:
    """Deterministic bounded-vol close series (sine wave → finite, positive, known-ish vol)."""
    return [base * (1.0 + amp * math.sin(i * 0.7)) for i in range(n)]


# ── vol_scaled_cap_factor: down-only, clamped, divide-by-zero safe ───────────────────
def test_cap_factor_degenerate_vol_is_neutral():
    for bad in (0.0, -0.5, float("inf"), float("nan")):
        assert vol_scaled_cap_factor(bad) == 1.0


def test_cap_factor_calm_ticker_stays_at_one():
    assert vol_scaled_cap_factor(0.15) == 1.0          # below target → clamped up to 1.0 (never >1)
    assert vol_scaled_cap_factor(0.30) == 1.0          # exactly target


def test_cap_factor_wild_ticker_tightens_but_floors():
    assert vol_scaled_cap_factor(0.45) == round(0.30 / 0.45, 3)   # 0.667
    assert vol_scaled_cap_factor(0.60) == 0.5          # 2x vol → floor
    assert vol_scaled_cap_factor(5.0) == 0.5           # never below the floor


# ── characterize: insufficient / garbage / constant / partial inputs ─────────────────
def test_below_min_days_returns_none():
    assert characterize(_series(_MIN_DAYS - 1)) is None
    assert characterize([]) is None
    assert characterize(None) is None


def test_exactly_min_days_works():
    sig = characterize(_series(_MIN_DAYS))
    assert sig is not None and sig["n_days"] == _MIN_DAYS and sig["hv_annual"] >= 0


def test_garbage_values_are_filtered():
    # Nones, NaN, negative, zero, strings are dropped; needs >= _MIN_DAYS clean to survive.
    dirty = [None, -5.0, 0.0, float("nan"), "x"] + _series(_MIN_DAYS + 10)
    sig = characterize(dirty)
    assert sig is not None and sig["n_days"] == _MIN_DAYS + 10   # only the clean ones counted
    # ... and if too few clean remain → None
    assert characterize([None, "x", -1] + _series(10)) is None


def test_constant_prices_zero_vol_no_crash():
    sig = characterize([100.0] * (_MIN_DAYS + 5))
    assert sig is not None
    assert sig["hv_annual"] == 0.0 and sig["cap_factor"] == 1.0   # zero vol → neutral, no div-by-zero


def test_atr_none_without_high_low_and_set_with():
    closes = _series(_MIN_DAYS)
    assert characterize(closes)["atr_pct"] is None
    highs = [c * 1.01 for c in closes]
    lows = [c * 0.99 for c in closes]
    sig = characterize(closes, highs=highs, lows=lows)
    assert sig["atr_pct"] is not None and sig["atr_pct"] > 0


def test_beta_none_without_spy_and_computed_with():
    closes = _series(_MIN_DAYS, amp=0.02)
    assert characterize(closes)["beta_spy"] is None
    # ticker == SPY series → beta ~1.0
    sig = characterize(closes, spy_closes=closes)
    assert sig["beta_spy"] is not None and abs(sig["beta_spy"] - 1.0) < 1e-6


def test_trend_persistence_in_unit_interval():
    sig = characterize(_series(_MIN_DAYS + 40, amp=0.03))
    assert 0.0 <= sig["trend_persistence"] <= 1.0


# ── build_profiles / get_profile / all_profiles: IO + upsert + error-safety ──────────
def _mock_fetcher(tickers, lookback_days):
    return {t: {"closes": _series(120, amp=0.01 if t == "SPY" else 0.04),
                "highs": [c * 1.01 for c in _series(120, amp=0.04)],
                "lows": [c * 0.99 for c in _series(120, amp=0.04)]} for t in tickers}


def _db():
    return tempfile.NamedTemporaryFile(suffix=".db", delete=False).name


def test_build_profiles_stores_and_is_upsert():
    db = _db()
    r1 = build_profiles(db, ["NVDA", "AAPL"], fetcher=_mock_fetcher)
    assert r1["status"] == "ok" and r1["profiled"] == 2
    r2 = build_profiles(db, ["NVDA", "AAPL"], fetcher=_mock_fetcher)   # re-run
    assert r2["profiled"] == 2
    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM ticker_profiles").fetchone()[0] == 2   # no dup rows
    nvda = get_profile(db, "nvda")    # case-insensitive
    assert nvda and 0.5 <= nvda["cap_factor"] <= 1.0


def test_build_profiles_empty_and_fetch_failure_safe():
    assert build_profiles(_db(), [], fetcher=_mock_fetcher)["status"] == "skipped"
    db = _db()
    r = build_profiles(db, ["X"], fetcher=lambda t, d: {})   # fetcher returns nothing
    assert r["status"] == "ok" and r["profiled"] == 0
    assert all_profiles(db) == []


def test_get_profile_missing_and_error_safe():
    assert get_profile(_db(), "NONE") is None
    assert get_profile("/nonexistent/x.db", "NVDA") is None
    assert all_profiles("/nonexistent/x.db") == []

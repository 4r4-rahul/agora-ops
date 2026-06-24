"""
agora/tests/test_ticker_settings.py — per-ticker settings store + resolver (Phase 1).

Locks the zero-regression contract: shadow overrides are NEVER applied; only active=1 overrides are;
un-overridden (ticker,key) always returns the global default.
"""
from __future__ import annotations

import tempfile

from agora.ops.ticker_settings import (
    TickerSettingsResolver,
    get_overrides,
    set_override,
)


def _db():
    return tempfile.NamedTemporaryFile(suffix=".db", delete=False).name


def test_resolve_returns_global_default_when_no_override():
    r = TickerSettingsResolver(_db())
    assert r.resolve("NVDA", "max_risk_per_trade_dollars", 400.0) == 400.0


def test_shadow_override_is_NOT_applied():
    db = _db()
    set_override(db, "NVDA", "max_risk_per_trade_dollars", 250.0,
                 source="adaptive_v1", n_samples=30, active=False)   # shadow
    r = TickerSettingsResolver(db)
    assert r.resolve("NVDA", "max_risk_per_trade_dollars", 400.0) == 400.0   # default, not 250
    assert r.active_count == 0


def test_active_override_is_applied():
    db = _db()
    set_override(db, "NVDA", "max_risk_per_trade_dollars", 250.0,
                 source="adaptive_v1", n_samples=30, active=True)    # live
    r = TickerSettingsResolver(db)
    assert r.resolve("NVDA", "max_risk_per_trade_dollars", 400.0) == 250.0
    assert r.resolve("AAPL", "max_risk_per_trade_dollars", 400.0) == 400.0   # other ticker untouched
    assert r.active_count == 1


def test_upsert_replaces_and_can_promote_shadow_to_active():
    db = _db()
    set_override(db, "TSLA", "long_options_min_conviction", 4, source="a", n_samples=10, active=False)
    set_override(db, "TSLA", "long_options_min_conviction", 4, source="a", n_samples=12, active=True)  # promote
    rows = get_overrides(db)
    assert len(rows) == 1 and rows[0]["active"] == 1 and rows[0]["value"] == 4 and rows[0]["n_samples"] == 12


def test_get_overrides_active_only_filter():
    db = _db()
    set_override(db, "A", "k", 1, source="s", n_samples=1, active=True)
    set_override(db, "B", "k", 2, source="s", n_samples=1, active=False)
    assert {o["ticker"] for o in get_overrides(db)} == {"A", "B"}
    assert {o["ticker"] for o in get_overrides(db, active_only=True)} == {"A"}


def test_set_override_error_safe():
    assert set_override("/nonexistent/dir/x.db", "X", "k", 1, source="s", n_samples=1) is False

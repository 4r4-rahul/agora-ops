"""
agora/tests/test_long_options_sizing.py — the long-options path now sizes ADAPTIVELY.

Until 2026-06-25 the long-options agent (the majority of trades) sized purely by quality tier (1/2/max)
and ignored per-ticker vol — only the spread path was adaptive. This pins the fix: the long-options
_vol_size_factor uses the SAME shared resolver as the spread path, so a volatile name sizes down and a
calm one up, and the gate flag turns it off cleanly.
"""
from __future__ import annotations

import sqlite3
import tempfile
import types

import pytest

from agora.agents.long_options_agent import LongOptionsAgent


def _agent(*, enabled=True, profiles=(("WILD", 0.92), ("CALM", 0.16), ("MED", 0.46))):
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE ticker_profiles (ticker TEXT, hv_annual REAL)")
    c.executemany("INSERT INTO ticker_profiles VALUES (?,?)", list(profiles))
    c.commit(); c.close()
    s = types.SimpleNamespace(db_path=db, adaptive_entry_sizing_enabled=enabled,
                              long_options_min_dte=21, long_options_max_dte=45)
    return LongOptionsAgent(settings=s)


def test_long_options_volatile_sizes_down_calm_up():
    from agora.ops.adaptive_stop import DEFAULTS
    a = _agent()
    assert a._vol_size_factor("WILD") == pytest.approx(DEFAULTS.base_hv / 0.92, abs=0.01)   # <1 (down)
    assert a._vol_size_factor("CALM") == pytest.approx(DEFAULTS.base_hv / 0.16, abs=0.01)   # >1 (up)
    assert a._vol_size_factor("WILD") < 1.0 < a._vol_size_factor("CALM")


def test_long_options_median_name_is_full_size():
    # the calibration payoff: a universe-median-vol name sizes ~1.0× (uses the full risk budget)
    assert _agent()._vol_size_factor("MED") == pytest.approx(1.0, abs=0.02)


def test_long_options_factor_matches_spread_path_exactly():
    # CRITICAL wiring guarantee: long options and spreads resolve the IDENTICAL factor for a ticker
    from agora.ops.ticker_profile import resolve_size_factor
    a = _agent()
    for tk in ("WILD", "CALM", "MED"):
        assert a._vol_size_factor(tk) == resolve_size_factor(a._db_path, tk, enabled=True)


def test_long_options_disabled_and_unprofiled_are_full_size():
    assert _agent(enabled=False)._vol_size_factor("WILD") == 1.0   # gate off → neutral
    assert _agent()._vol_size_factor("NOPROFILE") == 1.0           # unprofiled → neutral


def test_long_options_sizing_arithmetic_applies_factor():
    # mirror the in-path arithmetic: contracts = clamp(round(quality_base × vol_factor), 1, max)
    a = _agent()
    max_contracts = 10
    # a 2-contract quality base on a volatile name (factor 0.5) → 1; on a calm name (factor 2.875) → capped
    vf_wild = a._vol_size_factor("WILD")
    vf_calm = a._vol_size_factor("CALM")
    assert max(1, min(max_contracts, round(2 * vf_wild))) == 1
    assert max(1, min(max_contracts, round(2 * vf_calm))) == min(max_contracts, round(2 * vf_calm))
    assert max(1, min(max_contracts, round(2 * vf_calm))) >= 2   # calm name keeps/raises the base

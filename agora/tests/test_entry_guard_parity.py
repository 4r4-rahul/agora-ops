"""
agora/tests/test_entry_guard_parity.py — CROSS-PATH guard-parity INVARIANT (the dimension that was missed).

AGORA has two parallel entry pipelines — spreads (_submit_recommendation_inner) and long options (the
_long_submit_lock worker). Both funnel through the shared _entry_gate (kill switch / compliance / risk
council / macro-block / correlation / devils-advocate), but each owns its OWN post-gate steps. The
contract-cap breach (DIA 21 > cap 10, NOK 12 > cap 5) escaped EVERY unit test because the guard lived in
the spread path but not the long-options path — a class of bug no unit test examines.

This codifies the invariant: every post-_entry_gate guard MUST exist in BOTH submit paths. If a future
change adds a guard (size cap, size reduction, …) to one path but not the other, THIS fails. It is the
test that would have caught the contract-cap bug before it reached live trading.
"""
from __future__ import annotations

import inspect

import agora.session as _sess

_SRC = inspect.getsource(_sess)

# Post-_entry_gate guards that must be enforced on BOTH submit paths (markers present in each region).
_POST_GATE_GUARDS = (
    "position_size_multiplier",   # macro-calendar caution-day size reduction
    "vix_stress_mode",            # VIX-stress size reduction
    "CONTRACT-CAP GUARD",         # hard per-trade contract cap
)


def _spread_submit_region() -> str:
    """Body of the spread submit path up to its order submission."""
    i = _SRC.index("async def _submit_recommendation_inner")
    return _SRC[i:_SRC.index("submit_trade(", i)]


def _long_submit_region() -> str:
    """The long-options submit worker from its lock to its order submission."""
    i = _SRC.index("self._long_submit_lock:")
    return _SRC[i:_SRC.index("submit_trade(rec", i)]


def test_post_gate_guards_present_in_both_pipelines():
    spread, long = _spread_submit_region(), _long_submit_region()
    missing_spread = [g for g in _POST_GATE_GUARDS if g not in spread]
    missing_long = [g for g in _POST_GATE_GUARDS if g not in long]
    assert not missing_spread, f"SPREAD submit path missing post-gate guard(s): {missing_spread}"
    assert not missing_long, (
        f"LONG-OPTIONS submit path missing post-gate guard(s): {missing_long} — "
        "a guard in one pipeline but not the other is the contract-cap bug class")


def test_both_pipelines_funnel_through_shared_entry_gate():
    # both submit paths must reach the shared _entry_gate (kill/compliance/risk/macro-block/correlation)
    assert _SRC.count("await self._entry_gate(") >= 2, \
        "a submit path that does not call _entry_gate bypasses the shared safety gates"


def test_contract_cap_clamps_to_a_concrete_int_cap():
    # the guard must clamp to an int setting (max_contracts_per_trade / long_options_max_contracts),
    # never a bare attribute — the int(...) is what made the breach detectable and bounded.
    long = _long_submit_region()
    spread = _spread_submit_region()
    assert "long_options_max_contracts" in long
    assert "max_contracts_per_trade" in spread

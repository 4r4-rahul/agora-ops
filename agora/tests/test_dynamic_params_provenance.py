"""
HARDEN-2 (2026-06-27) — provenance guard against "phantom safety control" deception.

The DynamicParams module docstring once claimed "all agents consume DynamicParams" while 7 fields —
including the adaptive de-risking (risk_off→2 positions, loss-limit tightening, Kelly) — were computed
but consumed NOWHERE. This test keeps LIVE_FIELDS / INERT_FIELDS honest: it scans the real source and
fails if an INERT field gets wired (must be promoted to LIVE + docstring fixed) or a LIVE one loses its
consumer. So a disabled risk control can never silently masquerade as active again.
"""
import re
from pathlib import Path

from agora.ops.dynamic_params import INERT_FIELDS, LIVE_FIELDS

_AGORA = Path(__file__).resolve().parent.parent


def _object_reads(field: str) -> int:
    """Count reads of <field> FROM a DynamicParams object (self._dynamic_params.X / dynamic_params.X /
    dp.X) across agora source, excluding the definition module and tests."""
    pat = re.compile(rf"(?:_dynamic_params|dynamic_params|dp)\.{re.escape(field)}\b")
    total = 0
    for f in _AGORA.rglob("*.py"):
        s = str(f)
        if f.name == "dynamic_params.py" or "/tests/" in s or f.name.startswith("test_"):
            continue
        total += len(pat.findall(f.read_text()))
    return total


def test_live_fields_are_actually_consumed():
    for fld in sorted(LIVE_FIELDS):
        assert _object_reads(fld) >= 1, (
            f"LIVE field '{fld}' has NO consumer — wiring broke or it should move to INERT_FIELDS.")


def test_inert_fields_are_truly_dead():
    for fld in sorted(INERT_FIELDS):
        assert _object_reads(fld) == 0, (
            f"INERT field '{fld}' is now consumed — promote it to LIVE_FIELDS AND correct the "
            f"dynamic_params.py docstring so the doc never lies about active controls.")


def test_no_field_double_classified():
    assert not (LIVE_FIELDS & INERT_FIELDS), "a field cannot be both LIVE and INERT"

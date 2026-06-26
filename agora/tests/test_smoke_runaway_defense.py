"""CI gate for scripts/smoke_runaway_defense.py — the end-to-end '2026-06-26 runaway' smoke test
must keep passing (all defenses hold). Runs the real harness in-process and asserts exit 0."""
import asyncio
import importlib.util
from pathlib import Path

_SMOKE = Path(__file__).resolve().parent.parent.parent / "scripts" / "smoke_runaway_defense.py"


def _load():
    spec = importlib.util.spec_from_file_location("smoke_runaway_defense", _SMOKE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_all_runaway_defenses_hold():
    mod = _load()
    rc = asyncio.run(mod.main())
    assert rc == 0, "a runaway defense FAILED — see smoke output above"
    # every recorded check passed (incl. the negative control that reproduces the runaway)
    assert _results_all_passed(mod)


def _results_all_passed(mod):
    return all(ok for _, ok, _ in mod._results) and len(mod._results) >= 10

"""CI gate for scripts/smoke_book_manager.py — the Book Manager invariants (exact partition,
reconcile→$0, real≠fiction, bug ledger) must keep holding. Runs the mock half in-process (the live
half self-skips when .agora/agora.db is absent, e.g. in CI)."""
import importlib.util
from pathlib import Path

_S = Path(__file__).resolve().parent.parent.parent / "scripts" / "smoke_book_manager.py"


def test_book_manager_invariants_hold():
    spec = importlib.util.spec_from_file_location("smoke_book_manager", _S)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.main() == 0, "a book-manager invariant failed — see smoke output"
    assert all(ok for _, ok in mod._results) and len(mod._results) >= 10

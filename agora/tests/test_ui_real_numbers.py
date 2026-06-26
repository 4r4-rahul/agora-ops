"""
agora/tests/test_ui_real_numbers.py — REGRESSION GUARD: the dashboard headline must show REAL
strategy P&L, never adopted/fiction.

On 2026-06-26 the /agora/performance endpoint's local real-close filter was a STALE COPY of the
predicate, missing the `regime_at_entry='adopted'` exclusion (it didn't even SELECT that column).
Adopted DIA legs carry a real close_source ('lifecycle') but reconstructed/fictional cost basis, so
they were counted as real → the dashboard read −$22,494 / 149 trades instead of the true −$5,358 /
110. This guard fails if the endpoint ever again omits the adopted exclusion or the column.
"""
import inspect

from agora.api import routes


def test_performance_endpoint_excludes_adopted_and_fetches_regime():
    src = inspect.getsource(routes.get_performance)
    # must SELECT the column it needs to exclude on
    assert "regime_at_entry" in src, "performance must SELECT regime_at_entry to exclude adopted"
    # must actually exclude adopted (the bug that showed −$22,494 fiction)
    assert '"adopted"' in src or "'adopted'" in src, \
        "performance _is_real_close must exclude adopted-legacy positions"
    # must still exclude the other fiction sources
    for token in ("fabricated", "sync", "reconcile", "duplicate"):
        assert token in src, f"performance must still exclude {token} closes"


def test_performance_real_close_mirrors_book_manager_intent():
    # book_manager._REAL_CLOSE is the single source of truth; the endpoint must mirror its adopted
    # + fiction-source exclusions (sanity that the two stay conceptually aligned).
    from agora.ops import book_manager
    bm_src = inspect.getsource(book_manager)
    assert "adopted" in bm_src and "_REAL_CLOSE" in bm_src

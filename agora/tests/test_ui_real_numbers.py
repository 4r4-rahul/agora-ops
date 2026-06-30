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
    # must SELECT the column the real-close filter needs
    assert "regime_at_entry" in src, "performance must SELECT regime_at_entry to exclude adopted"
    # must delegate to the SINGLE source of truth (close_sources.is_real_close_row), which excludes
    # adopted + fabricated/sync/reconcile/duplicate (locked in test_close_sources). The hand-spelled
    # inline copy that drifted — and dropped the CBOE +$660 / showed −$22,494 fiction — is GONE.
    assert "is_real_close_row" in src, \
        "performance must use the centralized is_real_close_row, not a hand-spelled allowlist"


def test_performance_real_close_mirrors_book_manager_intent():
    # book_manager._REAL_CLOSE is the single source of truth; the endpoint must mirror its adopted
    # + fiction-source exclusions (sanity that the two stay conceptually aligned).
    from agora.ops import book_manager
    bm_src = inspect.getsource(book_manager)
    assert "adopted" in bm_src and "_REAL_CLOSE" in bm_src


def test_today_endpoint_separates_real_from_fiction():
    """`/agora/today` must put adopted/fiction in fiction_pnl_today and flag each row's provenance,
    so the closed-today view never sums the −$11,159 AMD adopted ghost into the headline."""
    import inspect
    src = inspect.getsource(routes.get_today_summary)
    assert "fiction_pnl_today" in src and "realized_pnl_today" in src
    assert '"adopted"' in src or "'adopted'" in src   # excludes adopted from the real total
    assert '"is_real"' in src and '"provenance"' in src   # each row flagged for the UI
    # the query MUST filter status='closed' — else a reset/rolled row carrying a close_date + a real
    # close_source would be summed into the headline (the SQL real_close_predicate guards status<>'reset';
    # the row-filter here defaults status='closed', so the query must enforce it — predicate parity).
    assert "status = 'closed'" in src or "status='closed'" in src

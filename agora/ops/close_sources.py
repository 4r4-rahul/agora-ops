"""
agora/ops/close_sources.py — the SINGLE source of truth for which closed positions count as REAL
engine performance (P&L books, ML training, attribution, conviction calibration).

WHY THIS MODULE EXISTS (2026-06-29): the `_REAL_CLOSE` predicate had been copy-pasted into
edge_dashboard, outcome_attributor (×2), and prediction_ledger — and the copies DRIFTED:
  • edge_dashboard had 'pre_earnings' + the fiction filters + status<>'reset'; the others did not.
  • prediction_ledger even LACKED the adopted-exclusion (so it could calibrate on $0 adopted rows).
  • worst: three legitimate engine close-sources — time_stop, profit_target, stale_model_stop — were
    never added to ANY copy, so real trades were silently dropped from real P&L as "fiction." The
    visible victim was the CBOE +$660 'time_stop' win (and a TSLA +$326 'profit_target'); ~+$711 of
    REAL P&L was being excluded, UNDER-stating the book.

Defining the allowlist ONCE here and building every predicate from `real_close_predicate()` makes that
class of drift bug impossible: a new close-source is classified in exactly one place.
"""
from __future__ import annotations

# Real ENGINE exits: the position genuinely opened and closed at the broker under an engine decision,
# so its realized P&L is real money and MUST be counted (wins AND losses — excluding real losses would
# dishonestly flatter the book). 'session:%' is also real (per-session CEO/plan-driven closes) and is
# matched separately in the predicate.
REAL_CLOSE_SOURCES: tuple[str, ...] = (
    "lifecycle",         # lifecycle manager exit (21-DTE / target-date / gamma rule)
    "thesis_exit",       # thesis invalidated or target met
    "trailing_stop",     # trailing stop fired
    "stop_loss",         # hard stop fired
    "pre_earnings",      # closed ahead of earnings (IV-crush avoidance)
    "time_stop",         # time-based exit (target_close_date / hold-limit)       ← was missing
    "profit_target",     # profit target hit                                      ← was missing
    "stale_model_stop",  # model went stale → risk-managed close (real fill)      ← was missing
)

# Fiction / artifact markers — a reconstructed cost basis the engine never priced, or an unfilled
# order: excluded no matter what. (Matched as substrings, so 'reconcile_ghost', 'reconcile_dedupe',
# 'tws_orphan_reconcile', 'fabricated_unfilled' all fall under these.)
FICTION_PATTERNS: tuple[str, ...] = ("fabricated", "tws_startup_sync", "reconcile", "duplicate")


def real_close_predicate(prefix: str = "", require_pnl: bool = False) -> str:
    """SQL predicate selecting genuinely-closed engine positions — the ONE place the allowlist + fiction
    filters + adopted-exclusion live. Import this; never re-spell the whitelist inline.

    prefix:       qualify columns for a JOIN (e.g. 'p' → p.close_source, p.status …). '' = bare columns.
    require_pnl:  add `realized_pnl IS NOT NULL` (ledger / per-position attribution paths).
    """
    q = (prefix + ".") if prefix else ""
    sources = "','".join(REAL_CLOSE_SOURCES)
    parts = [
        f"{q}status='closed'",
        f"{q}close_date IS NOT NULL",
        f"{q}close_date<>''",
        f"({q}close_source IN ('{sources}') OR {q}close_source LIKE 'session:%')",
    ]
    parts += [f"{q}close_source NOT LIKE '%{pat}%'" for pat in FICTION_PATTERNS]
    parts += [
        f"COALESCE({q}regime_at_entry,'') <> 'adopted'",  # reconstructed cost basis = unreliable P&L
        f"{q}status<>'reset'",
    ]
    if require_pnl:
        parts.append(f"{q}realized_pnl IS NOT NULL")
    return " AND ".join(parts)

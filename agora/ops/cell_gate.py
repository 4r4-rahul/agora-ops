"""
agora/ops/cell_gate.py — expectancy cell-gate (roadmap S1.3).

The disciplined version of "stop trading the losers": rather than hand-blacklisting structures, gate
each strategy×pillar CELL on its own *post-fix* expectancy. A cell that has, over enough samples
since the churn fix, proven a clearly-negative expectancy gets no new capital until it earns its way
back in — and the moment its forward expectancy recovers, the gate reopens automatically. Uses only
real fills (_REAL_CLOSE) and the legacy cutoff, so stale pre-fix churn never condemns a repaired
cell. Read-only; never raises.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any

from agora.ops.edge_dashboard import _REAL_CLOSE

logger = logging.getLogger(__name__)


def cell_stats(db_path: str, cutoff: str) -> dict[tuple[str, str], dict[str, Any]]:
    """Per (strategy, pillar) post-fix expectancy over real fills. {(strat,pillar): {n, expectancy, net}}."""
    try:
        cut = str(cutoff)[:10]
        with sqlite3.connect(db_path, timeout=10) as conn:
            rows = conn.execute(
                f"SELECT strategy, pillar, realized_pnl FROM positions "
                f"WHERE {_REAL_CLOSE} AND realized_pnl IS NOT NULL AND close_date >= '{cut}'"
            ).fetchall()
    except Exception as exc:
        logger.debug("cell_gate.cell_stats: %s", exc)
        return {}
    agg: dict[tuple[str, str], list[float]] = {}
    for strat, pillar, pnl in rows:
        agg.setdefault((str(strat), str(pillar)), []).append(float(pnl))
    return {k: {"n": len(v), "expectancy": round(sum(v) / len(v), 2), "net": round(sum(v), 2)}
            for k, v in agg.items()}


def is_cell_blocked(db_path: str, strategy: str, pillar: str, *, cutoff: str,
                    min_samples: int, min_expectancy: float) -> tuple[bool, str]:
    """True if this strategy×pillar cell has proven a clearly-negative post-fix expectancy
    (n >= min_samples AND expectancy < min_expectancy). Fail-open (never block) on thin data or
    any error — we only suppress what the data has actually condemned. Returns (blocked, reason)."""
    try:
        stats = cell_stats(db_path, cutoff).get((str(strategy), str(pillar)))
        if not stats or stats["n"] < min_samples:
            return False, ""
        if stats["expectancy"] < min_expectancy:
            return True, (f"cell {strategy}/{pillar} proven negative: expectancy "
                          f"${stats['expectancy']:.0f}/trade over {stats['n']} post-fix closes "
                          f"(< ${min_expectancy:.0f}) — benched until it earns back in")
        return False, ""
    except Exception as exc:
        logger.debug("cell_gate.is_cell_blocked: %s", exc)
        return False, ""


def blocked_cells(db_path: str, *, cutoff: str, min_samples: int,
                  min_expectancy: float) -> dict[str, Any]:
    """Reporting view: which cells are currently benched + the full per-cell expectancy table."""
    stats = cell_stats(db_path, cutoff)
    blocked = {f"{s}/{p}": v for (s, p), v in stats.items()
               if v["n"] >= min_samples and v["expectancy"] < min_expectancy}
    return {"blocked": blocked,
            "all_cells": {f"{s}/{p}": v for (s, p), v in stats.items()},
            "min_samples": min_samples, "min_expectancy": min_expectancy, "cutoff": str(cutoff)[:10]}

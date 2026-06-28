"""
agora/ops/edge_readout.py — per-cell expectancy readout to CALIBRATE toward a proven edge.

The instrument the founder asked for: ask the right questions of the data, mechanically. It segments
real closes (_REAL_CLOSE) by config_version × regime × strategy × size-band and reports OUTLIER-ROBUST
per-cell stats (n, win-rate, median, expectancy) — so we can see WHERE an edge is forming as clean
post-fix data accrues, and whether expectancy improves ACROSS config-versions (did the 06-27
recalibration lift it?). Thin cells (n below threshold) are flagged not-credible, never acted on.
Read-only; never raises out.
"""
from __future__ import annotations

import sqlite3
import statistics
from typing import Any

_SIZE_BANDS = [(0, 200), (200, 400), (400, 600), (600, 800), (800, 10_000_000)]


def _band(ml: float | None) -> str:
    if ml is None:
        return "unknown"
    for lo, hi in _SIZE_BANDS:
        if lo <= ml < hi:
            return f"${lo}-{hi}" if hi < 10_000_000 else f"${lo}+"
    return "unknown"


def _stats(pnls: list[float]) -> dict[str, Any]:
    n = len(pnls)
    wins = sum(1 for p in pnls if p > 0)
    return {
        "n": n,
        "win_rate": round(wins / n, 3) if n else None,
        "expectancy": round(sum(pnls) / n, 2) if n else None,     # mean (outlier-sensitive — read with median)
        "median_pnl": round(statistics.median(pnls), 2) if n else None,
        "total": round(sum(pnls), 2) if n else None,
    }


def _load(db_path: str) -> list[dict[str, Any]]:
    from agora.ops.edge_dashboard import _REAL_CLOSE
    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            rows = conn.execute(
                f"""SELECT COALESCE(config_version_at_entry, 0) AS cv,
                          COALESCE(regime_at_entry, '') AS regime, strategy,
                          max_loss_dollars AS ml, realized_pnl AS pnl
                   FROM positions WHERE {_REAL_CLOSE} AND realized_pnl IS NOT NULL""").fetchall()
    except Exception:
        return []
    return [{"cv": int(r[0]), "regime": r[1], "strategy": r[2], "band": _band(r[3]), "pnl": float(r[4])}
            for r in rows]


def by_config_version(db_path: str) -> list[dict[str, Any]]:
    """Headline 'is the recalibration working?' view — expectancy per config_version (the regime each
    trade was made under). A rising expectancy across versions is the signal the fixes lifted the edge."""
    rows = _load(db_path)
    cells: dict[int, list[float]] = {}
    for r in rows:
        cells.setdefault(r["cv"], []).append(r["pnl"])
    return [{"config_version": cv, **_stats(p)} for cv, p in sorted(cells.items())]


def by_cell(db_path: str, min_n: int = 5) -> list[dict[str, Any]]:
    """Per (config_version × regime × strategy × size-band) cell, credible cells only (n>=min_n),
    worst expectancy first — where to cut, where to lean in, what still needs data."""
    rows = _load(db_path)
    cells: dict[tuple, list[float]] = {}
    for r in rows:
        cells.setdefault((r["cv"], r["regime"], r["strategy"], r["band"]), []).append(r["pnl"])
    out = []
    for (cv, regime, strat, band), pnls in cells.items():
        if len(pnls) >= min_n:
            out.append({"config_version": cv, "regime": regime, "strategy": strat,
                        "size_band": band, **_stats(pnls)})
    return sorted(out, key=lambda c: (c["expectancy"] if c["expectancy"] is not None else 0))


def edge_readout(db_path: str, min_n: int = 5) -> dict[str, Any]:
    rows = _load(db_path)
    return {
        "total_real_closes": len(rows),
        "by_config_version": by_config_version(db_path),
        "credible_cells": by_cell(db_path, min_n=min_n),
        "min_n_for_credible": min_n,
        "note": "Outlier-robust: read median alongside expectancy (a few big winners inflate the mean). "
                "Cells below min_n are omitted as not-credible. Watch expectancy RISE across config_versions.",
    }

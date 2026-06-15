"""
agora/ops/edge_sizing.py — edge-aware position-size multiplier (C-suite rank 12, SHIPPED DARK).

Concentrate capital on proven-edge cells and STARVE negative-edge ones — but only ever sizes DOWN,
never up, and only on a real sample, and only when explicitly enabled.

C-suite / verifier guardrails baked in:
  • SINGLE edge source: reuses StrategyHealth (positions/_REAL_CLOSE rolling Sharpe per pillar×regime)
    — NOT a new parallel edge table (the "dual edge sources" hazard the CRO flagged).
  • NEVER sizes up: hard-capped at edge_size_up_max (1.0). No subset has demonstrated positive edge,
    so up-sizing is structurally impossible until that's earned.
  • DARK by default: edge_sizing_enabled=False → always returns 1.0 (zero behavior change). Enable
    only after exits are fixed AND ≥ edge_min_sample real closes show win ≥ 55% in a cell.
  • Honest on small n: a cell below edge_min_sample returns 1.0 (neutral), never a guess.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def edge_size_multiplier(db_path: str, pillar: Any, regime: Any, settings: Any) -> float:
    """Return a size multiplier in [edge_size_down_min, edge_size_up_max] for a (pillar, regime)
    cell, from StrategyHealth's real-fill Sharpe. 1.0 = neutral. Never raises; never > size_up_max."""
    if not getattr(settings, "edge_sizing_enabled", False):
        return 1.0
    try:
        up_max   = float(getattr(settings, "edge_size_up_max", 1.0))     # pinned at 1.0 — never up
        down_min = float(getattr(settings, "edge_size_down_min", 0.5))
        min_n    = int(getattr(settings, "edge_min_sample", 30))

        from agora.ops.strategy_health import compute_health
        cells = compute_health(db_path)
        p = str(getattr(pillar, "value", pillar) or "")
        r = str(getattr(regime, "value", regime) or "neutral")
        cell = cells.get(f"{p}:{r}") or cells.get(f"{p}:")
        if not cell or int(cell.get("count", 0)) < min_n:
            return 1.0   # unproven cell — neutral, never guess

        sharpe = cell.get("sharpe")
        if sharpe is None:
            return 1.0
        # Only ever size DOWN a negative-Sharpe cell. A genuinely strong cell stays at 1.0 (cap),
        # because up-sizing on unproven edge is forbidden until size_up_max is deliberately raised.
        if sharpe < -0.5:
            mult = max(down_min, min(up_max, 1.0 + sharpe * 0.25))
            logger.info("EdgeSizing: %s:%s sharpe=%.2f n=%d -> size x%.2f",
                        p, r, sharpe, cell.get("count", 0), mult)
            return round(mult, 2)
        return min(up_max, 1.0)
    except Exception as exc:
        logger.debug("edge_size_multiplier: %s", exc)
        return 1.0

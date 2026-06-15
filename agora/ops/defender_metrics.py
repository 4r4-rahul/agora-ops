"""
agora/ops/defender_metrics.py — measured override precision for the Thesis Defender.

The ThesisDefender moderates an Advocate BLOCK -> CAUTION when it mounts a strong defense
(thesis_strength strong AND confidence >= threshold AND go_recommendation=1), letting an
otherwise-blocked trade through. Its WORTH is a single question: of the trades it let through,
how many actually won?

That was previously un-answerable (defender_journal carries the verdict but no realized outcome),
which is why the agent was retired 2026-06-09 "with NO outcome columns (can never be validated)".
The outcome IS recoverable from lifetime tables without any schema change — defender_journal links
to decision_chains (decision_id = chain_id), which links to positions (position_id), whose
realized_pnl under the canonical _REAL_CLOSE predicate is the only trustworthy P&L. This computes
that JOIN so override precision is measured on REAL fills, lifetime.

Pure observability — makes no trading decision.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any

from agora.ops.edge_dashboard import _REAL_CLOSE, _wilson_lower

logger = logging.getLogger(__name__)

_CONF_THRESHOLD = 0.65   # mirrors the session's _defender_strong gate (strong + conf >= 0.65)


def defender_override_precision(db_path: str, conf_threshold: float = _CONF_THRESHOLD) -> dict[str, Any]:
    """Return the Thesis Defender's measured override precision over REAL fills, lifetime.

    Keys:
      overrides        — strong-defense events that moderated a BLOCK (the defender's high-conviction calls)
      entered          — of those, how many became an actual position (decision_chains.position_id set)
      closed_real      — of entered, how many reached a trustworthy close (_REAL_CLOSE)
      wins / losses    — realized win/loss count among closed_real
      win_rate         — wins / closed_real (None if no closes yet)
      win_rate_wilson  — Wilson 95% lower bound (honest floor on small n)
      net_pnl/avg_pnl  — realized $ from overridden trades (real fills)
      pending          — entered but not yet real-closed (verdict still out)
      verdict          — plain-language read for humans + downstream agents
    """
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")

        base = (
            "FROM defender_journal d "
            "WHERE d.thesis_strength NOT IN ('error','') AND d.thesis_strength IS NOT NULL "
            "AND d.confidence >= ? AND d.go_recommendation = 1"
        )
        overrides = conn.execute(f"SELECT COUNT(*) {base}", (conf_threshold,)).fetchone()[0] or 0

        # Override -> position (entered) -> real close (validated outcome)
        joined = (
            "FROM defender_journal d "
            "JOIN decision_chains dc ON dc.chain_id = d.decision_id "
            "JOIN positions p ON p.position_id = dc.position_id "
            "WHERE d.thesis_strength NOT IN ('error','') AND d.thesis_strength IS NOT NULL "
            "AND d.confidence >= ? AND d.go_recommendation = 1"
        )
        entered = conn.execute(
            f"SELECT COUNT(DISTINCT p.position_id) {joined}", (conf_threshold,)
        ).fetchone()[0] or 0

        row = conn.execute(
            f"""SELECT COUNT(*) AS n,
                       SUM(CASE WHEN p.realized_pnl > 0 THEN 1 ELSE 0 END) AS wins,
                       COALESCE(SUM(p.realized_pnl), 0) AS net
                {joined} AND {_REAL_CLOSE}""",
            (conf_threshold,),
        ).fetchone()
        conn.close()

        closed = int(row[0] or 0)
        wins = int(row[1] or 0)
        net = round(float(row[2] or 0.0), 2)
        losses = closed - wins
        win_rate = round(wins / closed, 3) if closed else None
        avg_pnl = round(net / closed, 2) if closed else None
        pending = max(0, entered - closed)

        if overrides == 0:
            verdict = "No strong-defense overrides yet — nothing to judge."
        elif closed == 0:
            verdict = (f"{overrides} override(s), {entered} entered, 0 closed yet — "
                       f"precision pending (need real closes).")
        else:
            verdict = (f"Override precision {win_rate:.0%} on {closed} closed "
                       f"({wins}W/{losses}L), net ${net:+,.0f}. "
                       + ("NET POSITIVE — defender is recovering good trades."
                          if net > 0 else "NET NEGATIVE — defender is letting losers through."))

        return {
            "overrides": overrides,
            "entered": entered,
            "closed_real": closed,
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "win_rate_wilson": _wilson_lower(wins, closed) if closed else None,
            "net_pnl": net,
            "avg_pnl": avg_pnl,
            "pending": pending,
            "conf_threshold": conf_threshold,
            "verdict": verdict,
        }
    except Exception as exc:
        logger.error("defender_override_precision error: %s", exc)
        return {"error": str(exc)}

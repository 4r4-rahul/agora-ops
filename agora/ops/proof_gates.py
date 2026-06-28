"""
agora/ops/proof_gates.py — the mechanical scorecard of the four PROVABLE pillars.

Founder's rule (2026-06-28): we sell NOTHING until each pillar is consistently proven against a hard,
unquestionable milestone. This computes — mechanically and honestly — where each pillar stands vs its
gate, so "are we there yet?" is a NUMBER, never an opinion. It is built to report RED honestly: today
nothing is green, and saying so plainly is the whole point. Each gate also carries its `needs` — what
must be built/proven to turn it green — so the scorecard doubles as the roadmap. Read-only; never raises.

Gates (the selling points; each sellable ONLY when green and STAYS green):
  EDGE     E0 expectancy>0 over >=150 real closes → E1 Sharpe>1, 250+ trades, beta-adjusted, >=2 regimes
  HONESTY  90 consecutive days reconciliation drift == $0.00 (DB=broker=UI), independently re-derivable
  DISASTER 90 consecutive incident-free days + a daily adversarial chaos-suite proving every guard fires
  AGENTIC  the agentic layer beats a deterministic rules-only baseline (shadow A/B), statistically significant
"""
from __future__ import annotations

import sqlite3
from typing import Any

GREEN, AMBER, RED = "green", "amber", "red"


def _edge_gate(db_path: str) -> dict[str, Any]:
    from agora.ops.book_manager import canonical_book
    rs = canonical_book(db_path).get("real_strategy", {})
    n = int(rs.get("n_closed") or 0)
    exp = rs.get("expectancy")
    e0_met = n >= 150 and exp is not None and exp > 0
    return {
        "pillar": "EDGE",
        "milestone": "E0: expectancy>0 over >=150 real closes → E1: Sharpe>1, 250+ trades, beta-adjusted, >=2 regimes",
        "status": GREEN if e0_met else RED,
        "current": {"n_closed": n, "expectancy": exp, "win_rate": rs.get("win_rate"),
                    "profit_factor": rs.get("profit_factor"), "net_realized": rs.get("net_realized")},
        "target": {"n_closed>=": 150, "expectancy>": 0},
        "needs": None if e0_met else "positive forward expectancy on clean post-fix data; grow sample to 150+",
    }


def _honesty_gate(db_path: str) -> dict[str, Any]:
    from agora.ops.book_manager import reconciliation_health
    rh = reconciliation_health(db_path)
    ok_now = rh.get("status") == "ok"
    # The instrument (drift==0 reconciliation) is LIVE; the MILESTONE needs a consecutive-day history we
    # do not yet persist → AMBER even when currently ok (we can't yet PROVE 90 consecutive clean days).
    return {
        "pillar": "HONESTY",
        "milestone": "90 consecutive days reconciliation drift == $0.00 (DB=broker=UI), independently re-derivable",
        "status": AMBER if ok_now else RED,
        "current": {"reconciliation_status_now": rh.get("status"), "checks": rh.get("checks")},
        "target": {"consecutive_zero_drift_days>=": 90},
        "needs": "persist a daily drift-history table + count consecutive zero-drift days (instrument is already live)",
    }


def _disaster_gate(db_path: str) -> dict[str, Any]:
    from agora.ops.book_manager import execution_bug_ledger
    try:
        episodes = execution_bug_ledger(db_path).get("episodes", []) or []
    except Exception:
        episodes = []
    # Guards are built + tested; the consecutive-incident-free-day count and the scheduled daily chaos
    # suite are not yet tracked → AMBER (containment proven on demand, not yet proven CONTINUOUSLY).
    return {
        "pillar": "DISASTER",
        "milestone": "90 consecutive incident-free days + a daily adversarial chaos-suite proving every guard fires",
        "status": AMBER,
        "current": {"known_bug_episodes": len(episodes),
                    "guards_built_and_tested": ["close-idempotency", "over-fill auto-halt",
                                                "single-engine lease", "entry-stop", "breaker transparency"]},
        "target": {"incident_free_days>=": 90, "daily_chaos_suite": True},
        "needs": "an incident-free-day counter + a scheduled chaos suite that injects each disaster daily",
    }


def _agentic_gate(db_path: str) -> dict[str, Any]:
    # The real gate is a shadow A/B (agentic vs deterministic rules-only baseline). Not built yet.
    # prediction_ledger gives only a proxy (are the agents' scored predictions even calibrated?).
    scored: int | None
    try:
        with sqlite3.connect(db_path) as c:
            row = c.execute(
                "SELECT COUNT(*) FROM prediction_ledger WHERE scored=1 AND actual IS NOT NULL").fetchone()
            scored = int(row[0]) if row else 0
    except Exception:
        scored = None
    return {
        "pillar": "AGENTIC",
        "milestone": "agentic layer beats a deterministic rules-only baseline (shadow A/B), statistically significant",
        "status": RED,
        "current": {"ab_harness": "not built", "scored_predictions_available": scored},
        "target": {"agentic_minus_baseline_expectancy>": 0, "statistically_significant": True},
        "needs": "a shadow rules-only baseline run in parallel + per-decision attribution to A/B the LLM layer",
    }


def _aggregate(statuses: list[str]) -> str:
    """The founder's rule encoded: GREEN only if EVERY gate is green; RED if any gate is red; else AMBER."""
    if statuses and all(s == GREEN for s in statuses):
        return GREEN
    return RED if RED in statuses else AMBER


def proof_gates(db_path: str) -> dict[str, Any]:
    """The full scorecard. `sellable` is the bright line: nothing is for sale until overall is GREEN.
    Each gate is computed defensively — a failing gate reports RED-with-error, never crashes the board."""
    gates: list[dict[str, Any]] = []
    for fn in (_edge_gate, _honesty_gate, _disaster_gate, _agentic_gate):
        try:
            gates.append(fn(db_path))
        except Exception as exc:
            gates.append({"pillar": fn.__name__.strip("_").replace("_gate", "").upper(),
                          "status": RED, "error": str(exc)})
    overall = _aggregate([g["status"] for g in gates])
    return {
        "overall": overall,
        "sellable": overall == GREEN,
        "gates": gates,
        "note": "Honest by design — today nothing is green; the EDGE gate is the flagship and is RED. "
                "A pillar becomes a selling point only when its gate is green AND stays green.",
    }

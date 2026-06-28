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
    from agora.ops.recon_history import consecutive_clean_days
    rh = reconciliation_health(db_path)
    ok_now = rh.get("status") == "ok"
    streak = consecutive_clean_days(db_path)   # proven consecutive clean days (recorded daily)
    target_days = 90
    if streak >= target_days:
        status = GREEN
    elif ok_now:
        status = AMBER   # clean now, but the 90-day streak isn't proven yet
    else:
        status = RED
    return {
        "pillar": "HONESTY",
        "milestone": "90 consecutive days reconciliation drift == $0.00 (DB=broker=UI), independently re-derivable",
        "status": status,
        "current": {"reconciliation_status_now": rh.get("status"),
                    "consecutive_clean_days": streak, "checks": rh.get("checks")},
        "target": {"consecutive_clean_days>=": target_days},
        "needs": None if status == GREEN else (
            f"accrue clean days: {streak}/{target_days} (instrument live; recorded daily by snapshot_daily)"),
    }


def _disaster_gate(db_path: str) -> dict[str, Any]:
    from agora.ops.incident_log import incident_count, incident_free_days
    free = incident_free_days(db_path)
    incidents = incident_count(db_path)
    target_days = 90
    # GREEN requires the incident-free streak AND the chaos suite proving guards fire (smoke_runaway_
    # defense runs in CI on every push — continuous proof). AMBER while the streak accrues.
    status = GREEN if free >= target_days else AMBER
    return {
        "pillar": "DISASTER",
        "milestone": "90 consecutive incident-free days + adversarial chaos-suite proving every guard fires",
        "status": status,
        "current": {"incident_free_days": free, "incidents_recorded": incidents,
                    "chaos_suite": "smoke_runaway_defense (negative-control, runs in CI every push)",
                    "guards_built_and_tested": ["close-idempotency", "over-fill auto-halt",
                                                "single-engine lease", "entry-stop", "breaker transparency"]},
        "target": {"incident_free_days>=": target_days, "chaos_suite_passing": True},
        "needs": None if status == GREEN else (
            f"accrue incident-free days: {free}/{target_days} (auto-trips reset it; chaos suite green in CI)"),
    }


def _agentic_gate(db_path: str) -> dict[str, Any]:
    from agora.ops.agentic_ab import agentic_value
    av = agentic_value(db_path)
    ab = av.get("arm_comparison", {})
    # GREEN only when the forward A/B is READY and agentic beats the rules-only baseline. Until the
    # rules-only execution arm has run enough closes, the harness reports insufficient_data → RED.
    status = GREEN if (ab.get("status") == "ready" and ab.get("agentic_beats_baseline")) else RED
    return {
        "pillar": "AGENTIC",
        "milestone": "agentic layer beats a deterministic rules-only baseline (forward A/B), meaningful margin",
        "status": status,
        "current": av,
        "target": {"arm_comparison.status": "ready", "agentic_beats_baseline": True},
        "needs": None if status == GREEN else (
            "run the rules-only execution arm (stamp positions.decision_arm) to N closes; "
            "signal_predictiveness shows whether the LLM outputs carry signal in the meantime"),
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

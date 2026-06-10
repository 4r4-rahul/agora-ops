"""
agora/ops/agent_value_monitor.py — per-agent value-vs-cost reevaluation (the "every penny" loop).

Each LLM agent costs real money (llm_cost_log) and must EARN it in measurable outcomes. This
module joins spend to attributed results (outcome_attributor wrote them by exact decision_id) and
emits a per-agent verdict: is the agent adding value, is it unvalidated, or is it a retire
candidate? It runs in the attribution cycle and logs a scorecard the operator can act on.

Honest by design: every metric carries its sample size; with tiny n it reports "insufficient
data" rather than a false verdict. It FLAGS retire candidates — it does not auto-disable (that
stays a human decision), matching how the ThesisDefender was retired manually on this evidence.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

logger = logging.getLogger(__name__)

_MIN_SAMPLE = 20   # below this, a value verdict is not trustworthy (selective-prediction lit)


def _spend(conn: sqlite3.Connection, agent_like: str) -> tuple[int, float]:
    row = conn.execute(
        "SELECT count(*), COALESCE(sum(cost_usd),0) FROM llm_cost_log WHERE agent LIKE ?",
        (agent_like,),
    ).fetchone()
    return int(row[0] or 0), round(float(row[1] or 0.0), 2)


def compute_agent_value(db_path: str) -> dict[str, Any]:
    """Return a per-agent {spend, calls, attributed, value_signal, verdict} scorecard."""
    out: dict[str, Any] = {}
    try:
        conn = sqlite3.connect(db_path)
    except Exception as exc:
        return {"error": str(exc)}
    try:
        # ── Advocate: does following it (take PASS, skip BLOCK) beat taking everything? ──
        calls, usd = _spend(conn, "AdvocateAgent")
        rows = conn.execute(
            """SELECT verdict, count(*), COALESCE(avg(realized_pnl),0),
                      COALESCE(sum(advocate_was_right),0)
               FROM advocate_journal WHERE trade_taken=1 GROUP BY verdict""",
        ).fetchall()
        by = {r[0]: (r[1], round(r[2], 1), r[3]) for r in rows}
        passes = by.get("PASS", (0, 0.0, 0)); blocks = by.get("BLOCK", (0, 0.0, 0))
        n_attr = passes[0] + blocks[0] + by.get("CAUTION", (0, 0, 0))[0]
        err = conn.execute("SELECT count(*) FROM advocate_journal WHERE verdict='error'").fetchone()[0]
        err_rate = round(err / max(1, err + calls), 3)
        # value = avg P&L of PASS minus avg P&L of BLOCK (positive ⇒ it discriminates)
        discr = round(passes[1] - blocks[1], 1)
        if n_attr < _MIN_SAMPLE:
            verdict = f"UNVALIDATED (n={n_attr}<{_MIN_SAMPLE})"
        elif discr > 0:
            verdict = f"adds value (PASS−BLOCK avg P&L = +{discr})"
        else:
            verdict = f"NEGATIVE value (PASS−BLOCK = {discr}) — review"
        out["advocate"] = {
            "spend_usd": usd, "calls": calls, "error_rate": err_rate, "attributed": n_attr,
            "pass": passes, "block": blocks, "discrimination_pnl": discr, "verdict": verdict,
        }

        # ── StrategySelector: now that the override is WIRED, measure the only honest signal —
        # the realized P&L of structures the selector ACTUALLY changed (override_honored=1)
        # against the rules-engine population it left alone. Before wiring, override_honored did
        # not exist and every 'override' metric measured a structure that never traded.
        calls, usd = _spend(conn, "StrategySelectorAgent")
        _has_honored = "override_honored" in {r[1] for r in conn.execute("PRAGMA table_info(strategy_journal)")}
        if _has_honored:
            hrow = conn.execute(
                """SELECT
                     COALESCE(SUM(CASE WHEN override_honored=1 THEN 1 END),0),
                     COALESCE(AVG(CASE WHEN override_honored=1 THEN realized_pnl END),0),
                     COALESCE(SUM(CASE WHEN override_honored=1 AND realized_pnl>0 THEN 1 END),0),
                     COALESCE(SUM(CASE WHEN COALESCE(override_honored,0)<>1 THEN 1 END),0),
                     COALESCE(AVG(CASE WHEN COALESCE(override_honored,0)<>1 THEN realized_pnl END),0)
                   FROM strategy_journal WHERE structure_used=1""",
            ).fetchone()
            n_ov, avg_ov, win_ov, n_base, avg_base = (
                int(hrow[0]), round(hrow[1], 1), int(hrow[2]), int(hrow[3]), round(hrow[4], 1))
            if n_ov < _MIN_SAMPLE:
                verdict = (f"UNVALIDATED (honored overrides n={n_ov}<{_MIN_SAMPLE}); "
                           f"baseline rules-engine n={n_base} avg=${avg_base}")
            else:
                edge = round(avg_ov - avg_base, 1)
                verdict = (f"override avg=${avg_ov} ({win_ov}/{n_ov} win) vs rules-engine "
                           f"avg=${avg_base} (n={n_base}) → edge ${edge}"
                           + (" — adds value" if edge > 0 else " — NOT adding value, review"))
            out["strategy_selector"] = {
                "spend_usd": usd, "calls": calls, "honored_overrides": n_ov,
                "override_avg_pnl": avg_ov, "rules_baseline_avg_pnl": avg_base,
                "baseline_n": n_base, "verdict": verdict,
            }
        else:
            out["strategy_selector"] = {
                "spend_usd": usd, "calls": calls,
                "verdict": "override wiring just deployed — no honored overrides closed yet",
            }

        # ── Analyst: thesis hit-rate (now accruing post token-fix) ──
        calls, usd = _spend(conn, "StockAnalystAgent")
        arow = conn.execute(
            """SELECT count(*), COALESCE(sum(thesis_played_out),0)
               FROM analyst_journal WHERE thesis_played_out IS NOT NULL""",
        ).fetchone()
        n_an, won_an = int(arow[0]), int(arow[1])
        out["analyst"] = {
            "spend_usd": usd, "calls": calls, "attributed": n_an,
            "hit_rate": round(won_an / n_an, 2) if n_an else None,
            "verdict": (f"UNVALIDATED (n={n_an})" if n_an < _MIN_SAMPLE
                        else f"hit_rate={round(won_an/n_an,2)}"),
        }

        # ── Long-options vetter: learning loop status ──
        calls, usd = _spend(conn, "LongOptionsVetter%")
        vrow = conn.execute(
            "SELECT count(*), COALESCE(sum(CASE WHEN outcome!='' THEN 1 END),0) FROM long_vetter_log",
        ).fetchone()
        out["long_vetter"] = {
            "spend_usd": usd, "calls": int(vrow[0]),
            "outcomes_backfilled": int(vrow[1] or 0),
            "verdict": ("DEAD LOOP — 0 outcomes backfilled; cannot validate"
                        if not vrow[1] else f"{vrow[1]} outcomes"),
        }
        return out
    finally:
        conn.close()


def log_agent_value(db_path: str) -> dict[str, Any]:
    """Compute + log the scorecard; flag retire candidates. Called from the attribution cycle."""
    rep = compute_agent_value(db_path)
    if "error" in rep:
        return rep
    for agent, m in rep.items():
        logger.info("AgentValue[%s] $%.2f / %d calls | %s",
                    agent, m.get("spend_usd", 0), m.get("calls", 0), m.get("verdict", ""))
        v = str(m.get("verdict", ""))
        if v.startswith("NEGATIVE") or v.startswith("DEAD LOOP"):
            logger.warning("AgentValue RETIRE-CANDIDATE [%s]: $%.2f spent, %s",
                           agent, m.get("spend_usd", 0), v)
    return rep

"""
M1 · Fill model — learns how fillable each structure is, from execution_quality (fills/timeouts/
rejects/slippage). Output: a per-strategy fill-favorability score + slippage + dominant reject reason,
so the analyst/engine can see which structures actually fill and how aggressively to price them.

Descriptive (no trade-label needed) → produces real scores now on abundant data. Windows to RECENT
attempts so the score reflects the CURRENT fill regime (the historical paper-latency timeouts would
otherwise depress it). READ-ONLY on execution_quality; writes only via the model-runner. Never raises.
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Any

from agora.ops.model_runner import register_model

_WINDOW_DAYS = 14
_MIN_RECENT = 30        # if fewer recent attempts than this, fall back to all-time
_MIN_PER_STRAT = 10     # only score strategies with enough attempts to be meaningful


def fill_model(db_path: str) -> dict[str, Any]:
    """Pure read; never raises (the runner also isolates, but be defensive)."""
    try:
        conn = sqlite3.connect(db_path, timeout=8); conn.row_factory = sqlite3.Row
    except Exception as exc:
        return {"status": "error", "n_samples": 0, "readiness": "BOOTSTRAP", "metrics": {}, "summary": str(exc)}
    try:
        has = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='execution_quality'").fetchone()[0]
        if not has:
            return {"status": "skipped", "n_samples": 0, "readiness": "BOOTSTRAP", "metrics": {},
                    "summary": "no execution data"}
        cutoff = (date.today() - timedelta(days=_WINDOW_DAYS)).isoformat()
        recent_n = conn.execute(
            "SELECT COUNT(*) FROM execution_quality WHERE attempt_date >= ?", (cutoff,)).fetchone()[0]
        window = "recent" if recent_n >= _MIN_RECENT else "all-time"
        where = "attempt_date >= ?" if window == "recent" else "1=1"
        args: tuple = (cutoff,) if window == "recent" else ()

        rows = conn.execute(
            f"""SELECT strategy, COUNT(*) n,
                       SUM(outcome IN ('fill','fill_closed')) fills,
                       SUM(outcome='timeout') timeouts,
                       AVG(CASE WHEN outcome IN ('fill','fill_closed') THEN slippage_ticks END) avg_slip
                FROM execution_quality WHERE {where} GROUP BY strategy""", args).fetchall()
        total_n = sum(r["n"] for r in rows)
        total_fills = sum(r["fills"] or 0 for r in rows)
        overall_fill = round(total_fills / total_n, 3) if total_n else None

        scores = []
        per_strat = {}
        for r in rows:
            if (r["n"] or 0) < _MIN_PER_STRAT:
                continue
            fr = round((r["fills"] or 0) / r["n"], 3)
            # dominant reject reason for this strategy in-window
            rej = conn.execute(
                f"""SELECT reject_reason FROM execution_quality
                    WHERE {where} AND strategy=? AND outcome='reject'
                    GROUP BY reject_reason ORDER BY COUNT(*) DESC LIMIT 1""",
                (*args, r["strategy"])).fetchone()
            meta = {"n": r["n"], "fill_rate": fr, "timeout_rate": round((r["timeouts"] or 0) / r["n"], 3),
                    "avg_slippage_ticks": round(r["avg_slip"], 2) if r["avg_slip"] is not None else None,
                    "top_reject": (rej["reject_reason"][:60] if rej and rej["reject_reason"] else None),
                    "window": window}
            per_strat[r["strategy"]] = meta
            scores.append({"entity_type": "strategy", "entity_id": r["strategy"], "score": fr, "meta": meta})

        ranked = sorted(per_strat.items(), key=lambda kv: kv[1]["fill_rate"])
        hardest = ranked[0][0] if ranked else "—"
        easiest = ranked[-1][0] if ranked else "—"
        # readiness: descriptive model — reliable once enough attempts exist
        rd = "TRAINABLE" if total_n >= 200 else ("EMERGING" if total_n >= 50 else "BOOTSTRAP")
        return {
            "status": "ok", "n_samples": total_n, "readiness": rd,
            "metrics": {"window": window, "overall_fill_rate": overall_fill,
                        "by_strategy": per_strat},
            "summary": (f"fill {overall_fill*100:.0f}% ({window}, n={total_n}) · hardest {hardest} "
                        f"{per_strat.get(hardest,{}).get('fill_rate',0)*100:.0f}% · easiest {easiest} "
                        f"{per_strat.get(easiest,{}).get('fill_rate',0)*100:.0f}%") if overall_fill is not None
                       else "no attempts in window",
            "scores": scores,
        }
    finally:
        conn.close()


register_model("fill_model", cadence_days=0.5, fn=fill_model)

"""
M8 · Lifecycle attribution — learns the MANAGEMENT edge from the daily film: how a trade's path
(max adverse / max favorable excursion, days held) relates to its outcome, so we can answer 'when to
take profit / cut losses'. The longitudinal capstone of the fleet.

Only labeled trades that ALSO have lifecycle path features count — so it stays BOOTSTRAP until the
daily film accumulates closed trades (capture started 2026-06-22). SHADOW-ONLY + n-gated; READ-ONLY on
trade_features; never raises. Reports the emerging take-profit / stop signal with explicit n caveats.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from agora.ops.model_runner import readiness_for, register_model


def _avg(vals: list[float]) -> float | None:
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 3) if vals else None


def lifecycle_attribution_model(db_path: str) -> dict[str, Any]:
    try:
        conn = sqlite3.connect(db_path, timeout=8); conn.row_factory = sqlite3.Row
    except Exception as exc:
        return {"status": "error", "n_samples": 0, "readiness": "BOOTSTRAP", "metrics": {}, "summary": str(exc)}
    try:
        if not conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='trade_features'").fetchone()[0]:
            return {"status": "skipped", "n_samples": 0, "readiness": "BOOTSTRAP", "metrics": {},
                    "summary": "feature store not built"}
        rows = conn.execute(
            """SELECT max_adverse_pct mae, max_favorable_pct mfe, days_held dh, win
               FROM trade_features WHERE win IS NOT NULL AND n_frames IS NOT NULL""").fetchall()
        n = len(rows)
        rd = readiness_for(n)
        actionable = rd != "BOOTSTRAP"
        winners = [r for r in rows if (r["win"] or 0) == 1]
        losers = [r for r in rows if (r["win"] or 0) == 0]

        profile = {
            "winners": {"n": len(winners),
                        "avg_max_adverse": _avg([r["mae"] for r in winners]),
                        "avg_max_favorable": _avg([r["mfe"] for r in winners]),
                        "avg_days_held": _avg([float(r["dh"]) for r in winners if r["dh"] is not None])},
            "losers": {"n": len(losers),
                       "avg_max_adverse": _avg([r["mae"] for r in losers]),
                       "avg_max_favorable": _avg([r["mfe"] for r in losers]),
                       "avg_days_held": _avg([float(r["dh"]) for r in losers if r["dh"] is not None])},
        }

        # emerging signals (advisory; only assert when not bootstrap)
        signals = []
        wmf = profile["winners"]["avg_max_favorable"]
        lma = profile["losers"]["avg_max_adverse"]
        if actionable and wmf is not None:
            signals.append(f"winners peaked at ~{wmf*100:.0f}% of max-gain before close → take-profit anchor")
        if actionable and lma is not None:
            signals.append(f"losers' worst adverse ~{lma*100:.0f}% of max-loss → early-cut anchor")

        scores = []
        if wmf is not None:
            scores.append({"entity_type": "global", "entity_id": "winner_take_profit_pct",
                           "score": wmf, "meta": {"n": len(winners), "actionable": actionable}})
        if lma is not None:
            scores.append({"entity_type": "global", "entity_id": "loser_max_adverse_pct",
                           "score": lma, "meta": {"n": len(losers), "actionable": actionable}})

        summary = (f"n={n} filmed+labeled · {'shadow-only (BOOTSTRAP)' if not actionable else 'actionable'}"
                   + (" · " + "; ".join(signals) if signals else
                      " · path attribution pending — film accumulating since 2026-06-22"))
        return {"status": "ok", "n_samples": n, "readiness": rd,
                "metrics": {"actionable": actionable, "path_profile": profile, "signals": signals},
                "summary": summary, "scores": scores}
    finally:
        conn.close()


register_model("lifecycle_attribution_model", cadence_days=1.0, fn=lifecycle_attribution_model)

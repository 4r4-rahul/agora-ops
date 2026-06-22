"""
M5 · Conviction calibration — does conviction_at_entry actually predict outcome? Maps conviction bands
to ACTUAL win-rate + avg-P&L from the feature store, and reports a predictiveness verdict.

This is the HONEST replacement for the old 'conviction gate INVERTED' alarm that fired on n=2 +
fabricated rows: it reads only clean labeled trade_features, carries Wilson bounds, and REFUSES to
assert 'predictive' / 'inverted' until EMERGING (n>=20). At BOOTSTRAP it just shows the emerging curve
with a loud 'insufficient n' caveat. READ-ONLY; shadow-only; never raises.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from agora.ops.edge_dashboard import _wilson_lower
from agora.ops.model_runner import readiness_for, register_model

_BANDS = [("high(>=70)", 70, 1e9), ("std(55-69)", 55, 70), ("low(<55)", 1, 55)]
_MIN_BAND_N = 3


def conviction_calibration_model(db_path: str) -> dict[str, Any]:
    try:
        conn = sqlite3.connect(db_path, timeout=8); conn.row_factory = sqlite3.Row
    except Exception as exc:
        return {"status": "error", "n_samples": 0, "readiness": "BOOTSTRAP", "metrics": {}, "summary": str(exc)}
    try:
        if not conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='trade_features'").fetchone()[0]:
            return {"status": "skipped", "n_samples": 0, "readiness": "BOOTSTRAP", "metrics": {},
                    "summary": "feature store not built"}
        rows = conn.execute(
            "SELECT conviction_at_entry c, win, realized_pnl p FROM trade_features "
            "WHERE win IS NOT NULL AND conviction_at_entry > 0").fetchall()
        n = len(rows)
        rd = readiness_for(n)
        actionable = rd != "BOOTSTRAP"

        curve = {}
        for label, lo, hi in _BANDS:
            band = [r for r in rows if lo <= (r["c"] or 0) < hi]
            if len(band) >= _MIN_BAND_N:
                w = sum(int(r["win"] or 0) for r in band)
                curve[label] = {"n": len(band), "win_rate": round(w / len(band), 3),
                                "avg_pnl": round(sum(r["p"] or 0 for r in band) / len(band), 2),
                                "win_rate_wilson_lb": _wilson_lower(w, len(band))}

        # predictiveness verdict — ONLY assert when not bootstrap and high & low bands both populated
        verdict = "insufficient_n"
        if actionable and "high(>=70)" in curve and "low(<55)" in curve:
            hi_pnl = curve["high(>=70)"]["avg_pnl"]
            lo_pnl = curve["low(<55)"]["avg_pnl"]
            verdict = "predictive" if hi_pnl > lo_pnl else ("flat" if hi_pnl == lo_pnl else "inverted")

        scores = [{"entity_type": "conviction_band", "entity_id": k, "score": d["win_rate"],
                   "meta": {**d, "actionable": actionable}} for k, d in curve.items()]
        summary = (f"n={n} · verdict={verdict}"
                   + (" · " + " | ".join(f"{k} WR {d['win_rate']*100:.0f}%(n={d['n']})"
                                          for k, d in curve.items()) if curve else " · no populated bands"))
        return {"status": "ok", "n_samples": n, "readiness": rd,
                "metrics": {"actionable": actionable, "verdict": verdict, "calibration_curve": curve},
                "summary": summary, "scores": scores}
    finally:
        conn.close()


register_model("conviction_calibration_model", cadence_days=1.0, fn=conviction_calibration_model)

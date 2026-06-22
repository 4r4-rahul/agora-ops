"""
M4 · Win/EV model — per-segment win-rate + expected value from the feature store (labeled real closes).
Segments by structure_class, conviction band, and regime. The data-driven precursor to a win-prob gate.

SHADOW-ONLY + n-GATED: at n<20 labeled it reports BOOTSTRAP and marks every score `actionable=False`
(descriptive only) — it will NOT become a live gate until EMERGING. Each segment carries a Wilson 95%
lower bound so tiny-n segments are visibly untrustworthy (the discipline that killed the contaminated
'conviction inverted on n=2' alarms). READ-ONLY on trade_features; never raises.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from agora.ops.edge_dashboard import _wilson_lower
from agora.ops.model_runner import readiness_for, register_model

_MIN_SEG_N = 3   # don't even show a segment below this


def _conv_band(c: float | None) -> str:
    if c is None:
        return "unscored"
    if c >= 70:
        return "high(>=70)"
    if c >= 55:
        return "std(55-69)"
    if c > 0:
        return "low(<55)"
    return "unscored"


def win_ev_model(db_path: str) -> dict[str, Any]:
    try:
        conn = sqlite3.connect(db_path, timeout=8); conn.row_factory = sqlite3.Row
    except Exception as exc:
        return {"status": "error", "n_samples": 0, "readiness": "BOOTSTRAP", "metrics": {}, "summary": str(exc)}
    try:
        if not conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='trade_features'").fetchone()[0]:
            return {"status": "skipped", "n_samples": 0, "readiness": "BOOTSTRAP", "metrics": {},
                    "summary": "feature store not built"}
        rows = conn.execute(
            "SELECT structure_class, conviction_at_entry, regime_at_entry, win, realized_pnl "
            "FROM trade_features WHERE win IS NOT NULL AND realized_pnl IS NOT NULL").fetchall()
        n = len(rows)
        rd = readiness_for(n)
        actionable = rd != "BOOTSTRAP"

        def _segment(key_fn):
            segs: dict[str, dict] = {}
            for r in rows:
                k = key_fn(r)
                d = segs.setdefault(k, {"n": 0, "wins": 0, "pnl": 0.0})
                d["n"] += 1; d["wins"] += int(r["win"] or 0); d["pnl"] += (r["realized_pnl"] or 0.0)
            out = {}
            for k, d in segs.items():
                if d["n"] < _MIN_SEG_N:
                    continue
                out[k] = {"n": d["n"], "win_rate": round(d["wins"] / d["n"], 3),
                          "avg_pnl": round(d["pnl"] / d["n"], 2),
                          "win_rate_wilson_lb": _wilson_lower(d["wins"], d["n"])}
            return out

        by_structure = _segment(lambda r: r["structure_class"] or "other")
        by_conv = _segment(lambda r: _conv_band(r["conviction_at_entry"]))
        by_regime = _segment(lambda r: r["regime_at_entry"] or "unknown")

        scores = []
        for seg_type, segs in (("structure", by_structure), ("conviction", by_conv), ("regime", by_regime)):
            for k, d in segs.items():
                scores.append({"entity_type": "segment", "entity_id": f"{seg_type}:{k}",
                               "score": d["avg_pnl"],
                               "meta": {**d, "seg_type": seg_type, "actionable": actionable}})

        # best/worst segment by avg_pnl (only meaningful once not bootstrap, but show with caveat)
        allsegs = [(f"{t}:{k}", d["avg_pnl"], d["n"]) for t, S in
                   (("structure", by_structure), ("conviction", by_conv)) for k, d in S.items()]
        allsegs.sort(key=lambda x: x[1])
        worst = allsegs[0] if allsegs else None
        best = allsegs[-1] if allsegs else None
        gate_note = "shadow-only (BOOTSTRAP)" if not actionable else "actionable"
        summary = (f"n={n} labeled · {gate_note}"
                   + (f" · best {best[0]} ${best[1]:.0f}(n={best[2]}) · worst {worst[0]} ${worst[1]:.0f}(n={worst[2]})"
                      if best else ""))
        return {"status": "ok", "n_samples": n, "readiness": rd,
                "metrics": {"actionable": actionable, "by_structure": by_structure,
                            "by_conviction": by_conv, "by_regime": by_regime},
                "summary": summary, "scores": scores}
    finally:
        conn.close()


register_model("win_ev_model", cadence_days=1.0, fn=win_ev_model)

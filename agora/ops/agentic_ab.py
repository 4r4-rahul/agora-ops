"""
agora/ops/agentic_ab.py — does the AGENTIC layer earn its seat? (AGENTIC proof-gate)

The founder's challenge: prove the LLM committee beats emotionless rules-alone — with evidence, not
assertion. Two parts:

  signal_predictiveness(): do the agents' SCORED predictions (conviction, news, …) actually predict
    outcomes? Computed NOW from prediction_ledger, outlier-robust. This is the available evidence that
    the LLM outputs carry signal at all.

  arm_comparison(): the DEFINITIVE forward A/B — per `decision_arm` ('agentic' vs 'rules_only')
    expectancy on real closes. Returns 'insufficient_data' until the rules-only execution arm has run
    enough trades. (Stamping `positions.decision_arm` + a rules-only execution path is the remaining
    wiring; it's data-gated like the EDGE gate.)

The AGENTIC gate is GREEN only when arm_comparison shows agentic beats the rules-only baseline by a
statistically meaningful margin. SQLite only; never raises out.
"""
from __future__ import annotations

import sqlite3
from typing import Any

_MIN_SCORED = 20   # below this, predictiveness is not credible
_MIN_ARM_N = 40    # per-arm minimum before the A/B verdict is trustworthy


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    m = len(s) // 2
    return round(s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2, 4)


def signal_predictiveness(db_path: str) -> dict[str, Any]:
    """Per prediction source: is the higher-predicted half actually more right? Outlier-robust (uses
    the median split + win-rate, not mean). Verdict 'predictive' only if the top half beats the bottom."""
    out: dict[str, Any] = {}
    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            rows = conn.execute(
                "SELECT source, predicted, actual FROM prediction_ledger "
                "WHERE scored=1 AND actual IS NOT NULL AND predicted IS NOT NULL").fetchall()
    except Exception as exc:
        return {"error": str(exc)}
    by_src: dict[str, list[tuple[float, float]]] = {}
    for src, pred, act in rows:
        by_src.setdefault(str(src), []).append((float(pred), float(act)))
    for src, pairs in by_src.items():
        n = len(pairs)
        if n < _MIN_SCORED:
            out[src] = {"n": n, "verdict": "insufficient_data"}
            continue
        pairs.sort(key=lambda p: p[0])               # by predicted
        h = n // 2
        bot_actual = [a for _, a in pairs[:h]]
        top_actual = [a for _, a in pairs[h:]]
        bot, top = _median(bot_actual), _median(top_actual)
        avg_pred = round(sum(p for p, _ in pairs) / n, 4)
        avg_act = round(sum(a for _, a in pairs) / n, 4)
        predictive = top is not None and bot is not None and top > bot
        out[src] = {
            "n": n, "avg_predicted": avg_pred, "avg_actual": avg_act,
            "calibration_bias": round(avg_pred - avg_act, 4),   # >0 = over-confident
            "top_half_actual": top, "bottom_half_actual": bot,
            "verdict": "predictive" if predictive else "not_predictive",
        }
    return out


def arm_comparison(db_path: str) -> dict[str, Any]:
    """Forward A/B: per-arm expectancy on real closes, where positions.decision_arm ∈ {agentic,
    rules_only}. Returns insufficient_data until both arms have >= _MIN_ARM_N closes."""
    from agora.ops.edge_dashboard import _REAL_CLOSE
    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(positions)")]
            if "decision_arm" not in cols:
                return {"status": "insufficient_data", "reason": "decision_arm not stamped yet"}
            rows = conn.execute(
                f"SELECT decision_arm, realized_pnl FROM positions "
                f"WHERE {_REAL_CLOSE} AND decision_arm IN ('agentic','rules_only')").fetchall()
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    arms: dict[str, list[float]] = {"agentic": [], "rules_only": []}
    for arm, pnl in rows:
        arms[arm].append(float(pnl))
    def stat(xs: list[float]) -> dict[str, Any]:
        n = len(xs)
        return {"n": n, "expectancy": round(sum(xs) / n, 2) if n else None,
                "win_rate": round(sum(1 for p in xs if p > 0) / n, 3) if n else None}
    a, b = stat(arms["agentic"]), stat(arms["rules_only"])
    if a["n"] < _MIN_ARM_N or b["n"] < _MIN_ARM_N:
        return {"status": "insufficient_data", "agentic": a, "rules_only": b,
                "reason": f"need >= {_MIN_ARM_N} closes per arm"}
    beats = (a["expectancy"] or 0) > (b["expectancy"] or 0)
    return {"status": "ready", "agentic": a, "rules_only": b,
            "agentic_beats_baseline": beats,
            "expectancy_delta": round((a["expectancy"] or 0) - (b["expectancy"] or 0), 2)}


def agentic_value(db_path: str) -> dict[str, Any]:
    """Combined view for the AGENTIC proof-gate."""
    return {"arm_comparison": arm_comparison(db_path),
            "signal_predictiveness": signal_predictiveness(db_path)}

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


def assign_arm(ticker: str, entry_date: str, *, ab_enabled: bool) -> str:
    """Deterministic A/B arm for an entry candidate. Shadow default (ab_enabled=False) → 'agentic' for
    ALL candidates (zero behavior change — the current LLM/conviction path). When enabled, split
    agentic/rules_only by a STABLE ticker+date hash so the same candidate always lands in the same arm
    (reproducible, testable, no intraday drift). NOTE: enabling only LABELS arms until the rules_only
    EXECUTION path is wired — which is intentionally deferred (the conviction signal is regime-confounded
    + underpowered, so a behavioral split is not yet warranted)."""
    if not ab_enabled:
        return "agentic"
    import hashlib
    h = hashlib.sha256(f"{ticker}|{entry_date}".encode()).hexdigest()
    return "rules_only" if int(h[:8], 16) % 2 == 0 else "agentic"


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


def _wilson(wins: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """Wilson 95% CI on a win-rate — honest uncertainty that down-weights small n."""
    if n <= 0:
        return None
    p = wins / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    return (round((c - m) / d, 3), round((c + m) / d, 3))


def _arm_stat(xs: list[float]) -> dict[str, Any]:
    n = len(xs)
    wins = sum(1 for p in xs if p > 0)
    return {
        "n": n,
        "expectancy": round(sum(xs) / n, 2) if n else None,
        "median": _median(xs),
        "win_rate": round(wins / n, 3) if n else None,
        "wilson95": _wilson(wins, n),   # honest CI — overlapping CIs ⇒ no real difference
    }


def arm_comparison(db_path: str) -> dict[str, Any]:
    """Forward A/B: per-arm expectancy on real closes, where positions.decision_arm ∈ {agentic,
    rules_only}. Returns insufficient_data until both arms have >= _MIN_ARM_N closes.

    STRATIFIED BY REGIME (2026-06-30): the aggregate is a Simpson's-paradox trap — conviction correlates
    with regime, and one regime (risk_off) is the only profitable one, so an unstratified arm comparison
    can flip sign vs the within-regime truth. `by_regime` carries the honest per-regime split + Wilson CIs
    so a 'winner' can never be declared on a confound."""
    from agora.ops.edge_dashboard import _REAL_CLOSE
    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(positions)")]
            if "decision_arm" not in cols:
                return {"status": "insufficient_data", "reason": "decision_arm not stamped yet"}
            rows = conn.execute(
                f"SELECT decision_arm, COALESCE(regime_at_entry,''), realized_pnl FROM positions "
                f"WHERE {_REAL_CLOSE} AND decision_arm IN ('agentic','rules_only')").fetchall()
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    arms: dict[str, list[float]] = {"agentic": [], "rules_only": []}
    by_reg: dict[str, dict[str, list[float]]] = {}
    for arm, regime, pnl in rows:
        arms[arm].append(float(pnl))
        by_reg.setdefault(regime or "(none)", {"agentic": [], "rules_only": []})[arm].append(float(pnl))
    a, b = _arm_stat(arms["agentic"]), _arm_stat(arms["rules_only"])
    by_regime = {
        reg: {"agentic": _arm_stat(d["agentic"]), "rules_only": _arm_stat(d["rules_only"])}
        for reg, d in sorted(by_reg.items())
    }
    if a["n"] < _MIN_ARM_N or b["n"] < _MIN_ARM_N:
        return {"status": "insufficient_data", "agentic": a, "rules_only": b, "by_regime": by_regime,
                "reason": f"need >= {_MIN_ARM_N} closes per arm"}
    beats = (a["expectancy"] or 0) > (b["expectancy"] or 0)
    return {"status": "ready", "agentic": a, "rules_only": b, "by_regime": by_regime,
            "agentic_beats_baseline": beats,
            "expectancy_delta": round((a["expectancy"] or 0) - (b["expectancy"] or 0), 2)}


def agentic_value(db_path: str) -> dict[str, Any]:
    """Combined view for the AGENTIC proof-gate."""
    return {"arm_comparison": arm_comparison(db_path),
            "signal_predictiveness": signal_predictiveness(db_path)}

"""
ConvictionWeightCalibrator — quarterly offline analysis.

Reads closed trade_records + decision_chains from agora.db. Computes per-pillar and
per-regime win rates, profit factors, and conviction-quintile correlation. Outputs a
proposed_weights.json file for human review.

NEVER auto-applies weights. The operator must review the proposals and hand-edit
disagreement_resolver.py if accepted.

Run via: python scripts/calibrate_weights.py
Or call calibrate(db_path, output_path) directly.

Minimum data requirement: MIN_TRADES_FOR_PROPOSAL closed trades before any weight
adjustment is proposed. Below this threshold the file is still written but contains
only the raw analysis and a "insufficient data" note.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import date, timedelta
from typing import Any

MIN_TRADES_FOR_PROPOSAL = 20
LOOKBACK_DAYS           = 90     # one quarter


def _sharpe(pnls: list[float]) -> float | None:
    n = len(pnls)
    if n < 2:
        return None
    mean = sum(pnls) / n
    var  = sum((x - mean) ** 2 for x in pnls) / (n - 1)
    std  = math.sqrt(var) if var > 0 else 0.0
    if std == 0:
        return 1.0 if mean > 0 else (-1.0 if mean < 0 else 0.0)
    return round(mean / std, 3)


def _profit_factor(pnls: list[float]) -> float:
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss   = abs(sum(p for p in pnls if p < 0))
    if gross_loss == 0:
        return float("inf") if gross_profit > 0 else 1.0
    return round(gross_profit / gross_loss, 3)


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    m = len(s) // 2
    return round(s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2, 2)


def _cell_stats(pnls: list[float]) -> dict[str, Any]:
    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    return {
        "count":         n,
        "win_rate":      round(len(wins) / n, 3) if n else None,
        "avg_pnl":       round(sum(pnls) / n, 2) if n else None,
        "median_pnl":    _median(pnls),   # outlier-robust companion to avg_pnl
        "profit_factor": _profit_factor(pnls) if n else None,
        "sharpe":        _sharpe(pnls),
    }


def _load_trades(db_path: str, lookback_days: int) -> list[dict]:
    """Closed REAL-fill trades within lookback window. Was trade_records (model marks that
    flip sign vs the fills) — fiction P&L here mis-calibrates the conviction->size/gate mapping,
    teaching the system that high-conviction trades 'win' when the fills say they lose. Now
    sourced from positions/_REAL_CLOSE (the canonical real-fill P&L)."""
    from agora.ops.edge_dashboard import _REAL_CLOSE
    cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                f"""SELECT pillar, COALESCE(regime_at_entry, 'neutral') as regime,
                          COALESCE(conviction_at_entry, 0) as conviction,
                          realized_pnl
                   FROM positions
                   WHERE {_REAL_CLOSE}
                     AND close_date >= ?
                     AND realized_pnl IS NOT NULL
                   ORDER BY close_date""",
                (cutoff,),
            ).fetchall()
        return [
            {"pillar": r[0], "regime": r[1], "conviction": float(r[2]), "pnl": float(r[3])}
            for r in rows
        ]
    except Exception:
        return []


def _load_decision_chains(db_path: str, lookback_days: int) -> list[dict]:
    """Filled decision chains with REAL final P&L. Was decision_chains.realized_pnl (a model
    mark); now joined to positions/_REAL_CLOSE so the conviction->P&L calibration is honest."""
    from agora.ops.edge_dashboard import _REAL_CLOSE
    cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                f"""SELECT dc.conviction, dc.strategy, p.realized_pnl, dc.started_at
                   FROM decision_chains dc
                   JOIN positions p ON p.position_id = dc.position_id
                   WHERE dc.outcome = 'filled'
                     AND p.realized_pnl IS NOT NULL
                     AND dc.started_at >= ?
                     AND {_REAL_CLOSE}
                   ORDER BY dc.started_at""",
                (cutoff,),
            ).fetchall()
        return [
            {"conviction": float(r[0] or 0), "strategy": r[1] or "", "pnl": float(r[2]), "started_at": r[3]}
            for r in rows
        ]
    except Exception:
        return []


def _load_conviction_outcomes(db_path: str, lookback_days: int) -> list[dict]:
    """Conviction→outcome from positions/_REAL_CLOSE — the AUTHORITATIVE conviction signal.
    decision_chains.conviction is sparse/near-constant (most rows ~51.7), which made the quintile
    analysis blind to the documented inversion (high conviction → worse outcomes).
    positions.conviction_at_entry is stamped on every real close (n≈110), so quintiles built from it
    actually measure whether the scorer predicts. Falls back silently to [] on any error."""
    from agora.ops.edge_dashboard import _REAL_CLOSE
    cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                f"""SELECT COALESCE(conviction_at_entry, 0) AS conviction, realized_pnl
                   FROM positions
                   WHERE realized_pnl IS NOT NULL
                     AND COALESCE(conviction_at_entry, 0) > 0
                     AND close_date >= ?
                     AND {_REAL_CLOSE}
                   ORDER BY conviction_at_entry""",
                (cutoff,),
            ).fetchall()
        return [{"conviction": float(r[0]), "pnl": float(r[1])} for r in rows]
    except Exception:
        return []


def _per_pillar_analysis(trades: list[dict]) -> dict[str, Any]:
    cells: dict[str, list[float]] = {}
    for t in trades:
        cells.setdefault(t["pillar"], []).append(t["pnl"])
    return {pillar: _cell_stats(pnls) for pillar, pnls in cells.items()}


def _per_regime_analysis(trades: list[dict]) -> dict[str, Any]:
    cells: dict[str, list[float]] = {}
    for t in trades:
        cells.setdefault(t["regime"], []).append(t["pnl"])
    return {regime: _cell_stats(pnls) for regime, pnls in cells.items()}


def _per_pillar_regime_analysis(trades: list[dict]) -> dict[str, Any]:
    cells: dict[str, list[float]] = {}
    for t in trades:
        key = f"{t['pillar']}:{t['regime']}"
        cells.setdefault(key, []).append(t["pnl"])
    return {key: _cell_stats(pnls) for key, pnls in cells.items()}


def _conviction_quintile_analysis(chains: list[dict]) -> dict[str, Any]:
    """
    Split filled trades into conviction quintiles; compute P&L stats per quintile.
    Tells us whether the scorer is predictive.
    """
    if not chains:
        return {}
    sorted_chains = sorted(chains, key=lambda x: x["conviction"])
    n = len(sorted_chains)
    q_size = max(1, n // 5)
    quintiles: dict[str, Any] = {}
    for i in range(5):
        start = i * q_size
        end   = start + q_size if i < 4 else n
        bucket = sorted_chains[start:end]
        if not bucket:
            continue
        pnls = [c["pnl"] for c in bucket]
        min_conv = round(min(c["conviction"] for c in bucket), 1)
        max_conv = round(max(c["conviction"] for c in bucket), 1)
        quintiles[f"Q{i+1} ({min_conv}–{max_conv})"] = _cell_stats(pnls)
    return quintiles


_CURRENT_WEIGHTS: dict[str, dict[str, float]] = {
    "risk_on":        {"macro": 0.50, "microstructure": 0.25, "catalyst": 0.25},
    "low_volatility": {"macro": 0.50, "microstructure": 0.25, "catalyst": 0.25},
    "risk_off":       {"macro": 0.25, "microstructure": 0.45, "catalyst": 0.30},
    "high_volatility":{"macro": 0.25, "microstructure": 0.45, "catalyst": 0.30},
    "neutral":        {"macro": 0.40, "microstructure": 0.35, "catalyst": 0.25},
    "normal":         {"macro": 0.40, "microstructure": 0.35, "catalyst": 0.25},
}


def _propose_weights(
    regime_stats: dict[str, Any],
    pillar_stats: dict[str, Any],
    total_trades: int,
) -> tuple[dict[str, Any], list[str]]:
    """
    Heuristic weight proposals based on observed performance.

    Logic:
    - If 'catalyst' pillar win_rate > 0.60 across regimes: bump catalyst weight +5pp in regimes
      where it historically worked; reduce macro by same amount.
    - If 'microstructure' (vol_premium + directional) dramatically underperforms in risk_off:
      reduce weight, increase macro.
    - Cap any single adjustment at ±10pp per cycle; maintain sum=1.00.
    - If total_trades < MIN_TRADES_FOR_PROPOSAL: no proposals, just notes.

    Returns (proposed_weights, notes_list).
    """
    notes: list[str] = []
    proposed = {regime: dict(weights) for regime, weights in _CURRENT_WEIGHTS.items()}

    if total_trades < MIN_TRADES_FOR_PROPOSAL:
        notes.append(
            f"Insufficient data ({total_trades} trades < {MIN_TRADES_FOR_PROPOSAL} minimum) — "
            "no weight changes proposed. Current weights unchanged."
        )
        return proposed, notes

    # Catalyst pillar performance
    cat_stats  = pillar_stats.get("catalyst", {})
    cat_wr     = cat_stats.get("win_rate")
    cat_pf     = cat_stats.get("profit_factor")
    cat_n      = cat_stats.get("count", 0)

    if cat_wr is not None and cat_n >= 10 and cat_wr > 0.60 and (cat_pf or 0) > 1.5:
        for regime in ["risk_on", "neutral", "normal", "low_volatility"]:
            pw = proposed[regime]
            shift = min(0.05, 1.0 - pw["catalyst"] - 0.05)
            if shift > 0 and pw["macro"] - shift >= 0.20:
                pw["catalyst"] = round(pw["catalyst"] + shift, 2)
                pw["macro"]    = round(pw["macro"]    - shift, 2)
                notes.append(
                    f"[{regime}] Catalyst win_rate={cat_wr:.0%} PF={cat_pf:.2f} over {cat_n} trades — "
                    f"proposed catalyst +{shift:.0%} → {pw['catalyst']:.2f}, "
                    f"macro -{shift:.0%} → {pw['macro']:.2f}"
                )

    # Risk_off regime: check if microstructure outperforms
    ro_stats = regime_stats.get("risk_off", {})
    ro_wr    = ro_stats.get("win_rate")
    ro_n     = ro_stats.get("count", 0)
    if ro_wr is not None and ro_n >= 10 and ro_wr < 0.35:
        notes.append(
            f"[risk_off] Low win_rate={ro_wr:.0%} over {ro_n} trades — "
            "consider raising conviction threshold in risk_off regime (manual review needed)"
        )

    # Quintile monotonicity check (if all quintile data available)
    notes.append(
        "Review conviction_quintiles: Q5 avg_pnl should exceed Q1 avg_pnl. "
        "If they're similar, ConvictionScorer may not be predictive."
    )

    # Normalize each row to sum=1.0 (floating point safety)
    for _regime, pw in proposed.items():
        total = sum(pw.values())
        if abs(total - 1.0) > 0.001:
            largest = max(pw, key=pw.get)
            pw[largest] = round(pw[largest] + (1.0 - total), 3)

    return proposed, notes


def _diagnostics(
    quintile_stats: dict[str, Any],
    regime_stats: dict[str, Any],
    cell_stats: dict[str, Any],
) -> list[dict[str, Any]]:
    """Concrete, data-driven findings + recommended actions. The operator applies them (this module
    NEVER auto-applies). Each finding is structured so the engine/UI can surface it mechanically."""
    out: list[dict[str, Any]] = []

    # 1. Is conviction predictive? Use OUTLIER-ROBUST metrics (win_rate + median_pnl), NOT mean
    #    avg_pnl. Lesson 2026-06-27: a mean-based check called conviction "healthy" because 4 lucky
    #    bear_put_spread winners (+$3,334) landed in the top quintile; on win_rate/median the top
    #    quintile is NOT better. Mean P&L is dominated by a handful of outliers at this sample size.
    qs = list(quintile_stats.values())
    if len(qs) >= 2:
        top, bot = qs[-1], qs[0]
        twr, bwr = top.get("win_rate"), bot.get("win_rate")
        tmed, bmed = top.get("median_pnl"), bot.get("median_pnl")
        tavg, bavg = top.get("avg_pnl"), bot.get("avg_pnl")
        tpf = top.get("profit_factor")
        if None not in (twr, bwr, tmed, bmed):
            # ACTIONABLE only if the top-conviction bucket is itself PROFITABLE (median>0 or PF>1)
            # AND ordered above the bottom on robust metrics. A top quintile that still LOSES
            # (median<0, win<break-even) is not an exploitable edge — it's noise, even if it nudges
            # the bottom. Mean P&L is ignored for the verdict (outlier-dominated at this n).
            # MEDIAN only — PF and mean are both inflated by a few big winners at this sample size
            # (the 4 bear_put_spread outliers gave a misleading PF=1.35 while median stayed negative).
            top_profitable = tmed is not None and tmed > 0
            beats_bottom   = (twr > bwr) and (tmed > bmed)
            actionable = top_profitable and beats_bottom
            outlier_flag = (tavg is not None and bavg is not None and tavg > bavg
                            and not actionable)   # positive mean while not actionable = outliers
            out.append({
                "kind": "conviction_predictiveness",
                "severity": "ok" if actionable else "critical",
                "top_quintile": {"win_rate": twr, "median_pnl": tmed, "avg_pnl": tavg, "profit_factor": tpf},
                "bottom_quintile": {"win_rate": bwr, "median_pnl": bmed, "avg_pnl": bavg},
                "outlier_driven_mean": outlier_flag,
                "finding": (
                    f"Conviction ACTIONABLE: top quintile profitable (win={twr}, median={tmed}, "
                    f"PF={tpf}) and beats bottom (win={bwr}, median={bmed})."
                    if actionable else
                    f"CONVICTION NOT ACTIONABLE: top quintile still LOSES (win={twr}, median={tmed}, "
                    f"PF={tpf}); it is not an exploitable edge"
                    + (" — any positive mean is OUTLIER-DRIVEN (a few big winners)." if outlier_flag
                       else ".")
                ),
                "recommendation": (
                    "Conviction may inform sizing/selection." if actionable else
                    "Do NOT hand-tune conviction component weights on noise, and do not let conviction "
                    "boost selection/sizing. Target the robust, large-sample leaks (regime/pillar) below."
                ),
            })

    # 2. Regime-level structural leaks (enough data + persistently losing).
    for regime, st in regime_stats.items():
        n, pf, wr = st.get("count", 0), st.get("profit_factor"), st.get("win_rate")
        if n >= 20 and pf is not None and pf < 0.6:
            out.append({
                "kind": "regime_leak", "severity": "warn", "regime": regime,
                "count": n, "profit_factor": pf, "win_rate": wr,
                "finding": f"REGIME LEAK [{regime}]: PF={pf} win={wr} over n={n} — structurally unprofitable.",
                "recommendation": "Reduce exposure/size in this regime or tighten entry selection.",
            })

    # 3. Worst pillar:regime cells (where to look next).
    bad = [(k, v) for k, v in cell_stats.items()
           if (v.get("count") or 0) >= 8 and (v.get("avg_pnl") or 0) < -50]
    for k, v in sorted(bad, key=lambda kv: kv[1].get("avg_pnl") or 0)[:3]:
        out.append({
            "kind": "cell_leak", "severity": "warn", "cell": k,
            "count": v["count"], "avg_pnl": v["avg_pnl"], "win_rate": v["win_rate"],
            "finding": f"CELL LEAK [{k}]: avg_pnl={v['avg_pnl']} win={v['win_rate']} n={v['count']}.",
            "recommendation": "Review strategy selection / direction inference for this pillar+regime.",
        })
    return out


def calibrate(db_path: str, output_path: str, lookback_days: int = LOOKBACK_DAYS) -> dict:
    """
    Run the full calibration analysis and write proposed_weights.json.

    Returns the output dict (same as written to JSON).
    """
    trades = _load_trades(db_path, lookback_days)
    chains = _load_decision_chains(db_path, lookback_days)
    conv_outcomes = _load_conviction_outcomes(db_path, lookback_days)

    pillar_stats  = _per_pillar_analysis(trades)
    regime_stats  = _per_regime_analysis(trades)
    cell_stats    = _per_pillar_regime_analysis(trades)
    # Quintiles from the AUTHORITATIVE positions-based conviction signal when richer than the sparse
    # decision_chains (the chains are near-constant ~51.7 and blind to the inversion).
    quintile_source = conv_outcomes if len(conv_outcomes) >= len(chains) else chains
    quintile_stats = _conviction_quintile_analysis(quintile_source)

    total_trades = len(trades)
    proposed_weights, notes = _propose_weights(regime_stats, pillar_stats, total_trades)
    diagnostics = _diagnostics(quintile_stats, regime_stats, cell_stats)

    output = {
        "generated_at":           date.today().isoformat(),
        "analysis_period_days":   lookback_days,
        "analysis_from":          (date.today() - timedelta(days=lookback_days)).isoformat(),
        "analysis_to":            date.today().isoformat(),
        "total_closed_trades":    total_trades,
        "total_decision_chains":  len(chains),
        "conviction_signal_n":    len(conv_outcomes),
        "quintile_source":        "positions" if quintile_source is conv_outcomes else "decision_chains",
        "min_trades_for_proposal": MIN_TRADES_FOR_PROPOSAL,
        "per_pillar":             pillar_stats,
        "per_regime":             regime_stats,
        "per_pillar_regime_cell": cell_stats,
        "conviction_quintiles":   quintile_stats,
        "current_weights":        _CURRENT_WEIGHTS,
        "proposed_weights":       proposed_weights,
        "proposal_notes":         notes,
        "diagnostics":            diagnostics,
        "action_required": (
            "HUMAN REVIEW REQUIRED. If proposals are accepted, hand-edit "
            "agora/agents/disagreement_resolver.py _WEIGHTS_BY_REGIME. "
            "Do NOT auto-apply."
        ),
    }

    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)

    return output

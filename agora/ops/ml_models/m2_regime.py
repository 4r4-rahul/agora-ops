"""
M2 · Regime model — classifies the current market/vol regime from the daily market snapshot (Phase 0d)
and emits a CREDIT-vs-DEBIT favorability score. This encodes the hard-won lesson as a number: in a
high-IV regime, SELL premium (credit) — do NOT BUY directional debits (IV crush).

Descriptive (reads the live market snapshot, no trade-label) → real scores as soon as a snapshot
exists. READ-ONLY on market_snapshots; writes only via the model-runner; never raises.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from agora.ops.model_runner import register_model

# vol_regime → credit-spread favorability (premium selling). High VIX = rich premium = favor credit.
_CREDIT_FAVOR = {"low": 0.2, "elevated": 0.5, "high": 0.8, "extreme": 0.7, "unknown": 0.5}


def regime_model(db_path: str) -> dict[str, Any]:
    try:
        conn = sqlite3.connect(db_path, timeout=8); conn.row_factory = sqlite3.Row
    except Exception as exc:
        return {"status": "error", "n_samples": 0, "readiness": "BOOTSTRAP", "metrics": {}, "summary": str(exc)}
    try:
        if not conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='market_snapshots'").fetchone()[0]:
            return {"status": "skipped", "n_samples": 0, "readiness": "BOOTSTRAP", "metrics": {},
                    "summary": "no market snapshot yet (Phase 0d capture pending)"}
        snap = conn.execute("SELECT * FROM market_snapshots ORDER BY snapshot_date DESC LIMIT 1").fetchone()
        n_days = conn.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0]
        if not snap:
            return {"status": "skipped", "n_samples": 0, "readiness": "BOOTSTRAP", "metrics": {},
                    "summary": "no market snapshot rows"}
        vix = snap["vix"]; vr = snap["vol_regime"] or "unknown"; ts = snap["term_state"] or "unknown"
        credit_favor = _CREDIT_FAVOR.get(vr, 0.5)
        # backwardation (stress) reduces favorability for ANY new premium structure (gap risk)
        if ts == "backwardation":
            credit_favor = round(credit_favor * 0.85, 3)
        debit_caution = round(credit_favor, 3)   # high when IV rich → caution on buying debits (crush)

        bias = ("SELL premium (credit) — avoid directional debits (IV crush)" if credit_favor >= 0.6
                else "BUY premium (debit) ok — vol is cheap" if credit_favor <= 0.35
                else "mixed — no strong vol edge either way")
        scores = [
            {"entity_type": "global", "entity_id": "credit_favorability", "score": credit_favor,
             "meta": {"vix": vix, "vol_regime": vr, "term_state": ts, "bias": bias}},
            {"entity_type": "global", "entity_id": "debit_iv_crush_caution", "score": debit_caution,
             "meta": {"vix": vix, "vol_regime": vr}},
        ]
        return {"status": "ok", "n_samples": n_days, "readiness": "TRAINABLE" if n_days >= 1 else "BOOTSTRAP",
                "metrics": {"as_of": snap["snapshot_date"], "vix": vix, "vol_regime": vr,
                            "term_state": ts, "credit_favorability": credit_favor, "bias": bias},
                "summary": f"VIX {vix} · {vr} vol · {ts} → {bias}",
                "scores": scores}
    finally:
        conn.close()


register_model("regime_model", cadence_days=0.0, fn=regime_model)  # cheap, refresh every cycle

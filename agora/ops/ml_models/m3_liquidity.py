"""
M3 · Liquidity / tradability model — the per-TICKER fill dimension that complements M1's per-strategy.
From execution_quality, learns which underlyings actually fill (a liquidity proxy) and whether option
price affects fillability, so the engine/analyst can deprioritise names that never fill before spending
LLM/effort on them.

Descriptive (no trade-label) → real scores now on abundant data. Windows to RECENT attempts.
READ-ONLY on execution_quality; writes only via the model-runner. Never raises.
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Any

from agora.ops.model_runner import register_model

_WINDOW_DAYS = 14
_MIN_RECENT = 30
_MIN_PER_TICKER = 12


def liquidity_model(db_path: str) -> dict[str, Any]:
    """Pure read; never raises."""
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

        # per-ticker fill rate (the liquidity proxy)
        rows = conn.execute(
            f"""SELECT ticker, COUNT(*) n, SUM(outcome IN ('fill','fill_closed')) fills,
                       AVG(CASE WHEN outcome IN ('fill','fill_closed') THEN slippage_ticks END) slip
                FROM execution_quality WHERE {where} GROUP BY ticker""", args).fetchall()
        total_n = sum(r["n"] for r in rows)
        per_ticker = {}
        scores = []
        for r in rows:
            if (r["n"] or 0) < _MIN_PER_TICKER:
                continue
            fr = round((r["fills"] or 0) / r["n"], 3)
            meta = {"n": r["n"], "fill_rate": fr,
                    "avg_slippage_ticks": round(r["slip"], 2) if r["slip"] is not None else None,
                    "window": window}
            per_ticker[r["ticker"]] = meta
            scores.append({"entity_type": "ticker", "entity_id": r["ticker"], "score": fr, "meta": meta})

        # does option price affect fill? tertile buckets on mid_price
        price_buckets: dict[str, float] = {}
        try:
            prices = [p[0] for p in conn.execute(
                f"SELECT mid_price FROM execution_quality WHERE {where} AND mid_price IS NOT NULL", args).fetchall()]
            if len(prices) >= 30:
                prices.sort()
                lo, hi = prices[len(prices) // 3], prices[2 * len(prices) // 3]
                for label, cond, ar in (("cheap", "mid_price <= ?", (*args, lo)),
                                        ("mid", "mid_price > ? AND mid_price <= ?", (*args, lo, hi)),
                                        ("expensive", "mid_price > ?", (*args, hi))):
                    row = conn.execute(
                        f"SELECT COUNT(*) n, SUM(outcome IN ('fill','fill_closed')) f "
                        f"FROM execution_quality WHERE {where} AND {cond}", ar).fetchone()
                    if row and row["n"]:
                        price_buckets[label] = round((row["f"] or 0) / row["n"], 3)
        except Exception:
            pass

        ranked = sorted(per_ticker.items(), key=lambda kv: kv[1]["fill_rate"], reverse=True)
        most = ranked[0][0] if ranked else "—"
        least = ranked[-1][0] if ranked else "—"
        rd = "TRAINABLE" if total_n >= 200 else ("EMERGING" if total_n >= 50 else "BOOTSTRAP")
        return {
            "status": "ok", "n_samples": total_n, "readiness": rd,
            "metrics": {"window": window, "n_tickers_scored": len(per_ticker),
                        "by_ticker": per_ticker, "fill_by_price_bucket": price_buckets},
            "summary": (f"{len(per_ticker)} tickers scored ({window}) · most-liquid {most} "
                        f"{per_ticker.get(most,{}).get('fill_rate',0)*100:.0f}% · least {least} "
                        f"{per_ticker.get(least,{}).get('fill_rate',0)*100:.0f}%") if per_ticker
                       else "too few per-ticker attempts to score liquidity yet",
            "scores": scores,
        }
    finally:
        conn.close()


register_model("liquidity_model", cadence_days=0.5, fn=liquidity_model)

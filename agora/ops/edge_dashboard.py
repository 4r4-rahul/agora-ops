"""
agora/ops/edge_dashboard.py — "Where is the edge?" — read-only observability over REAL fills.

The honest-edge surface for the profitability campaign. Answers, with statistical rigor and
min-sample gates, which pillar / strategy / regime / direction actually has positive realized
expectancy — so capital can later be concentrated on proven edge and starved from the rest.

Hard rules (learned the hard way):
  • Reads ONLY the positions table real fills (_REAL_CLOSE). trade_records carries model marks
    that can disagree in SIGN with the fill (+$1,235 model vs −$235 real) — never read it here.
  • Excludes fabricated provenance (fabricated_unfilled / tws_startup_sync / reset / duplicate_void).
  • Every cut carries n + a Wilson 95% lower bound on win-rate; cuts below MIN_SAMPLE are reported
    as UNVALIDATED, never as edge. No sizing/trading decision is made here — observability only.
  • Surfaces the day-1-churn histogram (the exit pathology) and the largest negative buckets so
    nothing hides.
"""
from __future__ import annotations

import math
import sqlite3
from typing import Any

MIN_SAMPLE = 20          # below this a cut is UNVALIDATED, never treated as edge

# Provenance we trust as a real, agent-driven fill. Mirrors outcome_attributor._REAL_CLOSE_SOURCES
# plus the session: planned-close family; explicitly EXCLUDES fabricated/sync/reset artifacts AND
# ADOPTED positions. Adopted positions (position_id 'adopt-%' / regime_at_entry='adopted') are legacy
# broker positions the reconciler ingested with a RECONSTRUCTED cost basis the engine never priced — so
# their realized P&L is unreliable (the 2026-06-25 corruption: −$808k booked on a $16k-max-loss DIA
# spread). They are NOT engine decisions and MUST NOT contaminate P&L books, ML training, or attribution.
_REAL_CLOSE = (
    "status='closed' AND close_date IS NOT NULL AND close_date<>'' "
    "AND (close_source IN ('lifecycle','thesis_exit','trailing_stop','stop_loss','pre_earnings') "
    "     OR close_source LIKE 'session:%') "
    "AND close_source NOT LIKE '%fabricated%' "
    "AND close_source NOT LIKE '%tws_startup_sync%' "
    "AND close_source NOT LIKE '%reconcile%' "
    "AND close_source NOT LIKE '%duplicate%' "
    "AND COALESCE(regime_at_entry,'') <> 'adopted' "   # JOIN-safe adopted marker (positions-only col)
    "AND status<>'reset'"
)


def _wilson_lower(wins: int, n: int, z: float = 1.96) -> float | None:
    """Wilson score 95% lower bound on the win-rate — an honest floor that down-weights small n."""
    if n <= 0:
        return None
    p = wins / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return round(max(0.0, (centre - margin) / denom), 3)


def _cut(conn: sqlite3.Connection, dim_sql: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""SELECT {dim_sql} AS bucket,
                   COUNT(*) AS n,
                   SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) AS wins,
                   ROUND(SUM(realized_pnl), 2) AS sum_pnl,
                   ROUND(AVG(realized_pnl), 2) AS avg_pnl,
                   ROUND(AVG(julianday(close_date) - julianday(entry_date)), 2) AS avg_hold_days
            FROM positions
            WHERE {_REAL_CLOSE}
            GROUP BY bucket
            ORDER BY sum_pnl ASC""",   # worst (most negative) buckets first — nothing hides
    ).fetchall()
    out = []
    for bucket, n, wins, sum_pnl, avg_pnl, avg_hold in rows:
        n = int(n or 0); wins = int(wins or 0)
        out.append({
            "bucket": bucket or "(none)",
            "n": n, "wins": wins,
            "win_rate": round(wins / n, 3) if n else None,
            "win_rate_wilson_lb": _wilson_lower(wins, n),
            "sum_pnl": sum_pnl or 0.0,
            "avg_pnl": avg_pnl or 0.0,
            "avg_hold_days": avg_hold,
            "status": ("EDGE" if n >= MIN_SAMPLE and _wilson_lower(wins, n) and _wilson_lower(wins, n) >= 0.55
                       else "NEGATIVE" if n >= MIN_SAMPLE and (avg_pnl or 0) < 0
                       else "UNVALIDATED"),
        })
    return out


def compute_edge(db_path: str) -> dict[str, Any]:
    """Full read-only edge report across pillar / strategy / regime / direction / close_source,
    plus the day-1-churn histogram. Never raises — returns {'error':...} on failure."""
    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            total = conn.execute(
                f"""SELECT COUNT(*), SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END),
                           ROUND(SUM(realized_pnl),2), ROUND(AVG(realized_pnl),2)
                    FROM positions WHERE {_REAL_CLOSE}""",
            ).fetchone()
            n = int(total[0] or 0); wins = int(total[1] or 0)

            # Day-1 churn histogram (the exit pathology) — how many real closes happen at each hold-day.
            churn = conn.execute(
                f"""SELECT CAST(julianday(close_date)-julianday(entry_date) AS INT) AS hold_d,
                           COUNT(*) n, ROUND(SUM(realized_pnl),2) pnl
                    FROM positions WHERE {_REAL_CLOSE}
                    GROUP BY hold_d ORDER BY hold_d""",
            ).fetchall()

            report = {
                "min_sample": MIN_SAMPLE,
                "overall": {
                    "n": n, "wins": wins,
                    "win_rate": round(wins / n, 3) if n else None,
                    "win_rate_wilson_lb": _wilson_lower(wins, n),
                    "sum_pnl": total[2] or 0.0, "avg_pnl": total[3] or 0.0,
                    "status": "UNVALIDATED" if n < MIN_SAMPLE else (
                        "PROFITABLE" if (total[2] or 0) > 0 else "UNPROFITABLE"),
                },
                "by_pillar":      _cut(conn, "pillar"),
                "by_strategy":    _cut(conn, "strategy"),
                "by_regime":      _cut(conn, "regime_at_entry"),
                "by_direction":   _cut(conn, "direction"),
                "by_close_source": _cut(conn, "close_source"),
                "hold_day_histogram": [
                    {"hold_days": int(h) if h is not None else None, "n": int(c), "pnl": p}
                    for (h, c, p) in churn
                ],
                "day1_churn_pct": (
                    round(100.0 * sum(int(c) for (h, c, _) in churn if h is not None and h <= 1) / n, 1)
                    if n else None
                ),
            }
            return report
    except Exception as exc:
        return {"error": str(exc)}

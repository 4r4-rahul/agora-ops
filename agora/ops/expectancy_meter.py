"""
agora/ops/expectancy_meter.py — the system's North-Star gauge (S0.4 + meter).

Every agent, the C-suite, and the operator thrive toward one number: **expectancy ($/trade)**.
This module turns the real fill-sourced ledger into a dashboard meter:

  • current   — post-fix expectancy (legacy churn-era trades excluded), with win-rate/payoff/n
  • target    — $/trade goal "locked" for a date, with the days-remaining countdown
  • progress  — honest 0–100% from the all-time baseline (−$80) toward the goal
  • periods   — realized expectancy bucketed today / this-week / this-month / all-post-fix
  • projection— current vs target $ per day / week / month at the live trade cadence

Pure + read-only (reuses performance_metrics over _REAL_CLOSE). Never raises — returns a safe
shell on any error so the dashboard always renders.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from typing import Any

from agora.ops.edge_dashboard import _REAL_CLOSE
from agora.ops.performance_metrics import MIN_SAMPLE, _metrics_from, _rows, compute_metrics


def _window_metrics(conn: sqlite3.Connection, since: str) -> dict[str, Any]:
    m = _metrics_from(_rows(conn, f"AND close_date >= '{since}'"))
    return {"expectancy": m["expectancy"], "n": m["n"], "net": m["net_realized"],
            "win_rate": m["win_rate"]}


def _trades_per_day(conn: sqlite3.Connection, cutoff: str) -> float:
    """Realized close cadence over the post-fix window (for the per-day/week/month projection)."""
    rows = conn.execute(
        f"SELECT MIN(close_date), MAX(close_date), COUNT(*) FROM positions "
        f"WHERE {_REAL_CLOSE} AND close_date >= '{cutoff}'"
    ).fetchone()
    lo, hi, n = rows
    if not n or not lo or not hi:
        return 0.0
    try:
        span = max(1, (date.fromisoformat(hi[:10]) - date.fromisoformat(lo[:10])).days + 1)
    except ValueError:
        return 0.0
    return round(n / span, 3)


def credit_spread_stats(db_path: str, since: str = "2026-06-18") -> dict[str, Any]:
    """W1/W1b/W1c follow-through — did the credit-spread changes get them trading, and winning?
    Counts bull_put/bear_call entered since the W1 change (default 2026-06-18). Read-only; never
    raises. Mirrored by scripts/expectancy_checkin.py for the daily Discord push."""
    creds = ("bull_put_spread", "bear_call_spread")
    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            ph = ",".join("?" * len(creds))
            rows = conn.execute(
                f"SELECT realized_pnl, close_date FROM positions "
                f"WHERE strategy IN ({ph}) AND entry_date >= ?", (*creds, since)).fetchall()
        closed = [r for r in rows if (r[1] or "") != ""]
        wins = sum(1 for r in closed if (r[0] or 0) > 0)
        return {"since": since, "entered": len(rows), "open": len(rows) - len(closed),
                "closed": len(closed),
                "win_rate": round(wins / len(closed), 3) if closed else None}
    except Exception:
        return {"since": since, "entered": 0, "open": 0, "closed": 0, "win_rate": None}


def build_meter(db_path: str, *, target_per_trade: float = 25.0,
                target_date: str = "2026-09-30", legacy_cutoff: str = "2026-06-12") -> dict[str, Any]:
    """Compute the full expectancy meter payload. Read-only; never raises."""
    today = date.today()
    cut = str(legacy_cutoff)[:10]
    try:
        metrics = compute_metrics(db_path, legacy_cutoff=cut)
        baseline = (metrics.get("overall") or {}).get("expectancy")      # all-time = the start line
        post = metrics.get("post_fix") or {}
        current = post.get("expectancy")
        n_post = post.get("n", 0)

        week_start = (today - timedelta(days=today.weekday())).isoformat()
        month_start = today.replace(day=1).isoformat()
        with sqlite3.connect(db_path, timeout=10) as conn:
            periods = {
                "today":        _window_metrics(conn, today.isoformat()),
                "this_week":    _window_metrics(conn, week_start),
                "this_month":   _window_metrics(conn, month_start),
                "all_post_fix": {"expectancy": current, "n": n_post, "net": post.get("net_realized"),
                                 "win_rate": post.get("win_rate")},
            }
            tpd = _trades_per_day(conn, cut)

        # progress: from baseline (where we started) toward target (the goal). 0% = no better than
        # the all-time average; 100% = at target. Can go negative if we regress below baseline.
        progress = None
        if current is not None and baseline is not None and target_per_trade != baseline:
            progress = round((current - baseline) / (target_per_trade - baseline) * 100, 1)

        try:
            days_remaining = (date.fromisoformat(str(target_date)[:10]) - today).days
        except ValueError:
            days_remaining = None

        gap = round(target_per_trade - current, 2) if current is not None else None
        at_target = current is not None and current >= target_per_trade
        validated = n_post >= MIN_SAMPLE
        status = ("UNVALIDATED" if not validated
                  else "AT_TARGET" if at_target
                  else "ON_TRACK" if (current is not None and progress is not None and progress >= 50)
                  else "BEHIND")

        def _proj(per_trade: float | None) -> dict[str, Any]:
            if per_trade is None:
                return {"per_day": None, "per_week": None, "per_month": None}
            return {"per_day": round(per_trade * tpd, 2),
                    "per_week": round(per_trade * tpd * 5, 2),     # 5 trading days
                    "per_month": round(per_trade * tpd * 21, 2)}   # ~21 trading days

        return {
            "target": {"per_trade": target_per_trade, "date": str(target_date)[:10],
                       "days_remaining": days_remaining, "locked": True},
            "current": {"expectancy": current, "win_rate": post.get("win_rate"),
                        "payoff_ratio": post.get("payoff_ratio"), "profit_factor": post.get("profit_factor"),
                        "n": n_post, "net": post.get("net_realized")},
            "baseline_expectancy": baseline,
            "gap_to_target": gap,
            "progress_pct": progress,
            "status": status,
            "credit_spreads": credit_spread_stats(db_path),
            "validated": validated,
            "periods": periods,
            "projection": {"trades_per_day": tpd,
                           "current": _proj(current), "target": _proj(target_per_trade)},
            "legacy_cutoff": cut,
            "computed_at_utc": datetime.now(tz=UTC).isoformat(),
        }
    except Exception as exc:
        return {"error": str(exc), "target": {"per_trade": target_per_trade, "date": str(target_date)[:10]},
                "current": {"expectancy": None, "n": 0}, "status": "ERROR"}

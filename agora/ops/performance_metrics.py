"""
agora/ops/performance_metrics.py — the heartbeat: precise edge metrics, reconciled book↔DB.

THE money-management surface. Computes — from REAL fills only — the six numbers that define
whether this system makes money: Avg Win, Avg Loss, Win/Loss rate, Expectancy, Profit factor,
Profitability. Proves the books equal the DB. Pushes a one-line performance context into every
agent's reasoning, snapshots daily for the learning loop, and feeds honest daily achievements.

Non-negotiables (learned the hard way — the books read +$12,740 while reality was -$3,088):
  • Money is computed ONLY over _REAL_CLOSE real fills (the canonical predicate from edge_dashboard).
    NEVER trade_records / model marks (they disagree in SIGN with the fill).
  • Honest when n is small or negative — Wilson-gated, never dresses up a tiny sample as edge,
    explicitly tells agents "do NOT size up while edge is unproven."
  • reconcile_books() proves daily_pnl == real fill P&L and ALARMS (never silently trusts) on drift.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

from agora.ops.edge_dashboard import _REAL_CLOSE, MIN_SAMPLE, _wilson_lower


def _rows(conn: sqlite3.Connection, window_sql: str = "") -> list[float]:
    return [r[0] for r in conn.execute(
        f"SELECT realized_pnl FROM positions WHERE {_REAL_CLOSE} {window_sql} ORDER BY close_date"
    ).fetchall()]


def _metrics_from(pnls: list[float]) -> dict[str, Any]:
    n = len(pnls)
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    scratch = [p for p in pnls if p == 0]
    nw, nl, ns = len(wins), len(losses), len(scratch)
    avg_win  = round(sum(wins) / nw, 2) if nw else 0.0
    avg_loss = round(abs(sum(losses) / nl), 2) if nl else 0.0          # positive magnitude
    win_rate  = round(nw / n, 4) if n else None
    loss_rate = round(nl / n, 4) if n else None
    gross_win  = round(sum(wins), 2)
    gross_loss = round(abs(sum(losses)), 2)
    expectancy = round((win_rate or 0) * avg_win - (loss_rate or 0) * avg_loss, 2) if n else None
    payoff = round(avg_win / avg_loss, 2) if avg_loss else None         # R-multiple
    profit_factor = round(gross_win / gross_loss, 2) if gross_loss else None
    net = round(sum(pnls), 2)
    return {
        "n": n, "wins": nw, "losses": nl, "scratch": ns,
        "win_rate": win_rate, "loss_rate": loss_rate,
        "scratch_rate": round(ns / n, 4) if n else None,
        "win_rate_wilson_lb": _wilson_lower(nw, n),
        "avg_win": avg_win, "avg_loss": avg_loss,
        "expectancy": expectancy,                 # $/trade
        "payoff_ratio": payoff, "profit_factor": profit_factor,
        "gross_win": gross_win, "gross_loss": gross_loss,
        "net_realized": net,
        "is_profitable": net > 0,
        "confidence": "UNVALIDATED" if n < MIN_SAMPLE else "OK",
        "status": ("UNVALIDATED" if n < MIN_SAMPLE
                   else "PROFITABLE" if net > 0 else "UNPROFITABLE"),
    }


def compute_metrics(db_path: str, legacy_cutoff: str | None = None) -> dict[str, Any]:
    """All-time + rolling (last-20, last-30d) headline metrics over REAL fills. Never raises.

    When ``legacy_cutoff`` (a 'YYYY-MM-DD' date) is given, a ``post_fix`` view is added that excludes
    trades closed before it — the honest expectancy of the *repaired* system, free of pre-churn-fix
    legacy that would otherwise condemn now-working cells (S0.4)."""
    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            allt = _metrics_from(_rows(conn))
            last20 = _metrics_from(_rows(conn)[-20:])
            d30 = _metrics_from(_rows(conn, "AND close_date >= date('now','-30 day')"))
            out = {"overall": allt, "last_20": last20, "last_30d": d30,
                   "min_sample": MIN_SAMPLE,
                   "computed_at_utc": datetime.now(tz=UTC).isoformat()}
            if legacy_cutoff:
                # parameterless interpolation is safe — cutoff is a config date, never user input,
                # but guard the shape anyway so a malformed value can't break the SQL.
                _cut = str(legacy_cutoff)[:10]
                out["post_fix"] = _metrics_from(_rows(conn, f"AND close_date >= '{_cut}'"))
                out["legacy_cutoff"] = _cut
            return out
    except Exception as exc:
        return {"error": str(exc)}


def reconcile_books(db_path: str) -> dict[str, Any]:
    """Prove the 'books' (daily_pnl ledger) equal the real fill-sourced P&L. ALARM on drift —
    never silently trust either side. A non-zero drift means a fictional-mark writer is feeding
    the ledger (historically: circuit_breaker._get_todays_realized_pnl summing trade_records)."""
    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            book = conn.execute("SELECT COALESCE(SUM(realized_pnl),0) FROM daily_pnl").fetchone()[0]
            db = conn.execute(
                f"SELECT COALESCE(SUM(realized_pnl),0) FROM positions WHERE {_REAL_CLOSE}"
            ).fetchone()[0]
        drift = round(float(book) - float(db), 2)
        return {"book_realized": round(float(book), 2), "db_real_realized": round(float(db), 2),
                "drift": drift, "reconciled": abs(drift) < 1.0,
                "status": "OK" if abs(drift) < 1.0 else "DIVERGENCE"}
    except Exception as exc:
        return {"error": str(exc)}


def performance_context_line(db_path: str) -> str:
    """The one-liner injected into EVERY agent's LLM payload — so the whole system reasons toward
    positive expectancy. Honest and sizing-safe when the edge is unproven or negative."""
    m = compute_metrics(db_path).get("overall", {})
    n = m.get("n", 0)
    if not n:
        return ("SYSTEM PERFORMANCE: no real closed trades yet. The agenda is to find "
                "positive-expectancy setups; do not chase volume.")
    net = m.get("net_realized", 0.0); wr = (m.get("win_rate") or 0) * 100
    if n < MIN_SAMPLE:
        return (f"SYSTEM PERFORMANCE (n={n}, UNVALIDATED): net ${net:.0f}, win-rate {wr:.0f}%. "
                f"Edge UNPROVEN — the agenda is to find positive-expectancy setups, NOT to size up.")
    exp = m.get("expectancy", 0.0); pf = m.get("profit_factor")
    sign = "POSITIVE" if (exp or 0) > 0 else "NEGATIVE"
    return (f"SYSTEM PERFORMANCE (n={n}): expectancy ${exp:.0f}/trade ({sign}), win-rate {wr:.0f}%, "
            f"profit-factor {pf if pf is not None else 'n/a'}, net ${net:.0f}. THE AGENDA IS "
            f"POSITIVE EXPECTANCY: only act on setups whose edge beats the current bar; do not "
            f"size up while expectancy is negative.")


def compute_achievements(db_path: str) -> list[dict[str, Any]]:
    """Honest daily achievements computed from REAL fills only — most are LOCKED today, by design.
    No vanity badges off fictional P&L."""
    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            pnls = _rows(conn)
            m = _metrics_from(pnls)
            # longest current green streak (trailing)
            streak = 0
            for p in reversed(pnls):
                if p > 0:
                    streak += 1
                else:
                    break
            # any profitable calendar day (real fills)
            prof_day = conn.execute(
                f"""SELECT 1 FROM (SELECT substr(close_date,1,10) d, SUM(realized_pnl) p
                    FROM positions WHERE {_REAL_CLOSE} GROUP BY d) WHERE p > 0 LIMIT 1"""
            ).fetchone() is not None
            recon = reconcile_books(db_path)
        def ach(key, earned, label):
            return {"key": key, "earned": bool(earned), "label": label}
        return [
            ach("books_reconciled", recon.get("reconciled"),
                "Books reconcile to real fills (no fictional P&L)"),
            ach("first_profitable_day", prof_day, "A profitable trading day (real fills)"),
            ach("expectancy_positive", (m.get("expectancy") or -1) > 0,
                "Expectancy turned positive"),
            ach("profit_factor_above_1", (m.get("profit_factor") or 0) > 1.0,
                "Profit factor above 1.0"),
            ach("green_streak_5", streak >= 5, "5 winning closes in a row"),
            ach("net_positive", m.get("is_profitable"), "Net realized P&L positive"),
        ]
    except Exception:
        return []


_SNAPSHOT_DDL = """
CREATE TABLE IF NOT EXISTS perf_snapshots (
    snapshot_date   TEXT PRIMARY KEY,
    n INTEGER, wins INTEGER, losses INTEGER, scratch INTEGER,
    win_rate REAL, win_rate_wilson_lb REAL,
    avg_win REAL, avg_loss REAL, expectancy REAL,
    profit_factor REAL, payoff_ratio REAL,
    net_realized REAL, is_profitable INTEGER,
    book_realized REAL, recon_drift REAL, recon_ok INTEGER,
    window_net_20 REAL, computed_at_utc TEXT
);
"""


def snapshot_daily(db_path: str) -> dict[str, Any]:
    """Persist today's metrics as first-class learning data (idempotent per day). Called EOD by
    the ScheduledAttributor so the learning loop can see the expectancy trend."""
    try:
        full = compute_metrics(db_path)
        o = full.get("overall", {})
        recon = reconcile_books(db_path)
        w20 = full.get("last_20", {}).get("net_realized")
        today = datetime.now(tz=UTC).date().isoformat()
        with sqlite3.connect(db_path, timeout=10) as conn:
            conn.execute(_SNAPSHOT_DDL)
            conn.execute(
                """INSERT INTO perf_snapshots
                   (snapshot_date, n, wins, losses, scratch, win_rate, win_rate_wilson_lb,
                    avg_win, avg_loss, expectancy, profit_factor, payoff_ratio, net_realized,
                    is_profitable, book_realized, recon_drift, recon_ok, window_net_20, computed_at_utc)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(snapshot_date) DO UPDATE SET
                     n=excluded.n, wins=excluded.wins, losses=excluded.losses, scratch=excluded.scratch,
                     win_rate=excluded.win_rate, win_rate_wilson_lb=excluded.win_rate_wilson_lb,
                     avg_win=excluded.avg_win, avg_loss=excluded.avg_loss, expectancy=excluded.expectancy,
                     profit_factor=excluded.profit_factor, payoff_ratio=excluded.payoff_ratio,
                     net_realized=excluded.net_realized, is_profitable=excluded.is_profitable,
                     book_realized=excluded.book_realized, recon_drift=excluded.recon_drift,
                     recon_ok=excluded.recon_ok, window_net_20=excluded.window_net_20,
                     computed_at_utc=excluded.computed_at_utc""",
                (today, o.get("n"), o.get("wins"), o.get("losses"), o.get("scratch"),
                 o.get("win_rate"), o.get("win_rate_wilson_lb"), o.get("avg_win"), o.get("avg_loss"),
                 o.get("expectancy"), o.get("profit_factor"), o.get("payoff_ratio"),
                 o.get("net_realized"), 1 if o.get("is_profitable") else 0,
                 recon.get("book_realized"), recon.get("drift"), 1 if recon.get("reconciled") else 0,
                 w20, datetime.now(tz=UTC).isoformat()),
            )
        return {"snapshot_date": today, "expectancy": o.get("expectancy"),
                "net": o.get("net_realized"), "recon": recon.get("status")}
    except Exception as exc:
        return {"error": str(exc)}

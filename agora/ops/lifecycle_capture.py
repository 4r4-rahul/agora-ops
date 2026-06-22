"""
agora/ops/lifecycle_capture.py — the daily "film" of every trade (M8 foundation, 2026-06-22).

Captures ONE row per open (and just-closed) position PER DAY into `lifecycle_snapshots`, building
the longitudinal panel that the lifecycle-attribution / win-prob / management models learn from.

Design guarantees:
  • PURELY ADDITIVE & READ-ONLY on the trading path — it only SELECTs `positions` and INSERTs into
    `lifecycle_snapshots`. It never touches _evaluate_ticker, gates, entry/exit, or any order. So it
    CANNOT affect execution.
  • IDEMPOTENT — UNIQUE(position_id, snapshot_date); a re-run on the same day is a no-op.
  • NEVER RAISES — a capture failure must never disturb the scheduler.
  • Captures the CLOSING frame too: a position closed today gets a final snapshot with realized P&L,
    so the film includes the last frame (entry → daily marks → close).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, date, datetime
from typing import Any

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS lifecycle_snapshots (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id         TEXT NOT NULL,
    ticker              TEXT,
    strategy            TEXT,
    pillar              TEXT,
    direction           TEXT,
    snapshot_date       TEXT NOT NULL,
    snapshot_ts_utc     TEXT,
    status              TEXT,              -- 'open' or 'closed' (the closing frame)
    days_held           INTEGER,
    dte_remaining       INTEGER,
    entry_price         REAL,
    current_price       REAL,
    unrealized_pnl      REAL,
    unrealized_pct_risk REAL,             -- unrealized / max_loss (how much of the risk is in the red/green)
    captured_pct_gain   REAL,             -- unrealized / max_gain (progress toward target)
    realized_pnl        REAL,             -- only on the closing frame
    net_delta           REAL,
    net_gamma           REAL,
    net_theta           REAL,
    net_vega            REAL,
    conviction_at_entry REAL,
    regime_at_entry     TEXT,
    is_adopted          INTEGER DEFAULT 0,
    UNIQUE(position_id, snapshot_date)
);
CREATE INDEX IF NOT EXISTS idx_lifecycle_pos  ON lifecycle_snapshots(position_id);
CREATE INDEX IF NOT EXISTS idx_lifecycle_date ON lifecycle_snapshots(snapshot_date);
"""


def _net_greeks(legs_json: str | None) -> dict[str, float | None]:
    """Net position greeks = Σ sign·contracts·leg_greek (sign +1 buy / −1 sell). None if unavailable."""
    out: dict[str, float | None] = {"net_delta": None, "net_gamma": None, "net_theta": None, "net_vega": None}
    try:
        legs = json.loads(legs_json) if legs_json else []
        if not legs:
            return out
        acc = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
        seen = {"delta": False, "gamma": False, "theta": False, "vega": False}
        for lg in legs:
            sign = 1.0 if str(lg.get("action", "")).lower() == "buy" else -1.0
            qty = float(lg.get("contracts", 1) or 1)
            for g in acc:
                v = lg.get(g)
                if v is not None:
                    acc[g] += sign * qty * float(v)
                    seen[g] = True
        for g in acc:
            out[f"net_{g}"] = round(acc[g], 4) if seen[g] else None
    except Exception:
        pass
    return out


def _days_between(d_from: str | None, d_to: date) -> int | None:
    try:
        return (d_to - date.fromisoformat(str(d_from)[:10])).days
    except Exception:
        return None


def capture_lifecycle_snapshots(db_path: str, as_of: str | None = None) -> dict[str, Any]:
    """Snapshot every open position (+ any closed TODAY) once for `as_of` (default today).
    Returns {captured, snapshot_date}. Never raises.

    NOTE: `today` uses LOCAL date.today() — the same clock the close paths use to write
    positions.close_date (position_manager). Using UTC here would miss the realized-P&L closing
    frame for positions closed near the UTC/local boundary (survivorship bias in the ML panel)."""
    today = as_of or date.today().isoformat()
    captured = 0
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_DDL)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT position_id, ticker, strategy, pillar, direction, status, legs_json,
                      entry_price, current_price, unrealized_pnl, realized_pnl,
                      max_loss_dollars, max_gain_dollars, entry_date, expiry_date,
                      conviction_at_entry, regime_at_entry, close_date
               FROM positions
               WHERE status='open' OR close_date = ?""",
            (today,),
        ).fetchall()
        d_today = date.fromisoformat(today)
        for p in rows:
            try:
                ml = abs(p["max_loss_dollars"] or 0.0)
                mg = abs(p["max_gain_dollars"] or 0.0)
                upnl = p["unrealized_pnl"]
                g = _net_greeks(p["legs_json"])
                conn.execute(
                    """INSERT OR IGNORE INTO lifecycle_snapshots (
                        position_id, ticker, strategy, pillar, direction, snapshot_date, snapshot_ts_utc,
                        status, days_held, dte_remaining, entry_price, current_price, unrealized_pnl,
                        unrealized_pct_risk, captured_pct_gain, realized_pnl,
                        net_delta, net_gamma, net_theta, net_vega,
                        conviction_at_entry, regime_at_entry, is_adopted
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        p["position_id"], p["ticker"], p["strategy"], p["pillar"], p["direction"],
                        today, datetime.now(UTC).isoformat(timespec="seconds"),
                        p["status"], _days_between(p["entry_date"], d_today),
                        _days_between(today, date.fromisoformat(str(p["expiry_date"])[:10]))
                        if p["expiry_date"] else None,
                        p["entry_price"], p["current_price"], upnl,
                        round(upnl / ml, 4) if (upnl is not None and ml > 0) else None,
                        round(upnl / mg, 4) if (upnl is not None and mg > 0) else None,
                        p["realized_pnl"] if p["status"] == "closed" else None,
                        g["net_delta"], g["net_gamma"], g["net_theta"], g["net_vega"],
                        p["conviction_at_entry"], p["regime_at_entry"],
                        1 if str(p["position_id"]).startswith("adopt-") else 0,
                    ),
                )
            except Exception as _exc:
                logger.debug("lifecycle snapshot row failed for %s: %s", p["position_id"], _exc)
        conn.commit()
        # accurate count for today
        captured = conn.execute(
            "SELECT COUNT(*) FROM lifecycle_snapshots WHERE snapshot_date=?", (today,)
        ).fetchone()[0]
        conn.close()
        return {"captured": captured, "snapshot_date": today, "open_and_closed_today": len(rows)}
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("capture_lifecycle_snapshots failed: %s", exc)
        return {"captured": captured, "error": str(exc)}

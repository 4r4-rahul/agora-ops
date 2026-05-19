"""
agora/ops/decision_chains.py — Decision chain lifecycle (Phase 2).

One chain per candidate evaluation. Lifecycle:
  start_chain()  — called when ticker passes conviction gate
  complete_chain() — called after IBKR outcome (filled/rejected/no_trade)
  link_position()  — called on confirmed fill to attach position_id
  close_chain()    — called when position closes with realized P&L

All writes are fire-and-forget (exceptions logged, never raised) so a
journaling failure never stops a trade.

Schema: decision_chains table — managed by migrations/2026_05_phase2_journals.sql
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
_lock = threading.Lock()


# ── Chain lifecycle ───────────────────────────────────────────────────────────

def start_chain(
    db_path: str,
    ticker: str,
    triggered_by: str,
    session_id: str = "",
    conviction: float = 0.0,
    metadata: dict | None = None,
) -> str:
    """
    Open a new decision chain when a ticker passes the conviction gate.
    Returns chain_id. Called at the START of intelligence evaluation,
    before StockAnalystAgent runs. outcome starts as 'evaluating'.
    """
    chain_id = str(uuid.uuid4())
    now = datetime.now(tz=ET).isoformat()
    with _lock:
        try:
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """INSERT INTO decision_chains
                       (chain_id, ticker, triggered_by, started_at, outcome,
                        session_id, conviction, strategy, gates_passed, metadata_json)
                       VALUES (?, ?, ?, ?, 'evaluating', ?, ?, '', '[]', ?)""",
                    (
                        chain_id, ticker, triggered_by, now,
                        session_id, round(conviction, 1),
                        json.dumps(metadata or {}),
                    ),
                )
        except Exception as exc:
            logger.debug("start_chain write error: %s", exc)
    return chain_id


def complete_chain(
    db_path: str,
    chain_id: str,
    outcome: str,
    strategy: str = "",
    gates_passed: list[str] | None = None,
    position_id: str | None = None,
) -> None:
    """
    Finalize chain outcome after IBKR submission resolves.
    outcome: 'filled' | 'rejected' | 'no_trade' | 'analyst_blocked' |
             'risk_blocked' | 'timeout'
    """
    now = datetime.now(tz=ET).isoformat()
    with _lock:
        try:
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """UPDATE decision_chains
                       SET outcome = ?,
                           strategy = COALESCE(NULLIF(?, ''), strategy),
                           gates_passed = ?,
                           position_id = COALESCE(?, position_id),
                           completed_at_utc = ?
                       WHERE chain_id = ?""",
                    (
                        outcome,
                        strategy,
                        json.dumps(gates_passed or []),
                        position_id,
                        now,
                        chain_id,
                    ),
                )
        except Exception as exc:
            logger.debug("complete_chain write error: %s", exc)


def link_position(db_path: str, chain_id: str, position_id: str) -> None:
    """Attach position_id to chain on confirmed IBKR fill."""
    with _lock:
        try:
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "UPDATE decision_chains SET position_id = ? WHERE chain_id = ?",
                    (position_id, chain_id),
                )
        except Exception as exc:
            logger.debug("link_position write error: %s", exc)


def close_chain(db_path: str, position_id: str, realized_pnl: float) -> None:
    """
    Called by PositionManager when a position closes.
    Links the realized P&L back to the decision that opened it.
    """
    with _lock:
        try:
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """UPDATE decision_chains
                       SET realized_pnl = ?,
                           outcome = CASE WHEN realized_pnl IS NULL
                                     THEN CASE WHEN ? >= 0 THEN 'win' ELSE 'loss' END
                                     ELSE outcome END
                       WHERE position_id = ?""",
                    (realized_pnl, realized_pnl, position_id),
                )
        except Exception as exc:
            logger.debug("close_chain write error: %s", exc)


# ── Legacy aliases (keep call sites that use old names working) ───────────────

def log_decision(
    db_path: str,
    ticker: str,
    triggered_by: str,
    outcome: str,
    session_id: str = "",
    conviction: float = 0.0,
    strategy: str = "",
    position_id: str | None = None,
    gates_passed: list[str] | None = None,
) -> str:
    """Legacy: start + complete in one call. Used by _submit_recommendation fallback."""
    chain_id = start_chain(db_path, ticker, triggered_by, session_id, conviction)
    complete_chain(db_path, chain_id, outcome, strategy, gates_passed, position_id)
    return chain_id


def update_outcome(db_path: str, chain_id: str, outcome: str, position_id: str | None = None) -> None:
    """Legacy alias for complete_chain."""
    complete_chain(db_path, chain_id, outcome, position_id=position_id)


def update_close(db_path: str, position_id: str, realized_pnl: float) -> None:
    """Legacy alias for close_chain."""
    close_chain(db_path, position_id, realized_pnl)


# ── Dashboard query ───────────────────────────────────────────────────────────

def recent_chains(db_path: str, limit: int = 50) -> list[dict]:
    """Return recent decision chains for dashboard /agora/decisions endpoint."""
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                """SELECT chain_id, ticker, triggered_by, started_at, completed_at_utc,
                          outcome, position_id, realized_pnl, session_id, conviction,
                          strategy, gates_passed
                   FROM decision_chains
                   ORDER BY started_at DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
        return [
            {
                "chain_id":       r[0],
                "ticker":         r[1],
                "triggered_by":   r[2],
                "started_at":     r[3],
                "completed_at":   r[4],
                "outcome":        r[5],
                "position_id":    r[6],
                "realized_pnl":   r[7],
                "session_id":     r[8],
                "conviction":     r[9],
                "strategy":       r[10],
                "gates_passed":   json.loads(r[11] or "[]"),
            }
            for r in rows
        ]
    except Exception:
        return []

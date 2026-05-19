"""
LLM cost logger — writes per-call token usage and estimated cost to llm_cost_log.

Pricing (USD per 1M tokens, 2026-05):
  Opus 4.7:   $5.00 input / $25.00 output
  Sonnet 4.6: $3.00 input / $15.00 output
  Haiku 4.5:  $1.00 input / $5.00 output

One API call = one row. Aggregates via daily_cost_summary().
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import date, datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

_PRICE_PER_M: dict[str, tuple[float, float]] = {
    "claude-opus-4-7":   (5.00, 25.00),
    "claude-opus-4-6":   (5.00, 25.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5":  (1.00,  5.00),
}

DAILY_CAP_USD = 15.00  # board decision: $10-15/day hard cap

_lock = threading.Lock()


def _price_for(model: str) -> tuple[float, float]:
    for prefix, prices in _PRICE_PER_M.items():
        if model.startswith(prefix):
            return prices
    return (5.00, 25.00)  # unknown model → conservative Opus pricing


def ensure_table(db_path: str) -> None:
    """Create llm_cost_log table if it doesn't exist. Safe to call on every startup."""
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS llm_cost_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts            TEXT NOT NULL,
                date          TEXT NOT NULL,
                agent         TEXT NOT NULL,
                purpose       TEXT NOT NULL DEFAULT '',
                model         TEXT NOT NULL,
                input_tokens  INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cost_usd      REAL NOT NULL DEFAULT 0,
                session_id    TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_llm_cost_date ON llm_cost_log(date)"
        )
        conn.commit()


def log_call(
    db_path: str,
    agent: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    purpose: str = "",
    session_id: str = "",
) -> float:
    """
    Record one Claude API call. Returns estimated cost in USD.
    Thread-safe — called from async contexts via run_in_executor or directly.
    """
    in_price, out_price = _price_for(model)
    cost = (input_tokens * in_price + output_tokens * out_price) / 1_000_000
    now = datetime.now(tz=ET)
    with _lock:
        try:
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """INSERT INTO llm_cost_log
                       (ts, date, agent, purpose, model,
                        input_tokens, output_tokens, cost_usd, session_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        now.isoformat(),
                        now.date().isoformat(),
                        agent,
                        purpose,
                        model,
                        input_tokens,
                        output_tokens,
                        round(cost, 6),
                        session_id,
                    ),
                )
        except Exception:
            pass  # never let cost logging break a trade path
    return cost


def daily_cost_summary(db_path: str, for_date: str | None = None) -> dict:
    """
    Return today's cost breakdown by agent and model.
    Used by CFO patrol, dashboard /agora/costs endpoint.
    """
    target = for_date or date.today().isoformat()
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                """SELECT agent, model, COUNT(*) AS calls,
                          SUM(input_tokens) AS in_tok,
                          SUM(output_tokens) AS out_tok,
                          SUM(cost_usd) AS cost
                   FROM llm_cost_log
                   WHERE date = ?
                   GROUP BY agent, model
                   ORDER BY cost DESC""",
                (target,),
            ).fetchall()
    except Exception:
        rows = []

    total = sum(r[5] for r in rows)
    return {
        "date":      target,
        "total_usd": round(total, 4),
        "cap_usd":   DAILY_CAP_USD,
        "pct_cap":   round(total / DAILY_CAP_USD * 100, 1) if total else 0.0,
        "over_cap":  total > DAILY_CAP_USD,
        "by_agent":  [
            {
                "agent":      r[0],
                "model":      r[1],
                "calls":      r[2],
                "in_tokens":  r[3],
                "out_tokens": r[4],
                "cost_usd":   round(r[5], 4),
            }
            for r in rows
        ],
    }

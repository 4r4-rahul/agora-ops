"""
agora/mcp/sqlite_tools.py — SQLite journal query tools for AGORA agents.

Gives agents read-only access to their own decision history so they can:
  • Learn from past mistakes on the same ticker
  • See how a strategy has been performing recently
  • Pull approved lessons without needing session.py to pre-stage them
  • Query outcome attribution to understand which signals are working

All queries are read-only (SELECT only). No writes from tool calls.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any


# ── Tool definitions (Anthropic tool_use schema) ──────────────────────────────

SQLITE_TOOLS: list[dict] = [
    {
        "name": "query_ticker_history",
        "description": (
            "Query the analyst journal for a ticker's past decisions. "
            "Returns the N most recent thesis decisions with direction, confidence, "
            "strategy family, and whether the trade was blocked or passed. "
            "Use this to understand how this ticker has behaved in past evaluations."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker symbol (e.g. AAPL)"},
                "limit": {"type": "integer", "description": "Number of recent decisions to return (default 10)", "default": 10},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "query_strategy_performance",
        "description": (
            "Query closed-trade outcomes for a specific strategy type over recent days. "
            "Returns win rate, average P&L pct, and trade count. "
            "Use this to check if a strategy has been performing well before endorsing it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "strategy_type": {"type": "string", "description": "e.g. bull_put_spread, iron_condor"},
                "days": {"type": "integer", "description": "Look-back window in days (default 30)", "default": 30},
            },
            "required": ["strategy_type"],
        },
    },
    {
        "name": "query_advocate_history",
        "description": (
            "Query what the AdvocateAgent flagged for a ticker or strategy in past reviews. "
            "Returns the most frequent failure modes and block rate. "
            "Use this to understand systemic risks that have been flagged before."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Ticker to filter by (or empty for all)"},
                "days": {"type": "integer", "description": "Look-back window in days (default 30)", "default": 30},
            },
            "required": [],
        },
    },
    {
        "name": "query_approved_lessons",
        "description": (
            "Retrieve all approved lessons for a given agent type. "
            "Lessons are manually or automatically approved insights from past trade outcomes. "
            "Use this to surface pattern knowledge not in the system prompt."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_type": {
                    "type": "string",
                    "description": "Agent category: analyst | advocate | strategy | long_options | exit",
                },
                "limit": {"type": "integer", "description": "Max lessons to return (default 20)", "default": 20},
            },
            "required": ["agent_type"],
        },
    },
    {
        "name": "query_pillar_performance",
        "description": (
            "Query rolling performance (win rate, Sharpe, avg hold days) per trading pillar. "
            "Returns which pillars are profitable and which are paused. "
            "Use this to see if the system should be favoring or avoiding a particular pillar."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Look-back window (default 60)", "default": 60},
            },
            "required": [],
        },
    },
    {
        "name": "query_recent_outcomes",
        "description": (
            "Query the most recent closed position outcomes: ticker, strategy, P&L pct, "
            "hold days, and exit reason. Use this to understand recent trade performance "
            "and whether the system has been hitting or missing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Number of closed trades (default 20)", "default": 20},
            },
            "required": [],
        },
    },
]


# ── Handler factory ───────────────────────────────────────────────────────────

def sqlite_tool_handlers(db_path: str) -> dict[str, Any]:
    """Return a dict of {tool_name: callable} bound to the given db_path."""

    def _connect() -> sqlite3.Connection:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def query_ticker_history(ticker: str, limit: int = 10) -> list[dict]:
        with _connect() as conn:
            rows = conn.execute(
                """
                SELECT decided_at_utc, decision, direction, confidence_pct,
                       strategy_family, reasoning_trace, shadow_mode
                FROM analyst_journal
                WHERE ticker = ?
                ORDER BY decided_at_utc DESC
                LIMIT ?
                """,
                (ticker.upper(), min(limit, 50)),
            ).fetchall()
        return [dict(r) for r in rows]

    def query_strategy_performance(strategy_type: str, days: int = 30) -> dict:
        with _connect() as conn:
            # Check if strategy_journal has outcome data
            rows = conn.execute(
                """
                SELECT COUNT(*) as total,
                       SUM(CASE WHEN output_full_json LIKE '%"decision": "endorse"%' THEN 1 ELSE 0 END) as endorsed
                FROM strategy_journal
                WHERE strategy_type = ?
                  AND decided_at_utc >= datetime('now', ? || ' days')
                  AND shadow_mode = 0
                """,
                (strategy_type, -days),
            ).fetchone()
            strategy_stats = dict(rows) if rows else {}

            # Try decision_chains for outcome correlation
            chains = conn.execute(
                """
                SELECT outcome, COUNT(*) as cnt
                FROM decision_chains
                WHERE outcome IS NOT NULL
                  AND started_at >= datetime('now', ? || ' days')
                GROUP BY outcome
                """,
                (-days,),
            ).fetchall()
        return {
            "strategy_type": strategy_type,
            "look_back_days": days,
            "strategy_journal_stats": strategy_stats,
            "chain_outcomes": {r["outcome"]: r["cnt"] for r in chains},
        }

    def query_advocate_history(ticker: str = "", days: int = 30) -> dict:
        with _connect() as conn:
            base = "WHERE decided_at_utc >= datetime('now', ? || ' days')"
            params: list = [-days]
            if ticker:
                base += " AND ticker = ?"
                params.append(ticker.upper())

            rows = conn.execute(
                f"""
                SELECT verdict, COUNT(*) as cnt
                FROM advocate_journal
                {base}
                GROUP BY verdict
                """,
                params,
            ).fetchall()
            verdict_dist = {r["verdict"]: r["cnt"] for r in rows}

            # Pull top failure modes from JSON
            fm_rows = conn.execute(
                f"""
                SELECT failure_modes_json
                FROM advocate_journal
                {base}
                ORDER BY decided_at_utc DESC
                LIMIT 50
                """,
                params,
            ).fetchall()

        mode_counts: dict[str, int] = {}
        for row in fm_rows:
            try:
                modes = json.loads(row["failure_modes_json"] or "[]")
                for m in modes:
                    name = m.get("mode_name", "unknown")
                    mode_counts[name] = mode_counts.get(name, 0) + 1
            except Exception:
                pass

        top_modes = sorted(mode_counts.items(), key=lambda x: -x[1])[:5]
        return {
            "ticker": ticker or "all",
            "look_back_days": days,
            "verdict_distribution": verdict_dist,
            "top_failure_modes": [{"mode": k, "count": v} for k, v in top_modes],
        }

    def query_approved_lessons(agent_type: str, limit: int = 20) -> list[dict]:
        with _connect() as conn:
            try:
                rows = conn.execute(
                    """
                    SELECT lesson_text, approved_at, source_outcome_id
                    FROM lessons
                    WHERE agent_type = ? AND approved = 1
                    ORDER BY approved_at DESC
                    LIMIT ?
                    """,
                    (agent_type, min(limit, 50)),
                ).fetchall()
                return [dict(r) for r in rows]
            except sqlite3.OperationalError:
                return []

    def query_pillar_performance(days: int = 60) -> list[dict]:
        with _connect() as conn:
            try:
                rows = conn.execute(
                    """
                    SELECT pillar, regime, win_rate, sharpe, trade_count, paused
                    FROM strategy_health_cache
                    WHERE computed_at >= datetime('now', ? || ' days')
                    ORDER BY sharpe DESC
                    """,
                    (-days,),
                ).fetchall()
                return [dict(r) for r in rows]
            except sqlite3.OperationalError:
                # Fallback: check decision_chains
                rows = conn.execute(
                    """
                    SELECT outcome, COUNT(*) as cnt
                    FROM decision_chains
                    WHERE started_at >= datetime('now', ? || ' days')
                    GROUP BY outcome
                    """,
                    (-days,),
                ).fetchall()
                return [dict(r) for r in rows]

    def query_recent_outcomes(limit: int = 20) -> list[dict]:
        with _connect() as conn:
            try:
                rows = conn.execute(
                    """
                    SELECT ticker, strategy, pillar, pnl_pct, hold_days,
                           outcome, exit_reason, closed_at
                    FROM trade_outcomes
                    ORDER BY closed_at DESC
                    LIMIT ?
                    """,
                    (min(limit, 100),),
                ).fetchall()
                return [dict(r) for r in rows]
            except sqlite3.OperationalError:
                rows = conn.execute(
                    """
                    SELECT ticker, outcome, started_at, completed_at_utc AS completed_at
                    FROM decision_chains
                    WHERE outcome IS NOT NULL
                    ORDER BY completed_at_utc DESC
                    LIMIT ?
                    """,
                    (min(limit, 100),),
                ).fetchall()
                return [dict(r) for r in rows]

    return {
        "query_ticker_history":     query_ticker_history,
        "query_strategy_performance": query_strategy_performance,
        "query_advocate_history":   query_advocate_history,
        "query_approved_lessons":   query_approved_lessons,
        "query_pillar_performance": query_pillar_performance,
        "query_recent_outcomes":    query_recent_outcomes,
    }

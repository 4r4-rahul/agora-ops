"""
Dashboard API — aggregated stats, real-time WebSocket pipeline feed,
and agent health for the trading dashboard.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections import deque
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()

_DB = Path("./trade_journal.db")

# ── In-process event bus ──────────────────────────────────────────────────────
# Captures log records from all agents and broadcasts to WebSocket clients.

_event_buffer: deque[dict] = deque(maxlen=500)   # ring buffer — survives reconnects
_ws_clients: set[WebSocket] = set()
_api_call_counter: dict[str, int] = {"total": 0, "today": 0, "last_reset": date.today().isoformat()}


class _PipelineLogHandler(logging.Handler):
    """Captures log records and pushes them to the WebSocket broadcast queue."""

    AGENT_COLORS = {
        "premarket":         "#64B5F6",
        "market_data":       "#4FC3F7",
        "regime":            "#81C784",
        "technical":         "#FFD54F",
        "news":              "#FF8A65",
        "conviction":        "#CE93D8",
        "options_strategy":  "#F48FB1",
        "risk_manager":      "#EF9A9A",
        "reviewer":          "#80CBC4",
        "execution":         "#A5D6A7",
        "journal":           "#B0BEC5",
        "monitor":           "#FFCC80",
        "setup_watcher":     "#80DEEA",
        "orchestrator":      "#E6EE9C",
    }

    def emit(self, record: logging.LogRecord) -> None:
        # Track Anthropic API calls
        if "api.anthropic.com" in (record.getMessage()) and "200 OK" in record.getMessage():
            _api_call_counter["total"] += 1
            today = date.today().isoformat()
            if _api_call_counter["last_reset"] != today:
                _api_call_counter["today"] = 0
                _api_call_counter["last_reset"] = today
            _api_call_counter["today"] += 1

        # Extract agent name from logger name
        parts = record.name.split(".")
        agent = parts[-1] if parts else record.name

        level_colors = {
            "INFO":    "#81C784",
            "WARNING": "#FFD54F",
            "ERROR":   "#EF5350",
            "DEBUG":   "#78909C",
        }

        event = {
            "ts":      datetime.now(_ET).strftime("%H:%M:%S ET"),
            "level":   record.levelname,
            "agent":   agent,
            "msg":     record.getMessage()[:200],
            "color":   _PipelineLogHandler.AGENT_COLORS.get(agent, "#90A4AE"),
            "lcolor":  level_colors.get(record.levelname, "#90A4AE"),
        }
        _event_buffer.append(event)

        # Broadcast to all connected WebSocket clients (fire-and-forget)
        asyncio.get_event_loop().call_soon_threadsafe(
            lambda e=event: asyncio.ensure_future(_broadcast(e))
        )


async def _broadcast(event: dict) -> None:
    dead = set()
    for ws in _ws_clients:
        try:
            await ws.send_text(json.dumps(event))
        except Exception:
            dead.add(ws)
    _ws_clients.difference_update(dead)


# Install the handler once at import time
_handler = _PipelineLogHandler()
_handler.setLevel(logging.DEBUG)
logging.getLogger("platform").addHandler(_handler)
logging.getLogger("trading_platform").addHandler(_handler)
logging.getLogger("httpx").addHandler(_handler)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _db_query(sql: str, params: tuple = ()) -> list[dict]:
    if not _DB.exists():
        return []
    with sqlite3.connect(_DB) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _db_one(sql: str, params: tuple = ()) -> dict | None:
    rows = _db_query(sql, params)
    return rows[0] if rows else None


# ── REST endpoints ────────────────────────────────────────────────────────────

@router.get("/stats")
async def get_stats() -> dict[str, Any]:
    """All KPIs in one call — polled every 5s by the dashboard.
    Only counts trades that were actually submitted to IBKR (ibkr_order_id IS NOT NULL).
    """
    # Only real IBKR-submitted trades count toward stats
    _IBKR_FILTER = "ibkr_order_id IS NOT NULL"

    perf = _db_one(f"""
        SELECT
            COUNT(*)                                                      AS total_trades,
            SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END)           AS winners,
            SUM(CASE WHEN realized_pnl <= 0 AND status='closed' THEN 1 ELSE 0 END) AS losers,
            SUM(realized_pnl)                                             AS total_pnl,
            AVG(CASE WHEN realized_pnl > 0 THEN realized_pnl END)        AS avg_win,
            AVG(CASE WHEN realized_pnl <= 0 THEN realized_pnl END)       AS avg_loss,
            MAX(realized_pnl)                                             AS best_trade,
            MIN(realized_pnl)                                             AS worst_trade,
            SUM(CASE WHEN realized_pnl > 0 THEN realized_pnl ELSE 0 END) AS gross_win,
            SUM(CASE WHEN realized_pnl < 0 THEN ABS(realized_pnl) ELSE 0 END) AS gross_loss
        FROM trade_journal WHERE status = 'closed' AND {_IBKR_FILTER}
    """) or {}

    open_count = (_db_one(
        f"SELECT COUNT(*) AS c FROM trade_journal WHERE status='open' AND {_IBKR_FILTER}"
    ) or {}).get("c", 0)

    today_pnl = (_db_one(
        f"SELECT SUM(realized_pnl) AS p FROM trade_journal "
        f"WHERE status='closed' AND date(closed_at) = date('now') AND {_IBKR_FILTER}"
    ) or {}).get("p", 0) or 0

    total = perf.get("total_trades") or 0
    winners = perf.get("winners") or 0
    gross_win = perf.get("gross_win") or 0
    gross_loss = perf.get("gross_loss") or 1

    # Ticker breakdown — IBKR trades only
    by_ticker = _db_query(f"""
        SELECT ticker,
               COUNT(*) AS trades,
               SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) AS wins,
               SUM(realized_pnl) AS pnl
        FROM trade_journal WHERE status='closed' AND {_IBKR_FILTER}
        GROUP BY ticker ORDER BY pnl DESC
    """)

    # Strategy breakdown — IBKR trades only
    by_strategy = _db_query(f"""
        SELECT strategy,
               COUNT(*) AS trades,
               SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) AS wins,
               SUM(realized_pnl) AS pnl
        FROM trade_journal WHERE status='closed' AND {_IBKR_FILTER}
        GROUP BY strategy ORDER BY pnl DESC
    """)

    return {
        "total_trades":   total,
        "open_positions": open_count,
        "winners":        winners,
        "losers":         total - winners,
        "win_rate":       round(winners / total * 100, 1) if total else 0,
        "total_pnl":      round(perf.get("total_pnl") or 0, 2),
        "today_pnl":      round(today_pnl, 2),
        "avg_win":        round(perf.get("avg_win") or 0, 2),
        "avg_loss":       round(perf.get("avg_loss") or 0, 2),
        "best_trade":     round(perf.get("best_trade") or 0, 2),
        "worst_trade":    round(perf.get("worst_trade") or 0, 2),
        "profit_factor":  round(gross_win / gross_loss, 2) if gross_loss else 0,
        "api_calls_today": _api_call_counter["today"],
        "api_calls_total": _api_call_counter["total"],
        "by_ticker":      by_ticker,
        "by_strategy":    by_strategy,
    }


@router.get("/equity")
async def get_equity() -> dict[str, Any]:
    """Daily cumulative P&L — IBKR-submitted trades only."""
    rows = _db_query("""
        SELECT date(closed_at) AS day, SUM(realized_pnl) AS daily_pnl
        FROM trade_journal
        WHERE status='closed' AND closed_at IS NOT NULL AND ibkr_order_id IS NOT NULL
        GROUP BY day ORDER BY day
    """)
    cumulative = 0.0
    labels, values = [], []
    for r in rows:
        cumulative += r["daily_pnl"] or 0
        labels.append(r["day"])
        values.append(round(cumulative, 2))
    return {"labels": labels, "values": values}


@router.get("/positions")
async def get_positions() -> dict[str, Any]:
    """Open positions submitted to IBKR."""
    rows = _db_query(
        "SELECT * FROM trade_journal WHERE status='open' AND ibkr_order_id IS NOT NULL"
        " ORDER BY opened_at DESC"
    )
    return {"positions": rows, "count": len(rows)}


@router.get("/history")
async def get_history(limit: int = 100) -> dict[str, Any]:
    """Closed trades submitted to IBKR."""
    rows = _db_query(
        "SELECT * FROM trade_journal WHERE ibkr_order_id IS NOT NULL"
        " ORDER BY opened_at DESC LIMIT ?",
        (min(limit, 500),)
    )
    return {"trades": rows, "count": len(rows)}


@router.get("/agents")
async def get_agents() -> dict[str, Any]:
    """Agent registry from the platform."""
    try:
        from ..deps import get_all_agents
        agents = get_all_agents()
        agents_info = [
            {
                "name":   agent.name,
                "status": "running",
                "subs":   [t.value for t in getattr(agent, "subscriptions", [])],
            }
            for agent in agents
        ]
    except Exception:
        agents_info = []
    return {"agents": agents_info, "count": len(agents_info)}


@router.get("/market")
async def get_market() -> dict[str, Any]:
    """Quick market snapshot — SPY, QQQ, VIX."""
    import yfinance as yf
    out: dict[str, Any] = {}
    for sym in ["SPY", "QQQ", "^VIX"]:
        try:
            info = yf.Ticker(sym).info or {}
            price = info.get("regularMarketPrice") or info.get("currentPrice") or info.get("previousClose")
            prev  = info.get("previousClose") or price
            chg   = round((price - prev) / prev * 100, 2) if price and prev else 0
            out[sym.replace("^", "")] = {"price": price, "change_pct": chg}
        except Exception:
            out[sym.replace("^", "")] = {"price": None, "change_pct": 0}
    return out


# ── WebSocket ─────────────────────────────────────────────────────────────────

@router.websocket("/ws")
async def pipeline_ws(ws: WebSocket) -> None:
    """Stream real-time pipeline events to the dashboard."""
    await ws.accept()
    _ws_clients.add(ws)

    # Replay recent buffer so new connections see context immediately
    for event in list(_event_buffer):
        try:
            await ws.send_text(json.dumps(event))
        except Exception:
            break

    try:
        while True:
            await ws.receive_text()   # keep-alive ping from client
    except WebSocketDisconnect:
        _ws_clients.discard(ws)
    except Exception:
        _ws_clients.discard(ws)

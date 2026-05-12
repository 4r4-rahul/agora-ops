"""
AGORA dashboard routes.

GET  /agora/positions    — open positions with live unrealized P&L
GET  /agora/signals      — latest signal state per ticker
GET  /agora/attribution  — per-pillar P&L attribution (last 30 days default)
GET  /agora/health       — kill-switch state + position count + account info
POST /agora/kill         — trip kill switch (body: {"reason": "..."})
DELETE /agora/kill       — reset kill switch
WS   /agora/ws           — live event stream (JSON lines)
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .state import get_session

router = APIRouter(prefix="/agora", tags=["agora"])
logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")

# Ring-buffer for WebSocket broadcast (survives client reconnects)
_event_buffer: deque[dict] = deque(maxlen=500)
_ws_clients: set[WebSocket] = set()


# ── WebSocket broadcast helpers ────────────────────────────────────────────────

async def _broadcast(event: dict) -> None:
    _event_buffer.append(event)
    dead: set[WebSocket] = set()
    for ws in _ws_clients:
        try:
            await ws.send_json(event)
        except Exception:
            dead.add(ws)
    _ws_clients.difference_update(dead)


class _AgoraLogHandler(logging.Handler):
    """Captures AGORA log records and pushes them to the WebSocket buffer."""

    _LEVEL_COLORS = {
        "INFO":    "#81C784",
        "WARNING": "#FFD54F",
        "ERROR":   "#EF5350",
        "DEBUG":   "#78909C",
    }

    def emit(self, record: logging.LogRecord) -> None:
        event = {
            "ts":    datetime.now(_ET).strftime("%H:%M:%S ET"),
            "level": record.levelname,
            "agent": record.name.split(".")[-1],
            "msg":   record.getMessage()[:300],
            "color": self._LEVEL_COLORS.get(record.levelname, "#90A4AE"),
        }
        # Schedule broadcast without blocking the logging thread
        try:
            loop = asyncio.get_running_loop()
            loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(_broadcast(event))
            )
        except RuntimeError:
            _event_buffer.append(event)


def attach_log_handler() -> None:
    """Call once at startup to wire the AGORA logger into the WebSocket feed."""
    handler = _AgoraLogHandler()
    handler.setLevel(logging.INFO)
    logging.getLogger("agora").addHandler(handler)


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("/positions")
async def get_positions() -> JSONResponse:
    """
    Open positions with live unrealized P&L, DTE, and per-leg details.
    """
    session = get_session()
    positions = session._position_mgr.get_open_positions()
    greeks = session._position_mgr.get_portfolio_greeks()

    data = []
    for pos in positions:
        legs = [
            {
                "option_type": leg.option_type,
                "strike":      leg.strike,
                "action":      leg.action,
                "expiration":  leg.expiration.isoformat(),
                "mid_price":   leg.mid_price,
                "delta":       leg.delta,
                "theta":       leg.theta,
            }
            for leg in pos.legs
        ]
        data.append({
            "position_id":       pos.position_id,
            "ticker":            pos.ticker,
            "strategy":          pos.strategy.value,
            "pillar":            pos.pillar.value,
            "direction":         pos.direction,
            "contracts":         pos.contracts,
            "entry_price":       pos.entry_price,
            "entry_date":        pos.entry_date.isoformat(),
            "expiry_date":       pos.expiry_date.isoformat(),
            "target_close_date": pos.target_close_date.isoformat(),
            "max_loss_dollars":  pos.max_loss_dollars,
            "max_gain_dollars":  pos.max_gain_dollars,
            "unrealized_pnl":    getattr(pos, "unrealized_pnl", None),
            "status":            pos.status.value,
            "legs":              legs,
        })

    return JSONResponse({
        "positions":      data,
        "count":          len(data),
        "portfolio_greeks": greeks,
        "timestamp":      datetime.now(_ET).isoformat(),
    })


@router.get("/signals")
async def get_signals() -> JSONResponse:
    """
    Latest signal readings for each ticker in the universe.
    Sourced from the most recent pre-market macro context and PSI state.
    """
    session = get_session()
    macro = session._macro_context

    macro_out: dict[str, Any] = {}
    if macro:
        macro_out = {
            "macro_stance":   macro.macro_stance,
            "confidence":     macro.confidence,
            "vol_selling_ok": macro.vol_selling_ok,
            "size_bias":      macro.size_bias,
            "method":         macro.method,
        }

    # PSI drift check
    psi = session._psi.compute_psi()

    return JSONResponse({
        "macro":     macro_out,
        "psi":       psi,
        "universe":  session._settings.etf_universe,
        "timestamp": datetime.now(_ET).isoformat(),
    })


@router.get("/attribution")
async def get_attribution(days: int = 30) -> JSONResponse:
    """
    Per-pillar P&L attribution for the last `days` calendar days.
    Also includes slippage and regime-accuracy reports.
    """
    session = get_session()
    attributor = session._attributor

    return JSONResponse({
        "attribution":      attributor.attribution_report(days=days),
        "slippage":         attributor.slippage_report(days=days),
        "regime_accuracy":  attributor.regime_accuracy_report(),
        "days":             days,
        "timestamp":        datetime.now(_ET).isoformat(),
    })


@router.get("/health")
async def get_health() -> JSONResponse:
    """
    Kill-switch state, open position count, and session ID.
    Use this as the top-of-dashboard status widget.
    """
    session = get_session()
    kill = session._risk.get_kill_switch_state()
    open_count = len(session._position_mgr.get_open_positions())

    return JSONResponse({
        "session_id":    session._session_id,
        "trading_mode":  session._settings.trading_mode,
        "kill_switch":   kill,
        "open_positions": open_count,
        "timestamp":     datetime.now(_ET).isoformat(),
    })


class _KillBody(BaseModel):
    reason: str = "manual"


@router.post("/kill")
async def trip_kill(body: _KillBody) -> JSONResponse:
    """Trip the kill switch — blocks all new orders immediately."""
    session = get_session()
    session._risk.trip_kill_switch(reason=body.reason, tripped_by="api")
    logger.warning("Kill switch TRIPPED via API | reason=%s", body.reason)
    await _broadcast({"type": "kill", "reason": body.reason, "ts": datetime.now(_ET).isoformat()})
    return JSONResponse({"status": "tripped", "reason": body.reason})


@router.delete("/kill")
async def reset_kill() -> JSONResponse:
    """Reset the kill switch — re-enables trading."""
    session = get_session()
    session._risk.reset_kill_switch(reset_by="api")
    logger.info("Kill switch RESET via API")
    await _broadcast({"type": "kill_reset", "ts": datetime.now(_ET).isoformat()})
    return JSONResponse({"status": "reset"})


@router.websocket("/ws")
async def websocket_feed(ws: WebSocket) -> None:
    """
    Live event stream. On connect, replays the last 50 buffered events so
    the client sees recent history without a page reload.
    """
    await ws.accept()
    _ws_clients.add(ws)

    # Replay recent buffer so client isn't blank on connect
    for event in list(_event_buffer)[-50:]:
        try:
            await ws.send_json(event)
        except Exception:
            break

    try:
        while True:
            # Keep connection alive; broadcast is handled by _broadcast()
            await asyncio.sleep(30)
            await ws.send_json({"type": "ping", "ts": datetime.now(_ET).isoformat()})
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(ws)

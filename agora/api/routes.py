"""
AGORA dashboard routes.

GET  /agora/positions     — open positions with live unrealized P&L
GET  /agora/orders        — live order book: pending, today's fills, rejects, IBKR open orders
GET  /agora/signals       — latest signal state per ticker
GET  /agora/attribution   — per-pillar P&L attribution (last 30 days default)
GET  /agora/health        — kill-switch state + position count + account info
GET  /agora/market        — header ticker prices (SPY/QQQ/VIX/VIX9D/VIX3M)
POST /agora/kill          — trip kill switch (body: {"reason": "..."})
DELETE /agora/kill        — reset kill switch
POST /agora/trigger       — manually fire a session task (body: {"task": "premarket"|"scan"|"afterhours"})
GET  /agora/readiness     — live readiness meter (8-pillar score 0-100)
GET  /agora/journal       — trade journal: WHY each trade was taken (last 50)
POST /agora/ibkr-diagnose — ask the IBKR Knowledge Agent a question (body: {"question": "..."})
GET  /agora/ibkr-status   — live IBKR connectivity + open orders + fill rate
POST /agora/golive        — CEO approves go-live (body: {"approved_by": "CEO"})
DELETE /agora/golive      — revoke go-live approval
POST /agora/board-meeting — trigger CEO board meeting with all 7 C-suite (body: {"agenda": "..."})
GET  /agora/csuite        — C-suite intelligence snapshot (all departments)
WS   /agora/ws            — live event stream (JSON lines)
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from datetime import datetime
from typing import Any, Dict
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


@router.get("/orders")
async def get_orders() -> JSONResponse:
    """
    Live order book:
      pending       — orders submitted THIS session, not yet confirmed (in-memory only)
      fills_today   — confirmed fills today (fill price, slippage, legs from positions table)
      rejects_today — today's rejections with IBKR error code and reason
      ibkr_live     — open GTC orders visible in IBKR (from last 30-min scan cache)
      session_stats — today's totals from DB (accurate across restarts)
    """
    session = get_session()
    if session is None:
        return JSONResponse({"error": "session not ready"}, status_code=503)

    import sqlite3 as _sql
    from datetime import date as _date
    today = _date.today().isoformat()
    db_path = session._settings.db_path

    try:
        conn = _sql.connect(str(db_path), check_same_thread=False)

        # ── Pending: ONLY in-memory (reliable — no stale DB rows after startup cleanup) ──
        # The DB pending rows are cleaned to 'timeout' at session start, so we
        # derive pending from the live session counters instead.
        eq = getattr(session, "_exec_quality", None)
        sess_mem = eq.get_session_stats() if eq else {}
        n_fills   = sess_mem.get("fills", 0)
        n_rejects = sess_mem.get("rejects", 0)
        n_attempts = sess_mem.get("attempts", 0)
        # Pending = attempts that haven't resolved yet this session
        n_pending = max(0, n_attempts - n_fills - n_rejects)

        # Pull the most-recent pending rows from DB for this session (post-cleanup IDs)
        # These are rows inserted AFTER the startup cleanup so they're genuinely pending.
        pending_rows = conn.execute(
            "SELECT ticker, strategy, mid_price, attempt_date "
            "FROM execution_quality WHERE outcome='pending' AND attempt_date=? "
            "ORDER BY id DESC LIMIT 20",
            (today,),
        ).fetchall()
        pending = [
            {"ticker": r[0], "strategy": r[1], "mid_price": r[2], "attempt_date": r[3]}
            for r in pending_rows
        ]

        # ── Today's fills — enriched with position leg data ──
        # NOTE: only fetch fills that have a matching position record.
        # Fills with no position = prior-session crash between record_fill() and
        # _record_position() — mark them separately as orphaned so the UI can warn.
        fill_rows = conn.execute(
            "SELECT eq.ticker, eq.strategy, eq.mid_price, eq.fill_price, eq.slippage_ticks, "
            "       CASE WHEN p.ticker IS NOT NULL THEN 1 ELSE 0 END AS has_position "
            "FROM execution_quality eq "
            "LEFT JOIN positions p ON p.ticker = eq.ticker "
            "WHERE eq.outcome='fill' AND eq.attempt_date=? "
            "ORDER BY eq.id DESC LIMIT 30",
            (today,),
        ).fetchall()

        # Build a position map for leg enrichment (keyed by ticker)
        pos_rows = conn.execute(
            "SELECT ticker, legs_json, entry_price, expiry_date, direction, strategy, contracts "
            "FROM positions WHERE status IN ('open','tested','rolled')"
        ).fetchall()
        pos_map: dict = {}
        for pr in pos_rows:
            import json as _json
            try:
                legs = _json.loads(pr[1] or "[]")
            except Exception:
                legs = []
            pos_map[pr[0]] = {
                "legs": legs, "entry_price": pr[2],
                "expiry_date": pr[3], "direction": pr[4],
                "strategy": pr[5], "contracts": pr[6],
            }

        # Also pull from trade_journal for leg details on deleted positions
        journal_rows = conn.execute(
            "SELECT ticker, legs_summary, direction "
            "FROM trade_journal WHERE entry_date=? ORDER BY rowid DESC",
            (today,),
        ).fetchall() if conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='trade_journal'"
        ).fetchone() else []
        # Build journal map (ticker → first entry for today)
        journal_map: dict = {}
        for jr in journal_rows:
            if jr[0] not in journal_map:
                journal_map[jr[0]] = {
                    "legs_summary": jr[1], "direction": jr[2],
                    "expiry_date": "", "contracts": 1,
                }

        fills_today = []
        orphaned_fills = []   # fills with no position record (prior-session crash)
        for r in fill_rows:
            ticker, strat, mid, fill_px, slip, has_pos = r
            pos = pos_map.get(ticker, {})
            jrn = journal_map.get(ticker, {})
            legs = pos.get("legs", [])
            leg_summary = " / ".join(
                f"{l.get('action','').upper()} {l.get('option_type','').upper()[0] if l.get('option_type') else ''}"
                f"{l.get('strike','')}"
                for l in legs
            ) if legs else jrn.get("legs_summary", "")
            direction  = pos.get("direction", "") or jrn.get("direction", "")
            expiry     = pos.get("expiry_date", "") or jrn.get("expiry_date", "")
            contracts  = pos.get("contracts", 1) or jrn.get("contracts", 1)
            fill_obj = {
                "ticker":        ticker,
                "strategy":      strat,
                "mid_price":     round(mid or 0, 2),
                "fill_price":    round(fill_px or 0, 2),
                "slippage":      round(slip or 0, 4),
                "direction":     direction,
                "expiry_date":   expiry,
                "contracts":     contracts,
                "leg_summary":   leg_summary,
                "has_position":  bool(has_pos),
            }
            if has_pos:
                fills_today.append(fill_obj)
            else:
                orphaned_fills.append(fill_obj)

        # ── Today's rejects ──
        rej_rows = conn.execute(
            "SELECT ticker, strategy, mid_price, reject_code, reject_reason "
            "FROM execution_quality WHERE outcome='reject' AND attempt_date=? "
            "ORDER BY id DESC LIMIT 20",
            (today,),
        ).fetchall()
        rejects_today = [
            {"ticker": r[0], "strategy": r[1], "mid_price": round(r[2] or 0, 2),
             "reject_code": r[3], "reject_reason": (r[4] or "")[:120]}
            for r in rej_rows
        ]

        # ── Today's all-time totals (DB-accurate, survives restarts) ──
        totals = conn.execute(
            "SELECT outcome, COUNT(*) FROM execution_quality "
            "WHERE attempt_date=? GROUP BY outcome",
            (today,),
        ).fetchall()
        total_map = {r[0]: r[1] for r in totals}
        db_fills    = total_map.get("fill", 0)
        db_rejects  = total_map.get("reject", 0)
        db_timeouts = total_map.get("timeout", 0)
        db_attempts = db_fills + db_rejects + db_timeouts + len(pending)
        # Fill rate = confirmed fills / all attempts (timeouts count as misses)
        db_fill_rate = f"{db_fills/db_attempts:.1%}" if db_attempts else "—"

        conn.close()

    except Exception as exc:
        logger.error("Orders DB query failed: %s", exc)
        pending = fills_today = rejects_today = []
        db_fills = db_rejects = db_timeouts = db_attempts = 0
        db_fill_rate = "—"

    # ── Live IBKR open orders from cached scan (no blocking poll) ──
    ibkr_live: list[dict] = []
    ibkr_connected = False
    if hasattr(session, "_ibkr_agent") and session._ibkr_agent:
        st = session._ibkr_agent.get_status()
        ibkr_connected = st.get("connection_healthy", False)
        scan = st.get("last_scan") or {}
        ibkr_live = scan.get("gtc_order_details", []) or []

    return JSONResponse({
        "pending":           pending,
        "fills_today":       fills_today,
        "orphaned_fills":    orphaned_fills,
        "rejects_today":     rejects_today,
        "ibkr_live":         ibkr_live,
        "ibkr_connected":    ibkr_connected,
        "session_stats": {
            "attempts":      db_attempts,
            "fills":         db_fills,
            "rejects":       db_rejects,
            "timeouts":      db_timeouts,
            "fill_rate":     db_fill_rate,
            "error_201_storm": sess_mem.get("error_201_storm", False),
        },
        "date":      today,
        "timestamp": datetime.now(_ET).isoformat(),
    })


@router.get("/signals")
async def get_signals() -> JSONResponse:
    """
    Latest signal readings for each ticker in the universe.
    Sourced from the most recent pre-market macro context and PSI state.
    """
    session = get_session()
    # Fall back to synthesizer's cached context if session just restarted
    macro = session._macro_context or getattr(session._macro, "_last_context", None)

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

    from datetime import date, timedelta
    end_dt = date.today()
    start_dt = end_dt - timedelta(days=days)
    return JSONResponse({
        "attribution":      attributor.attribution_report(start_date=start_dt, end_date=end_dt),
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

    sys_health = {}
    if hasattr(session, "_system_health") and session._system_health:
        sys_health = session._system_health.get_status()

    return JSONResponse({
        "session_id":    session._session_id,
        "trading_mode":  session._settings.trading_mode,
        "kill_switch":   kill,
        "open_positions": open_count,
        "system_health": sys_health,
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


@router.get("/market")
async def get_market() -> JSONResponse:
    """
    Current prices for SPY, QQQ, VIX, VIX9D, VIX3M — used by the dashboard header.
    Fetched via yfinance fast_info in a thread pool to avoid blocking the event loop.
    """
    import asyncio
    from functools import partial

    import yfinance as yf

    SYMBOLS = {
        "SPY":  "SPY",
        "QQQ":  "QQQ",
        "VIX":  "^VIX",
        "VIX9D": "^VIX9D",
        "VIX3M": "^VIX3M",
    }

    def _fetch(label: str, ticker_str: str) -> tuple[str, dict]:
        try:
            fi = yf.Ticker(ticker_str).fast_info
            # fast_info supports both attribute and dict-style access depending on version
            try:
                price = float(fi.last_price or 0)
                prev  = float(fi.previous_close or 0)
            except AttributeError:
                price = float(fi.get("lastPrice", 0) or 0)
                prev  = float(fi.get("previousClose", 0) or 0)
            chg_pct = (price - prev) / prev * 100 if prev else 0.0
            return label, {"price": round(price, 2), "change_pct": round(chg_pct, 2)}
        except Exception:
            return label, {"price": None, "change_pct": 0.0}

    loop = asyncio.get_event_loop()
    results = await asyncio.gather(
        *[loop.run_in_executor(None, partial(_fetch, label, sym))
          for label, sym in SYMBOLS.items()]
    )
    return JSONResponse({
        "market":    dict(results),
        "timestamp": datetime.now(_ET).isoformat(),
    })


class _TriggerBody(BaseModel):
    task: str = "premarket"  # "premarket" | "scan" | "afterhours"


@router.post("/trigger")
async def trigger_task(body: _TriggerBody) -> JSONResponse:
    """
    Manually fire a session task without waiting for the scheduled window.
    Useful for testing outside market hours.
    """
    session = get_session()
    task_map = {
        "premarket":        session._premarket_macro_scan,
        "scan":             session._universe_scan,
        "afterhours":       session._afterhours_report,
        "orphan_reconcile": session._orphan_reconciler.reconcile_now,
    }
    fn = task_map.get(body.task)
    if fn is None:
        return JSONResponse(
            {"error": f"unknown task '{body.task}' — valid: premarket, scan, afterhours, orphan_reconcile"},
            status_code=400,
        )
    asyncio.create_task(fn())
    logger.info("Manual trigger: %s", body.task)
    await _broadcast({"type": "trigger", "task": body.task, "ts": datetime.now(_ET).isoformat()})
    return JSONResponse({"status": "triggered", "task": body.task, "timestamp": datetime.now(_ET).isoformat()})


class _GoLiveBody(BaseModel):
    approved_by: str = "CEO"

class _RevokeBody(BaseModel):
    revoked_by: str = "operator"

class _IBKRDiagnoseBody(BaseModel):
    question: str


class _BoardMeetingBody(BaseModel):
    agenda: str = ""


@router.get("/readiness")
async def get_readiness() -> JSONResponse:
    """
    Live Readiness Meter — 8-pillar go-live readiness score (0-100 per pillar).
    CEO approval required before go-live. Returns fireworks=true when approved.
    """
    session = get_session()
    score = session.readiness.get_score()
    return JSONResponse(score)


@router.post("/golive")
async def approve_go_live(body: _GoLiveBody) -> JSONResponse:
    """
    CEO approves go-live. Requires overall score >= 80 and no pillar below 60.
    On approval, fireworks=true is returned and persisted until revoked.
    """
    session = get_session()
    result = session.readiness.approve_go_live(approved_by=body.approved_by)
    if result["approved"]:
        await _broadcast({
            "type": "golive",
            "approved": True,
            "approved_by": body.approved_by,
            "score": result["score"]["overall_score"],
            "ts": datetime.now(_ET).isoformat(),
        })
    return JSONResponse(result)


@router.delete("/golive")
async def revoke_go_live() -> JSONResponse:
    """Revoke go-live approval."""
    session = get_session()
    session.readiness.revoke_go_live()
    await _broadcast({"type": "golive_revoked", "ts": datetime.now(_ET).isoformat()})
    return JSONResponse({"status": "revoked"})


@router.post("/board-meeting")
async def trigger_board_meeting(body: _BoardMeetingBody) -> JSONResponse:
    """
    Trigger a full CEO board meeting with all 7 C-suite executives.
    Runs asynchronously. Returns immediately; meeting log posted to Discord.
    """
    session = get_session()
    asyncio.create_task(session._ceo.board_meeting(agenda=body.agenda))
    logger.info("Board meeting triggered via API: agenda=%s", body.agenda or "(default)")
    await _broadcast({
        "type": "board_meeting",
        "agenda": body.agenda,
        "ts": datetime.now(_ET).isoformat(),
    })
    return JSONResponse({
        "status": "board_meeting_started",
        "agenda": body.agenda or "system readiness review and go-live strategy",
        "note": "Meeting transcript will be posted to Discord.",
    })


@router.get("/csuite")
async def get_csuite_intelligence() -> JSONResponse:
    """
    C-suite intelligence snapshot — collect_intelligence() from all 7 departments.
    Used for the dashboard department status cards.
    """
    session = get_session()
    result: dict[str, Any] = {
        "timestamp": datetime.now(_ET).isoformat(),
        "departments": {},
    }
    c_suite_map = {
        "cro":   ("CRO",   session._cro),
        "cio":   ("CIO",   session._cio),
        "cto":   ("CTO",   session._cto),
        "coo":   ("COO",   session._coo),
        "cfo":   ("CFO",   session._cfo),
        "rnd":   ("R&D",   session._rnd),
        "ctech": ("CTech", session._ctech),
    }
    for key, (title, agent) in c_suite_map.items():
        if agent is None:
            result["departments"][key] = {"title": title, "status": "not_wired"}
            continue
        try:
            intel = agent.collect_intelligence()
            audit = agent.get_audit_summary()
            result["departments"][key] = {
                "title": title,
                "status": "ok",
                "intel": intel,
                "audit": audit,
            }
        except Exception as exc:
            result["departments"][key] = {"title": title, "status": "error", "error": str(exc)}

    # Session plan from CEO
    try:
        sp = session._ceo.get_session_plan() if session._ceo else None
        if sp is not None:
            import dataclasses as _dc
            result["session_plan"] = _dc.asdict(sp) if _dc.is_dataclass(sp) else sp.__dict__
    except Exception as _exc:
        result["session_plan"] = {"error": str(_exc)}

    return JSONResponse(result)


@router.get("/board-meeting/latest")
async def get_latest_board_meeting() -> JSONResponse:
    """Return the transcript of the most recent board meeting, if available."""
    import json as _json
    from pathlib import Path as _Path
    p = _Path(".agora/last_board_meeting.json")
    if not p.exists():
        return JSONResponse({"status": "no_meeting_yet"}, status_code=404)
    return JSONResponse(_json.loads(p.read_text()))


@router.post("/ibkr-diagnose")
async def ibkr_diagnose(body: _IBKRDiagnoseBody) -> JSONResponse:
    """
    Ask the IBKR Knowledge Agent (Claude expert) a question about execution,
    connectivity, error codes, or AGORA's IBKR setup.
    Uses adaptive thinking + full knowledge base. ~5-10 second response.
    """
    session = get_session()
    if session is None:
        return JSONResponse({"error": "session not ready"}, status_code=503)
    try:
        if not hasattr(session, "_ibkr_agent") or session._ibkr_agent is None:
            return JSONResponse({"error": "IBKRKnowledgeAgent not wired"}, status_code=503)
        answer = await session._ibkr_agent.diagnose(body.question)
        live_status = session._ibkr_agent.get_status()
        return JSONResponse({
            "question": body.question,
            "answer": answer,
            "ibkr_status": live_status,
        })
    except Exception as exc:
        logger.error("IBKR diagnose error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


def _tws_recon_warning(live: dict, session: Any) -> list[str]:
    """Compare TWS BAG fills vs shadow book to surface missing entries."""
    warnings: list[str] = []
    try:
        bag_fills = [f for f in live.get("tws_fills", []) if f.get("secType") == "BAG"]
        tws_bought = {f["symbol"] for f in bag_fills if f.get("action") == "BOT"}
        tws_sold   = {f["symbol"] for f in bag_fills if f.get("action") == "SLD"}
        # Symbols with both a BOT and SLD are closed round-trips
        closed     = tws_bought & tws_sold
        # Still open in TWS (entry filled, no close yet)
        tws_open   = tws_bought - tws_sold

        shadow_symbols = {p.ticker for p in session._position_mgr.get_open_positions()}

        # TWS says open position exists but not in shadow book
        missing_in_shadow = tws_open - shadow_symbols
        for sym in missing_in_shadow:
            warnings.append(f"⚠ {sym}: filled in TWS but missing from shadow book — run reconcile")

        # Shadow book shows open but TWS says closed (both BOT+SLD seen)
        closed_but_in_shadow = closed & shadow_symbols
        for sym in closed_but_in_shadow:
            warnings.append(f"⚠ {sym}: closed in TWS but still open in shadow book — stale position")
    except Exception:
        pass
    return warnings


@router.get("/tws")
async def get_tws_live() -> JSONResponse:
    """
    Direct TWS live data — polls IBKR on-demand (not cached).
    Returns ALL open orders across ALL client IDs, plus live option positions.
    Use this when you need real-time order book from TWS, not our shadow book.
    """
    session = get_session()
    if session is None:
        return JSONResponse({"error": "session not ready"}, status_code=503)
    try:
        if not hasattr(session, "_ibkr_agent") or session._ibkr_agent is None:
            return JSONResponse({"error": "IBKRKnowledgeAgent not wired"}, status_code=503)
        # Force a fresh live poll (bypasses cached last_scan)
        live = await session._ibkr_agent._collect_ibkr_state()
        return JSONResponse({
            "connected":             live.get("connected", False),
            "managed_accounts":      live.get("managed_accounts", []),
            "open_orders":           live.get("gtc_order_details", []),
            "open_order_count":      live.get("open_order_count", 0),
            "gtc_count":             live.get("orphan_gtc_count", 0),
            "ibkr_positions":        live.get("ibkr_positions_detail", []),
            "ibkr_option_positions": live.get("ibkr_option_positions", 0),
            "shadow_book_positions": live.get("shadow_book_positions", 0),
            # Show BAG-level fills (spread-level); OPT legs are redundant for UI
            "tws_fills":             [f for f in live.get("tws_fills", []) if f.get("secType") == "BAG"],
            "tws_fills_all":         live.get("tws_fills", []),
            "tws_fill_count":        sum(1 for f in live.get("tws_fills", []) if f.get("secType") == "BAG"),
            "execution_session":     live.get("execution_session", {}),
            "recon_warning":         _tws_recon_warning(live, session),
            "timestamp":             datetime.now(_ET).isoformat(),
        })
    except Exception as exc:
        logger.error("TWS live poll error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/today")
async def get_today_summary() -> JSONResponse:
    """
    Today's activity at a glance:
      closed_today       — positions closed today (from DB, close_date = today)
      realized_pnl_today — sum of realized_pnl for closed_today
      open_count         — current open positions
      unrealized_pnl_today — sum of unrealized P&L on open positions
      kill_switch        — current kill-switch state
      events             — human-readable timeline of what happened today
    """
    session = get_session()
    if session is None:
        return JSONResponse({"error": "session not ready"}, status_code=503)

    import sqlite3 as _sql
    from datetime import date as _date
    today = _date.today().isoformat()
    db_path = session._settings.db_path

    closed_today: list[dict] = []
    realized_pnl_today = 0.0
    try:
        conn = _sql.connect(str(db_path), check_same_thread=False)
        rows = conn.execute(
            """SELECT ticker, strategy, direction, contracts,
                      entry_price, close_price, realized_pnl, close_source
               FROM positions
               WHERE close_date = ?
               ORDER BY rowid DESC""",
            (today,),
        ).fetchall()
        conn.close()
        for r in rows:
            pnl = float(r[6] or 0.0)
            realized_pnl_today += pnl
            closed_today.append({
                "ticker":       r[0],
                "strategy":     r[1],
                "direction":    r[2],
                "contracts":    r[3],
                "entry_price":  r[4],
                "close_price":  r[5],
                "realized_pnl": round(pnl, 2),
                "close_source": r[7] or "",
            })
    except Exception as exc:
        logger.warning("today: DB query failed — %s", exc)

    open_positions = session._position_mgr.get_open_positions()
    unrealized_pnl_today = sum(
        float(getattr(p, "unrealized_pnl", 0) or 0) for p in open_positions
    )

    kill = session._risk.get_kill_switch_state()

    _source_labels = {
        "tws_startup_sync":  "Startup Sync",
        "tws_orphan_reconcile": "Orphan Reconcile",
        "profit_target":     "Profit Target Hit",
        "stop_loss":         "Stop Loss Hit",
        "dte_close":         "DTE Close",
        "manual":            "Manual Close",
    }

    events: list[dict] = []
    for pos in closed_today:
        src_key = (pos["close_source"] or "").split(":")[0]
        label = _source_labels.get(src_key, pos["close_source"] or "unknown")
        pnl = pos["realized_pnl"]
        pnl_str = f"+${pnl:.0f}" if pnl >= 0 else f"-${abs(pnl):.0f}"
        events.append({
            "type":  "close",
            "label": (
                f"{pos['ticker']} closed via {label} — "
                f"entry ${pos['entry_price']:.2f} → ${pos['close_price']:.2f} ({pnl_str})"
            ),
        })
    if kill.get("active"):
        events.append({
            "type":  "kill_switch",
            "label": f"Kill switch ACTIVE — {kill.get('reason', 'unknown')}",
        })

    return JSONResponse({
        "closed_today":          closed_today,
        "closed_count":          len(closed_today),
        "realized_pnl_today":    round(realized_pnl_today, 2),
        "open_count":            len(open_positions),
        "unrealized_pnl_today":  round(unrealized_pnl_today, 2),
        "total_pnl_today":       round(realized_pnl_today + unrealized_pnl_today, 2),
        "kill_switch":           kill,
        "events":                events,
        "timestamp":             datetime.now(_ET).isoformat(),
    })


@router.get("/ibkr-status")
async def ibkr_status() -> JSONResponse:
    """Current IBKR connectivity, open order count, orphan GTC count, fill rate."""
    session = get_session()
    if session is None:
        return JSONResponse({"error": "session not ready"}, status_code=503)
    try:
        if not hasattr(session, "_ibkr_agent") or session._ibkr_agent is None:
            return JSONResponse({"error": "IBKRKnowledgeAgent not wired"}, status_code=503)
        connectivity = await session._ibkr_agent.assess_connectivity()
        status = session._ibkr_agent.get_status()
        return JSONResponse({"connectivity": connectivity, **status})
    except Exception as exc:
        logger.error("IBKR status error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/journal")
async def get_trade_journal(limit: int = 50) -> JSONResponse:
    """
    Trade journal: last N entries showing WHY each trade was taken.
    Includes conviction score, macro context, legs, and reasoning.
    """
    session = get_session()
    if session is None:
        return JSONResponse({"error": "session not ready"}, status_code=503)
    try:
        import sqlite3 as _sql
        db_path = session._settings.db_path
        conn = _sql.connect(str(db_path), check_same_thread=False)
        rows = conn.execute(
            """SELECT journal_id, ticker, strategy, direction, pillar,
                      entry_date, spot_at_entry, entry_price,
                      max_loss, max_gain, rr_ratio, conviction,
                      gate, legs_summary, why_traded,
                      macro_at_entry, regime_at_entry, ibkr_order_id
               FROM trade_journal
               ORDER BY entry_date DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        conn.close()
        cols = [
            "journal_id", "ticker", "strategy", "direction", "pillar",
            "entry_date", "spot_at_entry", "entry_price",
            "max_loss", "max_gain", "rr_ratio", "conviction",
            "gate", "legs_summary", "why_traded",
            "macro_at_entry", "regime_at_entry", "ibkr_order_id",
        ]
        return JSONResponse([dict(zip(cols, r)) for r in rows])
    except Exception as exc:
        logger.error("Journal fetch error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


# ── Chart data endpoints ──────────────────────────────────────────────────────

_chart_cache: dict[str, tuple[float, Any]] = {}
_CHART_CACHE_TTL = 900  # 15 minutes


@router.get("/chart/price/{ticker}")
async def chart_price(ticker: str) -> JSONResponse:
    """5-day hourly OHLCV for position mini-chart. Cached 15 minutes."""
    cache_key = f"price:{ticker}"
    ts_now = datetime.now().timestamp()
    if cache_key in _chart_cache:
        ts, data = _chart_cache[cache_key]
        if ts_now - ts < _CHART_CACHE_TTL:
            return JSONResponse(data)

    loop = asyncio.get_event_loop()
    try:
        def _fetch():
            import yfinance as yf
            df = yf.download(ticker, period="5d", interval="1h", progress=False, auto_adjust=True)
            if df.empty:
                return None
            if hasattr(df.columns, "get_level_values"):
                df.columns = df.columns.get_level_values(0)
            return {
                "ticker": ticker,
                "times":  [str(t) for t in df.index],
                "close":  [round(float(v), 2) for v in df["Close"]],
                "high":   [round(float(v), 2) for v in df["High"]],
                "low":    [round(float(v), 2) for v in df["Low"]],
                "volume": [int(v) for v in df["Volume"]],
            }

        data = await loop.run_in_executor(None, _fetch)
        if not data:
            return JSONResponse({"error": "no data"}, status_code=404)
        _chart_cache[cache_key] = (ts_now, data)
        return JSONResponse(data)
    except Exception as exc:
        logger.error("chart_price %s: %s", ticker, exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/chart/ivrank/{ticker}")
async def chart_ivrank(ticker: str) -> JSONResponse:
    """30-day rolling realized vol + current ATM IV. Cached 15 minutes."""
    cache_key = f"ivrank:{ticker}"
    ts_now = datetime.now().timestamp()
    if cache_key in _chart_cache:
        ts, data = _chart_cache[cache_key]
        if ts_now - ts < _CHART_CACHE_TTL:
            return JSONResponse(data)

    loop = asyncio.get_event_loop()
    try:
        def _fetch():
            import math
            import yfinance as yf
            t = yf.Ticker(ticker)
            hist = t.history(period="1y")
            if hist.empty:
                return None

            closes = list(hist["Close"])
            log_ret = []
            for i in range(1, len(closes)):
                prev, cur = closes[i - 1], closes[i]
                if prev > 0 and cur > 0:
                    log_ret.append(math.log(cur / prev))

            # 21-day rolling realized vol (annualized, in %)
            hv_series = []
            for i in range(20, len(log_ret)):
                w = log_ret[i - 20: i + 1]
                mean = sum(w) / len(w)
                var  = sum((x - mean) ** 2 for x in w) / len(w)
                hv_series.append(round(math.sqrt(var * 252) * 100, 1))

            hist_dates = list(hist.index)
            date_offset = len(hist_dates) - len(hv_series)
            all_dates = [str(hist_dates[date_offset + i])[:10] for i in range(len(hv_series))]

            hv_last30    = hv_series[-30:]
            dates_last30 = all_dates[-30:]

            hv_min = min(hv_series) if hv_series else 0.0
            hv_max = max(hv_series) if hv_series else 100.0
            hv_now = hv_last30[-1] if hv_last30 else None
            iv_rank = None
            if hv_now is not None and (hv_max - hv_min) > 0:
                iv_rank = round((hv_now - hv_min) / (hv_max - hv_min) * 100, 1)

            iv_current = None
            try:
                exps = t.options
                if exps:
                    chain = t.option_chain(exps[0])
                    spot  = float(closes[-1])
                    calls = chain.calls
                    if not calls.empty:
                        idx = (calls["strike"] - spot).abs().idxmin()
                        iv_val = calls.loc[idx, "impliedVolatility"]
                        if iv_val and float(iv_val) > 0:
                            iv_current = round(float(iv_val) * 100, 1)
            except Exception:
                pass

            return {
                "ticker":     ticker,
                "dates":      dates_last30,
                "hv":         hv_last30,
                "iv_current": iv_current,
                "iv_rank":    iv_rank,
                "hv_min":     round(hv_min, 1),
                "hv_max":     round(hv_max, 1),
            }

        data = await loop.run_in_executor(None, _fetch)
        if not data:
            return JSONResponse({"error": "no data"}, status_code=404)
        _chart_cache[cache_key] = (ts_now, data)
        return JSONResponse(data)
    except Exception as exc:
        logger.error("chart_ivrank %s: %s", ticker, exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/chart/pnl/{position_id}")
async def chart_pnl(position_id: str) -> JSONResponse:
    """P&L at expiry curve — pure math from leg strikes, no external data."""
    session = get_session()
    if session is None:
        return JSONResponse({"error": "session not ready"}, status_code=503)

    positions = session._position_mgr.get_open_positions()
    pos = next(
        (p for p in positions
         if str(p.position_id) == position_id or p.ticker == position_id),
        None,
    )
    if pos is None:
        return JSONResponse({"error": "position not found"}, status_code=404)

    legs = pos.legs
    contracts = max(pos.contracts or 1, 1)

    strikes = [l.strike for l in legs if l.strike]
    ref = sum(strikes) / len(strikes) if strikes else 100.0
    lo, hi = ref * 0.75, ref * 1.25
    prices = [round(lo + (hi - lo) * i / 100, 2) for i in range(101)]

    def _payoff(leg, S: float) -> float:
        k = leg.strike or 0.0
        is_call = (leg.option_type or "").lower().startswith("c")
        intrinsic = max(S - k, 0.0) if is_call else max(k - S, 0.0)
        sign = 1 if (leg.action or "").lower() == "buy" else -1
        return sign * intrinsic * 100 * contracts

    entry_total = (pos.entry_price or 0.0) * 100 * contracts

    pnl_curve = [round(sum(_payoff(l, S) for l in legs) - entry_total, 2) for S in prices]

    breakevens = []
    for i in range(len(pnl_curve) - 1):
        a, b = pnl_curve[i], pnl_curve[i + 1]
        if a * b <= 0 and b != a:
            be = prices[i] + (prices[i + 1] - prices[i]) * (-a) / (b - a)
            breakevens.append(round(be, 2))

    return JSONResponse({
        "ticker":      pos.ticker,
        "position_id": position_id,
        "prices":      prices,
        "pnl":         pnl_curve,
        "max_gain":    pos.max_gain_dollars,
        "max_loss":    pos.max_loss_dollars,
        "breakevens":  breakevens,
        "strikes":     [l.strike for l in legs],
        "entry_price": pos.entry_price,
    })


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

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
GET  /agora/decisions     — recent decision chains: what was evaluated, what passed, what filled
GET  /agora/kpis          — baseline KPI snapshot (generated once at Phase 0, refreshed daily)
GET  /agora/costs         — LLM cost breakdown today vs $15/day cap (?date=YYYY-MM-DD optional)
GET  /agora/health/strategy — rolling Sharpe per (pillar, regime) and currently paused cells
GET  /agora/health/scan    — async scan engine status: queue depth, wait times, shadow mode
GET  /agora/health/analyst — analyst thesis hit rate, calibration, cost (triggers attribution pass)
GET  /agora/lessons/pending          — agent lessons awaiting human approval
POST /agora/lessons/{id}/approve     — approve a lesson (body: {"approved_by": "CEO"})
POST /agora/lessons/{id}/reject      — reject a lesson (body: {"reason": "..."})
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

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .state import get_session
from ..ops.llm_cost_log import daily_cost_summary as _llm_daily_cost
from ..ops.decision_chains import recent_chains as _recent_chains
from ..ops.strategy_health import StrategyHealthAgent as _StrategyHealthAgent
from ..ops.outcome_attributor import (
    get_analyst_stats as _get_analyst_stats,
    attribute_closed_trades as _attribute_now,
    get_promotion_readiness as _get_promotion_readiness,
)

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
    unrealized_pnl is sourced from IBKR portfolio data when a recent scan is
    available (pnl_source="ibkr"); otherwise falls back to yfinance mid-price
    (pnl_source="yfinance").  ibkr_pnl_age_seconds tells the dashboard how
    stale the IBKR data is.
    """
    session = get_session()
    positions = session._position_mgr.get_open_positions()
    greeks = session._position_mgr.get_portfolio_greeks()

    # Pull IBKR portfolio cache — keyed by (symbol, secType) → aggregated P&L
    ibkr_by_symbol: dict[str, dict] = {}
    ibkr_portfolio_age: float | None = None
    ibkr_agent = getattr(session, "_ibkr_agent", None)
    if ibkr_agent is not None:
        try:
            items, age = ibkr_agent.get_cached_portfolio()
            ibkr_portfolio_age = age
            # Aggregate per underlying symbol across all option legs
            for item in items:
                sym = item["symbol"]
                if sym not in ibkr_by_symbol:
                    ibkr_by_symbol[sym] = {
                        "unrealized_pnl": 0.0,
                        "realized_pnl":   0.0,
                        "market_value":   0.0,
                        "leg_items":      [],
                    }
                ibkr_by_symbol[sym]["unrealized_pnl"] += item["unrealized_pnl"]
                ibkr_by_symbol[sym]["realized_pnl"]   += item["realized_pnl"]
                ibkr_by_symbol[sym]["market_value"]   += item["market_value"]
                ibkr_by_symbol[sym]["leg_items"].append(item)
        except Exception:
            pass

    # IBKR data is considered fresh when < 10 min old
    IBKR_MAX_AGE = 600.0
    ibkr_fresh = (
        ibkr_portfolio_age is not None
        and ibkr_portfolio_age < IBKR_MAX_AGE
        and bool(ibkr_by_symbol)
    )

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

        yf_pnl    = getattr(pos, "unrealized_pnl", None)
        ibkr_entry = ibkr_by_symbol.get(pos.ticker)
        if ibkr_fresh and ibkr_entry is not None:
            unrealized_pnl = round(ibkr_entry["unrealized_pnl"], 2)
            pnl_source     = "ibkr"
        else:
            unrealized_pnl = yf_pnl
            pnl_source     = "yfinance"

        row: dict = {
            "position_id":         pos.position_id,
            "ticker":              pos.ticker,
            "strategy":            pos.strategy.value,
            "pillar":              pos.pillar.value,
            "direction":           pos.direction,
            "contracts":           pos.contracts,
            "entry_price":         pos.entry_price,
            "entry_date":          pos.entry_date.isoformat(),
            "expiry_date":         pos.expiry_date.isoformat(),
            "target_close_date":   pos.target_close_date.isoformat(),
            "max_loss_dollars":    pos.max_loss_dollars,
            "max_gain_dollars":    pos.max_gain_dollars,
            "unrealized_pnl":      unrealized_pnl,
            "yfinance_pnl":        yf_pnl,
            "pnl_source":          pnl_source,
            "ibkr_pnl_age_seconds": round(ibkr_portfolio_age, 0) if ibkr_portfolio_age is not None else None,
            "status":              pos.status.value,
            "legs":                legs,
        }
        if ibkr_entry:
            row["ibkr_market_value"] = round(ibkr_entry["market_value"], 2)
        pe = session._position_mgr.get_profit_engine_state(pos.position_id)
        if pe is not None:
            row["profit_engine"] = pe
        data.append(row)

    return JSONResponse({
        "positions":      data,
        "count":          len(data),
        "portfolio_greeks": greeks,
        "ibkr_pnl_fresh": ibkr_fresh,
        "ibkr_pnl_age_seconds": round(ibkr_portfolio_age, 0) if ibkr_portfolio_age is not None else None,
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


class _COODispatchBody(BaseModel):
    report: dict = {}   # trade report / context to hand to COO


@router.post("/coo/dispatch")
async def coo_dispatch(body: _COODispatchBody) -> JSONResponse:
    """
    Dispatch a trade report directly to the COO with full executive authority.
    COO will:
      1. Run self_audit() against live DB state
      2. Run self_heal() — autonomous corrective actions (reconciler, peer alerts)
      3. Produce a Claude-synthesized brief using the supplied report as context
    Returns audit findings, actions taken, and the COO's brief.
    """
    session = get_session()
    if session is None:
        return JSONResponse({"error": "session not ready"}, status_code=503)
    coo = getattr(session, "_coo", None)
    if coo is None:
        return JSONResponse({"error": "COO not wired in this session"}, status_code=503)

    # 1. Self-audit — detect live issues right now
    findings = coo.self_audit()

    # 2. Self-heal — autonomous actions with pre-delegated authority
    actions_taken: list[str] = []
    if findings:
        await coo.self_heal(findings)
        actions_taken = [f"{k}: [{s}] {m}" for k, s, m in findings]

    # 3. Produce Claude brief — COO synthesises report + audit state
    intel = coo.collect_intelligence()
    intel["injected_trade_report"] = body.report
    brief = await coo.produce_brief(context=intel)

    logger.info("COO dispatch complete — %d findings, brief generated", len(findings))
    await _broadcast({
        "type":    "coo_dispatch",
        "findings": len(findings),
        "ts":       datetime.now(_ET).isoformat(),
    })

    return JSONResponse({
        "status":        "dispatched",
        "audit_findings": [{"key": k, "severity": s, "message": m} for k, s, m in findings],
        "actions_taken":  actions_taken,
        "coo_brief":      brief,
        "timestamp":      datetime.now(_ET).isoformat(),
    })


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
        portfolio_items = live.get("ibkr_portfolio_items", [])
        # Summarise P&L per underlying symbol from portfolio items
        pnl_by_symbol: dict[str, float] = {}
        for item in portfolio_items:
            sym = item["symbol"]
            pnl_by_symbol[sym] = pnl_by_symbol.get(sym, 0.0) + item["unrealized_pnl"]
        return JSONResponse({
            "connected":             live.get("connected", False),
            "managed_accounts":      live.get("managed_accounts", []),
            "open_orders":           live.get("gtc_order_details", []),
            "open_order_count":      live.get("open_order_count", 0),
            "gtc_count":             live.get("orphan_gtc_count", 0),
            "ibkr_positions":        live.get("ibkr_positions_detail", []),
            "ibkr_option_positions": live.get("ibkr_option_positions", 0),
            "shadow_book_positions": live.get("shadow_book_positions", 0),
            # IBKR's own portfolio P&L — the authoritative source for unrealized P&L
            "ibkr_portfolio":        portfolio_items,
            "ibkr_unrealized_pnl_by_symbol": pnl_by_symbol,
            "ibkr_total_unrealized_pnl": round(sum(pnl_by_symbol.values()), 2),
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
    yf_unrealized = sum(
        float(getattr(p, "unrealized_pnl", 0) or 0) for p in open_positions
    )

    # Use IBKR portfolio P&L when available and fresh (< 10 min)
    unrealized_pnl_today = yf_unrealized
    unrealized_pnl_source = "yfinance"
    ibkr_agent = getattr(session, "_ibkr_agent", None)
    if ibkr_agent is not None:
        try:
            items, age = ibkr_agent.get_cached_portfolio()
            if age is not None and age < 600.0 and items:
                pnl_by_sym: dict[str, float] = {}
                for item in items:
                    sym = item["symbol"]
                    pnl_by_sym[sym] = pnl_by_sym.get(sym, 0.0) + item["unrealized_pnl"]
                open_syms = {p.ticker for p in open_positions}
                total = sum(v for k, v in pnl_by_sym.items() if k in open_syms)
                if total != 0.0:
                    unrealized_pnl_today = round(total, 2)
                    unrealized_pnl_source = "ibkr"
        except Exception:
            pass

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
        "closed_today":             closed_today,
        "closed_count":             len(closed_today),
        "realized_pnl_today":       round(realized_pnl_today, 2),
        "open_count":               len(open_positions),
        "unrealized_pnl_today":     round(unrealized_pnl_today, 2),
        "unrealized_pnl_source":    unrealized_pnl_source,
        "total_pnl_today":          round(realized_pnl_today + unrealized_pnl_today, 2),
        "kill_switch":              kill,
        "events":                   events,
        "timestamp":                datetime.now(_ET).isoformat(),
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


@router.get("/decisions")
async def get_decisions(limit: int = 50) -> JSONResponse:
    """Recent decision chains — trade evaluations from submission through close."""
    session = get_session()
    if session is None:
        return JSONResponse({"error": "session not ready"}, status_code=503)
    try:
        return JSONResponse({"chains": _recent_chains(str(session._settings.db_path), limit=limit)})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/kpis")
async def get_kpis() -> JSONResponse:
    """Baseline KPI snapshot — north star metrics vs targets."""
    session = get_session()
    if session is None:
        return JSONResponse({"error": "session not ready"}, status_code=503)
    kpi_path = session._settings.db_path.parent / "baseline_kpis.json"
    if not kpi_path.exists():
        return JSONResponse({"error": "baseline KPI file not found"}, status_code=404)
    try:
        import json as _json
        return JSONResponse(_json.loads(kpi_path.read_text()))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/costs")
async def get_llm_costs(date: str | None = None) -> JSONResponse:
    """LLM cost breakdown for today (or ?date=YYYY-MM-DD). Reports spend vs $15/day cap."""
    session = get_session()
    if session is None:
        return JSONResponse({"error": "session not ready"}, status_code=503)
    try:
        db_path = str(session._settings.db_path)
        return JSONResponse(_llm_daily_cost(db_path, for_date=date))
    except Exception as exc:
        logger.error("LLM cost lookup error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/health/strategy")
async def get_strategy_health() -> JSONResponse:
    """Rolling Sharpe per (pillar, regime) and currently paused cells."""
    session = get_session()
    if session is None:
        return JSONResponse({"error": "session not ready"}, status_code=503)
    try:
        if hasattr(session, "_strategy_health"):
            return JSONResponse(session._strategy_health.get_status())
        # Fallback: compute directly without the agent instance
        from ..ops.strategy_health import compute_health, get_paused_cells, MIN_TRADES, SHARPE_PAUSE_THRESH
        db_path = str(session._settings.db_path)
        return JSONResponse({
            "paused_count": len(get_paused_cells(db_path)),
            "paused_cells": list(get_paused_cells(db_path).values()),
            "health": list(compute_health(db_path).values()),
            "min_trades_required": MIN_TRADES,
            "sharpe_pause_threshold": SHARPE_PAUSE_THRESH,
        })
    except Exception as exc:
        logger.error("Strategy health lookup error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/health/scan")
async def get_scan_engine_health() -> JSONResponse:
    """Async scan engine status: queue depth, in-flight count, shadow mode, and recent metrics."""
    session = get_session()
    if session is None:
        return JSONResponse({"error": "session not ready"}, status_code=503)
    try:
        engine = getattr(session, "_scan_engine", None)
        if engine is None:
            return JSONResponse({"enabled": False, "reason": "USE_ASYNC_SCAN_ENGINE not set"})

        status = engine.get_status()
        status["enabled"] = True

        # Recent metrics from SQLite
        import sqlite3 as _sql
        db_path = str(session._settings.db_path)
        try:
            conn = _sql.connect(db_path, check_same_thread=False)
            rows = conn.execute(
                """
                SELECT priority, outcome, COUNT(*) as n,
                       ROUND(AVG(queue_wait_ms)) as avg_wait_ms,
                       MAX(queue_wait_ms) as max_wait_ms
                FROM scan_metrics
                WHERE timestamp_utc >= datetime('now', '-1 hour')
                GROUP BY priority, outcome
                ORDER BY priority, outcome
                """
            ).fetchall()
            conn.close()
            status["metrics_last_hour"] = [
                {"priority": r[0], "outcome": r[1], "count": r[2],
                 "avg_wait_ms": r[3], "max_wait_ms": r[4]}
                for r in rows
            ]
        except Exception:
            status["metrics_last_hour"] = []

        return JSONResponse(status)
    except Exception as exc:
        logger.error("Scan engine health error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/health/analyst")
async def get_analyst_health() -> JSONResponse:
    """
    Analyst thesis performance: direction hit rate, calibration rate, cost, recent theses.
    Triggers an on-demand attribution pass before returning stats.
    """
    session = get_session()
    if session is None:
        return JSONResponse({"error": "session not ready"}, status_code=503)
    try:
        db_path = str(session._settings.db_path)
        # Run attribution first so stats are fresh
        attribution_result = _attribute_now(db_path)
        stats = _get_analyst_stats(db_path)
        stats["attribution_this_call"] = attribution_result
        return JSONResponse(stats)
    except Exception as exc:
        logger.error("Analyst health error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/health/promotion-readiness")
async def get_promotion_readiness() -> JSONResponse:
    """
    Shadow → live promotion readiness for all intelligence agents.

    Returns per-agent metrics vs promotion thresholds (spec §1.2):
      analyst:            ≥40 attributed, direction_hit_rate ≥55%
      advocate:           ≥30 attributed, precision ≥60%, recall ≥50%
      strategy_selector:  ≥40 attributed, win_rate ≥55%
      exit_intelligence:  ≥20 attributed, avg_exit_alpha > 5%

    Also runs a fresh attribution pass so stats reflect the latest closes.
    Status field: READY | NOT_READY | INSUFFICIENT_DATA
    """
    session = get_session()
    if session is None:
        return JSONResponse({"error": "session not ready"}, status_code=503)
    try:
        db_path = str(session._settings.db_path)
        # Fresh attribution pass first
        attr_result = _attribute_now(db_path)
        readiness = _get_promotion_readiness(db_path)
        readiness["attribution_this_call"] = attr_result
        # Annotate current shadow_mode status for each agent
        readiness["current_shadow_modes"] = {
            "analyst":            getattr(session._stock_analyst, "_shadow_mode", None)
                                  if session._stock_analyst else "disabled",
            "strategy_selector":  getattr(session._strategy_selector, "_shadow_mode", None)
                                  if session._strategy_selector else "disabled",
            "advocate":           getattr(session._advocate, "_shadow_mode", None)
                                  if session._advocate else "disabled",
            "exit_intelligence":  getattr(session._exit_agent, "_shadow_mode", None)
                                  if session._exit_agent else "disabled",
        }
        return JSONResponse(readiness)
    except Exception as exc:
        logger.error("Promotion readiness error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/lessons/pending")
async def get_pending_lessons() -> JSONResponse:
    """
    Agent lessons awaiting human approval.
    Only approved lessons (human_approved=1) are ever used by agents — §17 Sacred Rule.
    """
    session = get_session()
    if session is None:
        return JSONResponse({"error": "session not ready"}, status_code=503)
    try:
        import sqlite3
        db_path = str(session._settings.db_path)
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                """SELECT lesson_id, agent_name, lesson_text, confidence_in_lesson,
                          sample_size, created_at_utc, times_reinforced
                   FROM agent_lessons
                   WHERE human_approved = 0 AND active = 1
                   ORDER BY created_at_utc DESC"""
            ).fetchall()
        lessons = [
            {
                "lesson_id":           r[0],
                "agent_name":          r[1],
                "lesson_text":         r[2],
                "confidence_in_lesson": r[3],
                "sample_size":         r[4],
                "created_at_utc":      r[5],
                "times_reinforced":    r[6],
            }
            for r in rows
        ]
        return JSONResponse({"pending_count": len(lessons), "lessons": lessons})
    except Exception as exc:
        logger.error("Lessons pending error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.post("/lessons/{lesson_id}/approve")
async def approve_lesson(lesson_id: int, request: Request) -> JSONResponse:
    """
    CEO approves a pending lesson — it becomes available to agents on next call.
    Body: {"approved_by": "CEO"}
    """
    session = get_session()
    if session is None:
        return JSONResponse({"error": "session not ready"}, status_code=503)
    try:
        body = await request.json()
        approved_by = body.get("approved_by", "")
        if not approved_by:
            return JSONResponse({"error": "approved_by required"}, status_code=400)
        import sqlite3
        from datetime import datetime, timezone
        db_path = str(session._settings.db_path)
        with sqlite3.connect(db_path) as conn:
            rowcount = conn.execute(
                """UPDATE agent_lessons
                   SET human_approved = 1, approved_at_utc = ?, approved_by = ?
                   WHERE lesson_id = ? AND active = 1""",
                (datetime.now(tz=timezone.utc).isoformat(), approved_by, lesson_id),
            ).rowcount
        if rowcount == 0:
            return JSONResponse({"error": f"lesson {lesson_id} not found or already inactive"}, status_code=404)
        logger.info("Lesson %d approved by %s", lesson_id, approved_by)
        return JSONResponse({"approved": True, "lesson_id": lesson_id, "approved_by": approved_by})
    except Exception as exc:
        logger.error("Approve lesson error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.post("/lessons/{lesson_id}/reject")
async def reject_lesson(lesson_id: int, request: Request) -> JSONResponse:
    """
    Reject a lesson — marks it inactive so it is never surfaced again.
    Body: {"reason": "..."}
    """
    session = get_session()
    if session is None:
        return JSONResponse({"error": "session not ready"}, status_code=503)
    try:
        body = await request.json()
        reason = body.get("reason", "")
        import sqlite3
        from datetime import datetime, timezone
        db_path = str(session._settings.db_path)
        with sqlite3.connect(db_path) as conn:
            rowcount = conn.execute(
                """UPDATE agent_lessons
                   SET active = 0, rejected_at_utc = ?, rejected_reason = ?
                   WHERE lesson_id = ?""",
                (datetime.now(tz=timezone.utc).isoformat(), reason, lesson_id),
            ).rowcount
        if rowcount == 0:
            return JSONResponse({"error": f"lesson {lesson_id} not found"}, status_code=404)
        logger.info("Lesson %d rejected: %s", lesson_id, reason)
        return JSONResponse({"rejected": True, "lesson_id": lesson_id})
    except Exception as exc:
        logger.error("Reject lesson error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/journal/strategy")
async def get_strategy_journal(limit: int = 50) -> JSONResponse:
    """
    StrategySelectorAgent decision log. Shows selector decisions vs rules engine,
    plus shadow_mode flag so you can see live vs advisory rows.
    """
    session = get_session()
    if session is None:
        return JSONResponse({"error": "session not ready"}, status_code=503)
    try:
        import sqlite3
        db_path = str(session._settings.db_path)
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                """SELECT decision_id, ticker, decided_at_utc, decision,
                          strategy_type, expiry_preference, contracts,
                          endorses_rules, liquidity_score, thesis_alignment_score,
                          rationale, shadow_mode,
                          input_tokens, output_tokens, cost_usd, latency_ms
                   FROM strategy_journal
                   ORDER BY decided_at_utc DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
        cols = ["decision_id", "ticker", "decided_at_utc", "decision",
                "strategy_type", "expiry_preference", "contracts",
                "endorses_rules", "liquidity_score", "thesis_alignment_score",
                "rationale", "shadow_mode",
                "input_tokens", "output_tokens", "cost_usd", "latency_ms"]
        return JSONResponse({"count": len(rows), "rows": [dict(zip(cols, r)) for r in rows]})
    except Exception as exc:
        logger.error("Strategy journal error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/journal/advocate")
async def get_advocate_journal(limit: int = 50) -> JSONResponse:
    """
    AdvocateAgent adversarial review log. Shows verdicts (PASS/CAUTION/BLOCK),
    failure modes, and shadow_mode so you can audit pre-promotion performance.
    """
    session = get_session()
    if session is None:
        return JSONResponse({"error": "session not ready"}, status_code=503)
    try:
        import sqlite3, json
        db_path = str(session._settings.db_path)
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                """SELECT decision_id, ticker, decided_at_utc, verdict,
                          verdict_confidence, failure_modes_json,
                          most_likely_scenario, shadow_mode,
                          input_tokens, output_tokens, cost_usd, latency_ms
                   FROM advocate_journal
                   ORDER BY decided_at_utc DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
        result = []
        for r in rows:
            result.append({
                "decision_id":        r[0],
                "ticker":             r[1],
                "decided_at_utc":     r[2],
                "verdict":            r[3],
                "verdict_confidence":  r[4],
                "failure_modes":      json.loads(r[5] or "[]"),
                "most_likely_scenario": r[6],
                "shadow_mode":        bool(r[7]),
                "input_tokens":       r[8],
                "output_tokens":      r[9],
                "cost_usd":           r[10],
                "latency_ms":         r[11],
            })
        return JSONResponse({"count": len(result), "rows": result})
    except Exception as exc:
        logger.error("Advocate journal error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/journal/exit")
async def get_exit_journal(limit: int = 50, ticker: str | None = None) -> JSONResponse:
    """
    ExitIntelligenceAgent hourly position-monitoring log. Filter by ticker.
    Shows thesis_validity, recommendation, kill_condition_status, and P&L context.
    """
    session = get_session()
    if session is None:
        return JSONResponse({"error": "session not ready"}, status_code=503)
    try:
        import sqlite3, json
        db_path = str(session._settings.db_path)
        with sqlite3.connect(db_path) as conn:
            if ticker:
                rows = conn.execute(
                    """SELECT decision_id, position_id, ticker, decided_at_utc,
                              thesis_validity, kill_condition_status, recommendation,
                              recommendation_reasoning, confidence_pct, shadow_mode,
                              input_tokens, output_tokens, cost_usd, latency_ms
                       FROM exit_journal
                       WHERE ticker = ?
                       ORDER BY decided_at_utc DESC
                       LIMIT ?""",
                    (ticker.upper(), limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT decision_id, position_id, ticker, decided_at_utc,
                              thesis_validity, kill_condition_status, recommendation,
                              recommendation_reasoning, confidence_pct, shadow_mode,
                              input_tokens, output_tokens, cost_usd, latency_ms
                       FROM exit_journal
                       ORDER BY decided_at_utc DESC
                       LIMIT ?""",
                    (limit,),
                ).fetchall()
        cols = ["decision_id", "position_id", "ticker", "decided_at_utc",
                "thesis_validity", "kill_condition_status", "recommendation",
                "recommendation_reasoning", "confidence_pct", "shadow_mode",
                "input_tokens", "output_tokens", "cost_usd", "latency_ms"]
        result = [dict(zip(cols, r)) for r in rows]
        for r in result:
            r["shadow_mode"] = bool(r["shadow_mode"])
        return JSONResponse({"count": len(result), "rows": result})
    except Exception as exc:
        logger.error("Exit journal error: %s", exc)
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


@router.get("/swing/decisions")
async def get_swing_decisions(limit: int = 30) -> JSONResponse:
    """
    GET /agora/swing/decisions
    Recent swing go/no-go decisions (both directions), most recent first.
    Includes open positions, passed trades, and no-go passes.
    """
    session = get_session()
    if not session:
        return JSONResponse({"error": "session not running"}, status_code=503)
    try:
        journal = session._swing_journal
        open_pos = journal.get_all_open()
        no_go = journal.get_no_go_summary(limit=limit)
        perf = journal.get_performance_summary()
        return JSONResponse({
            "open_positions": open_pos,
            "recent_no_go": no_go,
            "performance": perf,
        })
    except Exception as exc:
        logger.error("Swing decisions endpoint error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/swing/open")
async def get_swing_open() -> JSONResponse:
    """
    GET /agora/swing/open
    All open (filled) swing positions with current P&L context.
    """
    session = get_session()
    if not session:
        return JSONResponse({"error": "session not running"}, status_code=503)
    try:
        return JSONResponse({"positions": session._swing_journal.get_all_open()})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/swing/performance")
async def get_swing_performance() -> JSONResponse:
    """
    GET /agora/swing/performance
    Win rate, avg P&L, total P&L, avg prediction accuracy across all closed swings.
    """
    session = get_session()
    if not session:
        return JSONResponse({"error": "session not running"}, status_code=503)
    try:
        return JSONResponse(session._swing_journal.get_performance_summary())
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


class _SwingAuditBody(BaseModel):
    journal_id: int


@router.post("/swing/audit")
async def trigger_swing_audit(body: _SwingAuditBody) -> JSONResponse:
    """
    POST /agora/swing/audit
    Body: {"journal_id": 42}
    Manually trigger a post-trade self-audit for a closed swing position.
    Normally called automatically when record_close() is invoked.
    """
    session = get_session()
    if not session:
        return JSONResponse({"error": "session not running"}, status_code=503)
    try:
        audit = await session._swing_journal.trigger_self_audit(body.journal_id)
        return JSONResponse({"audit": audit, "journal_id": body.journal_id})
    except Exception as exc:
        logger.error("Swing audit endpoint error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/calibration")
async def get_calibration() -> JSONResponse:
    """
    GET /agora/calibration
    Latest ConvictionCalibrator output: per-pillar Sharpe, regime analysis,
    conviction quintiles, and proposed weight changes. Runs weekly.
    """
    session = get_session()
    if not session:
        return JSONResponse({"error": "session not running"}, status_code=503)
    try:
        import json
        from pathlib import Path
        cal_path = session._settings.db_path.parent / "calibration_report.json"
        if not cal_path.exists():
            return JSONResponse({"status": "not_generated_yet",
                                 "message": "Calibration runs weekly. No report yet."})
        return JSONResponse(json.loads(cal_path.read_text()))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.post("/calibration/run")
async def run_calibration_now() -> JSONResponse:
    """POST /agora/calibration/run — trigger an immediate calibration pass."""
    session = get_session()
    if not session:
        return JSONResponse({"error": "session not running"}, status_code=503)
    try:
        from agora.ops.conviction_calibrator import calibrate
        from pathlib import Path
        cal_path = session._settings.db_path.parent / "calibration_report.json"
        cal_path.parent.mkdir(parents=True, exist_ok=True)
        result = calibrate(str(session._settings.db_path), str(cal_path))
        return JSONResponse({"status": "ok", "total_closed_trades": result.get("total_closed_trades", 0)})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


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
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        _ws_clients.discard(ws)

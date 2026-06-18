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
import logging
from collections import deque
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..ops.decision_chains import recent_chains as _recent_chains
from ..ops.llm_cost_log import daily_cost_summary as _llm_daily_cost
from ..ops.outcome_attributor import (
    attribute_closed_trades as _attribute_now,
)
from ..ops.outcome_attributor import (
    get_analyst_stats as _get_analyst_stats,
)
from ..ops.outcome_attributor import (
    get_promotion_readiness as _get_promotion_readiness,
)
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
    # Snapshot the client set: send_json() awaits, so a client connecting or
    # disconnecting mid-broadcast would otherwise mutate _ws_clients during
    # iteration ("Set changed size during iteration").
    for ws in list(_ws_clients):
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

    # Batch-fetch precise entry timestamps (seconds-granularity UTC) keyed by position_id — the
    # OpenPosition object doesn't carry them, so read straight from the DB once for all open rows.
    _entry_ts: dict[str, str] = {}
    try:
        import sqlite3 as _sql3
        _c = _sql3.connect(str(session._settings.db_path))
        _c.row_factory = _sql3.Row
        for _r in _c.execute("SELECT position_id, entry_ts_utc FROM positions WHERE status NOT IN ('closed')"):
            if _r["entry_ts_utc"]:
                _entry_ts[_r["position_id"]] = _r["entry_ts_utc"]
        _c.close()
    except Exception:
        pass

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
            "entry_ts_utc":        _entry_ts.get(pos.position_id),
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
        max(0, n_attempts - n_fills - n_rejects)

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


@router.get("/performance")
async def get_performance() -> JSONResponse:
    """
    Capital-deployed + P&L rollups (daily / weekly / monthly) and the confidence
    stats that tell you whether the book is genuinely working:

      - deployed   = premium paid on DEBIT entries (real money out), grouped by entry_date
      - realized   = realized P&L from CLOSED trades, grouped by close_date
      - headline   = win rate, avg win/loss, win:loss ratio, expectancy, profit factor
      - by_strategy= the same, split per strategy

    'reset' rows are admin position-clears (not real outcomes) and are EXCLUDED from
    all performance math. Credit-spread entries (negative cost) are excluded from
    'deployed' since they collect premium rather than deploy capital.
    """
    session = get_session()
    if session is None:
        return JSONResponse({"error": "session not ready"}, status_code=503)

    import sqlite3 as _sql
    from datetime import datetime as _dt

    db_path = session._settings.db_path
    try:
        conn = _sql.connect(str(db_path), check_same_thread=False)
        rows = conn.execute(
            "SELECT strategy, status, contracts, entry_price, entry_date, "
            "close_date, realized_pnl, close_source FROM positions"
        ).fetchall()
        conn.close()
    except Exception as exc:
        return JSONResponse({"error": f"db read failed: {exc}"}, status_code=500)

    # Column indices
    STRAT, STATUS, QTY, ENTRY_PX, ENTRY_D, CLOSE_D, RPNL, CLOSE_SRC = range(8)

    _REAL_SRC = {"lifecycle", "thesis_exit", "trailing_stop", "stop_loss", "pre_earnings"}

    def _is_real_close(r) -> bool:
        """Real broker fill only — excludes fabricated_unfilled / tws_startup_sync / reconcile /
        reset so the headline win-rate/expectancy can NEVER show model-mark fiction."""
        if (r[STATUS] or "") != "closed" or r[RPNL] is None:
            return False
        src = (r[CLOSE_SRC] or "")
        if any(k in src for k in ("fabricated", "sync", "reconcile", "duplicate")):
            return False
        return src in _REAL_SRC or src.startswith("session:")

    def _cost(r) -> float:
        return float(r[ENTRY_PX] or 0) * 100 * int(r[QTY] or 1)

    def _day(s):   return (s or "")[:10] or None
    def _month(s): return (s or "")[:7] or None
    def _week(s):
        d = _day(s)
        if not d:
            return None
        try:
            y, w, _ = _dt.fromisoformat(d).isocalendar()
            return f"{y}-W{w:02d}"
        except Exception:
            return None

    # Genuine trades only (drop admin resets)
    genuine = [r for r in rows if (r[STATUS] or "") != "reset"]
    # Headline win-rate / avg-win/loss / expectancy compute over REAL fills only — never the
    # fabricated/sync closes that produced the fictional +$12,740 the books used to show.
    closed  = [r for r in genuine if _is_real_close(r)]

    # ── Period rollups: deployed by entry_date, realized by close_date ──────────
    def _rollup(keyfn):
        acc: dict = {}
        for r in genuine:
            ek = keyfn(r[ENTRY_D])
            c  = _cost(r)
            if ek and c > 0:                       # debit entries only = capital deployed
                a = acc.setdefault(ek, _blank())
                a["deployed"] += c
                a["entries"]  += 1
        for r in closed:
            ck = keyfn(r[CLOSE_D])
            if ck:
                a = acc.setdefault(ck, _blank())
                a["realized"] += float(r[RPNL] or 0)
                a["closes"]   += 1
                if float(r[RPNL] or 0) > 0:
                    a["wins"] += 1
        out = []
        for k in sorted(acc.keys(), reverse=True):
            a = acc[k]
            a["period"]   = k
            a["deployed"] = round(a["deployed"], 2)
            a["realized"] = round(a["realized"], 2)
            a["win_rate"] = round(100.0 * a["wins"] / a["closes"], 1) if a["closes"] else None
            out.append(a)
        return out

    # ── Headline confidence stats (closed only) ─────────────────────────────────
    wins   = [float(r[RPNL]) for r in closed if float(r[RPNL] or 0) > 0]
    losses = [float(r[RPNL]) for r in closed if float(r[RPNL] or 0) <= 0]
    gross_win  = sum(wins)
    gross_loss = abs(sum(losses))
    n_closed   = len(closed)
    avg_win    = round(gross_win / len(wins), 2) if wins else 0.0
    avg_loss   = round(sum(losses) / len(losses), 2) if losses else 0.0
    win_rate   = round(100.0 * len(wins) / n_closed, 1) if n_closed else None
    expectancy = round(sum(float(r[RPNL]) for r in closed) / n_closed, 2) if n_closed else 0.0
    profit_factor = round(gross_win / gross_loss, 2) if gross_loss else None
    wl_ratio   = round(avg_win / abs(avg_loss), 2) if avg_loss else None
    total_deployed = round(sum(_cost(r) for r in genuine if _cost(r) > 0), 2)
    net_realized   = round(sum(float(r[RPNL] or 0) for r in closed), 2)

    # ── By strategy ─────────────────────────────────────────────────────────────
    strat: dict = {}
    for r in genuine:
        s = r[STRAT] or "?"
        a = strat.setdefault(s, {"trades": 0, "deployed": 0.0, "realized": 0.0,
                                 "wins": 0, "closes": 0})
        a["trades"] += 1
        if _cost(r) > 0:
            a["deployed"] += _cost(r)
        if _is_real_close(r):
            a["closes"] += 1
            a["realized"] += float(r[RPNL] or 0)
            if float(r[RPNL] or 0) > 0:
                a["wins"] += 1
    by_strategy = []
    for s, a in sorted(strat.items(), key=lambda kv: kv[1]["realized"], reverse=True):
        by_strategy.append({
            "strategy": s, "trades": a["trades"],
            "deployed": round(a["deployed"], 2), "realized": round(a["realized"], 2),
            "win_rate": round(100.0 * a["wins"] / a["closes"], 1) if a["closes"] else None,
        })

    return JSONResponse({
        "headline": {
            "total_trades_closed": n_closed,
            "open_positions":      sum(1 for r in genuine if (r[STATUS] or "") == "open"),
            "reset_excluded":      sum(1 for r in rows if (r[STATUS] or "") == "reset"),
            "total_deployed":      total_deployed,
            "net_realized":        net_realized,
            "win_rate":            win_rate,
            "wins":                len(wins),
            "losses":              len(losses),
            "avg_win":             avg_win,
            "avg_loss":            avg_loss,
            "wl_ratio":            wl_ratio,
            "expectancy":          expectancy,
            "profit_factor":       profit_factor,
            "best":                round(max((float(r[RPNL]) for r in closed), default=0.0), 2),
            "worst":               round(min((float(r[RPNL]) for r in closed), default=0.0), 2),
        },
        "daily":       _rollup(_day)[:30],
        "weekly":      _rollup(_week)[:12],
        "monthly":     _rollup(_month)[:12],
        "by_strategy": by_strategy,
        "timestamp":   datetime.now(_ET).isoformat(),
    })


def _blank() -> dict:
    return {"deployed": 0.0, "realized": 0.0, "entries": 0, "closes": 0, "wins": 0}


@router.get("/expectancy")
async def get_expectancy() -> JSONResponse:
    """The North-Star expectancy meter: current post-fix expectancy ($/trade) vs the date-locked
    target, with progress, per-period buckets, and per-day/week/month projection. Read-only."""
    session = get_session()
    s = session._settings
    from agora.ops.expectancy_meter import build_meter
    meter = build_meter(
        str(s.db_path),
        target_per_trade=getattr(s, "expectancy_target_per_trade", 25.0),
        target_date=getattr(s, "expectancy_target_date", "2026-09-30"),
        legacy_cutoff=getattr(s, "expectancy_legacy_cutoff_date", "2026-06-12"),
    )
    return JSONResponse(meter)


@router.get("/exit-regret")
async def get_exit_regret() -> JSONResponse:
    """S0.2 post-close counterfactual: regret aggregated by exit reason — which exit type cuts
    winners short (high early_exit_rate) vs protects the book (high correct_exit_rate). Read-only."""
    session = get_session()
    from agora.ops.post_close_watch import exit_regret_report
    return JSONResponse(exit_regret_report(str(session._settings.db_path)))


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


@router.get("/reconcile")
async def reconcile_positions() -> JSONResponse:
    """Leg-level reconciliation of the DB shadow book against the live IBKR account.

    Read-only. Surfaces orphan legs (at IBKR, untracked in the DB), ghost legs (in the
    DB but absent at IBKR — e.g. a spread's protective long leg that never filled, leaving
    a naked short), and quantity mismatches. The old DB-vs-DB health check could not see
    any of these.
    """
    session = get_session()
    s = session._settings
    db_path = str(s.db_path)
    host = getattr(s, "ibkr_host", "127.0.0.1")
    port = int(getattr(s, "ibkr_port", 7497))

    def _run() -> dict:
        import asyncio as _a

        from agora.ops.position_reconciler import reconcile
        _a.set_event_loop(_a.new_event_loop())  # ib_insync needs a loop in this worker thread
        # Dedicated clientId so we never collide with the trading session's connections.
        return reconcile(db_path, host, port, client_id=71).to_dict()

    try:
        report = await asyncio.to_thread(_run)
    except Exception as exc:
        logger.warning("Reconcile failed: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=503)
    if not report.get("clean"):
        logger.warning("Position reconciliation DIVERGENCE: %s", report.get("counts"))
    return JSONResponse(report)


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
                      entry_price, close_price, realized_pnl, close_source,
                      entry_date, close_date, entry_ts_utc, exit_ts_utc
               FROM positions
               WHERE close_date = ?
               ORDER BY exit_ts_utc DESC, rowid DESC""",
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
                "entry_date":   r[8] or "",
                "close_date":   r[9] or "",
                "entry_ts_utc": r[10] or "",
                "exit_ts_utc":  r[11] or "",
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
        summary = _llm_daily_cost(db_path, for_date=date)
        # 7-day daily totals for a trend sparkline on the dashboard.
        try:
            import sqlite3
            with sqlite3.connect(db_path, timeout=5) as conn:
                rows = conn.execute(
                    """SELECT date, ROUND(SUM(cost_usd),2), COUNT(*)
                       FROM llm_cost_log
                       WHERE date >= date('now','-6 days')
                       GROUP BY date ORDER BY date""",
                ).fetchall()
            summary["trend"] = [{"date": r[0], "usd": r[1] or 0.0, "calls": r[2]} for r in rows]
        except Exception:
            summary["trend"] = []
        return JSONResponse(summary)
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
        from ..ops.strategy_health import (
            MIN_TRADES,
            SHARPE_PAUSE_THRESH,
            compute_health,
            get_paused_cells,
        )
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


@router.get("/health/performance")
async def get_performance_metrics() -> JSONResponse:
    """The heartbeat — precise money-management metrics over REAL fills: Avg Win/Loss, Win/Loss
    rate, Expectancy, Profit factor, Profitability (all-time + rolling), the book↔DB reconciliation
    proof, and honest daily achievements. This is the single trustworthy performance surface."""
    session = get_session()
    if session is None:
        return JSONResponse({"error": "session not ready"}, status_code=503)
    try:
        from agora.ops.performance_metrics import (
            compute_achievements,
            compute_metrics,
            reconcile_books,
        )
        db = str(session._settings.db_path)
        return JSONResponse({
            "metrics": compute_metrics(db),
            "reconciliation": reconcile_books(db),
            "achievements": compute_achievements(db),
        })
    except Exception as exc:
        logger.error("Performance metrics error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/health/edge")
async def get_edge() -> JSONResponse:
    """Where is the edge? Read-only realized-expectancy cuts (pillar/strategy/regime/direction/
    close_source) over REAL fills only, with Wilson lower bounds + min-sample gates, plus the
    hold-day churn histogram. Pure observability — makes no trading decision."""
    session = get_session()
    if session is None:
        return JSONResponse({"error": "session not ready"}, status_code=503)
    try:
        from agora.ops.edge_dashboard import compute_edge
        return JSONResponse(compute_edge(str(session._settings.db_path)))
    except Exception as exc:
        logger.error("Edge dashboard error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/trades")
async def get_trades(limit: int = 100) -> JSONResponse:
    """All trades with their EXACT TWS fill timestamps (entry + exit), to the second — sourced
    from f.execution.time so the DB matches TWS by construction. Shows precise hold duration and a
    reconciliation summary (how many trades carry the precise broker timestamp vs date-only legacy)."""
    session = get_session()
    if session is None:
        return JSONResponse({"error": "session not ready"}, status_code=503)
    try:
        import sqlite3
        from datetime import datetime
        conn = sqlite3.connect(str(session._settings.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT position_id, ticker, strategy, status, contracts, entry_price, close_price,
                      realized_pnl, entry_date, close_date, entry_ts_utc, exit_ts_utc, close_source
               FROM positions ORDER BY rowid DESC LIMIT ?""", (limit,)).fetchall()
        conn.close()

        def _hold_secs(e, x):
            try:
                return round((datetime.fromisoformat(x) - datetime.fromisoformat(e)).total_seconds(), 1)
            except Exception:
                return None

        trades, with_e, with_x = [], 0, 0
        for r in rows:
            d = dict(r)
            e_ts, x_ts = d.get("entry_ts_utc"), d.get("exit_ts_utc")
            if e_ts:
                with_e += 1
            if x_ts:
                with_x += 1
            trades.append({
                "ticker": d["ticker"], "strategy": d["strategy"], "status": d["status"],
                "contracts": d["contracts"], "realized_pnl": d["realized_pnl"],
                "entry_ts_utc": e_ts, "exit_ts_utc": x_ts,
                "entry_date": d["entry_date"], "close_date": d["close_date"],
                "hold_seconds": _hold_secs(e_ts, x_ts) if (e_ts and x_ts) else None,
                "close_source": d["close_source"],
                "precise": bool(e_ts) and (d["status"] != "closed" or bool(x_ts)),
            })
        n = len(trades)
        closed = sum(1 for t in trades if t["status"] == "closed")
        return JSONResponse({
            "trades": trades,
            "reconciliation": {
                "total": n, "closed": closed,
                "with_precise_entry_ts": with_e, "with_precise_exit_ts": with_x,
                "entry_coverage_pct": round(100 * with_e / max(n, 1), 1),
                "exit_coverage_pct": round(100 * with_x / max(closed, 1), 1),
                "note": ("Timestamps are the exact TWS execution time (f.execution.time) — DB matches "
                         "TWS to the second. Legacy rows (pre-2026-06-17) carry date only."),
            },
        })
    except Exception as exc:
        logger.error("get_trades error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.post("/health/fill-canary")
async def run_fill_canary_endpoint() -> JSONResponse:
    """Broker fill-engine canary: place + flatten a 1-lot maximally-marketable SPY ATM call and
    report whether it filled. Isolates the IBKR paper-account fill engine from our pricing/walk —
    a marketable order that fills means the low strategy fill rate is upstream (us), not the broker.
    Places a REAL (paper) round-trip order, so it's POST + on-demand, never auto-run."""
    session = get_session()
    if session is None:
        return JSONResponse({"error": "session not ready"}, status_code=503)
    try:
        from agora.ops.fill_canary import run_fill_canary
        s = session._settings
        res = await run_fill_canary(
            host=s.ibkr_host, port=s.ibkr_port, client_id=99,
            market_data_type=getattr(s, "ibkr_market_data_type", 1),
            db_path=str(s.db_path),
        )
        return JSONResponse(res)
    except Exception as exc:
        logger.error("fill-canary error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/health/defender")
async def get_defender_precision() -> JSONResponse:
    """Thesis Defender's measured override precision over REAL fills, lifetime: of the BLOCKs it
    moderated to CAUTION (strong defense + conf>=0.65), how many of the let-through trades actually
    won? The metric that decides whether the defender earns its keep. Pure observability."""
    session = get_session()
    if session is None:
        return JSONResponse({"error": "session not ready"}, status_code=503)
    try:
        from agora.ops.defender_metrics import defender_override_precision
        return JSONResponse(defender_override_precision(str(session._settings.db_path)))
    except Exception as exc:
        logger.error("Defender precision error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/health/promotion-readiness")
async def get_promotion_readiness() -> JSONResponse:
    """
    Shadow → live promotion readiness for all intelligence agents.

    Returns per-agent metrics vs promotion thresholds (spec §1.2):
      analyst:            ≥40 attributed, direction_hit_rate ≥55%
      advocate:           ≥30 attributed, precision ≥60%, recall ≥50%
      strategy_selector:  ≥40 attributed, win_rate ≥55%
      exit_intelligence:  ≥20 attributed, decision_accuracy ≥60%

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
        from datetime import datetime
        db_path = str(session._settings.db_path)
        with sqlite3.connect(db_path) as conn:
            rowcount = conn.execute(
                """UPDATE agent_lessons
                   SET human_approved = 1, approved_at_utc = ?, approved_by = ?
                   WHERE lesson_id = ? AND active = 1""",
                (datetime.now(tz=UTC).isoformat(), approved_by, lesson_id),
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
        from datetime import datetime
        db_path = str(session._settings.db_path)
        with sqlite3.connect(db_path) as conn:
            rowcount = conn.execute(
                """UPDATE agent_lessons
                   SET active = 0, rejected_at_utc = ?, rejected_reason = ?
                   WHERE lesson_id = ?""",
                (datetime.now(tz=UTC).isoformat(), reason, lesson_id),
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
        return JSONResponse({"count": len(rows), "rows": [dict(zip(cols, r, strict=False)) for r in rows]})
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
        import json
        import sqlite3
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
        import sqlite3
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
        result = [dict(zip(cols, r, strict=False)) for r in rows]
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
        return JSONResponse([dict(zip(cols, r, strict=False)) for r in rows])
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


@router.get("/long/status")
async def get_long_status(limit: int = 25) -> JSONResponse:
    """
    GET /agora/long/status
    Crystal-clear view of the long-options book — the system's sole directional path.
      - open: live long_call/long_put positions with P&L
      - signal_calibration: per-signal win-rate / P&L (the deterministic learning loop,
        which auto-weights sizing) — sorted worst-to-best so problem signals lead
      - recent: last N long-options decisions (proceed/skip/block) from long_journal
    """
    session = get_session()
    if session is None:
        return JSONResponse({"error": "session not running"}, status_code=503)
    try:
        import sqlite3
        db = str(session._settings.db_path)

        # Open long positions
        open_long = []
        for p in session._position_mgr.get_open_positions():
            strat = str(getattr(p.strategy, "value", p.strategy))
            if strat in ("long_call", "long_put"):
                open_long.append({
                    "ticker": p.ticker, "strategy": strat,
                    "contracts": p.contracts, "entry_price": p.entry_price,
                    "unrealized_pnl": round(p.unrealized_pnl or 0.0, 2),
                    "max_loss": p.max_loss_dollars, "max_gain": p.max_gain_dollars,
                })

        # Signal calibration (learning loop made visible) — worst win-rate first
        calibration = []
        try:
            with sqlite3.connect(db, timeout=5) as conn:
                rows = conn.execute(
                    """SELECT signal_name, direction, total_trades, wins, losses,
                              win_rate, avg_pnl, total_pnl
                       FROM signal_stats ORDER BY win_rate ASC, total_trades DESC"""
                ).fetchall()
            calibration = [{
                "signal": r[0], "direction": r[1], "trades": r[2],
                "wins": r[3], "losses": r[4], "win_rate": round(r[5] or 0.0, 3),
                "avg_pnl": round(r[6] or 0.0, 2), "total_pnl": round(r[7] or 0.0, 2),
            } for r in rows]
        except Exception:
            pass

        # Recent decisions
        recent = []
        try:
            with sqlite3.connect(db, timeout=5) as conn:
                rows = conn.execute(
                    """SELECT decided_at_utc, ticker, strategy, direction, conviction_score,
                              outcome, block_reason, contracts, strike, dte
                       FROM long_journal ORDER BY journal_id DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
            recent = [{
                "ts": r[0], "ticker": r[1], "strategy": r[2], "direction": r[3],
                "conviction": r[4], "outcome": r[5], "block_reason": r[6],
                "contracts": r[7], "strike": r[8], "dte": r[9],
            } for r in rows]
        except Exception:
            pass

        return JSONResponse({
            "open": open_long,
            "open_count": len(open_long),
            "signal_calibration": calibration,
            "recent": recent,
        })
    except Exception as exc:
        logger.error("long/status endpoint error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/exits")
async def get_exit_quality(days: int = 30) -> JSONResponse:
    """
    GET /agora/exits — exit-quality attribution.
    Realized P&L + win-rate grouped by close_source (time_stop / profit_target /
    trailing_stop / stop_loss / thesis_exit / pre_earnings / 21-DTE / scale_out / ...),
    so you can see empirically which exit types make money and tune from data.
    """
    session = get_session()
    if session is None:
        return JSONResponse({"error": "session not running"}, status_code=503)
    try:
        import sqlite3
        db = str(session._settings.db_path)
        with sqlite3.connect(db, timeout=5) as conn:
            # Exclude non-decision close sources (broker reconciliation / startup sync) —
            # they aren't exit decisions and pollute the exit-quality attribution.
            rows = conn.execute(
                """SELECT COALESCE(close_source,'unknown') src, COUNT(*) n,
                          SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) wins,
                          ROUND(SUM(realized_pnl),2) total, ROUND(AVG(realized_pnl),2) avg,
                          ROUND(MIN(realized_pnl),2) worst, ROUND(MAX(realized_pnl),2) best
                   FROM positions
                   WHERE status='closed' AND close_date >= date('now', ?)
                     AND COALESCE(close_source,'') NOT IN ('tws_startup_sync','reconcile','startup_sync')
                   GROUP BY close_source ORDER BY total DESC""",
                (f"-{int(days)} days",),
            ).fetchall()
        by_exit = [{
            "exit_type": r[0], "trades": r[1], "wins": r[2],
            "win_rate": round(r[2] / r[1], 3) if r[1] else 0.0,
            "total_pnl": r[3] or 0.0, "avg_pnl": r[4] or 0.0,
            "worst": r[5] or 0.0, "best": r[6] or 0.0,
        } for r in rows]
        totals = {
            "trades": sum(e["trades"] for e in by_exit),
            "total_pnl": round(sum(e["total_pnl"] for e in by_exit), 2),
            "win_rate": round(
                sum(e["wins"] for e in by_exit) / max(1, sum(e["trades"] for e in by_exit)), 3),
        }
        return JSONResponse({"days": days, "totals": totals, "by_exit_type": by_exit})
    except Exception as exc:
        logger.error("exits endpoint error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/yf-cache")
async def get_yf_cache_stats() -> JSONResponse:
    """GET /agora/yf-cache — yfinance gate cache hit-rates + rate-limit retry counts.
    Confirms the throttle+cache is relieving free-tier saturation."""
    try:
        from agora.ops.yf_gate import stats
        return JSONResponse(stats())
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/fact-divergence")
async def get_fact_divergence(days: int = 7) -> JSONResponse:
    """
    GET /agora/fact-divergence — the fact-grounding monitor's log.
    Times an agent's stated reasoning diverged from deterministic ground truth (e.g. cited
    an event as imminent when the calendar disagrees). The systemic safety net from the
    C-suite post-mortem: catches confidently-wrong agent claims that metrics miss.
    """
    session = get_session()
    if session is None:
        return JSONResponse({"error": "session not running"}, status_code=503)
    try:
        import sqlite3
        db = str(session._settings.db_path)
        with sqlite3.connect(db, timeout=5) as conn:
            try:
                rows = conn.execute(
                    """SELECT ts_utc, source, ticker, claimed_event, actual_next_event, issue, severity
                       FROM fact_divergence_log
                       WHERE ts_utc >= datetime('now', ?)
                       ORDER BY ts_utc DESC LIMIT 50""",
                    (f"-{int(days)} days",),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []   # table not created yet → no divergences ever recorded
        items = [{
            "ts": r[0], "source": r[1], "ticker": r[2], "claimed_event": r[3],
            "actual": r[4], "issue": r[5], "severity": r[6],
        } for r in rows]
        return JSONResponse({"days": days, "count": len(items), "divergences": items})
    except Exception as exc:
        logger.error("fact-divergence endpoint error: %s", exc)
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

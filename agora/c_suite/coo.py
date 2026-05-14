"""
Chief Operations Officer (COO) — AGORA Operations & Reliability

Domain expertise:
  IBKR TWS API (order types, error codes, client IDs, connectivity), trade reconciliation,
  orphan order detection, data feed quality, execution quality monitoring, SQLite ops,
  operational risk (technology, connectivity, data vendor), GTC order lifecycle management.

Sub-agents supervised:
  ExecutionQualityAgent, OrphanOrderReconciler, DataIntegrityAgent, PositionManager
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .base import ExecutiveAgent
from ..core.config import AgoraSettings

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

_COO_SYSTEM_PROMPT = """\
You are the Chief Operations Officer (COO) of AGORA, an autonomous options trading platform owned by Rahul.
You report to the CEO. You are responsible for the reliability, integrity, and efficiency of all operations.
In trading, operational failure is financial failure — a missed fill, a stuck order, a bad data feed
are not IT problems, they are P&L problems. You treat every operational incident as a priority.

═══ IBKR TWS API — ORDER MANAGEMENT ═══

Order Types (IBKR specific):
  LMT:  Limit order. Use for all options. Specify limitPrice. Never exceed 10% worse than mid.
  MKT:  Market order. NEVER use for options. Bid-ask spread will eat any edge.
  STP:  Stop order. Use for stop-loss on underlying positions (not options).
  MOC:  Market-on-Close. Use when position MUST close today (e.g., expiration day).
  REL:  Relative/Pegged. Good for improving fills on liquid options — pegs to NBBO.
  BAG:  Combo order (basket). Use for multi-leg options (spreads). Sends legs together → better fills.

Order Lifecycle States:
  Submitted → PreSubmitted → Filled | Cancelled | Inactive
  Inactive = order rejected but not explicitly cancelled → create orphan risk.
  ApiPending → API received but not yet transmitted to exchange.
  Pendingcancel → cancel submitted but not yet confirmed.

Critical Error Codes:
  201: "Order rejected - reason:" + reason. Most common in AGORA.
      Subcodes: "riskfid:max combination orders" = too many simultaneous combos (IBKR limit).
      Cause: previous cancelled/rejected combo orders still consuming slots.
      Fix: OrphanOrderReconciler — cancel stale GTC children.
  202: "Order cannot be cancelled - not found." Order doesn't exist or already filled/cancelled.
  103: "Duplicate order id." Reused reqId — increment and retry.
  110: "Price does not conform to the minimum price variation." Tick size violation. Adjust price.
  162: "Historical market data service error." Data feed issue.
  200: "No security definition has been found." Contract not recognized. Verify qualifyContracts.
  321: "Error validating request — 'ba' : cause - The account does not have trading permissions."
  502: "Couldn't connect to TWS." IBKR Gateway/TWS not running. Check connectivity.
  1100: "Connectivity between IB and Trader Workstation has been lost." Reconnect required.
  1102: "Connectivity between IB and Trader Workstation restored — data maintained." Normal.
  2104: "Market data farm connection is OK." Informational — normal.

IBKR Client ID Architecture:
  Each connection must have unique clientId. AGORA uses:
    clientId=2:  Main session (paper trading) — order submission
    clientId=4:  IBKRNewsAgent — real-time news subscription
    clientId=9:  OrphanOrderReconciler — reconciliation
    clientId=10: Main session (live trading)
  Rule: NEVER reuse clientId across concurrent connections. Causes conflict.

GTC Order Management:
  GTC (Good Till Cancelled) orders persist until: filled, explicitly cancelled, or 180 days.
  Problem: parent order fails, child GTC orders become orphans consuming order slots.
  Pattern: bracket orders (parent + take-profit child + stop-loss child). If parent fails,
    children may remain as orphans → Error 201 storm when new orders submitted.
  Reconciliation: check ib.trades() for GTC children where parentId has no active parent.
  Cancel all orphans on startup AND after every Error 201.

═══ DATA INTEGRITY & FEED QUALITY ═══

IVR Data Anomalies:
  Raw IV (decimal) vs IVR (0-100 percentile rank) confusion:
    Raw IV: 0.35 = 35% annualized vol. Often mistakenly used as IVR = 35 (correct).
    But: if raw IV = 1.085 (108.5% vol), mistakenly used as IVR = 108.5 → WRONG.
    Result: IVR > 100 or IVR = 1 (decimal passed as rank) → broken trading signals.
  Detection: clamp IVR to [0, 100]. Alert if > 3 ETFs simultaneously show IVR ≥ 99.
  Response: disable vol premium bypass until feed recovers.

Price Feed Sanity:
  Stale prices: same price for 2+ consecutive 30-min polls → flag as stale.
  Spike detection: price change > 20% in 30 min → likely bad tick, not real move.
  Split/dividend adjusted: yfinance returns split-adjusted prices. Compare to IBKR.
  Corporate actions: splits → wrong option strikes (1000 shares @ $5 instead of 100 @ $50).

yfinance Reliability Issues:
  Options 401 errors: yfinance hits Yahoo Finance API rate limits. Rotate intervals or delay.
  Stale options chains: yfinance caches chains — may show old data. Check `lastTradeDate`.
  Missing data: `.options` returns [] for some tickers during market hours → retry logic.
  IV calculation: yfinance implied volatility uses its own model, may differ from IBKR.
  Mitigation: use `_YF_OPTIONS_LOCK` for thread safety; never concurrent yfinance options calls.

EDGAR API:
  Rate limits: 10 requests/second for EDGAR full-text search (EFTS). 40k/hour for company API.
  RSS feed: polling interval = 300 seconds (5 min). Faster polling = rate limit risk.
  Deduplication: same filing appears in multiple feeds (company page + type feed). Track by accession number.
  Auth: user-agent header required ("AGORA/1.0 contact@example.com"). Missing → 403 error.

═══ TRADE RECONCILIATION ═══

Shadow Book vs IBKR Book:
  Shadow book: AGORA's internal position record (SQLite `positions` table).
  IBKR book: actual positions at broker (ib.positions()).
  Reconciliation: shadow_book - ibkr_book should = 0 for every ticker.
  Discrepancies signal: missed fill update, orphan order, manual trade outside AGORA.
  Run reconciliation: every session start, every 30 minutes during market hours.

Position Manager SQLite Schema:
  Table: positions (id, session_id, ticker, strategy, direction, contracts, short_strike,
    long_strike, expiry_date, entry_date, entry_credit, max_profit, max_loss_dollars,
    delta, vega, theta, gamma, is_active, status, unrealized_pnl, regime_at_entry,
    conviction_at_entry, pillar, earnings_date, is_pre_earnings)
  Indexes: ticker, expiry_date, is_active. Check for missing indexes if queries are slow.
  WAL mode: Write-Ahead Logging. Essential for concurrent readers (API + session).
  Backup: daily SQLite backup to .agora/agora.db.bak before session starts.

Fill Rate Benchmarks:
  Excellent: > 90% fills on limit orders at mid
  Good: 70-90% fills
  Warning: 50-70% fills → spreads may be too wide, market moving fast, or IBKR connectivity issue
  Critical: < 50% fills → investigate immediately. Could be data issue, connectivity, or Error 201 storm.

Slippage Benchmarks by liquidity tier:
  Tier 1 (SPY, QQQ, AAPL, MSFT): ≤ $0.05/share slippage acceptable
  Tier 2 (NVDA, TSLA, META): ≤ $0.10/share
  Tier 3 (mid-cap single names): ≤ $0.20/share
  Above benchmark: adjust limit price aggressiveness or reduce position size.

═══ SYSTEM RELIABILITY ═══

IBKR Connectivity:
  Paper trading: port 7497, Gateway recommended (vs TWS for headless operation).
  Live trading: port 7496. IBKR Gateway required — do not use TWS for production.
  Auto-reconnect: ib_insync handles reconnect internally. Monitor 1100/1102 error codes.
  Market data farm: separate from order routing. Data loss ≠ order loss.

Session Startup Checklist:
  1. Verify IBKR Gateway running and connected (ib.isConnected())
  2. Reconcile shadow book vs IBKR positions (cancel orphan GTCs)
  3. Check all sub-agent status (data feeds, API connectivity)
  4. Verify kill switch state (should be inactive for normal session)
  5. Run DataIntegrityAgent poll cycle (verify IVR feed health)
  6. Log session start with all parameters (mode, account, positions count)

SQLite Operations:
  Use WAL (Write-Ahead Logging): PRAGMA journal_mode=WAL
  Set busy timeout: PRAGMA busy_timeout=5000 (5 seconds — prevents lock contention)
  Regular VACUUM: monthly to reclaim space and defragment
  Check integrity: PRAGMA integrity_check — run on startup after crash
  Index critical columns: ticker, is_active, attempt_date for fast queries

API Rate Limiting:
  Anthropic: claude-opus-4-7 — default limits apply. Use prompt caching (cache_control: ephemeral)
    on system prompts to reduce token cost on repeated calls.
  yfinance: unofficial API, no SLA. Rate limit = soft (HTTP 429 / 401).
  EDGAR: 10 req/sec. 40k/hour. Use polling interval ≥ 300s.
  IBKR: 50 market data requests per second. 100 simultaneous subscriptions default.

═══ AGORA SESSION TIMING ARCHITECTURE ═══

Session Loop (asyncio.sleep(60) cadence — core intelligence cycle):
  7:00 AM ET:        _premarket_macro_scan() — MacroSynthesizer runs extended thinking scan.
  9:30–15:30 ET:     _universe_scan() — every 30 min (skips 9:00 AM to avoid open chaos).
  16:05 ET:          _afterhours_report() — P&L attribution, PsiMonitor, nightly CEO report.
  Every 60s (9-16h): _check_pre_earnings_closes() — T-1 close check for pre-earnings positions.
  Parallel loop:     _price_monitor_loop() — price move detection (> 1% or volume 3×) →
                     promotes tickers to _priority_queue for immediate evaluation.

Macro Refresh Timing:
  Primary: 7:00 AM ET pre-market scan (full Claude extended thinking call).
  Intraday refresh: triggered if SPY moves > 1% OR VIX moves > 2 pts from last synthesis.
  Cooldown: 120 minutes (_MACRO_REFRESH_COOLDOWN_MIN) between any two refreshes.
  State tracked: _last_macro_spy, _last_macro_vix, _last_macro_refresh_et.

File System Paths (all relative to working directory):
  Database:     .agora/agora.db       (SQLite, WAL mode, busy_timeout=5000ms)
  Shadow book:  .agora/shadow_book.json (JSON snapshot, reconciled on startup)
  Audit log:    .agora/audit.log      (append-only, never truncate)
  IV cache:     .agora/iv_cache/      (per-ticker IV percentile rank, per-session)
  DB backup:    .agora/agora.db.bak   (created before session start each day)

Reconciliation Schedule (what COO enforces):
  Session startup: full shadow-book vs ib.positions() diff + orphan GTC cancellation.
  Every 30 min during market hours: incremental shadow-book reconciliation.
  After every Error 201: immediate orphan scan — OrphanOrderReconciler.reconcile_now().
  Pre-earnings proximity: asyncio.create_task(_earnings_proximity_startup_check()) on run().

Universe Scan Priority Queue:
  _tier1: [SPY, QQQ, IWM, NVDA, AAPL, MSFT, META, TSLA, PLTR, MSTR, GLD, TLT] — always evaluated.
  _priority_queue: tickers promoted by price monitor — evaluated first each cycle.
  _scan_index: rotating cursor through full etf_universe for Tier 2 tickers.
  COO monitors: if priority_queue grows > 10 tickers → price monitor may be running hot (investigate).

Key Operational State (session object fields to monitor):
  _pre_earnings_tickers: dict[ticker, earnings_date] — active pre-earnings positions (no double entry).
  _earnings_date_cache:  dict[ticker, date|None]     — yfinance calendar cache (per session, one call per ticker).
  _running: bool                                     — False triggers graceful stop() across all agents.
  _macro_context: MacroContext | None                — None before 7 AM scan; None = conservative defaults.
"""


class COOAgent(ExecutiveAgent):
    """Chief Operations Officer — system reliability, execution ops, data integrity."""

    TITLE = "Chief Operations Officer (COO)"
    BRIEF_CADENCE = 2  # every 2 hours — operations needs frequent monitoring

    def __init__(
        self,
        settings: AgoraSettings | None = None,
        ceo_agent: Any = None,
        exec_quality: Any = None,
        orphan_reconciler: Any = None,
        data_integrity: Any = None,
        position_mgr: Any = None,
        system_health: Any = None,
        ibkr_agent: Any = None,
    ) -> None:
        super().__init__(settings, ceo_agent)
        self._eq         = exec_quality
        self._reconciler = orphan_reconciler
        self._di         = data_integrity
        self._pm         = position_mgr
        self._health     = system_health
        self._ibkr_agent = ibkr_agent  # IBKRKnowledgeAgent — IBKR expert sub-agent

    @property
    def _system_prompt(self) -> str:
        return _COO_SYSTEM_PROMPT

    def self_audit(self) -> list[tuple[str, str, str]]:
        """
        COO proactive patrol: ghost fills, fill rate, IBKR connectivity,
        data integrity, orphan orders, stale prices — all independently checked.
        """
        findings: list[tuple[str, str, str]] = []
        import sqlite3 as _sql

        # ── Ghost fills: fills in execution_quality with no matching position ──
        try:
            conn = _sql.connect(str(self._settings.db_path), check_same_thread=False)
            ghost_rows = conn.execute(
                """SELECT eq.ticker, eq.fill_price, eq.attempt_date
                   FROM execution_quality eq
                   WHERE eq.outcome='fill'
                   AND eq.attempt_date >= date('now', '-7 days')
                   AND eq.ticker NOT IN (SELECT ticker FROM positions)"""
            ).fetchall()
            conn.close()
            if ghost_rows:
                tickers = list({r[0] for r in ghost_rows})
                findings.append((
                    "ghost_fills_detected",
                    "critical",
                    f"{len(ghost_rows)} ghost fill(s) detected: {', '.join(tickers)}. "
                    "execution_quality shows confirmed fills with NO matching position record. "
                    "Shadow book incomplete — these IBKR trades are invisible to risk management.",
                ))
        except Exception:
            pass

        # ── Fill rate and timeout rate ──
        if self._eq:
            try:
                today = self._eq.get_today_db_stats()
                total        = today.get("total", 0)
                fills        = today.get("fills", 0)
                fill_rate    = today.get("fill_rate")
                timeout_rate = today.get("timeout_rate")

                if total >= 3:
                    if fill_rate is not None and fill_rate < 0.15:
                        findings.append((
                            "fill_rate_critical",
                            "critical",
                            f"Fill rate {fill_rate:.1%} today ({fills}/{total}). "
                            "Check IBKR connectivity and fill-callback timeout settings.",
                        ))
                    if timeout_rate is not None and timeout_rate > 0.85:
                        findings.append((
                            "timeout_rate_critical",
                            "critical",
                            f"Timeout rate {timeout_rate:.1%} today. "
                            "Fill callbacks not reaching session — shadow book diverging from IBKR.",
                        ))
                    if self._eq.get_session_stats().get("error_201_storm", False):
                        findings.append((
                            "error_201_storm",
                            "critical",
                            "IBKR Error 201 storm: max combo order slots exhausted. "
                            "OrphanOrderReconciler must cancel stale GTC children NOW.",
                        ))
            except Exception:
                pass

        # ── Orphan reconciler: orphans cancelled this cycle ──
        if self._reconciler:
            try:
                cancelled = self._reconciler.last_orphans_cancelled
                if cancelled and cancelled > 0:
                    findings.append((
                        "orphan_gtc_orders_cancelled",
                        "warning",
                        f"OrphanOrderReconciler cancelled {cancelled} stale GTC order(s) this cycle. "
                        "Prior session left zombie orders consuming IBKR combo slots.",
                    ))
            except Exception:
                pass

        # ── IVR data feed health ──
        if self._di:
            try:
                if not self._di.ivr_feed_healthy:
                    findings.append((
                        "ivr_feed_unhealthy",
                        "critical",
                        "IVR feed is UNHEALTHY. Vol-premium bypass signals are unreliable. "
                        "Vol-bypass is disabled until feed recovers — no credit spread entries.",
                    ))
                stale = list(self._di.get_stale_tickers()) if hasattr(self._di, "get_stale_tickers") else []
                if len(stale) > 3:
                    findings.append((
                        "stale_price_feeds",
                        "warning",
                        f"{len(stale)} tickers with stale price data: {', '.join(stale[:5])}. "
                        "Stale prices feed incorrect conviction scores — investigation required.",
                    ))
            except Exception:
                pass

        # ── IBKR connectivity ──
        if self._ibkr_agent:
            try:
                status = self._ibkr_agent.get_status()
                if not status.get("connection_healthy", False):
                    findings.append((
                        "ibkr_connection_unhealthy",
                        "critical",
                        "IBKR connection is UNHEALTHY — orders cannot be submitted. "
                        "Check IBKR Gateway running on correct port (7497 paper / 7496 live).",
                    ))
            except Exception:
                findings.append((
                    "ibkr_agent_unreachable",
                    "critical",
                    "IBKRKnowledgeAgent status check failed — cannot confirm order routing health.",
                ))

        # ── System health ──
        if self._health:
            try:
                if not self._health.is_healthy():
                    findings.append((
                        "system_health_failing",
                        "critical",
                        "System health check FAILING. One or more sub-systems are in degraded state.",
                    ))
            except Exception:
                pass

        return findings

    def collect_intelligence(self) -> dict[str, Any]:
        now = datetime.now(tz=ET).isoformat()
        intel: dict[str, Any] = {"timestamp": now, "department": "Operations"}
        alerts: list[str] = []   # COO self-audit findings — escalated to CEO

        # ── Execution quality — DB-accurate (in-memory resets on restart) ──
        if self._eq:
            try:
                today = self._eq.get_today_db_stats()
                w7    = self._eq.get_7day_stats()
                fills    = today.get("fills", 0)
                timeouts = today.get("timeouts", 0)
                total    = today.get("total", 0)
                fill_rate    = today.get("fill_rate")
                timeout_rate = today.get("timeout_rate")

                intel["execution_today"] = {
                    "attempts":     total,
                    "fills":        fills,
                    "rejects":      today.get("rejects", 0),
                    "timeouts":     timeouts,
                    "fill_rate_pct":    f"{fill_rate*100:.1f}%" if fill_rate is not None else "no data",
                    "timeout_rate_pct": f"{timeout_rate*100:.1f}%" if timeout_rate is not None else "no data",
                }
                intel["execution_7day"] = {
                    "fill_rate_pct":  f"{w7.get('fill_rate',0)*100:.1f}%",
                    "total_attempts": w7.get("total", 0),
                }

                # Self-audit: flag abnormal fill/timeout rates
                if fill_rate is not None and fill_rate < 0.15:
                    alerts.append(
                        f"🚨 FILL RATE CRITICAL: {fill_rate:.1%} today ({fills}/{total}). "
                        "Check IBKR connectivity and fill-callback timeout settings."
                    )
                if timeout_rate is not None and timeout_rate > 0.85:
                    alerts.append(
                        f"⚠️ TIMEOUT RATE HIGH: {timeout_rate:.1%} today. "
                        "Fill callbacks may not be reaching the session — check ib_insync connection."
                    )
            except Exception as exc:
                intel["execution_error"] = str(exc)

        # ── Ghost fill detection — PROACTIVE DATA INTEGRITY CHECK ──
        # Ghost fill = execution_quality says 'fill' but no position in DB.
        # Cause: session crash between record_fill() and _record_position().
        # Effect: real IBKR trade with no shadow book entry — P&L blind spot.
        import sqlite3 as _sql
        try:
            db_path = str(self._settings.db_path)
            conn = _sql.connect(db_path, check_same_thread=False)
            ghost_rows = conn.execute(
                """SELECT eq.ticker, eq.fill_price, eq.attempt_date
                   FROM execution_quality eq
                   WHERE eq.outcome='fill'
                   AND eq.attempt_date >= date('now', '-7 days')
                   AND eq.ticker NOT IN (SELECT ticker FROM positions)"""
            ).fetchall()
            conn.close()
            ghost_count = len(ghost_rows)
            ghost_tickers = [r[0] for r in ghost_rows]
            intel["ghost_fills"] = {
                "count":   ghost_count,
                "tickers": ghost_tickers,
            }
            if ghost_count > 0:
                alerts.append(
                    f"🚨 {ghost_count} GHOST FILL(S) DETECTED: {', '.join(ghost_tickers)}. "
                    "execution_quality has confirmed fills with no matching position record. "
                    "Shadow book is INCOMPLETE. These trades exist at IBKR but are invisible to AGORA."
                )
        except Exception as exc:
            intel["ghost_fill_error"] = str(exc)

        # ── Realized P&L today — closed positions ──
        try:
            db_path = str(self._settings.db_path)
            conn = _sql.connect(db_path, check_same_thread=False)
            rows = conn.execute(
                "SELECT ticker, realized_pnl, close_source FROM positions WHERE close_date=date('now')"
            ).fetchall()
            conn.close()
            realized_total = sum(r[1] or 0 for r in rows)
            intel["closed_today"] = {
                "count":          len(rows),
                "realized_pnl":   round(realized_total, 2),
                "positions":      [{"ticker": r[0], "pnl": r[1], "source": r[2]} for r in rows],
            }
            if len(rows) > 0 and realized_total < 0:
                alerts.append(
                    f"📉 Realized P&L today: ${realized_total:.2f} "
                    f"({len(rows)} position(s) closed: "
                    f"{', '.join(r[0] for r in rows)})."
                )
        except Exception as exc:
            intel["closed_today_error"] = str(exc)

        # ── Data integrity ──
        if self._di:
            try:
                stale = list(self._di.get_stale_tickers())
                intel["data_integrity"] = {
                    "ivr_feed_healthy":  self._di.ivr_feed_healthy,
                    "vol_bypass_allowed": self._di.vol_bypass_allowed(),
                    "stale_tickers":     stale,
                }
                if not self._di.ivr_feed_healthy:
                    alerts.append("⚠️ IVR feed unhealthy — vol-premium bypass signals unreliable.")
                if stale:
                    alerts.append(f"⚠️ Stale price feeds: {', '.join(stale[:5])}.")
            except Exception:
                pass

        # ── System health ──
        if self._health:
            try:
                intel["system_health"] = self._health.get_status()
                intel["system_healthy"] = self._health.is_healthy()
                if not self._health.is_healthy():
                    alerts.append("🚨 System health check FAILING — see system_health for details.")
            except Exception:
                pass

        # ── Orphan reconciler ──
        if self._reconciler:
            try:
                cancelled = self._reconciler.last_orphans_cancelled
                synced    = self._reconciler.last_positions_synced
                intel["orphan_reconciler"] = {
                    "last_orphans_cancelled":  cancelled,
                    "last_positions_synced":   synced,
                }
                if cancelled > 0:
                    alerts.append(
                        f"♻️ Orphan reconciler cancelled {cancelled} GTC order(s) this cycle. "
                        "Prior session left zombie orders consuming IBKR combo slots."
                    )
            except Exception:
                pass

        # ── Shadow book vs DB ──
        if self._pm:
            try:
                positions = self._pm.get_open_positions()
                intel["position_count"] = len(positions)
            except Exception:
                pass

        # ── IBKR live ──
        if self._ibkr_agent:
            try:
                ibkr_status = self._ibkr_agent.get_status()
                intel["ibkr"] = ibkr_status
                if not ibkr_status.get("connection_healthy", False):
                    alerts.append("🚨 IBKR connection UNHEALTHY — orders cannot be submitted.")
            except Exception as exc:
                intel["ibkr"] = {"error": str(exc)}
                alerts.append("🚨 IBKRKnowledgeAgent status check failed.")

        # ── Attach all COO alerts to intel ──
        intel["alerts"] = alerts
        intel["alert_count"] = len(alerts)
        if alerts:
            logger.warning("COO self-audit: %d alert(s): %s", len(alerts), "; ".join(alerts))

        return intel

    async def self_heal(self, findings: list[tuple[str, str, str]]) -> None:
        """
        COO corrective actions — pre-delegated authority:
          ghost_fills_detected      → trigger orphan reconciler immediately
          error_201_storm           → force orphan reconciler reconcile_now()
          ibkr_connection_unhealthy → publish ibkr_disconnected event, alert CTO
          fill_rate_critical        → publish fill_rate_critical event to CTO
        """
        keys = {k for k, _, _ in findings}

        if "ghost_fills_detected" in keys:
            self._heal_attempts["ghost_fills"] = self._heal_attempts.get("ghost_fills", 0) + 1
            if self._reconciler and self._heal_attempts["ghost_fills"] <= 3:
                try:
                    logger.info("COO self_heal: ghost fills detected — triggering reconciler")
                    await self._reconciler.reconcile_now()
                    await self.notify_peers("ghost_fills_detected", {
                        "action": "reconciler triggered by COO self_heal",
                    })
                except Exception as exc:
                    logger.error("COO self_heal: reconciler failed: %s", exc)

        if "error_201_storm" in keys or "orphan_gtc_orders_cancelled" in keys:
            if self._reconciler:
                try:
                    logger.info("COO self_heal: Error 201 — forcing immediate orphan reconciliation")
                    await self._reconciler.reconcile_now()
                except Exception as exc:
                    logger.error("COO self_heal: orphan reconcile failed: %s", exc)

        if "ibkr_connection_unhealthy" in keys:
            self._heal_attempts["ibkr_down"] = self._heal_attempts.get("ibkr_down", 0) + 1
            if self._heal_attempts["ibkr_down"] == 1:
                await self.notify_peers("ibkr_disconnected", {
                    "reason": "COO patrol: IBKR connection health check failed",
                })
                logger.warning("COO self_heal: IBKR disconnected — published event to peers")

        if "fill_rate_critical" in keys:
            self._heal_attempts["fill_rate"] = self._heal_attempts.get("fill_rate", 0) + 1
            if self._heal_attempts["fill_rate"] == 1:
                await self.notify_peers("fill_rate_critical", {
                    "reason": "COO patrol: fill rate < 15%",
                })

        if "ivr_feed_unhealthy" in keys and self._di:
            try:
                logger.info("COO self_heal: IVR feed unhealthy — requesting feed refresh")
                if hasattr(self._di, "request_refresh"):
                    self._di.request_refresh()
            except Exception:
                pass

    async def on_peer_event(self, event_type: str, publisher: str, payload: dict) -> None:
        """COO monitors ghost fills and fill rate events from other agents."""
        if event_type == "fill_rate_critical":
            logger.warning("COO: fill_rate_critical received from %s — escalating to reconciler", publisher)
            if self._reconciler:
                try:
                    await self._reconciler.reconcile_now()
                except Exception:
                    pass

    def get_readiness_tasks(self) -> list[str]:
        tasks = []
        if not self._reconciler:
            tasks.append("Wire OrphanOrderReconciler to COO — Error 201 auto-fix disabled")
        if not self._di:
            tasks.append("Wire DataIntegrityAgent to COO — IVR feed health monitoring disabled")
        if not self._ibkr_agent:
            tasks.append("Wire IBKRKnowledgeAgent to COO — IBKR connection health check disabled")
        return tasks

    async def diagnose_ibkr(self, question: str) -> str:
        """
        Delegate an IBKR-specific question to the IBKRKnowledgeAgent (Claude expert).
        Called by CEO board meeting or directly from the dashboard /agora/ibkr-diagnose endpoint.
        """
        if self._ibkr_agent:
            return await self._ibkr_agent.diagnose(question)
        return "IBKRKnowledgeAgent not wired — cannot diagnose."

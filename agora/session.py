"""
AGORA Session — the main trading session orchestrator.

Coordinates all agents in one async event loop:
  1. Pre-market (7 AM ET): MacroSynthesizer runs extended thinking scan
  2. Market open (9:30 AM ET): Signal computation for universe
  3. Intraday (continuous): CatalystDiscoveryAgent + EarningsTranscriptAgent + SmartMoneyAgent
  4. Position lifecycle (every 60s): PositionManager checks targets/stops
  5. Risk gating: every trade goes through RiskCouncil before IBKR
  6. 3:30 PM ET: Auto-close expiring positions
  7. After-hours (4 PM ET): P&L attribution, PSI check, nightly report

Run: python -m agora.session
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from agora.agents import (
    CEOAgent,
    ConvictionScorer,
    DisagreementResolver,
    MacroSynthesizer,
    SectorMomentumAgent,
    SignalInput,
)
from agora.agents.premarket_setup import PreMarketSetupAgent, PositionAlert
from agora.agents.sector_intelligence import SectorIntelligenceAgent
from agora.agents.price_target import PriceTargetAgent
from agora.core.config import AgoraSettings, get_settings
from agora.core.models import Catalyst, OpenPosition, PositionStatus, StrategyPillar
from agora.discovery.analyst_revision import AnalystRevisionTracker
from agora.discovery.catalyst_agent import CatalystDiscoveryAgent
from agora.discovery.earnings_calendar import EarningsCalendarAgent, EarningsSetup
from agora.discovery.earnings_transcript import EarningsTranscriptAgent
from agora.discovery.ibkr_news import IBKRNewsAgent
from agora.discovery.market_interest import MarketInterestAgent
from agora.discovery.smart_money import SmartMoneyAgent
from agora.discovery.universe_discovery import UniverseDiscoveryAgent
from agora.lifecycle.position_manager import PositionManager
from agora.execution.ibkr_bridge import close_trade, submit_trade
from agora.ops.agent_performance import AgentPerformanceMonitor
from agora.ops.attribution import PnlAttributor, PsiMonitor
from agora.ops.system_health import SystemHealthAgent
from agora.ops.execution_quality import ExecutionQualityAgent
from agora.ops.data_integrity import DataIntegrityAgent
from agora.ops.pillar_health import PillarHealthAgent
from agora.ops.orphan_reconciler import OrphanOrderReconciler
from agora.ops.ibkr_knowledge_agent import IBKRKnowledgeAgent
from agora.c_suite import CROAgent, CIOAgent, CTOAgent, COOAgent, CFOAgent, RNDAgent
from agora.c_suite.ctech import CTechAgent
from agora.core.events import AgentEventBus
from agora.ops.live_readiness import LiveReadinessMeter
from agora.risk.circuit_breaker import CircuitBreakerAgent
from agora.risk.compliance import ComplianceAgent
from agora.risk.entry_timing import EntryTimingGate
from trading_platform.services.macro_calendar import get_macro_calendar
from agora.risk.risk_council import RiskCouncil
from agora.signals.event_patterns import EventPatternEngine
from agora.signals.iv_premium import IvPremiumScreen
from agora.signals.vol_regime import VolRegimeClassifier
from agora.strategies.rules_engine import StrategyRulesEngine

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")


class AgoraSession:
    """
    Top-level async orchestrator. Runs as a long-lived process.
    Paper trading by default; flip to live in .env: TRADING_MODE=live
    """

    def __init__(self, settings: AgoraSettings | None = None) -> None:
        self._settings = settings or get_settings()
        self._session_id = f"AGORA-{datetime.now(tz=ET).strftime('%Y%m%d-%H%M')}"

        # Signal generators
        self._vol_classifier  = VolRegimeClassifier()
        self._iv_screen       = IvPremiumScreen()
        self._event_engine    = EventPatternEngine()
        self._psi             = PsiMonitor()

        # Agents
        self._macro           = MacroSynthesizer(self._settings)
        self._sector          = SectorMomentumAgent(self._settings)
        self._scorer          = ConvictionScorer()
        self._resolver        = DisagreementResolver()
        self._strategy        = StrategyRulesEngine(self._settings)

        # Infrastructure
        self._position_mgr    = PositionManager(
            self._settings,
            on_close_order=self._execute_close,
            on_roll_order=self._execute_roll,
        )
        self._risk            = RiskCouncil(self._settings)
        self._attributor      = PnlAttributor(self._settings)

        # Discovery (fires async callbacks into _on_catalyst / _on_earnings)
        self._catalyst_agent  = CatalystDiscoveryAgent(
            self._settings,
            on_catalyst=self._on_catalyst,
        )
        self._earnings_agent  = EarningsTranscriptAgent(
            self._settings,
            on_earnings=self._on_earnings,
            on_catalyst=self._on_catalyst,
        )
        self._smart_money     = SmartMoneyAgent(
            self._settings,
            on_catalyst=self._on_catalyst,
        )
        self._ibkr_news       = IBKRNewsAgent(
            self._settings,
            on_catalyst=self._on_catalyst,
        )

        # Pre-earnings intelligence (proactive — runs before announcements)
        self._earnings_calendar = EarningsCalendarAgent(
            self._settings,
            on_earnings_setup=self._on_earnings_setup,
        )

        # Entry timing gate — hard block on after-hours and pre-market entries
        self._entry_timing    = EntryTimingGate()

        # CEO Agent — autonomous daily operator, reports to Rahul
        self._ceo             = CEOAgent(
            self._settings,
            position_mgr=self._position_mgr,
            risk_council=self._risk,
        )

        # Circuit breaker — real-time P&L monitor; trips kill switch on breach
        self._circuit_breaker = CircuitBreakerAgent(
            self._settings,
            position_mgr=self._position_mgr,
            risk_council=self._risk,
            ceo_agent=self._ceo,
        )

        # Pre-market position review (6:30 AM ET)
        self._premarket_setup = PreMarketSetupAgent(
            self._settings,
            position_mgr=self._position_mgr,
            on_position_alert=self._on_position_alert,
        )

        # Sector intelligence — peer earnings regression (updated daily)
        self._sector_intel    = SectorIntelligenceAgent(self._settings)

        # Price target — analyst consensus + bull/base/bear scenarios
        self._price_target    = PriceTargetAgent(
            self._settings,
            sector_intel_agent=self._sector_intel,
        )

        # Market interest fingerprints (6 signals, 30-min intraday)
        self._market_interest = MarketInterestAgent(self._settings)

        # Dynamic universe expansion (weekly + catalyst-triggered)
        self._universe_disc   = UniverseDiscoveryAgent(
            self._settings,
            market_interest_agent=self._market_interest,
        )

        # Compliance gate — wash sale, Reg T (NO PDT — repealed June 4, 2026)
        self._compliance      = ComplianceAgent(self._settings)

        # System health — IBKR/yfinance/Anthropic/DB heartbeat
        self._system_health   = SystemHealthAgent(
            self._settings,
            ceo_agent=self._ceo,
            position_mgr=self._position_mgr,
        )

        # Agent performance monitor — alpha attribution by pillar/regime/conviction
        self._agent_perf      = AgentPerformanceMonitor(self._settings)

        # Analyst revision tracker — post-earnings upgrade/downgrade momentum
        self._analyst_rev     = AnalystRevisionTracker(
            self._settings,
            on_catalyst=self._on_catalyst,
        )

        # ── New board-approved agents ──────────────────────────────────
        # ExecutionQualityAgent — fill rate, reject taxonomy, slippage tracking
        self._exec_quality    = ExecutionQualityAgent(
            self._settings,
            ceo_agent=self._ceo,
        )

        # DataIntegrityAgent — IVR feed sanity; disables vol bypass on bad data
        self._data_integrity  = DataIntegrityAgent(
            self._settings,
            ceo_agent=self._ceo,
        )

        # PillarHealthAgent — detects silent directional pillars
        self._pillar_health   = PillarHealthAgent(
            self._settings,
            ceo_agent=self._ceo,
        )

        # OrphanOrderReconciler — cancels stale IBKR GTC children (fixes Error 201)
        self._orphan_reconciler = OrphanOrderReconciler(
            self._settings,
            position_mgr=self._position_mgr,
            ceo_agent=self._ceo,
        )

        # IBKRKnowledgeAgent — Claude-powered IBKR expert; sub-agent of COO
        self._ibkr_agent = IBKRKnowledgeAgent(
            self._settings,
            exec_quality=self._exec_quality,
            orphan_reconciler=self._orphan_reconciler,
            position_mgr=self._position_mgr,
        )

        # ── C-suite executives ──────────────────────────────────────
        self._cro = CROAgent(
            self._settings,
            ceo_agent=self._ceo,
            risk_council=self._risk,
            circuit_breaker=self._circuit_breaker,
            compliance=self._compliance,
            position_mgr=self._position_mgr,
        )
        self._cio = CIOAgent(
            self._settings,
            ceo_agent=self._ceo,
            macro_synthesizer=self._macro,
            sector_intel=self._sector_intel,
            market_interest=self._market_interest,
            catalyst_agent=self._catalyst_agent,
            smart_money=self._smart_money,
            ibkr_news=self._ibkr_news,
        )
        self._cto = CTOAgent(
            self._settings,
            ceo_agent=self._ceo,
            conviction_scorer=self._scorer,
            disagreement_resolver=self._resolver,
            position_mgr=self._position_mgr,
            exec_quality=self._exec_quality,
            close_callback=self._execute_close,
        )
        self._coo = COOAgent(
            self._settings,
            ceo_agent=self._ceo,
            exec_quality=self._exec_quality,
            orphan_reconciler=self._orphan_reconciler,
            data_integrity=self._data_integrity,
            position_mgr=self._position_mgr,
            system_health=self._system_health,
            ibkr_agent=self._ibkr_agent,
        )
        self._cfo = CFOAgent(
            self._settings,
            ceo_agent=self._ceo,
            position_mgr=self._position_mgr,
            pnl_attributor=self._attributor,
            agent_performance=self._agent_perf,
        )
        self._rnd = RNDAgent(
            self._settings,
            ceo_agent=self._ceo,
            pillar_health=self._pillar_health,
            earnings_calendar=self._earnings_calendar,
            earnings_transcript=self._earnings_agent,
            event_engine=self._event_engine,
            universe_disc=self._universe_disc,
            analyst_rev=self._analyst_rev,
        )
        self._ctech = CTechAgent(
            self._settings,
            ceo_agent=self._ceo,
            conviction_scorer=self._scorer,
            disagreement_resolver=self._resolver,
            event_engine=self._event_engine,
            iv_screen=self._iv_screen,
            vol_regime=self._vol_classifier,
            macro_synthesizer=self._macro,
            sector_intel=self._sector_intel,
            universe_disc=self._universe_disc,
        )

        # Wire C-suite into CEO (post-instantiation to avoid circular deps)
        self._ceo.wire_c_suite(
            cro=self._cro, cio=self._cio, cto=self._cto,
            coo=self._coo, cfo=self._cfo, rnd=self._rnd,
            ctech=self._ctech,
        )

        # ── AgentEventBus — lateral C-to-C communications ────────────────────
        self._event_bus = AgentEventBus()

        # Each agent subscribes to the events it cares about
        self._cro.wire_event_bus(self._event_bus,
            "profit_factor_low", "fill_rate_critical")
        self._cio.wire_event_bus(self._event_bus,
            "position_closed")
        self._cto.wire_event_bus(self._event_bus,
            "kill_switch_tripped", "kill_switch_reset",
            "size_bias_changed", "regime_changed",
            "macro_context_updated", "fill_rate_critical",
            "ibkr_disconnected", "daily_loss_warning", "conviction_drift")
        self._coo.wire_event_bus(self._event_bus,
            "fill_rate_critical")
        self._cfo.wire_event_bus(self._event_bus,
            "position_closed")
        self._rnd.wire_event_bus(self._event_bus,
            "position_closed", "conviction_drift")
        self._ctech.wire_event_bus(self._event_bus,
            "fill_rate_critical", "ibkr_disconnected")

        # ── SessionPlan — CEO publishes, all C-suite reads ────────────────────
        for agent in (self._cro, self._cio, self._cto, self._coo,
                      self._cfo, self._rnd, self._ctech):
            agent.wire_session_plan(self._ceo.get_session_plan)

        # ── Trade feedback hook — wire CEO's feedback processor to PositionManager ──
        # PositionManager calls on_position_closed when a close is recorded.
        if hasattr(self._position_mgr, "register_close_callback"):
            self._position_mgr.register_close_callback(self._on_position_closed_feedback)

        # ── register_csuite_manager() — wire each sub-agent's alert chain ──────
        # Each sub-agent routes alerts through its C-suite manager before CEO.
        # Pattern: SubAgent._notify() → C-suite.receive_alert() → CEO (if critical)

        # Risk sub-agents → CRO
        self._risk.register_csuite_manager(self._cro)
        self._compliance.register_csuite_manager(self._cro)
        self._circuit_breaker.register_csuite_manager(self._cro)

        # Intelligence / discovery sub-agents → CIO
        self._catalyst_agent.register_csuite_manager(self._cio)
        self._smart_money.register_csuite_manager(self._cio)
        self._ibkr_news.register_csuite_manager(self._cio)

        # Ops sub-agents → COO
        self._exec_quality.register_csuite_manager(self._coo)
        self._data_integrity.register_csuite_manager(self._coo)
        self._system_health.register_csuite_manager(self._coo)
        self._orphan_reconciler.register_csuite_manager(self._coo)
        self._ibkr_agent.register_csuite_manager(self._coo)  # IBKR expert → COO

        # Research sub-agents → RND
        self._pillar_health.register_csuite_manager(self._rnd)
        self._earnings_calendar.register_csuite_manager(self._rnd)
        self._earnings_agent.register_csuite_manager(self._rnd)
        self._analyst_rev.register_csuite_manager(self._rnd)

        # Live Readiness Meter — CEO-controlled go-live gate
        self._readiness = LiveReadinessMeter()
        # Wire readiness + exec quality into CEO for integrity patrol
        self._ceo.wire_readiness(self._readiness)
        self._ceo.wire_exec_quality(self._exec_quality)
        self._readiness.register_agents(
            risk_council=self._risk,
            circuit_breaker=self._circuit_breaker,
            compliance=self._compliance,
            macro=self._macro,
            sector_intel=self._sector_intel,
            market_interest=self._market_interest,
            pillar_health=self._pillar_health,
            conviction_scorer=self._scorer,
            disagreement_resolver=self._resolver,
            universe_disc=self._universe_disc,
            exec_quality=self._exec_quality,
            orphan_reconciler=self._orphan_reconciler,
            data_integrity=self._data_integrity,
            system_health=self._system_health,
            agent_performance=self._agent_perf,
            earnings_cal=self._earnings_calendar,
            earnings_transcript=self._earnings_agent,
            analyst_rev=self._analyst_rev,
            event_engine=self._event_engine,
            position_mgr=self._position_mgr,
        )

        # State
        self._macro_context   = None
        self._running         = False

        # ── Event-driven scan state ────────────────────────────────
        # Tier 1: always scanned every 30-min cycle (highest liquidity / most active)
        self._tier1: list[str] = [
            "SPY", "QQQ", "IWM",                          # broad market
            "NVDA", "AAPL", "MSFT", "META", "TSLA",       # mega-cap
            "PLTR", "MSTR", "GLD", "TLT",                 # vol + macro
        ]
        # Priority queue: tickers promoted by price monitor (move/volume triggers)
        self._priority_queue: list[str] = []
        self._priority_reasons: dict[str, str] = {}       # ticker → trigger reason
        # Rotation index for Tier 2 (the remaining tickers)
        self._scan_index: int = 0
        # Last known prices for move detection
        self._last_prices: dict[str, float] = {}
        self._last_volumes: dict[str, float] = {}

        # ── Intraday macro refresh state ───────────────────────────
        # Tracks market levels at last synthesis so we can detect regime shifts
        self._last_macro_spy: float | None = None   # SPY price at last macro synthesis
        self._last_macro_vix: float | None = None   # VIX level at last macro synthesis
        self._last_macro_refresh_et: datetime | None = None  # time of last synthesis
        self._synthesis_in_progress: bool = False   # guard against concurrent synthesis calls

        # ── Event deduplication ────────────────────────────────────
        # Tracks tickers with active pre-earnings setups (ticker → earnings_date).
        # Prevents concurrent catalyst + pre-earnings double-entry on the same ticker.
        self._pre_earnings_tickers: dict[str, Any] = {}  # ticker → earnings date

        # Catalyst cooldown: (ticker, catalyst_type) → last_emit_time (UTC).
        # Prevents the same story arriving from multiple IBKR providers (DJ-N, DJ-RTG,
        # DJ-RTPRO) from triggering duplicate entries. 30-minute window.
        self._catalyst_cooldown: dict[tuple, datetime] = {}
        self._CATALYST_COOLDOWN_SECS: int = 1800  # 30 minutes

        # Execution cooldown: block re-submission of a ticker that failed to fill.
        # Key = ticker, value = UTC datetime after which re-submission is allowed.
        # Prevents the same illiquid spread from consuming execution budget on every scan.
        self._exec_cooldowns: dict[str, datetime] = {}
        self._EXEC_COOLDOWN_SECS: int = 7200  # 2 hours after a timeout/reject

        # ── Earnings proximity cache ───────────────────────────────
        # Per-session cache of next earnings dates from yfinance.calendar.
        # Populated on first lookup per ticker; prevents repeated yfinance calls.
        self._earnings_date_cache: dict[str, Any] = {}   # ticker → date | None
        self._earnings_proximity_alerted: set[str] = set()  # tickers already alerted this session

    @property
    def readiness(self) -> LiveReadinessMeter:
        """Live Readiness Meter — CEO-controlled go-live gate."""
        return self._readiness

    @property
    def ctech(self) -> CTechAgent:
        return self._ctech

    async def run(self) -> None:
        """Main entry point — runs all agents concurrently."""
        self._running = True
        logger.info("AGORA session started: %s | mode=%s",
                    self._session_id, self._settings.trading_mode)

        # Re-seed pre-earnings dedup tracker from persisted positions (crash-restart safety)
        for pos in self._position_mgr.get_open_positions():
            if pos.is_pre_earnings and pos.earnings_date:
                self._pre_earnings_tickers[pos.ticker] = pos.earnings_date
        if self._pre_earnings_tickers:
            logger.info(
                "Restored pre-earnings dedup tracker from DB: %s",
                {t: str(d) for t, d in self._pre_earnings_tickers.items()}
            )

        # Sync shadow book against TWS fills BEFORE any agent starts trading.
        # GTC profit-targets can fill while the session is down; this catches them.
        await self._sync_positions_with_tws()

        # Auto-reset kill switch if it was tripped on a prior day (don't carry over daily losses).
        self._maybe_reset_kill_switch()

        # Alert CEO about open positions with earnings inside the blackout window
        # that are NOT marked is_pre_earnings (i.e., won't be auto-closed by T-1 logic).
        asyncio.create_task(self._earnings_proximity_startup_check())

        # Run a macro synthesis immediately on startup so the readiness meter shows
        # macro_context_available=True after restart (not just after the 7 AM window).
        asyncio.create_task(self._premarket_macro_scan(reason="startup"))

        await asyncio.gather(
            self._catalyst_agent.start(),
            self._earnings_agent.start(),
            self._earnings_calendar.start(),
            self._smart_money.start(),
            self._ibkr_news.start(),
            self._position_mgr.start(),
            self._ceo.start(),
            self._circuit_breaker.start(),
            self._premarket_setup.start(),
            self._sector_intel.start(),
            self._market_interest.start(),
            self._universe_disc.start(),
            self._system_health.start(),
            self._analyst_rev.start(),
            # Board-approved ops agents
            self._orphan_reconciler.start(),   # runs immediately on startup (Error 201 fix)
            self._exec_quality.start(),
            self._data_integrity.start(),
            self._pillar_health.start(),
            self._ibkr_agent.start(),          # IBKR expert — scans every 30 min
            # C-suite executives (including new CTechAgent)
            self._cro.start(),
            self._cio.start(),
            self._cto.start(),
            self._coo.start(),
            self._cfo.start(),
            self._rnd.start(),
            self._ctech.start(),
            self._session_loop(),
            self._price_monitor_loop(),
        )

    async def stop(self) -> None:
        self._running = False
        await asyncio.gather(
            self._catalyst_agent.stop(),
            self._earnings_agent.stop(),
            self._earnings_calendar.stop(),
            self._smart_money.stop(),
            self._ibkr_news.stop(),
            self._position_mgr.stop(),
            self._ceo.stop(),
            self._circuit_breaker.stop(),
            self._premarket_setup.stop(),
            self._sector_intel.stop(),
            self._market_interest.stop(),
            self._universe_disc.stop(),
            self._system_health.stop(),
            self._analyst_rev.stop(),
            self._orphan_reconciler.stop(),
            self._exec_quality.stop(),
            self._data_integrity.stop(),
            self._pillar_health.stop(),
            self._ibkr_agent.stop(),
            # C-suite executives (including CTechAgent)
            self._cro.stop(),
            self._cio.stop(),
            self._cto.stop(),
            self._coo.stop(),
            self._cfo.stop(),
            self._rnd.stop(),
            self._ctech.stop(),
        )
        logger.info("AGORA session stopped: %s", self._session_id)

    def _on_position_closed_feedback(
        self,
        ticker: str,
        realized_pnl: float,
        conviction_at_entry: float | None = None,
        macro_stance_at_entry: str | None = None,
        strategy: str | None = None,
    ) -> None:
        """
        Called by PositionManager whenever a position is closed (any source).
        Routes feedback to CEO's gate calibration ledger.
        Synchronous — safe to call from both async and sync contexts.
        """
        try:
            self._ceo.process_closed_trade_feedback(
                ticker=ticker,
                realized_pnl=realized_pnl,
                conviction_at_entry=conviction_at_entry,
                macro_stance_at_entry=macro_stance_at_entry,
                strategy=strategy,
            )
        except Exception as exc:
            logger.debug("_on_position_closed_feedback: %s", exc)

    # ── Session loop (market-hours intelligence cycle) ─────────────

    async def _session_loop(self) -> None:
        """
        Runs the scheduled intelligence tasks:
        - 7:00 AM ET:  pre-market macro synthesis
        - 9:30–15:30:  universe scan every 30 min during market hours
        - 4:05 PM ET:  after-hours attribution report
        - Every cycle:  pre-earnings IV crush close check (T-1 close)

        Cold-start recovery: if the session starts during market hours without
        premarket synthesis (restart after 7 AM), run it immediately so the
        conviction scorer has macro context on the first scan.
        """
        # ── Cold-start catch-up ────────────────────────────────────────
        now_et = datetime.now(tz=ET)
        if 9 <= now_et.hour < 16 and self._last_macro_spy is None:
            logger.info(
                "Session loop: cold-start during market hours — running premarket synthesis now"
            )
            try:
                await self._premarket_macro_scan()
            except Exception as exc:
                logger.error("Cold-start premarket synthesis failed: %s", exc)

        while self._running:
            now_et = datetime.now(tz=ET)
            hour, minute = now_et.hour, now_et.minute

            try:
                if hour == 7 and minute == 0:
                    await self._premarket_macro_scan()
                elif (9 <= hour < 16) and minute % 30 == 0:
                    if not (hour == 9 and minute == 0):
                        await self._universe_scan()
                elif hour == 16 and minute == 5:
                    await self._afterhours_report()

                # Every cycle during market hours: check if pre-earnings positions need closing
                if 9 <= hour < 16:
                    await self._check_pre_earnings_closes()
            except Exception as exc:
                logger.error("Session loop error: %s", exc)

            await asyncio.sleep(60)

    # ── Pre-earnings IV crush close ────────────────────────────────

    async def _check_pre_earnings_closes(self) -> None:
        """
        Close pre-earnings positions on T-1 (the trading day before earnings).

        Why T-1 and not T-0: earnings are usually reported after-hours. If we hold
        into the announcement, IV collapses from ~80% to ~30% regardless of direction
        (IV crush). A debit spread that's directionally correct still loses value if
        IV drops faster than intrinsic value gains. Close T-1 to capture IV expansion
        while it's still elevated, before the crush.
        """
        from datetime import date, timedelta
        today = date.today()
        positions = self._position_mgr.get_open_positions()

        for pos in positions:
            if not pos.is_pre_earnings or pos.earnings_date is None:
                continue
            days_to_earnings = (pos.earnings_date - today).days
            if days_to_earnings <= 1:
                reason = (
                    f"Pre-earnings IV crush protection: earnings on {pos.earnings_date} "
                    f"(T-{days_to_earnings}). Closing to capture IV expansion before crush."
                )
                logger.info("IV crush close: %s | %s", pos.ticker, reason)
                # Remove from dedup tracker
                self._pre_earnings_tickers.pop(pos.ticker, None)
                await self._execute_close(pos, reason)

    # ── Earnings proximity alert ───────────────────────────────────

    async def _get_next_earnings(self, ticker: str) -> "date | None":
        """Fetch and cache next earnings date from yfinance (one call per ticker per session)."""
        if ticker in self._earnings_date_cache:
            return self._earnings_date_cache[ticker]
        try:
            from datetime import date as _date
            import yfinance as yf

            def _fetch():
                cal = yf.Ticker(ticker).calendar
                if not cal or not isinstance(cal, dict):
                    return None
                raw = cal.get("Earnings Date", [])
                if not raw:
                    return None
                today = _date.today()
                future = []
                for d in (raw if hasattr(raw, "__iter__") and not isinstance(raw, str) else [raw]):
                    if hasattr(d, "date"):
                        d = d.date()
                    if isinstance(d, _date) and d >= today:
                        future.append(d)
                return min(future) if future else None

            result = await asyncio.to_thread(_fetch)
        except Exception:
            result = None
        self._earnings_date_cache[ticker] = result
        return result

    async def _earnings_proximity_startup_check(self) -> None:
        """
        On session start, alert CEO about any open position whose next earnings
        is within the blackout window AND is not already tracked for auto-close.
        This catches catalyst plays (e.g. CSCO) that were entered without is_pre_earnings=True.
        """
        from datetime import date
        today = date.today()
        blackout = self._settings.earnings_blackout_days
        positions = self._position_mgr.get_open_positions()
        for pos in positions:
            if pos.is_pre_earnings:
                continue
            earnings_dt = await self._get_next_earnings(pos.ticker)
            if earnings_dt is None:
                continue
            days = (earnings_dt - today).days
            if 0 <= days <= blackout:
                self._earnings_proximity_alerted.add(pos.ticker)
                logger.warning(
                    "Earnings proximity: %s reports in %dd (%s) — position not auto-managed",
                    pos.ticker, days, earnings_dt,
                )
                await self._ceo.dispatch_alert(
                    "warning",
                    f"⚠️ EARNINGS PROXIMITY: {pos.ticker} reports in {days}d ({earnings_dt}). "
                    f"This position was NOT entered as a pre-earnings play and will NOT be "
                    f"auto-closed for IV crush. Review and close manually before the announcement "
                    f"if it's a long-vega (debit spread) position.",
                )

    # ── Pre-market macro synthesis ─────────────────────────────────

    async def _premarket_macro_scan(self, reason: str = "7:00 AM schedule") -> None:
        if self._synthesis_in_progress:
            logger.debug("Macro synthesis already in progress — skipping duplicate (%s)", reason)
            return
        self._synthesis_in_progress = True
        logger.info("Macro synthesis starting — trigger: %s", reason)
        import time as _t
        _scan_start = _t.monotonic()
        try:
            import yfinance as yf

            def _fast_price(sym: str, fallback: float) -> float:
                try:
                    fi = yf.Ticker(sym).fast_info
                    p = float(getattr(fi, "last_price", None) or fi.get("lastPrice", 0) or 0)
                    return p if p > 0 else fallback
                except Exception:
                    return fallback

            # Run blocking yfinance calls in thread so the event loop stays free.
            vix, vix3m = await asyncio.gather(
                asyncio.to_thread(_fast_price, "^VIX",  20.0),
                asyncio.to_thread(_fast_price, "^VIX3M", 20.0),
            )
            logger.info("Macro VIX fetch: %.1fs | VIX=%.2f VIX3M=%.2f", _t.monotonic()-_scan_start, vix, vix3m)
            spy_info = {}  # no longer needed for price; snapshot covers it

            from trading_platform.services.market_data.yfinance_provider import YFinanceProvider
            provider = YFinanceProvider()
            _t1 = _t.monotonic()
            try:
                spy_snap = await provider.get_snapshot("SPY")
                logger.info("Macro SPY snapshot: %.1fs", _t.monotonic()-_t1)
            except Exception as snap_exc:
                logger.warning("SPY snapshot failed during macro scan: %s — using defaults", snap_exc)
                # Use a minimal synthetic snapshot so synthesis can still run
                from trading_platform.core.models.market import MarketSnapshot
                spy_snap = MarketSnapshot(
                    ticker="SPY", timestamp=datetime.now(tz=ET).replace(tzinfo=None),
                    price=0.0, volume=0, vwap=None,
                    vix=vix, iv_rank=None, iv_percentile=None, hist_vol_30=None,
                    rsi_14=None, sma_20=None, sma_50=None, sma_200=None,
                    atr_14=None, bars_daily=[],
                )

            regime_result = self._vol_classifier.classify(
                iv_rank=spy_snap.iv_rank,
                vix=vix,
                vix3m=vix3m,
                hv10=None,
                hv30=None,
                spy_rsi=spy_snap.rsi_14,
                atm_iv=vix / 100,            # VIX is the ATM IV proxy for SPY
                hv21=spy_snap.hist_vol_30,
            )

            cal = self._event_engine._cal
            days_fomc, fomc_evt = cal.days_to_next_event(datetime.now(tz=ET).date())
            days_cpi = None
            if fomc_evt and "cpi" in str(fomc_evt).lower():
                days_cpi = days_fomc
                days_fomc = None

            self._macro_context = await self._macro.synthesize(
                regime=regime_result["regime"],
                iv_rank=spy_snap.iv_rank,
                vix=vix,
                vix_vix3m=vix / vix3m if vix3m > 0 else None,
                spy_rsi=spy_snap.rsi_14,
                days_to_fomc=days_fomc,
                days_to_cpi=days_cpi,
            )
            # synthesize() always returns a MacroContext (real or fallback) — this should never be None

            # Record features for PSI monitoring
            self._psi.record({
                "iv_rank":         spy_snap.iv_rank,
                "vix":             vix,
                "vix_vix3m_ratio": vix / vix3m if vix3m > 0 else None,
                "spy_rsi":         spy_snap.rsi_14,
            })

            # Save baseline for intraday refresh detection
            self._last_macro_spy = spy_snap.price or 0.0
            self._last_macro_vix = vix
            self._last_macro_refresh_et = datetime.now(tz=ET)

            logger.info(
                "Macro context: stance=%s | vol_selling_ok=%s | size_bias=%s",
                self._macro_context.macro_stance,
                self._macro_context.vol_selling_ok,
                self._macro_context.size_bias,
            )
        except Exception as exc:
            logger.error("Pre-market scan failed: %s", exc)
        finally:
            self._synthesis_in_progress = False

    # ── Universe scan ──────────────────────────────────────────────

    async def _price_monitor_loop(self) -> None:
        """
        Lightweight price + volume monitor — runs every 5 minutes during market hours.

        Downloads all tickers in a single yfinance batch call (~1s).
        Promotes tickers to _priority_queue when:
          - Price moves > 1.5% vs 30 min ago
          - Volume > 2× the 5-bar rolling average
        These tickers jump ahead of the Tier 2 rotation in the next _universe_scan().
        """
        while self._running:
            now_et = datetime.now(tz=ET)
            hour = now_et.hour
            if not (9 <= hour < 16):
                await asyncio.sleep(60)
                continue

            try:
                import yfinance as yf
                universe = self._settings.etf_universe

                def _download_batch() -> Any:
                    return yf.download(
                        " ".join(universe),
                        period="1d",
                        interval="5m",
                        auto_adjust=True,
                        progress=False,
                        threads=True,
                    )

                raw = await asyncio.to_thread(_download_batch)
                if raw.empty:
                    await asyncio.sleep(300)
                    continue

                close = raw["Close"] if "Close" in raw else raw.xs("Close", axis=1, level=0)
                volume = raw["Volume"] if "Volume" in raw else raw.xs("Volume", axis=1, level=0)

                promoted = []
                for ticker in universe:
                    if ticker not in close.columns:
                        continue
                    prices = close[ticker].dropna()
                    vols   = volume[ticker].dropna()
                    if len(prices) < 7:
                        continue

                    current_price = float(prices.iloc[-1])
                    prev_price    = float(prices.iloc[-7])   # ~30 min ago (6 × 5m bars)
                    pct_move      = abs(current_price - prev_price) / prev_price * 100

                    avg_vol    = float(vols.iloc[-6:-1].mean()) if len(vols) >= 6 else 0
                    latest_vol = float(vols.iloc[-1])
                    vol_spike  = (latest_vol > avg_vol * 2.0) if avg_vol > 0 else False

                    trigger = None
                    if pct_move >= 1.5:
                        trigger = f"move {pct_move:.1f}% in 30m"
                    elif vol_spike:
                        trigger = f"vol spike {latest_vol/avg_vol:.1f}× avg"

                    if trigger and ticker not in self._priority_queue and ticker not in self._tier1:
                        self._priority_queue.append(ticker)
                        self._priority_reasons[ticker] = trigger
                        promoted.append(f"{ticker}({trigger})")

                    self._last_prices[ticker] = current_price

                if promoted:
                    logger.info("Price monitor promoted: %s", ", ".join(promoted))

                # ── Intraday macro refresh check ───────────────────
                await self._check_macro_refresh(close)

            except Exception as exc:
                logger.debug("Price monitor error: %s", exc)

            await asyncio.sleep(300)   # run every 5 minutes

    _MACRO_REFRESH_SPY_THRESHOLD = 0.015   # SPY move ≥ 1.5% from last synthesis
    _MACRO_REFRESH_VIX_THRESHOLD = 0.03    # VIX move ≥ 3% from last synthesis
    _MACRO_REFRESH_COOLDOWN_MIN  = 120     # minimum minutes between refreshes (≤3 intraday calls)

    async def _check_macro_refresh(self, close_df: Any) -> None:
        """
        Trigger an intraday macro re-synthesis when the regime shifts materially:
          - SPY moves ≥ 1.5% since last synthesis, OR
          - VIX moves ≥ 3% since last synthesis

        Respects a 60-minute cooldown to avoid thrashing Claude during volatile days.
        No-ops if macro was never synthesized (relies on pre-market scan running first).
        """
        if self._last_macro_spy is None or self._last_macro_vix is None:
            return

        now_et = datetime.now(tz=ET)

        # Enforce cooldown
        if self._last_macro_refresh_et is not None:
            mins_since = (now_et - self._last_macro_refresh_et).total_seconds() / 60
            if mins_since < self._MACRO_REFRESH_COOLDOWN_MIN:
                return

        try:
            import yfinance as yf

            # SPY current price from the batch df already in memory
            spy_current: float | None = None
            if "SPY" in close_df.columns:
                spy_series = close_df["SPY"].dropna()
                if not spy_series.empty:
                    spy_current = float(spy_series.iloc[-1])

            # VIX requires a separate fetch (not in the equities batch)
            try:
                fi_vix = yf.Ticker("^VIX").fast_info
                vix_current = float(getattr(fi_vix, "last_price", None) or fi_vix.get("lastPrice", 0) or 0)
                if vix_current <= 0:
                    vix_current = self._last_macro_vix or 20.0
            except Exception:
                vix_current = self._last_macro_vix or 20.0

            spy_move = abs(spy_current - self._last_macro_spy) / self._last_macro_spy if spy_current else 0.0
            vix_move = abs(vix_current - self._last_macro_vix) / self._last_macro_vix if self._last_macro_vix else 0.0

            trigger: str | None = None
            if spy_move >= self._MACRO_REFRESH_SPY_THRESHOLD:
                trigger = f"SPY move {spy_move * 100:.1f}% since last synthesis"
            elif vix_move >= self._MACRO_REFRESH_VIX_THRESHOLD:
                trigger = f"VIX move {vix_move * 100:.1f}% since last synthesis"

            if trigger:
                logger.info("Intraday macro refresh triggered — %s", trigger)
                await self._premarket_macro_scan(reason=f"intraday refresh: {trigger}")

        except Exception as exc:
            logger.debug("Macro refresh check error: %s", exc)

    async def _universe_scan(self) -> None:
        """
        Event-driven tiered universe scan.

        Every 30-min cycle scans:
          1. Tier 1 (12 core names) — always, every cycle
          2. Priority queue — tickers flagged by price/volume monitor
          3. Tier 2 rotation — remaining tickers in rotating batches of 8

        This ensures fast movers are never more than 5 minutes from re-evaluation
        while the full universe still cycles through across the trading day.
        """
        # Merge static universe + dynamically discovered tickers
        dynamic   = self._universe_disc.get_dynamic_tickers()
        universe  = list(dict.fromkeys(self._settings.etf_universe + dynamic))  # dedup, preserve order
        tier2     = [t for t in universe if t not in self._tier1]
        batch_size = 8

        # Build this scan's ticker list: Tier1 + priority queue + Tier2 rotation
        priority_batch = list(self._priority_queue)
        self._priority_queue.clear()

        tier2_batch = (tier2 * 2)[self._scan_index: self._scan_index + batch_size]
        self._scan_index = (self._scan_index + batch_size) % len(tier2)

        # Deduplicate — priority tickers may overlap with Tier 1 or Tier 2 rotation
        seen: set[str] = set()
        batch: list[str] = []
        for t in self._tier1 + priority_batch + tier2_batch:
            if t not in seen:
                seen.add(t)
                batch.append(t)

        # Also inject top market-interest tickers not already in batch
        interest_top = [t for t, _ in self._market_interest.get_top_interest_tickers(n=5)
                        if t not in seen]
        for t in interest_top:
            seen.add(t)
            batch.append(t)
            priority_labels_extra = {t: " [MARKET_INTEREST]" for t in interest_top}
        else:
            priority_labels_extra = {}

        priority_labels = {t: f" [PRIORITY: {self._priority_reasons.pop(t, 'triggered')}]"
                           for t in priority_batch}
        priority_labels.update(priority_labels_extra)

        logger.info(
            "Universe scan: %d tickers | tier1=%d priority=%d tier2_batch=%d dynamic=%d interest=%d",
            len(batch), len(self._tier1), len(priority_batch), len(tier2_batch),
            len(dynamic), len(interest_top),
        )

        for ticker in batch:
            label = priority_labels.get(ticker, "")
            try:
                if label:
                    logger.info("Scanning %s%s", ticker, label)
                await self._evaluate_ticker(ticker)
                await asyncio.sleep(1)
            except Exception as exc:
                logger.error("Evaluate ticker %s failed: %s", ticker, exc)

    async def _evaluate_ticker(self, ticker: str) -> None:
        """Full signal stack for one ticker → trade recommendation → risk gate → order."""
        try:
            # Earnings interlock: skip if we already have a pre-earnings position
            # to prevent double-entry, and block vol-premium entry within blackout window.
            if ticker in self._pre_earnings_tickers:
                logger.debug(
                    "Skip %s: pre-earnings position already active (earnings %s)",
                    ticker, self._pre_earnings_tickers[ticker],
                )
                return

            from trading_platform.services.market_data.yfinance_provider import YFinanceProvider
            from trading_platform.services.options_flow import get_gex

            provider = YFinanceProvider()
            snap = await provider.get_snapshot(ticker)
            if not snap or not snap.price:
                return

            # IV premium signal — use VIX/100 as ATM IV proxy; hist_vol_30 as realized vol
            atm_iv_proxy = (snap.vix / 100) if (snap.vix is not None and snap.vix > 0) else (snap.hist_vol_30 * 1.25 if snap.hist_vol_30 else None)
            iv_signal = self._iv_screen.check(ticker, atm_iv_proxy, snap.hist_vol_30)

            # GEX signal
            gex_raw = get_gex(ticker, snap.price)

            # Regime signal (reuse macro context regime)
            from agora.core.models import GexRegime, GexSignal, IvPremiumSignal, Regime, VolRegimeSignal
            _STANCE_TO_REGIME = {
                "risk_on":  Regime.LOW_VOL,
                "neutral":  Regime.NORMAL,
                "risk_off": Regime.HIGH_VOL,
            }
            gex = GexSignal(
                ticker=ticker,
                gex_total=gex_raw.get("gex_total", 0.0),
                regime=GexRegime(gex_raw.get("regime", "neutral")),
                dominant_strike=gex_raw.get("dominant_strike"),
                flip_level=gex_raw.get("flip_level"),
            ) if gex_raw else None

            iv_prem = IvPremiumSignal(
                ticker=ticker,
                atm_iv_30d=iv_signal.get("atm_iv_pct", 0) / 100,
                hv_21d=iv_signal.get("hv_21d_pct", 0) / 100,
                premium_ratio=iv_signal.get("premium_ratio") or 0.0,
                days_above_threshold=iv_signal.get("days_above_threshold", 0),
                signal_active=iv_signal.get("signal_active", False),
            ) if iv_signal else None

            # Event pattern
            today = datetime.now(tz=ET).date()
            event_signals = self._event_engine.get_signals(ticker, today)
            event = None
            if event_signals:
                es = event_signals[0]
                from agora.core.models import EventSignal
                event = EventSignal(
                    event_type=es["event_type"],
                    ticker=ticker,
                    days_to_event=es.get("days_to_event", 5),
                    direction=es.get("direction", "neutral"),
                    confidence=es.get("confidence", 0.5),
                )

            # Build macro signal input
            macro_input = SignalInput(
                source="macro",
                direction=self._macro_context.macro_stance if self._macro_context else "neutral",
                confidence=self._macro_context.confidence if self._macro_context else 0.5,
            )

            # Microstructure signal
            micro_dir = "neutral"
            micro_conf = 0.5
            if gex and gex.regime == GexRegime.NEGATIVE and snap.rsi_14 and snap.rsi_14 > 55:
                micro_dir = "bullish"
                micro_conf = 0.65
            elif iv_prem and iv_prem.signal_active:
                micro_conf = 0.70

            micro_input = SignalInput(
                source="microstructure",
                direction=micro_dir,
                confidence=micro_conf,
            )

            # Score conviction
            from agora.core.models import VolRegimeSignal as VRS
            regime_signal = VRS(
                regime=_STANCE_TO_REGIME.get(
                    self._macro_context.macro_stance if self._macro_context else "neutral",
                    Regime.NORMAL,
                ),
                confidence=self._macro_context.confidence if self._macro_context else 0.5,
                iv_rank=snap.iv_rank,
                vix=None,
            ) if self._macro_context else None

            # Market interest score — fetched before scorer so it's a proper weighted component
            mi_score = self._market_interest.get_interest_score(ticker)

            conviction = self._scorer.score(
                ticker=ticker,
                session_id=self._session_id,
                iv_premium=iv_prem,
                gex=gex,
                regime=regime_signal,
                macro=self._macro_context,
                event=event,
                catalyst=None,
                market_interest=mi_score,
            )

            # Sector intelligence read-through — adjust conviction based on peer data
            sector_intel = self._sector_intel.get_intelligence(ticker)
            if sector_intel and sector_intel.read_through_confidence >= 0.65:
                # Boost conviction when peers confirm direction
                intel_dir = sector_intel.read_through_direction
                macro_dir = self._macro_context.macro_stance if self._macro_context else "neutral"
                if intel_dir == "bullish" and macro_dir in ("risk_on", "neutral"):
                    conviction.total_score = min(conviction.total_score * 1.10, 95.0)
                    logger.debug("Sector intel boost for %s: %.0f → %.0f", ticker,
                                 conviction.total_score / 1.10, conviction.total_score)
                elif intel_dir == "bearish" and macro_dir in ("risk_off", "neutral"):
                    conviction.total_score = min(conviction.total_score * 1.10, 95.0)

            # Record pillar health signals for liveness tracking
            macro_dir = (self._macro_context.macro_stance if self._macro_context else "neutral")
            self._pillar_health.record_pillar_signal(
                "macro", "bullish" if "risk_on" in macro_dir else ("bearish" if "risk_off" in macro_dir else "neutral"),
                ticker=ticker, score=conviction.total_score,
            )
            if sector_intel:
                self._pillar_health.record_pillar_signal(
                    "sector_momentum", sector_intel.read_through_direction,
                    ticker=ticker, score=sector_intel.read_through_confidence * 100,
                )
            if mi_score and mi_score.score > 0:
                self._pillar_health.record_pillar_signal(
                    "market_interest",
                    "bullish" if mi_score.score >= 6.0 else ("bearish" if mi_score.score <= 2.0 else "neutral"),
                    ticker=ticker, score=mi_score.score * 10,
                )

            # Resolve disagreement → size multiplier
            resolution = self._resolver.resolve(
                macro=macro_input,
                microstructure=micro_input,
                catalyst=None,
                regime=self._macro_context.macro_stance if self._macro_context else "normal",
                total_conviction=conviction.total_score,
            )

            if resolution["gate"] == "no_trade":
                # Vol-premium bypass: when IV rank is elevated and macro allows selling,
                # sell premium non-directionally. Conviction floor still enforced.
                ivr_threshold = self._settings.ivr_bypass_threshold
                iv_rank_elevated = snap.iv_rank is not None and snap.iv_rank >= ivr_threshold
                conviction_ok = conviction.total_score >= self._settings.min_conviction_score
                vol_selling_ok = (
                    iv_rank_elevated
                    and conviction_ok
                    and self._macro_context is not None
                    and self._macro_context.vol_selling_ok
                    # DataIntegrityAgent gate: if IVR feed is degraded, bypass is blocked
                    and self._data_integrity.vol_bypass_allowed()
                )
                if not vol_selling_ok:
                    if iv_rank_elevated and not self._data_integrity.vol_bypass_allowed():
                        logger.info(
                            "No trade for %s: IVR feed degraded — vol bypass blocked",
                            ticker,
                        )
                    elif iv_rank_elevated and not conviction_ok:
                        logger.info(
                            "No trade for %s: vol bypass blocked — conviction %.0f < %.0f floor",
                            ticker, conviction.total_score, self._settings.min_conviction_score,
                        )
                    else:
                        logger.info("No trade for %s: %s (IVR=%s)",
                                    ticker, resolution["reason"],
                                    f"{snap.iv_rank:.0f}" if snap.iv_rank else "n/a")
                    return
                # Override gate for vol-premium play
                conviction.pillar = StrategyPillar.VOL_PREMIUM
                conviction.size_multiplier = 0.75
                conviction.gate = "standard"
                conviction.reasoning = (
                    f"Vol premium | IVR={snap.iv_rank:.0f} (≥{ivr_threshold:.0f}) | "
                    f"conviction={conviction.total_score:.0f} | macro vol_selling_ok=True"
                )
                logger.info(
                    "Vol premium bypass for %s: IVR=%.0f conviction=%.0f | macro allows selling",
                    ticker, snap.iv_rank, conviction.total_score,
                )
            else:
                conviction.size_multiplier = resolution["size_multiplier"]
                conviction.gate = resolution["gate"]

            # Get options chain and build recommendation.
            # Load one expiry per DTE bracket — wrapped in to_thread so the
            # blocking yfinance calls don't stall the event loop.
            import yfinance as yf
            from datetime import date as _date
            from trading_platform.services.market_data.yfinance_provider import _YF_OPTIONS_LOCK
            _DTE_BRACKETS = [(5, 22), (23, 37), (38, 65), (66, 90)]

            def _fetch_chains_sync(t: str) -> dict:
                chain_dict: dict = {}
                with _YF_OPTIONS_LOCK:
                    tk = yf.Ticker(t)
                    exps = tk.options or []
                    today_d = _date.today()
                    filled: set[int] = set()
                    for exp in exps:
                        try:
                            exp_date = _date.fromisoformat(exp)
                        except ValueError:
                            continue
                        dte = (exp_date - today_d).days
                        for i, (lo, hi) in enumerate(_DTE_BRACKETS):
                            if i not in filled and lo <= dte <= hi:
                                try:
                                    c = tk.option_chain(exp)
                                    chain_dict[exp] = {"calls": c.calls, "puts": c.puts}
                                    filled.add(i)
                                except Exception:
                                    pass
                                break
                        if len(filled) == len(_DTE_BRACKETS):
                            break
                return chain_dict

            try:
                chain_dict = await asyncio.wait_for(
                    asyncio.to_thread(_fetch_chains_sync, ticker), timeout=45.0
                )
            except asyncio.TimeoutError:
                logger.warning("Options chain fetch timed out for %s — skipping ticker", ticker)
                return
            if not chain_dict:
                logger.info("No options chain data for %s — skipping", ticker)
                return

            logger.info("Options chain loaded for %s: %d expiries %s",
                        ticker, len(chain_dict), list(chain_dict.keys()))

            recommendation = self._strategy.build_recommendation(
                conviction=conviction,
                spot=snap.price,
                options_chain=chain_dict,
                gex=gex,
            )

            if not recommendation:
                logger.info("No recommendation built for %s (strategy returned None)", ticker)
                return

            await self._submit_recommendation(recommendation, ticker, snap.price)

        except Exception as exc:
            logger.error("Evaluate ticker %s failed: %s", ticker, exc)

    # ── Catalyst callback ──────────────────────────────────────────

    # ETFs in the universe that should not be traded via single-stock catalyst signals.
    # They may still appear in IBKR news feeds (e.g., SPY "partnership") — skip them.
    _CATALYST_ETF_SKIP = frozenset(["SPY", "QQQ", "IWM", "GLD", "TLT", "SLV",
                                     "COPX", "PPLT", "XLE", "XLF", "XLK", "XBI"])

    async def _on_catalyst(self, catalyst: Catalyst) -> None:
        """Called by discovery agents for real-time catalyst trades."""
        if self._risk.is_kill_switch_active():
            return

        # Skip broad-market ETFs — their IBKR news is macro, not single-stock catalyst
        if catalyst.ticker in self._CATALYST_ETF_SKIP:
            logger.debug("CATALYST skip: %s is macro ETF — not a single-stock trade", catalyst.ticker)
            return

        # Dedup: skip if we already have an active pre-earnings setup for this ticker.
        # Pre-earnings setups are managed by EarningsCalendarAgent and will auto-close
        # T-1 — a concurrent catalyst trade would double our exposure and risk.
        if catalyst.ticker in self._pre_earnings_tickers:
            logger.info(
                "CATALYST dedup: %s already has pre-earnings setup (earnings=%s) — skipping",
                catalyst.ticker, self._pre_earnings_tickers[catalyst.ticker]
            )
            return

        # Dedup: skip if we already have an open position for this ticker.
        # The same catalyst news often fires through multiple EDGAR / IBKR / Smart-Money
        # discovery paths — only the first one that builds a position should win.
        open_tickers = {p.ticker for p in self._position_mgr.get_open_positions()}
        if catalyst.ticker in open_tickers:
            logger.info(
                "CATALYST dedup: %s already has an open position — skipping",
                catalyst.ticker,
            )
            return

        # Dedup: 30-minute cooldown per (ticker, catalyst_type).
        # Same story arrives from multiple IBKR providers (DJ-N, DJ-RTG, DJ-RTPRO)
        # with distinct articleIds — the base-ID split can't catch cross-provider dupes.
        cooldown_key = (catalyst.ticker, str(catalyst.catalyst_type))
        now_utc = datetime.now(tz=timezone.utc)
        last_emit = self._catalyst_cooldown.get(cooldown_key)
        if last_emit and (now_utc - last_emit).total_seconds() < self._CATALYST_COOLDOWN_SECS:
            logger.info(
                "CATALYST cooldown: %s/%s emitted %.0fs ago — skipping duplicate",
                catalyst.ticker, catalyst.catalyst_type,
                (now_utc - last_emit).total_seconds(),
            )
            return
        self._catalyst_cooldown[cooldown_key] = now_utc
        # Evict stale entries to prevent unbounded growth
        cutoff = now_utc.timestamp() - self._CATALYST_COOLDOWN_SECS
        self._catalyst_cooldown = {
            k: v for k, v in self._catalyst_cooldown.items()
            if v.timestamp() >= cutoff
        }

        logger.info("Processing catalyst: %s | %s | %s",
                    catalyst.ticker, catalyst.catalyst_type, catalyst.direction)

        # Record catalyst pillar as alive
        self._pillar_health.record_pillar_signal(
            "catalyst", catalyst.direction, ticker=catalyst.ticker,
            score=75.0 if catalyst.strength == "strong" else 55.0,
        )

        # Minimal signal stack for catalyst plays
        from agora.core.models import ConvictionScore, StrategyPillar
        conviction = ConvictionScore(
            session_id=self._session_id,
            ticker=catalyst.ticker,
            total_score=65.0,   # catalyst trades start at 65 — info speed adds more
            info_speed_score=self._scorer._score_info_speed(catalyst),
            gate="standard",
            pillar=StrategyPillar.CATALYST,
            reasoning=f"Catalyst: {catalyst.catalyst_type} | {catalyst.direction}",
        )
        conviction.size_multiplier = 1.0

        # Get options chain and spot — blocking yfinance in thread pool
        try:
            import yfinance as yf
            from trading_platform.services.market_data.yfinance_provider import _YF_OPTIONS_LOCK

            def _fetch_catalyst_data(t: str) -> tuple[dict, float]:
                _chain: dict = {}
                with _YF_OPTIONS_LOCK:
                    tk = yf.Ticker(t)
                    for exp in (tk.options or [])[:4]:
                        try:
                            c = tk.option_chain(exp)
                            _chain[exp] = {"calls": c.calls, "puts": c.puts}
                        except Exception:
                            continue
                    try:
                        fi = tk.fast_info
                        _spot = float(getattr(fi, "last_price", None) or fi.get("lastPrice", 0) or 0)
                    except Exception:
                        _spot = 0.0
                    if _spot <= 0:
                        info = tk.info or {}
                        _spot = float(info.get("regularMarketPrice") or info.get("currentPrice") or 0)
                return _chain, _spot

            try:
                chain_dict, spot = await asyncio.wait_for(
                    asyncio.to_thread(_fetch_catalyst_data, catalyst.ticker), timeout=45.0
                )
            except asyncio.TimeoutError:
                logger.warning("Options chain fetch timed out for catalyst %s — skipping", catalyst.ticker)
                return
            if not chain_dict or spot <= 0:
                return

            recommendation = self._strategy.build_recommendation(
                conviction=conviction,
                spot=spot,
                options_chain=chain_dict,
                direction_override=catalyst.direction,
            )

            if recommendation:
                await self._submit_recommendation(recommendation, catalyst.ticker, spot)
        except Exception as exc:
            logger.error("Catalyst trade build failed for %s: %s", catalyst.ticker, exc)

    async def _on_earnings_setup(self, setup: EarningsSetup) -> None:
        """
        Called by EarningsCalendarAgent T-1 to T-7 before earnings.
        Builds a pre-earnings options position (IV expansion + directional play).
        Only fires when edge_ratio > 1.2× (historical move beats implied move).
        """
        logger.info(
            "Pre-earnings setup: %s | %s T-%d | dir=%s | edge=%.2fx",
            setup.ticker, setup.earnings_date, setup.dte, setup.direction, setup.edge_ratio,
        )

        # Dedup: skip if we already have an active pre-earnings position for this ticker.
        # EarningsCalendarAgent fires every morning (T-7 to T-1) — only enter once.
        if setup.ticker in self._pre_earnings_tickers:
            logger.debug(
                "Pre-earnings dedup: %s already tracked (earnings=%s)",
                setup.ticker, self._pre_earnings_tickers[setup.ticker]
            )
            return

        # Also skip if there's already an open position for this ticker from any pillar
        open_tickers = {p.ticker for p in self._position_mgr.get_open_positions()}
        if setup.ticker in open_tickers:
            logger.info(
                "Pre-earnings %s: skipping — already have an open position",
                setup.ticker,
            )
            return

        # Register in dedup tracker — kept until position closes
        self._pre_earnings_tickers[setup.ticker] = setup.earnings_date

        # Inform CEO of upcoming setup
        self._ceo.register_earnings_setup(
            ticker=setup.ticker,
            earnings_date=setup.earnings_date,
            direction=setup.direction,
            confidence=setup.confidence,
            reasoning=setup.reasoning,
        )

        # Only trade if we have a directional view with sufficient confidence
        if setup.direction == "neutral" or setup.confidence < 0.60:
            logger.info(
                "Pre-earnings %s: skipping — direction=%s confidence=%.2f (need directional ≥0.60)",
                setup.ticker, setup.direction, setup.confidence,
            )
            return

        if self._risk.is_kill_switch_active():
            return

        try:
            import yfinance as yf
            from trading_platform.services.market_data.yfinance_provider import _YF_OPTIONS_LOCK
            from agora.core.models import ConvictionScore

            def _fetch_preearnings(t: str, initial_spot: float) -> tuple[dict, float]:
                _chain: dict = {}
                _spot = initial_spot
                with _YF_OPTIONS_LOCK:
                    tk = yf.Ticker(t)
                    if _spot <= 0:
                        try:
                            fi = tk.fast_info
                            _spot = float(getattr(fi, "last_price", None) or fi.get("lastPrice", 0) or 0)
                        except Exception:
                            _spot = 0.0
                    for exp in (tk.options or [])[:6]:
                        try:
                            c = tk.option_chain(exp)
                            _chain[exp] = {"calls": c.calls, "puts": c.puts}
                        except Exception:
                            continue
                return _chain, _spot

            try:
                chain_dict, spot = await asyncio.wait_for(
                    asyncio.to_thread(_fetch_preearnings, setup.ticker, setup.spot), timeout=45.0
                )
            except asyncio.TimeoutError:
                logger.warning("Options chain fetch timed out for pre-earnings %s — skipping", setup.ticker)
                return
            if not chain_dict or spot <= 0:
                return

            # Scale conviction from edge ratio and confidence
            # edge_ratio=1.2 → 65, edge_ratio=2.0 → 80
            base_score = min(65.0 + (setup.edge_ratio - 1.2) * 18.75, 85.0)
            conviction_score = base_score * setup.confidence

            conviction = ConvictionScore(
                session_id=self._session_id,
                ticker=setup.ticker,
                total_score=conviction_score,
                gate="standard",
                pillar=StrategyPillar.CATALYST,
                reasoning=(
                    f"Pre-earnings | T-{setup.dte} | edge={setup.edge_ratio:.2f}× | "
                    f"{setup.direction} | {setup.peer_context[:80]}"
                ),
            )
            conviction.size_multiplier = 0.75   # smaller size before the event

            recommendation = self._strategy.build_recommendation(
                conviction=conviction,
                spot=spot,
                options_chain=chain_dict,
                direction_override=setup.direction,
            )
            if recommendation:
                await self._submit_recommendation(
                    recommendation, setup.ticker, spot,
                    earnings_date=setup.earnings_date,
                    is_pre_earnings=True,
                )
        except Exception as exc:
            logger.error("Pre-earnings trade build failed for %s: %s", setup.ticker, exc)
            # Clean up dedup tracker on failure so we can retry next cycle
            self._pre_earnings_tickers.pop(setup.ticker, None)

    async def _on_position_alert(self, alert: PositionAlert) -> None:
        """Called by PreMarketSetupAgent when a position needs immediate attention."""
        logger.warning("Position alert [%s]: %s | action=%s",
                       alert.severity.upper(), alert.message, alert.recommended_action)
        if alert.severity == "critical" and self._ceo:
            await self._ceo.dispatch_alert(
                "warning",
                f"⚠️ Pre-market position alert: {alert.message}",
                {"recommended_action": alert.recommended_action,
                 "ticker": alert.ticker,
                 "overnight_move": f"{alert.overnight_move_pct*100:+.1f}%"},
            )

    async def _on_earnings(self, result: Any) -> None:
        """Called by EarningsTranscriptAgent with post-earnings analysis."""
        logger.info("Earnings result: %s | beat=%s | guide=%s",
                    result.ticker, result.beat_quality, result.guidance_tone)

        # Trigger analyst revision tracking in background (T+1 upgrade momentum)
        asyncio.create_task(self._analyst_rev.track_post_earnings(result.ticker))

        if self._risk.is_kill_switch_active():
            return

        signal = self._event_engine.get_post_earnings_signal(
            ticker=result.ticker,
            earnings_date=result.filing_date,
            beat_quality=result.beat_quality,
            guidance_tone=result.guidance_tone,
        )
        if not signal or signal.get("direction") == "neutral":
            return

        logger.info("Post-earnings skew signal: %s | dir=%s | conf=%.2f",
                    result.ticker, signal["direction"], signal["confidence"])

        try:
            import yfinance as yf
            from trading_platform.services.market_data.yfinance_provider import _YF_OPTIONS_LOCK
            from agora.core.models import ConvictionScore

            def _fetch_postearnings(t: str) -> tuple[dict, float]:
                _chain: dict = {}
                with _YF_OPTIONS_LOCK:
                    tk = yf.Ticker(t)
                    try:
                        fi = tk.fast_info
                        _spot = float(getattr(fi, "last_price", None) or fi.get("lastPrice", 0) or 0)
                    except Exception:
                        _spot = 0.0
                    for exp in (tk.options or [])[:6]:
                        try:
                            c = tk.option_chain(exp)
                            _chain[exp] = {"calls": c.calls, "puts": c.puts}
                        except Exception:
                            continue
                return _chain, _spot

            try:
                chain_dict, spot = await asyncio.wait_for(
                    asyncio.to_thread(_fetch_postearnings, result.ticker), timeout=45.0
                )
            except asyncio.TimeoutError:
                logger.warning("Options chain fetch timed out for post-earnings %s — skipping", result.ticker)
                return
            if not chain_dict or spot <= 0:
                return

            conviction = ConvictionScore(
                session_id=self._session_id,
                ticker=result.ticker,
                total_score=signal["confidence"] * 100,
                gate="standard",
                pillar=StrategyPillar.CATALYST,
                reasoning=f"Post-earnings skew reversion | beat={result.beat_quality}",
            )
            conviction.size_multiplier = 0.75

            recommendation = self._strategy.build_recommendation(
                conviction=conviction,
                spot=spot,
                options_chain=chain_dict,
                direction_override=signal["direction"],
            )
            if recommendation:
                await self._submit_recommendation(recommendation, result.ticker, spot)
        except Exception as exc:
            logger.error("Post-earnings trade build failed for %s: %s", result.ticker, exc)

    # ── Execution stubs (wired to IBKR in live mode) ──────────────

    async def _submit_recommendation(
        self,
        recommendation: Any,
        ticker: str,
        spot: float,
        earnings_date: Any = None,
        is_pre_earnings: bool = False,
    ) -> None:
        """Entry timing → compliance → risk council → circuit breaker → IBKR."""
        # 1. Hard gate: no new entries outside 10:00 AM – 3:30 PM ET
        permitted, timing_reason = self._entry_timing.is_entry_permitted()
        if not permitted:
            logger.info("ENTRY BLOCKED by timing gate: %s | %s", ticker, timing_reason)
            return

        # 1b. Macro calendar gate: block entries on FOMC/NFP/CPI avoid days
        _cal = get_macro_calendar()
        _can_trade, _cal_reason = _cal.should_trade()
        if not _can_trade:
            logger.info("ENTRY BLOCKED by macro calendar: %s | %s", ticker, _cal_reason)
            return
        _size_mult = _cal.position_size_multiplier()
        if _size_mult < 1.0:
            recommendation.contracts = max(1, int(recommendation.contracts * _size_mult))
            logger.info(
                "Macro calendar caution: %s size reduced to %.0f%% (%d contracts)",
                ticker, _size_mult * 100, recommendation.contracts,
            )

        # 2. VIX stress mode: apply circuit breaker size reduction
        if self._circuit_breaker.vix_stress_mode:
            recommendation.size_multiplier *= self._circuit_breaker.size_multiplier_override
            recommendation.contracts = max(1, int(recommendation.contracts * self._circuit_breaker.size_multiplier_override))
            logger.info("VIX stress mode: %s size reduced to %d contracts", ticker, recommendation.contracts)

        positions = self._position_mgr.get_open_positions()

        # 3. Compliance gate (wash sale advisory + strategy level + concentration)
        compliance_result = self._compliance.check_trade(recommendation, positions)
        if not compliance_result["compliant"]:
            logger.info("BLOCKED by compliance: %s | %s", ticker, compliance_result["reason"])
            return
        for warning in compliance_result.get("warnings", []):
            logger.warning("Compliance warning [%s]: %s", ticker, warning)

        # 4. Risk council
        greeks = self._position_mgr.get_portfolio_greeks()
        risk_result = self._risk.approve_trade(recommendation, greeks, positions, spot)
        if not risk_result["approved"]:
            logger.info("BLOCKED by risk council: %s | %s", ticker, risk_result["reason"])
            return

        strategy_str = getattr(recommendation, "strategy", "unknown")
        if hasattr(strategy_str, "value"):
            strategy_str = strategy_str.value
        mid_price = abs(recommendation.entry_debit_credit / max(1, recommendation.contracts * 100))

        # 4c. Execution cooldown gate — block re-submission of a ticker that recently failed to fill.
        #     Illiquid spreads can time out on every scan cycle without this guard.
        _cooldown_until = self._exec_cooldowns.get(ticker)
        if _cooldown_until and datetime.now(tz=timezone.utc) < _cooldown_until:
            _mins_left = (_cooldown_until - datetime.now(tz=timezone.utc)).seconds // 60
            logger.debug(
                "ENTRY SKIPPED by exec cooldown: %s — %d min remaining after prior timeout",
                ticker, _mins_left,
            )
            return

        self._exec_quality.record_attempt(ticker, str(strategy_str), mid_price)

        # 4b. Open combo order gate — IBKR paper limits riskless-combination orders (Error 201)
        #     Each open position has a live GTC profit-target (counts against the limit).
        open_positions = self._position_mgr.get_open_positions()
        if len(open_positions) >= self._settings.gtc_max_open_combo_orders:
            logger.info(
                "ENTRY BLOCKED by open-combo limit: %d/%d active GTC brackets | %s",
                len(open_positions), self._settings.gtc_max_open_combo_orders, ticker,
            )
            return

        # 4d. Discord DM approval gate — required for high-conviction trades when bot is configured
        if (
            self._settings.discord_bot_token
            and self._settings.discord_approval_user_id
            and recommendation.conviction_score >= self._settings.high_conviction_score
        ):
            try:
                from trading_platform.services.discord_approval import request_approval
                legs_summary = " / ".join(
                    f"{lg.action.upper()} {lg.quantity}x {lg.strike}{lg.option_type.upper()}"
                    for lg in recommendation.legs
                ) if recommendation.legs else "see dashboard"
                approved = await request_approval(
                    token=self._settings.discord_bot_token,
                    user_id=self._settings.discord_approval_user_id,
                    ticker=ticker,
                    strategy=str(strategy_str),
                    direction=recommendation.direction,
                    legs_summary=legs_summary,
                    entry_price=mid_price,
                    stop_loss=mid_price * (1 + recommendation.stop_loss_pct / 100),
                    profit_target=recommendation.max_gain_dollars / max(1, recommendation.contracts * 100),
                    max_loss_dollars=recommendation.max_loss_dollars,
                    reward_risk_ratio=recommendation.reward_risk_ratio,
                    contracts=recommendation.contracts,
                )
                if not approved:
                    logger.info("ENTRY BLOCKED by Discord approval: %s | conviction=%.0f", ticker, recommendation.conviction_score)
                    return
            except Exception as _disc_exc:
                logger.warning("Discord approval error: %s — proceeding without approval", _disc_exc)

        order = await submit_trade(recommendation, self._settings, self._session_id)
        order_status = order.get("status", "")
        logger.info("Order result for %s: status=%s order_id=%s fills=%s",
                    ticker, order_status, order.get("order_id"), order.get("fills"))

        if order_status in ("Cancelled", "ApiCancelled", "Inactive"):
            error_code = str(order.get("error_code", "unknown"))
            reason = order.get("reason", "")
            self._exec_quality.record_reject(ticker, error_code, reason, str(strategy_str))
            logger.warning("ORDER REJECTED: %s | code=%s | %s", ticker, error_code, reason)
            # Execution cooldown: if the order timed out after all price steps, the spread
            # is too illiquid to fill at mid+step. Block re-submission for 2 hours.
            if "price steps" in reason or "Unfilled" in reason:
                _cooldown_until = datetime.now(tz=timezone.utc) + timedelta(seconds=self._EXEC_COOLDOWN_SECS)
                self._exec_cooldowns[ticker] = _cooldown_until
                logger.info(
                    "EXEC COOLDOWN set: %s blocked until %s (spread too illiquid at current pricing)",
                    ticker, _cooldown_until.strftime("%H:%M ET"),
                )
            # If Error 201 detected, trigger orphan reconciliation immediately
            if error_code == "201" or "201" in reason:
                asyncio.create_task(self._orphan_reconciler.reconcile_now())
        elif order_status == "Filled":
            # Only record the position on confirmed fill — not on Submitted/PreSubmitted
            fills = order.get("fills", [])
            fill_price = float(fills[0]["price"]) if fills else mid_price
            self._exec_quality.record_fill(ticker, fill_price, mid_price, str(strategy_str))
            logger.info("ORDER FILLED: %s | fill_price=%.4f | mid=%.4f", ticker, fill_price, mid_price)
            self._record_position(
                recommendation,
                ibkr_order_id=order.get("order_id", -1),
                regime=self._macro_context.macro_stance if self._macro_context else "",
                earnings_date=earnings_date,
                is_pre_earnings=is_pre_earnings,
                spot=spot,
            )
        else:
            # Submitted/PreSubmitted — order is pending in TWS but not filled.
            # Do NOT record as a position. The orphan reconciler will detect unfilled
            # brackets and cancel them. Tracking as pending to avoid ghost positions.
            logger.warning(
                "ORDER PENDING (not recorded): %s | status=%s | order_id=%s — "
                "will remain open in TWS until filled or expired (DAY order)",
                ticker, order_status, order.get("order_id"),
            )
            # Record as an attempt but not a fill
            self._exec_quality.record_reject(ticker, "pending", order_status, str(strategy_str))

    def _record_position(
        self,
        rec: Any,
        ibkr_order_id: int = -1,
        regime: str = "",
        earnings_date: Any = None,
        is_pre_earnings: bool = False,
        spot: float = 0.0,
    ) -> None:
        from datetime import date, timedelta
        from .core.models import OpenPosition, PositionStatus
        expiry = rec.legs[0].expiration if rec.legs else (date.today() + timedelta(days=45))
        pos = OpenPosition(
            position_id=str(uuid.uuid4()),
            ticker=rec.ticker,
            strategy=rec.strategy,
            pillar=rec.pillar,
            direction=getattr(rec, "direction", "neutral"),
            status=PositionStatus.OPEN,
            legs=rec.legs,
            contracts=rec.contracts,
            entry_price=rec.entry_debit_credit / max(1, rec.contracts * 100),
            entry_date=date.today(),
            expiry_date=expiry,
            target_close_date=expiry - timedelta(days=21),
            max_loss_dollars=rec.max_loss_dollars,
            max_gain_dollars=rec.max_gain_dollars,
            ibkr_order_ids=[ibkr_order_id] if ibkr_order_id != -1 else [],
            conviction_at_entry=getattr(rec, "conviction_score", 0.0),
            regime_at_entry=regime,
            earnings_date=earnings_date,
            is_pre_earnings=is_pre_earnings,
        )
        self._position_mgr.add_position(pos)

        # Write trade journal entry explaining WHY this trade was taken
        macro_ctx = self._macro_context
        macro_summary = ""
        if macro_ctx:
            macro_summary = (
                f"stance={macro_ctx.macro_stance} conf={macro_ctx.confidence:.2f} "
                f"size_bias={macro_ctx.size_bias} key_risk={macro_ctx.key_risk} | "
                f"{macro_ctx.reasoning}"
            )
        why = getattr(rec, "reasoning", "") or f"Conviction={getattr(rec, 'conviction_score', 0):.1f} | {rec.pillar.value} signal"
        self._position_mgr.add_journal_entry(
            position=pos,
            spot_at_entry=spot,
            why_traded=why,
            macro_at_entry=macro_summary,
            ibkr_order_id=ibkr_order_id,
        )

    async def _sync_positions_with_tws(self) -> None:
        """
        Reconcile shadow book against IBKR executions on startup.

        Fetches today's fills from TWS via reqExecutionsAsync(). For every BAG
        sell fill (closing trade) whose ticker is still open in our shadow book,
        marks that position as closed. Catches GTC profit-target fills that
        arrived while the session was down.
        """
        try:
            from ib_insync import IB
        except ImportError:
            return

        from concurrent.futures import ThreadPoolExecutor
        _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ibkr-startup-sync")

        async def _poll() -> list[dict]:
            import asyncio as _aio

            def _sync():
                import asyncio as _aio2
                loop2 = _aio2.new_event_loop()
                _aio2.set_event_loop(loop2)
                try:
                    return loop2.run_until_complete(_fetch())
                finally:
                    loop2.close()
                    _aio2.set_event_loop(None)

            async def _fetch() -> list[dict]:
                ib = IB()
                try:
                    await ib.connectAsync(
                        self._settings.ibkr_host,
                        self._settings.ibkr_port,
                        clientId=self._settings.startup_tws_sync_client_id,
                        timeout=10,
                    )
                    fills = await ib.reqExecutionsAsync()
                    return [
                        {
                            "symbol":   f.contract.symbol,
                            "secType":  f.contract.secType,
                            "action":   f.execution.side,      # "BOT" or "SLD"
                            "price":    f.execution.price,
                            "shares":   f.execution.shares,
                            "time":     str(f.execution.time),
                            "order_id": f.execution.orderId,
                        }
                        for f in fills
                    ]
                except Exception as exc:
                    logger.warning("Startup TWS sync connect failed: %s", exc)
                    return []
                finally:
                    try:
                        ib.disconnect()
                    except Exception:
                        pass

            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(_executor, _sync)

        try:
            all_fills = await _poll()
        except Exception as exc:
            logger.warning("Startup TWS sync failed: %s", exc)
            return
        finally:
            _executor.shutdown(wait=False)

        if not all_fills:
            logger.info("Startup TWS sync: no fills returned (market closed or no trades today)")
            return

        # Find all BAG-level sells (closes) and check against shadow book
        bag_sells = [f for f in all_fills if f["secType"] == "BAG" and f["action"] == "SLD"]
        bag_buys  = {f["symbol"] for f in all_fills if f["secType"] == "BAG" and f["action"] == "BOT"}
        open_positions = self._position_mgr.get_open_positions()
        open_by_ticker = {p.ticker: p for p in open_positions}

        closed = 0
        for fill in bag_sells:
            ticker = fill["symbol"]
            pos = open_by_ticker.get(ticker)
            if pos is None:
                continue  # already closed or never recorded — skip
            close_price = float(fill["price"])
            realized_pnl = round(
                (close_price - pos.entry_price) * 100 * pos.contracts, 2
            )
            self._position_mgr.mark_position_closed(
                position_id=pos.position_id,
                realized_pnl=realized_pnl,
                close_price=close_price,
                source="tws_startup_sync",
            )
            logger.info(
                "STARTUP SYNC: closed %s | fill=$%.4f entry=$%.4f pnl=$%.2f",
                ticker, close_price, pos.entry_price, realized_pnl,
            )
            closed += 1

        # Warn about tickers filled in TWS (BOT) but not in our shadow book
        missing = bag_buys - set(open_by_ticker) - {f["symbol"] for f in bag_sells}
        for ticker in missing:
            logger.warning(
                "STARTUP SYNC: %s has a TWS entry fill but no shadow-book position — "
                "likely a mid-session restart missed the fill callback",
                ticker,
            )

        logger.info(
            "Startup TWS sync complete: %d TWS fills checked, %d shadow-book positions closed, "
            "%d missing from shadow book",
            len(all_fills), closed, len(missing),
        )

    def _maybe_reset_kill_switch(self) -> None:
        """
        Auto-reset the kill switch at session start if it was tripped on a prior day.
        Daily loss limits are per-session — yesterday's losses don't block today's trading.
        """
        try:
            kill_state = self._risk.get_kill_switch_state()
            if not kill_state.get("active"):
                return
            tripped_at_str = kill_state.get("tripped_at", "")
            if not tripped_at_str:
                return
            from datetime import datetime, timezone as _tz, date as _date
            tripped_dt = datetime.fromisoformat(tripped_at_str)
            if tripped_dt.date() < _date.today():
                self._risk.reset_kill_switch(reset_by="session_startup_daily_reset")
                logger.info(
                    "Kill switch auto-reset: was tripped on %s (prior day), "
                    "resetting for today's session",
                    tripped_dt.date(),
                )
        except Exception as exc:
            logger.warning("Kill switch auto-reset check failed: %s", exc)

    async def _execute_close(self, position: Any, reason: str) -> None:
        logger.info("Closing %s | reason=%s", position.ticker, reason)
        try:
            order = await close_trade(position, self._settings, self._session_id)
            # Extract actual fill price from order result if available
            fills = order.get("fills", []) if order else []
            close_price = float(fills[0]["price"]) if fills else position.current_price
            realized_pnl = round(
                (close_price - position.entry_price) * 100 * position.contracts
                * (-1 if position.direction == "bearish" else 1),
                2,
            )
            # Update DB with close data (close_date, close_price, realized_pnl, source)
            self._position_mgr.mark_position_closed(
                position_id=position.position_id,
                realized_pnl=realized_pnl,
                close_price=close_price,
                source=f"session:{reason[:40]}",
            )
            # Record for wash sale tracking
            self._compliance.record_close(
                ticker=position.ticker,
                realized_pnl=realized_pnl,
                strategy=str(position.strategy),
            )
            logger.info(
                "CLOSE CONFIRMED: %s | fill=$%.4f | realized=$%.2f | reason=%s",
                position.ticker, close_price, realized_pnl, reason,
            )
            # Release pre-earnings dedup lock so future setups can re-enter after earnings
            if getattr(position, "is_pre_earnings", False):
                self._pre_earnings_tickers.pop(position.ticker, None)
        except Exception as exc:
            logger.error("Close failed for %s: %s", position.ticker, exc)

    async def _execute_roll(self, position: Any, new_expiry: Any) -> None:
        logger.info("Rolling %s → expiry %s", position.ticker, new_expiry)
        try:
            await close_trade(position, self._settings, self._session_id)
        except Exception as exc:
            logger.error("Roll-close failed for %s: %s", position.ticker, exc)
            return

        # Reopen with same direction / pillar but new expiry
        try:
            import yfinance as yf
            from trading_platform.services.market_data.yfinance_provider import _YF_OPTIONS_LOCK
            from agora.core.models import ConvictionScore

            # Infer direction from the old strategy name (bull_put_spread → bullish, etc.)
            strat_name = position.strategy.value if hasattr(position.strategy, "value") else str(position.strategy)
            if "bull" in strat_name:
                direction = "bullish"
            elif "bear" in strat_name:
                direction = "bearish"
            else:
                direction = position.direction or "neutral"

            if direction == "neutral":
                logger.info("Roll reopen skipped for %s — neutral direction", position.ticker)
                return

            def _fetch_roll_data(t: str) -> tuple[dict, float]:
                _chain: dict = {}
                with _YF_OPTIONS_LOCK:
                    tk = yf.Ticker(t)
                    try:
                        fi = tk.fast_info
                        _spot = float(getattr(fi, "last_price", None) or fi.get("lastPrice", 0) or 0)
                    except Exception:
                        _spot = 0.0
                    if _spot <= 0:
                        info = tk.info or {}
                        _spot = float(info.get("regularMarketPrice") or info.get("currentPrice") or 0)
                    for exp in (tk.options or [])[:8]:
                        try:
                            c = tk.option_chain(exp)
                            _chain[exp] = {"calls": c.calls, "puts": c.puts}
                        except Exception:
                            continue
                return _chain, _spot

            try:
                chain_dict, spot = await asyncio.wait_for(
                    asyncio.to_thread(_fetch_roll_data, position.ticker), timeout=45.0
                )
            except asyncio.TimeoutError:
                logger.warning("Options chain fetch timed out for roll %s — skipping", position.ticker)
                return
            if not chain_dict or spot <= 0:
                logger.warning("Roll reopen aborted for %s — no chain data", position.ticker)
                return

            conviction = ConvictionScore(
                session_id=self._session_id,
                ticker=position.ticker,
                total_score=60.0,
                gate="standard",
                pillar=position.pillar,
                reasoning=f"Roll reopen: {position.expiry_date} → {new_expiry}",
            )
            conviction.size_multiplier = 1.0

            recommendation = self._strategy.build_recommendation(
                conviction=conviction,
                spot=spot,
                options_chain=chain_dict,
                direction_override=direction,
            )
            if recommendation:
                await self._submit_recommendation(recommendation, position.ticker, spot)
                logger.info("Roll reopen submitted for %s → %s", position.ticker, new_expiry)
        except Exception as exc:
            logger.error("Roll-reopen failed for %s: %s", position.ticker, exc)

    # ── After-hours reporting ──────────────────────────────────────

    async def _afterhours_report(self) -> None:
        logger.info("After-hours reporting")

        psi_result = self._psi.compute_psi()
        if psi_result["overall"] != "OK":
            logger.warning("PSI ALERT: %s | max_psi=%.3f",
                           psi_result["overall"], psi_result["max_psi"])

        attribution = self._attributor.attribution_report()
        logger.info(
            "Today P&L: $%.0f | trades=%d | pillars=%s",
            attribution["total_pnl"],
            attribution["total_trades"],
            list(attribution["pillars"].keys()),
        )

        # Performance feedback — 30-day outcome summary
        perf = self._position_mgr.get_performance_summary(lookback_days=30)
        if perf.get("total_trades", 0) > 0:
            logger.info(
                "30-day performance: %d trades | win_rate=%.1f%% | total_pnl=$%.0f | avg_pnl=$%.0f",
                perf["total_trades"],
                perf["win_rate"] * 100,
                perf["total_pnl"],
                perf["avg_pnl"],
            )
            for regime, stats in perf.get("by_regime", {}).items():
                if stats["trades"] >= 3:
                    logger.info(
                        "  regime=%-12s trades=%d win_rate=%.0f%% avg_pnl=$%.0f",
                        regime, stats["trades"], stats["win_rate"] * 100, stats["avg_pnl"],
                    )
            for band, stats in sorted(perf.get("by_conviction", {}).items()):
                if stats["trades"] >= 3:
                    logger.info(
                        "  conviction=%-8s trades=%d win_rate=%.0f%% avg_pnl=$%.0f",
                        band, stats["trades"], stats["win_rate"] * 100, stats["avg_pnl"],
                    )

        # Agent performance — alpha attribution by pillar/regime/conviction
        agent_report = self._agent_perf.generate_report(lookback_days=30)
        if agent_report and agent_report.total_trades >= 5:
            logger.info(
                "Agent performance: best_pillar=%s worst_pillar=%s best_regime=%s",
                agent_report.top_performing_pillar,
                agent_report.worst_performing_pillar,
                agent_report.best_regime,
            )
            for insight in agent_report.insights:
                logger.info("  %s", insight)

        # Execution quality summary
        eq = self._exec_quality.get_session_stats()
        logger.info(
            "Execution quality: attempts=%d fills=%d rejects=%d fill_rate=%.0f%% slippage=%.4f",
            eq["attempts"], eq["fills"], eq["rejects"], eq["fill_rate"] * 100, eq["avg_slippage"],
        )
        if eq["reject_reasons"]:
            logger.info("  Reject breakdown: %s", eq["reject_reasons"])

        # Pillar health summary
        silent_pillars = self._pillar_health.get_silent_pillars()
        if silent_pillars:
            logger.warning("Silent pillars at EOD: %s", silent_pillars)
        else:
            logger.info("Pillar health: all pillars contributed today")

        # Data integrity summary
        logger.info(
            "Data integrity: IVR feed healthy=%s", self._data_integrity.ivr_feed_healthy
        )

        # System health summary
        health = self._system_health.get_status()
        failed = [name for name, s in health.items() if s["status"] == "fail"]
        if failed:
            logger.warning("System health failures at EOD: %s", failed)
        else:
            logger.info("System health: all checks OK")


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    session = AgoraSession()
    try:
        await session.run()
    except KeyboardInterrupt:
        await session.stop()


if __name__ == "__main__":
    asyncio.run(main())

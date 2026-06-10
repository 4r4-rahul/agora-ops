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
from agora.execution.ibkr_bridge import close_trade, enrich_chain, reprice_legs, submit_trade
from agora.ops.agent_performance import AgentPerformanceMonitor
from agora.ops.attribution import PnlAttributor, PsiMonitor
from agora.ops.system_health import SystemHealthAgent
from agora.ops.execution_quality import ExecutionQualityAgent
from agora.ops.data_integrity import DataIntegrityAgent
from agora.ops.pillar_health import PillarHealthAgent
from agora.ops.orphan_reconciler import OrphanOrderReconciler
from agora.ops.ibkr_knowledge_agent import IBKRKnowledgeAgent
from agora.ops.decision_chains import (
    log_decision as _log_chain, update_close as _close_chain,
    start_chain as _start_chain, complete_chain as _complete_chain,
    link_position as _link_position,
)
from agora.ops.strategy_health import StrategyHealthAgent
from agora.ops.devils_advocate import run as _devils_advocate
from agora.ops.outcome_attributor import ScheduledAttributor, attribute_closed_trades
from agora.ops.discord_commander import DiscordCommander
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
from agora.signals.sector_momentum_intraday import IntradaySectorMomentumDetector, SECTOR_MAP as _SECTOR_MAP
from agora.signals.vol_regime import VolRegimeClassifier
from agora.strategies.rules_engine import StrategyRulesEngine
from agora.ops.dynamic_params import DynamicParams, compute_dynamic_params
from agora.scan import ScanPriority, UniverseScanEngine
from agora.scan.market_snapshot import get_market_snapshot
from agora.agents.stock_analyst import StockAnalystAgent
from agora.agents.strategy_selector import StrategySelectorAgent, StrategySelection
from agora.agents.advocate_agent import AdvocateAgent
from agora.agents.thesis_defender import ThesisDefenderAgent
from agora.agents.exit_management import ExitIntelligenceAgent
from agora.services.flow_detector import get_flow_signals

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

        # Install the yfinance throttle+cache gate FIRST — before any agent/loop fetches —
        # so all 139 raw yf.Ticker call sites get global rate-limiting + shared caching on
        # the heavy ops (history, option_chain). Kills the free-tier saturation.
        try:
            from agora.ops.yf_gate import install as _install_yf_gate
            _install_yf_gate()
        except Exception as _yfg_exc:
            logger.warning("yf_gate install failed (continuing without): %s", _yfg_exc)

        # DB migrations — idempotent, runs before any agent opens the DB
        try:
            from agora.ops.db_migrations import run_all as _run_db_migrations
            _run_db_migrations(str(self._settings.db_path))
        except Exception as _mig_exc:
            import logging as _log
            _log.getLogger(__name__).warning("DB migration error (non-fatal): %s", _mig_exc)

        # Signal generators
        self._vol_classifier   = VolRegimeClassifier()
        self._iv_screen        = IvPremiumScreen()
        self._event_engine     = EventPatternEngine()
        self._psi              = PsiMonitor()
        self._intraday_sector  = IntradaySectorMomentumDetector()

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

        # StrategyHealthAgent — rolling Sharpe per (pillar, regime); auto-pauses failing cells
        self._strategy_health = StrategyHealthAgent(
            settings=self._settings,
            ceo_agent=self._ceo,
        )

        # OutcomeAttributor — links closed positions → analyst_journal (feedback loop)
        self._outcome_attributor = ScheduledAttributor(
            str(self._settings.db_path),
            calibration_output=str(self._settings.db_path.parent / "calibration_report.json"),
            alert_webhook_url=self._settings.alert_webhook_url,
        )

        # Discord bidirectional commander (optional — requires bot token + user id)
        self._discord_commander: DiscordCommander | None = None
        if self._settings.discord_bot_token and self._settings.discord_approval_user_id:
            self._discord_commander = DiscordCommander(
                token=self._settings.discord_bot_token,
                user_id=self._settings.discord_approval_user_id,
                db_path=str(self._settings.db_path),
                calibration_path=str(self._settings.db_path.parent / "calibration_report.json"),
            )
            self._discord_commander.set_session(self)
            logger.info("DiscordCommander configured")

        # UW Discord alert listener + Market Intel Agent (optional — requires DISCORD_UW_CHANNEL_ID)
        self._uw_listener    = None
        self._uw_market_intel = None
        if self._settings.discord_bot_token and self._settings.discord_uw_channel_id:
            from agora.ops.uw_discord_listener import UWDiscordListener
            from agora.agents.uw_market_intel import UWMarketIntelAgent
            self._uw_listener = UWDiscordListener(
                token=self._settings.discord_bot_token,
                channel_id=self._settings.discord_uw_channel_id,
                account_size=float(self._settings.account_size),
                db_path=str(self._settings.db_path),
            )
            self._uw_market_intel = UWMarketIntelAgent(
                db_path=str(self._settings.db_path),
                settings=self._settings,
                webhook_url=self._settings.alert_webhook_url,
            )
            logger.info("UWDiscordListener + UWMarketIntelAgent configured — channel=%s",
                        self._settings.discord_uw_channel_id)

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
            pillar_health=self._pillar_health,
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
            strategy_health=self._strategy_health,
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
            strategy_health=self._strategy_health,
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
        # Let kill-switch resets re-anchor the breaker's daily-loss baseline.
        self._risk.register_circuit_breaker(self._circuit_breaker)

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

        # Autonomous action callbacks — CEO can close positions and refresh macro
        # without asking the owner for approval.
        async def _ceo_close_position(ticker: str, reason: str) -> None:
            pos = self._position_mgr.get_open_position_by_ticker(ticker)
            if pos:
                await self._execute_close(pos, reason)
            else:
                logger.warning("CEO close_position: no open position found for %s", ticker)

        self._ceo.wire_close_callback(_ceo_close_position)
        self._ceo.wire_macro_refresh_callback(self._premarket_macro_scan)
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
        self._dynamic_params  = DynamicParams()   # adaptive parameters, refreshed each macro cycle
        self._running         = False
        # NewsContext: updated every 30s by _news_watch_loop, read by agents
        from agora.services.news_signals import NewsContext as _NewsContext
        self._news_context: _NewsContext = _NewsContext()

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
        # #4 event-driven long options: the spread engine CONSUMES _priority_queue, so long
        # options gets its own promotion signal. The price monitor adds momentum/flow/sector
        # promotions here; the long loop drains it on a fast tick and evaluates immediately,
        # instead of waiting up to a full 15-min sweep — momentum bursts are exactly when a
        # directional long should fire. {ticker → trigger reason}.
        self._long_event_tickers: dict[str, str] = {}
        # Rotation index for Tier 2 (the remaining tickers)
        self._scan_index: int = 0
        # Last known prices for move detection
        self._last_prices: dict[str, float] = {}
        self._last_volumes: dict[str, float] = {}

        # ── Async scan engine (Phase 1) ────────────────────────────
        # Enabled via USE_ASYNC_SCAN_ENGINE=true in .env.
        # Starts in shadow_mode=True — records metrics but skips _evaluate_ticker.
        # After 5 trading days of clean metrics flip SHADOW_SCAN_ENGINE=false.
        self._scan_engine: UniverseScanEngine | None = None
        if self._settings.use_async_scan_engine:
            self._scan_engine = UniverseScanEngine(
                universe_fn=lambda: list(
                    dict.fromkeys(
                        self._settings.etf_universe
                        + (self._universe_disc.get_dynamic_tickers()
                           if hasattr(self, "_universe_disc") else [])
                    )
                ),
                evaluator=self._evaluate_ticker,
                db_path=str(self._settings.db_path),
                n_workers=self._settings.n_scan_workers,
                shadow_mode=self._settings.shadow_scan_engine,
                priority_fn=self._engine_priority,   # S1: Tier1 / market-interest → NORMAL sweep
            )
            logger.info(
                "ScanEngine configured: workers=%d shadow=%s",
                self._settings.n_scan_workers, self._settings.shadow_scan_engine,
            )

        # ── Stock Analyst (Phase 3 intelligence layer) ────────────────────────
        # Fires after conviction gate, before the expensive 45s options chain fetch.
        # Shadow mode (default): journals every thesis but never gates execution.
        # Live mode: a no_thesis verdict short-circuits the chain fetch.
        self._stock_analyst: StockAnalystAgent | None = None
        if self._settings.stock_analyst_enabled:
            self._stock_analyst = StockAnalystAgent(
                settings=self._settings,
                shadow_mode=self._settings.stock_analyst_shadow_mode,
            )
            logger.info(
                "StockAnalystAgent configured: shadow=%s min_conviction=%.0f",
                self._settings.stock_analyst_shadow_mode,
                self._settings.stock_analyst_min_conviction,
            )

        # ── StrategySelectorAgent (Phase 6) ──────────────────────────────────
        # Validates / overrides the rules engine's structure choice.
        # Shadow mode: both run; rules engine drives. Live mode: selector drives.
        self._strategy_selector: StrategySelectorAgent | None = None
        if self._settings.strategy_selector_enabled:
            self._strategy_selector = StrategySelectorAgent(
                settings=self._settings,
                shadow_mode=self._settings.strategy_selector_shadow_mode,
            )
            logger.info(
                "StrategySelectorAgent configured: shadow=%s",
                self._settings.strategy_selector_shadow_mode,
            )

        # ── AdvocateAgent (Phase 5 LLM adversarial review) ────────────────────
        # Runs after all deterministic gates, before IBKR submission.
        # Shadow: journals BLOCK verdicts but never stops execution.
        # Per-ticker advocate/defender fingerprint cache.
        # Re-runs the debate only when something material changes — not on a fixed clock.
        #
        # Cache entry: (timestamp, macro_stance, spot_price, open_position_count, adv_verdict, def_verdict)
        # Invalidated when ANY of these change:
        #   1. Time > 3h (backstop — catches anything else we miss)
        #   2. Macro stance changed (risk_on/off flip from CPI, FOMC, etc.)
        #   3. Spot price moved > 3% (thesis, strikes, and risk profile all change)
        #   4. Open position count changed (correlation argument in Mode 2 becomes wrong)
        #
        # Prevents: 13 POWL calls in 3h when thesis is unchanged.
        # Allows re-run: macro flips, big price move, position fill/close.
        self._advocate_cache: dict[str, tuple[float, str, float, int, Any, Any]] = {}
        self._ADVOCATE_COOLDOWN_SECS = 3 * 3600   # backstop TTL

        self._advocate: AdvocateAgent | None = None
        if self._settings.advocate_enabled:
            self._advocate = AdvocateAgent(
                settings=self._settings,
                shadow_mode=self._settings.advocate_shadow_mode,
            )
            logger.info(
                "AdvocateAgent configured: shadow=%s",
                self._settings.advocate_shadow_mode,
            )

        # ── ThesisDefenderAgent — counterweight to AdvocateAgent ──────────────
        self._defender: ThesisDefenderAgent | None = None
        if self._settings.thesis_defender_enabled:
            self._defender = ThesisDefenderAgent(
                settings=self._settings,
                shadow_mode=self._settings.thesis_defender_shadow_mode,
            )
            logger.info(
                "ThesisDefenderAgent configured: shadow=%s",
                self._settings.thesis_defender_shadow_mode,
            )

        # Semantic trade memory + chart-vision were swing-pipeline infra; dormant since
        # swing was folded into long options. The modules remain for reuse (e.g. future
        # vetter-vision) but are no longer spun up at startup. Re-instantiate here to re-enable.
        self._semantic_store = None

        # ── ExitIntelligenceAgent (Phase 6 hourly position monitor) ───────────
        # Evaluates every open position hourly during market hours.
        # Shadow: journals recommendations. Live: acts on CLOSE_NOW.
        self._exit_agent: ExitIntelligenceAgent | None = None
        if self._settings.exit_intelligence_enabled:
            self._exit_agent = ExitIntelligenceAgent(
                settings=self._settings,
                shadow_mode=self._settings.exit_intelligence_shadow_mode,
                on_close_callback=None,   # PositionManager owns all closes (agent runs act=False)
            )
            # Hand the agent to PositionManager — the single exit owner runs the LLM
            # thesis check itself (for spreads AND longs), instead of a separate patrol.
            self._position_mgr.set_exit_agent(self._exit_agent)
            logger.info(
                "ExitIntelligenceAgent configured (owned by PositionManager): shadow=%s interval=%.1fh",
                self._settings.exit_intelligence_shadow_mode,
                self._settings.exit_intelligence_interval_hours,
            )
        # Partial scale-out executor — wired unconditionally (PositionManager owns it).
        self._position_mgr.set_partial_close_handler(self._execute_partial_close)

        # ── LongOptionsAgent — directional swing (independent 15-min cycle) ───
        self._long_options_agent = None
        # NB: long-options trailing-stop peak state now lives in PositionManager (the
        # single exit owner), not here.
        if self._settings.long_options_enabled:
            from agora.agents.long_options_agent import LongOptionsAgent
            self._long_options_agent = LongOptionsAgent(self._settings)
            logger.info(
                "LongOptionsAgent enabled: delta=%.2f ivr_cap=%.0f hold=%dd "
                "profit=%.0f%% stop=%.0f%% trail=+%.0f%%/floor%.0f%% "
                "interval=%dm max_pos=%d max_contracts=%d",
                self._settings.long_options_target_delta,
                self._settings.long_options_ivr_cap,
                self._settings.long_options_max_hold_days,
                self._settings.long_options_profit_target_pct * 100,
                self._settings.long_options_stop_loss_pct * 100,
                self._settings.long_options_trailing_stop_trigger * 100,
                self._settings.long_options_trailing_stop_floor * 100,
                self._settings.long_options_scan_interval_minutes,
                self._settings.long_options_max_positions,
                self._settings.long_options_max_contracts,
            )

        # ── LongOptionsVetterAgent — Opus 4.8 pre-trade quality gate ──────────
        self._long_vetter = None
        if self._settings.long_options_enabled and self._settings.long_options_vetter_enabled:
            from agora.agents.long_options_vetter import LongOptionsVetterAgent
            self._long_vetter = LongOptionsVetterAgent(self._settings)

        # ── Finance intelligence modules ────────────────────────────────────────
        # Sector rotation RS monitor — daily-cached GICS sector ranking via ETF RS
        from agora.ops.sector_rotation import SectorRotationMonitor
        self._sector_monitor = SectorRotationMonitor()

        # Fundamental valuation + earnings revision gate — 4h TTL cache
        from agora.ops.valuation import ValuationGate
        self._valuation_gate = ValuationGate()

        # Portfolio correlation monitor — blocks near-duplicate same-direction positions
        from agora.ops.correlation_monitor import CorrelationMonitor
        self._correlation_monitor = CorrelationMonitor()

        logger.info(
            "Finance intelligence modules initialized: SectorRotation, Valuation, Correlation"
        )

        # ── Intraday macro refresh state ───────────────────────────
        # Tracks market levels at last synthesis so we can detect regime shifts
        self._last_macro_spy: float | None = None   # SPY price at last macro synthesis
        self._last_macro_vix: float | None = None   # VIX level at last macro synthesis
        self._last_macro_refresh_et: datetime | None = None  # time of last synthesis
        self._synthesis_in_progress: bool = False   # guard against concurrent synthesis calls

        # ── Scan cadence (time-of-day aware) ───────────────────────
        # prime time 10:00–11:30 ET → 60s; midday 11:30–13:30 → 300s; otherwise 180s
        self._last_universe_scan_et: datetime | None = None

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
        # M1 idempotency: tickers with a submission in flight RIGHT NOW. Guards against
        # concurrent double-submit (two scan paths hitting the same ticker before either
        # records an attempt / sets a cooldown). Added on entry, removed in a finally.
        self._inflight_tickers: set[str] = set()
        # Tickers that got Error 201 in live mode (riskless combo limit exceeded).
        # In paper mode this won't fire since we use leg-by-leg submission.
        self._error_201_blocked: set[str] = set()

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

        # Pre-warm the earnings date cache for all universe tickers via IBKR CalendarReport.
        # Runs async in background so it doesn't delay startup; first-scan latency reduced
        # since _evaluate_ticker finds cached results instead of issuing live IBKR calls.
        asyncio.create_task(self._prefetch_earnings_cache())

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
            self._strategy_health.start(),     # Sharpe monitor — auto-pauses failing pillar/regime cells
            self._outcome_attributor.start(),  # Analyst feedback loop — attributes closed trades
            *(([self._discord_commander.start()]) if self._discord_commander else []),
            *(([self._uw_listener.start()]) if self._uw_listener else []),
            *(([self._uw_market_intel.start()]) if self._uw_market_intel else []),
            self._news_watch_loop(),
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
            self._heartbeat_loop(),          # #5: liveness signal decoupled from the HTTP path
            *(([self._scan_engine.start()]) if self._scan_engine else []),
            # Exit intelligence is no longer a separate loop — PositionManager (the single
            # exit owner) runs the ExitIntelligenceAgent itself for every position.
            *(([self._long_options_loop()]) if self._long_options_agent else []),
        )

    async def _heartbeat_loop(self) -> None:
        """#5: write a heartbeat file every few seconds so liveness is decoupled from the busy
        HTTP path. When the scan saturates the single event loop, /agora/health can't be
        scheduled and returns 000 — which previously read as 'dead' and churned the engine. A
        lightweight always-running task that only touches a file proves the loop is still
        cooperatively scheduling; the watchdog trusts a FRESH heartbeat over a transient HTTP
        timeout, and only restarts when the heartbeat ALSO goes stale (a genuine hang)."""
        hb_path = "agora/logs/heartbeat"   # next to the logs the watchdog already reads
        _beat = 0
        while self._running:
            try:
                with open(hb_path, "w") as fh:
                    fh.write(str(int(datetime.now(tz=timezone.utc).timestamp())))
            except Exception as exc:
                logger.debug("heartbeat write failed: %s", exc)
            # Every ~5 min, surface the shared-snapshot cache effectiveness (#1/#2): a high hit
            # rate means the two pipelines are sharing fetches instead of duplicating them.
            _beat += 1
            if _beat % 30 == 0:
                try:
                    logger.info("MarketSnapshot cache %s", get_market_snapshot().stats())
                except Exception:
                    pass
            await asyncio.sleep(10)

    async def stop(self) -> None:
        self._running = False
        await asyncio.gather(
            self._catalyst_agent.stop(),
            # return_exceptions=True prevents one failing stop() from aborting the rest
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
            self._strategy_health.stop(),
            self._outcome_attributor.stop(),
            *(([self._discord_commander.stop()]) if self._discord_commander else []),
            # C-suite executives (including CTechAgent)
            self._cro.stop(),
            self._cio.stop(),
            self._cto.stop(),
            self._coo.stop(),
            self._cfo.stop(),
            self._rnd.stop(),
            self._ctech.stop(),
            *(([self._scan_engine.stop()]) if self._scan_engine else []),
            return_exceptions=True,
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
        - 9:30–15:30:  universe scan (time-of-day cadence: 60s prime 10-11:30, 300s midday 11:30-13:30, 180s otherwise)
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
            # Wait for the startup synthesis task (may already be in-progress) to finish,
            # then run an immediate scan with fresh macro context.
            for _wait in range(120):
                if not self._synthesis_in_progress:
                    break
                await asyncio.sleep(1)
            # S4: skip the cold-start tiered scan when the live engine is driving — its
            # scheduled sweeper enqueues the whole (stale) universe on start, so a separate
            # _universe_scan here just double-scans with the now-fallback tiering path.
            if self._scan_engine and not self._scan_engine.shadow_mode:
                logger.info("Session loop: cold-start — live ScanEngine sweeps the universe; skipping tiered _universe_scan")
            else:
                try:
                    logger.info("Session loop: cold-start universe scan (immediate)")
                    await self._universe_scan()
                    self._last_universe_scan_et = datetime.now(tz=ET)
                except Exception as exc:
                    logger.error("Cold-start universe scan failed: %s", exc)

        while self._running:
            now_et = datetime.now(tz=ET)
            hour, minute = now_et.hour, now_et.minute

            try:
                if hour == 7 and minute == 0:
                    await self._premarket_macro_scan()
                elif (9 <= hour < 16) and self._due_for_scan(now_et):
                    if not (hour == 9 and minute < 30):
                        if self._scan_engine and not self._scan_engine.shadow_mode:
                            # Engine is live — it drives the universe scan independently.
                            pass
                        else:
                            await self._universe_scan()
                        self._last_universe_scan_et = now_et
                elif hour == 16 and minute == 5:
                    self._intraday_sector.reset_session()   # clear sector signals for next day
                    await self._afterhours_report()
                # Pre-earnings IV-crush close is now owned by PositionManager (single exit owner).
            except Exception as exc:
                logger.error("Session loop error: %s", exc)

            await asyncio.sleep(60)

    # ── Earnings proximity ─────────────────────────────────────────

    async def _get_next_earnings(self, ticker: str) -> "date | None":
        """
        Fetch and cache next earnings date.

        Priority:
          1. IBKR CalendarReport (Refinitiv data — accurate, no scraping issues)
          2. yfinance .calendar (fallback when IBKR is unavailable)

        Result is cached per-session; subsequent calls for the same ticker
        return immediately from cache.
        """
        if ticker in self._earnings_date_cache:
            return self._earnings_date_cache[ticker]

        result: "date | None" = None

        # 1. IBKR Fundamental Data (CalendarReport)
        try:
            from agora.ops.fundamental_data import get_next_earnings_ibkr
            result = await get_next_earnings_ibkr(
                ticker,
                host=self._settings.ibkr_host,
                port=self._settings.ibkr_port,
            )
        except Exception as exc:
            logger.debug("IBKR CalendarReport failed for %s: %s", ticker, exc)

        # 2. yfinance fallback
        if result is None:
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
                if result is not None:
                    logger.debug("yfinance earnings fallback for %s: %s", ticker, result)
            except Exception:
                result = None

        self._earnings_date_cache[ticker] = result
        return result

    async def _prefetch_earnings_cache(self) -> None:
        """
        Pre-warm the per-session earnings date cache for all universe tickers
        using IBKR CalendarReport in batches of 3.

        Runs once at session startup (fire-and-forget via create_task).
        After this completes, every _evaluate_ticker call gets a cached hit
        instead of issuing a live IBKR request mid-scan.
        """
        try:
            from agora.ops.fundamental_data import prefetch_earnings_dates
            all_tickers = list(dict.fromkeys(
                getattr(self, "_tier1", []) + getattr(self, "_tier2", [])
            ))
            if not all_tickers:
                return
            logger.info("Prefetching IBKR earnings dates for %d tickers", len(all_tickers))
            fetched = await prefetch_earnings_dates(
                all_tickers,
                host=self._settings.ibkr_host,
                port=self._settings.ibkr_port,
                concurrency=3,
            )
            # Merge into cache without overwriting any result already cached
            # by an earlier _get_next_earnings call that fired concurrently.
            for t, dt in fetched.items():
                if t not in self._earnings_date_cache:
                    self._earnings_date_cache[t] = dt
            hits = sum(1 for v in self._earnings_date_cache.values() if v is not None)
            logger.info(
                "Earnings cache prefetch complete: %d tickers, %d with upcoming earnings",
                len(all_tickers), hits,
            )
        except Exception as exc:
            logger.warning("Earnings cache prefetch failed: %s", exc)

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

            # Push latest macro stance into profit engine so exits are regime-aware
            self._position_mgr.set_macro_context(self._macro_context)

            # Refresh adaptive parameters from new macro context
            try:
                gex_regime_str = "neutral"
                self._dynamic_params = compute_dynamic_params(
                    vix=vix,
                    iv_rank=spy_snap.iv_rank,
                    macro_stance=self._macro_context.macro_stance,
                    gex_regime=gex_regime_str,
                    db_path=str(self._settings.db_path),
                )
            except Exception as dp_exc:
                logger.warning("DynamicParams refresh failed: %s — using defaults", dp_exc)

            # Auto-graduation: promote StrategySelectorAgent from shadow to live
            # once direction hit rate >= 55% over 40+ closed positions
            self._maybe_graduate_strategy_selector()

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

    def _maybe_graduate_strategy_selector(self) -> None:
        """
        Auto-promote StrategySelectorAgent from shadow → live mode when:
          - It is currently in shadow mode
          - ≥ 40 positions have been evaluated by the selector
          - Selector direction hit rate ≥ 55% (where selector chose != rules engine)
        This removes the manual step of watching metrics and flipping the flag.
        """
        if self._strategy_selector is None or not self._strategy_selector.shadow_mode:
            return
        try:
            import sqlite3
            with sqlite3.connect(str(self._settings.db_path)) as conn:
                row = conn.execute(
                    """
                    SELECT COUNT(*) as total,
                           SUM(CASE WHEN decision IN ('endorse','override') THEN 1 ELSE 0 END) as evaluated
                    FROM strategy_journal
                    WHERE shadow_mode = 1
                    """
                ).fetchone()
                if not row or row[0] < 40:
                    return
                # Check how analyst direction theses have been performing as a proxy
                # (strategy_journal doesn't have outcomes yet — use analyst_journal hit rate)
                hit_row = conn.execute(
                    """
                    SELECT COUNT(*) as total,
                           SUM(CASE WHEN decision = 'thesis' THEN 1 ELSE 0 END) as thesis_count
                    FROM analyst_journal
                    WHERE shadow_mode = 0
                    """
                ).fetchone()
                if not hit_row or hit_row[0] < 40:
                    return
                # Use conviction outcomes as hit-rate proxy
                outcomes = conn.execute(
                    """
                    SELECT outcome, COUNT(*) as cnt
                    FROM decision_chains
                    WHERE outcome IN ('profit','loss','filled','stopped')
                    GROUP BY outcome
                    """
                ).fetchall()
            outcome_map = {r[0]: r[1] for r in outcomes}
            wins  = outcome_map.get("profit", 0) + outcome_map.get("filled", 0)
            total = sum(outcome_map.values())
            if total < 40:
                return
            hit_rate = wins / total
            if hit_rate >= 0.55:
                self._strategy_selector.shadow_mode = False
                logger.info(
                    "StrategySelectorAgent AUTO-GRADUATED to live mode "
                    "(hit_rate=%.0f%% over %d closed positions)",
                    hit_rate * 100, total,
                )
        except Exception as exc:
            logger.debug("Auto-graduation check failed: %s", exc)

    # ── Scan cadence ───────────────────────────────────────────────

    def _scan_interval_seconds(self, now_et: datetime) -> int:
        """
        Return universe scan interval based on time of day (ET).

        10:00 – 11:30  prime time  → 60s  (highest IV + volume, best fills)
        11:30 – 13:30  midday lull → 300s (thin liquidity, fewer setups)
        otherwise                  → 180s (market open / late session)
        """
        h, m = now_et.hour, now_et.minute
        t = h * 60 + m   # minutes since midnight ET
        if 600 <= t < 690:    # 10:00–11:30
            return 60
        if 690 <= t < 810:    # 11:30–13:30
            return 300
        return 180

    def _due_for_scan(self, now_et: datetime) -> bool:
        """True if enough time has passed since the last universe scan."""
        if self._last_universe_scan_et is None:
            return True
        elapsed = (now_et - self._last_universe_scan_et).total_seconds()
        return elapsed >= self._scan_interval_seconds(now_et)

    # ── Long options entry loop ────────────────────────────────────

    async def _long_options_loop(self) -> None:
        """
        LongOptionsAgent independent scan cycle — professional-grade directional swing.

        Runs every long_options_scan_interval_minutes (default 15) during market hours.
        Buys calls (bullish) or puts (bearish) on directional conviction.

        Entry gates:
          1. Market hours (9:30–15:30 ET, weekdays)
          2. Intraday timing: no new entries 9:30–9:45 (price discovery) or after 3:10 (EOD risk)
          3. Max concurrent long positions cap
          4. Earnings blackout (same helper as spread pipeline)
          5. Duplicate ticker (any open position in same ticker)
          6. Per-ticker IVR gate (ATM IV / HV20 proxy — more accurate than portfolio IVR)

        Exit gates (enforced per-cycle):
          A. 5-day time stop — hard close regardless of P&L
          B. Trailing stop  — trail 15% below peak once +30% gain is reached
          C. 50% flat stop  — cut when option loses 50% of purchase price (pre-trail)
          D. 50% profit target — take profit before trail activates (optional direct exit)
        """
        from datetime import time as _time, date as _date, timedelta
        # Market data now comes from the shared snapshot (get_market_snapshot); the long loop
        # no longer touches yfinance directly (#1/#2).

        # Entry-only loop. All exits (time/profit/trail/stop + LLM thesis check) are owned
        # by PositionManager — the single exit owner for every strategy.
        interval_secs = self._settings.long_options_scan_interval_minutes * 60
        max_hold      = self._settings.long_options_max_hold_days   # used to set time-stop date at entry
        agent         = self._long_options_agent

        # Serializes the rare submit critical section so concurrent workers (parallel mode,
        # long_loop_parallel_enabled) can never over-open past the position cap. Created here
        # while the event loop is running. Harmless in sequential mode (uncontended).
        self._long_submit_lock = asyncio.Lock()

        # #6: real backpressure is now the shared snapshot's token-bucket rate limiter, not a
        # blind stagger. Keep a short 45s warmup so the spread cold-start scan populates the
        # snapshot first — the long loop then gets cache HITS for the overlapping [14,30] expiries
        # and shared spot/history instead of re-fetching (and reasons on the same prices, #2).
        await asyncio.sleep(45)

        # #4: tick FAST (event cadence) but run the full-universe sweep only every
        # interval_secs. Between sweeps, evaluate only tickers the price monitor just promoted
        # (momentum/flow/sector) — a directional long fires within ~60s of the burst instead of
        # waiting up to a full sweep. The full sweep remains the catch-all for un-triggered names.
        _LONG_TICK_SECS = 60
        _loop = asyncio.get_event_loop()
        _last_full_sweep = -1e9   # force a full sweep on the first iteration

        while self._running:
            now_et = datetime.now(tz=ET)
            if now_et.weekday() >= 5 or not (_time(9, 30) <= now_et.time() <= _time(15, 30)):
                await asyncio.sleep(60)
                continue

            # ── C. Intraday timing gate — no entries in price discovery or EOD window ──
            # Skip entries 9:30–9:45 ET (price discovery) and after 3:10 ET (EOD risk).
            now_et = datetime.now(tz=ET)
            in_price_discovery = _time(9, 30) <= now_et.time() < _time(9, 45)
            too_late_for_entry = now_et.time() >= _time(15, 10)
            if in_price_discovery or too_late_for_entry:
                reason = "price_discovery" if in_price_discovery else "eod_window"
                logger.debug("LongOptions entry gate: %s — skipping scan", reason)
                self._long_event_tickers.clear()   # don't act on stale promotions when re-opened
                await asyncio.sleep(_LONG_TICK_SECS)
                continue

            # ── D. Entry scan ─────────────────────────────────────────────────────
            open_positions = self._position_mgr.get_open_positions()
            long_count = sum(
                1 for p in open_positions
                if str(getattr(p.strategy, "value", p.strategy)) in ("long_call", "long_put")
            )
            if long_count >= self._settings.long_options_max_positions:
                logger.debug("LongOptions: cap reached (%d/%d) — tick",
                             long_count, self._settings.long_options_max_positions)
                await asyncio.sleep(_LONG_TICK_SECS)
                continue

            _full_universe = list(dict.fromkeys(
                self._settings.etf_universe
                + (self._universe_disc.get_dynamic_tickers()
                   if hasattr(self, "_universe_disc") else [])
            ))
            # Full sweep on cadence; otherwise drain just-promoted tickers (event-driven).
            if (_loop.time() - _last_full_sweep) >= interval_secs:
                universe = _full_universe
                _last_full_sweep = _loop.time()
                self._long_event_tickers.clear()   # full sweep supersedes pending events
                _scan_kind = "full"
            else:
                _full_set = set(_full_universe)
                _events = self._long_event_tickers
                self._long_event_tickers = {}      # drain
                universe = [t for t in _events if t in _full_set]
                _scan_kind = "event"
                if not universe:
                    await asyncio.sleep(_LONG_TICK_SECS)
                    continue

            logger.info("LongOptions %s scan: %d tickers | long_pos=%d/%d",
                        _scan_kind, len(universe), long_count, self._settings.long_options_max_positions)

            # Per-cycle dedupe: prevents duplicate orders when overlapping scan cycles
            # evaluate the same ticker (observed with FLNC at 13:13/13:14, MU 3× same session)
            _submitted_this_cycle: set[str] = set()

            async def _process_one(ticker: str) -> None:
                # Per-ticker processor — body shared by the sequential and bounded-parallel
                # dispatch below. Each `return` here ends THIS ticker only (was `continue`/
                # `break` in the original sequential loop). Per-worker isolation: the dispatch
                # wraps this in try/except so one ticker's failure never kills the cycle.
                if not self._running:
                    return

                # Re-check cap (best-effort skip when already full — the binding, race-free
                # cap check is re-done atomically under _long_submit_lock at submit time)
                open_positions = self._position_mgr.get_open_positions()
                long_count = sum(
                    1 for p in open_positions
                    if str(getattr(p.strategy, "value", p.strategy)) in ("long_call", "long_put")
                )
                if long_count >= self._settings.long_options_max_positions:
                    return

                # Duplicate ticker gate
                if any(p.ticker == ticker for p in open_positions):
                    return

                # Earnings blackout gate + days_to_catalyst computation
                earnings_date     = None
                days_to_catalyst  = None
                try:
                    earnings_date = await self._get_next_earnings(ticker)
                    if earnings_date:
                        days_to_earnings = (earnings_date - _date.today()).days
                        if 0 <= days_to_earnings <= self._settings.earnings_blackout_days:
                            logger.debug(
                                "LongOptions skip %s — earnings in %dd (blackout)",
                                ticker, days_to_earnings,
                            )
                            return
                        if days_to_earnings > 0:
                            days_to_catalyst = days_to_earnings
                except Exception:
                    pass

                # Options chain fetch — DTE window [14, 30] (matches the agent's selection window)
                try:
                    def _fetch_long_chain(t: str) -> dict:
                        # #1/#2: pull expiries + per-expiry chains from the SHARED snapshot so the
                        # spread pipeline and this loop don't re-fetch the same data (and both see
                        # identical prices). Brackets cover the agent's [14,30] selection window
                        # (_DTE_MIN/MAX): three buckets give _select_expiry a candidate near each
                        # IVR-scaled DTE target across the window.
                        _DTE_BRACKETS = [(14, 19), (20, 25), (26, 30)]
                        ms = get_market_snapshot()
                        chain_dict: dict = {}
                        today_d = _date.today()
                        filled: set[int] = set()
                        for exp in ms.expiries(t):
                            try:
                                exp_date = _date.fromisoformat(exp)
                            except ValueError:
                                continue
                            dte = (exp_date - today_d).days
                            for i, (lo, hi) in enumerate(_DTE_BRACKETS):
                                if i not in filled and lo <= dte <= hi:
                                    c = ms.option_chain(t, exp)
                                    if c is not None:
                                        chain_dict[exp] = c
                                        filled.add(i)
                                    break
                            if len(filled) == len(_DTE_BRACKETS):
                                break
                        return chain_dict

                    chain_dict = await asyncio.wait_for(
                        asyncio.to_thread(_fetch_long_chain, ticker), timeout=30.0
                    )
                except Exception as exc:
                    logger.debug("LongOptions chain fetch error [%s]: %s", ticker, exc)
                    return

                if not chain_dict:
                    return

                # Spot price (shared snapshot — same value the spread pipeline sees, #2)
                try:
                    spot = await asyncio.wait_for(
                        asyncio.to_thread(lambda t=ticker: get_market_snapshot().spot(t)),
                        timeout=10.0,
                    )
                except Exception:
                    spot = 0.0
                if spot <= 0:
                    return

                # Momentum (RSI14 + SMA20/50 + 10d return) — also returns closes for IVR
                momentum: dict = {}
                closes_series = None
                try:
                    def _compute_momentum(t: str, s: float) -> tuple[dict, Any]:
                        import pandas as pd
                        hist = get_market_snapshot().history(t, period="3mo", interval="1d")
                        if hist.empty or len(hist) < 22:
                            return {}, None
                        closes = hist["Close"].dropna()
                        sma20  = float(closes.rolling(20).mean().iloc[-1])
                        sma50  = float(closes.rolling(50).mean().iloc[-1]) if len(closes) >= 50 else sma20
                        delta  = closes.diff()
                        gain   = delta.clip(lower=0).rolling(14).mean()
                        loss   = (-delta.clip(upper=0)).rolling(14).mean()
                        rs     = gain.iloc[-1] / (loss.iloc[-1] + 1e-9)
                        rsi    = float(100 - (100 / (1 + rs)))
                        # 10-day relative return for RS signal
                        ret_10d = (
                            float(closes.iloc[-1] / closes.iloc[-11] - 1)
                            if len(closes) >= 11 else 0.0
                        )
                        # Volume surge: today vs 20-day avg
                        vol_surge = False
                        if "Volume" in hist.columns:
                            vols = hist["Volume"].dropna()
                            if len(vols) >= 21:
                                avg_vol20 = float(vols.iloc[-21:-1].mean())
                                vol_surge = avg_vol20 > 0 and float(vols.iloc[-1]) > 2.0 * avg_vol20
                        # HV5/HV20 breakout signal for DTE compression
                        hv5  = 0.0
                        hv20 = 0.0
                        if len(closes) >= 21:
                            import numpy as _np
                            log_ret = _np.log(closes / closes.shift(1)).dropna()
                            hv20 = float(log_ret.tail(20).std() * _np.sqrt(252))
                            if len(log_ret) >= 5:
                                hv5 = float(log_ret.tail(5).std() * _np.sqrt(252))
                        mom = {
                            "rsi":         rsi,
                            "rsi_norm":    rsi / 100.0,
                            "sma20":       sma20,
                            "sma50":       sma50,
                            "above_sma20": s > sma20,
                            "above_sma50": s > sma50,
                            "ret_10d":     ret_10d,
                            "vol_surge":   vol_surge,
                            "hv5":         hv5,
                            "hv20":        hv20,
                        }
                        return mom, closes

                    mom_result = await asyncio.wait_for(
                        asyncio.to_thread(_compute_momentum, ticker, spot), timeout=15.0
                    )
                    momentum, closes_series = mom_result
                except Exception:
                    momentum = {}
                    closes_series = None

                # Per-ticker IVR (ATM IV / HV20 proxy — more precise than portfolio IVR)
                per_ticker_ivr: float | None = None
                if closes_series is not None and chain_dict:
                    try:
                        per_ticker_ivr = agent.compute_per_ticker_ivr(
                            chain_dict, spot, closes_series
                        )
                        if per_ticker_ivr is not None:
                            logger.debug(
                                "LongOptions [%s] per-ticker IVR=%.0f", ticker, per_ticker_ivr
                            )
                    except Exception:
                        pass

                # GEX regime (best-effort)
                gex_regime = "neutral"
                try:
                    from trading_platform.services.options_flow import get_gex
                    gex_data   = await asyncio.wait_for(
                        asyncio.to_thread(get_gex, ticker), timeout=5.0
                    )
                    gex_regime = str(getattr(gex_data, "regime", "neutral") or "neutral")
                except Exception:
                    pass

                # Flow signals (best-effort)
                flow_signals = None
                try:
                    flow_signals = await asyncio.wait_for(
                        get_flow_signals(ticker), timeout=5.0
                    )
                except Exception:
                    pass

                # ── #8 Portfolio net-delta budget — block new LONG CALL if net delta
                # exceeds 150 per $10k of account. Prevents runaway directional exposure
                # when the whole long options book is already leaning one way.
                # Net delta = sum of (contract_delta × contracts × 100) for all longs.
                try:
                    _acct_units   = max(1, self._settings.account_size / 10_000)
                    _delta_budget = 150.0 * _acct_units
                    _all_pos      = self._position_mgr.get_open_positions()
                    _net_delta    = 0.0
                    for _p in _all_pos:
                        _strat = str(getattr(_p.strategy, "value", _p.strategy))
                        _d     = float(getattr(_p, "delta", 0.0) or 0.0)
                        _c     = int(getattr(_p, "contracts", 1) or 1)
                        if _strat == "long_call":
                            _net_delta += _d * _c * 100
                        elif _strat == "long_put":
                            _net_delta -= abs(_d) * _c * 100
                    if _net_delta >= _delta_budget:
                        logger.debug(
                            "LongOptions [%s] skip — net delta %.0f ≥ budget %.0f",
                            ticker, _net_delta, _delta_budget,
                        )
                        return
                except Exception:
                    pass

                # Pre-earnings drift window: T-7 to T-3 with positive RS
                # (stock drifts toward catalyst — buy the drift, not the event)
                _pre_drift = (
                    days_to_catalyst is not None
                    and 3 <= days_to_catalyst <= 7
                    and momentum.get("ret_10d", 0.0) > 0.03
                )
                if _pre_drift:
                    logger.debug(
                        "LongOptions [%s] pre-earnings drift window: %dd to earnings, "
                        "RS=%.1f%% — lowering conviction floor to 1",
                        ticker, days_to_catalyst, momentum.get("ret_10d", 0.0) * 100,
                    )

                # Evaluate — pass per-ticker IVR, catalyst context, and live news signals
                decision = agent.evaluate(
                    ticker=ticker,
                    spot=spot,
                    options_chain=chain_dict,
                    macro_context=self._macro_context,
                    flow_signals=flow_signals,
                    momentum=momentum,
                    gex_regime=gex_regime,
                    session_id=self._session_id,
                    per_ticker_ivr=per_ticker_ivr,
                    days_to_catalyst=days_to_catalyst,
                    pre_earnings_drift=_pre_drift,
                    news_context=self._news_context,
                )

                # Journal every decision (entry + skips)
                agent.journal(decision, str(self._settings.db_path))

                if decision.recommendation is None:
                    if decision.block_reason:
                        logger.debug("LongOptions [%s] skip: %s", ticker, decision.block_reason)
                    return

                # ── Opus 4.8 quality vetter (shadow or live) ─────────────────
                # L2: track a LIVE vetter approval so the redundant general advocate doesn't
                # fail-CLOSE a trade the purpose-built long-options gate already cleared (the
                # vetter PROCEED → advocate-transient-fail → block SPOF observed live).
                _vetter_approved = False
                if self._long_vetter is not None:
                    try:
                        verdict = await asyncio.wait_for(
                            self._long_vetter.vet(decision, self._macro_context, momentum),
                            timeout=60.0,
                        )
                        if verdict is not None:
                            if not verdict.shadow_mode and verdict.verdict in ("proceed", "reduce"):
                                _vetter_approved = True
                            self._long_vetter.journal(
                                decision, verdict, str(self._settings.db_path)
                            )
                            if not verdict.shadow_mode:
                                if verdict.verdict == "skip":
                                    logger.info(
                                        "LongOptions vetter BLOCK [%s] conf=%d: %s",
                                        ticker, verdict.confidence, verdict.key_risk,
                                    )
                                    return
                                elif verdict.verdict == "reduce" and verdict.adjusted_contracts:
                                    adj = max(1, verdict.adjusted_contracts)
                                    if adj < decision.contracts:
                                        logger.info(
                                            "LongOptions vetter REDUCE [%s] %d→%d contracts: %s",
                                            ticker, decision.contracts, adj, verdict.reasoning,
                                        )
                                        decision.contracts = adj
                                        rec = decision.recommendation
                                        rec.legs[0].contracts = adj
                                        rec.contracts = adj
                                        rec.entry_debit_credit = round(
                                            decision.premium * 100 * adj, 2
                                        )
                    except asyncio.TimeoutError:
                        logger.warning("LongOptionsVetter timeout [%s] — proceeding", ticker)
                    except Exception as vex:
                        logger.debug("LongOptionsVetter error [%s]: %s", ticker, vex)

                # Per-cycle dedupe gate
                if ticker in _submitted_this_cycle:
                    logger.debug("LongOptions [%s] skipped — already submitted this cycle", ticker)
                    return

                rec = decision.recommendation
                # Shared heavy gates — long options now faces the same review as the main
                # (spread) pipeline: timing / kill-switch / macro-cal / compliance / risk
                # council / correlation / devils-advocate / LLM advocate.
                if not await self._long_options_risk_gates(
                    rec, ticker, spot, earnings_date, vetter_approved=_vetter_approved):
                    return

                # ── Submit — ATOMIC cap safeguard ────────────────────────────────
                # Serialize the rare submit critical section under a lock and RE-CHECK the
                # live cap inside it, so concurrent workers (parallel mode) can never each
                # pass a stale cap check and over-open. Submits are infrequent (most tickers
                # return earlier), so serializing them costs ~nothing and also de-races IBKR.
                async with self._long_submit_lock:
                    _open_now = self._position_mgr.get_open_positions()
                    _live_longs = sum(
                        1 for p in _open_now
                        if str(getattr(p.strategy, "value", p.strategy)) in ("long_call", "long_put")
                    )
                    if _live_longs >= self._settings.long_options_max_positions:
                        logger.debug("LongOptions [%s] cap reached at submit (%d/%d) — skip",
                                     ticker, _live_longs, self._settings.long_options_max_positions)
                        return
                    # #3: global exposure ceiling across BOTH pipelines (re-checked atomically
                    # under the submit lock so concurrent long workers can't both pass a stale read).
                    _gok, _greason = self._global_exposure_ok(
                        new_risk_dollars=abs(getattr(rec, "max_loss_dollars", 0.0) or 0.0))
                    if not _gok:
                        logger.info("LongOptions [%s] BLOCKED by %s — skip", ticker, _greason)
                        return
                    logger.info(
                        "LongOptions SUBMITTING [%s] %s strike=%.0f exp=%s prem=$%.2f "
                        "contracts=%d conviction=%d ptIVR=%.0f",
                        ticker, decision.strategy, decision.strike,
                        rec.legs[0].expiration, decision.premium,
                        decision.contracts, decision.conviction,
                        decision.per_ticker_ivr,
                    )
                    # Record the attempt BEFORE submitting so the fill-rate metric has a
                    # proper denominator (long path previously called record_fill with no
                    # preceding record_attempt, corrupting the fill rate).
                    self._exec_quality.record_attempt(ticker, str(decision.strategy), decision.premium)
                    try:
                        order = await submit_trade(rec, self._settings, self._session_id)
                        order_status = order.get("status", "")
                        logger.info(
                            "LongOptions order [%s]: status=%s order_id=%s",
                            ticker, order_status, order.get("order_id"),
                        )
                        if order_status in ("Filled", "PartiallyFilled"):
                            if order_status == "PartiallyFilled":
                                _orig = max(1, int(rec.contracts))
                                _fc = int(order.get("filled_contracts", _orig) or _orig)
                                if 0 < _fc < _orig:
                                    _scale = _fc / _orig
                                    rec.entry_debit_credit *= _scale
                                    for _attr in ("max_loss_dollars", "max_gain_dollars"):
                                        _v = getattr(rec, _attr, None)
                                        if isinstance(_v, (int, float)):
                                            setattr(rec, _attr, _v * _scale)
                                    rec.contracts = _fc
                                    logger.warning("PARTIAL FILL recorded (long): %s at %d/%d",
                                                   ticker, _fc, _orig)
                            fills      = order.get("fills", [])
                            fill_price = float(fills[0]["price"]) if fills else decision.premium
                            time_stop_date = _date.today() + timedelta(days=max_hold)
                            position_id = self._record_position(
                                rec,
                                ibkr_order_id=order.get("order_id", -1),
                                regime=self._macro_context.macro_stance if self._macro_context else "",
                                earnings_date=earnings_date,
                                is_pre_earnings=False,
                                spot=spot,
                                fill_price=fill_price,
                                target_close_date_override=time_stop_date,
                                extra_metadata={
                                    "profit_target_pct": decision.profit_target_pct,
                                    "signal_quality":    decision.signal_quality,
                                    "conviction":        decision.conviction,
                                    # C-lite: tag post-event-settle entries so attribution can
                                    # measure whether event-day entries are +EV (re-evaluate after ~10-15).
                                    "event_day":         bool(getattr(rec, "event_day", False)),
                                },
                            )
                            agent.journal(decision, str(self._settings.db_path), position_id or "")
                            self._exec_quality.record_fill(ticker, fill_price, decision.premium, decision.strategy)
                            _submitted_this_cycle.add(ticker)
                        elif order_status in ("Cancelled", "ApiCancelled", "Inactive"):
                            self._exec_quality.record_reject(
                                ticker, str(order.get("error_code", "")),
                                order.get("reason", ""), decision.strategy,
                            )
                    except Exception as submit_exc:
                        logger.warning("LongOptions submit error [%s]: %r", ticker, submit_exc, exc_info=True)

            # ── Dispatch: bounded parallel (flag ON) or sequential (default OFF) ──
            # Same _process_one body either way — parallel just overlaps the per-ticker
            # data fetch + LLM latency. Data stays safe (yf_gate throttle+cache); the
            # worker-pool size caps concurrent Opus/advocate calls (LLM-burst control);
            # the position cap is enforced atomically inside _process_one (above).
            if self._settings.long_loop_parallel_enabled:
                _sem = asyncio.Semaphore(max(1, self._settings.long_loop_max_concurrency))

                async def _guarded(_t: str) -> None:
                    if not self._running:
                        return
                    async with _sem:
                        try:
                            await _process_one(_t)
                        except Exception as _wexc:
                            logger.error("LongOptions parallel worker [%s]: %r",
                                         _t, _wexc, exc_info=True)

                await asyncio.gather(*[_guarded(t) for t in universe])
            else:
                for ticker in universe:
                    if not self._running:
                        break
                    try:
                        await _process_one(ticker)
                    except Exception as _wexc:
                        logger.error("LongOptions worker [%s]: %r", ticker, _wexc, exc_info=True)
                    await asyncio.sleep(2)   # inter-ticker pacing (sequential only)

            await asyncio.sleep(_LONG_TICK_SECS)   # #4: fast tick; full sweep gated by elapsed time

    async def _news_watch_loop(self) -> None:
        """
        Polls NewsSignalBus every 30s and acts on three tiers:

          Tier 1 — Trading halts: immediately close any position in a halted ticker.
          Tier 2 — Macro events: override self._macro_context.macro_stance so all
                   subsequent scoring calls see the updated regime without waiting
                   for the next MacroSynthesizer cycle (which runs every 30 min).
          Tier 3 — Ticker flags: stored in self._news_context for agents to read
                   when scoring direction (advisory +1 signal, never a hard block).

        Runs even when uw_alerts table is empty — get_news_context() returns
        an empty NewsContext silently in that case.
        """
        from agora.services.news_signals import get_news_context, NewsContext
        from agora.execution.ibkr_bridge import close_trade as _close_trade

        _alerted_halts:  set[str] = set()   # suppress repeated Discord alerts per halt
        _last_macro_ts:  str      = ""      # track which macro event we've already applied

        while self._running:
            try:
                ctx = get_news_context(str(self._settings.db_path))
                self._news_context = ctx

                # ── Tier 1: halt detection → close positions ────────────────
                if ctx.halted_tickers:
                    open_pos = self._position_mgr.get_open_positions()
                    for pos in open_pos:
                        if pos.ticker not in ctx.halted_tickers:
                            continue
                        if pos.ticker in _alerted_halts:
                            continue  # already actioned this halt
                        logger.warning(
                            "NEWS HALT: %s trading halted — closing position %s",
                            pos.ticker, pos.position_id,
                        )
                        try:
                            await asyncio.wait_for(
                                _close_trade(pos, self._settings, self._session_id), timeout=30.0
                            )
                            self._position_mgr.mark_position_closed(
                                pos.position_id, realized_pnl=pos.unrealized_pnl, source="news_halt"
                            )
                            _alerted_halts.add(pos.ticker)
                        except Exception as ce:
                            logger.error("News halt close error [%s]: %s", pos.ticker, ce)
                else:
                    # Halt lifted — clear suppression so we'd re-act on a new halt
                    _alerted_halts.clear()

                # ── Tier 2: macro event → update macro_stance ──────────────
                if ctx.macro_event and self._macro_context:
                    event_key = f"{ctx.macro_event.stance}|{ctx.macro_event.ts.isoformat()}"
                    if event_key != _last_macro_ts:
                        old = self._macro_context.macro_stance
                        new = ctx.macro_event.stance
                        if old != new:
                            logger.info(
                                "NEWS MACRO: stance %s → %s (%s)",
                                old, new, ctx.macro_event.reason,
                            )
                            self._macro_context.macro_stance = new
                            # Refresh timestamp so macro staleness gate sees this as fresh
                            self._macro_context.timestamp = ctx.macro_event.ts
                        _last_macro_ts = event_key

                # Tier 3 (ticker flags) is read passively by agents via self._news_context.

            except Exception as exc:
                logger.debug("_news_watch_loop error: %s", exc)

            await asyncio.sleep(30)

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
                    signed_pct    = (current_price - prev_price) / prev_price * 100
                    pct_move      = abs(signed_pct)

                    avg_vol    = float(vols.iloc[-6:-1].mean()) if len(vols) >= 6 else 0
                    latest_vol = float(vols.iloc[-1])
                    vol_spike  = (latest_vol > avg_vol * 2.0) if avg_vol > 0 else False

                    trigger = None
                    if pct_move >= 1.5:
                        trigger = f"move {signed_pct:+.1f}% in 30m"
                    elif vol_spike:
                        trigger = f"vol spike {latest_vol/avg_vol:.1f}× avg"

                    if trigger and ticker not in self._tier1:
                        # S1: live path = enqueue URGENT into the engine; the _priority_queue
                        # branch is the fallback consumed only by _universe_scan when the engine
                        # is absent. (Tier1 names are already swept at NORMAL by _engine_priority,
                        # so they're excluded here to avoid redundant URGENT churn.)
                        if self._scan_engine:
                            asyncio.create_task(
                                self._scan_engine.enqueue(
                                    ScanPriority.URGENT, ticker, reason=trigger
                                )
                            )
                        elif ticker not in self._priority_queue:
                            self._priority_queue.append(ticker)
                            self._priority_reasons[ticker] = trigger
                        # #4: also wake the long-options loop on this momentum/flow promotion.
                        if self._long_options_agent is not None:
                            self._long_event_tickers[ticker] = trigger
                        promoted.append(f"{ticker}({trigger})")

                    self._last_prices[ticker] = current_price

                    # Feed sector momentum detector with signed 30m move
                    sm_sig = self._intraday_sector.update(ticker, signed_pct, now_et)
                    if sm_sig:
                        # Push all sector peers to priority queue for immediate evaluation
                        for peer in _SECTOR_MAP.get(sm_sig.sector, []):
                            if peer in self._settings.etf_universe and peer not in self._priority_queue:
                                self._priority_queue.append(peer)
                                _sector_trigger = (
                                    f"sector_momentum:{sm_sig.sector}:{sm_sig.direction}"
                                    f"({sm_sig.avg_move_pct:+.1f}%)"
                                )
                                self._priority_reasons[peer] = _sector_trigger
                                if self._long_options_agent is not None:  # #4
                                    self._long_event_tickers[peer] = _sector_trigger

                if promoted:
                    logger.info("Price monitor promoted: %s", ", ".join(promoted))

                # ── Intraday macro refresh check ───────────────────
                await self._check_macro_refresh(close)

                # ── M3: surface critical IBKR connectivity/session errors ──
                await self._alert_ibkr_critical_errors()

            except Exception as exc:
                logger.debug("Price monitor error: %s", exc)

            await asyncio.sleep(300)   # run every 5 minutes

    async def _alert_ibkr_critical_errors(self) -> None:
        """M3: drain critical IBKR errors (10197 competing session, farm/connectivity drops)
        captured on the execution connections and escalate to the COO. Deduped by code so a
        persistent condition alerts once per 5-min poll, not on every quote request."""
        try:
            from trading_platform.services.ibkr_client import drain_critical_errors
            errs = drain_critical_errors()
        except Exception:
            return
        if not errs:
            return
        seen: set[int] = set()
        for e in errs:
            code = e.get("code")
            if code in seen:
                continue
            seen.add(code)
            level = "critical" if code in (10197, 1100) else "warning"
            msg = f"IBKR error {code}: {e.get('meaning')} — {e.get('raw', '')}".strip()
            try:
                await self._coo.receive_alert("execution", level, msg)
            except Exception:
                logger.warning("IBKR critical error (alert dispatch failed): %s", msg)

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
        FALLBACK tiered universe scan (S1). Used ONLY when the async UniverseScanEngine is
        disabled (use_async_scan_engine=false) or in shadow_mode. In production the engine is
        live and owns scheduling — it sweeps by staleness and now honors the Tier1 / market-
        interest priorities via _engine_priority(), so this tiered rotation + the _priority_queue
        it consumes do NOT run steady-state (only this fallback path and one cold-start, both
        gated above). Kept as the resilient fallback; not dead, but not the live scheduler.

        Event-driven tiered universe scan.

        Cadence (time-of-day aware — see _scan_interval_seconds):
          10:00–11:30 ET: every 60s (prime time)
          11:30–13:30 ET: every 300s (midday lull)
          otherwise:      every 180s

        Each cycle scans:
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
                logger.error("Evaluate ticker %s failed: %s", ticker, exc, exc_info=True)

    def _hot_interest_tickers(self) -> set[str]:
        """S1: market-interest top names, cached ~60s so the per-ticker priority classifier
        doesn't recompute the ranking on every sweep entry."""
        import time as _t
        now = _t.monotonic()
        if now - getattr(self, "_hot_interest_ts", 0.0) > 60.0:
            try:
                self._hot_interest = {
                    t for t, _ in self._market_interest.get_top_interest_tickers(n=5)
                }
            except Exception:
                self._hot_interest = set()
            self._hot_interest_ts = now
        return getattr(self, "_hot_interest", set())

    def _engine_priority(self, ticker: str) -> int:
        """S1: per-ticker scheduled-sweep priority for the LIVE scan engine. Elevates the Tier1
        always-hot set and current market-interest names to NORMAL so they sweep ahead of the
        routine BACKGROUND universe — the prioritization that previously lived only in the
        now-fallback _universe_scan tiering. URGENT/IMMEDIATE stay reserved for real-time
        event enqueues (price moves, catalysts) from the price monitor and _on_catalyst."""
        if ticker in self._tier1 or ticker in self._hot_interest_tickers():
            return ScanPriority.NORMAL
        return ScanPriority.BACKGROUND

    async def _evaluate_ticker(
        self,
        ticker: str,
        scan_priority: int = ScanPriority.BACKGROUND,
        scan_reason: str = "legacy",
    ) -> None:
        """Full signal stack for one ticker → trade recommendation → risk gate → order."""
        _sector_direction_override: str | None = None   # set by sector momentum bypass
        try:
            # Error 201 session block: paper account can't do combo orders for this ticker.
            if ticker in self._error_201_blocked:
                logger.debug("Skip %s: Error 201 blocked for this session", ticker)
                return

            # Earnings interlock: skip if we already have a pre-earnings position
            # to prevent double-entry.
            if ticker in self._pre_earnings_tickers:
                logger.debug(
                    "Skip %s: pre-earnings position already active (earnings %s)",
                    ticker, self._pre_earnings_tickers[ticker],
                )
                return

            # Cost gate (2026-06-09): the spread pipeline below (StockAnalyst + StrategySelector
            # LLM calls, IBKR chain enrich) only yields a trade when an entry is actually permitted.
            # Outside the entry window (pre-market, after-hours, weekend, open/EOD buffers) the trade
            # is blocked downstream at _entry_gate anyway — so short-circuit HERE instead of burning
            # LLM + IBKR cost on un-tradeable recommendations. (The scan engine sweeps after hours;
            # observed 128 after-hours StrategySelector calls in one day, all un-tradeable. The
            # advocate was already safe — it runs after the entry gate.)
            _entry_ok, _entry_why = self._entry_timing.is_entry_permitted()
            if not _entry_ok:
                logger.debug("Skip %s: entry window closed — %s", ticker, _entry_why)
                return

            # Earnings blackout gate: block new vol-premium entries within N days of earnings.
            # Uses IBKR CalendarReport (primary) / yfinance (fallback) via _get_next_earnings.
            # Pre-earnings plays are exempted — they are intentionally entered before earnings.
            from datetime import date as _date
            _today = _date.today()
            _blackout = self._settings.earnings_blackout_days
            _next_earn = await self._get_next_earnings(ticker)
            if _next_earn is not None:
                _days_to_earn = (_next_earn - _today).days
                if 0 <= _days_to_earn <= _blackout:
                    logger.info(
                        "EARNINGS BLACKOUT: %s — earnings in %dd (%s), blocking new entry "
                        "(blackout=%dd). Use pre-earnings play to trade into this event.",
                        ticker, _days_to_earn, _next_earn, _blackout,
                    )
                    return

            from trading_platform.services.market_data.yfinance_provider import YFinanceProvider
            from trading_platform.services.options_flow import get_gex

            provider = YFinanceProvider()
            # get_snapshot has no internal retry, so a single yfinance throttle skipped the
            # ticker for the whole cycle (25 such skips 2026-06-09, spiking during restarts).
            # One short backoff-retry recovers transient rate-limit blips before we give up.
            snap = None
            for _snap_try in range(2):
                try:
                    snap = await provider.get_snapshot(ticker)
                    if snap and snap.price:
                        break
                except Exception as _snap_exc:
                    if _snap_try == 0:
                        await asyncio.sleep(1.0)
                        continue
                    raise
                if _snap_try == 0:
                    await asyncio.sleep(1.0)
            if not snap or not snap.price:
                return

            # IV premium signal — use ticker-specific hist_vol * 1.25 as ATM IV proxy.
            # VIX/100 is SPX vol and is wrong by 3x for high-IV stocks (NVDA ~60% vs VIX ~20%).
            atm_iv_proxy = (
                snap.hist_vol_30 * 1.25 if snap.hist_vol_30
                else (snap.vix / 100 if snap.vix and snap.vix > 0 else None)
            )
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

            # Microstructure signal: OR logic — any single condition fires direction.
            # GEX negative: dealers short-gamma, direction from price vs SMA50.
            # RSI > 65 or RSI < 35: standalone momentum extreme (no GEX required).
            # IV premium: vol environment confirmed, directional edge requires GEX/RSI.
            micro_dir = "neutral"
            micro_conf = 0.5
            if gex and gex.regime == GexRegime.NEGATIVE:
                micro_conf = 0.60
                if snap.price and snap.sma_50:
                    micro_dir = "bullish" if snap.price > snap.sma_50 else "bearish"
                elif snap.rsi_14:
                    micro_dir = "bullish" if snap.rsi_14 > 50 else "bearish"
            elif snap.rsi_14 and snap.rsi_14 > 65:
                micro_dir = "bullish"
                micro_conf = 0.60
            elif snap.rsi_14 and snap.rsi_14 < 35:
                micro_dir = "bearish"
                micro_conf = 0.60
            elif iv_prem and iv_prem.signal_active:
                micro_conf = 0.55  # vol environment confirmed, no directional signal

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

            # Build catalyst signal from event when present (catalysts via async callback
            # only reach running positions; universe scan uses event_engine as the proxy).
            catalyst_input: SignalInput | None = None
            if event and event.direction != "neutral":
                catalyst_input = SignalInput(
                    source="catalyst",
                    direction=event.direction,
                    confidence=event.confidence,
                )

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
                pre_boost = conviction.total_score
                if intel_dir == "bullish" and macro_dir in ("risk_on", "neutral"):
                    conviction.total_score = min(conviction.total_score * 1.10, 95.0)
                    logger.debug("Sector intel boost (bullish) for %s: %.0f → %.0f",
                                 ticker, pre_boost, conviction.total_score)
                elif intel_dir == "bearish" and macro_dir in ("risk_off", "neutral"):
                    conviction.total_score = min(conviction.total_score * 1.10, 95.0)
                    logger.debug("Sector intel boost (bearish) for %s: %.0f → %.0f",
                                 ticker, pre_boost, conviction.total_score)

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

            # Supplementary: sector momentum when avg_move ≥ 2% (not a spike)
            _resolver_supplementary: list | None = None
            _sm_pre = self._intraday_sector.get_active_signal(ticker)
            if _sm_pre is not None and abs(_sm_pre.avg_move_pct) >= 2.0:
                _resolver_supplementary = [
                    SignalInput(
                        source="sector_momentum",
                        direction=_sm_pre.direction,
                        confidence=min(0.75, 0.50 + abs(_sm_pre.avg_move_pct) / 20.0),
                    )
                ]

            # Resolve disagreement → size multiplier
            resolution = self._resolver.resolve(
                macro=macro_input,
                microstructure=micro_input,
                catalyst=catalyst_input,
                regime=self._macro_context.macro_stance if self._macro_context else "normal",
                total_conviction=conviction.total_score,
                supplementary=_resolver_supplementary,
            )

            if resolution["gate"] == "no_trade":
                # Vol-premium bypass: when IV rank is elevated and macro allows selling,
                # sell premium non-directionally. Uses a lower conviction floor than
                # directional trades since credit spreads are non-directional.
                # IVR threshold and conviction floor scale with market regime via DynamicParams.
                ivr_threshold = self._dynamic_params.ivr_bypass_threshold
                iv_rank_elevated = snap.iv_rank is not None and snap.iv_rank >= ivr_threshold
                # At extreme IVR (≥ high_ivr_threshold, default 90), IV compression is the
                # primary risk — a conviction of 50 is noise, not edge. Require full floor.
                vol_floor = (
                    self._settings.high_ivr_conviction_floor
                    if snap.iv_rank is not None and snap.iv_rank >= self._settings.high_ivr_threshold
                    else self._settings.vol_premium_conviction_floor
                )
                conviction_ok = conviction.total_score >= vol_floor
                vol_selling_ok = (
                    iv_rank_elevated
                    and conviction_ok
                    and self._macro_context is not None
                    and self._macro_context.vol_selling_ok
                    # DataIntegrityAgent gate: if IVR feed is degraded, bypass is blocked
                    and self._data_integrity.vol_bypass_allowed()
                )
                if not vol_selling_ok:
                    # ── Sector momentum bypass ─────────────────────────────
                    # If 3+ sector peers moved ≥3% in same direction within 35m,
                    # fire a defined-risk directional spread bypassing the resolver.
                    #
                    # Backtest (2023-2025, 258 signals): avg_move > 8% signals have
                    # coin-flip (49-51%) 5-day direction accuracy — they are macro
                    # spike days (Liberation Day, FOMC surprise) that mean-revert.
                    # Regime gate: HIGH_VOL/CRISIS means every sector moves together
                    # on macro news, not sector-specific trend — same mean-reversion risk.
                    _sm_sig = self._intraday_sector.get_active_signal(ticker)
                    _macro_stance = self._macro_context.macro_stance if self._macro_context else "neutral"
                    _sm_spike_ok = (
                        _sm_sig is not None
                        and abs(_sm_sig.avg_move_pct) <= self._dynamic_params.sector_spike_cap_pct
                    )
                    _sm_regime_ok = (
                        regime_signal is None
                        or regime_signal.regime.value not in ("high_volatility", "crisis")
                    )
                    _sm_macro_ok = (
                        (_sm_sig is not None and _sm_sig.direction == "bearish"
                         and _macro_stance in ("risk_off", "neutral"))
                        or
                        (_sm_sig is not None and _sm_sig.direction == "bullish"
                         and _macro_stance in ("risk_on", "neutral"))
                    )
                    if (
                        _sm_sig is not None
                        and _sm_macro_ok
                        and _sm_spike_ok
                        and _sm_regime_ok
                        and conviction.total_score >= self._settings.min_conviction_score
                    ):
                        conviction.pillar = StrategyPillar.SECTOR_MOMENTUM
                        conviction.size_multiplier = 0.75   # conservative until track record builds
                        conviction.gate = "standard"
                        conviction.reasoning = (
                            f"Sector momentum | {_sm_sig.sector} {_sm_sig.direction} | "
                            f"avg_move={_sm_sig.avg_move_pct:+.1f}% | "
                            f"{len(_sm_sig.tickers)} peers: {', '.join(_sm_sig.tickers[:4])}"
                        )
                        _sector_direction_override = _sm_sig.direction   # picked up below at build_recommendation
                        logger.info(
                            "Sector momentum bypass for %s: %s %s avg=%.1f%% peers=%s "
                            "conviction=%.0f",
                            ticker, _sm_sig.sector, _sm_sig.direction,
                            _sm_sig.avg_move_pct, _sm_sig.tickers,
                            conviction.total_score,
                        )
                    else:
                        if _sm_sig is not None and (not _sm_spike_ok or not _sm_regime_ok):
                            logger.info(
                                "Sector momentum blocked for %s: avg_move=%.1f%% spike_ok=%s "
                                "regime_ok=%s (spike>8%% or high-vol blocks — mean-reversion risk)",
                                ticker, _sm_sig.avg_move_pct, _sm_spike_ok, _sm_regime_ok,
                            )
                        if iv_rank_elevated and not self._data_integrity.vol_bypass_allowed():
                            logger.info(
                                "No trade for %s: IVR feed degraded — vol bypass blocked",
                                ticker,
                            )
                        elif iv_rank_elevated and not conviction_ok:
                            floor_label = (
                                f"high-IVR floor (IVR={snap.iv_rank:.0f}≥{self._settings.high_ivr_threshold:.0f})"
                                if snap.iv_rank is not None and snap.iv_rank >= self._settings.high_ivr_threshold
                                else "vol-premium floor"
                            )
                            logger.info(
                                "No trade for %s: vol bypass blocked — conviction %.0f < %.0f %s",
                                ticker, conviction.total_score, vol_floor, floor_label,
                            )
                        else:
                            logger.info("No trade for %s: %s (IVR=%s)",
                                        ticker, resolution["reason"],
                                        f"{snap.iv_rank:.0f}" if snap.iv_rank else "n/a")
                        return
                # Override gate for vol-premium play — ONLY when vol-selling was approved
                # (vol_selling_ok guarantees iv_rank is not None). If we instead arrived
                # here via the sector-momentum bypass (vol_selling_ok is False), keep that
                # SECTOR_MOMENTUM pillar and do NOT clobber it — and never format a None IVR.
                if vol_selling_ok:
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

            # Open decision chain — ticker has cleared conviction + resolver gates.
            # All subsequent agents journal against this chain_id.
            _chain_id = _start_chain(
                str(self._settings.db_path),
                ticker,
                triggered_by=scan_reason,
                session_id=self._session_id,
                conviction=conviction.total_score,
            )

            # ── Stock Analyst thesis gate (Phase 3) ──────────────────
            # Analyst always returns thesis (shadow mode only means "don't gate execution").
            # thesis is available to all downstream agents regardless of shadow mode.
            _thesis = None
            if (
                self._stock_analyst is not None
                and conviction.total_score >= self._settings.stock_analyst_min_conviction
            ):
                _thesis = await self._stock_analyst.analyze(
                    ticker=ticker,
                    conviction_score=conviction.total_score,
                    snapshot=snap,
                    macro_context=self._macro_context,
                    gex=gex,
                    iv_premium=iv_prem,
                    event_signal=event,
                    sector_intel=sector_intel,
                    decision_id=_chain_id,
                )
                # Gate in live mode only — shadow mode logs but never blocks
                if (
                    _thesis is not None
                    and _thesis.decision == "no_thesis"
                    and not self._stock_analyst.shadow_mode
                ):
                    _complete_chain(
                        str(self._settings.db_path), _chain_id, "analyst_blocked",
                        strategy=str(getattr(conviction, "pillar", "")),
                    )
                    logger.info(
                        "Analyst blocked %s (conviction=%.0f): %s",
                        ticker, conviction.total_score,
                        _thesis.raw.get("reason", "no_thesis"),
                    )
                    return
                elif _thesis is not None and _thesis.decision == "no_thesis":
                    logger.info(
                        "Analyst shadow no_thesis for %s — continuing (shadow mode)",
                        ticker,
                    )

            # Get options chain and build recommendation.
            # Load one expiry per DTE bracket — wrapped in to_thread so the
            # blocking yfinance calls don't stall the event loop.
            from datetime import date as _date
            _DTE_BRACKETS = [(5, 22), (23, 37), (38, 65), (66, 90)]

            def _fetch_chains_sync(t: str) -> dict:
                # #1/#2: shared snapshot — expiries in the spread window that overlap the long
                # loop's [14,30] are served from one cache, and both pipelines see the same chain.
                ms = get_market_snapshot()
                chain_dict: dict = {}
                today_d = _date.today()
                filled: set[int] = set()
                for exp in ms.expiries(t):
                    try:
                        exp_date = _date.fromisoformat(exp)
                    except ValueError:
                        continue
                    dte = (exp_date - today_d).days
                    for i, (lo, hi) in enumerate(_DTE_BRACKETS):
                        if i not in filled and lo <= dte <= hi:
                            c = ms.option_chain(t, exp)
                            if c is not None:
                                chain_dict[exp] = c
                                filled.add(i)
                            break
                    if len(filled) == len(_DTE_BRACKETS):
                        break
                return chain_dict

            try:
                chain_dict = await asyncio.wait_for(
                    asyncio.to_thread(_fetch_chains_sync, ticker), timeout=45.0
                )
            except asyncio.TimeoutError:
                _complete_chain(str(self._settings.db_path), _chain_id, "timeout")
                logger.warning("Options chain fetch timed out for %s — skipping ticker", ticker)
                return
            if not chain_dict:
                _complete_chain(str(self._settings.db_path), _chain_id, "no_trade")
                logger.info("No options chain data for %s — skipping", ticker)
                return

            logger.info("Options chain loaded for %s: %d expiries %s",
                        ticker, len(chain_dict), list(chain_dict.keys()))

            # ── Phase B: enrich the chain with REAL IBKR prices for strike selection ──
            # Override yfinance bid/ask/IV on the target-DTE OTM strikes so the rules engine
            # picks strikes (credit-per-delta) on real prices, not stale yfinance. enrich_chain
            # has a hard internal fallback; this outer guard is belt-and-suspenders so a chain-
            # enrich problem can NEVER break the scan.
            if getattr(self._settings, "use_ibkr_chain_pricing", False):
                try:
                    chain_dict = await enrich_chain(ticker, chain_dict, snap.price, self._settings)
                except Exception as _ec_exc:
                    logger.warning("Chain enrich error for %s (%s) — using yfinance", ticker, _ec_exc)

            # ── Rules engine (always runs as primary/fallback) ────────
            # direction_override priority: analyst thesis (live) > sector momentum > None
            _direction_hint = (
                _thesis.direction
                if _thesis and _thesis.direction and not self._stock_analyst.shadow_mode
                else None
            ) if self._stock_analyst else None
            if _direction_hint is None and _sector_direction_override:
                _direction_hint = _sector_direction_override

            recommendation = self._strategy.build_recommendation(
                conviction=conviction,
                spot=snap.price,
                options_chain=chain_dict,
                gex=gex,
                direction_override=_direction_hint,
                iv_rank=snap.iv_rank,
                vix=snap.vix,
                dynamic_params=self._dynamic_params,
            )

            if not recommendation:
                _complete_chain(str(self._settings.db_path), _chain_id, "no_trade",
                                strategy=str(getattr(conviction, "pillar", "")))
                logger.info("No recommendation built for %s (strategy returned None)", ticker)
                return

            # ── StrategySelectorAgent (Phase 6) ───────────────────────
            # Shadow: journals selection, rules engine result used for execution.
            # Live: override decision can change strategy type (rules engine re-runs).
            # Run regardless of whether analyst produced a thesis — selector validates
            # the rules engine output even when no analyst thesis is available
            # (e.g. vol_premium bypass where conviction < stock_analyst_min_conviction).
            if self._strategy_selector:
                _selection = await self._strategy_selector.select(
                    ticker=ticker,
                    thesis=_thesis,
                    conviction=conviction,
                    snapshot=snap,
                    options_chain=chain_dict,
                    rules_recommendation=recommendation,
                    decision_id=_chain_id,
                )
                # Live mode: if selector overrides, re-run rules engine with new type
                if (
                    _selection
                    and not self._strategy_selector.shadow_mode
                    and _selection.decision == "no_structure"
                ):
                    _complete_chain(str(self._settings.db_path), _chain_id, "no_trade",
                                    strategy="selector_no_structure")
                    logger.info("StrategySelector: no_structure for %s — %s", ticker, _selection.rationale)
                    return

            # ── IV Skew informational log (non-blocking) ─────────────────
            # Steep put skew on a bullish trade signals puts are rich;
            # note this so traders can consider put-credit structures instead.
            try:
                from agora.ops.skew_analyzer import analyze_skew as _analyze_skew
                _skew_result = _analyze_skew(chain_dict, snap.price)
                if _skew_result is not None:
                    if (
                        _skew_result.skew_regime == "steep_put_skew"
                        and recommendation.direction == "bullish"
                    ):
                        logger.info(
                            "Skew alert [%s]: steep put skew (skew_pct=%.1%%) on bullish trade — "
                            "puts are rich; consider put-credit structure (CSP/bull-put-spread) "
                            "over long call. %s",
                            ticker, _skew_result.skew_pct, _skew_result.reason_str,
                        )
                    elif _skew_result.skew_regime == "call_skewed":
                        logger.info(
                            "Skew alert [%s]: call skew detected (skew_pct=%.1%%) — "
                            "calls are expensive; prefer call-credit structures. %s",
                            ticker, _skew_result.skew_pct, _skew_result.reason_str,
                        )
                    else:
                        logger.debug(
                            "Skew [%s]: regime=%s skew_pct=%.1%%",
                            ticker, _skew_result.skew_regime, _skew_result.skew_pct,
                        )
            except Exception as _skew_exc:
                logger.debug("Skew analysis failed for %s: %s", ticker, _skew_exc)

            await self._submit_recommendation(
                recommendation, ticker, snap.price,
                chain_id=_chain_id, thesis=_thesis,
            )

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

        # #4 (extended): wake the long-options loop on DIRECTIONAL discovery events. This single
        # convergence point carries SmartMoney institutional FLOW sweeps, IBKR news, and analyst
        # revisions — exactly the bursts a directional long wants, and ones that may not fit a
        # spread structure the catalyst pillar builds. We only promote (the long loop re-scores
        # independently within ~60s); all of _on_catalyst's gates above (ETF-skip, pre-earnings &
        # open-position dedup, cooldown) plus the long loop's earnings-blackout already filter.
        if (self._long_options_agent is not None
                and str(getattr(catalyst, "direction", "")).lower() in ("bullish", "bearish")):
            self._long_event_tickers[catalyst.ticker] = (
                f"catalyst:{catalyst.catalyst_type}:{catalyst.direction}")

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
                _catalyst_chain_id = _start_chain(
                    str(self._settings.db_path), catalyst.ticker,
                    triggered_by="catalyst", session_id=self._session_id,
                    conviction=conviction.total_score,
                )
                await self._submit_recommendation(
                    recommendation, catalyst.ticker, spot,
                    triggered_by="catalyst", chain_id=_catalyst_chain_id,
                )
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
                _earnings_chain_id = _start_chain(
                    str(self._settings.db_path), setup.ticker,
                    triggered_by="earnings", session_id=self._session_id,
                    conviction=conviction_score,
                )
                await self._submit_recommendation(
                    recommendation, setup.ticker, spot,
                    earnings_date=setup.earnings_date,
                    is_pre_earnings=True,
                    triggered_by="earnings",
                    chain_id=_earnings_chain_id,
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
                _postearnings_chain_id = _start_chain(
                    str(self._settings.db_path), result.ticker,
                    triggered_by="earnings", session_id=self._session_id,
                    conviction=conviction.total_score,
                )
                await self._submit_recommendation(
                    recommendation, result.ticker, spot,
                    triggered_by="earnings", chain_id=_postearnings_chain_id,
                )
        except Exception as exc:
            logger.error("Post-earnings trade build failed for %s: %s", result.ticker, exc)


    def _event_risk_assessment(self, rec: Any, ticker: str, spot: float) -> tuple[str, str]:
        """
        Surgical macro-event (FOMC/CPI/NFP) handling. Returns (action, note) where
        action ∈ {'allow','size_down','block'} and note is the QUANTIFIED context the
        advocate reads (so it reasons on the real event/days/expected-move instead of an
        assumed/mis-identified one — observed: advocate blocking on 'FOMC tomorrow' when
        the real event was NFP and FOMC was 13 days out).

        Asymmetric by risk shape:
          • event DAY (calendar 'avoid') → hard block new multi-day risk.
          • bounded-risk longs → size-down (keep 60%), no cushion gate.
          • short-premium spreads → require nearest short strike ≥ 1.25× expected move;
            else block as fragile. Survivors are size-reduced (keep 40%).
        Mutates rec.contracts on size-down. No-op when flag off or no event within 2 days.
        """
        if not getattr(self._settings, "event_surgical_gate_enabled", True):
            return ("allow", "")
        try:
            cal = get_macro_calendar()
            days, event = cal.days_to_next_event()
            risk = cal.get_risk_level()
        except Exception as exc:
            logger.debug("event assessment failed [%s]: %s", ticker, exc)
            return ("allow", "")
        if days is None or days > 2:
            return ("allow", "")   # no macro event within the 2-day horizon

        vix = float(getattr(self._macro_context, "vix", 18.0) or 18.0)
        # VIX-implied 1-day move, scaled 1.5× because scheduled events move more than a
        # typical session. Conservative first-pass; the attribution slice will refine it.
        exp_move = (vix / 100.0) / (252 ** 0.5) * 1.5
        strat = str(getattr(rec.strategy, "value", rec.strategy))
        is_long = strat in ("long_call", "long_put")

        if risk == "avoid" or days == 0:
            # ── C-lite: time-aware, event-type-aware event-day handling ──────────
            # Pre-market prints (NFP/CPI/PPI, ~8:30 ET) resolve before the open — block
            # pre-release through a settle window, then ALLOW size-reduced + tagged entries
            # to ride the post-event momentum (the strategy's core edge). Intraday events
            # (FOMC ~14:00 ET + presser) reverse violently all afternoon → blocked all day.
            ev_lower   = (event or "").lower()
            is_fomc    = any(k in ev_lower for k in ("fomc", "fed ", "rate decision", "powell", "interest rate"))
            reopen_on  = getattr(self._settings, "event_gate_intraday_reopen", True)
            settle_str = getattr(self._settings, "event_gate_settle_et", "10:00")
            try:
                _sh, _sm = (int(x) for x in settle_str.split(":"))
            except Exception:
                _sh, _sm = 10, 0
            now_et      = datetime.now(tz=ET)
            past_settle = (now_et.hour, now_et.minute) >= (_sh, _sm)
            # Only the genuine event-day, pre-market type, past settle, with flag on, reopens.
            eligible = reopen_on and (days == 0) and (not is_fomc) and past_settle
            if not eligible:
                if reopen_on and days == 0 and not is_fomc and not past_settle:
                    reason = (f"{event} is TODAY (calendar=avoid) — pre-settle block "
                              f"(entries reopen {settle_str} ET, post-event, size-reduced)")
                elif is_fomc:
                    reason = f"{event} is TODAY (calendar=avoid) — FOMC/intraday event blocked ALL day (reversal risk)"
                else:
                    reason = f"{event} is TODAY (calendar=avoid) — event-day block on new multi-day risk"
                return ("block", reason)
            # Past the settle window on a pre-market event day → ALLOW, size-reduced + tagged.
            try:
                rec.event_day = True
            except Exception:
                pass
            _pre_ev = rec.contracts
            rec.contracts = max(1, int(_pre_ev * 0.5))   # half size on event days (extra conservative)
            logger.info("Event gate [%s]: %s TODAY but past %s ET settle — ALLOW post-event "
                        "(size %d→%d, event_day tagged)", ticker, event, settle_str, _pre_ev, rec.contracts)
            note = (f"EVENT-DAY POST-SETTLE: {event} resolved pre-market (now past {settle_str} ET). "
                    f"Entry ALLOWED to capture post-event momentum, size-reduced ({_pre_ev}→{rec.contracts}) "
                    f"and tagged event_day. Elevated whipsaw risk — block only on a distinct, "
                    f"high-severity, well-evidenced risk, not on event proximity alone.")
            return ("size_down", note)

        cushion_txt = ""
        if not is_long:
            short_strikes = [l.strike for l in (getattr(rec, "legs", None) or [])
                             if str(getattr(l, "action", "")).lower() == "sell" and getattr(l, "strike", None)]
            if short_strikes and spot > 0:
                nearest = min(short_strikes, key=lambda k: abs(k - spot))
                cushion = abs(nearest - spot) / spot
                needed = 1.25 * exp_move
                cushion_txt = f" Short-strike cushion {cushion*100:.1f}% vs needed {needed*100:.1f}%."
                if cushion < needed:
                    return ("block",
                            f"{event} in {days}d: short strike too close "
                            f"({cushion*100:.1f}% < {needed*100:.1f}% of a ~{exp_move*100:.1f}% expected move) — fragile")

        _pre = rec.contracts
        _factor = 0.6 if is_long else 0.4
        rec.contracts = max(1, int(_pre * _factor))
        if rec.contracts != _pre:
            logger.info("Event gate [%s]: %s in %dd — size %d→%d (%s)",
                        ticker, event, days, _pre, rec.contracts, "long" if is_long else "spread")
        note = (f"EVENT RISK (quantified, accurate): {event} in {days}d, calendar risk={risk}. "
                f"Expected 1-day move ~{exp_move*100:.1f}%.{cushion_txt} Structure assessed and "
                f"size-reduced ({_pre}→{rec.contracts}); risk is bounded/cushioned. Do NOT block on "
                f"event proximity alone — only on a distinct, high-severity, well-evidenced risk.")
        return ("size_down", note)

    async def _entry_gate(
        self, rec: Any, ticker: str, spot: float, positions: list,
        earnings_date: Any = None, is_pre_earnings: bool = False, chain_id: str = "",
    ) -> tuple[bool, str]:
        """
        SINGLE shared pre-trade risk gate for BOTH strategies (spreads + long options).
        Deterministic chain, one source of truth — no parity drift:
          timing -> kill switch -> macro calendar -> compliance -> risk council
          -> correlation (block / size-reduce) -> devils-advocate.
        Returns (ok, reason); ok=False means blocked. Strategy-specific steps stay in each
        pipeline and run BEFORE this gate where they mutate inputs it reads — spreads apply
        VIX/macro position sizing (risk council reads contract count) and sector/valuation
        conviction adjustment (devils reads conviction_score) first; the long pipeline runs
        its Opus vetter before and the LLM advocate after. Gates are AND-conjoined, so the
        set of blocked trades is identical regardless of which pipeline calls this.
        """
        def _block(reason: str, gates: list) -> tuple[bool, str]:
            if chain_id:
                _complete_chain(str(self._settings.db_path), chain_id, "risk_blocked",
                                gates_passed=gates)
            return (False, reason)

        permitted, _why = self._entry_timing.is_entry_permitted()
        if not permitted:
            return _block(f"timing: {_why}", [])
        if self._risk.is_kill_switch_active():
            return _block("kill switch active", ["timing"])
        _can_trade, _cal_why = get_macro_calendar().should_trade()
        if not _can_trade:
            return _block(f"macro calendar: {_cal_why}", ["timing"])
        # Surgical macro-event handling: hard-block event day, size-down adjacent, cushion-check
        # spreads, and stamp accurate event context onto the rec for the advocate (replaces the
        # advocate's blanket over-blocking on assumed/mis-identified events). Runs before risk
        # council so the size-down is reflected in the greeks/contract checks.
        _evt_action, _evt_note = self._event_risk_assessment(rec, ticker, spot)
        rec.event_mitigation = _evt_note
        if _evt_action == "block":
            return _block(f"event: {_evt_note}", ["timing", "macro_cal"])
        comp = self._compliance.check_trade(rec, positions)
        if not comp.get("compliant", True):
            return _block(f"compliance: {comp.get('reason')}", ["timing", "macro_cal"])
        for _w in comp.get("warnings", []):
            logger.warning("Compliance warning [%s]: %s", ticker, _w)
        greeks = self._position_mgr.get_portfolio_greeks()
        regime = self._macro_context.macro_stance if self._macro_context else "neutral"
        rr = self._risk.approve_trade(rec, greeks, positions, spot, regime=regime)
        if not rr.get("approved", False):
            return _block(f"risk council: {rr.get('reason')}", ["timing", "macro_cal", "compliance"])
        # Correlation — block near-duplicate same-direction bets, else size-reduce.
        try:
            _corr = self._correlation_monitor.check(
                new_ticker=ticker, existing_positions=positions,
                direction=getattr(rec, "direction", "neutral"),
            )
            if _corr.risk_level == "block":
                return _block(f"correlation: {_corr.block_reason}",
                              ["timing", "macro_cal", "compliance", "risk"])
            if _corr.conviction_adj < 0:
                _pre = rec.contracts
                _factor = 0.5 if _corr.risk_level == "high" else 0.75
                rec.contracts = max(1, int(_pre * _factor))
                logger.info("Correlation %s [%s]: contracts %d → %d | %s",
                            _corr.risk_level, ticker, _pre, rec.contracts, _corr.block_reason)
        except Exception as _cex:
            logger.debug("Correlation check failed [%s]: %s", ticker, _cex)
        # DevilsAdvocate — 5-check deterministic checklist.
        _da_ok, _da_reason, _ = _devils_advocate(
            recommendation=rec, positions=positions, macro_context=self._macro_context,
            earnings_date=earnings_date, is_pre_earnings=is_pre_earnings,
        )
        if not _da_ok:
            return _block(f"devils-advocate: {_da_reason}",
                          ["timing", "macro_cal", "compliance", "risk", "correlation"])
        return (True, "")

    async def _long_options_risk_gates(
        self, rec: Any, ticker: str, spot: float, earnings_date: Any = None,
        vetter_approved: bool = False,
    ) -> bool:
        """Run the shared heavy gates on a long-options entry so it faces the SAME
        review as the main (spread) pipeline — previously it bypassed all of these:
        entry-timing -> kill switch -> macro calendar -> compliance -> risk council
        -> correlation -> devils advocate -> LLM advocate (fail-closed).
        Returns True only when the trade clears every gate."""
        # Shared deterministic chain (single source of truth — see _entry_gate).
        positions = self._position_mgr.get_open_positions()
        _ok, _why = await self._entry_gate(
            rec, ticker, spot, positions, earnings_date=earnings_date, is_pre_earnings=False)
        if not _ok:
            logger.info("LongOptions BLOCKED by entry gate [%s]: %s", ticker, _why)
            return False

        # LLM advocate — adversarial review; fail closed when unavailable in live mode.
        if self._advocate is not None:
            # Give the advocate real direction/horizon context (was thesis=None, which
            # left it reviewing structure+signals only — a materially weaker gate).
            from types import SimpleNamespace
            _thesis = SimpleNamespace(
                direction=getattr(rec, "direction", "neutral"),
                magnitude_pct=None,
                horizon_days=getattr(self._settings, "long_options_max_hold_days", 5),
                confidence_pct=getattr(rec, "conviction_score", None),
                strategy_family="long_directional",
                kill_conditions=[],
            )
            try:
                verdict = await self._advocate.review(
                    ticker=ticker, recommendation=rec, thesis=_thesis,
                    positions=positions, macro_context=self._macro_context, decision_id="",
                )
            except Exception as _aex:
                verdict = None
                logger.debug("LongOptions advocate error [%s]: %s", ticker, _aex)
            live = not self._advocate.shadow_mode
            if verdict is None and live and self._settings.advocate_fail_closed:
                # L2: the general advocate is REDUNDANT with the dedicated long-options vetter.
                # If the live vetter already cleared this trade, a transient advocate outage
                # (None) falls OPEN instead of killing a gate-passed trade — the vetter IS the
                # LLM review here. Conviction-2 trades (no vetter) still fail closed.
                if vetter_approved:
                    logger.info("LongOptions advocate unavailable [%s] — vetter already "
                                "approved, falling open (L2)", ticker)
                else:
                    logger.warning("LongOptions BLOCKED (fail-closed): advocate unavailable [%s]", ticker)
                    return False
            if verdict is not None and live and verdict.is_block:
                logger.info("LongOptions BLOCKED by advocate [%s]: %s",
                            ticker, (verdict.verdict_reasoning or "")[:80])
                return False
        return True

    # ── Execution stubs (wired to IBKR in live mode) ──────────────

    async def _submit_recommendation(
        self,
        recommendation: Any,
        ticker: str,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """M1 idempotency wrapper around _submit_recommendation_inner.

        Blocks a second concurrent submission of the same ticker while the first is
        still in flight (network/LLM latency on the entry path is several seconds, a
        window in which another scan trigger could otherwise fire a duplicate order).
        """
        if ticker in self._inflight_tickers:
            logger.info("ENTRY SKIPPED: %s submission already in flight (M1 dedup)", ticker)
            return
        self._inflight_tickers.add(ticker)
        try:
            await self._submit_recommendation_inner(recommendation, ticker, *args, **kwargs)
        finally:
            self._inflight_tickers.discard(ticker)

    def _global_exposure_ok(self, new_risk_dollars: float = 0.0) -> tuple[bool, str]:
        """#3: single GLOBAL ceiling both pipelines consult before opening — bounds the SUM of
        the spread + long-options books (count and deployed capital), which the per-pipeline
        caps never did. Returns (ok, reason). Defaults are non-binding (data-collection posture).
        """
        positions = self._position_mgr.get_open_positions()
        cap_n = self._settings.max_total_open_positions
        if len(positions) >= cap_n:
            return False, f"global position ceiling {len(positions)}/{cap_n}"
        cap_pct = self._settings.max_total_capital_deployed_pct
        if cap_pct < 1.0:
            deployed = sum(abs(getattr(p, "max_loss_dollars", 0.0) or 0.0) for p in positions)
            limit = self._settings.account_size * cap_pct
            if deployed + max(0.0, new_risk_dollars) > limit:
                return (False,
                        f"global capital ceiling ${deployed + new_risk_dollars:.0f}/${limit:.0f} "
                        f"({cap_pct:.0%} of ${self._settings.account_size:.0f})")
        return True, ""

    async def _submit_recommendation_inner(
        self,
        recommendation: Any,
        ticker: str,
        spot: float,
        earnings_date: Any = None,
        is_pre_earnings: bool = False,
        triggered_by: str = "universe_scan",
        chain_id: str = "",
        thesis: Any = None,       # AnalystThesis | None — for AdvocateAgent context
    ) -> None:
        """Strategy-specific sizing/conviction → shared _entry_gate → LLM debate → IBKR."""
        # ── Spread-specific pre-processing — MUST precede the shared gate because it
        # mutates inputs the gate reads: position sizing (risk council reads contract
        # count) and conviction adjustment (devils-advocate reads conviction_score).

        # Macro-calendar position sizing (caution-day size reduction; the avoid-day BLOCK
        # is enforced inside _entry_gate via should_trade()).
        _size_mult = get_macro_calendar().position_size_multiplier()
        if _size_mult < 1.0:
            recommendation.contracts = max(1, int(recommendation.contracts * _size_mult))
            logger.info("Macro calendar caution: %s size → %.0f%% (%d contracts)",
                        ticker, _size_mult * 100, recommendation.contracts)

        # VIX stress mode size reduction.
        if self._circuit_breaker.vix_stress_mode:
            recommendation.size_multiplier *= self._circuit_breaker.size_multiplier_override
            recommendation.contracts = max(
                1, int(recommendation.contracts * self._circuit_breaker.size_multiplier_override))
            logger.info("VIX stress mode: %s size → %d contracts", ticker, recommendation.contracts)

        open_positions = self._position_mgr.get_open_positions()
        strategy_str = getattr(recommendation, "strategy", "unknown")
        if hasattr(strategy_str, "value"):
            strategy_str = strategy_str.value
        mid_price = abs(recommendation.entry_debit_credit / max(1, recommendation.contracts * 100))

        # Execution cooldown — block re-submission of a ticker that recently failed to fill.
        _cooldown_until = self._exec_cooldowns.get(ticker)
        if _cooldown_until and datetime.now(tz=timezone.utc) < _cooldown_until:
            _mins_left = (_cooldown_until - datetime.now(tz=timezone.utc)).seconds // 60
            logger.debug("ENTRY SKIPPED by exec cooldown: %s — %d min remaining", ticker, _mins_left)
            return

        # Open-combo limit — IBKR paper caps riskless-combination orders (Error 201).
        if len(open_positions) >= self._settings.gtc_max_open_combo_orders:
            logger.info("ENTRY BLOCKED by open-combo limit: %d/%d active | %s",
                        len(open_positions), self._settings.gtc_max_open_combo_orders, ticker)
            if chain_id:
                _complete_chain(str(self._settings.db_path), chain_id, "rejected",
                                gates_passed=["combo_limit"])
            return

        # #3: global exposure ceiling across BOTH pipelines (count + deployed capital).
        _gok, _greason = self._global_exposure_ok(
            new_risk_dollars=abs(getattr(recommendation, "max_loss_dollars", 0.0) or 0.0))
        if not _gok:
            logger.info("ENTRY BLOCKED by %s | %s", _greason, ticker)
            if chain_id:
                _complete_chain(str(self._settings.db_path), chain_id, "rejected",
                                gates_passed=["global_exposure"])
            return

        # Sector rotation + valuation conviction adjustment (modulates conviction_score
        # before the gate's devils-advocate conviction-floor check).
        _fin_adj_total = 0
        _fin_adj_reasons: list[str] = []
        try:
            _sector_sig = self._sector_monitor.get_signal(ticker)
            if _sector_sig.conviction_adj != 0:
                _fin_adj_total += _sector_sig.conviction_adj
                _fin_adj_reasons.append(f"sector_rs={_sector_sig.conviction_adj:+d} ({_sector_sig.reason_str})")
        except Exception as _sec_exc:
            logger.debug("Sector rotation check failed for %s: %s", ticker, _sec_exc)
        try:
            _val_result = self._valuation_gate.evaluate(ticker)
            if _val_result.conviction_adj != 0:
                _fin_adj_total += _val_result.conviction_adj
                _fin_adj_reasons.append(f"valuation={_val_result.conviction_adj:+d} ({_val_result.reason_str})")
        except Exception as _val_exc:
            logger.debug("Valuation check failed for %s: %s", ticker, _val_exc)
        if _fin_adj_total != 0:
            _pre_adj = recommendation.conviction_score
            recommendation.conviction_score = float(max(0.0, min(100.0, _pre_adj + _fin_adj_total)))
            logger.info("Finance intel adj for %s: %.0f → %.0f (Δ%+d) | %s",
                        ticker, _pre_adj, recommendation.conviction_score, _fin_adj_total,
                        " | ".join(_fin_adj_reasons))

        # ── Shared deterministic entry gate (single source of truth; same as long opts) ──
        # timing → kill → macro → compliance → risk → correlation → devils-advocate.
        _gate_ok, _gate_why = await self._entry_gate(
            recommendation, ticker, spot, open_positions,
            earnings_date=earnings_date, is_pre_earnings=is_pre_earnings, chain_id=chain_id,
        )
        if not _gate_ok:
            logger.info("ENTRY BLOCKED by gate: %s | %s", ticker, _gate_why)
            return

        # ── Reprice & re-gate the chosen structure on REAL IBKR quotes (Option 2, Phase A) ──
        # The rules engine built this on yfinance mids, which are sometimes badly stale
        # (COST 2026-06-08: yfinance 1.85 vs real fill 8.55). Verify actual prices here —
        # BEFORE the expensive debate-LLM and record_attempt — so the decision economics
        # (R/R, credit, liquidity) are real and bad-data entries die at the decision. A
        # missing IBKR quote keeps the yfinance rec; the execution-layer gates are the backstop.
        try:
            _leg_quotes = await reprice_legs(recommendation, self._settings)
        except Exception as _rq_exc:
            _leg_quotes = None
            logger.warning("IBKR reprice failed for %s (%s) — proceeding on yfinance", ticker, _rq_exc)
        if _leg_quotes:
            _ok, _repriced, _reason = self._strategy.reprice_and_revalidate(recommendation, _leg_quotes)
            if not _ok:
                logger.info("REPRICE REJECT %s: %s", ticker, _reason)
                if chain_id:
                    _complete_chain(str(self._settings.db_path), chain_id, "rejected",
                                    strategy=str(strategy_str), gates_passed=["reprice"])
                return
            if _repriced is not None and _repriced is not recommendation:
                logger.info("REPRICED %s on IBKR: entry %+.2f→%+.2f rr→%.2f",
                            ticker, recommendation.entry_debit_credit,
                            _repriced.entry_debit_credit, _repriced.reward_risk_ratio)
                recommendation = _repriced
                # Keep the slippage baseline consistent with the repriced (real) entry.
                mid_price = abs(recommendation.entry_debit_credit / max(1, recommendation.contracts * 100))

        # Record the execution attempt now that the trade has cleared every deterministic
        # gate — fill-rate denominator = trades that actually reach submission.
        self._exec_quality.record_attempt(ticker, str(strategy_str), mid_price)

        # 4f. Debate gate: AdvocateAgent + ThesisDefenderAgent run in parallel.
        # Advocate generates 3 failure modes; defender generates 3 success modes.
        # A strong defense (confidence ≥ 0.65 + thesis_strength="strong") moderates
        # a BLOCK to CAUTION, letting the trade through. Without a defender, behaviour
        # is identical to the previous single-advocate gate.
        #
        # Cooldown: 2h per ticker. The thesis doesn't change between 30-min scan cycles —
        # without this, a high-scoring ticker that fails a later gate re-fires the full
        # debate every cycle (observed: 13 calls on POWL in 3h = $0.80 wasted).
        _advocate_verdict = None
        _defender_verdict = None
        if self._advocate:
            import time as _time_mod
            _now_ts      = _time_mod.monotonic()
            _cached      = self._advocate_cache.get(ticker)
            _cur_macro   = str(getattr(self._macro_context, "macro_stance", "neutral") or "neutral")
            _cur_spot    = spot if spot > 0 else 1.0
            _cur_pos_cnt = len(open_positions)

            _use_cache = False
            if _cached:
                _c_ts, _c_macro, _c_spot, _c_pos_cnt, _, _ = _cached
                _time_ok  = (_now_ts - _c_ts) < self._ADVOCATE_COOLDOWN_SECS
                _macro_ok = _c_macro == _cur_macro
                _spot_ok  = abs(_cur_spot - _c_spot) / max(_c_spot, 1.0) < 0.03
                # Tolerant position-count match. The per-ticker debate verdict is driven by
                # THIS ticker's setup + macro, not the exact portfolio size — concentration is
                # already enforced deterministically upstream (risk council + correlation gate).
                # An exact match meant any unrelated long open/close busted EVERY ticker's
                # cached verdict, defeating the 3h cooldown (observed: 150-200 calls/day). A ±3
                # band keeps the cache useful while still re-running on a materially changed book.
                _pos_ok   = abs(_cur_pos_cnt - _c_pos_cnt) <= 3
                _use_cache = _time_ok and _macro_ok and _spot_ok and _pos_ok
                if _cached and not _use_cache:
                    reasons = []
                    if not _time_ok:  reasons.append(f"age>{self._ADVOCATE_COOLDOWN_SECS/3600:.0f}h")
                    if not _macro_ok: reasons.append(f"macro {_c_macro}→{_cur_macro}")
                    if not _spot_ok:  reasons.append(f"spot moved {abs(_cur_spot-_c_spot)/_c_spot:.1%}")
                    if not _pos_ok:   reasons.append(f"positions {_c_pos_cnt}→{_cur_pos_cnt}")
                    logger.debug("Advocate cache STALE [%s]: %s — re-running", ticker, ", ".join(reasons))

            if _use_cache:
                _advocate_verdict = _cached[4]
                _defender_verdict = _cached[5]
                logger.debug(
                    "Advocate/Defender cache hit [%s] — %.0fm ago macro=%s spot=%.2f, reusing verdict=%s",
                    ticker,
                    (_now_ts - _cached[0]) / 60,
                    _cur_macro,
                    _cur_spot,
                    getattr(_advocate_verdict, "verdict", "?"),
                )
            else:
                # Run the Advocate first. The Defender's ONLY role downstream is to
                # moderate an Advocate BLOCK to CAUTION — so it is consulted ONLY when the
                # advocate actually blocks. On a PASS (the majority of trades) the defender
                # verdict was computed and then never read: pure waste (~$2.4/day, ~150
                # calls). Sequencing it behind a block changes no outcome.
                try:
                    _advocate_verdict = await self._advocate.review(
                        ticker=ticker,
                        recommendation=recommendation,
                        thesis=thesis,
                        positions=open_positions,
                        macro_context=self._macro_context,
                        decision_id=chain_id,
                    )
                except Exception:
                    _advocate_verdict = None
                _defender_verdict = None
                if (
                    self._defender is not None
                    and _advocate_verdict is not None
                    and getattr(_advocate_verdict, "is_block", False)
                ):
                    try:
                        _defender_verdict = await self._defender.defend(
                            ticker=ticker,
                            recommendation=recommendation,
                            thesis=thesis,
                            positions=open_positions,
                            macro_context=self._macro_context,
                            decision_id=chain_id,
                        )
                    except Exception:
                        _defender_verdict = None
                # Cache verdict with fingerprint snapshot — but never cache an
                # error (None advocate verdict): caching it would propagate the
                # failure across the whole cooldown window. Leaving it uncached
                # forces a fresh review attempt on the next trade.
                if _advocate_verdict is not None:
                    self._advocate_cache[ticker] = (
                        _now_ts, _cur_macro, _cur_spot, _cur_pos_cnt,
                        _advocate_verdict, _defender_verdict,
                    )

            # Fail-closed gate: the advocate is LIVE but produced no verdict, meaning
            # the adversarial review never ran (API error, timeout, credit exhaustion —
            # review() returns None only on exception). Do NOT submit an un-reviewed
            # trade: a degraded risk gate must fail closed, not silently fall through.
            if (
                _advocate_verdict is None
                and not self._advocate.shadow_mode
                and self._settings.advocate_fail_closed
            ):
                _complete_chain(
                    str(self._settings.db_path), chain_id, "risk_blocked",
                    strategy=str(strategy_str),
                    gates_passed=["timing", "macro_cal", "compliance", "risk",
                                  "combo_limit", "devils_advocate"],
                ) if chain_id else None
                logger.warning(
                    "BLOCKED (fail-closed): LLM Advocate unavailable for %s — no adversarial "
                    "review ran (likely API error / credit exhaustion). Trade not submitted. "
                    "Set ADVOCATE_FAIL_CLOSED=false to fall open instead.",
                    ticker,
                )
                return

            if (
                _advocate_verdict
                and _advocate_verdict.is_block
                and not self._advocate.shadow_mode
            ):
                # Check if defender provides strong-enough counterweight to override
                _defender_strong = (
                    _defender_verdict is not None
                    and not self._defender.shadow_mode  # type: ignore[union-attr]
                    and getattr(_defender_verdict, "thesis_strength", "") == "strong"
                    and getattr(_defender_verdict, "confidence", 0.0) >= 0.65
                )
                if _defender_strong:
                    logger.info(
                        "ADVOCATE BLOCK moderated by strong DEFENDER [%s]: "
                        "advocate=%s | defender=strong (conf=%.0f%%) — trade continues as CAUTION",
                        ticker,
                        _advocate_verdict.verdict_reasoning[:80],
                        _defender_verdict.confidence * 100,  # type: ignore[union-attr]
                    )
                    # Fall through — trade continues with caution flag
                else:
                    _complete_chain(
                        str(self._settings.db_path), chain_id, "risk_blocked",
                        strategy=str(strategy_str),
                        gates_passed=["timing", "macro_cal", "compliance", "risk",
                                      "combo_limit", "devils_advocate"],
                    ) if chain_id else None
                    logger.info(
                        "BLOCKED by LLM Advocate: %s | %s | top_failure=%s",
                        ticker, _advocate_verdict.verdict_reasoning,
                        _advocate_verdict.failure_modes[0]["mode_name"]
                        if _advocate_verdict.failure_modes else "n/a",
                    )
                    # L3 shadow book: record the blocked trade so its counterfactual outcome can
                    # later score whether the advocate was RIGHT to block (a live BLOCK never fills,
                    # so this is the only way BLOCK precision becomes measurable).
                    try:
                        from agora.ops.shadow_book import record_block
                        record_block(str(self._settings.db_path), chain_id or "",
                                     ticker, recommendation, _cur_spot)
                    except Exception:
                        pass
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

        _chain_outcome = (
            "filled" if order_status in ("Filled", "PartiallyFilled")
            else "rejected" if order_status in ("Cancelled", "ApiCancelled", "Inactive")
            else "pending"
        )
        _chain_strategy = str(strategy_str)
        _chain_conviction = getattr(recommendation, "conviction_score", 0.0)
        _gates = ["timing", "macro_cal", "compliance", "risk", "combo_limit",
                  "devils_advocate", "llm_advocate"]

        if order_status in ("Cancelled", "ApiCancelled", "Inactive"):
            _complete_chain(
                str(self._settings.db_path), chain_id, "rejected",
                strategy=_chain_strategy, gates_passed=_gates,
            ) if chain_id else _log_chain(
                str(self._settings.db_path), ticker, triggered_by, "rejected",
                session_id=self._session_id, conviction=_chain_conviction,
                strategy=_chain_strategy, gates_passed=_gates,
            )
            error_code = str(order.get("error_code", "unknown"))
            reason = order.get("reason", "")
            self._exec_quality.record_reject(ticker, error_code, reason, str(strategy_str))
            logger.warning("ORDER REJECTED: %s | code=%s | %s", ticker, error_code, reason)
            # Execution cooldown: any non-201 cancel means the spread didn't fill even
            # after walking mid -> natural. Re-submitting the same idea this cycle just
            # restarts a multi-minute walk that will fail again (this is what produced
            # the ~20-attempts/ticker/day storm). Block re-submission for 2 hours.
            # (Error 201 has its own per-session block below.)
            if error_code != "201":
                _cooldown_until = datetime.now(tz=timezone.utc) + timedelta(seconds=self._EXEC_COOLDOWN_SECS)
                self._exec_cooldowns[ticker] = _cooldown_until
                logger.info(
                    "EXEC COOLDOWN set: %s blocked until %s (unfilled after repricing walk)",
                    ticker, _cooldown_until.strftime("%H:%M ET"),
                )
            # If Error 201 detected, trigger orphan reconciliation and block ticker for session.
            # Paper accounts can't do combo orders for single-name equities — no point retrying.
            if error_code == "201" or "201" in reason:
                self._error_201_blocked.add(ticker)
                logger.info("Error 201 session block: %s will not be re-evaluated this session", ticker)
                asyncio.create_task(self._orphan_reconciler.reconcile_now())
        elif order_status in ("Filled", "PartiallyFilled"):
            # Record the position on a full OR partial fill — a partially-filled BAG (M5)
            # leaves a real, smaller position open at the broker that MUST be tracked.
            if order_status == "PartiallyFilled":
                _orig = max(1, int(recommendation.contracts))
                _fc = int(order.get("filled_contracts", _orig) or _orig)
                if 0 < _fc < _orig:
                    _scale = _fc / _orig
                    # Scale every per-position TOTAL to the filled size; per-share economics
                    # (entry_debit_credit / (contracts*100)) are unchanged by the scaling.
                    recommendation.entry_debit_credit *= _scale
                    for _attr in ("max_loss_dollars", "max_gain_dollars"):
                        _v = getattr(recommendation, _attr, None)
                        if isinstance(_v, (int, float)):
                            setattr(recommendation, _attr, _v * _scale)
                    recommendation.contracts = _fc
                    logger.warning("PARTIAL FILL recorded: %s at %d/%d contracts", ticker, _fc, _orig)
            # Only record the position on confirmed fill — not on Submitted/PreSubmitted
            fills = order.get("fills", [])
            # Use the NET combo fill (same units as the mid) for slippage; fall back
            # to a single leg, then the mid. The old fills[0]["price"] was one LEG's
            # price vs the net mid — that logged nonsense slippage (mid 0.53 vs 5.79).
            fill_price = float(
                order.get("net_fill_price")
                or (fills[0]["price"] if fills else 0.0)
                or mid_price
            )
            self._exec_quality.record_fill(ticker, fill_price, mid_price, str(strategy_str))
            logger.info("ORDER FILLED: %s | fill_price=%.4f | mid=%.4f", ticker, fill_price, mid_price)
            position_id = self._record_position(
                recommendation,
                ibkr_order_id=order.get("order_id", -1),
                regime=self._macro_context.macro_stance if self._macro_context else "",
                earnings_date=earnings_date,
                is_pre_earnings=is_pre_earnings,
                spot=spot,
                fill_price=fill_price,
            )
            _complete_chain(
                str(self._settings.db_path), chain_id, "filled",
                strategy=_chain_strategy, gates_passed=_gates,
                position_id=position_id,
            ) if chain_id else _log_chain(
                str(self._settings.db_path), ticker, triggered_by, "filled",
                session_id=self._session_id, conviction=_chain_conviction,
                strategy=_chain_strategy, gates_passed=_gates,
                position_id=position_id,
            )
            if chain_id and position_id:
                _link_position(str(self._settings.db_path), chain_id, position_id)

            # ── Post-fill profit engine enrichment ──────────────────────────────
            if position_id and mid_price > 0:
                # Fill quality: did we get a better-than-mid price?
                # Credit spread (entry_debit_credit < 0): more credit = better → fill > mid
                # Debit spread (entry_debit_credit ≥ 0): less debit = better → fill < mid
                is_credit = recommendation.entry_debit_credit < 0
                fill_bonus = (
                    (fill_price - mid_price) / mid_price
                    if is_credit
                    else (mid_price - fill_price) / mid_price
                )
                self._position_mgr.set_fill_quality_for_position(position_id, fill_bonus)

                # Price target: if PriceTargetAgent cached analysis, attach aligned scenario
                _pt_cached = (
                    self._price_target._cache.get(ticker) if self._price_target else None
                )
                if _pt_cached:
                    _, pt_analysis = _pt_cached
                    direction = getattr(recommendation, "direction", "neutral")
                    if direction == "bullish":
                        _s = next((s for s in pt_analysis.scenarios if s.label == "bull"), None)
                        aligned_ret = _s.return_pct if _s else 0.0
                    elif direction == "bearish":
                        _s = next((s for s in pt_analysis.scenarios if s.label == "bear"), None)
                        aligned_ret = abs(_s.return_pct) if _s else 0.0
                    else:
                        aligned_ret = 0.0
                    if aligned_ret >= 0.20:
                        self._position_mgr.set_price_target_for_position(
                            position_id, aligned_ret, spot
                        )
        else:
            # Submitted/PreSubmitted — order is pending in TWS but not filled.
            # Do NOT record as a position. The orphan reconciler will detect unfilled
            # brackets and cancel them. Tracking as pending to avoid ghost positions.
            _complete_chain(
                str(self._settings.db_path), chain_id, "pending",
                strategy=_chain_strategy, gates_passed=_gates,
            ) if chain_id else _log_chain(
                str(self._settings.db_path), ticker, triggered_by, "pending",
                session_id=self._session_id, conviction=_chain_conviction,
                strategy=_chain_strategy, gates_passed=_gates,
            )
            logger.warning(
                "ORDER PENDING (not recorded): %s | status=%s | order_id=%s — "
                "armed exec cooldown; orphan reconciler will cancel if still open",
                ticker, order_status, order.get("order_id"),
            )
            # Record as an attempt but not a fill
            self._exec_quality.record_reject(ticker, "pending", order_status, str(strategy_str))

            # Arm the SAME exec cooldown as a cancel. A resting/limbo order must NOT be
            # re-submitted — that created duplicate working orders and was the engine of
            # the ~20-attempts/ticker/day storm (the old 90s auto-retry re-evaluated the
            # ticker with no cooldown, looping all day). The orphan reconciler cancels any
            # order still open in TWS; the cooldown lets the spread settle before we retry.
            _cooldown_until = datetime.now(tz=timezone.utc) + timedelta(seconds=self._EXEC_COOLDOWN_SECS)
            self._exec_cooldowns[ticker] = _cooldown_until
            logger.info(
                "EXEC COOLDOWN set (pending): %s blocked until %s",
                ticker, _cooldown_until.strftime("%H:%M ET"),
            )

    def _record_position(
        self,
        rec: Any,
        ibkr_order_id: int = -1,
        regime: str = "",
        earnings_date: Any = None,
        is_pre_earnings: bool = False,
        spot: float = 0.0,
        fill_price: float = 0.0,
        target_close_date_override: "date | None" = None,
        extra_metadata: dict | None = None,
    ) -> str:
        # extra_metadata (e.g. long-options profit_target_pct/signal_quality/conviction) is
        # accepted so callers don't crash AFTER a fill — which previously left the position
        # live at the broker but unrecorded (an orphan). Exit logic uses the global
        # per-strategy config, so the per-position copy is not persisted here yet.
        _ = extra_metadata
        from datetime import date, timedelta
        from .core.models import OpenPosition, PositionStatus
        expiry = rec.legs[0].expiration if rec.legs else (date.today() + timedelta(days=45))
        # entry_price must use the SIGNED net value per share so the P&L formula
        # (current_mid - entry_price) * 100 is always correct:
        #   Credit spreads: entry_price = negative (net credit received, e.g. -$1.47)
        #   Debit spreads / long options: entry_price = positive (net debit paid, e.g. +$7.84)
        #
        # IBKR paper trading fill prices for BAG combos are unreliable — they often report a
        # single leg's price rather than the net spread value.  For multi-leg positions we
        # ignore fill_price entirely and derive entry_price from entry_debit_credit.
        # For single-leg positions the fill price is trustworthy and we keep it.
        signed_mid  = rec.entry_debit_credit / max(1, rec.contracts * 100)
        is_multi_leg = len(rec.legs) > 1
        if is_multi_leg:
            entry_price = signed_mid          # signed: negative for credit, positive for debit
        else:
            entry_price = fill_price if fill_price > 0 else abs(signed_mid)
        # target_close_date_override lets callers set a time-stop (e.g. long options: +5 days)
        # Default behaviour: close at 21 DTE remaining (spread/naked convention).
        target_close_date = target_close_date_override or (expiry - timedelta(days=21))
        pos = OpenPosition(
            position_id=str(uuid.uuid4()),
            ticker=rec.ticker,
            strategy=rec.strategy,
            pillar=rec.pillar,
            direction=getattr(rec, "direction", "neutral"),
            status=PositionStatus.OPEN,
            legs=rec.legs,
            contracts=rec.contracts,
            entry_price=entry_price,
            entry_date=date.today(),
            expiry_date=expiry,
            target_close_date=target_close_date,
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
        return pos.position_id

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
                # Try configured clientId first, then two fallbacks if it's still in use
                # from a rapid restart (TWS holds connections for ~30s after disconnect).
                base_id = self._settings.startup_tws_sync_client_id
                try:
                    connected = False
                    for _cid in (base_id, base_id + 10, base_id + 20):
                        try:
                            await ib.connectAsync(
                                self._settings.ibkr_host,
                                self._settings.ibkr_port,
                                clientId=_cid,
                                timeout=10,
                            )
                            connected = True
                            break
                        except Exception as _ce:
                            if "already in use" in str(_ce).lower() or "326" in str(_ce):
                                logger.debug("Startup sync clientId %d in use, trying %d", _cid, _cid + 10)
                                continue
                            raise
                    if not connected:
                        logger.warning("Startup TWS sync: all clientIds in use, skipping")
                        return []
                    fills = await ib.reqExecutionsAsync()
                    return [
                        {
                            "symbol":   f.contract.symbol,
                            "secType":  f.contract.secType,
                            "action":   f.execution.side,
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

        # Warn about tickers filled in TWS (BOT) but not in our shadow book.
        # Exclude tickers that already appear in our DB as recently closed — these
        # are legitimate round-trips whose close fill may linger in IBKR's exec log.
        recently_closed = set(
            r[0] for r in self._position_mgr._db.execute(
                "SELECT DISTINCT ticker FROM positions WHERE status='closed' "
                "AND date(close_date) >= date('now','-2 days')"
            ).fetchall()
        ) if hasattr(self._position_mgr, "_db") else set()
        missing = bag_buys - set(open_by_ticker) - {f["symbol"] for f in bag_sells} - recently_closed
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

    async def _execute_partial_close(self, position: Any, qty: int, reason: str) -> None:
        """Close `qty` of position.contracts (scale-out); leave the remainder open.
        Long-options only — P&L is the premium change (no spread direction flip).
        PositionManager.apply_partial_close then resizes the open position + records the
        closed slice for attribution."""
        try:
            slice_pos = position.model_copy(update={"contracts": int(qty)})
            order = await close_trade(slice_pos, self._settings, self._session_id)
            fills = order.get("fills", []) if order else []
            close_price = float(fills[0]["price"]) if fills else position.current_price
            realized = round((close_price - position.entry_price) * 100 * int(qty), 2)
            self._position_mgr.apply_partial_close(position.position_id, int(qty), close_price, realized)
        except Exception as exc:
            logger.error("Partial close failed for %s: %s", position.ticker, exc, exc_info=True)

    async def _execute_close(self, position: Any, reason: str) -> bool:
        """Flatten a position. Returns True ONLY if it actually closed (filled).

        C3: retries before giving up; on persistent failure it ESCALATES to the CRO and
        returns False so the caller leaves the position OPEN (managed/retried next cycle)
        rather than silently marking a still-open position closed (a stranded, unbounded loss).
        """
        logger.info("Closing %s | reason=%s", position.ticker, reason)
        last_err = "?"
        for attempt in range(1, 4):  # up to 3 attempts on the dedicated EXIT executor
            try:
                order = await close_trade(position, self._settings, self._session_id)
                status = (order or {}).get("status", "")
                # A position is only CLOSED if it actually flattened. "PartiallyClosed" means a leg
                # is stranded at the broker — do NOT mark closed (orphan) and do NOT blindly retry
                # (would over-close the already-flat leg); escalate and leave OPEN for recon. Any
                # other non-Filled status (Failed/Cancelled — nothing flattened) is safe to retry.
                if status == "PartiallyClosed":
                    last_err = (f"PARTIAL close — {order.get('n_filled','?')}/{order.get('n_legs','?')} "
                                f"legs flattened, remainder stranded")
                    logger.error("PARTIAL close for %s — %s; escalating, leaving OPEN",
                                 position.ticker, last_err)
                    break  # skip remaining retries → fall through to escalation below
                if status == "Filled":
                    fills = order.get("fills", [])
                    close_price = float(
                        order.get("avg_price")
                        or (fills[0]["price"] if fills else position.current_price)
                    )
                    realized_pnl = round(
                        (close_price - position.entry_price) * 100 * position.contracts
                        * (-1 if position.direction == "bearish" else 1),
                        2,
                    )
                    self._position_mgr.mark_position_closed(
                        position_id=position.position_id,
                        realized_pnl=realized_pnl,
                        close_price=close_price,
                        source=f"session:{reason[:40]}",
                    )
                    self._compliance.record_close(
                        ticker=position.ticker,
                        realized_pnl=realized_pnl,
                        strategy=str(position.strategy),
                    )
                    logger.info(
                        "CLOSE CONFIRMED: %s | fill=$%.4f | realized=$%.2f | reason=%s (attempt %d)",
                        position.ticker, close_price, realized_pnl, reason, attempt,
                    )
                    if getattr(position, "is_pre_earnings", False):
                        self._pre_earnings_tickers.pop(position.ticker, None)
                    return True
                last_err = f"status={status} {(order or {}).get('reason', '')}"
                logger.warning("Close attempt %d/3 for %s did not fill: %s",
                               attempt, position.ticker, last_err)
            except Exception as exc:
                last_err = str(exc)
                logger.error("Close attempt %d/3 failed for %s: %s", attempt, position.ticker, exc)
            await asyncio.sleep(2)

        # All attempts failed — escalate; leave the position OPEN (do NOT mark closed).
        msg = (f"🚨 CLOSE FAILED for {position.ticker} after 3 attempts ({reason}) — position is "
               f"STILL OPEN in IBKR and was NOT marked closed. Last error: {last_err}. "
               f"Manual intervention may be required.")
        logger.critical(msg)
        try:
            if getattr(self, "_cro", None):
                await self._cro.receive_alert("PositionManager", "critical", msg)
            elif getattr(self, "_ceo", None):
                await self._ceo.dispatch_alert("critical", msg)
        except Exception as _alert_exc:
            logger.error("Close-failure alert dispatch failed for %s: %s", position.ticker, _alert_exc)
        return False

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
                _roll_chain_id = _start_chain(
                    str(self._settings.db_path), position.ticker,
                    triggered_by="roll", session_id=self._session_id,
                    conviction=conviction.total_score,
                )
                await self._submit_recommendation(
                    recommendation, position.ticker, spot,
                    triggered_by="roll", chain_id=_roll_chain_id,
                )
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

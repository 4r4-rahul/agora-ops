"""
Chief Technology Officer (CTech) — AGORA Software Engineering & Infrastructure

Domain expertise:
  Signal pipeline architecture, AI/ML scoring systems, Claude API integration,
  data infrastructure, Python async patterns, SQLite/WAL mode, performance profiling,
  error detection, system observability, CI/CD, code quality.

Sub-agents supervised:
  ConvictionScorer, DisagreementResolver, EventPatternEngine,
  IvPremiumScreen, VolRegimeAgent, MacroSynthesizer,
  SectorIntelligenceAgent, UniverseDiscoveryAgent
  Note: GEX is computed inline in session._evaluate_ticker() via GexSignal/GexRegime models.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from ..core.config import AgoraSettings
from .base import ExecutiveAgent

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

_CTECH_SYSTEM_PROMPT = """\
You are the Chief Technology Officer (CTech) of AGORA, an autonomous options trading system owned by Rahul.
You report to the CEO. You are responsible for the entire software stack: signal pipeline, AI scoring,
data infrastructure, async architecture, and production stability. Every line of code you oversee directly
affects trade quality and system uptime. Technical debt in a trading system is financial liability.
You operate at zero tolerance for hallucination — all analysis must be grounded in actual code, logs, or data.

═══ AGORA SIGNAL PIPELINE ARCHITECTURE ═══

Signal Generation Layer (all synchronous, called in _evaluate_ticker()):
  IvPremiumScreen     → IvPremiumSignal      (IV rank, HV comparison, VRP check)
  GexSignal (inline)  → GexSignal            (put/call wall, gamma flip price — computed in session._evaluate_ticker() via agora.core.models.GexSignal/GexRegime)
  VolRegimeAgent      → VolRegimeSignal      (VIX level, regime classification)
  MacroSynthesizer    → MacroContext         (macro stance, vol_selling_ok flag)
  EventPatternEngine  → EventSignal          (earnings, FOMC, post-earnings drift)
  CatalystDiscovery   → Catalyst             (8-K EDGAR, press releases)
  SmartMoneyAgent     → Catalyst             (13D/13G/Form4 clusters)
  MarketInterestAgent → MarketInterestScore  (6-fingerprint flow analysis)

Scoring Layer:
  ConvictionScorer.score() — 8 components, 100 pts total:
    vol_premium:    0-30  (IVR ≥ 60 = 30, IVR 40-60 = 20, IVR 20-40 = 10)
    gex:            0-20  (negative GEX = 12-20 pts; positive = 0-8 pts)
    regime:         0-20  (risk_off/high_vol = 0; risk_on/low_vol = 15-20)
    event:          0-15  (post_earnings_skew = 15; FOMC_vol_expansion = 12)
    macro:          0-5   (vol_selling_ok=True=5; False=0)
    smart_money:    0-5   (strong catalyst=5; moderate=3)
    info_speed:     0-5   (strong catalyst from IBKR news=5; moderate=3)
    market_interest: 0-10 (score from 6-fingerprint 0-10 scale)

  Gates: total ≥ 70 = "high" | ≥ 55 = "standard" | ≥ 40 = "low" | < 40 = "no_trade"
  Dominant pillar drives strategy selection and sizing.

Resolution Layer:
  DisagreementResolver.resolve(macro, microstructure, catalyst, regime, total_conviction)
  → size_multiplier ∈ {0.0, 0.5, 1.0, 1.5}
  → gate: "high" | "standard" | "low" | "no_trade"
  Regime-weighted: risk_on=macro-heavy; risk_off=microstructure-heavy
  Dynamic min_agreers = ceil(n × 0.6) where n = non-None signal count
  Crisis regime always returns size_multiplier=0 regardless of conviction.

═══ CLAUDE API INTEGRATION ═══

Model assignments (all in agora/core/config.py):
  claude_fast_model:  claude-haiku-4-5-20251001   — rapid classification (CatalystDiscovery, SmartMoney Form4 triage)
  claude_model:       claude-opus-4-8             — full analysis (13D intent, MacroSynthesizer synthesis, EarningsTranscript deep inference)

API features in use:
  Streaming:        CatalystDiscoveryAgent — <3s first-token for urgent 8-K classification
  Prompt caching:   CatalystDiscovery, SmartMoney, EarningsTranscript — stable system prompts cached
  Adaptive thinking: EarningsTranscriptAgent — for multi-hop derivative ticker inference
  Tool use loop:    SmartMoneyAgent — Claude reads Item 4 section via read_filing_section tool
  Token counting:   SystemHealthAgent ping — cheapest Anthropic reachability check

All Claude clients: anthropic.AsyncAnthropic — async throughout, never blocking the event loop.
client_id=None means each agent creates its own client (no sharing — avoids rate limit entanglement).

═══ ASYNC ARCHITECTURE ═══

Event Loop: single uvicorn event loop. All agents run as asyncio Tasks via asyncio.create_task().
Exception: IBKRNewsAgent runs on a dedicated daemon thread (ib_insync requires its own loop).
  Cross-loop callback: asyncio.run_coroutine_threadsafe(coro, main_loop)

Session lifecycle (agora/session.py):
  AgoraSession.start():
    1. Initialize all agents
    2. asyncio.gather(all background tasks)
    3. Main loop: 60-second evaluation cycle per universe ticker

SQLite: single DB file (agora.db). WAL mode for concurrent reader/writer access.
  Writers: RiskCouncil.record_daily_pnl(), ComplianceAgent.record_close(), PositionManager
  Readers: SystemHealthAgent._check_db(), all C-suite collect_intelligence() calls
  Connection per agent (check_same_thread=False) — not connection pooling (SQLite limitation).

═══ DATA INFRASTRUCTURE ═══

Market Data:
  yfinance: primary source for options chains (OI, bid/ask, IV), spot prices, earnings dates
  IBKR tick 292 (mdoff,292): real-time news headlines via IBKRNewsAgent (ib_insync)
  EDGAR EFTS RSS: 8-K filings and 13D/13G/Form4 via HTTP polling

Polling intervals:
  Main evaluation loop:      60 seconds
  EDGAR catalyst:            60 seconds (edgar_poll_seconds setting)
  Macro synthesizer:         120-minute cooldown between full syntheses
  Sector intelligence:       Once per day at 6 AM ET
  Earnings calendar sweep:   Once per day at 6:30 AM ET
  Orphan reconciler:         30 minutes
  System health check:       5 minutes
  Pillar health check:       10 minutes

Caching strategy:
  SectorIntelligenceAgent: in-memory dict _cache (ticker → SectorIntelligence)
  MacroSynthesizer:         _last_context property (MacroContext, updated on each synthesis)
  MarketInterestAgent:      in-memory (refreshed every evaluation cycle)

═══ PRODUCTION STABILITY STANDARDS ═══

Error handling hierarchy:
  1. Market hours check before any evaluation (9:30–16:00 ET)
  2. try/except on every external call (yfinance, EDGAR, IBKR)
  3. Degraded-mode fallback: signal=None → scorer receives None input, scores 0 for that component
  4. Kill switch: circuit breaker trips on VIX > 35 or any daily loss > limit

Known technical debt:
  - yfinance .info["regularMarketPrice"] sometimes returns None during pre-market (returns 0)
  - IBKR Error 201 "max combination orders": OrphanOrderReconciler clears these every 30 min
  - ib_insync cannot share asyncio loop with uvicorn → daemon thread + run_coroutine_threadsafe
  - SQLite WAL reader contention under high write load (not observed in practice at current scale)

Performance targets:
  Signal evaluation per ticker: < 500ms (all I/O async, no blocking calls)
  Claude Haiku classification: < 3s (streaming first token)
  Claude Opus deep analysis:   < 30s (adaptive thinking)
  Kill switch check (SQLite):  < 1ms (single indexed row read)

═══ CODE QUALITY STANDARDS ═══

You enforce zero tolerance for:
  - Blocking I/O in the async event loop (use asyncio.to_thread or executor)
  - Unhandled exceptions in background tasks (always try/except in async loops)
  - Direct private attribute access across module boundaries (use public API)
  - Hardcoded model strings outside config.py
  - Any hallucinated API (if you are not certain a method exists, grep first)

Refactoring principles:
  - Add public APIs before consuming private attributes from other modules
  - Session state is ground truth; DB is persistence; they must stay in sync
  - All alert routing: SubAgent → CSuite manager → CEO (never SubAgent → CEO directly)

You cannot approve trade execution decisions — that is the CTO (Chief Trading Officer)'s domain.
Your domain is the technology layer: correctness, reliability, observability, performance.
"""


class CTechAgent(ExecutiveAgent):
    """
    Chief Technology Officer — oversees signal pipeline, AI scoring,
    data infrastructure, and all software engineering concerns.
    """

    TITLE = "Chief Technology Officer (CTech)"
    BRIEF_CADENCE = 4  # every 4 hours during trading day

    def __init__(
        self,
        settings: AgoraSettings | None = None,
        ceo_agent: Any = None,
        conviction_scorer: Any = None,
        disagreement_resolver: Any = None,
        event_engine: Any = None,
        iv_screen: Any = None,
        vol_regime: Any = None,
        macro_synthesizer: Any = None,
        sector_intel: Any = None,
        universe_disc: Any = None,
        strategy_health: Any = None,
    ) -> None:
        super().__init__(settings, ceo_agent)
        self._scorer          = conviction_scorer
        self._resolver        = disagreement_resolver
        self._event           = event_engine
        self._iv              = iv_screen
        self._vol             = vol_regime
        self._macro           = macro_synthesizer
        self._sector          = sector_intel
        self._universe        = universe_disc
        self._strategy_health = strategy_health
        self._started_at      = datetime.now(tz=UTC)

    @property
    def _system_prompt(self) -> str:
        return _CTECH_SYSTEM_PROMPT

    def self_audit(self) -> list[tuple[str, str, str]]:
        """
        CTech proactive patrol: signal pipeline coverage, sub-agent heartbeats,
        DB health, macro staleness, conviction scorer activity, error rate.
        """
        findings: list[tuple[str, str, str]] = []
        import sqlite3 as _sql

        now_et = datetime.now(tz=ET)
        is_market_hours = 9 <= now_et.hour < 16

        # ── Signal pipeline coverage ──
        pipeline = {
            "iv_screen":          self._iv,
            "vol_regime":         self._vol,
            "macro_synthesizer":  self._macro,
            "event_engine":       self._event,
            "sector_intel":       self._sector,
            "universe_disc":      self._universe,
            "conviction_scorer":  self._scorer,
            "disagreement_resolver": self._resolver,
        }
        missing = [name for name, agent in pipeline.items() if agent is None]
        if len(missing) >= 3:
            findings.append((
                "pipeline_coverage_critical",
                "critical",
                f"{len(missing)} pipeline components missing: {', '.join(missing)}. "
                "Signal generation severely degraded — conviction scores are incomplete.",
            ))
        elif missing:
            findings.append((
                "pipeline_coverage_partial",
                "warning",
                f"Pipeline components not wired: {', '.join(missing)}. "
                "Affected signal components score 0 — trades may be under-informed.",
            ))

        # ── Conviction scorer: no scans after startup period ──
        uptime_minutes = (datetime.now(tz=UTC) - self._started_at).total_seconds() / 60
        if self._scorer and is_market_hours:
            try:
                stats = self._scorer.get_session_stats()
                scored = stats.get("scored", 0)
                # Grace period: don't alarm within first 15 min (premarket synthesis can take ~90s,
                # and the first evaluation cycle starts after that completes)
                if now_et.hour >= 10 and scored == 0 and uptime_minutes >= 15:
                    findings.append((
                        "conviction_scorer_no_scans",
                        "critical",
                        "ConvictionScorer has completed 0 ticker scans past 10:30 AM. "
                        "Main evaluation loop may be stalled — no trades can be initiated.",
                    ))
            except Exception:
                findings.append((
                    "conviction_scorer_unresponsive",
                    "critical",
                    "ConvictionScorer.get_session_stats() raised an exception. "
                    "Agent may have crashed — scoring pipeline is broken.",
                ))

        # ── Disagreement resolver: unresponsive check ──
        if self._resolver:
            try:
                stats = self._resolver.get_session_stats()
                errors = stats.get("error_count", 0)
                if errors and errors > 5:
                    findings.append((
                        "resolver_error_rate_high",
                        "warning",
                        f"DisagreementResolver logged {errors} errors this session. "
                        "Resolver failures default to 0.0 multiplier — trades are being blocked unnecessarily.",
                    ))
            except Exception:
                findings.append((
                    "resolver_unresponsive",
                    "warning",
                    "DisagreementResolver.get_session_stats() raised an exception. Agent may be degraded.",
                ))

        # ── Macro synthesizer: staleness check during market hours ──
        if self._macro and is_market_hours:
            try:
                ctx = getattr(self._macro, "last_context", None)
                if ctx is None:
                    findings.append((
                        "macro_context_missing",
                        "critical",
                        "MacroSynthesizer has no context yet during market hours. "
                        "Pre-market scan (7 AM) may have failed — macro regime unknown.",
                    ))
                else:
                    ctx_ts = getattr(ctx, "synthesized_at", None)
                    if ctx_ts:
                        try:
                            age_hours = (now_et - ctx_ts).total_seconds() / 3600
                            if age_hours > 8:
                                findings.append((
                                    "macro_context_stale",
                                    "critical",
                                    f"MacroContext is {age_hours:.1f}h old — severely stale. "
                                    "Macro regime may have changed (SPY/VIX move > threshold needed to re-trigger).",
                                ))
                            elif age_hours > 4:
                                findings.append((
                                    "macro_context_aging",
                                    "warning",
                                    f"MacroContext is {age_hours:.1f}h old. "
                                    "A market regime shift may not be captured — watch SPY/VIX for refresh trigger.",
                                ))
                        except Exception:
                            pass
            except Exception:
                pass

        # ── Database health ──
        try:
            conn = _sql.connect(str(self._settings.db_path), check_same_thread=False)
            # Check integrity (fast — returns 'ok' if clean)
            integrity = conn.execute("PRAGMA integrity_check").fetchone()
            conn.close()
            if integrity and integrity[0] != "ok":
                findings.append((
                    "db_integrity_failure",
                    "critical",
                    f"SQLite PRAGMA integrity_check returned: {integrity[0]}. "
                    "Database may be corrupted — position records at risk. Restore from backup.",
                ))
        except Exception as exc:
            findings.append((
                "db_unreachable",
                "critical",
                f"Cannot connect to AGORA database: {exc}. "
                "All position management, risk checks, and fill recording are broken.",
            ))

        # ── Event engine: poll failure detection ──
        if self._event and is_market_hours:
            try:
                # Use a sample ticker to verify the event engine responds
                self._event.get_signals(self._settings.etf_universe[0])
                # If no exception, engine is alive — even empty list is OK
            except Exception as exc:
                findings.append((
                    "event_engine_error",
                    "warning",
                    f"EventPatternEngine.get_signals() raised: {exc}. "
                    "Event-driven signals (FOMC, earnings) are not being generated.",
                ))

        # ── StrategyHealthAgent: ops status ──
        if not self._strategy_health:
            findings.append((
                "strategy_health_not_wired",
                "warning",
                "StrategyHealthAgent is not wired to CTech. "
                "Pillar/regime auto-pause status is not visible in the ops brief.",
            ))
        else:
            try:
                status = self._strategy_health.get_status()
                paused = status.get("paused_count", 0)
                if paused > 0:
                    cells = status.get("paused_cells", [])
                    cell_summary = ", ".join(
                        f"{c['pillar']}/{c['regime']}"
                        for c in cells
                    )
                    findings.append((
                        "strategy_health_cells_paused",
                        "critical" if paused >= 3 else "warning",
                        f"StrategyHealth: {paused} pillar/regime cell(s) auto-paused: {cell_summary}. "
                        "New entries in these cells are blocked at RiskCouncil gate #0. "
                        "Sharpe must recover above 0.0 over ≥20 trades to auto-unpause.",
                    ))
            except Exception as exc:
                findings.append((
                    "strategy_health_unresponsive",
                    "warning",
                    f"StrategyHealthAgent.get_status() raised: {exc}. "
                    "Patrol may have crashed — pillar pause state unknown.",
                ))

        return findings

    async def self_heal(self, findings: list[tuple[str, str, str]]) -> None:
        """
        CTech corrective actions — pre-delegated authority:
          macro_context_missing/stale → trigger macro re-synthesis (via CIO event)
          conviction_scorer_no_scans  → log alert; if stalled coroutine detected, restart
          db_unreachable              → critical escalation only (can't self-heal a broken DB)
          pipeline_coverage_partial   → log missing components for operator to wire
        """
        keys = {k for k, _, _ in findings}

        if "macro_context_missing" in keys or "macro_context_stale" in keys:
            self._heal_attempts["macro_stale"] = self._heal_attempts.get("macro_stale", 0) + 1
            if self._heal_attempts["macro_stale"] <= 3 and self._macro:
                try:
                    logger.info("CTech self_heal: triggering macro re-synthesis")
                    await self._macro.synthesize(reason="ctech_self_heal")
                except Exception as exc:
                    logger.error("CTech self_heal: macro re-synthesis failed: %s", exc)

        if "conviction_scorer_no_scans" in keys:
            self._heal_attempts["scorer_stalled"] = self._heal_attempts.get("scorer_stalled", 0) + 1
            count = self._heal_attempts["scorer_stalled"]
            if count == 1:
                logger.error("CTech self_heal: ConvictionScorer stalled (0 scans) — signalling for restart")
                # Publish to CEO for operator awareness (can't auto-restart coroutine safely)
                await self._escalate_to_ceo(
                    "critical",
                    "CTech: ConvictionScorer stalled — 0 tickers scored past 10:30 AM. "
                    "Main evaluation loop may be deadlocked. Manual restart may be required."
                )
            elif count >= 3:
                # After 3 consecutive stalls, attempt to reset session stats and re-trigger
                try:
                    if hasattr(self._scorer, "reset_session"):
                        self._scorer.reset_session()
                        logger.info("CTech self_heal: ConvictionScorer session stats reset")
                except Exception:
                    pass

        if "db_unreachable" in keys:
            # Cannot self-heal a broken DB — only escalate
            self._heal_attempts["db_down"] = self._heal_attempts.get("db_down", 0) + 1
            if self._heal_attempts["db_down"] == 1:
                await self._escalate_to_ceo(
                    "critical",
                    "CTech: AGORA database unreachable. "
                    "All position management and risk checks are non-functional. "
                    "Manual intervention required — check disk space and file permissions."
                )

        if "pipeline_coverage_critical" in keys or "pipeline_coverage_partial" in keys:
            missing = [k.replace("pipeline_coverage_", "") for k in keys
                       if k.startswith("pipeline_coverage_")]
            logger.warning("CTech self_heal: pipeline gaps — %s. Cannot auto-wire; operator must check session.py", missing)

    async def on_peer_event(self, event_type: str, publisher: str, payload: dict) -> None:
        """CTech monitors system events for technology health signals."""
        if event_type == "fill_rate_critical":
            logger.warning("CTech: fill_rate_critical from %s — checking IBKR callback pipeline", publisher)
            self._latest_intel["fill_rate_alert"] = payload
        elif event_type == "ibkr_disconnected":
            logger.error("CTech: IBKR disconnected (from %s) — ib_insync reconnect expected", publisher)
            self._latest_intel["ibkr_down"] = {"ts": datetime.now(tz=ET).isoformat(), **payload}

    def get_readiness_tasks(self) -> list[str]:
        tasks = []
        missing = [n for n, a in [
            ("iv_screen", self._iv), ("vol_regime", self._vol),
            ("macro_synthesizer", self._macro), ("event_engine", self._event),
            ("sector_intel", self._sector), ("universe_disc", self._universe),
            ("conviction_scorer", self._scorer), ("disagreement_resolver", self._resolver),
            ("strategy_health", self._strategy_health),
        ] if a is None]
        for m in missing:
            tasks.append(f"Wire {m} to CTech — signal health monitoring blind spot")
        return tasks

    def collect_intelligence(self) -> dict[str, Any]:
        now = datetime.now(tz=ET).isoformat()
        intel: dict[str, Any] = {"timestamp": now, "department": "Technology"}

        # Signal pipeline health
        pipeline_status = {}
        for name, agent in [
            ("iv_screen", self._iv),
            ("gex_signal_inline", "inline"),  # GEX computed in session._evaluate_ticker()
            ("vol_regime", self._vol),
            ("macro_synthesizer", self._macro),
            ("event_engine", self._event),
            ("sector_intel", self._sector),
            ("universe_disc", self._universe),
        ]:
            pipeline_status[name] = "wired" if agent is not None else "missing"
        intel["signal_pipeline_status"] = pipeline_status
        wired_count = sum(1 for k, v in pipeline_status.items()
                         if v == "wired" or v == "inline")
        intel["pipeline_coverage_pct"] = round(wired_count / len(pipeline_status) * 100, 0)

        # Conviction scorer session stats
        if self._scorer:
            try:
                intel["conviction_scorer"] = self._scorer.get_session_stats()
            except Exception:
                pass

        # Disagreement resolver session stats
        if self._resolver:
            try:
                intel["disagreement_resolver"] = self._resolver.get_session_stats()
            except Exception:
                pass

        # Universe discovery
        if self._universe:
            try:
                dynamic = self._universe.get_dynamic_tickers()
                intel["universe"] = {
                    "static_size": len(self._settings.etf_universe),
                    "dynamic_discovered": len(dynamic),
                    "sample": dynamic[:5],
                }
            except Exception:
                pass

        # Sector intelligence cache
        if self._sector:
            try:
                intel["sector_cache"] = {
                    "cached_count": self._sector.get_cached_count(),
                    "top_sectors": self._sector.get_top_sectors(n=3),
                }
            except Exception:
                pass

        # Ops agents status
        ops: dict[str, Any] = {}
        if self._strategy_health:
            try:
                sh = self._strategy_health.get_status()
                ops["strategy_health"] = {
                    "paused_count":    sh.get("paused_count", 0),
                    "paused_cells":    [
                        f"{c['pillar']}/{c['regime']}" for c in sh.get("paused_cells", [])
                    ],
                    "cells_monitored": len(sh.get("health", [])),
                }
            except Exception:
                ops["strategy_health"] = "error"
        else:
            ops["strategy_health"] = "not_wired"
        intel["ops_agents"] = ops

        # Config snapshot
        intel["model_config"] = {
            "fast_model":  self._settings.claude_fast_model,
            "main_model":  self._settings.claude_model,
        }
        intel["poll_config"] = {
            "edgar_poll_seconds":  self._settings.edgar_poll_seconds,
            "main_loop_seconds":   60,
            "macro_cooldown_min":  120,
            "orphan_reconcile_min": 30,
            "health_check_min":    5,
        }

        return intel

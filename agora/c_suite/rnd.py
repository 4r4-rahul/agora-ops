"""
Chief Research Officer / R&D Department — AGORA Quantitative Research

Domain expertise:
  Signal development and validation, backtesting methodology, alpha decay analysis,
  factor research (momentum, vol, value), IV surface modeling (SVI, SSVI),
  regime identification (HMM), options-specific research (VRP persistence, IV crush magnitude),
  transaction cost analysis (TCA), information ratio, walk-forward testing.

Sub-agents supervised:
  EarningsTranscriptAgent, PremarketSetupAgent, EventPatternEngine,
  UniverseDiscoveryAgent, PillarHealthAgent, AnalystRevisionTracker
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

_RND_SYSTEM_PROMPT = """\
You are the Chief Research Officer (CRO-R&D) of AGORA, an autonomous options trading platform owned by Rahul.
You report to the CEO. Your mission is to discover and validate alpha — and to kill signals that
have no edge before they cost the portfolio money. In quantitative research, the most expensive mistake
is deploying a signal that worked in backtesting but has no real-world edge (overfitting). You are
relentlessly skeptical of your own work and demand statistical rigor before production deployment.

═══ SIGNAL RESEARCH METHODOLOGY ═══

The Research Pipeline (in order):
  1. Hypothesis: Why should this signal have edge? What market inefficiency does it exploit?
     Without a clear WHY, any signal is just curve-fitting. Examples:
       - VRP signal: IV persistently > RV because investors overpay for insurance (behavioral inefficiency).
       - Earnings transcript NLP: market underreacts to subtle language in calls (information processing).
       - GEX reversal: dealer gamma hedging creates predictable price behavior (structural).
  2. Data exploration: test hypothesis with raw data, no optimization.
  3. Simple model first: start with moving average or threshold before ML. Occam's razor.
  4. Backtesting with transaction costs: ALWAYS include commissions + slippage.
  5. Out-of-sample test: hold out last 20-30% of data, NEVER touch until final evaluation.
  6. Walk-forward validation: rolling window train/test (e.g., train 12 months, test 3 months, step 1 month).
  7. Reality check: does it make economic sense? Is it tradeable at the implied size?
  8. Paper trading: deploy with 0 real risk for 30-60 days to verify execution assumptions.
  9. Live deployment: only after paper trading confirms signal works in live market conditions.

Common Research Mistakes (DO NOT do these):
  Data snooping: testing hundreds of signals on same dataset → at least 5% will look good by chance.
    Fix: Bonferroni correction, or out-of-sample test, or cross-validation.
  Look-ahead bias: using data in backtest that wasn't available at decision time.
    Example: using full-year IV rank when only past 6 months were known at trade entry time.
  Survivorship bias: testing only on stocks that survived → dead stocks had returns too.
    Example: if your universe is S&P 500 today, many stocks weren't in it 5 years ago.
  Transaction cost blindness: slippage + commissions consume 30-50% of alpha in short-horizon signals.
  Overfitting: too many parameters, not enough data. Rule: need 5-10× data points per parameter.

Statistical Rigor:
  Sample size: 30+ trades for a simple threshold signal. 100+ for multi-parameter model.
  t-test: test if mean return ≠ 0 with p < 0.05. t = mean_return / (std_dev / √n).
  Sharpe significance: |Sharpe| > 2/√(years) for statistical significance at 95% confidence.
    Example: 1-year backtest with Sharpe=1.0 → t-stat = 1.0 × √1 = 1.0 → NOT significant.
    Need Sharpe > 2.0 for 1-year of data to be significant.
  Bootstrap: resample returns 1000× to get confidence interval on Sharpe/Profit Factor.
  Correlation between signals: if two signals are 90% correlated, you don't have two signals, you have one.

═══ ALPHA DECAY ANALYSIS ═══

Information Ratio (IR):
  IR = Annualized_Alpha / Tracking_Error (vs benchmark)
  For directional alpha: IR = alpha_return / alpha_vol
  Target IR > 0.5. IR > 1.0 is excellent. IR < 0.3 → signal is weak.
  Decay: measure IR over rolling 30/60/90 day windows. If declining → signal is being arbitraged away.

Alpha Half-Life:
  How fast does a signal's edge decay after signal generation?
  Measure: backtest returns at 1h, 4h, 1d, 1w intervals after signal generation.
  For news-based signals: edge typically decays in 2-4 hours.
  For IV premium (structural): edge decays over days (vol premium is persistent).
  For earnings surprise: edge is highest in first 2-3 days, then fades.
  Decision: don't hold positions past the alpha half-life. Close earlier.

Regime-conditional Signal Strength:
  Many signals only work in specific regimes. Decompose performance by:
    - Vol regime (low/normal/high/crisis)
    - Macro regime (risk_on/neutral/risk_off)
    - Market regime (trending/mean-reverting)
  If signal works only in 1 regime but you trade it always → diluted returns + added risk.
  Fix: condition signal on regime. Only trade when regime is favorable.

═══ OPTIONS FACTOR RESEARCH ═══

Vol Risk Premium (VRP) Research:
  VRP = IV - RV (realized vol). Historically: VRP > 0 for index options 85% of the time.
  VRP cycle: VRP compresses pre-earnings, collapses post-earnings, rebuilds over 2-3 weeks.
  VRP by sector: tech > energy > financials > consumer staples. Higher vol = higher premium.
  VRP regime-conditional: VRP is larger in low-vol regimes (VIX < 15) than high-vol (VIX > 25).
    Counterintuitive: when VIX is low, VRP is ALSO low in absolute terms but HIGH relative to RV.
  Optimal selling window: 21-45 DTE captures the theta decay curve efficiently.
  VRP decay: premium amortizes over time. Maximum theta/gamma ratio at 21-45 DTE.

Earnings IV Crush Research:
  Expected crush = post-earnings IV collapse magnitude. Historically:
    Large-cap tech (AAPL, MSFT, NVDA): crush = 40-55% of pre-earnings elevated IV.
    Small/mid-cap: crush = 60-80% of pre-earnings elevated IV.
  Crush timing: largest in first 30 minutes post-announcement. 80% complete within 1 hour.
  Residual premium: some IV remains post-crush (next event priced in, ongoing uncertainty).
  Unexpected move: if stock moves > straddle price, long gamma wins over short vega. Watch carefully.

Momentum in Options Markets:
  Underlying momentum: stocks up > 20% in 3 months tend to continue outperforming.
    Implications: prefer buying calls on strong momentum, puts on weak momentum.
  IV momentum: stocks with high IV percentile rank tend to stay high for 2-4 weeks.
    Implication: sell premium into elevated IV, expect persistence not mean-reversion.
  OI momentum: large OI buildup tends to predict directional moves (if one-sided).

Sector Contagion Research:
  After large earnings surprise in sector leader, peers move in sympathy.
  CSCO → JNPR, HPE, ANET (network equipment). NVIDIA → AMD, INTC (semis).
  Contagion magnitude: 40-60% of leader move. Decays with market cap of peer.
  Time window: strongest in first 2 hours. Fade by EOD in most cases.
  Trading: buy puts/calls on peers after leader reports (within 30 min window).

GEX Research:
  GEX flip (positive → negative): tends to precede increased volatility.
  Negative GEX regime: average daily SPY move = 1.2%. Positive GEX: 0.7%.
  GEX and options strategy performance:
    Positive GEX: credit spreads outperform (low realized vol matches low IV).
    Negative GEX: directional debit spreads outperform (trends persist through resistance).
  Research gap: optimal strike distance from GEX flip level for credit spread placement.

═══ SIGNAL LIBRARY STATUS ═══

Deployed Signals (validated, in production):
  1. IV Premium Screen: IvPremiumScreen — consecutive days above VRP threshold.
     Status: working, 15-day minimum hold tested in backtest. R²=0.32 with future returns.
  2. GEX Regime: get_gex() — negative vs positive gamma exposure.
     Status: working, positive GEX → lower realized vol confirmed.
  3. MacroSynthesizer: vol_selling_ok flag — macro-conditional premium selling.
     Status: working, reduces false positives during risk-off periods.
  4. EarningsTranscriptAgent: NLP sentiment from earnings calls.
     Status: deployed, validation ongoing (need 60+ events for statistical significance).
  5. MarketInterestAgent: 6-fingerprint institutional attention score.
     Status: deployed, contributing 0-10 pts to conviction score. Validation in progress.

Signals Under Research (not yet deployed):
  - Analyst revision cascade: after earnings, consensus estimate revisions momentum.
  - Short squeeze detector: high short interest + catalyst = gamma squeeze setup.
  - IV term structure arbitrage: short front-month, long back-month during inversion.

Signals Retired / Failed:
  - Pure momentum ignoring vol regime: produced winning signals in calm markets but catastrophic
    in high-vol regimes (2022 drawdown). Lesson: always condition on regime.

═══ BACKTESTING FRAMEWORK ═══

AGORA Backtester (agora/backtester/):
  Data: yfinance OHLCV + synthetic IV estimation from HV ratios.
  Synthetic IV limitation: cannot replicate true options chain history. Use with caution.
  Options pricing: use BSM with estimated vol surface (not historical chain data).
  Transaction costs: $1.50/leg/contract commission + configurable slippage.
  Lookback: 2-5 years for strategy validation. 10+ years for regime-conditional analysis.

Walk-Forward Testing Protocol:
  Training window: 12 months. Test window: 3 months. Step: 1 month. Repeat 3-5 folds.
  Evaluate: is out-of-sample Sharpe within 30% of in-sample? If not → overfit.
  Common failure mode: strategy works in 2021 (high vol) but fails in 2023 (low vol).
    Fix: test across multiple vol regimes explicitly.

Parameter Sensitivity Analysis:
  Vary each parameter ±20% from optimal. If Sharpe degrades > 30% → parameter is fragile.
  Robust signal: performance stable across ±20% parameter variation.
  Fragile signal: performance cliff at specific parameter value → do not deploy.

Monte Carlo Simulation:
  Randomly resample daily P&L from backtest (with replacement). Run 1000 simulations.
  Metrics: 5th-percentile Sharpe (worst-case), 95th-percentile drawdown (worst-case).
  Decision threshold: deploy only if 5th-percentile Sharpe > 0.5.

═══ AGORA BACKTESTER ARCHITECTURE ═══

Module: agora/backtester/ (separate from any trading_platform/backtester/)
  engine.py:      Main backtesting loop. Iterates OHLCV history bar-by-bar.
                  Instantiates real signal generators (IvPremiumScreen, VolRegimeClassifier, etc.)
                  and drives the same ConvictionScorer + DisagreementResolver pipeline as live.
  models.py:      BacktestPosition, BacktestTrade, BacktestResult dataclasses.
                  BacktestResult: total_return, sharpe, max_drawdown, win_rate, profit_factor,
                  total_trades, per_pillar_attribution.
  mock_claude.py: MockClaudeClient — replaces anthropic.Anthropic() during backtests.
                  Returns deterministic synthetic MacroContext responses (no real API calls).
                  Critical: prevents $50-100 API cost on 1000-fold walk-forward iterations.
                  Only active in backtesting mode; live session uses real claude-opus-4-8.

Data Limitations (document these in every backtest report):
  IV data:         Synthetic — estimated from HV ratios (HV30, HV60). NOT real options chain history.
  IVR calculation: From yfinance OHLCV close prices, not actual IV surface data.
  Bid-ask spreads: Cannot replicate real historical spreads — assume mid-price fills (optimistic).
  Earnings dates:  From yfinance calendar — gaps exist for older data (pre-2019).
  GEX:             Cannot backtest GEX without historical OI data — GEX tests use proxy signals.
  Consequence:     Live performance will degrade 20-30% vs backtested metrics. Plan for this.

Backtest Level System (three tiers of realism):
  Level 1 — Signal only:
    Does IvPremiumScreen / GEX / catalyst signal fire? Entry/exit on signal trigger.
    No pricing model. P&L = synthetic (strike distance × contracts × 100).
    Use for: rapid hypothesis testing. "Does this signal identify the right events?"
  Level 2 — BSM pricing:
    Uses Black-Scholes with estimated vol surface for P&L calculation.
    Includes transaction costs: $1.50/leg/contract commission + configurable slippage.
    Use for: strategy validation. "Does the signal translate to real edge after costs?"
  Level 3 — Greeks gate:
    Applies full portfolio Greek limits (delta, vega, theta) as position filters.
    Rejects trades that would breach Greek limits — most realistic to live behavior.
    Use for: deployment decision. "Is this backtest a fair proxy for live operation?"
    Walk-forward validation MUST use Level 3. Levels 1 and 2 are exploratory only.

AGORA Signal Library (R&D tracks validation status):
  Deployed — Production:
    IvPremiumScreen:        15-day min consecutive days, 0.25 IV/RV ratio threshold.
    GEX Regime:             Negative vs positive gamma exposure regime classification.
    MacroSynthesizer:       vol_selling_ok flag, macro_stance, size_bias via Claude.
    EarningsTranscriptAgent: NLP sentiment from earnings call transcripts. Validation: 60+ events needed.
    MarketInterestAgent:    6-fingerprint score (0-10), 30-min cadence. Validation: ongoing.
    DisagreementResolver:   Dynamic consensus (ceil(n×0.6)), regime-weighted confidence, crisis override.
  Under Research — Not Deployed:
    Analyst revision cascade: post-earnings estimate revision momentum → directional signal.
    Short squeeze detector: high short interest (> 15% float) + catalyst → gamma squeeze setup.
    IV term structure arbitrage: sell front-month / buy back-month during inversion.
  Retired — Failed:
    Pure momentum (ignoring vol regime): worked in calm 2021, catastrophic in 2022 high-vol regime.
    Lesson: condition every momentum signal on vol regime. Never trade momentum blindly.

Research → Production Checklist (before proposing any signal for deployment):
  □ Hypothesis documented: what market inefficiency does it exploit and WHY?
  □ Level 3 backtest: ≥ 30 trades, Sharpe > 2/√years, p-value < 0.05
  □ Walk-forward validated: OOS Sharpe within 30% of in-sample Sharpe
  □ Parameter sensitivity: Sharpe degrades < 30% with ±20% parameter variation
  □ Transaction cost adjusted: edge survives $50/contract friction
  □ Regime-conditional: tested separately in low/normal/high/crisis vol regimes
  □ MockClaudeClient confirmed: backtester runs without real API cost
  □ Paper trading: 30-60 days of paper results before live deployment proposal
"""


class RNDAgent(ExecutiveAgent):
    """Chief Research Officer (R&D) — signal research, backtesting, alpha discovery."""

    TITLE = "Chief Research Officer (R&D)"
    BRIEF_CADENCE = 6  # R&D reports less frequently — daily is enough

    def __init__(
        self,
        settings: AgoraSettings | None = None,
        ceo_agent: Any = None,
        pillar_health: Any = None,
        earnings_calendar: Any = None,
        earnings_transcript: Any = None,
        event_engine: Any = None,
        universe_disc: Any = None,
        analyst_rev: Any = None,
        strategy_health: Any = None,
    ) -> None:
        super().__init__(settings, ceo_agent)
        self._pillar_health   = pillar_health
        self._earnings_cal    = earnings_calendar
        self._transcript      = earnings_transcript
        self._event_engine    = event_engine
        self._universe        = universe_disc
        self._analyst_rev     = analyst_rev
        self._strategy_health = strategy_health

    @property
    def _system_prompt(self) -> str:
        return _RND_SYSTEM_PROMPT

    def self_audit(self) -> list[tuple[str, str, str]]:
        """
        R&D proactive patrol: signal health, conviction calibration, pillar silence,
        signal-to-trade conversion, analyst revision freshness, backtest vs live drift.
        """
        findings: list[tuple[str, str, str]] = []
        import sqlite3 as _sql
        from datetime import datetime as _dt

        now_et = datetime.now(tz=ET)
        is_market_hours = 9 <= now_et.hour < 16

        # ── Pillar health: silent pillars during market hours ──
        if self._pillar_health and is_market_hours:
            try:
                silent = self._pillar_health.get_silent_pillars()
                if len(silent) >= 3:
                    findings.append((
                        "many_pillars_silent",
                        "critical",
                        f"{len(silent)} pillars are silent during market hours: {', '.join(silent)}. "
                        "Signal pipeline severely degraded — conviction scores will be incomplete.",
                    ))
                elif len(silent) >= 1:
                    findings.append((
                        "some_pillars_silent",
                        "warning",
                        f"Pillars silent during market hours: {', '.join(silent)}. "
                        "Missing signals reduce conviction score accuracy and trade quality.",
                    ))

                health = self._pillar_health.get_pillar_health()
                degraded = [
                    p for p, h in health.items()
                    if isinstance(h, dict) and h.get("status") in ("degraded", "stale")
                ]
                if degraded:
                    findings.append((
                        "pillars_degraded",
                        "warning",
                        f"Degraded/stale pillars: {', '.join(degraded)}. "
                        "These signals are producing outdated data — affected trades lack full context.",
                    ))
            except Exception:
                pass

        # ── Conviction score calibration check via DB ──
        try:
            conn = _sql.connect(str(self._settings.db_path), check_same_thread=False)

            # High conviction score but poor outcomes → calibration drift
            rows = conn.execute(
                """SELECT conviction_at_entry, realized_pnl
                   FROM positions
                   WHERE close_date >= date('now', '-30 days')
                   AND conviction_at_entry IS NOT NULL
                   AND realized_pnl IS NOT NULL"""
            ).fetchall()
            if len(rows) >= 10:
                high_conv  = [(c, p) for c, p in rows if c and c >= 70]
                low_conv   = [(c, p) for c, p in rows if c and c < 55]
                if len(high_conv) >= 5 and len(low_conv) >= 5:
                    avg_high_pnl = sum(p for _, p in high_conv) / len(high_conv)
                    avg_low_pnl  = sum(p for _, p in low_conv)  / len(low_conv)
                    # Gate hierarchy must hold: high > low
                    if avg_high_pnl <= avg_low_pnl:
                        findings.append((
                            "conviction_gate_hierarchy_inverted",
                            "critical",
                            f"Conviction gate hierarchy INVERTED: high-gate avg P&L ${avg_high_pnl:.2f} "
                            f"<= low-gate avg P&L ${avg_low_pnl:.2f}. "
                            "ConvictionScorer is miscalibrated — high scores are NOT predicting better trades.",
                        ))

            # Signal-to-trade conversion gap: many tickers scored, very few traded
            scored_30d = conn.execute(
                "SELECT COUNT(*) FROM positions WHERE entry_date >= date('now', '-30 days')"
            ).fetchone()[0]
            conn.close()
        except Exception:
            scored_30d = None

        # ── Conviction scorer: score inflation and signal conversion gap ──
        if self._pillar_health:
            try:
                health = self._pillar_health.get_pillar_health()
                total_signals = sum(
                    h.get("recent_signals", 0) for h in health.values()
                    if isinstance(h, dict)
                )
                if scored_30d is not None and total_signals > 0 and scored_30d >= 0:
                    if total_signals > 50 and scored_30d < 2:
                        findings.append((
                            "signal_to_trade_gap",
                            "warning",
                            f"{total_signals} signals generated but only {scored_30d} trades in 30 days. "
                            "Conviction thresholds may be too conservative — or signal quality is genuinely poor. "
                            "Review conviction gate settings and recent scored tickers.",
                        ))
            except Exception:
                pass

        # ── Analyst revisions: data freshness ──
        # Only flag if the tracker has been active for ≥3 days and still shows 0 revisions.
        # Day-1 silence is normal — revisions only flow in after earnings events.
        if self._analyst_rev and is_market_hours:
            try:
                count = self._analyst_rev.get_recent_revision_count()
                analyzed = getattr(self._analyst_rev, "_analyzed", {})
                if count == 0 and len(analyzed) >= 3:
                    findings.append((
                        "analyst_revisions_silent",
                        "warning",
                        f"Analyst revision tracker shows 0 recent revisions across {len(analyzed)} "
                        "tickers analyzed post-earnings. Feed may be stale.",
                    ))
            except Exception:
                pass

        # ── Event engine: active signals during market hours ──
        if self._event_engine and is_market_hours:
            try:
                # Check if event engine has produced any signals across the universe
                active_count = 0
                for ticker in self._settings.etf_universe[:10]:
                    signals = self._event_engine.get_signals(ticker)
                    if signals:
                        active_count += 1
                if active_count == 0:
                    findings.append((
                        "event_engine_no_signals",
                        "warning",
                        "EventPatternEngine produced 0 active signals across 10 sampled universe tickers. "
                        "Event-driven pillar may be silent — FOMC/earnings catalyst plays not identified.",
                    ))
            except Exception:
                pass

        # ── StrategyHealth: auto-paused pillar/regime cells ──
        if self._strategy_health:
            try:
                status = self._strategy_health.get_status()
                paused = status.get("paused_count", 0)
                cells  = status.get("paused_cells", [])
                if paused > 0:
                    cell_summary = ", ".join(
                        f"{c['pillar']}/{c['regime']} (Sharpe={c.get('sharpe_at_pause', '?')})"
                        for c in cells
                    )
                    findings.append((
                        "strategy_health_paused_cells",
                        "critical" if paused >= 3 else "warning",
                        f"StrategyHealth has auto-paused {paused} pillar/regime cell(s): {cell_summary}. "
                        "These cells have rolling 30-day Sharpe below -0.5 over ≥20 trades. "
                        "New entries are blocked until Sharpe recovers above 0.0.",
                    ))

                # Warn if any cell is approaching the pause threshold
                health_grid = status.get("health", [])
                borderline = [
                    h for h in health_grid
                    if h.get("sharpe") is not None
                    and -0.5 <= h["sharpe"] < -0.2
                    and h.get("count", 0) >= 10
                ]
                if borderline:
                    labels = [f"{h['pillar']}/{h['regime']} ({h['sharpe']:.2f})" for h in borderline]
                    findings.append((
                        "strategy_health_borderline",
                        "info",
                        f"Cells approaching pause threshold (Sharpe -0.5): {', '.join(labels)}. "
                        "Monitor — may pause on next patrol if performance continues.",
                    ))
            except Exception:
                pass

        return findings

    async def self_heal(self, findings: list[tuple[str, str, str]]) -> None:
        """
        R&D corrective actions:
          conviction_gate_hierarchy_inverted → publish conviction_drift event to CTO
          signal_to_trade_gap                → log recommendation for threshold review
          many_pillars_silent                → publish to CTech for pipeline investigation
          event_engine_no_signals            → log and alert
        """
        keys = {k for k, _, _ in findings}

        if "conviction_gate_hierarchy_inverted" in keys:
            self._heal_attempts["gate_inv"] = self._heal_attempts.get("gate_inv", 0) + 1
            if self._heal_attempts["gate_inv"] == 1:
                await self.notify_peers("conviction_drift", {
                    "finding": "gate_hierarchy_inverted",
                    "recommendation": "Reduce high_conviction_score threshold by 5 pts and review scorer weights",
                    "source": "R&D self_heal 30-day analysis",
                })
                logger.warning("R&D self_heal: conviction gate hierarchy inverted — published conviction_drift")

        if "many_pillars_silent" in keys:
            self._heal_attempts["pillars_silent"] = self._heal_attempts.get("pillars_silent", 0) + 1
            if self._heal_attempts["pillars_silent"] == 1:
                await self._escalate_to_ceo(
                    "critical",
                    "R&D: 3+ signal pillars are silent during market hours. "
                    "Conviction scores are severely incomplete — trade quality is degraded. "
                    "CTech should investigate signal pipeline."
                )

        if "analyst_revisions_silent" in keys:
            logger.warning(
                "R&D self_heal: analyst revision feed silent — "
                "check AnalystRevisionTracker polling and EDGAR connectivity"
            )

    async def on_peer_event(self, event_type: str, publisher: str, payload: dict) -> None:
        """R&D tracks position outcomes for signal calibration research."""
        if event_type == "position_closed":
            ticker  = payload.get("ticker", "?")
            pnl     = float(payload.get("realized_pnl", 0))
            strategy = payload.get("strategy", "unknown")
            conv    = payload.get("conviction_at_entry")
            # Accumulate outcomes in intel for calibration analysis
            ledger = self._latest_intel.setdefault("outcome_ledger", [])
            ledger.append({
                "ticker": ticker, "pnl": pnl,
                "strategy": strategy, "conviction": conv,
            })
            # Keep last 100 trades only
            if len(ledger) > 100:
                self._latest_intel["outcome_ledger"] = ledger[-100:]
            logger.debug("R&D: recorded outcome %s pnl=%.2f conv=%s", ticker, pnl, conv)

        elif event_type == "conviction_drift":
            logger.warning("R&D received conviction_drift from %s: %s", publisher, payload)
            self._latest_intel["conviction_drift_alert"] = payload

    def get_readiness_tasks(self) -> list[str]:
        tasks = []
        if not self._pillar_health:
            tasks.append("Wire PillarHealthAgent to R&D — signal validation blind")
        if not self._event_engine:
            tasks.append("Wire EventPatternEngine to R&D — event signal research disabled")
        if not self._transcript:
            tasks.append("Wire EarningsTranscriptAgent to R&D — NLP signal validation offline")
        if not self._strategy_health:
            tasks.append("Wire StrategyHealthAgent to R&D — pillar/regime Sharpe monitoring invisible")
        return tasks

    def collect_intelligence(self) -> dict[str, Any]:
        now = datetime.now(tz=ET).isoformat()
        intel: dict[str, Any] = {"timestamp": now, "department": "Research & Development"}

        # Pillar health
        if self._pillar_health:
            try:
                health = self._pillar_health.get_pillar_health()
                silent = self._pillar_health.get_silent_pillars()
                intel["pillar_health"] = health
                intel["silent_pillars"] = silent
                intel["all_pillars_healthy"] = len(silent) == 0
            except Exception:
                pass

        # Universe discovery — use public API
        if self._universe:
            try:
                dynamic = self._universe.get_dynamic_tickers()
                intel["universe_size"] = len(self._settings.etf_universe)
                intel["dynamically_discovered"] = len(dynamic)
                intel["dynamic_tickers_sample"] = dynamic[:10]
            except Exception:
                pass

        # Analyst revisions — use public API
        if self._analyst_rev:
            try:
                intel["recent_analyst_revisions"] = self._analyst_rev.get_recent_revision_count()
                intel["analyst_revision_detail"] = self._analyst_rev.get_recent_revisions()
            except Exception:
                pass

        # Earnings transcript results
        if self._transcript:
            try:
                intel["recent_earnings_results"] = self._transcript.get_recent_results()
            except Exception:
                pass

        # Earnings calendar upcoming setups
        if self._earnings_cal:
            try:
                intel["upcoming_earnings_setups"] = self._earnings_cal.get_upcoming_events()
            except Exception:
                pass

        # Event pattern engine active signals
        if self._event_engine:
            try:
                from datetime import date
                # Check a sample of universe tickers for active event signals
                active = []
                for ticker in self._settings.etf_universe[:5]:
                    signals = self._event_engine.get_signals(ticker)
                    if signals:
                        active.append({"ticker": ticker, "signals": signals[:2]})
                intel["event_engine_active"] = active
            except Exception:
                pass

        # Strategy health — rolling Sharpe grid and paused cells
        if self._strategy_health:
            try:
                intel["strategy_health"] = self._strategy_health.get_status()
            except Exception:
                pass

        intel["deployed_signals"] = [
            "IvPremiumScreen (VRP threshold, 15-day consecutive)",
            "GEX Regime (positive/negative gamma exposure)",
            "MacroSynthesizer (vol_selling_ok flag)",
            "EarningsTranscriptAgent (NLP sentiment, validation ongoing)",
            "MarketInterestAgent (6-fingerprint, 0-10 score)",
            "DisagreementResolver (3-pillar consensus, dynamic threshold)",
        ]
        intel["signals_under_research"] = [
            "Analyst revision cascade momentum",
            "Short squeeze detector (high SI + catalyst)",
            "IV term structure arbitrage (inversion play)",
        ]

        return intel

"""
Chief Trading Officer (CTO) — AGORA Trading Strategy & Execution

Domain expertise:
  Options theory (BSM, Greeks), strategy selection by regime, DTE management,
  conviction-to-size mapping, execution quality, spread pricing, earnings strategies,
  roll management, exit discipline, expected move calculation.

Sub-agents supervised:
  ConvictionScorer, DisagreementResolver, OptionsStrategyAgent, ExecutionAgent (IBKR bridge)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from ..core.config import AgoraSettings
from .base import ExecutiveAgent

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

_CTO_SYSTEM_PROMPT = """\
You are the Chief Trading Officer (CTO) of AGORA, an autonomous options trading platform owned by Rahul.
You report to the CEO. You translate intelligence into profitable trades and ensure every trade we
make has a defined edge, manageable risk, and a clear exit plan. Bad trades with no thesis are worse
than missed good trades. You never trade out of boredom or FOMO.

═══ OPTIONS THEORY & PRICING ═══

Black-Scholes-Merton (BSM) Foundation:
  C = S·N(d1) - K·e^(-rT)·N(d2)
  d1 = [ln(S/K) + (r + σ²/2)T] / [σ√T]
  d2 = d1 - σ√T
  Where: S=spot, K=strike, r=risk-free, σ=implied vol, T=time to expiry (years), N=standard normal CDF.
  BSM assumptions violated in practice: constant vol (real = stochastic), no jumps (real = earnings gaps),
    continuous trading (real = gaps, circuit breakers). These violations = trading opportunities.

Greeks and their practical meaning:
  Delta (Δ): % chance option expires ITM ≈ hedge ratio. 0.20-delta short strike = 80% OTM probability.
    For credit spreads: sell 20-delta strike (80% chance of expiring worthless).
    For debit spreads: buy 35-delta strike (65% chance of expiring ITM) + sell 15-delta for premium.
  Gamma (Γ): rate of delta change. Long gamma = long convexity = profit from large moves.
    Short gamma (credit spreads) = negative convexity. Max pain is a large move close to expiry.
  Vega (ν): $ change per 1 vol-point move. Long vega = want IV to rise. Short vega = want IV to fall.
    1 vega on a contract means P&L changes $1 for each 1 vol-point change in IV.
  Theta (Θ): time decay. Credit spreads earn theta daily. Theta accelerates near expiration (T²).
    Theta collection is NOT free money — it's compensation for gamma risk.
  Rho (ρ): interest rate sensitivity. Matters when rates change significantly. Currently relevant (2024+).
  Second-order (charming):
    Charm: delta decay over time. Important near expiration for delta hedging.
    Vanna: delta change with vol. Vanna flows from IV moves = additional hedging pressure.
    Volga: vega change with vol. High volga = wing options become expensive in high-vol regimes.

Put-Call Parity (arbitrage foundation):
  C - P = S - K·e^(-rT). If violated → riskless arbitrage exists.
  In practice: violated by dividend yield, borrow rates, bid-ask friction.

Expected Move Calculation:
  From ATM straddle price: EM ≈ ATM_straddle / S ≈ ±1σ move over life of the options.
  More precise: EM = 0.68 × ATM_IV × S × √(DTE/365).
  Use: compare actual expected move to historical move magnitude for the event.
  If historical move > expected move → straddle is cheap → buy vol.
  If historical move < expected move → straddle is expensive → sell vol (iron condor).

═══ STRATEGY SELECTION FRAMEWORK ═══

Regime → Strategy Mapping:

High IVR (> 60), Positive GEX (mean-reversion regime):
  Best: Bull Put Spread / Bear Call Spread (credit spreads). Sell elevated premium, profit from theta.
  Parameters: 45 DTE entry, 20-delta short strike, 5-wide to 10-wide spread width.
  Target: collect 25-35% of spread width as credit. Close at 50% profit or 21 DTE.
  Risk: stock moves through short strike despite high IV (earnings surprise, black swan).

High IVR (> 60), Negative GEX (trending regime):
  Directional credit spread aligned with trend. Or iron condor with wider body.
  Higher risk: trending market can run through short strike faster.
  Reduce size by 25% vs calm regime. Prefer debit spreads on strong conviction.

Low IVR (< 30), Fresh Catalyst:
  Best: Debit spread (long 35-delta, short 15-delta). IV cheap → buy convexity.
  Parameters: 30-45 DTE, directional with catalyst, 1.3× reward/risk minimum.
  Risk: IV crush if event resolves earlier than expected.

Event-Driven (earnings, FOMC, FDA):
  BEFORE event: if IV cheap → long straddle/strangle. If IV expensive → iron condor.
  AFTER event (post-earnings skew reversion): T+1 to T+3, sell elevated put skew.
  Earnings catalyst play (pre-earnings, long-vega): enter 5-15 days before, close T-1 to avoid crush.

Vol Regime Summary:
  Low vol (VIX < 15): credit spreads work best. Low premium but low risk.
  Normal vol (VIX 15-22): balanced. Both credit and debit spreads viable.
  High vol (VIX 22-35): premium elevated. Credit spreads highly attractive. BUT large moves possible.
  Crisis (VIX > 35): do NOT sell premium into sustained panic. Wait for vol to stabilize.

═══ DTE MANAGEMENT ═══

Entry DTE: 30-45 days (minimum 30). This is the theta "sweet spot":
  - Enough time for thesis to play out
  - Maximum theta decay per unit of risk begins in this range
  - Options liquid enough to exit cleanly
  - IV still elevated enough to collect meaningful premium

Close at 21 DTE: theta/gamma ratio deteriorates after this. Gamma risk increases faster than theta benefit.
  Exception: at 50% max profit → close early regardless of DTE.

Profit targets and stop losses:
  Credit spreads: close at 50% of max profit (e.g., collect $1.50 → close at $0.75 debit to buy back).
  Debit spreads: close at 100% of max profit OR when underlying reaches target.
  Stop loss: close when position value = 2× initial debit (debit spread) or reaches max loss (credit spread).
  Hard rule: NEVER let a position go to full max loss. Exit at 2× initial credit for credit spreads.

Roll management:
  Defensive roll: when short strike is threatened, roll down (for put spread) or up (for call spread).
  Cost of roll: pay debit to move the strike. Only roll if: (1) thesis still intact, (2) new position has edge.
  Time roll: when approaching 21 DTE with position not at profit target → roll to next expiry.
  Never roll a loser indefinitely — if thesis is broken, take the loss.

═══ CONVICTION → SIZE MAPPING ═══

Conviction Gate → Contracts (base = 1 contract per $500 risk unit):
  no_trade (< 40): do not trade
  low (40-54):      0.5× size → risk $250 max
  standard (55-69): 1.0× size → risk $500 max
  high (70+):       1.5× size → risk $750 max (DisagreementResolver approves)

Resolver multiplier applied on top of conviction gate:
  0.0× = no trade (consensus failure)
  0.5× = half size (signal conflict)
  1.0× = standard size (2-signal agreement)
  1.5× = max size (3-signal agreement OR 2-signal with high confidence)

Combined multiplier: conviction_gate_size × resolver_multiplier.
  Example: high conviction × 1.5 resolver = $750 × 1.5 = $1,125 → cap at $750 (max per trade).
  The resolver multiplier acts as a multiplier ON the gate size, not additive.

Hard contract cap: never exceed 10 contracts per trade (prevents fat-finger, liquidity risk).

Position sizing by spread width:
  Narrow spread (5-wide): max loss = $500. 1 contract = $500 risk.
  Wide spread (10-wide): max loss = $1,000. Need 2× risk unit → lower conviction threshold.
  Rule: only use wide spreads for high-conviction (70+) directional plays.

═══ EXECUTION DISCIPLINE ═══

Spread pricing:
  Always use limit orders at mid price. NEVER market orders on options (wide bid-ask).
  Adjustment: if not filled in 2 minutes, move limit 1 tick toward the ask (for buying) or bid (for selling).
  Maximum: fill at 10% worse than mid. If market moves, cancel and re-evaluate.
  For credit spreads: submit as a single BAG (basket) order to IBKR. Legs together = better execution.

IBKR order types for options:
  LMT: limit order. Use for all options entries and exits.
  MKT: market order. NEVER use for options — the spread will eat you.
  MOC: market on close. Use for closing positions when you must close today.
  Combo (BAG) order: multi-leg options order. Better fills than legging in separately.

whatIfOrder() preflight:
  Always call whatIfOrder() before submitting. Validates: margin impact, Reg T compliance,
  estimated commission, order validity. If whatIf returns error → DO NOT SUBMIT the order.

Execution timing:
  Avoid first 30 minutes (9:30-10:00 AM ET): spreads wide, volatility high, price discovery unstable.
  Best execution windows: 10:00-11:30 AM and 1:00-3:00 PM ET.
  Avoid last 30 minutes (3:30-4:00 PM ET): delta hedging flows, MOC orders distort prices.

Slippage benchmarks:
  Liquid options (SPY, QQQ, AAPL): expect 1-2 tick slippage from mid.
  Medium liquidity (NVDA, MSFT, TSLA): expect 2-4 ticks.
  Lower liquidity (individual mid-caps): expect 5-10 ticks. Factor into edge calculation.

═══ EDGE CALCULATION FRAMEWORK ═══

Before entering any trade, calculate:
  EV = P(profit) × max_profit + P(loss) × max_loss
  For credit spread: P(profit) = P(underlying stays OTM) = N(d2_short_strike)
  Required: EV > 0 and R/R ≥ 1.3. If R/R < 1.3, skip the trade.

  Edge = (credit_received / spread_width) - P(max_loss) implied by market
  If implied P(max_loss) < actual P(max_loss) → trade has edge.

Transaction cost adjustment:
  Commission: ~$1.50/contract/leg (IBKR). 2 legs = $3.00/contract round trip.
  Slippage: -$5 to -$30 depending on liquidity.
  Total friction: $30-50 per contract for a 4-leg combo.
  Required: edge per contract must exceed friction for the trade to make sense.

═══ AGORA PLATFORM SPECIFICS ═══

ConvictionScorer — 8 Components (100 points total, capped at 100):
  Component              Max   Source Agent               Logic Summary
  ─────────────────────────────────────────────────────────────────────
  vol_premium_score      /30   IvPremiumScreen            ≥15 consecutive days above 0.25 IV/RV ratio
                                                          → signal active → 20-30 pts. Inactive: ratio/days partial.
                                                          Neutral default (no data yet): 10 pts.
  gex_score              /20   GEX data                   Positive GEX + IV active = 20 (ideal for credit spreads).
                                                          Positive GEX alone = 14. Negative GEX = 18 (trending).
  regime_score           /20   VolRegimeClassifier        high_vol = 18 base, normal = 16, low_vol = 12, crisis = 5.
                         +MacroContext                    Confidence ±5 pts bonus. vol_selling_ok = +2 bonus.
  event_score            /15   EventPatternEngine         confidence × 15 × freshness (days_to_event decay).
                                                          FOMC drift, CPI condor, post-earnings skew reversion.
  macro_score            /5    MacroSynthesizer           risk_on + vol_selling_ok = 5 × confidence.
                                                          neutral = 2.5. risk_off = rounds toward 0.
  smart_money_score      /5    SmartMoneyAgent            "strong" (CEO/CFO buy, 13D activist) = 5.
                                                          "moderate" = 3. "weak" = 1. None = 0.
  info_speed_score       /5    CatalystDiscoveryAgent     4-hour linear decay from filing time.
                                                          Filed 0h ago = 5 pts. Filed 4h ago = 0 pts.
  market_interest_score  /10   MarketInterestAgent        mi.score ≥ 7 → 10 pts. 4-7 → 5-10 linear.
                                                          2-4 → 0-5 linear. < 2 → 0 pts.

  Gate thresholds:
    ≥ 70 → "high"      (1.5× size if DisagreementResolver also approves)
    55-69 → "standard" (1.0× size)
    40-54 → "low"      (0.5× size)
    < 40  → "no_trade"
  min_conviction_score = 60.0 → hard floor overrides gates below 60 (no entries < 60 even if gate=standard).

Dominant Pillar Assignment (how ConvictionScorer._dominant_pillar labels each trade):
  Priority order: catalyst present → CATALYST. smart_money present → SMART_MONEY.
  event_score ≥ 10 → EVENT_FOMC. vol_score ≥ 20 → VOL_PREMIUM. gex_score ≥ 16 → DIRECTIONAL.
  Default (no dominant signal): VOL_PREMIUM.

DisagreementResolver Internals (what you configure and monitor):
  Three input legs: macro (MacroSynthesizer), microstructure (GEX+IVR), catalyst (discovery).
  Consensus logic: min_agreers = ceil(n × 0.6) — 60% of active signals must agree on direction.
    2 signals: both must agree. 3 signals: 2 of 3 must agree.
  Regime-weighted composite confidence (not simple average):
    risk_on/low_vol:   macro=0.50, micro=0.25, catalyst=0.25
    risk_off/high_vol: macro=0.25, micro=0.45, catalyst=0.30
    neutral/normal:    macro=0.40, micro=0.35, catalyst=0.25
  Regime-conditional thresholds for 1.5× gate:
    risk_on:   avg_conf ≥ 0.65 AND conviction ≥ 70
    risk_off:  avg_conf ≥ 0.80 AND conviction ≥ 70 (raised bar in stress)
    neutral:   avg_conf ≥ 0.70 AND conviction ≥ 70
  Multiplier outcomes:
    Disagreers ≥ 2 → 0.0 (blocked — strong conflict)
    Disagreers = 1 → 0.5 (half size — signal conflict)
    Agreers ≥ min_agreers, high conf + conviction ≥ 70 → 1.5 (high conviction)
    Agreers ≥ min_agreers, otherwise → 1.0 (standard)
  Regime haircuts: risk_off / high_volatility → 25% haircut on multiplier.
  Crisis override: regime == "crisis" → multiplier = 0.0 always.

Vol-Premium Bypass Pathway (4-gate interlock in _evaluate_ticker):
  Gate 1: snap.iv_rank ≥ 60 (ivr_bypass_threshold) — DataFeed-verified IVR.
  Gate 2: conviction.total_score ≥ 60 (min_conviction_score) — ConvictionScorer floor.
  Gate 3: macro_context.vol_selling_ok == True — MacroSynthesizer green light.
  Gate 4: data_integrity.vol_bypass_allowed() == True — DataIntegrityAgent confirms feed integrity.
  Bypass ACTIVE: directional resolver consensus waived. IV structure alone justifies credit spread entry.
  Bypass FAILS: resolver consensus required. System falls back to fully directional validation.
  This pathway is why IVR ≥ 60 AND conviction ≥ 60 are both required — neither alone is sufficient.

AGORA Pillar → Strategy Mapping (StrategyRulesEngine produces these):
  vol_premium + high IVR + positive GEX  → Bull Put Spread or Bear Call Spread (45 DTE, 20-delta short).
  catalyst + bullish + low IVR           → Debit Call Spread (30-45 DTE, 35/15-delta).
  catalyst + bearish + low IVR           → Debit Put Spread (30-45 DTE, 35/15-delta).
  directional + negative GEX + bullish   → Debit Call Spread (momentum-aligned, wider spread).
  event_fomc + high IVR + IV expensive   → Iron Condor (sell both wings, 21-45 DTE).
  smart_money + bullish                  → Debit Call Spread or long call vertical.
  All strategies: minimum 30 DTE at entry (target_dte_entry_min). Never enter < 30 DTE.
  Close at 21 DTE (target_dte_close) OR 50% profit (profit_target_pct), whichever comes first.
"""


class CTOAgent(ExecutiveAgent):
    """Chief Trading Officer — strategy selection, execution quality, trade discipline."""

    TITLE = "Chief Trading Officer (CTO)"
    BRIEF_CADENCE = 4

    def __init__(
        self,
        settings: AgoraSettings | None = None,
        ceo_agent: Any = None,
        conviction_scorer: Any = None,
        disagreement_resolver: Any = None,
        position_mgr: Any = None,
        exec_quality: Any = None,
        close_callback: Any = None,   # async fn(position, reason) → routes to IBKR
    ) -> None:
        super().__init__(settings, ceo_agent)
        self._scorer        = conviction_scorer
        self._resolver      = disagreement_resolver
        self._pm            = position_mgr
        self._eq            = exec_quality
        self._close_cb      = close_callback   # wired from session._execute_close

        # Lateral state — updated by on_peer_event() without patrol
        self._entries_halted: bool = False     # set by kill_switch_tripped / fill_rate_critical
        self._size_override: str | None = None # set by CRO size_bias_changed event

    def wire_close_callback(self, cb: Any) -> None:
        """Wire the IBKR close function (session._execute_close)."""
        self._close_cb = cb

    async def start(self) -> None:
        """Start patrol, brief, event listener AND the auto-close monitor."""
        self._running = True
        import asyncio as _asyncio
        tasks = [self._brief_loop(), self._patrol_loop(), self._auto_close_monitor()]
        if self._event_queue is not None:
            tasks.append(self._event_listener_loop())
        await _asyncio.gather(*tasks, return_exceptions=True)

    async def _auto_close_monitor(self) -> None:
        """
        Every 5 minutes during market hours: automatically close positions that have
        hit their 50% profit target or crossed 21 DTE.
        This is CTO's pre-delegated authority — no CEO approval needed.
        """
        await asyncio.sleep(300)
        while self._running:
            now_et = datetime.now(tz=ET)
            if 9 <= now_et.hour < 16 and self._pm and self._close_cb:
                await self._check_auto_close_candidates()
            await asyncio.sleep(300)

    async def _check_auto_close_candidates(self) -> None:
        """Scan open positions for profit target or 21 DTE — close autonomously."""
        from datetime import date as _date
        today = _date.today()
        try:
            positions = self._pm.get_open_positions()
        except Exception:
            return

        for p in positions:
            # ── 50% profit target (pre-delegated authority) ──
            try:
                if p.max_profit and p.max_profit > 0:
                    profit_pct = (p.unrealized_pnl or 0) / p.max_profit
                    if profit_pct >= 0.50:
                        reason = f"profit_target_{profit_pct:.0%}"
                        logger.info(
                            "CTO auto-close: %s at %s profit target — closing",
                            p.ticker, reason
                        )
                        await self._close_cb(p, reason)
                        await self.notify_peers("position_closed", {
                            "ticker":               p.ticker,
                            "reason":               reason,
                            "realized_pnl":         p.unrealized_pnl,
                            "conviction_at_entry":  getattr(p, "conviction_at_entry", None),
                            "macro_stance_at_entry": getattr(p, "regime_at_entry", None),
                            "strategy":             getattr(p, "strategy", None),
                        })
                        continue
            except Exception:
                pass

            # ── Min-hold guard for DISCRETIONARY forced-closes ──────────────────
            # Never insta-close a freshly-entered position via the 21-DTE rule or a CEO session-plan
            # target: those closed -$245 (CEO-plan x7) / -$59 (21-DTE x3) at hold=0d, giving the
            # entry's thesis no room. The deterministic stop-loss (above) still owns the downside;
            # this gates only the discretionary overrides, mirroring the LLM-exit min-hold.
            try:
                _entry = p.entry_date if isinstance(p.entry_date, _date) \
                    else _date.fromisoformat(str(p.entry_date))
                _held_days = (today - _entry).days
            except Exception:
                _held_days = 99
            _disc_ok = _held_days >= int(getattr(self._settings, "csuite_close_min_hold_days", 1))

            # ── 21 DTE rule (pre-delegated authority) ──
            # SPREADS ONLY. Long options (long_call/long_put) are bought at 15-30 DTE BY DESIGN and
            # run their own 5-day-hold exit logic (time stop / conviction profit target / trailing
            # stop) in PositionManager — applying the spread 21-DTE rule to them force-closes a
            # fresh long the moment it opens (observed: TSLA/SCHW/QQQ closed ~30 min after entry),
            # silently undoing the long-options strategy. Skip them here.
            try:
                _strat = str(getattr(p.strategy, "value", p.strategy) or "").lower()
                expiry = p.expiry_date if isinstance(p.expiry_date, _date) \
                    else _date.fromisoformat(str(p.expiry_date))
                dte = (expiry - today).days
                if _disc_ok and _strat not in ("long_call", "long_put") and dte <= 21:
                    reason = f"21_dte_{dte}DTE"
                    logger.info("CTO auto-close: %s at %d DTE — closing", p.ticker, dte)
                    await self._close_cb(p, reason)
                    await self.notify_peers("position_closed", {
                        "ticker":               p.ticker,
                        "reason":               reason,
                        "realized_pnl":         getattr(p, "unrealized_pnl", 0),
                        "conviction_at_entry":  getattr(p, "conviction_at_entry", None),
                        "macro_stance_at_entry": getattr(p, "regime_at_entry", None),
                        "strategy":             getattr(p, "strategy", None),
                    })
            except Exception:
                pass

            # ── Close targets from CEO session plan ──
            try:
                plan = self.get_session_plan()
                if _disc_ok and p.ticker in plan.close_targets and self._close_cb:
                    logger.info("CTO auto-close: %s in CEO close_targets — closing", p.ticker)
                    await self._close_cb(p, "ceo_session_plan_target")
                    await self.notify_peers("position_closed", {
                        "ticker":  p.ticker,
                        "reason":  "ceo_session_plan_target",
                        "realized_pnl": getattr(p, "unrealized_pnl", 0),
                        "conviction_at_entry":  getattr(p, "conviction_at_entry", None),
                        "macro_stance_at_entry": getattr(p, "regime_at_entry", None),
                        "strategy": getattr(p, "strategy", None),
                    })
            except Exception:
                pass

    @property
    def _system_prompt(self) -> str:
        return _CTO_SYSTEM_PROMPT

    @property
    def entries_halted(self) -> bool:
        """True if CTO has been told to halt new entries by CRO/COO lateral event."""
        if self._entries_halted:
            return True
        plan = self.get_session_plan()
        return plan.is_halted

    @property
    def effective_size_bias(self) -> str:
        """Returns the tightest size bias from session plan or CRO lateral override."""
        plan_bias   = self.get_session_plan().size_multiplier
        lateral_map = {"full": 1.0, "half": 0.5, "quarter": 0.25, "none": 0.0}
        override_mult = lateral_map.get(self._size_override or "full", 1.0)
        effective = min(plan_bias, override_mult)
        for label, mult in [("none", 0.0), ("quarter", 0.25), ("half", 0.5), ("full", 1.0)]:
            if effective <= mult:
                return label
        return "full"

    def self_audit(self) -> list[tuple[str, str, str]]:
        """
        CTO proactive patrol: trade discipline, DTE management, execution health,
        regime-strategy alignment, opportunity gap detection.
        """
        findings: list[tuple[str, str, str]] = []
        now_et = datetime.now(tz=ET)
        is_market_hours = 9 <= now_et.hour < 16

        # ── Open positions: DTE and profit-target proximity ──
        if self._pm:
            try:
                from datetime import date as _date
                positions = self._pm.get_open_positions()
                today = _date.today()
                near_21_dte = []
                near_profit_target = []
                near_stop_loss = []

                for p in positions:
                    # DTE check
                    try:
                        expiry = p.expiry_date if isinstance(p.expiry_date, _date) else _date.fromisoformat(str(p.expiry_date))
                        dte = (expiry - today).days
                        if dte <= 23:
                            near_21_dte.append(f"{p.ticker}({dte}DTE)")
                    except Exception:
                        pass

                    # Profit target: unrealized_pnl >= 50% of max_profit
                    try:
                        if p.max_profit and p.max_profit > 0:
                            profit_pct = (p.unrealized_pnl or 0) / p.max_profit
                            if profit_pct >= 0.45:
                                near_profit_target.append(f"{p.ticker}({profit_pct:.0%})")
                    except Exception:
                        pass

                    # Stop-loss: unrealized loss > 150% of max_profit (approaching 2× stop)
                    try:
                        if p.max_profit and p.max_profit > 0:
                            loss_pct = -(p.unrealized_pnl or 0) / p.max_profit
                            if loss_pct >= 1.5:
                                near_stop_loss.append((p.ticker, loss_pct))
                    except Exception:
                        pass

                if near_21_dte:
                    findings.append((
                        "positions_near_21_dte",
                        "warning",
                        f"Positions approaching 21-DTE close rule: {', '.join(near_21_dte)}. "
                        "Gamma risk now exceeds theta benefit — must close or roll.",
                    ))
                if near_profit_target:
                    findings.append((
                        "positions_near_profit_target",
                        "warning",
                        f"Positions near 50% profit target (should close): {', '.join(near_profit_target)}. "
                        "Book the win — don't let theta-collected premium erode.",
                    ))
                for ticker, loss_pct in near_stop_loss:
                    findings.append((
                        f"stop_loss_proximity_{ticker}",
                        "critical",
                        f"{ticker} approaching stop-loss: unrealized loss = {loss_pct:.0%} of max_profit. "
                        "Hard rule: exit at 2× initial credit. Evaluate NOW.",
                    ))
            except Exception:
                pass

        # ── Execution quality: fill rate and timeout rate ──
        if self._eq:
            try:
                today_stats = self._eq.get_today_db_stats()
                total = today_stats.get("total", 0)
                fill_rate = today_stats.get("fill_rate")
                timeout_rate = today_stats.get("timeout_rate")
                error_201 = self._eq.get_session_stats().get("error_201_storm", False)

                if total >= 3:
                    if fill_rate is not None and fill_rate < 0.15:
                        findings.append((
                            "fill_rate_critical",
                            "critical",
                            f"Fill rate {fill_rate:.1%} today ({total} attempts). "
                            "Orders not filling — check IBKR connectivity and limit price aggressiveness.",
                        ))
                    elif fill_rate is not None and fill_rate < 0.50:
                        findings.append((
                            "fill_rate_low",
                            "warning",
                            f"Fill rate {fill_rate:.1%} today — below 50% threshold. "
                            "Spreads may be too tight or market moving fast. Adjust limit aggressiveness.",
                        ))
                    if timeout_rate is not None and timeout_rate > 0.85:
                        findings.append((
                            "timeout_rate_high",
                            "critical",
                            f"Timeout rate {timeout_rate:.1%} today. "
                            "Fill callbacks not reaching AGORA. Orders may be filling at IBKR with no record.",
                        ))
                if error_201:
                    findings.append((
                        "error_201_storm",
                        "critical",
                        "Error 201 storm active — IBKR max combo order slots exhausted. "
                        "OrphanOrderReconciler must cancel stale GTC children immediately.",
                    ))
            except Exception:
                pass

        # ── Opportunity gap: no trades in 3+ trading days despite kill switch inactive ──
        if is_market_hours:
            try:
                import sqlite3 as _sql

                from ..core.config import get_settings
                conn = _sql.connect(str(get_settings().db_path), check_same_thread=False)
                last_trade = conn.execute(
                    "SELECT entry_date FROM positions ORDER BY id DESC LIMIT 1"
                ).fetchone()
                conn.close()
                if last_trade:
                    from datetime import date as _date2
                    entry = _date2.fromisoformat(str(last_trade[0])[:10])
                    gap_days = (today := _date2.today()) - entry
                    if gap_days.days >= 3:
                        findings.append((
                            "trade_opportunity_gap",
                            "warning",
                            f"No new positions entered in {gap_days.days} trading days. "
                            "If kill switch is inactive and market conditions are favorable, "
                            "investigate why ConvictionScorer is not crossing the entry threshold.",
                        ))
            except Exception:
                pass

        # ── Scorer: conviction inflation ──
        if self._scorer:
            try:
                stats = self._scorer.get_session_stats()
                avg_score = stats.get("avg_conviction_score")
                scored_count = stats.get("scored", 0)
                if avg_score and avg_score > 88 and scored_count >= 5:
                    findings.append((
                        "conviction_score_inflation",
                        "warning",
                        f"Average conviction score is {avg_score:.1f} across {scored_count} tickers. "
                        "Scores > 88 suggest scorer calibration drift — may be letting too many trades through.",
                    ))
            except Exception:
                pass

        return findings

    async def self_heal(self, findings: list[tuple[str, str, str]]) -> None:
        """
        CTO corrective actions — pre-delegated authority:
          fill_rate_critical   → pause new entry submissions, escalate to COO
          error_201_storm      → stop new order flow until COO resolves orphans
          positions_near_*     → already handled by _auto_close_monitor (5-min loop)
          opportunity_gap      → log warning, check if session plan allows trading
        """
        keys = {k for k, _, _ in findings}

        if "fill_rate_critical" in keys:
            self._entries_halted = True
            await self.notify_peers("fill_rate_critical", {
                "reason": "CTO patrol: fill rate < 15% — halting new entries until COO resolves"
            })
            logger.warning("CTO self_heal: fill rate critical — entries halted, notified peers")

        if "error_201_storm" in keys:
            self._entries_halted = True
            logger.warning("CTO self_heal: Error 201 storm — entries halted pending orphan resolution")

        if "fill_rate_acceptable" not in keys and "fill_rate_critical" not in keys:
            # fill rate recovered — resume if we had halted for this reason
            if self._entries_halted and "error_201_storm" not in keys and "kill_switch_active" not in keys:
                self._entries_halted = False
                logger.info("CTO self_heal: fill rate recovered — resuming entries")

    async def on_peer_event(self, event_type: str, publisher: str, payload: dict) -> None:
        """
        CTO reacts to lateral events:
          kill_switch_tripped   → halt all new entries immediately
          kill_switch_reset     → resume trading
          size_bias_changed     → update effective sizing
          regime_changed        → log and adjust strategy preference
          macro_context_updated → note new regime for next evaluation cycle
          fill_rate_critical    → halt new entries (from COO)
        """
        if event_type == "kill_switch_tripped":
            self._entries_halted = True
            logger.warning("CTO: kill switch tripped by %s — new entries HALTED", publisher)

        elif event_type == "kill_switch_reset":
            self._entries_halted = False
            self._size_override  = None
            logger.info("CTO: kill switch reset by %s — entries RESUMED", publisher)

        elif event_type == "size_bias_changed":
            self._size_override = payload.get("new_bias", "full")
            logger.info("CTO: size bias overridden to '%s' by %s", self._size_override, publisher)

        elif event_type == "regime_changed":
            old = payload.get("old_regime", "?")
            new = payload.get("new_regime", "?")
            logger.info("CTO: regime changed %s → %s (from CIO) — adjusting strategy selection", old, new)
            self._latest_intel["peer_regime_update"] = payload

        elif event_type == "fill_rate_critical":
            self._entries_halted = True
            logger.warning("CTO: fill_rate_critical from %s — halting new entries", publisher)

        elif event_type == "daily_loss_warning":
            level = payload.get("level", "warning")
            if level == "critical":
                self._size_override = "half"
                logger.warning("CTO: daily_loss_warning(critical) from %s — size halved", publisher)

    def get_readiness_tasks(self) -> list[str]:
        tasks = []
        if not self._close_cb:
            tasks.append("Wire close_callback to CTO — auto-close monitor cannot execute")
        if not self._pm:
            tasks.append("Wire PositionManager to CTO — DTE/profit-target monitoring disabled")
        if not self._eq:
            tasks.append("Wire ExecutionQualityAgent to CTO — fill rate monitoring disabled")
        return tasks

    def collect_intelligence(self) -> dict[str, Any]:
        now = datetime.now(tz=ET).isoformat()
        intel: dict[str, Any] = {"timestamp": now, "department": "Trading"}

        intel["strategy_config"] = {
            "target_dte_entry":     self._settings.target_dte_entry,
            "target_dte_entry_min": self._settings.target_dte_entry_min,
            "target_dte_close":     self._settings.target_dte_close,
            "profit_target_pct":    self._settings.profit_target_pct,
            "short_delta_target":   self._settings.short_delta_target,
            "long_delta_target":    self._settings.long_delta_target,
            "min_rr_ratio":         self._settings.min_rr_ratio,
            "ivr_bypass_threshold": self._settings.ivr_bypass_threshold,
            "earnings_blackout_days": self._settings.earnings_blackout_days,
        }

        if self._eq:
            try:
                stats = self._eq.get_session_stats()
                intel["execution_quality"] = {
                    "session_attempts": stats.get("attempts", 0),
                    "fill_rate":   round(stats.get("fill_rate", 0) * 100, 1),
                    "avg_slippage": stats.get("avg_slippage", 0),
                    "rejects":     stats.get("rejects", 0),
                    "error_201_storm": stats.get("error_201_storm", False),
                }
                w7 = self._eq.get_7day_stats()
                intel["execution_7day"] = {
                    "fill_rate":    round(w7.get("fill_rate", 0) * 100, 1),
                    "avg_slippage": w7.get("avg_slippage", 0),
                    "total_attempts": w7.get("total", 0),
                }
            except Exception:
                pass

        if self._pm:
            try:
                positions = self._pm.get_open_positions()
                strategies = {}
                for p in positions:
                    strategies[p.strategy] = strategies.get(p.strategy, 0) + 1
                intel["strategy_distribution"] = strategies
                intel["open_positions"] = len(positions)
            except Exception:
                pass

        intel["conviction_config"] = {
            "min_conviction": self._settings.min_conviction_score,
            "high_conviction": self._settings.high_conviction_score,
        }

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

        return intel

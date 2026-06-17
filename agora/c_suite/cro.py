"""
Chief Risk Officer (CRO) — AGORA Risk Management

Domain expertise:
  Portfolio Greeks, VaR, Expected Shortfall, drawdown management,
  position sizing (Kelly), options-specific risks (IV crush, pin, assignment),
  correlation breakdown in stress, kill switch protocol, stress testing.

Sub-agents supervised:
  RiskCouncil, CircuitBreaker, ComplianceAgent
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from ..core.config import AgoraSettings
from .base import ExecutiveAgent

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

_CRO_SYSTEM_PROMPT = """\
You are the Chief Risk Officer (CRO) of AGORA, an autonomous options trading platform owned by Rahul.
You report to the CEO. Your primary obligation is capital preservation. You would rather miss 10 trades
than blow up the account on one bad risk management decision.

═══ PORTFOLIO RISK FUNDAMENTALS ═══

Greek Exposure Limits (account = $25,000):
  Delta:  |portfolio_delta| ≤ 0.30 × (account/$10k) = 0.75 deltas total
  Vega:   |portfolio_vega|  ≤ 200 × (account/$10k)  = 500 vega total
  Theta:  |portfolio_theta| ≤ 0.5% of account/day   = $125/day max decay
  Gamma:  Monitor for gamma squeeze: short gamma + large move = exponential loss

Options P&L anatomy:
  dP = delta×dS + ½×gamma×dS² + vega×dIV + theta×dt
  In a crisis: dS is large (gamma dominates), dIV spikes (vega losses), theta irrelevant.
  Credit spreads: short vega + short gamma. When vol spikes, BOTH hurt simultaneously.

═══ OPTIONS-SPECIFIC RISKS ═══

IV Crush:
  Before earnings: ATM IV rises (event risk priced in). After announcement: IV collapses 50-80%.
  Long vega positions (debit spreads) lose from IV crush even if directionally correct.
  Rule: ALWAYS close long-vega positions T-1 before earnings. T-0 is too late.
  Magnitude by cap size: large-cap (AAPL, MSFT) crush 40-50%. Small-cap 60-80%.

Pin Risk (expiration):
  Underlying "pins" near short strike → uncertain whether options expire ITM or OTM.
  Risk: being assigned on short strike, long leg expires worthless → net loss exceeds max.
  Mitigation: close spreads that are within 1% of short strike with < 5 DTE.

Early Assignment Risk:
  American options on dividend-paying stocks → risk of early exercise before ex-dividend date.
  Rule: avoid selling in-the-money calls on dividend stocks near ex-date.
  If short ITM call with dividend approaching: buy back or roll immediately.

Assignment:
  Short options ITM at expiration → assignment. Short put → must buy 100 shares.
  For 10k account: buying 100 shares of a $50 stock ($5,000) consumes 50% of capital.
  Rule: never let short options expire ITM. Close at 21 DTE.

Liquidity Risk:
  Bid-ask spread > 10% of mid → execution friction eats the edge.
  OI < 500 → illiquid, cannot exit position cleanly.
  Rule: check bid-ask and OI BEFORE entering. Non-negotiable.

═══ POSITION SIZING & RISK BUDGETING ═══

Kelly Criterion (simplified for options):
  f* = (edge / odds) where edge = EV of the trade, odds = max win / max loss.
  Full Kelly is too aggressive. Use 25% Kelly (quarter Kelly) for robustness.
  Example: win_rate=55%, RR=1.3 → Kelly = (0.55×1.3 - 0.45)/1.3 = 20% → quarter Kelly = 5%.
  For a $25k account: 5% = $1,250 max risk per trade.

Risk Per Trade Hierarchy:
  Level 1 (no_trade, conviction < 40): SKIP
  Level 2 (low, 40-54): risk 0.5× base = $250 max loss
  Level 3 (standard, 55-69): risk 1.0× base = $500 max loss
  Level 4 (high, 70+): risk 1.5× base = $750 max loss
  Absolute max: never risk > 3% of account on any single trade.

Correlation & Concentration:
  Equity correlations spike to ~0.90 in crisis (was 0.3 in calm). All positions lose together.
  Sector limit: max 1 position per correlated group (SPY/QQQ/IWM count as one group).
  Strategy limit: max 60% of positions in same strategy type (e.g., credit spreads).
  Earnings concentration: never have > 2 positions with earnings within the same 2-week window.

═══ DRAWDOWN MANAGEMENT ═══

Drawdown tiers:
  -1% account ($250): standard monitoring
  -2% account ($500): daily loss limit → auto kill switch trip
  -5% account ($1,250): weekly loss limit review with owner
  -10% account ($2,500): strategy review, halt new trades for 3 days
  -20% account ($5,000): full system halt, owner intervention required

Recovery math: a 20% drawdown requires 25% gain to recover. A 50% drawdown requires 100%.
Never try to trade out of a drawdown by increasing size → this is how accounts blow up.

Calmar Ratio = Annualized Return / Max Drawdown. Target > 1.5. Below 0.5 → review strategy.
Sortino Ratio = (Return - Risk_free) / Downside_deviation. Penalizes only negative vol. Target > 1.0.

═══ STRESS TESTING SCENARIOS ═══

Run weekly. Report results to CEO.

Scenario 1 — VIX Shock (VIX 15→40 overnight):
  Short vega positions lose vega × ΔIV per contract. Calculate total vega P&L.
  Example: portfolio vega = -300, ΔIV = +25 vol points → loss = -300 × 25 = -$7,500.
  Check: does this exceed weekly loss limit? If yes, reduce vega exposure.

Scenario 2 — Flash Crash (-7% SPY in 1 day):
  Delta P&L: portfolio_delta × (-0.07 × SPY_price × 100_per_contract).
  Gamma P&L (second order): ½ × portfolio_gamma × (0.07 × SPY_price)².
  Margin call risk: portfolio margin accounts may face intraday calls.

Scenario 3 — Earnings Cluster (3 positions report same week):
  If all 3 gap adversely: assume 2σ adverse move on each.
  Calculate max loss scenario.

Scenario 4 — Liquidity Crisis (bid-ask widens 5×):
  Cost to exit all positions at worst bid. Can we exit? At what cost?

═══ KILL SWITCH PROTOCOL ═══

Auto-trip conditions:
  1. Daily loss > $500 (2% of $25k account)
  2. Single position loss > 2× max_loss_dollars
  3. Portfolio delta breach: |delta| > 0.75
  4. IBKR connectivity loss > 10 minutes during market hours

Never auto-reset. Reset protocol:
  1. Identify root cause
  2. Confirm market conditions are stable
  3. Verify all positions are properly accounted for
  4. Manual reset by owner via POST /agora/kill/reset

Reporting escalation levels:
  INFO:     normal monitoring, brief in daily report
  WARNING:  deviation from plan, include in next CEO brief
  CRITICAL: immediate CEO alert + owner notification

═══ AGORA PLATFORM SPECIFICS ═══

Strategy Pillars (how positions are classified for risk attribution):
  vol_premium:  Credit spreads on elevated IVR. Primary structural edge. Most consistent P&L.
  catalyst:     Event-driven (EDGAR 8-K, M&A, FDA). Short alpha decay window (< 4 hours).
  directional:  GEX-aligned momentum debit spreads. Trend-following; higher variance.
  smart_money:  Insider cluster / 13D activist. Long-biased, lower frequency, high confidence.
  event_fomc:   Calendar-driven (FOMC, CPI, NFP). Iron condors or straddles around known events.

Risk Budget by Pillar (CRO enforces these allocations):
  vol_premium:          40% of total risk → $200/day risk capacity (@ $500/day limit).
  catalyst + smart_money: 35% combined → $175/day.
  directional + event_fomc: 25% combined → $125/day.
  Cross-pillar rule: never > 2 positions with earnings in the same 2-week window.

Current Live Config (production values — check config.py if changed):
  account_size = $25,000
  max_open_positions = 6            (hard cap; block new entries at limit)
  max_per_correlation_group = 1     (SPY / QQQ / IWM count as one correlated group)
  earnings_blackout_days = 3        (block vol_premium entries within 3 DTE of earnings)
  min_conviction_score = 60.0       (hard floor; no entries below this score)
  ivr_bypass_threshold = 60.0       (minimum feed-verified IVR to activate vol bypass)
  risk_per_trade_dollars = $500     (base risk unit — 1 contract, 5-wide spread)
  daily_loss_limit = $500 (2%)      (auto kill-switch trip)
  weekly_loss_limit = $1,500 (6%)   (manual owner review required)
  stop_loss_multiplier = 2.0×       (exit when loss = 2× initial credit/debit)
  profit_target_pct = 50%           (close at 50% of max profit)

Earnings Proximity Interlock (know this pipeline cold):
  _pre_earnings_tickers: dict — live session state tracking tickers with active pre-earnings positions.
  Interlock rule: if ticker in _pre_earnings_tickers → new entry BLOCKED (prevents double-position).
  Startup check (_earnings_proximity_startup_check): on session start, alerts CEO about open positions
    NOT marked is_pre_earnings but within earnings_blackout_days of next earnings.
    These will NOT be auto-closed by the T-1 logic — require manual CRO review.
  Vol-premium entries: additionally blocked if next earnings ≤ earnings_blackout_days away
    (even if ticker is not in _pre_earnings_tickers).

Vol-Premium Bypass Gate (4-gate interlock — ALL must pass or bypass is denied):
  Gate 1: snap.iv_rank ≥ 60 (ivr_bypass_threshold) — DataFeed IVR validation.
  Gate 2: conviction.total_score ≥ 60 (min_conviction_score) — ConvictionScorer floor.
  Gate 3: macro_context.vol_selling_ok == True — MacroSynthesizer green light.
  Gate 4: data_integrity.vol_bypass_allowed() == True — DataIntegrityAgent feed health check.
  When bypass ACTIVE: directional resolver consensus is waived — vol structure alone justifies entry.
  When bypass FAILS: DisagreementResolver consensus IS required — more conservative fallback path.
  CRO monitors: if Gate 4 trips (feed degraded), alert CEO and disable new vol_premium entries.
"""


class CROAgent(ExecutiveAgent):
    """Chief Risk Officer — capital preservation and risk governance."""

    TITLE = "Chief Risk Officer (CRO)"
    BRIEF_CADENCE = 2  # every 2 hours during trading day

    def __init__(
        self,
        settings: AgoraSettings | None = None,
        ceo_agent: Any = None,
        risk_council: Any = None,
        circuit_breaker: Any = None,
        compliance: Any = None,
        position_mgr: Any = None,
    ) -> None:
        super().__init__(settings, ceo_agent)
        self._risk_council  = risk_council
        self._circuit_breaker = circuit_breaker
        self._compliance    = compliance
        self._position_mgr  = position_mgr

    @property
    def _system_prompt(self) -> str:
        return _CRO_SYSTEM_PROMPT

    def collect_intelligence(self) -> dict[str, Any]:
        now = datetime.now(tz=ET).isoformat()
        intel: dict[str, Any] = {"timestamp": now, "department": "Risk Management"}

        if self._risk_council:
            try:
                ks = self._risk_council.get_kill_switch_state()
                intel["kill_switch"] = ks
            except Exception:
                pass

        if self._position_mgr:
            try:
                greeks = self._position_mgr.get_portfolio_greeks()
                positions = self._position_mgr.get_open_positions()
                settings = self._settings
                intel["portfolio_greeks"] = {
                    "delta":  round(greeks.get("delta", 0), 4),
                    "vega":   round(greeks.get("vega", 0), 2),
                    "theta":  round(greeks.get("theta", 0), 2),
                    "gamma":  round(greeks.get("gamma", 0), 4),
                }
                intel["greek_limits"] = {
                    "delta_limit":  round(settings.max_portfolio_delta, 2),
                    "vega_limit":   round(settings.max_portfolio_vega, 0),
                    "theta_limit":  round(settings.max_daily_theta_dollars, 0),
                }
                intel["delta_utilization_pct"] = round(
                    abs(greeks.get("delta", 0)) / settings.max_portfolio_delta * 100, 1
                ) if settings.max_portfolio_delta else 0
                intel["vega_utilization_pct"] = round(
                    abs(greeks.get("vega", 0)) / settings.max_portfolio_vega * 100, 1
                ) if settings.max_portfolio_vega else 0
                intel["open_position_count"] = len(positions)
                intel["max_position_count"]  = settings.max_open_positions
                # Identify highest-risk positions (largest unrealized loss)
                risky = sorted(positions, key=lambda p: p.unrealized_pnl)[:3]
                intel["highest_risk_positions"] = [
                    {
                        "ticker":       p.ticker,
                        "strategy":     p.strategy,
                        "unrealized_pnl": round(p.unrealized_pnl, 2),
                        "max_loss":     round(p.max_loss_dollars, 2),
                        "expiry":       str(p.expiry_date),
                    }
                    for p in risky
                ]
            except Exception as exc:
                intel["positions_error"] = str(exc)

        # Stress test quick estimate
        vega = intel.get("portfolio_greeks", {}).get("vega", 0)
        intel["stress_vix_spike_40_loss"] = round(vega * 25, 2)  # VIX +25 pts
        intel["daily_loss_limit"] = self._settings.daily_loss_limit_dollars
        intel["account_size"] = self._settings.account_size

        # Circuit breaker state
        if self._circuit_breaker:
            try:
                cb_state = self._circuit_breaker.get_state()
                intel["circuit_breaker"] = cb_state
            except Exception:
                pass

        # Compliance — wash sale watchlist
        if self._compliance:
            try:
                watchlist = self._compliance.get_wash_sale_watchlist()
                intel["wash_sale_watchlist"] = watchlist
                intel["wash_sale_count"] = len(watchlist)
            except Exception:
                pass

        return intel

    def self_audit(self) -> list[tuple[str, str, str]]:
        """
        CRO proactive risk audit — called every 30 min by the patrol loop.
        Checks every risk dimension independently of what sub-agents self-report.
        """
        findings: list[tuple[str, str, str]] = []
        s = self._settings

        # ── Kill switch ───────────────────────────────────────────────
        try:
            if self._risk_council and self._risk_council.is_kill_switch_active():
                ks = self._risk_council.get_kill_switch_state()
                findings.append((
                    "kill_switch_active", "critical",
                    f"Kill switch ACTIVE: {ks.get('reason','unknown')} "
                    f"(tripped {ks.get('tripped_at','?')[:16]} by {ks.get('tripped_by','?')}). "
                    f"No new positions should be opened until reset."
                ))
        except Exception:
            pass

        # ── Greek utilization ─────────────────────────────────────────
        try:
            if self._position_mgr:
                greeks = self._position_mgr.get_portfolio_greeks()
                delta = abs(greeks.get("delta", 0))
                vega  = abs(greeks.get("vega", 0))

                delta_pct = delta / s.max_portfolio_delta * 100 if s.max_portfolio_delta else 0
                vega_pct  = vega  / s.max_portfolio_vega  * 100 if s.max_portfolio_vega  else 0

                if delta_pct > 100:
                    findings.append(("delta_limit_breached", "critical",
                        f"Portfolio delta {delta:.2f} EXCEEDS limit {s.max_portfolio_delta:.2f} "
                        f"({delta_pct:.0f}% of limit). Reduce directional exposure immediately."))
                elif delta_pct > 80:
                    findings.append(("delta_limit_high", "warning",
                        f"Portfolio delta {delta:.2f} at {delta_pct:.0f}% of limit "
                        f"{s.max_portfolio_delta:.2f}. Approaching breach."))

                if vega_pct > 100:
                    findings.append(("vega_limit_breached", "critical",
                        f"Portfolio vega {vega:.1f} EXCEEDS limit {s.max_portfolio_vega:.1f} "
                        f"({vega_pct:.0f}% of limit). Vol exposure too large."))
                elif vega_pct > 80:
                    findings.append(("vega_limit_high", "warning",
                        f"Portfolio vega {vega:.1f} at {vega_pct:.0f}% of limit. Approaching breach."))
        except Exception:
            pass

        # ── Positions approaching stop-loss ───────────────────────────
        try:
            if self._position_mgr:
                positions = self._position_mgr.get_open_positions()
                stop_mult = s.stop_loss_multiplier
                for p in positions:
                    entry = getattr(p, "entry_price", 0) or 0
                    upnl  = getattr(p, "unrealized_pnl", 0) or 0
                    stop_threshold = entry * 100 * getattr(p, "contracts", 1) * stop_mult * -1
                    if entry > 0 and upnl < stop_threshold * 0.75:
                        findings.append((
                            f"stop_loss_proximity_{p.ticker}", "critical",
                            f"{p.ticker} unrealized P&L ${upnl:.0f} is >75% toward stop-loss "
                            f"${stop_threshold:.0f}. Consider closing to limit max loss."
                        ))
                    elif entry > 0 and upnl < stop_threshold * 0.50:
                        findings.append((
                            f"stop_loss_warning_{p.ticker}", "warning",
                            f"{p.ticker} unrealized P&L ${upnl:.0f} is >50% toward stop-loss "
                            f"${stop_threshold:.0f}. Monitor closely."
                        ))
        except Exception:
            pass

        # ── Daily loss utilization ────────────────────────────────────
        try:
            if self._position_mgr:
                import sqlite3 as _sql
                db_path = str(self._settings.db_path)
                conn = _sql.connect(db_path, check_same_thread=False)
                realized = conn.execute(
                    "SELECT COALESCE(SUM(realized_pnl),0) FROM positions WHERE close_date=date('now')"
                ).fetchone()[0]
                conn.close()
                unrealized = sum(
                    getattr(p, "unrealized_pnl", 0) or 0
                    for p in (self._position_mgr.get_open_positions())
                )
                total_loss = min(realized + unrealized, 0)
                daily_limit = s.daily_loss_limit_dollars
                if daily_limit > 0:
                    pct_used = abs(total_loss) / daily_limit * 100
                    if pct_used >= 85:
                        findings.append(("daily_loss_critical", "critical",
                            f"Daily loss ${total_loss:.0f} is {pct_used:.0f}% of limit "
                            f"${daily_limit:.0f}. Kill switch will trip at 100%."))
                    elif pct_used >= 60:
                        findings.append(("daily_loss_high", "warning",
                            f"Daily loss ${total_loss:.0f} is {pct_used:.0f}% of limit "
                            f"${daily_limit:.0f}. Tighten new position sizing."))
        except Exception:
            pass

        # ── Correlation concentration ─────────────────────────────────
        try:
            if self._position_mgr:
                positions = self._position_mgr.get_open_positions()
                pillar_counts: dict[str, list[str]] = {}
                for p in positions:
                    pillar = str(getattr(p, "pillar", "unknown"))
                    pillar_counts.setdefault(pillar, []).append(p.ticker)
                for pillar, tickers in pillar_counts.items():
                    max_allowed = s.max_per_correlation_group
                    if len(tickers) > max_allowed:
                        findings.append((
                            f"concentration_{pillar}", "warning",
                            f"Pillar '{pillar}' has {len(tickers)} positions "
                            f"({', '.join(tickers)}) — limit is {max_allowed}. "
                            f"Correlated positions amplify drawdown risk."
                        ))
        except Exception:
            pass

        # ── Circuit breaker ───────────────────────────────────────────
        try:
            if self._circuit_breaker:
                state = self._circuit_breaker.get_state()
                if state.get("tripped"):
                    findings.append(("circuit_breaker_tripped", "critical",
                        f"Circuit breaker TRIPPED: {state.get('reason','unknown')}. "
                        f"Order submission blocked until reset."))
        except Exception:
            pass

        return findings

    async def self_heal(self, findings: list[tuple[str, str, str]]) -> None:
        """
        CRO corrective actions — taken autonomously within pre-delegated authority:
          kill_switch_active      → publish event so CTO halts new entries immediately
          daily_loss_critical     → reduce size_bias to 'half' via event, escalate
          delta/vega limit breach → publish event for CTO to stop directional entries
        """
        keys = {k for k, _, _ in findings}
        {k: s for k, s, _ in findings}

        # Kill switch active → notify CTO to stop new entries laterally
        if "kill_switch_active" in keys:
            self._heal_attempts["kill_switch_active"] = self._heal_attempts.get("kill_switch_active", 0) + 1
            if self._heal_attempts["kill_switch_active"] == 1:  # only publish once per activation
                await self.notify_peers("kill_switch_tripped", {
                    "reason": "CRO patrol: kill switch active",
                    "halt_new_entries": True,
                })
                logger.warning("CRO self_heal: published kill_switch_tripped event to peers")

        # Daily loss ≥ 85% → reduce size to half, notify CTO
        if "daily_loss_critical" in keys:
            self._heal_attempts["daily_loss_critical"] = self._heal_attempts.get("daily_loss_critical", 0) + 1
            if self._heal_attempts["daily_loss_critical"] <= 2:
                await self.notify_peers("size_bias_changed", {
                    "new_bias": "half",
                    "reason": "CRO: daily loss critical — auto-reducing position sizing to 50%",
                })
                await self.notify_peers("daily_loss_warning", {
                    "level": "critical",
                    "pct_used": 85,
                })
                logger.warning("CRO self_heal: daily loss critical — published size_bias_changed(half)")

        elif "daily_loss_high" in keys:
            self._heal_attempts["daily_loss_high"] = self._heal_attempts.get("daily_loss_high", 0) + 1
            if self._heal_attempts["daily_loss_high"] == 1:
                await self.notify_peers("daily_loss_warning", {
                    "level": "warning",
                    "pct_used": 60,
                })

        # Delta/vega limit breach → notify CTO to stop directional new entries
        if "delta_limit_breached" in keys or "vega_limit_breached" in keys:
            await self.notify_peers("size_bias_changed", {
                "new_bias": "none",
                "reason": "CRO: Greek limit breached — blocking new directional positions",
            })
            logger.warning("CRO self_heal: Greek limit breach — published size_bias_changed(none)")

    async def on_peer_event(self, event_type: str, publisher: str, payload: dict) -> None:
        """CRO receives feedback events from CFO (profit_factor_low, fill_rate_critical)."""
        if event_type == "profit_factor_low":
            logger.warning(
                "CRO received profit_factor_low from %s: %s — will tighten sizing in next brief",
                publisher, payload.get("profit_factor", "?")
            )
            self._latest_intel["cfo_profit_factor_alert"] = payload

    def get_readiness_tasks(self) -> list[str]:
        tasks = []
        s = self._settings
        if not (s.daily_loss_limit_dollars > 0):
            tasks.append("Set daily_loss_limit_dollars in config (required for kill switch)")
        if not (s.weekly_loss_limit_dollars > 0):
            tasks.append("Set weekly_loss_limit_dollars in config")
        if not self._circuit_breaker:
            tasks.append("Wire CircuitBreakerAgent to CRO")
        return tasks

"""
Chief Financial Officer (CFO) — AGORA Financial Performance & Capital Management

Domain expertise:
  P&L attribution (alpha vs beta, by pillar/regime/conviction), performance metrics
  (Sharpe, Sortino, Calmar, profit factor, win rate), capital allocation (Kelly, risk budgeting),
  drawdown analysis, risk-adjusted returns, tax considerations, performance reporting.

Sub-agents supervised:
  PnlAttributor, PsiMonitor (rolling metrics), AgentPerformanceMonitor
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from ..core.config import AgoraSettings
from ..ops.llm_cost_log import DAILY_CAP_USD as _LLM_DAILY_CAP
from ..ops.llm_cost_log import daily_cost_summary as _llm_daily_cost
from .base import ExecutiveAgent

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

_CFO_SYSTEM_PROMPT = """\
You are the Chief Financial Officer (CFO) of AGORA, an autonomous options trading platform owned by Rahul.
You report to the CEO. You are the keeper of financial truth. Every number you report must be accurate.
You translate trading activity into financial performance, ensure capital is allocated efficiently,
and tell the CEO exactly how much the system is making, losing, and why.

═══ P&L FUNDAMENTALS ═══

Options P&L Components:
  Realized P&L: closed position gain/loss. P&L = (exit_price - entry_price) × contracts × 100.
    For credit spreads: P&L = credit_received - debit_to_close (positive if profitable).
    For debit spreads: P&L = debit_to_close - initial_debit (positive if profitable).
  Unrealized P&L: open position mark-to-market. Mark = current_option_price × contracts × 100.
    Use mid-price for mark (bid-ask midpoint). Never use ask-price for long, bid for short.
  Total P&L: realized + unrealized. Daily, weekly, monthly, YTD aggregations.

P&L Attribution Dimensions:
  By strategy type: credit spreads vs debit spreads vs event-driven vs vol-premium bypass.
  By pillar: vol_premium, catalyst, directional, smart_money, event_fomc.
  By regime: risk_on, neutral, risk_off, high_vol, low_vol.
  By conviction gate: high (70+) vs standard (55-69) vs low (40-54).
  By ticker: per-name attribution (which tickers contribute most alpha).
  By sector: tech vs finance vs energy vs healthcare.

Alpha vs Beta Decomposition:
  Beta = market-correlated P&L. Credit spreads on SPY/QQQ = high beta.
  Alpha = market-independent P&L. Catalyst plays, smart money trades = alpha-seeking.
  True alpha: P&L unexplained by SPY/sector returns. Regression: P&L = α + β × SPY_return.
  Target: α > 0 with statistical significance (p < 0.05). β between -0.3 and +0.3 (market-neutral).

═══ PERFORMANCE METRICS ═══

Return Metrics:
  Total Return = (ending_nav - starting_nav) / starting_nav × 100%
  Annualized Return = (1 + total_return)^(365/days) - 1
  CAGR for options: meaningful only after 12+ months of data.
  Risk-adjusted Return = annualized_return / annualized_vol (Sharpe numerator).

Sharpe Ratio:
  Sharpe = (Rp - Rf) / σp
  Where: Rp = portfolio return, Rf = risk-free rate (3-month T-bill, ~5% currently), σp = portfolio std dev.
  Calculation: use daily P&L, annualize (multiply by √252).
  Benchmarks: < 0 = value destruction. 0-1 = marginal. 1-2 = good. > 2 = excellent.
  Options caveat: Sharpe is flawed for options (non-normal returns, fat tails, negative skew).
    Credit spreads have high Sharpe until they blow up (hidden tail risk).

Sortino Ratio:
  Sortino = (Rp - Rf) / σ_downside
  Only penalizes downside volatility. Better metric for options (asymmetric payoffs).
  σ_downside = std dev of NEGATIVE daily P&L only.
  Target: Sortino > 1.5 for options strategies.

Calmar Ratio:
  Calmar = Annualized_Return / Max_Drawdown (absolute value).
  Example: 30% annual return, 10% max drawdown → Calmar = 3.0 (excellent).
  Target: Calmar > 1.5. Below 0.5 → strategy risk/reward is unacceptable.

Profit Factor:
  PF = Gross_Profit / Gross_Loss (absolute value). PF > 1 = profitable.
  Calculation: sum all winning trades / sum all losing trades (absolute).
  Target: PF > 1.5. PF > 2.0 = excellent. PF < 1.0 = losing strategy.

Win Rate (% profitable trades):
  High win rate + low profit factor = many small wins, few large losses (credit spread profile).
  Low win rate + high profit factor = few big wins, many small losses (debit spread profile).
  The COMBINATION matters: EV = win_rate × avg_win + (1 - win_rate) × avg_loss (negative).
  Acceptable for credit spreads: win_rate 65-75%, PF 1.3-1.8.
  Acceptable for debit spreads: win_rate 40-55%, PF 1.8-3.0.

Expected Value per Trade:
  EV = (win_rate × avg_profit) - ((1 - win_rate) × avg_loss)
  Positive EV is NECESSARY but not sufficient. Also need adequate sample size (n > 30).
  Statistical significance: need 30+ trades per strategy type to claim edge.

Max Drawdown:
  MDD = (Peak - Trough) / Peak × 100%
  Track: current drawdown from recent peak (real-time alert if > 5%).
  Recovery time: how many days to recover to previous peak.
  Drawdown duration: time from peak to trough + time from trough back to peak.
  Underwater curve: visual of drawdown over time. Monitor for elongating recovery periods.

═══ CAPITAL ALLOCATION ═══

Risk Budgeting:
  Total risk budget = account_size × max_risk_pct = $25,000 × 3% = $750/day.
  Allocate budget across strategies:
    vol_premium: 40% of budget ($300/day risk capacity)
    catalyst/event: 35% of budget ($262/day risk capacity)
    smart_money: 25% of budget ($187/day risk capacity)
  Rebalance when one strategy underperforms by > 20% vs benchmark.

Kelly Criterion for Capital Allocation:
  Full Kelly: f* = (p × b - q) / b where b = odds ratio (win/loss), p = P(win), q = P(loss).
  For portfolio: allocate Kelly fraction of TOTAL capital to each strategy bucket.
  Use HALF Kelly for robustness (reduces ruin probability from 0% to near-0% in theory).
  Example: strategy with 60% win rate, 1.5 R/R:
    Kelly = (0.60 × 1.5 - 0.40) / 1.5 = (0.90 - 0.40)/1.5 = 0.333 = 33% → half Kelly = 16.5%.
    With $25k account: allocate $4,125 to this strategy bucket.

Margin & Buying Power:
  Reg T margin: spreads defined by max_loss, not full notional. No leverage on defined-risk spreads.
  Buying power reduction per spread = spread_width × 100 × contracts (this is the max loss).
  Available buying power = cash - Σ(all open position max losses).
  NEVER let available buying power go below 20% of account ($5,000 buffer).
  Portfolio margin accounts: more efficient capital use, but require $125k+ account (not applicable yet).

═══ TAX CONSIDERATIONS ═══

Options Tax Treatment (US):
  Short-term gain: options held < 1 year (most options trades). Taxed at ordinary income rate.
  Section 1256 contracts: broad-based index options (SPX, SPY options qualify).
    60/40 rule: 60% long-term capital gain rate, 40% short-term. Regardless of holding period.
    MAJOR advantage: use SPY/SPX options when possible for tax efficiency.
  Mark-to-market election (Section 475): professional traders can elect MTM accounting.
    Not applicable for most retail accounts.

Wash Sale Rule:
  If you sell an option at a loss and repurchase "substantially identical" within 30 days:
    the loss is deferred. Cost basis adjusted.
  For options: selling a put and buying a call on same stock within 30 days may trigger.
  AGORA compliance: ComplianceAgent tracks this. Do not open same-side position within 30 days of loss close.

Tax-Loss Harvesting Opportunity:
  Near year-end: realize losses in positions that won't recover to offset gains.
  Do not close winners just for tax purposes if position thesis is intact.
  Coordinate with owner before any tax-motivated trades.

═══ PERFORMANCE REPORTING ═══

Daily Report (4:30 PM):
  - Net P&L today (realized + unrealized change)
  - Contribution by strategy/pillar
  - Win/loss trades today with reasoning
  - Running monthly P&L vs target

Weekly Report (Friday EOD):
  - Week summary: return, Sharpe estimate, win rate
  - Best and worst trades with analysis
  - Strategy performance breakdown
  - Risk metrics: current drawdown, max drawdown, position concentration

Monthly Report:
  - Full performance metrics vs benchmark (SPY buy-and-hold comparison)
  - Factor attribution: how much from vol premium vs catalyst vs event
  - Capital allocation review: rebalance budget across strategy buckets if needed
  - Tax lot summary for owner review

Performance Target (suggested starting benchmarks):
  Monthly: +1.5-3% net (18-36% annualized for a premium-selling strategy)
  Max monthly drawdown: -3% (stop new trades, review, report to owner)
  Win rate: > 60% (credit spread dominated book)
  Sharpe: > 1.0 rolling 90 days
  Profit factor: > 1.5 rolling 30 trades

═══ AGORA PLATFORM SPECIFICS ═══

AGORA Strategy Pillars (P&L attribution must break down by these):
  vol_premium:  Credit spreads on elevated IVR. Primary edge (structural VRP). Target: 40% of risk budget.
                Best Sharpe in AGORA — consistent theta collection. Monitor for VRP regime decay.
  catalyst:     Event-driven (EDGAR 8-K, M&A, FDA catalyst). Short alpha decay (< 4h window).
                Target: 20% of risk budget. High hit rate but small sample size per month.
  directional:  GEX-aligned momentum debit spreads. Trend-following. Target: 15% of budget.
                Higher variance than vol_premium. Only active in negative GEX regime.
  smart_money:  Insider cluster / 13D activist plays. Long-biased, lower frequency.
                Target: 15% of budget. Best win rate (insider signal most reliable).
  event_fomc:   Calendar-driven (FOMC, CPI, NFP). Iron condors or straddles. Target: 10%.
                Known entry/exit dates — operationally clean. IV crush timing predictable.
  Key diagnostic: if vol_premium P&L is negative → VRP regime may have inverted → alert CRO.
  Key diagnostic: if catalyst P&L exceeds vol_premium → portfolio is becoming more event-driven → assess.

Financial Sub-Agent Roster (your direct reports — know their outputs):
  PnlAttributor:           Breaks realized P&L by pillar, regime, conviction gate, ticker, and sector.
                           Also computes alpha vs beta: P&L = α + β × SPY_return (regression per period).
                           Target: α > 0 statistically significant, β between -0.3 and +0.3.
  PsiMonitor:              Rolling Sharpe, Sortino, Calmar, max drawdown, and profit factor.
                           PSI (Performance Stability Index): 30-day rolling Sharpe. Alert at < 0.5.
                           Tracks drawdown underwater curve — alert if recovery time > 10 trading days.
  AgentPerformanceMonitor: Per-signal-agent alpha: which agents (IvPremiumScreen, CatalystAgent, etc.)
                           are generating actual P&L. Tracks: signal count, hit rate, avg conviction, P&L/signal.
                           Use to detect signal decay: if agent's hit rate drops below 50% → flag to R&D.

Conviction Gate → P&L Diagnostic:
  high (70+, 1.5× size):    Should show best win rate and avg profit. If not → scorer miscalibrated.
  standard (55-69, 1.0×):   Baseline. Largest sample — primary Sharpe calculation population.
  low (40-54, 0.5×):        Should underperform high gate materially. If similar → low gate adds noise.
  Tracker: compare avg P&L per trade by gate each month. Gate hierarchy must hold; escalate if inverted.

Risk Budget Allocation (CFO monitors these ratios against actuals):
  Total daily risk budget = $25,000 × 2% = $500/day.
  vol_premium:         40% → $200/day capacity.
  catalyst:            20% → $100/day capacity.
  directional:         15% → $75/day capacity.
  smart_money:         15% → $75/day capacity.
  event_fomc:          10% → $50/day capacity.
  Rebalance trigger: if one pillar exceeds its allocation by > 20% for 5+ trading days → adjust.

Current Live Config (financial parameters):
  account_size = $25,000
  risk_per_trade_dollars = $500    (1× base unit; 0.5× low gate, 1.5× high gate)
  max_contracts_per_trade = 10     (hard cap regardless of size multiplier)
  stop_loss_multiplier = 2.0×      (exit when position loss = 2× initial credit or debit)
  profit_target_pct = 50%          (close at 50% of max profit — don't get greedy)
  min_rr_ratio = 1.3               (entry blocked if reward/risk < 1.3)
  Buying power rule: available BP must stay > 20% of account ($5,000 buffer) at all times.
"""


class CFOAgent(ExecutiveAgent):
    """Chief Financial Officer — P&L, performance metrics, capital efficiency."""

    TITLE = "Chief Financial Officer (CFO)"
    BRIEF_CADENCE = 4

    def __init__(
        self,
        settings: AgoraSettings | None = None,
        ceo_agent: Any = None,
        position_mgr: Any = None,
        pnl_attributor: Any = None,
        agent_performance: Any = None,
    ) -> None:
        super().__init__(settings, ceo_agent)
        self._pm         = position_mgr
        self._attributor = pnl_attributor
        self._perf       = agent_performance

    @property
    def _system_prompt(self) -> str:
        return _CFO_SYSTEM_PROMPT

    def self_audit(self) -> list[tuple[str, str, str]]:
        """
        CFO proactive patrol: daily loss consumption, win rate, profit factor,
        drawdown, consecutive losing days, capital efficiency.
        """
        findings: list[tuple[str, str, str]] = []
        import sqlite3 as _sql

        # ── Pull realized P&L data from DB ──
        try:
            conn = _sql.connect(str(self._settings.db_path), check_same_thread=False)

            # Today's realized P&L vs daily loss limit
            today_rows = conn.execute(
                "SELECT COALESCE(SUM(realized_pnl), 0) FROM positions WHERE close_date = date('now')"
            ).fetchone()
            today_pnl = today_rows[0] if today_rows else 0.0

            daily_limit = self._settings.daily_loss_limit_dollars
            if daily_limit and daily_limit > 0 and today_pnl < 0:
                loss_pct = abs(today_pnl) / daily_limit
                if loss_pct >= 0.85:
                    findings.append((
                        "daily_loss_limit_near_critical",
                        "critical",
                        f"Daily loss ${today_pnl:.2f} = {loss_pct:.0%} of ${daily_limit:.0f} limit. "
                        "Halt new entries immediately — risk of kill switch trip.",
                    ))
                elif loss_pct >= 0.60:
                    findings.append((
                        "daily_loss_limit_near_warning",
                        "warning",
                        f"Daily loss ${today_pnl:.2f} = {loss_pct:.0%} of ${daily_limit:.0f} limit. "
                        "Reduce new position sizing — approaching daily loss threshold.",
                    ))

            # 30-day win rate and profit factor
            rows_30d = conn.execute(
                """SELECT realized_pnl FROM positions
                   WHERE close_date >= date('now', '-30 days')
                   AND realized_pnl IS NOT NULL"""
            ).fetchall()
            pnl_list = [r[0] for r in rows_30d if r[0] is not None]
            if len(pnl_list) >= 10:
                wins   = [p for p in pnl_list if p > 0]
                losses = [p for p in pnl_list if p < 0]
                win_rate = len(wins) / len(pnl_list)
                gross_profit = sum(wins) if wins else 0
                gross_loss   = abs(sum(losses)) if losses else 0
                profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

                if win_rate < 0.40:
                    findings.append((
                        "win_rate_low_30d",
                        "warning",
                        f"30-day win rate {win_rate:.0%} ({len(wins)}/{len(pnl_list)} trades). "
                        "Below 40% — strategy thesis or signal calibration needs review.",
                    ))
                if profit_factor < 0.8:
                    findings.append((
                        "profit_factor_critical_30d",
                        "critical",
                        f"30-day profit factor {profit_factor:.2f} — below 0.8 is destroying capital. "
                        f"Gross profit ${gross_profit:.2f}, gross loss ${gross_loss:.2f}. "
                        "Suspend new entries until root cause is identified.",
                    ))
                elif profit_factor < 1.0:
                    findings.append((
                        "profit_factor_below_1_30d",
                        "warning",
                        f"30-day profit factor {profit_factor:.2f} — below 1.0 means we are net losing. "
                        f"Gross profit ${gross_profit:.2f}, gross loss ${gross_loss:.2f}. Review immediately.",
                    ))

            # Consecutive losing days
            daily_pnl_rows = conn.execute(
                """SELECT close_date, SUM(realized_pnl) as day_pnl
                   FROM positions
                   WHERE close_date >= date('now', '-14 days')
                   AND realized_pnl IS NOT NULL
                   GROUP BY close_date
                   ORDER BY close_date DESC"""
            ).fetchall()
            consecutive_losses = 0
            for _, day_pnl in daily_pnl_rows:
                if day_pnl is not None and day_pnl < 0:
                    consecutive_losses += 1
                else:
                    break
            if consecutive_losses >= 4:
                findings.append((
                    "consecutive_losing_days",
                    "critical",
                    f"{consecutive_losses} consecutive losing trading days. "
                    "Systematic issue — halt new entries, convene strategy review with CRO.",
                ))
            elif consecutive_losses >= 3:
                findings.append((
                    "consecutive_losing_days_warning",
                    "warning",
                    f"{consecutive_losses} consecutive losing days. "
                    "Pattern suggests regime mismatch or signal calibration issue.",
                ))

            # Drawdown check from all realized P&L
            all_pnl = conn.execute(
                "SELECT realized_pnl FROM positions WHERE realized_pnl IS NOT NULL ORDER BY close_date, id"
            ).fetchall()
            if all_pnl:
                equity = 0.0
                peak = 0.0
                max_dd = 0.0
                for (p,) in all_pnl:
                    equity += p
                    if equity > peak:
                        peak = equity
                    dd = (peak - equity) / peak if peak > 0 else 0
                    if dd > max_dd:
                        max_dd = dd
                if max_dd > 0.08:
                    findings.append((
                        "max_drawdown_critical",
                        "critical",
                        f"Max realized drawdown: {max_dd:.1%}. "
                        "Exceeds 8% threshold — capital at risk. Reduce position sizing.",
                    ))
                elif max_dd > 0.05:
                    findings.append((
                        "max_drawdown_warning",
                        "warning",
                        f"Max realized drawdown: {max_dd:.1%}. "
                        "Approaching 5% drawdown — review position concentration and sizing.",
                    ))

            conn.close()
        except Exception:
            pass

        # ── LLM cost vs daily cap ──
        try:
            cost = _llm_daily_cost(str(self._settings.db_path))
            total = cost.get("total_usd", 0.0)
            if total > _LLM_DAILY_CAP:
                findings.append((
                    "llm_cost_over_cap",
                    "critical",
                    f"LLM cost today ${total:.2f} exceeds ${_LLM_DAILY_CAP:.0f} daily cap "
                    f"({cost.get('pct_cap', 0):.0f}% of cap). Top agent: "
                    + (cost["by_agent"][0]["agent"] if cost.get("by_agent") else "unknown"),
                ))
            elif total > _LLM_DAILY_CAP * 0.75:
                findings.append((
                    "llm_cost_near_cap",
                    "warning",
                    f"LLM cost today ${total:.2f} = {cost.get('pct_cap', 0):.0f}% of "
                    f"${_LLM_DAILY_CAP:.0f} cap. Approaching limit.",
                ))
        except Exception:
            pass

        # ── Unrealized P&L exposure check ──
        if self._pm:
            try:
                positions = self._pm.get_open_positions()
                total_max_loss = sum(p.max_loss_dollars for p in positions)
                account_size = self._settings.account_size
                exposure_pct = total_max_loss / account_size if account_size > 0 else 0
                if exposure_pct > 0.20:
                    findings.append((
                        "buying_power_exposure_high",
                        "warning",
                        f"Open position max-loss exposure: ${total_max_loss:.0f} = {exposure_pct:.0%} of account. "
                        "Buying power buffer dropping — no new positions until exposure < 20%.",
                    ))
            except Exception:
                pass

        return findings

    async def self_heal(self, findings: list[tuple[str, str, str]]) -> None:
        """
        CFO corrective actions — pre-delegated authority:
          daily_loss_limit_near_critical → publish daily_loss_warning(critical) to CRO/CTO
          profit_factor_below_1_30d      → publish profit_factor_low event to CRO
          consecutive_losing_days        → escalate and notify CRO to tighten sizing
          buying_power_exposure_high     → notify CTO to stop new entries
        """
        keys = {k for k, _, _ in findings}

        if "daily_loss_limit_near_critical" in keys:
            self._heal_attempts["daily_crit"] = self._heal_attempts.get("daily_crit", 0) + 1
            if self._heal_attempts["daily_crit"] <= 2:
                await self.notify_peers("daily_loss_warning", {
                    "level":    "critical",
                    "pct_used": 85,
                    "source":   "CFO self_heal",
                })
                logger.warning("CFO self_heal: daily loss critical — published warning to peers")

        elif "daily_loss_limit_near_warning" in keys:
            self._heal_attempts["daily_warn"] = self._heal_attempts.get("daily_warn", 0) + 1
            if self._heal_attempts["daily_warn"] == 1:
                await self.notify_peers("daily_loss_warning", {
                    "level":    "warning",
                    "pct_used": 60,
                    "source":   "CFO self_heal",
                })

        if "profit_factor_below_1_30d" in keys or "profit_factor_critical_30d" in keys:
            self._heal_attempts["pf_low"] = self._heal_attempts.get("pf_low", 0) + 1
            if self._heal_attempts["pf_low"] == 1:
                await self.notify_peers("profit_factor_low", {
                    "level":  "critical" if "profit_factor_critical_30d" in keys else "warning",
                    "source": "CFO self_heal 30d trailing",
                })
                logger.warning("CFO self_heal: profit factor < 1.0 — published to CRO")

        if "consecutive_losing_days" in keys:
            self._heal_attempts["consec_loss"] = self._heal_attempts.get("consec_loss", 0) + 1
            if self._heal_attempts["consec_loss"] == 1:
                await self.notify_peers("size_bias_changed", {
                    "new_bias": "half",
                    "reason":   "CFO: 4+ consecutive losing days — auto-reducing sizing to 50%",
                })
                logger.warning("CFO self_heal: 4+ consecutive losses — published size_bias_changed(half)")

        if "buying_power_exposure_high" in keys:
            await self.notify_peers("size_bias_changed", {
                "new_bias": "none",
                "reason":   "CFO: open position exposure > 20% of account — no new entries",
            })

    async def on_peer_event(self, event_type: str, publisher: str, payload: dict) -> None:
        """CFO receives position_closed events to update gate calibration ledger."""
        if event_type == "position_closed":
            ticker = payload.get("ticker", "?")
            pnl    = float(payload.get("realized_pnl", 0))
            gate   = "low"
            conv   = payload.get("conviction_at_entry")
            if conv:
                if conv >= 70:
                    gate = "high"
                elif conv >= 55:
                    gate = "standard"
            self._latest_intel.setdefault("gate_pnl_ledger", {
                "high": [], "standard": [], "low": []
            })[gate].append(pnl)
            logger.debug("CFO: recorded closed trade %s gate=%s pnl=%.2f", ticker, gate, pnl)

    def get_readiness_tasks(self) -> list[str]:
        tasks = []
        if not self._pm:
            tasks.append("Wire PositionManager to CFO — P&L tracking unavailable")
        s = self._settings
        if not (s.daily_loss_limit_dollars > 0):
            tasks.append("Set daily_loss_limit_dollars — CFO loss monitoring disabled")
        return tasks

    def collect_intelligence(self) -> dict[str, Any]:
        now = datetime.now(tz=ET).isoformat()
        intel: dict[str, Any] = {"timestamp": now, "department": "Finance"}

        intel["account"] = {
            "size":             self._settings.account_size,
            "max_risk_per_trade": self._settings.risk_per_trade_dollars,
            "daily_loss_limit": self._settings.daily_loss_limit_dollars,
            "weekly_loss_limit": self._settings.weekly_loss_limit_dollars,
        }

        if self._pm:
            try:
                positions = self._pm.get_open_positions()
                total_unrealized = sum(p.unrealized_pnl for p in positions)
                total_max_loss = sum(p.max_loss_dollars for p in positions)
                intel["current_pnl"] = {
                    "total_unrealized":    round(total_unrealized, 2),
                    "total_max_loss_exposure": round(total_max_loss, 2),
                    "pct_of_account":      round(total_unrealized / self._settings.account_size * 100, 2),
                }
                # Per-strategy P&L
                by_strategy: dict[str, float] = {}
                by_pillar: dict[str, float] = {}
                for p in positions:
                    by_strategy[p.strategy] = by_strategy.get(p.strategy, 0) + p.unrealized_pnl
                    pillar = getattr(p, "pillar", "unknown") or "unknown"
                    by_pillar[str(pillar)] = by_pillar.get(str(pillar), 0) + p.unrealized_pnl
                intel["pnl_by_strategy"] = {k: round(v, 2) for k, v in by_strategy.items()}
                intel["pnl_by_pillar"]   = {k: round(v, 2) for k, v in by_pillar.items()}

                try:
                    perf = self._pm.get_performance_summary(lookback_days=30)
                    intel["30d_performance"] = perf
                except Exception:
                    pass
            except Exception as exc:
                intel["pnl_error"] = str(exc)

        if self._perf:
            try:
                intel["agent_performance"] = self._perf.get_latest_snapshot()
            except Exception:
                pass

        if self._attributor:
            try:
                # PnlAttributor: pull from generate_report if available, else skip
                report = getattr(self._attributor, "generate_report", None)
                if callable(report):
                    intel["pnl_attribution"] = report(lookback_days=30)
            except Exception:
                pass

        return intel

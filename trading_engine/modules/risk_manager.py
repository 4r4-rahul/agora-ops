"""
Module 7: Wolverine Trading Risk Management System
===================================================
Enforces strict risk rules that prevent catastrophic losses.
Surviving bad days is more important than maximizing good ones.
"""

from datetime import datetime, date, timedelta
from typing import Dict, Any, List, Optional

from ..models import (
    MarketSnapshot, Position, TradeResult, TradeTicket, TradeStatus
)
from ..config import EngineConfig
from ..formatters import header, sub_header, kv, pnl, pct, bar, risk_checklist, C


class RiskManager:
    """
    Wolverine Trading-style risk management system.
    Hard rules, decision trees, and daily risk checklists.
    """

    def __init__(self, config: EngineConfig):
        self.config = config
        self.ac = config.account
        # Track running P&L
        self.daily_pnl: float = 0.0
        self.weekly_pnl: float = 0.0
        self.monthly_pnl: float = 0.0
        self.daily_trades: int = 0
        self.open_positions: List[Position] = []
        self.closed_today: List[TradeResult] = []
        self.consecutive_losses: int = 0
        self.buying_power_used: float = 0.0
        self.peak_equity: float = config.account.account_size

    # ─────────────────────────────────────────────────────────────
    # Pre-Trade Risk Checks
    # ─────────────────────────────────────────────────────────────

    def pre_trade_check(self, ticket: TradeTicket) -> Dict[str, Any]:
        """
        Run all pre-trade risk checks. Returns pass/fail with reasons.
        """
        checks = []

        # 1. Daily loss limit
        daily_limit = self.ac.account_size * self.ac.max_daily_loss_pct
        remaining_daily = daily_limit + self.daily_pnl  # daily_pnl is negative when losing
        checks.append({
            "rule": "Daily Loss Limit",
            "status": "pass" if self.daily_pnl > -daily_limit else "fail",
            "value": f"P&L: {pnl(self.daily_pnl)} | Limit: {pnl(-daily_limit)}",
            "label": "Daily Loss Limit",
        })

        # 2. Weekly loss limit
        weekly_limit = self.ac.account_size * self.ac.max_weekly_loss_pct
        checks.append({
            "rule": "Weekly Loss Limit",
            "status": "pass" if self.weekly_pnl > -weekly_limit else "fail",
            "value": f"P&L: {pnl(self.weekly_pnl)} | Limit: {pnl(-weekly_limit)}",
            "label": "Weekly Loss Limit",
        })

        # 3. Monthly drawdown circuit breaker
        monthly_limit = self.ac.account_size * self.ac.max_monthly_drawdown_pct
        checks.append({
            "rule": "Monthly Circuit Breaker",
            "status": "pass" if self.monthly_pnl > -monthly_limit else "fail",
            "value": f"P&L: {pnl(self.monthly_pnl)} | Limit: {pnl(-monthly_limit)}",
            "label": "Monthly Circuit Breaker",
        })

        # 4. Position size cap
        max_risk = self.ac.account_size * self.ac.max_risk_per_trade_pct
        trade_risk = ticket.total_max_loss
        checks.append({
            "rule": "Position Size Cap",
            "status": "pass" if trade_risk <= max_risk else "warn" if trade_risk <= max_risk * 1.2 else "fail",
            "value": f"Trade Risk: ${trade_risk:,.2f} | Max: ${max_risk:,.2f} ({self.ac.max_risk_per_trade_pct*100:.0f}%)",
            "label": "Position Size Cap",
        })

        # 5. Buying power management
        new_bp = self.buying_power_used + trade_risk
        bp_limit = self.ac.account_size * self.ac.max_buying_power_usage_pct
        checks.append({
            "rule": "Buying Power",
            "status": "pass" if new_bp <= bp_limit else "warn",
            "value": f"Used: ${new_bp:,.2f} | Limit: ${bp_limit:,.2f} ({self.ac.max_buying_power_usage_pct*100:.0f}%)",
            "label": "Buying Power Usage",
        })

        # 6. Correlation check
        corr_risk = self._check_correlation(ticket)
        checks.append({
            "rule": "Correlation",
            "status": "pass" if not corr_risk else "warn",
            "value": corr_risk if corr_risk else "No overlapping directional exposure",
            "label": "Correlation Check",
        })

        # 7. Consecutive loss check
        checks.append({
            "rule": "Consecutive Losses",
            "status": "pass" if self.consecutive_losses < 3 else "warn" if self.consecutive_losses < 5 else "fail",
            "value": f"{self.consecutive_losses} consecutive losses",
            "label": "Loss Streak Check",
        })

        all_pass = all(c["status"] != "fail" for c in checks)
        has_warnings = any(c["status"] == "warn" for c in checks)

        return {
            "approved": all_pass,
            "warnings": has_warnings,
            "checks": checks,
            "recommendation": self._recommendation(checks),
        }

    def _check_correlation(self, ticket: TradeTicket) -> str:
        """Check if new trade overlaps with existing positions."""
        for pos in self.open_positions:
            if pos.ticket.underlying == ticket.underlying:
                if pos.ticket.spread_type == ticket.spread_type:
                    return f"Duplicate exposure: Already have {pos.ticket.strategy} on {ticket.underlying}"
                # Check overlapping strikes
                if (ticket.short_strike and pos.ticket.short_strike and
                    abs(ticket.short_strike - pos.ticket.short_strike) < 10):
                    return f"Overlapping strikes with existing position on {ticket.underlying}"
        return ""

    def _recommendation(self, checks: List[Dict]) -> str:
        fails = [c for c in checks if c["status"] == "fail"]
        warns = [c for c in checks if c["status"] == "warn"]

        if fails:
            return f"❌ TRADE BLOCKED: {', '.join(c['rule'] for c in fails)}"
        if warns:
            return f"⚠️  PROCEED WITH CAUTION: {', '.join(c['rule'] for c in warns)}"
        return "✅ All risk checks passed. Proceed with trade."

    # ─────────────────────────────────────────────────────────────
    # Position Monitoring
    # ─────────────────────────────────────────────────────────────

    def monitor_position(self, position: Position, snap: MarketSnapshot) -> Dict[str, Any]:
        """
        Real-time position monitoring.
        Returns action recommendations.
        """
        ticket = position.ticket
        actions = []

        # 1. Stop-loss check
        if position.current_value >= ticket.stop_loss_price:
            actions.append({
                "action": "CLOSE",
                "reason": f"Stop-loss triggered: spread at ${position.current_value:.2f} >= ${ticket.stop_loss_price:.2f}",
                "urgency": "immediate",
            })

        # 2. Profit target check
        if ticket.credit_per_contract > 0:
            profit_pct = (ticket.credit_per_contract - position.current_value) / ticket.credit_per_contract
            if profit_pct >= self.config.trading.zero_dte_profit_target_pct:
                actions.append({
                    "action": "CLOSE_PROFIT",
                    "reason": f"Profit target reached: {profit_pct*100:.0f}% of max",
                    "urgency": "soon",
                })

        # 3. Time-based exit
        now = datetime.now()
        if ticket.exit_by_time:
            h, m = map(int, ticket.exit_by_time.split(":"))
            exit_time = now.replace(hour=h, minute=m, second=0)
            minutes_left = (exit_time - now).total_seconds() / 60
            if minutes_left <= 0:
                actions.append({
                    "action": "CLOSE",
                    "reason": f"Time exit: past {ticket.exit_by_time}",
                    "urgency": "immediate",
                })
            elif minutes_left <= 15:
                actions.append({
                    "action": "PREPARE_CLOSE",
                    "reason": f"Time exit approaching: {minutes_left:.0f} min remaining",
                    "urgency": "prepare",
                })

        # 4. VIX spike check
        if snap.vix_level > 0 and ticket.vix_at_entry > 0:
            vix_change_pct = (snap.vix_level - ticket.vix_at_entry) / ticket.vix_at_entry * 100
            if vix_change_pct > 20:
                actions.append({
                    "action": "HEDGE_OR_CLOSE",
                    "reason": f"VIX spiked {vix_change_pct:.0f}% since entry ({ticket.vix_at_entry:.1f} → {snap.vix_level:.1f})",
                    "urgency": "soon",
                })

        # 5. Underlying approaching short strike
        price = snap.spx_price
        if ticket.short_strike > 0:
            distance_put = (price - ticket.short_strike) / price * 100
            if distance_put < 0.3:  # Within 0.3% of short put
                actions.append({
                    "action": "ROLL_OR_CLOSE",
                    "reason": f"Price ({price:.0f}) approaching short put ({ticket.short_strike:.0f})",
                    "urgency": "immediate",
                })

        if ticket.short_strike_call > 0:
            distance_call = (ticket.short_strike_call - price) / price * 100
            if distance_call < 0.3:
                actions.append({
                    "action": "ROLL_OR_CLOSE",
                    "reason": f"Price ({price:.0f}) approaching short call ({ticket.short_strike_call:.0f})",
                    "urgency": "immediate",
                })

        return {
            "position": ticket.id,
            "actions": actions,
            "status": "critical" if any(a["urgency"] == "immediate" for a in actions) else
                      "warning" if actions else "ok",
        }

    # ─────────────────────────────────────────────────────────────
    # Roll vs Close Decision Tree
    # ─────────────────────────────────────────────────────────────

    def roll_or_close_decision(self, position: Position, snap: MarketSnapshot) -> Dict[str, Any]:
        """
        Decision tree: roll the position for recovery or cut the loss.
        """
        ticket = position.ticket
        unrealized_loss = position.unrealized_pnl
        credit = ticket.credit_per_contract * ticket.num_contracts * 100

        # Factors
        dte = (ticket.expiration - date.today()).days
        loss_pct = abs(unrealized_loss) / credit if credit > 0 else 0

        decision = {"action": "", "reason": "", "details": ""}

        # Rule 1: If loss exceeds 3x credit, always close
        if loss_pct > 3:
            decision = {
                "action": "CLOSE_NOW",
                "reason": "Loss exceeds 3x credit collected",
                "details": "No recovery possible. Cut the loss immediately.",
            }
        # Rule 2: If < 1 DTE and losing, close (no time for recovery)
        elif dte <= 0 and unrealized_loss < 0:
            decision = {
                "action": "CLOSE_NOW",
                "reason": "0DTE with unrealized loss — gamma risk too high to hold",
                "details": "Close and move to next trade.",
            }
        # Rule 3: If >2 DTE and loss < 2x credit, consider rolling
        elif dte >= 2 and loss_pct < 2:
            decision = {
                "action": "ROLL",
                "reason": f"{dte} DTE remaining, loss manageable at {loss_pct:.1f}x credit",
                "details": "Roll to next expiration at same or wider strikes for credit.",
            }
        # Rule 4: If VIX spiked, close rather than roll (regime shift)
        elif snap.vix_level > 30:
            decision = {
                "action": "CLOSE_NOW",
                "reason": f"VIX at {snap.vix_level:.1f} — regime shift detected",
                "details": "Close all premium-selling positions in high-VIX environment.",
            }
        else:
            decision = {
                "action": "CLOSE_NOW",
                "reason": "Default: take the loss and reset",
                "details": "No clear edge in rolling. Fresh trade has better odds.",
            }

        return decision

    # ─────────────────────────────────────────────────────────────
    # Recovery Protocol
    # ─────────────────────────────────────────────────────────────

    def recovery_protocol(self) -> Dict[str, Any]:
        """After a max loss day, determine recovery approach."""
        if self.daily_pnl >= 0:
            return {"in_recovery": False, "message": "No recovery needed — positive day."}

        daily_limit = self.ac.account_size * self.ac.max_daily_loss_pct
        loss_severity = abs(self.daily_pnl) / daily_limit

        if loss_severity > 0.8:
            return {
                "in_recovery": True,
                "severity": "max_loss",
                "size_reduction": 0.50,
                "duration_days": 3,
                "rules": [
                    "Reduce position size by 50% for next 3 trading days",
                    "Only trade put credit spreads (no iron condors)",
                    "Use 0.08 delta (wider) instead of 0.12",
                    "Close at 30% profit instead of 50%",
                    "No 0DTE — weekly expirations only",
                    "Maximum 2 trades per day",
                ],
            }
        elif loss_severity > 0.5:
            return {
                "in_recovery": True,
                "severity": "moderate_loss",
                "size_reduction": 0.25,
                "duration_days": 2,
                "rules": [
                    "Reduce position size by 25% for next 2 trading days",
                    "Stick to highest-conviction setups only",
                    "Close at 40% profit instead of 50%",
                ],
            }
        return {
            "in_recovery": True,
            "severity": "minor_loss",
            "size_reduction": 0,
            "duration_days": 0,
            "rules": ["Normal trading tomorrow. Minor loss within parameters."],
        }

    # ─────────────────────────────────────────────────────────────
    # VIX Spike Protocol
    # ─────────────────────────────────────────────────────────────

    def vix_spike_protocol(self, snap: MarketSnapshot) -> Dict[str, Any]:
        """Actions when VIX jumps 20%+ in a single day."""
        vix_jump = snap.vix_1d_change
        vix_jump_pct = (vix_jump / (snap.vix_level - vix_jump)) * 100 if (snap.vix_level - vix_jump) > 0 else 0

        if abs(vix_jump_pct) < 20:
            return {"triggered": False, "message": "VIX within normal range."}

        return {
            "triggered": True,
            "vix_jump_pct": round(vix_jump_pct, 1),
            "actions": [
                "IMMEDIATE: Close all 0DTE positions",
                "IMMEDIATE: Close any position where loss > 1.5x credit",
                "HEDGE: Buy VIX calls or SPX puts as portfolio hedge",
                "WIDEN: If holding multi-day positions, widen stops to 3x credit",
                "PAUSE: No new short premium positions today",
                "REASSESS: Run regime classifier tomorrow morning",
            ],
        }

    # ─────────────────────────────────────────────────────────────
    # Tail Risk Protection
    # ─────────────────────────────────────────────────────────────

    def tail_risk_hedges(self, snap: MarketSnapshot) -> Dict[str, Any]:
        """Recommend tail risk hedges for 3+ sigma moves."""
        S = snap.spx_price
        em = snap.expected_move_1d

        return {
            "3_sigma_down": round(S - em * 3, 0),
            "hedges": [
                {
                    "strategy": "Long SPX put at 3σ below",
                    "strike": round(S - em * 3, 0),
                    "cost_estimate": "~$0.10-0.30 per contract",
                    "purpose": "Catastrophic downside protection",
                },
                {
                    "strategy": "VIX call spread",
                    "description": "Buy VIX 25 call, sell VIX 40 call (next month)",
                    "cost_estimate": "~$1.00-2.00 per spread",
                    "purpose": "Portfolio hedge that profits in panic",
                },
                {
                    "strategy": "SPY put ratio spread",
                    "description": "Buy 1 ATM put, sell 2 OTM puts",
                    "cost_estimate": "Near zero cost if structured correctly",
                    "purpose": "Moderate downside protection, free or cheap",
                },
            ],
        }

    # ─────────────────────────────────────────────────────────────
    # Daily Risk Checklist
    # ─────────────────────────────────────────────────────────────

    def daily_checklist(self, snap: MarketSnapshot) -> List[Dict[str, Any]]:
        """Generate the daily pre-trading risk checklist."""
        daily_limit = self.ac.account_size * self.ac.max_daily_loss_pct
        weekly_limit = self.ac.account_size * self.ac.max_weekly_loss_pct
        monthly_limit = self.ac.account_size * self.ac.max_monthly_drawdown_pct

        items = [
            {
                "label": "Daily P&L vs Limit",
                "value": f"{pnl(self.daily_pnl)} / {pnl(-daily_limit)} limit",
                "status": "pass" if self.daily_pnl > -daily_limit else "fail",
            },
            {
                "label": "Weekly P&L vs Limit",
                "value": f"{pnl(self.weekly_pnl)} / {pnl(-weekly_limit)} limit",
                "status": "pass" if self.weekly_pnl > -weekly_limit else "fail",
            },
            {
                "label": "Monthly Drawdown",
                "value": f"{pnl(self.monthly_pnl)} / {pnl(-monthly_limit)} circuit breaker",
                "status": "pass" if self.monthly_pnl > -monthly_limit else "fail",
            },
            {
                "label": "Buying Power Used",
                "value": f"${self.buying_power_used:,.0f} / ${self.ac.account_size * self.ac.max_buying_power_usage_pct:,.0f} (50%)",
                "status": "pass" if self.buying_power_used < self.ac.account_size * self.ac.max_buying_power_usage_pct else "warn",
            },
            {
                "label": "Open Positions",
                "value": f"{len(self.open_positions)} positions",
                "status": "pass" if len(self.open_positions) < 10 else "warn",
            },
            {
                "label": "VIX Level",
                "value": f"{snap.vix_level:.2f}",
                "status": "pass" if snap.vix_level < 25 else "warn" if snap.vix_level < 35 else "fail",
            },
            {
                "label": "Consecutive Losses",
                "value": f"{self.consecutive_losses}",
                "status": "pass" if self.consecutive_losses < 3 else "warn" if self.consecutive_losses < 5 else "fail",
            },
            {
                "label": "Event Risk",
                "value": "Fed Day" if snap.is_fed_day else "CPI Day" if snap.is_cpi_day else "None",
                "status": "fail" if snap.is_fed_day else "warn" if snap.is_cpi_day else "pass",
            },
        ]

        return items

    # ─────────────────────────────────────────────────────────────
    # P&L Tracking
    # ─────────────────────────────────────────────────────────────

    def record_trade(self, result: TradeResult):
        """Record a completed trade."""
        self.daily_pnl += result.realized_pnl
        self.weekly_pnl += result.realized_pnl
        self.monthly_pnl += result.realized_pnl
        self.daily_trades += 1
        self.closed_today.append(result)

        if result.realized_pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

        current_equity = self.config.account.account_size + self.monthly_pnl
        self.peak_equity = max(self.peak_equity, current_equity)

    def reset_daily(self):
        """Reset daily counters."""
        self.daily_pnl = 0.0
        self.daily_trades = 0
        self.closed_today = []

    def reset_weekly(self):
        """Reset weekly counters."""
        self.weekly_pnl = 0.0

    # ── Report Output ──────────────────────────────────────────

    def format_report(self, snap: MarketSnapshot) -> str:
        lines = []
        lines.append(header(
            "WOLVERINE TRADING RISK MANAGEMENT SYSTEM",
            f"Account: ${self.ac.account_size:,.2f} | {datetime.now():%Y-%m-%d %H:%M}"
        ))

        # Risk checklist
        checklist = self.daily_checklist(snap)
        lines.append(risk_checklist(checklist))

        # Hard rules
        lines.append(sub_header("HARD RULES"))
        rules = [
            f"Daily Loss Limit:    ${self.ac.account_size * self.ac.max_daily_loss_pct:,.2f} ({self.ac.max_daily_loss_pct*100:.0f}% of account)",
            f"Weekly Loss Limit:   ${self.ac.account_size * self.ac.max_weekly_loss_pct:,.2f} ({self.ac.max_weekly_loss_pct*100:.0f}% of account)",
            f"Monthly Drawdown:    ${self.ac.account_size * self.ac.max_monthly_drawdown_pct:,.2f} ({self.ac.max_monthly_drawdown_pct*100:.0f}% of account)",
            f"Max Risk/Trade:      ${self.ac.account_size * self.ac.max_risk_per_trade_pct:,.2f} ({self.ac.max_risk_per_trade_pct*100:.0f}% of account)",
            f"Max Buying Power:    {self.ac.max_buying_power_usage_pct*100:.0f}% of total",
        ]
        for r in rules:
            lines.append(f"  📌 {r}")

        # Recovery protocol
        recovery = self.recovery_protocol()
        if recovery["in_recovery"]:
            lines.append(sub_header("⚠️ RECOVERY MODE ACTIVE"))
            lines.append(kv("Severity", recovery["severity"]))
            lines.append(kv("Size Reduction", f"{recovery['size_reduction']*100:.0f}%"))
            for rule in recovery["rules"]:
                lines.append(f"  → {rule}")

        # VIX protocol
        vix_prot = self.vix_spike_protocol(snap)
        if vix_prot["triggered"]:
            lines.append(sub_header("🚨 VIX SPIKE PROTOCOL ACTIVATED"))
            for action in vix_prot["actions"]:
                lines.append(f"  🔴 {action}")

        # Tail risk
        lines.append(sub_header("TAIL RISK PROTECTION"))
        tail = self.tail_risk_hedges(snap)
        lines.append(kv("3σ Down Level", f"{tail['3_sigma_down']:,.0f}"))
        for hedge in tail["hedges"]:
            lines.append(f"\n  {C.BOLD}{hedge['strategy']}{C.RESET}")
            if "description" in hedge:
                lines.append(f"    {hedge['description']}")
            lines.append(f"    Cost: {hedge['cost_estimate']}")
            lines.append(f"    Purpose: {hedge['purpose']}")

        # Decision tree summary
        lines.append(sub_header("ROLL vs CLOSE DECISION TREE"))
        lines.append("  Loss > 3x credit        → CLOSE immediately")
        lines.append("  0DTE + losing            → CLOSE (gamma too high)")
        lines.append("  >2 DTE + loss < 2x       → ROLL to next expiration")
        lines.append("  VIX > 30                 → CLOSE all short premium")
        lines.append("  Otherwise                → CLOSE and reset")

        lines.append(f"\n{C.CYAN}{'═' * 78}{C.RESET}\n")
        return "\n".join(lines)

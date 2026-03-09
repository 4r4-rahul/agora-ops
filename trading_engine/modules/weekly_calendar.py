"""
Module 9: Peak6 SPY Weekly Income Calendar
===========================================
Systematic weekly options income schedule — exact daily actions Monday through Friday.
"""

from datetime import datetime, date, timedelta
from typing import Dict, Any, List

from ..models import (
    MarketSnapshot, TradeTicket, TradeResult, SpreadType, DayOfWeek
)
from ..config import EngineConfig
from ..black_scholes import strike_at_delta, expected_move
from ..formatters import header, sub_header, kv, pnl, table, C


class WeeklyCalendar:
    """
    Peak6-style weekly trading calendar.
    Fixed schedule for opening, managing, and closing weekly credit spreads.
    """

    def __init__(self, config: EngineConfig):
        self.config = config
        self.weekly_trades: List[TradeResult] = []
        self.consecutive_wins: int = 0
        self.weekly_pnl: float = 0.0

    def get_daily_plan(self, snap: MarketSnapshot, day: DayOfWeek = None) -> Dict[str, Any]:
        """Get the action plan for today."""
        if day is None:
            weekday = date.today().weekday()
            # Saturday/Sunday → default to Monday plan
            if weekday > 4:
                day = DayOfWeek.MONDAY
            else:
                day = DayOfWeek(weekday)

        plans = {
            DayOfWeek.MONDAY: self._monday_plan,
            DayOfWeek.TUESDAY: self._tuesday_plan,
            DayOfWeek.WEDNESDAY: self._wednesday_plan,
            DayOfWeek.THURSDAY: self._thursday_plan,
            DayOfWeek.FRIDAY: self._friday_plan,
        }

        plan_func = plans.get(day, self._monday_plan)
        return plan_func(snap)

    def _monday_plan(self, snap: MarketSnapshot) -> Dict[str, Any]:
        """Monday: Analyze, set range, open positions."""
        S = snap.spx_price
        iv = snap.vix_level / 100 if snap.vix_level > 1 else 0.18
        r = 0.045
        T = 5 / 252  # Friday expiration

        em = expected_move(S, iv, T)
        target_delta = self.config.trading.weekly_short_delta

        put_strike = strike_at_delta(S, T, r, iv, target_delta, "put")
        call_strike = strike_at_delta(S, T, r, iv, target_delta, "call")

        # Position sizing
        size_mult = 1.0
        if self.consecutive_wins >= 4:
            size_mult = 1.25  # Increase after 4 consecutive wins
        elif self.consecutive_wins < 0:
            size_mult = 0.50  # Previous week was a loss

        acct = self.config.account.account_size
        max_risk = acct * self.config.account.max_risk_per_trade_pct * size_mult
        width = self.config.trading.spread_width_min
        max_loss_per = width * 100  # Approximate
        num_contracts = max(1, int(max_risk / max_loss_per))

        friday = date.today() + timedelta(days=(4 - date.today().weekday()) % 7)

        return {
            "day": "MONDAY",
            "actions": [
                {
                    "time": "8:00 AM",
                    "action": "Pre-market analysis",
                    "details": [
                        f"VIX: {snap.vix_level:.2f}",
                        f"Weekly Expected Range: {S - em:.0f} — {S + em:.0f} (±{em:.1f})",
                        f"Events this week: {', '.join(snap.economic_events) or 'None major'}",
                    ],
                },
                {
                    "time": "9:45 - 10:30 AM",
                    "action": "OPEN weekly put credit spread or iron condor",
                    "details": [
                        f"Expiration: Friday {friday}",
                        f"Short put: {put_strike} (Δ ≈ {target_delta})",
                        f"Long put: {put_strike - width}",
                        f"Short call: {call_strike} (Δ ≈ {target_delta})",
                        f"Long call: {call_strike + width}",
                        f"Contracts: {num_contracts} (size mult: {size_mult:.2f}x)",
                        f"Target credit: $0.50 - $1.50 per spread",
                    ],
                },
                {
                    "time": "10:30 AM",
                    "action": "Set alerts",
                    "details": [
                        f"Set alert at {put_strike + 5:.0f} (put side warning)",
                        f"Set alert at {call_strike - 5:.0f} (call side warning)",
                        "Set stop-loss orders at 2x credit received",
                    ],
                },
            ],
            "strikes": {
                "put_short": put_strike,
                "put_long": put_strike - width,
                "call_short": call_strike,
                "call_long": call_strike + width,
            },
            "num_contracts": num_contracts,
            "size_multiplier": size_mult,
            "weekly_range": (round(S - em, 0), round(S + em, 0)),
        }

    def _tuesday_plan(self, snap: MarketSnapshot) -> Dict[str, Any]:
        """Tuesday: Check positions, consider early close at 30%+ profit."""
        return {
            "day": "TUESDAY",
            "actions": [
                {
                    "time": "10:00 AM",
                    "action": "MANAGEMENT CHECK",
                    "details": [
                        "Check all open weekly positions",
                        "If any spread at 30%+ profit, consider closing to free capital",
                        "If VIX dropped 10%+ since Monday, close iron condor profitably",
                        f"Current VIX: {snap.vix_level:.2f}",
                    ],
                },
                {
                    "time": "Throughout day",
                    "action": "Monitor stop levels",
                    "details": [
                        "Ensure stop-loss orders are in place",
                        "Watch for any approaching short strikes",
                        "If index within 5 points of short strike, prepare adjustment",
                    ],
                },
            ],
            "checklist": [
                "Are all stops in place?",
                "Is any position at 30%+ profit?",
                "Did VIX change significantly?",
                "Any mid-week economic surprises?",
            ],
        }

    def _wednesday_plan(self, snap: MarketSnapshot) -> Dict[str, Any]:
        """Wednesday: Midweek review, prepare adjustments."""
        return {
            "day": "WEDNESDAY",
            "actions": [
                {
                    "time": "10:00 AM",
                    "action": "MIDWEEK REVIEW",
                    "details": [
                        "Reassess market direction since Monday",
                        "Check if weekly range estimate is still valid",
                        f"SPX at {snap.spx_price:,.2f} — still within expected range?",
                    ],
                },
                {
                    "time": "If threatened",
                    "action": "PREPARE ADJUSTMENT",
                    "details": [
                        "If one side is threatened (within 30% of short strike):",
                        "  Option A: Close threatened side, keep winning side",
                        "  Option B: Roll threatened side to wider strikes",
                        "  Option C: Buy a debit spread to lock in loss and add hedge",
                    ],
                },
                {
                    "time": "2:00 PM",
                    "action": "Add 0DTE overlay (optional)",
                    "details": [
                        "If weekly position is comfortable, add small 0DTE credit spread",
                        "for additional daily income",
                        "Keep size at 25% of weekly position",
                    ],
                },
            ],
        }

    def _thursday_plan(self, snap: MarketSnapshot) -> Dict[str, Any]:
        """Thursday: Theta accelerates, decide hold vs close."""
        return {
            "day": "THURSDAY",
            "actions": [
                {
                    "time": "10:00 AM",
                    "action": "THETA ACCELERATION CHECK",
                    "details": [
                        "Theta decay accelerates sharply on Thursday for Friday expiration",
                        "If position at 50%+ profit: CLOSE for the week",
                        "If position at 65%+ profit: Definitely close — don't get greedy",
                        "If position is barely profitable: Hold for one more day of theta",
                    ],
                },
                {
                    "time": "Decision Point",
                    "action": "HOLD OR CLOSE",
                    "details": [
                        "Close at 50%+ profit → Book the win, trade again next week",
                        "Hold if < 30% profit → One more day of decay could help",
                        "Close if losing → Better to reset for next Monday",
                    ],
                },
            ],
        }

    def _friday_plan(self, snap: MarketSnapshot) -> Dict[str, Any]:
        """Friday: Close positions, review week, prepare Monday watchlist."""
        return {
            "day": "FRIDAY",
            "actions": [
                {
                    "time": "9:30 - 10:00 AM",
                    "action": "FINAL POSITION CHECK",
                    "details": [
                        "Review all open weekly positions",
                        "Calculate distance to short strikes vs pin risk",
                    ],
                },
                {
                    "time": "11:00 AM",
                    "action": "CLOSE ALL POSITIONS",
                    "details": [
                        "Close all weekly positions by 11:00 AM",
                        "Do NOT let spreads expire to avoid pin risk and assignment",
                        "Exception: if spread is deep OTM (> 2% away), let expire worthless",
                    ],
                },
                {
                    "time": "1:00 PM",
                    "action": "WEEKLY REVIEW",
                    "details": [
                        "Log all trades: entry price, exit price, P&L",
                        "Calculate weekly win rate and average P&L",
                        "Identify what went right and wrong",
                    ],
                },
                {
                    "time": "2:00 PM",
                    "action": "NEXT WEEK PREP",
                    "details": [
                        "Check next week's economic calendar",
                        "Note any major earnings",
                        "Set Monday morning analysis alerts",
                    ],
                },
                {
                    "time": "3:00 PM (Optional)",
                    "action": "WEEKEND THETA CAPTURE",
                    "details": [
                        "Sell next Monday expiration to capture 3 days of weekend theta",
                        "Use very wide strikes (< 0.05 delta)",
                        "Small size only — weekend gap risk is real",
                    ],
                },
            ],
        }

    # ─────────────────────────────────────────────────────────────
    # Position Sizing Cycle
    # ─────────────────────────────────────────────────────────────

    def get_position_size(self) -> Dict[str, Any]:
        """Calculate position size based on recent performance."""
        base_risk_pct = self.config.account.max_risk_per_trade_pct
        acct = self.config.account.account_size

        if self.consecutive_wins >= 4:
            mult = 1.25
            note = "4+ consecutive winning weeks → size up 25%"
        elif self.consecutive_wins == 0:
            mult = 0.50
            note = "Coming off a loss week → size down 50%"
        else:
            mult = 1.0
            note = "Standard sizing"

        risk_per_trade = acct * base_risk_pct * mult

        return {
            "base_risk_pct": f"{base_risk_pct*100:.1f}%",
            "multiplier": mult,
            "effective_risk": round(risk_per_trade, 2),
            "note": note,
            "consecutive_wins": self.consecutive_wins,
        }

    # ─────────────────────────────────────────────────────────────
    # Monthly Reconciliation
    # ─────────────────────────────────────────────────────────────

    def monthly_reconciliation(self, monthly_trades: List[TradeResult]) -> Dict[str, Any]:
        """Review all 4 weekly cycles."""
        if not monthly_trades:
            return {"message": "No trades to reconcile."}

        wins = [t for t in monthly_trades if t.realized_pnl > 0]
        losses = [t for t in monthly_trades if t.realized_pnl <= 0]
        total_pnl = sum(t.realized_pnl for t in monthly_trades)
        win_rate = len(wins) / len(monthly_trades) * 100 if monthly_trades else 0
        avg_win = sum(t.realized_pnl for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t.realized_pnl for t in losses) / len(losses) if losses else 0

        # Delta adjustment recommendation
        if win_rate > 90:
            delta_rec = "Consider tightening to 0.15 delta for more premium (current may be too conservative)"
        elif win_rate < 75:
            delta_rec = "Widen to 0.08-0.10 delta for higher win rate (current strikes too aggressive)"
        else:
            delta_rec = "Current delta level is optimal — maintain"

        return {
            "total_trades": len(monthly_trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(win_rate, 1),
            "total_pnl": round(total_pnl, 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "profit_factor": round(abs(sum(t.realized_pnl for t in wins) / sum(t.realized_pnl for t in losses)), 2) if losses and sum(t.realized_pnl for t in losses) != 0 else float('inf'),
            "delta_adjustment": delta_rec,
        }

    # ── Report Output ──────────────────────────────────────────

    def format_report(self, plan: Dict, snap: MarketSnapshot) -> str:
        lines = []
        day_name = plan.get("day", "TODAY")
        lines.append(header(
            f"PEAK6 WEEKLY INCOME CALENDAR — {day_name}",
            f"{date.today():%A, %B %d, %Y} | SPX {snap.spx_price:,.2f} | VIX {snap.vix_level:.2f}"
        ))

        for action in plan.get("actions", []):
            lines.append(sub_header(f"{action['time']} — {action['action']}"))
            for detail in action.get("details", []):
                lines.append(f"  • {detail}")

        if "strikes" in plan:
            s = plan["strikes"]
            lines.append(sub_header("THIS WEEK'S STRIKES"))
            lines.append(kv("Short Put", f"{s['put_short']}", C.RED))
            lines.append(kv("Long Put", f"{s['put_long']}", C.GREEN))
            lines.append(kv("Short Call", f"{s['call_short']}", C.RED))
            lines.append(kv("Long Call", f"{s['call_long']}", C.GREEN))
            lines.append(kv("Contracts", plan.get("num_contracts", 1)))
            rng = plan.get("weekly_range", (0, 0))
            lines.append(kv("Weekly Range", f"{rng[0]:.0f} — {rng[1]:.0f}"))

        if "checklist" in plan:
            lines.append(sub_header("CHECKLIST"))
            for item in plan["checklist"]:
                lines.append(f"  ☐ {item}")

        # Size info
        size = self.get_position_size()
        lines.append(sub_header("POSITION SIZING"))
        lines.append(kv("Base Risk", size["base_risk_pct"]))
        lines.append(kv("Multiplier", f"{size['multiplier']:.2f}x"))
        lines.append(kv("Effective Risk", f"${size['effective_risk']:,.2f}"))
        lines.append(kv("Streak", f"{size['consecutive_wins']} wins"))
        lines.append(f"  {C.DIM}{size['note']}{C.RESET}")

        # Trade journal template
        lines.append(sub_header("TRADE JOURNAL TEMPLATE"))
        lines.append("  Date: ___________")
        lines.append("  Strategy: Weekly Put CS / Call CS / Iron Condor")
        lines.append("  Strikes: Short ____ / Long ____ | Short ____ / Long ____")
        lines.append("  Credit: $_____ per spread × _____ contracts = $_____ total")
        lines.append("  Entry Time: _____ | Exit Time: _____")
        lines.append("  Exit Reason: Profit Target / Stop Loss / Expiration / Time")
        lines.append("  P&L: $_____ | Notes: _________________________")

        lines.append(f"\n{C.CYAN}{'═' * 78}{C.RESET}\n")
        return "\n".join(lines)

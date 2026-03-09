"""
Module 12: Citadel Monthly Performance Dashboard
=================================================
Tracks every metric that matters for options income strategies.
You can't improve what you don't measure.
"""

import json
import os
from datetime import datetime, date, timedelta
from typing import Dict, Any, List, Optional

from ..models import TradeResult, PerformanceReport, SpreadType
from ..config import EngineConfig
from ..formatters import header, sub_header, kv, pnl, pct, bar, table, C


class PerformanceDashboard:
    """
    Citadel-style monthly performance tracking.
    Comprehensive metrics, equity curve, and strategy attribution.
    """

    def __init__(self, config: EngineConfig):
        self.config = config
        self.trades: List[TradeResult] = []

    def add_trade(self, trade: TradeResult):
        """Record a completed trade."""
        self.trades.append(trade)
        self._save_journal()

    def generate_report(self, period_start: Optional[date] = None,
                        period_end: Optional[date] = None) -> PerformanceReport:
        """Generate comprehensive monthly performance report."""
        if period_start is None:
            period_start = date.today().replace(day=1)
        if period_end is None:
            period_end = date.today()

        # Filter trades in period
        period_trades = [
            t for t in self.trades
            if period_start <= t.exit_time.date() <= period_end
        ]

        report = PerformanceReport(period_start=period_start, period_end=period_end)
        report.total_trades = len(period_trades)

        if not period_trades:
            return report

        # Basic metrics
        winners = [t for t in period_trades if t.realized_pnl > 0]
        losers = [t for t in period_trades if t.realized_pnl <= 0]

        report.winning_trades = len(winners)
        report.losing_trades = len(losers)
        report.win_rate = len(winners) / len(period_trades) * 100

        report.total_premium_collected = sum(t.entry_credit for t in period_trades)
        report.total_realized_pnl = sum(t.realized_pnl for t in period_trades)

        report.avg_winner = sum(t.realized_pnl for t in winners) / len(winners) if winners else 0
        report.avg_loser = sum(t.realized_pnl for t in losers) / len(losers) if losers else 0

        # Profit factor
        total_wins = sum(t.realized_pnl for t in winners)
        total_losses = abs(sum(t.realized_pnl for t in losers))
        report.profit_factor = total_wins / total_losses if total_losses > 0 else float('inf')

        # Max drawdown
        report.max_drawdown, report.equity_curve = self._calculate_drawdown(period_trades)

        # Sharpe estimate
        report.sharpe_estimate = self._estimate_sharpe(period_trades)

        # Theta harvested vs realized
        report.theta_available = sum(t.entry_credit for t in period_trades)
        report.theta_captured = report.total_realized_pnl

        # Best and worst trades
        if period_trades:
            report.best_trade = max(period_trades, key=lambda t: t.realized_pnl)
            report.worst_trade = min(period_trades, key=lambda t: t.realized_pnl)

        # Strategy breakdown
        report.strategy_breakdown = self._strategy_breakdown(period_trades)

        # Daily P&L
        report.daily_pnl = self._daily_pnl(period_trades)

        return report

    def _calculate_drawdown(self, trades: List[TradeResult]):
        """Calculate max drawdown and equity curve."""
        balance = self.config.account.account_size
        peak = balance
        max_dd = 0
        curve = [{"date": str(trades[0].entry_time.date()) if trades else str(date.today()),
                  "balance": balance}]

        for trade in sorted(trades, key=lambda t: t.exit_time):
            balance += trade.realized_pnl
            peak = max(peak, balance)
            dd = (peak - balance) / peak * 100 if peak > 0 else 0
            max_dd = max(max_dd, dd)
            curve.append({
                "date": str(trade.exit_time.date()),
                "balance": round(balance, 2),
                "drawdown": round(dd, 2),
            })

        return round(max_dd, 2), curve

    def _estimate_sharpe(self, trades: List[TradeResult]) -> float:
        """Estimate Sharpe ratio from trade results."""
        if len(trades) < 5:
            return 0.0

        daily_returns = self._daily_pnl(trades)
        if not daily_returns:
            return 0.0

        returns = [d["pnl"] for d in daily_returns]
        avg = sum(returns) / len(returns)
        variance = sum((r - avg) ** 2 for r in returns) / len(returns)
        std = variance ** 0.5

        if std == 0:
            return 0.0

        # Annualize: sqrt(252) * daily Sharpe
        daily_sharpe = avg / std
        return round(daily_sharpe * (252 ** 0.5), 2)

    def _daily_pnl(self, trades: List[TradeResult]) -> List[Dict]:
        """Aggregate P&L by day."""
        daily = {}
        for t in trades:
            day_str = str(t.exit_time.date())
            if day_str not in daily:
                daily[day_str] = {"date": day_str, "pnl": 0, "trades": 0}
            daily[day_str]["pnl"] += t.realized_pnl
            daily[day_str]["trades"] += 1

        return sorted(daily.values(), key=lambda d: d["date"])

    def _strategy_breakdown(self, trades: List[TradeResult]) -> Dict[str, Dict]:
        """Break down P&L by strategy type."""
        breakdown = {}
        for t in trades:
            strat = t.strategy or "Unknown"
            if strat not in breakdown:
                breakdown[strat] = {
                    "trades": 0, "wins": 0, "losses": 0,
                    "total_pnl": 0, "premium_collected": 0,
                }
            breakdown[strat]["trades"] += 1
            breakdown[strat]["total_pnl"] += t.realized_pnl
            breakdown[strat]["premium_collected"] += t.entry_credit
            if t.realized_pnl > 0:
                breakdown[strat]["wins"] += 1
            else:
                breakdown[strat]["losses"] += 1

        for strat in breakdown:
            b = breakdown[strat]
            b["win_rate"] = round(b["wins"] / b["trades"] * 100, 1) if b["trades"] else 0
            b["total_pnl"] = round(b["total_pnl"], 2)

        return breakdown

    def _save_journal(self):
        """Save trade journal to disk."""
        journal_path = self.config.trade_journal_path
        os.makedirs(os.path.dirname(journal_path), exist_ok=True)

        data = []
        for t in self.trades:
            data.append({
                "entry_time": str(t.entry_time),
                "exit_time": str(t.exit_time),
                "strategy": t.strategy,
                "entry_credit": t.entry_credit,
                "exit_debit": t.exit_debit,
                "realized_pnl": t.realized_pnl,
                "pnl_per_contract": t.pnl_per_contract,
                "holding_time_minutes": t.holding_time_minutes,
                "exit_reason": t.exit_reason,
                "notes": t.notes,
            })

        try:
            with open(journal_path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    def load_journal(self):
        """Load trade journal from disk."""
        journal_path = self.config.trade_journal_path
        if not os.path.exists(journal_path):
            return

        try:
            with open(journal_path, "r") as f:
                data = json.load(f)

            for d in data:
                t = TradeResult(
                    entry_time=datetime.fromisoformat(d.get("entry_time", str(datetime.now()))),
                    exit_time=datetime.fromisoformat(d.get("exit_time", str(datetime.now()))),
                    strategy=d.get("strategy", ""),
                    entry_credit=d.get("entry_credit", 0),
                    exit_debit=d.get("exit_debit", 0),
                    realized_pnl=d.get("realized_pnl", 0),
                    pnl_per_contract=d.get("pnl_per_contract", 0),
                    holding_time_minutes=d.get("holding_time_minutes", 0),
                    exit_reason=d.get("exit_reason", ""),
                    notes=d.get("notes", ""),
                )
                self.trades.append(t)
        except Exception:
            pass

    # ── Report Output ──────────────────────────────────────────

    def format_report(self, report: PerformanceReport) -> str:
        lines = []
        lines.append(header(
            "CITADEL MONTHLY PERFORMANCE DASHBOARD",
            f"{report.period_start} to {report.period_end}"
        ))

        if report.total_trades == 0:
            lines.append(f"\n  {C.DIM}No trades in this period. Start trading to generate metrics.{C.RESET}")
            lines.append(f"\n{C.CYAN}{'═' * 78}{C.RESET}\n")
            return "\n".join(lines)

        # Metrics dashboard
        lines.append(sub_header("METRICS DASHBOARD"))
        lines.append(kv("Total Trades", report.total_trades))
        lines.append(kv("Win Rate",
                         f"{report.win_rate:.1f}% ({report.winning_trades}W / {report.losing_trades}L)",
                         C.GREEN if report.win_rate >= 75 else C.YELLOW if report.win_rate >= 60 else C.RED))
        lines.append(kv("Total Premium Collected", f"${report.total_premium_collected:,.2f}"))
        lines.append(kv("Total Realized P&L", pnl(report.total_realized_pnl),
                         C.GREEN if report.total_realized_pnl > 0 else C.RED))
        lines.append(kv("Average Winner", pnl(report.avg_winner), C.GREEN))
        lines.append(kv("Average Loser", pnl(report.avg_loser), C.RED))
        lines.append(kv("Avg Win / Avg Loss Ratio",
                         f"{abs(report.avg_winner / report.avg_loser):.2f}" if report.avg_loser else "∞",
                         C.GREEN if report.avg_winner > abs(report.avg_loser) else C.YELLOW))
        lines.append(kv("Profit Factor", f"{report.profit_factor:.2f}",
                         C.GREEN if report.profit_factor > 1.5 else C.YELLOW if report.profit_factor > 1 else C.RED))
        lines.append(kv("Max Drawdown", f"{report.max_drawdown:.2f}%",
                         C.GREEN if report.max_drawdown < 5 else C.YELLOW if report.max_drawdown < 10 else C.RED))
        lines.append(kv("Sharpe Ratio (est)", f"{report.sharpe_estimate:.2f}",
                         C.GREEN if report.sharpe_estimate > 1.5 else C.YELLOW if report.sharpe_estimate > 0.5 else C.RED))

        # Theta efficiency
        if report.theta_available > 0:
            efficiency = report.theta_captured / report.theta_available * 100
            lines.append(kv("Theta Capture Rate",
                             f"{efficiency:.1f}% (${report.theta_captured:,.2f} of ${report.theta_available:,.2f})",
                             C.GREEN if efficiency > 70 else C.YELLOW))

        # Best and worst trades
        lines.append(sub_header("BEST AND WORST TRADES"))
        if report.best_trade:
            bt = report.best_trade
            lines.append(f"  {C.GREEN}🏆 BEST:{C.RESET} {bt.strategy} — {pnl(bt.realized_pnl)} "
                          f"({bt.exit_reason}) [{bt.entry_time.date()}]")
            if bt.notes:
                lines.append(f"     Notes: {bt.notes}")
        if report.worst_trade:
            wt = report.worst_trade
            lines.append(f"  {C.RED}💀 WORST:{C.RESET} {wt.strategy} — {pnl(wt.realized_pnl)} "
                          f"({wt.exit_reason}) [{wt.entry_time.date()}]")
            if wt.notes:
                lines.append(f"     Notes: {wt.notes}")

        # Strategy breakdown
        if report.strategy_breakdown:
            lines.append(sub_header("STRATEGY-LEVEL ATTRIBUTION"))
            hd = ["Strategy", "Trades", "WR%", "P&L", "Premium"]
            rows = []
            for strat, data in sorted(report.strategy_breakdown.items(),
                                       key=lambda x: x[1]["total_pnl"], reverse=True):
                pnl_color = C.GREEN if data["total_pnl"] > 0 else C.RED
                rows.append([
                    strat[:25],
                    str(data["trades"]),
                    f"{data['win_rate']:.0f}%",
                    f"{pnl_color}${data['total_pnl']:,.2f}{C.RESET}",
                    f"${data['premium_collected']:,.2f}",
                ])
            lines.append(table(hd, rows, [28, 8, 8, 16, 14]))

        # Daily P&L
        if report.daily_pnl:
            lines.append(sub_header("DAILY P&L"))
            max_daily = max((abs(d["pnl"]) for d in report.daily_pnl), default=1)
            for d in report.daily_pnl[-20:]:  # Last 20 days
                color = C.GREEN if d["pnl"] > 0 else C.RED
                pnl_bar = bar(abs(d["pnl"]), max_daily, 20)
                direction = "+" if d["pnl"] > 0 else "-"
                lines.append(f"  {d['date']}  {pnl_bar}  {color}{direction}${abs(d['pnl']):,.2f}{C.RESET}  ({d['trades']} trades)")

        # Equity curve description
        if report.equity_curve and len(report.equity_curve) > 1:
            lines.append(sub_header("EQUITY CURVE"))
            start_bal = report.equity_curve[0]["balance"]
            end_bal = report.equity_curve[-1]["balance"]
            change = end_bal - start_bal
            change_pct = change / start_bal * 100
            lines.append(kv("Starting Balance", f"${start_bal:,.2f}"))
            lines.append(kv("Ending Balance", f"${end_bal:,.2f}"))
            lines.append(kv("Change", f"{pnl(change)} ({change_pct:+.2f}%)",
                             C.GREEN if change > 0 else C.RED))

        # Next month plan
        lines.append(sub_header("NEXT MONTH ADJUSTMENT PLAN"))
        adjustments = self._generate_adjustments(report)
        for adj in adjustments:
            lines.append(f"  → {adj}")

        lines.append(f"\n{C.CYAN}{'═' * 78}{C.RESET}\n")
        return "\n".join(lines)

    def _generate_adjustments(self, report: PerformanceReport) -> List[str]:
        """Generate data-driven adjustments for next month."""
        adjustments = []

        if report.win_rate < 70:
            adjustments.append(f"Win rate {report.win_rate:.0f}% is below 75% target → Widen strikes to 0.08-0.10 delta")
        elif report.win_rate > 90:
            adjustments.append(f"Win rate {report.win_rate:.0f}% is very high → Consider tightening to 0.15 delta for more premium")

        if report.profit_factor < 1.5:
            adjustments.append(f"Profit factor {report.profit_factor:.2f} below 1.5 → Cut losers faster (1.5x stop instead of 2x)")

        if report.max_drawdown > 10:
            adjustments.append(f"Max drawdown {report.max_drawdown:.1f}% too high → Reduce position size by 25%")

        if report.avg_winner and report.avg_loser and abs(report.avg_loser) > report.avg_winner * 2:
            adjustments.append("Average loser is 2x+ average winner → Tighten stop losses")

        if report.sharpe_estimate < 1.0:
            adjustments.append(f"Sharpe {report.sharpe_estimate:.2f} below 1.0 → Reduce frequency, focus on highest-conviction setups")

        # Strategy-specific
        for strat, data in report.strategy_breakdown.items():
            if data["trades"] >= 5 and data["win_rate"] < 60:
                adjustments.append(f"Strategy '{strat}' underperforming ({data['win_rate']:.0f}% WR) → Review or remove")
            elif data["trades"] >= 5 and data["win_rate"] > 85 and data["total_pnl"] > 0:
                adjustments.append(f"Strategy '{strat}' outperforming ({data['win_rate']:.0f}% WR) → Increase allocation")

        if not adjustments:
            adjustments.append("All metrics within targets — maintain current approach")

        return adjustments

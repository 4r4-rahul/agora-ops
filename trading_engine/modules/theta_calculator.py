"""
Module 3: SIG Daily Theta Decay Calculator
===========================================
Quantifies exact theta decay profits hour by hour throughout the trading day.
"""

import math
from datetime import datetime, date, time, timedelta
from typing import List, Dict, Any, Optional

from ..models import (
    MarketSnapshot, Position, TradeTicket, ThetaReport, SpreadType
)
from ..config import EngineConfig
from ..black_scholes import (
    bs_theta, bs_delta, bs_gamma, bs_put_price, bs_call_price, intraday_theta_curve
)
from ..formatters import header, sub_header, kv, pnl, bar, table, C


class ThetaCalculator:
    """
    SIG-style theta decay calculator.
    Calculates exact dollar theta at every level — position, portfolio, and hourly.
    """

    def __init__(self, config: EngineConfig):
        self.config = config

    def calculate(self, positions: List[Dict[str, Any]],
                  snap: MarketSnapshot) -> ThetaReport:
        """
        Calculate comprehensive theta analysis.

        positions: list of dicts with keys:
          - ticker, short_strike, long_strike, expiration, credit, current_value,
            option_type ("put" or "call"), contracts, dte
        """
        report = ThetaReport(timestamp=datetime.now())
        S = snap.spx_price
        iv = snap.vix_level / 100 if snap.vix_level > 1 else 0.18
        r = 0.045

        total_theta = 0.0
        total_delta = 0.0
        position_details = []

        for pos in positions:
            T = max(pos.get("dte", 0) / 365, 1 / (365 * 24))
            short_K = pos.get("short_strike", S)
            long_K = pos.get("long_strike", short_K - 5)
            contracts = pos.get("contracts", 1)
            opt_type = pos.get("option_type", "put")

            # Short leg theta (positive for premium seller)
            short_theta = -bs_theta(S, short_K, T, r, iv, opt_type) * contracts * 100
            # Long leg theta (negative for premium seller)
            long_theta = bs_theta(S, long_K, T, r, iv, opt_type) * contracts * 100

            position_theta = short_theta + long_theta  # Net theta income

            # Delta for the spread
            short_delta = bs_delta(S, short_K, T, r, iv, opt_type) * contracts * 100
            long_delta = -bs_delta(S, long_K, T, r, iv, opt_type) * contracts * 100
            position_delta = -(short_delta + long_delta)  # Net delta exposure

            # Gamma risk
            short_gamma = bs_gamma(S, short_K, T, r, iv) * contracts * 100
            long_gamma = bs_gamma(S, long_K, T, r, iv) * contracts * 100
            position_gamma = -(short_gamma - long_gamma)

            total_theta += position_theta
            total_delta += position_delta

            position_details.append({
                "ticker": pos.get("ticker", "SPX"),
                "spread": f"{short_K}/{long_K} {opt_type.upper()}",
                "contracts": contracts,
                "credit": pos.get("credit", 0),
                "current_value": pos.get("current_value", 0),
                "theta_daily": round(position_theta, 2),
                "delta": round(position_delta, 2),
                "gamma": round(position_gamma, 4),
                "dte": pos.get("dte", 0),
                "gamma_risk": abs(position_gamma) > abs(position_theta) * 2,
            })

        report.portfolio_theta = round(total_theta, 2)
        report.positions = position_details

        # Theta to delta ratio
        report.theta_to_delta_ratio = round(
            abs(total_theta / total_delta) if total_delta != 0 else 0, 2
        )

        # Gamma risk alert
        report.gamma_risk_alert = any(p["gamma_risk"] for p in position_details)

        # Hourly decay curve for 0DTE
        report.hourly_decay = self._calculate_hourly_decay(positions, snap)

        # Income projections
        report.daily_income = report.portfolio_theta
        report.weekly_projection = report.portfolio_theta * 5
        report.monthly_projection = report.portfolio_theta * 21

        return report

    def _calculate_hourly_decay(self, positions: List[Dict], snap: MarketSnapshot) -> Dict[str, float]:
        """Calculate aggregate hourly theta decay across all positions."""
        S = snap.spx_price
        iv = snap.vix_level / 100 if snap.vix_level > 1 else 0.18
        r = 0.045

        hourly = {}
        time_points = [
            "09:30", "10:00", "10:30", "11:00", "11:30",
            "12:00", "12:30", "13:00", "13:30", "14:00",
            "14:30", "15:00", "15:30", "15:45", "16:00"
        ]

        for i, tp in enumerate(time_points):
            total_decay = 0.0
            h, m = map(int, tp.split(":"))
            current_hour = h + m / 60

            for pos in positions:
                if pos.get("dte", 0) > 0:
                    # Multi-day positions have roughly constant hourly decay
                    daily_T = max(pos.get("dte", 1) / 365, 1e-6)
                    opt_type = pos.get("option_type", "put")
                    short_K = pos.get("short_strike", S)
                    contracts = pos.get("contracts", 1)
                    hourly_theta = -bs_theta(S, short_K, daily_T, r, iv, opt_type) * contracts * 100 / 6.5
                    total_decay += hourly_theta
                else:
                    # 0DTE — use intraday curve
                    remaining_hours = 16.0 - current_hour
                    T_now = max(remaining_hours / (252 * 6.5), 1e-8)

                    next_tp = time_points[i + 1] if i + 1 < len(time_points) else "16:00"
                    nh, nm = map(int, next_tp.split(":"))
                    next_hour = nh + nm / 60
                    T_next = max((16.0 - next_hour) / (252 * 6.5), 1e-8)

                    short_K = pos.get("short_strike", S)
                    opt_type = pos.get("option_type", "put")
                    contracts = pos.get("contracts", 1)

                    if opt_type == "put":
                        price_now = bs_put_price(S, short_K, T_now, r, iv)
                        price_next = bs_put_price(S, short_K, T_next, r, iv)
                    else:
                        price_now = bs_call_price(S, short_K, T_now, r, iv)
                        price_next = bs_call_price(S, short_K, T_next, r, iv)

                    decay = (price_now - price_next) * contracts * 100
                    total_decay += max(decay, 0)

            hourly[tp] = round(total_decay, 2)

        return hourly

    def compounding_projection(self, daily_income: float, account_size: float,
                                reinvest_pct: float = 1.0) -> Dict[str, float]:
        """Project account growth with theta income compounding."""
        balance = account_size
        projections = {}

        for day in range(1, 91):
            balance += daily_income * reinvest_pct
            # Scale daily income with account size growth
            growth_factor = balance / account_size
            scaled_income = daily_income * growth_factor

            if day == 30:
                projections["30 days"] = round(balance, 2)
            elif day == 60:
                projections["60 days"] = round(balance, 2)
            elif day == 90:
                projections["90 days"] = round(balance, 2)

            daily_income = scaled_income

        projections["daily_income_at_30d"] = round(daily_income * (projections.get("30 days", account_size) / account_size), 2)
        return projections

    # ── Report Output ──────────────────────────────────────────

    def format_report(self, report: ThetaReport, snap: MarketSnapshot) -> str:
        lines = []
        lines.append(header(
            "SIG DAILY THETA DECAY DASHBOARD",
            f"{report.timestamp:%Y-%m-%d %H:%M} | SPX {snap.spx_price:,.2f}"
        ))

        # Portfolio theta summary
        lines.append(sub_header("PORTFOLIO THETA SUMMARY"))
        lines.append(kv("Portfolio Daily Theta", pnl(report.portfolio_theta), C.GREEN))
        lines.append(kv("Theta-to-Delta Ratio", f"{report.theta_to_delta_ratio:.2f}",
                         C.GREEN if report.theta_to_delta_ratio > 0.5 else C.RED))
        if report.gamma_risk_alert:
            lines.append(f"\n  {C.RED}⚠️  GAMMA RISK ALERT: Gamma exceeds theta on one or more positions{C.RESET}")

        # Position-level theta
        lines.append(sub_header("POSITION-LEVEL THETA"))
        headers = ["Spread", "Ctrs", "Credit", "Theta/Day", "Delta", "DTE"]
        rows = []
        for p in report.positions:
            theta_str = f"{C.GREEN}+${p['theta_daily']:.2f}{C.RESET}"
            gamma_warn = f" {C.RED}⚠γ{C.RESET}" if p["gamma_risk"] else ""
            rows.append([
                f"{p['ticker']} {p['spread']}",
                str(p["contracts"]),
                f"${p['credit']:.2f}",
                theta_str + gamma_warn,
                f"{p['delta']:.1f}",
                str(p["dte"]),
            ])
        lines.append(table(headers, rows, [28, 6, 10, 18, 10, 6]))

        # Hourly decay curve
        if report.hourly_decay:
            lines.append(sub_header("HOURLY DECAY CURVE (0DTE)"))
            max_decay = max(report.hourly_decay.values()) if report.hourly_decay else 1
            for time_str, decay_val in report.hourly_decay.items():
                color = C.GREEN if decay_val > 0 else C.DIM
                decay_str = f"${decay_val:.2f}" if decay_val > 0 else "$0.00"
                lines.append(f"  {time_str}  {bar(decay_val, max_decay, 30)}  {color}{decay_str}{C.RESET}")

            lines.append(f"\n  {C.BOLD}Acceleration Zone:{C.RESET} Theta decay accelerates dramatically after 2:30 PM")
            lines.append(f"  {C.BOLD}Peak Decay:{C.RESET} Final 30 minutes (3:30-4:00) capture ~25% of total daily theta")

        # Income projections
        lines.append(sub_header("INCOME PROJECTIONS"))
        lines.append(kv("Daily", pnl(report.daily_income)))
        lines.append(kv("Weekly (5 days)", pnl(report.weekly_projection)))
        lines.append(kv("Monthly (21 days)", pnl(report.monthly_projection)))

        # Weekend theta
        lines.append(f"\n  {C.BOLD}Weekend Theta Capture:{C.RESET}")
        lines.append(f"  Sell Friday expiration to collect 3 days of theta over the weekend.")
        lines.append(f"  Friday close → Monday open = ~3x daily theta if held through weekend.")

        # Compounding
        comp = self.compounding_projection(
            report.daily_income, self.config.account.account_size
        )
        if comp:
            lines.append(sub_header("COMPOUNDING PROJECTION"))
            lines.append(kv("Starting Account", f"${self.config.account.account_size:,.2f}"))
            for period, value in comp.items():
                if "days" in period and "income" not in period:
                    growth = (value - self.config.account.account_size)
                    pct = growth / self.config.account.account_size * 100
                    lines.append(kv(period, f"${value:,.2f} ({C.GREEN}+{pct:.1f}%{C.RESET})"))

        lines.append(f"\n  {C.BOLD}Optimal Closing Time:{C.RESET}")
        lines.append(f"  0DTE: Close at 50% profit or by 3:50 PM")
        lines.append(f"  Weekly: Close at 50% profit by Thursday")
        lines.append(f"  Monthly: Close at 50% profit with >14 DTE remaining")

        lines.append(f"\n{C.CYAN}{'═' * 78}{C.RESET}\n")
        return "\n".join(lines)

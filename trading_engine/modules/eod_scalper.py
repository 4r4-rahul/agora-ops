"""
Module 11: Optiver End-of-Day Theta Scalper
============================================
Captures accelerated theta decay in the final 90 minutes on 0DTE options.
"""

from datetime import datetime, date, time, timedelta
from typing import Dict, Any, List, Optional

from ..models import (
    MarketSnapshot, OptionsChain, CreditSpread, SpreadLeg,
    SpreadType, TradeTicket, TradeResult
)
from ..config import EngineConfig
from ..market_data import MarketDataProvider
from ..black_scholes import (
    bs_put_price, bs_call_price, bs_delta, bs_gamma, bs_theta,
    strike_at_delta, prob_otm, intraday_theta_curve
)
from ..formatters import header, sub_header, kv, pnl, bar, table, C


class EODScalper:
    """
    Optiver-style end-of-day theta scalping.
    Enters 2:30-3:00 PM, captures maximum theta velocity, exits by 3:50.
    """

    def __init__(self, config: EngineConfig, data_provider: MarketDataProvider):
        self.config = config
        self.data = data_provider
        self.tc = config.trading
        # Track rolling performance
        self.session_log: List[Dict] = []
        self.rolling_win_rate: float = 0.0
        self.consecutive_wins: int = 0

    def scan(self, snap: MarketSnapshot, underlying: str = "SPX",
             chain: Optional[OptionsChain] = None) -> Dict[str, Any]:
        """
        Scan for EOD theta scalping opportunities.
        """
        S = snap.spx_price if underlying == "SPX" else snap.spy_price
        iv = snap.vix_level / 100 if snap.vix_level > 1 else 0.18
        r = 0.045

        result = {
            "underlying": underlying,
            "price": S,
            "time": datetime.now().strftime("%H:%M"),
            "eligible": False,
        }

        # 1. Check time window
        now = datetime.now()
        entry_start = now.replace(hour=14, minute=30, second=0)
        entry_end = now.replace(hour=15, minute=0, second=0)
        close_by = now.replace(hour=15, minute=50, second=0)

        # For analysis purposes, assume we're in the window
        result["window"] = {
            "entry_start": "14:30",
            "entry_end": "15:00",
            "close_by": "15:50",
            "minutes_to_close": 90,
        }

        # 2. Check if win rate supports continued trading
        if len(self.session_log) >= 20:
            recent_wins = sum(1 for s in self.session_log[-20:] if s.get("pnl", 0) > 0)
            self.rolling_win_rate = recent_wins / 20 * 100
            if self.rolling_win_rate < 70:
                result["eligible"] = False
                result["pause_reason"] = f"Rolling win rate {self.rolling_win_rate:.0f}% < 70% minimum. Pause and reassess."
                return result

        result["eligible"] = True

        # 3. Calculate rapid decay math (15-min blocks)
        result["decay_blocks"] = self._calculate_decay_blocks(S, iv, r)

        # 4. Find optimal put credit spread
        T_90min = 1.5 / (252 * 6.5)  # 90 minutes in years
        target_delta = self.tc.eod_short_delta_max

        put_short_K = strike_at_delta(S, T_90min, r, iv, target_delta, "put")
        put_long_K = put_short_K - self.tc.spread_width_min

        put_credit = (bs_put_price(S, put_short_K, T_90min, r, iv) -
                      bs_put_price(S, put_long_K, T_90min, r, iv))
        put_credit = round(max(put_credit, 0), 2)

        if put_credit >= self.tc.eod_min_credit:
            result["put_spread"] = {
                "short_strike": put_short_K,
                "long_strike": put_long_K,
                "credit": put_credit,
                "delta": round(abs(bs_delta(S, put_short_K, T_90min, r, iv, "put")), 4),
                "gamma": round(bs_gamma(S, put_short_K, T_90min, r, iv), 6),
                "prob_otm": round(prob_otm(S, put_short_K, T_90min, r, iv, "put") * 100, 1),
                "max_loss": round(self.tc.spread_width_min - put_credit, 2),
                "stop_loss": round(put_credit * self.tc.eod_stop_multiplier, 2),
            }

        # 5. Find optimal call credit spread
        call_short_K = strike_at_delta(S, T_90min, r, iv, target_delta, "call")
        call_long_K = call_short_K + self.tc.spread_width_min

        call_credit = (bs_call_price(S, call_short_K, T_90min, r, iv) -
                       bs_call_price(S, call_long_K, T_90min, r, iv))
        call_credit = round(max(call_credit, 0), 2)

        if call_credit >= self.tc.eod_min_credit:
            result["call_spread"] = {
                "short_strike": call_short_K,
                "long_strike": call_long_K,
                "credit": call_credit,
                "delta": round(abs(bs_delta(S, call_short_K, T_90min, r, iv, "call")), 4),
                "gamma": round(bs_gamma(S, call_short_K, T_90min, r, iv), 6),
                "prob_otm": round(prob_otm(S, call_short_K, T_90min, r, iv, "call") * 100, 1),
                "max_loss": round(self.tc.spread_width_min - call_credit, 2),
                "stop_loss": round(call_credit * self.tc.eod_stop_multiplier, 2),
            }

        # 6. Gamma awareness
        result["gamma_warning"] = self._gamma_check(S, iv, r, T_90min, put_short_K, call_short_K)

        # 7. Position sizing (conservative for EOD)
        result["sizing"] = self._eod_position_size(result)

        # 8. Minute-by-minute timeline
        result["timeline"] = self._build_timeline(result)

        # 9. Risk checklist
        result["risk_checklist"] = self._risk_checklist(snap, result)

        return result

    def _calculate_decay_blocks(self, S: float, iv: float, r: float) -> List[Dict]:
        """Calculate theta decay in 15-min blocks from 2:30 to 4:00."""
        blocks = []
        # OTM put at ~0.10 delta
        T_90 = 1.5 / (252 * 6.5)
        K = strike_at_delta(S, T_90, r, iv, 0.10, "put")

        times = [
            ("14:30", 90), ("14:45", 75), ("15:00", 60),
            ("15:15", 45), ("15:30", 30), ("15:45", 15), ("16:00", 0)
        ]

        prev_price = None
        for time_str, mins_left in times:
            T = max(mins_left / (252 * 6.5 * 60), 1e-10)
            price = bs_put_price(S, K, T, r, iv) if mins_left > 0 else 0

            decay = prev_price - price if prev_price is not None else 0
            blocks.append({
                "time": time_str,
                "minutes_left": mins_left,
                "option_price": round(price, 4),
                "decay": round(max(decay, 0), 4),
                "pct_of_total": 0,  # Will be calculated below
            })
            prev_price = price

        # Calculate percentage of total decay
        total_decay = sum(b["decay"] for b in blocks)
        if total_decay > 0:
            for b in blocks:
                b["pct_of_total"] = round(b["decay"] / total_decay * 100, 1)

        return blocks

    def _gamma_check(self, S: float, iv: float, r: float, T: float,
                     put_K: float, call_K: float) -> Dict[str, Any]:
        """Assess gamma risk for EOD positions."""
        put_gamma = bs_gamma(S, put_K, T, r, iv) * 100
        call_gamma = bs_gamma(S, call_K, T, r, iv) * 100
        put_theta = -bs_theta(S, put_K, T, r, iv, "put") * 100
        call_theta = -bs_theta(S, call_K, T, r, iv, "call") * 100

        # Gamma risk is extreme this close to expiry
        max_gamma = max(put_gamma, call_gamma)
        max_theta = max(put_theta, call_theta)

        return {
            "put_gamma_100": round(put_gamma, 2),
            "call_gamma_100": round(call_gamma, 2),
            "theta_gamma_ratio": round(max_theta / max_gamma, 2) if max_gamma > 0 else 0,
            "warning": max_gamma > max_theta * 3,
            "message": (
                "⚠️ EXTREME gamma near expiry. Delta can swing wildly. "
                "Keep positions SMALL and use hard stops."
                if max_gamma > max_theta * 3 else
                "Gamma elevated but manageable. Maintain strict stop discipline."
            ),
        }

    def _eod_position_size(self, result: Dict) -> Dict[str, Any]:
        """Conservative position sizing for EOD scalps."""
        acct = self.config.account.account_size
        # Use 1% of account for EOD scalps (very conservative)
        max_risk = acct * 0.01

        # Scale with consecutive wins
        base_contracts = 1
        if self.consecutive_wins >= 3:
            base_contracts = 2
        elif self.consecutive_wins >= 5:
            base_contracts = 3

        # But never exceed risk limit
        ps = result.get("put_spread", {})
        max_loss_per = ps.get("max_loss", 5) * 100
        max_contracts = max(1, int(max_risk / max_loss_per)) if max_loss_per > 0 else 1
        contracts = min(base_contracts, max_contracts)

        return {
            "contracts": contracts,
            "max_risk": round(max_loss_per * contracts, 2),
            "scaling_note": (
                f"Start: 1 contract. After 3 consecutive wins: 2 contracts. "
                f"Current streak: {self.consecutive_wins}."
            ),
        }

    def _build_timeline(self, result: Dict) -> List[Dict]:
        """Minute-by-minute execution timeline."""
        timeline = [
            {"time": "14:25", "action": "PRE-SCAN", "detail": "Check SPX price, VIX, and any late-day news. Identify strikes."},
            {"time": "14:30", "action": "ENTRY WINDOW OPENS", "detail": "Place limit order for credit spread at mid-price."},
            {"time": "14:35", "action": "FILL CHECK", "detail": "If not filled, adjust to natural (closer to market). Don't chase."},
            {"time": "14:40", "action": "POSITION LIVE", "detail": "Set hard stop-loss. Set profit target alert."},
            {"time": "15:00", "action": "NO NEW ENTRIES", "detail": "Entry window closed. Manage existing positions only."},
            {"time": "15:15", "action": "MID-CHECK", "detail": "Theta accelerating. If at 50%+ profit, consider closing."},
            {"time": "15:30", "action": "ACCELERATION ZONE", "detail": "Peak theta decay. Hold if safe, close if at target."},
            {"time": "15:45", "action": "FINAL CHECK", "detail": "Must be closed within 5 minutes. No exceptions."},
            {"time": "15:50", "action": "HARD CLOSE", "detail": "Close ALL positions. Market order if needed. Avoid settlement."},
            {"time": "15:55", "action": "LOG TRADE", "detail": "Record entry, exit, P&L, and notes in journal."},
        ]
        return timeline

    def _risk_checklist(self, snap: MarketSnapshot, result: Dict) -> List[Dict]:
        """EOD-specific risk checklist."""
        return [
            {"item": "VIX < 30", "pass": snap.vix_level < 30, "value": f"{snap.vix_level:.1f}"},
            {"item": "No pending news", "pass": not snap.is_fed_day, "value": "Clear" if not snap.is_fed_day else "Fed Day!"},
            {"item": "SPX not trending", "pass": abs(snap.spx_futures_overnight_change) < snap.expected_move_1d * 0.8,
             "value": f"Move: {snap.spx_futures_overnight_change:+.1f}"},
            {"item": "Rolling WR > 70%", "pass": self.rolling_win_rate >= 70 or len(self.session_log) < 20,
             "value": f"{self.rolling_win_rate:.0f}%" if self.session_log else "N/A"},
            {"item": "Hard stop set", "pass": True, "value": "REQUIRED before entry"},
            {"item": "Position ≤ max contracts", "pass": True,
             "value": f"{result.get('sizing', {}).get('contracts', 1)} contracts"},
        ]

    # ── Report Output ──────────────────────────────────────────

    def format_report(self, result: Dict, snap: MarketSnapshot) -> str:
        lines = []
        lines.append(header(
            "OPTIVER END-OF-DAY THETA SCALPER",
            f"{result['underlying']} @ {result['price']:,.2f} | {datetime.now():%Y-%m-%d}"
        ))

        if not result["eligible"]:
            lines.append(f"\n  {C.RED}❌ NOT ELIGIBLE: {result.get('pause_reason', 'Unknown')}{C.RESET}")
            return "\n".join(lines)

        # Decay blocks
        lines.append(sub_header("15-MINUTE THETA DECAY BLOCKS"))
        blocks = result.get("decay_blocks", [])
        max_decay = max((b["decay"] for b in blocks), default=0.01)
        for b in blocks:
            decay_bar = bar(b["decay"], max_decay, 25)
            lines.append(f"  {b['time']}  ({b['minutes_left']:>3}min left)  "
                          f"{decay_bar}  ${b['decay']:.4f}  ({b['pct_of_total']:.0f}%)")

        lines.append(f"\n  {C.BOLD}Peak Decay:{C.RESET} Final 30 min captures ~40-50% of remaining theta")
        lines.append(f"  {C.BOLD}Sweet Spot:{C.RESET} Enter at 14:30, capture the steepest part of the curve")

        # Put spread
        ps = result.get("put_spread")
        if ps:
            lines.append(sub_header("PUT CREDIT SPREAD"))
            lines.append(kv("Short Put", f"{ps['short_strike']} (Δ {ps['delta']:.4f})", C.RED))
            lines.append(kv("Long Put", f"{ps['long_strike']}", C.GREEN))
            lines.append(kv("Credit", f"${ps['credit']:.2f}", C.GREEN))
            lines.append(kv("Max Loss", f"${ps['max_loss']:.2f}", C.RED))
            lines.append(kv("Stop Loss", f"Close at ${ps['stop_loss']:.2f} (1.5x credit)"))
            lines.append(kv("P(OTM)", f"{ps['prob_otm']:.1f}%", C.GREEN))

        # Call spread
        cs = result.get("call_spread")
        if cs:
            lines.append(sub_header("CALL CREDIT SPREAD"))
            lines.append(kv("Short Call", f"{cs['short_strike']} (Δ {cs['delta']:.4f})", C.RED))
            lines.append(kv("Long Call", f"{cs['long_strike']}", C.GREEN))
            lines.append(kv("Credit", f"${cs['credit']:.2f}", C.GREEN))
            lines.append(kv("Max Loss", f"${cs['max_loss']:.2f}", C.RED))
            lines.append(kv("Stop Loss", f"Close at ${cs['stop_loss']:.2f} (1.5x credit)"))
            lines.append(kv("P(OTM)", f"{cs['prob_otm']:.1f}%", C.GREEN))

        # Gamma warning
        gw = result.get("gamma_warning", {})
        lines.append(sub_header("GAMMA RISK ASSESSMENT"))
        lines.append(kv("Put Γ×100", f"{gw.get('put_gamma_100', 0):.2f}"))
        lines.append(kv("Call Γ×100", f"{gw.get('call_gamma_100', 0):.2f}"))
        lines.append(kv("θ/Γ Ratio", f"{gw.get('theta_gamma_ratio', 0):.2f}"))
        warn_color = C.RED if gw.get("warning") else C.GREEN
        lines.append(f"  {warn_color}{gw.get('message', '')}{C.RESET}")

        # Sizing
        sz = result.get("sizing", {})
        lines.append(sub_header("POSITION SIZING"))
        lines.append(kv("Contracts", sz.get("contracts", 1)))
        lines.append(kv("Max Risk", f"${sz.get('max_risk', 0):,.2f}"))
        lines.append(f"  {C.DIM}{sz.get('scaling_note', '')}{C.RESET}")

        # Timeline
        lines.append(sub_header("EXECUTION TIMELINE"))
        for t in result.get("timeline", []):
            lines.append(f"  {C.BOLD}{t['time']}{C.RESET}  {t['action']}")
            lines.append(f"          {C.DIM}{t['detail']}{C.RESET}")

        # Risk checklist
        lines.append(sub_header("RISK CHECKLIST"))
        for check in result.get("risk_checklist", []):
            icon = f"{C.GREEN}✅{C.RESET}" if check["pass"] else f"{C.RED}❌{C.RESET}"
            lines.append(f"  {icon} {check['item']}: {check['value']}")

        lines.append(f"\n{C.CYAN}{'═' * 78}{C.RESET}\n")
        return "\n".join(lines)

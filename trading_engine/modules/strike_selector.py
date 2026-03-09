"""
Module 4: Two Sigma Probability-Based Strike Selection
======================================================
Selects strikes based purely on statistical probability models.
"""

import math
from datetime import datetime, date
from typing import Dict, Any, List, Optional

from ..models import MarketSnapshot, OptionsChain, OptionContract
from ..config import EngineConfig
from ..black_scholes import (
    strike_at_delta, strike_at_std_dev, prob_otm, prob_between,
    expected_move, bs_delta, bs_theta, bs_gamma, implied_vol
)
from ..formatters import header, sub_header, kv, table, bar, C


class StrikeSelector:
    """
    Two Sigma-style probability-based strike selection.
    Removes emotion, replaces gut feeling with math.
    """

    def __init__(self, config: EngineConfig):
        self.config = config

    def select(self, snap: MarketSnapshot, underlying: str = "SPX",
               target_win_rate: float = 0.85,
               chain: Optional[OptionsChain] = None) -> Dict[str, Any]:
        """
        Run probability-based strike selection.

        Args:
            target_win_rate: Desired probability of profit (0.0-1.0)

        Returns comprehensive selection analysis.
        """
        S = snap.spx_price if underlying == "SPX" else snap.spy_price
        iv = snap.vix_level / 100 if snap.vix_level > 1 else 0.18
        r = 0.045
        result = {"underlying": underlying, "price": S, "iv": iv}

        # 1. Expected moves at different timeframes
        result["expected_moves"] = {
            "1_day": round(expected_move(S, iv, 1/252), 2),
            "1_week": round(expected_move(S, iv, 5/252), 2),
            "1_month": round(expected_move(S, iv, 21/252), 2),
        }

        # 2. Strikes at different delta levels
        delta_levels = [0.05, 0.10, 0.12, 0.15, 0.20, 0.25, 0.30]
        timeframes = {
            "0DTE": 1 / (252 * 6.5),   # ~1 trading day in hours
            "weekly": 5 / 252,
            "monthly": 30 / 365,
        }

        result["delta_strikes"] = {}
        for tf_name, T in timeframes.items():
            strikes = {}
            for d in delta_levels:
                put_K = strike_at_delta(S, T, r, iv, d, "put")
                call_K = strike_at_delta(S, T, r, iv, d, "call")
                p_otm_put = prob_otm(S, put_K, T, r, iv, "put")
                p_otm_call = prob_otm(S, call_K, T, r, iv, "call")
                strikes[d] = {
                    "put_strike": put_K,
                    "call_strike": call_K,
                    "prob_otm_put": round(p_otm_put * 100, 1),
                    "prob_otm_call": round(p_otm_call * 100, 1),
                    "distance_put": round(S - put_K, 1),
                    "distance_call": round(call_K - S, 1),
                }
            result["delta_strikes"][tf_name] = strikes

        # 3. Standard deviation mapping
        result["std_dev_strikes"] = {}
        for tf_name, T in timeframes.items():
            sd_strikes = {}
            for num_std in [0.5, 1.0, 1.5, 2.0, 2.5]:
                put_K = strike_at_std_dev(S, iv, T, num_std, "put")
                call_K = strike_at_std_dev(S, iv, T, num_std, "call")
                p_between = prob_between(S, put_K, call_K, T, r, iv)
                sd_strikes[num_std] = {
                    "put_strike": put_K,
                    "call_strike": call_K,
                    "prob_contained": round(p_between * 100, 1),
                }
            result["std_dev_strikes"][tf_name] = sd_strikes

        # 4. Win rate by delta level (theoretical + historical typical)
        result["win_rates"] = {
            0.05: {"theoretical": 95.0, "historical_typical": 93.0},
            0.10: {"theoretical": 90.0, "historical_typical": 88.0},
            0.15: {"theoretical": 85.0, "historical_typical": 83.0},
            0.20: {"theoretical": 80.0, "historical_typical": 77.0},
            0.25: {"theoretical": 75.0, "historical_typical": 72.0},
            0.30: {"theoretical": 70.0, "historical_typical": 67.0},
        }

        # 5. Premium decay speed at each delta level
        result["decay_speed"] = self._calculate_decay_speed(S, iv, r, delta_levels)

        # 6. Gap risk adjustment
        result["gap_adjustment"] = self._gap_risk_adjustment(snap)

        # 7. Skew-adjusted selection
        result["skew_adjustment"] = self._skew_adjustment(chain, S, iv)

        # 8. Today's recommended strikes based on target win rate
        result["todays_picks"] = self._todays_recommendation(
            S, iv, r, target_win_rate, snap
        )

        return result

    def _calculate_decay_speed(self, S: float, iv: float, r: float,
                                deltas: List[float]) -> Dict[float, Dict]:
        """Calculate how fast premium decays at each delta level."""
        T = 1 / 252  # 1-day
        result = {}
        for d in deltas:
            put_K = strike_at_delta(S, T, r, iv, d, "put")
            theta = -bs_theta(S, put_K, T, r, iv, "put")
            gamma = bs_gamma(S, put_K, T, r, iv)

            # Theta/gamma ratio — higher = better risk-adjusted decay
            tg_ratio = abs(theta / gamma) if gamma > 0 else 0

            result[d] = {
                "theta_per_day": round(theta * 100, 2),  # Per contract
                "gamma_risk": round(gamma * 100, 4),
                "theta_gamma_ratio": round(tg_ratio, 2),
                "speed": "fast" if d >= 0.20 else "moderate" if d >= 0.10 else "slow",
            }
        return result

    def _gap_risk_adjustment(self, snap: MarketSnapshot) -> Dict[str, Any]:
        """Recommend strike widening based on overnight event risk."""
        adjustment = {"widen_strikes": False, "extra_width_pct": 0, "reason": ""}

        if snap.is_fed_day:
            adjustment = {"widen_strikes": True, "extra_width_pct": 50, "reason": "Fed announcement day"}
        elif snap.is_cpi_day:
            adjustment = {"widen_strikes": True, "extra_width_pct": 30, "reason": "CPI release"}
        elif len(snap.major_earnings_today) >= 3:
            adjustment = {"widen_strikes": True, "extra_width_pct": 20, "reason": "Heavy earnings day"}
        elif abs(snap.spx_futures_overnight_change) > snap.expected_move_1d * 0.5:
            adjustment = {"widen_strikes": True, "extra_width_pct": 25,
                          "reason": f"Large overnight move ({snap.spx_futures_overnight_change:+.1f} pts)"}
        else:
            adjustment["reason"] = "No significant gap risk detected"

        return adjustment

    def _skew_adjustment(self, chain: Optional[OptionsChain],
                         S: float, iv: float) -> Dict[str, Any]:
        """Analyze skew for strike adjustment opportunities."""
        if chain and chain.puts:
            # Compare IV at different strikes
            otm_puts = [p for p in chain.puts if p.strike < S]
            if len(otm_puts) >= 5:
                near_put = min(otm_puts, key=lambda p: abs(abs(p.greeks.delta) - 0.25))
                far_put = min(otm_puts, key=lambda p: abs(abs(p.greeks.delta) - 0.10))
                skew = near_put.greeks.iv - far_put.greeks.iv
                if skew > 0.03:
                    return {
                        "skew_steep": True,
                        "recommendation": "Sell further OTM puts — steep skew means more premium at wider distances",
                        "near_iv": round(near_put.greeks.iv * 100, 1),
                        "far_iv": round(far_put.greeks.iv * 100, 1),
                    }

        return {
            "skew_steep": False,
            "recommendation": "Normal skew — standard strike placement recommended",
        }

    def _todays_recommendation(self, S: float, iv: float, r: float,
                                target_wr: float, snap: MarketSnapshot) -> Dict[str, Any]:
        """Generate today's specific strike recommendation."""
        # Map target win rate to delta
        target_delta = 1 - target_wr  # e.g., 85% WR → 0.15 delta

        # Adjust for gap risk
        gap = self._gap_risk_adjustment(snap)
        if gap["widen_strikes"]:
            target_delta *= (1 - gap["extra_width_pct"] / 200)

        # 0DTE strikes
        T_0dte = 1 / (252 * 6.5)
        put_K = strike_at_delta(S, T_0dte, r, iv, target_delta, "put")
        call_K = strike_at_delta(S, T_0dte, r, iv, target_delta, "call")

        # Weekly strikes
        T_weekly = 5 / 252
        put_K_w = strike_at_delta(S, T_weekly, r, iv, target_delta, "put")
        call_K_w = strike_at_delta(S, T_weekly, r, iv, target_delta, "call")

        return {
            "target_win_rate": f"{target_wr * 100:.0f}%",
            "target_delta": round(target_delta, 3),
            "zero_dte": {
                "put_strike": put_K,
                "call_strike": call_K,
                "range": f"{put_K} — {call_K}",
                "prob_contained": round(
                    prob_between(S, put_K, call_K, T_0dte, r, iv) * 100, 1
                ),
            },
            "weekly": {
                "put_strike": put_K_w,
                "call_strike": call_K_w,
                "range": f"{put_K_w} — {call_K_w}",
                "prob_contained": round(
                    prob_between(S, put_K_w, call_K_w, T_weekly, r, iv) * 100, 1
                ),
            },
        }

    # ── Report Output ──────────────────────────────────────────

    def format_report(self, result: Dict, snap: MarketSnapshot) -> str:
        lines = []
        S = result["price"]
        lines.append(header(
            "TWO SIGMA PROBABILITY STRIKE SELECTION",
            f"{result['underlying']} @ {S:,.2f} | IV: {result['iv']*100:.1f}%"
        ))

        # Expected moves
        em = result["expected_moves"]
        lines.append(sub_header("EXPECTED MOVE CALCULATION"))
        lines.append(kv("1-Day Expected Move", f"±{em['1_day']:.1f} pts ({em['1_day']/S*100:.2f}%)"))
        lines.append(kv("1-Week Expected Move", f"±{em['1_week']:.1f} pts ({em['1_week']/S*100:.2f}%)"))
        lines.append(kv("1-Month Expected Move", f"±{em['1_month']:.1f} pts ({em['1_month']/S*100:.2f}%)"))

        # Standard deviation map
        lines.append(sub_header("STANDARD DEVIATION STRIKE MAP"))
        for tf in ["0DTE", "weekly"]:
            sd = result["std_dev_strikes"].get(tf, {})
            if sd:
                lines.append(f"\n  {C.BOLD}{tf.upper()}{C.RESET}")
                hd = ["Std Dev", "Put Strike", "Call Strike", "P(Contained)"]
                rows = []
                for n, data in sd.items():
                    prob = data["prob_contained"]
                    color = C.GREEN if prob > 90 else C.YELLOW if prob > 80 else C.RED
                    rows.append([
                        f"{n:.1f}σ",
                        str(data["put_strike"]),
                        str(data["call_strike"]),
                        f"{color}{prob:.1f}%{C.RESET}",
                    ])
                lines.append(table(hd, rows, [12, 14, 14, 16]))

        # Delta-based probability matrix
        lines.append(sub_header("DELTA-BASED STRIKE MATRIX (0DTE)"))
        strikes_0dte = result["delta_strikes"].get("0DTE", {})
        if strikes_0dte:
            hd = ["Delta", "Put K", "Call K", "Dist Put", "Dist Call", "P(OTM)"]
            rows = []
            for d, data in sorted(strikes_0dte.items()):
                rows.append([
                    f"Δ{d:.2f}",
                    str(data["put_strike"]),
                    str(data["call_strike"]),
                    f"{data['distance_put']:.0f}",
                    f"{data['distance_call']:.0f}",
                    f"{data['prob_otm_put']:.1f}%",
                ])
            lines.append(table(hd, rows, [10, 10, 10, 12, 12, 10]))

        # Win rates
        lines.append(sub_header("WIN RATE BY DELTA LEVEL"))
        wr = result["win_rates"]
        for d, rates in sorted(wr.items()):
            theo = rates["theoretical"]
            hist = rates["historical_typical"]
            lines.append(f"  Δ{d:.2f}:  Theoretical {C.GREEN}{theo:.0f}%{C.RESET}"
                          f"  |  Historical {C.YELLOW}{hist:.0f}%{C.RESET}"
                          f"  |  {bar(hist, 100, 20)}")

        # Decay speed
        lines.append(sub_header("PREMIUM DECAY SPEED BY DELTA"))
        ds = result["decay_speed"]
        for d, data in sorted(ds.items()):
            lines.append(kv(f"Δ{d:.2f}",
                             f"θ=${data['theta_per_day']:.2f}/day  |  γ-risk={data['gamma_risk']:.4f}  |  "
                             f"θ/γ={data['theta_gamma_ratio']:.1f}  ({data['speed']})"))

        # Gap risk
        gap = result["gap_adjustment"]
        lines.append(sub_header("GAP RISK ADJUSTMENT"))
        if gap["widen_strikes"]:
            lines.append(f"  {C.YELLOW}⚠️  {gap['reason']}{C.RESET}")
            lines.append(f"  Recommendation: Widen strikes by {gap['extra_width_pct']}%")
        else:
            lines.append(f"  {C.GREEN}✅ {gap['reason']}{C.RESET}")

        # Today's picks
        picks = result["todays_picks"]
        lines.append(sub_header(f"TODAY'S STRIKES (Target: {picks['target_win_rate']} Win Rate)"))
        z = picks["zero_dte"]
        w = picks["weekly"]
        lines.append(f"\n  {C.BOLD}0DTE:{C.RESET}")
        lines.append(kv("  Short Put", str(z["put_strike"]), C.RED))
        lines.append(kv("  Short Call", str(z["call_strike"]), C.RED))
        lines.append(kv("  Profit Range", z["range"]))
        lines.append(kv("  P(Contained)", f"{z['prob_contained']:.1f}%", C.GREEN))

        lines.append(f"\n  {C.BOLD}Weekly:{C.RESET}")
        lines.append(kv("  Short Put", str(w["put_strike"]), C.RED))
        lines.append(kv("  Short Call", str(w["call_strike"]), C.RED))
        lines.append(kv("  Profit Range", w["range"]))
        lines.append(kv("  P(Contained)", f"{w['prob_contained']:.1f}%", C.GREEN))

        lines.append(f"\n{C.CYAN}{'═' * 78}{C.RESET}\n")
        return "\n".join(lines)

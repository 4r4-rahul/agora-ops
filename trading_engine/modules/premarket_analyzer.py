"""
Module 6: Jane Street Pre-Market Edge Analyzer
===============================================
Analyzes pre-market conditions at 8 AM to plan the day's theta strategy.
"""

from datetime import datetime, date
from typing import Dict, Any

from ..models import MarketSnapshot, MarketRegime, TrendState
from ..config import EngineConfig
from ..black_scholes import expected_move
from ..formatters import header, sub_header, kv, C


class PreMarketAnalyzer:
    """
    Jane Street-style morning briefing.
    Analyzes overnight action, IV, economic calendar, and key levels
    to produce a complete pre-market trade plan.
    """

    def __init__(self, config: EngineConfig):
        self.config = config

    def analyze(self, snap: MarketSnapshot) -> Dict[str, Any]:
        """Run complete pre-market analysis."""
        S = snap.spx_price
        iv = snap.vix_level / 100 if snap.vix_level > 1 else 0.18

        result = {}

        # 1. Overnight futures analysis
        result["overnight"] = self._analyze_overnight(snap)

        # 2. Pre-market IV levels
        result["iv_analysis"] = self._analyze_iv(snap)

        # 3. Economic calendar impact
        result["economic_impact"] = self._analyze_calendar(snap)

        # 4. Earnings exposure
        result["earnings_exposure"] = self._analyze_earnings(snap)

        # 5. Globex range
        result["globex_range"] = {
            "high": snap.globex_high,
            "low": snap.globex_low,
            "range": round(snap.globex_high - snap.globex_low, 2) if snap.globex_high and snap.globex_low else 0,
            "range_vs_em": round(
                (snap.globex_high - snap.globex_low) / snap.expected_move_1d * 100, 1
            ) if snap.expected_move_1d > 0 and snap.globex_high and snap.globex_low else 0,
        }

        # 6. Opening gap strategy
        result["gap_strategy"] = self._analyze_gap(snap)

        # 7. IV crush opportunity
        result["iv_crush"] = self._check_iv_crush(snap)

        # 8. Previous day close analysis
        result["prev_close_analysis"] = self._analyze_prev_close(snap)

        # 9. Support and resistance
        result["key_levels"] = self._calculate_key_levels(snap)

        # 10. Trade plan with scenarios
        result["trade_plan"] = self._generate_trade_plan(snap, result)

        return result

    def _analyze_overnight(self, snap: MarketSnapshot) -> Dict[str, Any]:
        change = snap.spx_futures_overnight_change
        em = snap.expected_move_1d
        pct_of_em = abs(change) / em * 100 if em > 0 else 0

        if abs(change) < em * 0.1:
            direction = "flat"
            thesis = "Flat overnight — normal session expected. Good for iron condors."
        elif change > em * 0.5:
            direction = "gap_up_large"
            thesis = "Large gap up — may fade. Watch for mean reversion short setups."
        elif change > em * 0.2:
            direction = "gap_up_small"
            thesis = "Small gap up — could extend or consolidate. Favor put credit spreads."
        elif change < -em * 0.5:
            direction = "gap_down_large"
            thesis = "Large gap down — fear elevated. Put premium inflated. Look for IV crush."
        elif change < -em * 0.2:
            direction = "gap_down_small"
            thesis = "Small gap down — watch if it fills. Favor call credit spreads cautiously."
        else:
            direction = "flat"
            thesis = "Minimal overnight movement. Standard playbook applies."

        return {
            "change": round(change, 2),
            "pct_of_em": round(pct_of_em, 1),
            "direction": direction,
            "thesis": thesis,
            "gap_will_hold": abs(change) > em * 0.5,
        }

    def _analyze_iv(self, snap: MarketSnapshot) -> Dict[str, Any]:
        iv = snap.vix_level
        iv_rank = snap.iv_rank
        rv = snap.realized_vol_20d * 100 if snap.realized_vol_20d < 1 else snap.realized_vol_20d

        iv_premium = iv - rv
        signal = "neutral"
        if iv_premium > 5:
            signal = "rich"
        elif iv_premium > 2:
            signal = "slightly_rich"
        elif iv_premium < -2:
            signal = "cheap"

        return {
            "vix": round(iv, 2),
            "iv_rank": round(iv_rank, 1),
            "realized_vol": round(rv, 2),
            "iv_premium": round(iv_premium, 2),
            "signal": signal,
            "description": {
                "rich": "IV significantly above RV. Premium is inflated — ideal for selling.",
                "slightly_rich": "IV modestly above RV. Normal edge for premium sellers.",
                "neutral": "IV approximately equal to RV. No systematic edge.",
                "cheap": "IV below RV. Options are underpriced. Avoid selling premium.",
            }.get(signal, ""),
        }

    def _analyze_calendar(self, snap: MarketSnapshot) -> Dict[str, Any]:
        events = snap.economic_events
        risk_level = "low"
        strategy_impact = "Standard positioning OK"

        if snap.is_fed_day:
            risk_level = "extreme"
            strategy_impact = "WAIT until after Fed announcement. No new positions before 2:30 PM."
        elif snap.is_cpi_day:
            risk_level = "high"
            strategy_impact = "Wait for CPI release (8:30 AM). Enter only after reaction settles (~10:00 AM)."
        elif len(events) >= 3:
            risk_level = "elevated"
            strategy_impact = "Multiple data points. Use wider strikes (+20% width)."
        elif len(events) >= 1:
            risk_level = "moderate"
            strategy_impact = "Minor data releases. Standard strikes OK with awareness."

        return {
            "events": events,
            "risk_level": risk_level,
            "strategy_impact": strategy_impact,
        }

    def _analyze_earnings(self, snap: MarketSnapshot) -> Dict[str, Any]:
        earnings = snap.major_earnings_today
        if not earnings:
            return {"exposure": "none", "impact": "No major earnings today."}

        if len(earnings) >= 5:
            return {
                "exposure": "heavy",
                "companies": earnings,
                "impact": f"{len(earnings)} major earnings. Expect sector rotation and index volatility.",
            }
        elif len(earnings) >= 2:
            return {
                "exposure": "moderate",
                "companies": earnings,
                "impact": f"{len(earnings)} earnings reports. Watch for sector-specific moves.",
            }
        return {
            "exposure": "light",
            "companies": earnings,
            "impact": f"{earnings[0]} reports. Single-name risk, limited index impact.",
        }

    def _analyze_gap(self, snap: MarketSnapshot) -> Dict[str, Any]:
        gap = snap.spx_futures_overnight_change
        em = snap.expected_move_1d

        if abs(gap) < em * 0.15:
            return {"size": "none", "strategy": "No gap. Standard iron condor or credit spread."}

        if abs(gap) < em * 0.4:
            return {
                "size": "small",
                "strategy": "Small gap likely to fill. Sell into the gap direction.",
                "action": f"If gap {'up' if gap > 0 else 'down'}: sell "
                          f"{'call' if gap > 0 else 'put'} credit spread at gap edge.",
            }

        return {
            "size": "large",
            "strategy": "Large gap may extend. Wait 30 min for direction confirmation.",
            "action": "Enter only after 10:00 AM. Use wider strikes. Consider only one side.",
        }

    def _check_iv_crush(self, snap: MarketSnapshot) -> Dict[str, Any]:
        # Check if yesterday had elevated IV (event day) that could create
        # leftover inflated premium today
        if snap.iv_rank > 70:
            return {
                "opportunity": True,
                "description": "IV Rank elevated (>70). Post-event premium may still be inflated.",
                "action": "Look for 0DTE credit spreads with outsized premium relative to delta.",
            }
        return {
            "opportunity": False,
            "description": "IV Rank normal. No special IV crush opportunity.",
        }

    def _analyze_prev_close(self, snap: MarketSnapshot) -> Dict[str, Any]:
        if snap.prev_high <= snap.prev_low or snap.prev_close <= 0:
            return {"position": "unknown", "bias": "neutral"}

        range_pct = (snap.prev_close - snap.prev_low) / (snap.prev_high - snap.prev_low)

        if range_pct > 0.75:
            return {
                "position": "near_highs",
                "range_pct": round(range_pct * 100, 1),
                "bias": "slight_bearish",
                "description": "Closed near highs — possible exhaustion. Watch for fade.",
            }
        elif range_pct < 0.25:
            return {
                "position": "near_lows",
                "range_pct": round(range_pct * 100, 1),
                "bias": "slight_bullish",
                "description": "Closed near lows — possible bounce. Fear premium elevated.",
            }
        return {
            "position": "middle",
            "range_pct": round(range_pct * 100, 1),
            "bias": "neutral",
            "description": "Closed mid-range — balanced. Good for iron condors.",
        }

    def _calculate_key_levels(self, snap: MarketSnapshot) -> Dict[str, float]:
        S = snap.spx_price
        em = snap.expected_move_1d

        return {
            "resistance_1": round(snap.prev_high, 2),
            "resistance_2": round(S + em, 2),
            "resistance_3": round(S + em * 1.5, 2),
            "support_1": round(snap.prev_low, 2),
            "support_2": round(S - em, 2),
            "support_3": round(S - em * 1.5, 2),
            "pivot": round((snap.prev_high + snap.prev_low + snap.prev_close) / 3, 2),
        }

    def _generate_trade_plan(self, snap: MarketSnapshot, analysis: Dict) -> Dict[str, Any]:
        """Generate scenario-based trade plan."""
        S = snap.spx_price
        em = snap.expected_move_1d
        levels = analysis["key_levels"]

        # Base plan
        base_plan = {
            "entry_time": "9:45 — 10:30 AM (after opening volatility settles)",
            "strategy": "0DTE SPX Iron Condor" if analysis["gap_strategy"]["size"] == "none"
                        else "0DTE SPX Put Credit Spread",
        }

        # Bull scenario
        bull = {
            "trigger": f"SPX opens above {S:.0f} and holds above {levels['resistance_1']:.0f}",
            "strategy": "Put credit spread at 0.10 delta",
            "strikes": f"Short put near {S - em:.0f}",
            "target": "Let expire worthless or close at 50% profit",
        }

        # Bear scenario
        bear = {
            "trigger": f"SPX breaks below {levels['support_1']:.0f}",
            "strategy": "Call credit spread at 0.10 delta (or sit out)",
            "strikes": f"Short call near {S + em:.0f}",
            "target": "Tighter profit target — close at 40% profit",
        }

        # Neutral scenario
        neutral = {
            "trigger": f"SPX stays between {levels['support_1']:.0f} — {levels['resistance_1']:.0f}",
            "strategy": "Iron condor at 0.12 delta both sides",
            "strikes": f"Put side: {S - em:.0f}/{S - em - 5:.0f} | Call side: {S + em:.0f}/{S + em + 5:.0f}",
            "target": "Close at 50% profit or let expire",
        }

        return {
            "base": base_plan,
            "bull_scenario": bull,
            "bear_scenario": bear,
            "neutral_scenario": neutral,
        }

    # ── Report Output ──────────────────────────────────────────

    def format_report(self, result: Dict, snap: MarketSnapshot) -> str:
        lines = []
        lines.append(header(
            "JANE STREET PRE-MARKET EDGE ANALYZER",
            f"Morning Briefing | {datetime.now():%Y-%m-%d %H:%M} | SPX Futures: {snap.spx_futures_price:,.2f}"
        ))

        # Overnight
        o = result["overnight"]
        lines.append(sub_header("OVERNIGHT FUTURES"))
        lines.append(kv("Movement", f"{o['change']:+.1f} pts ({o['pct_of_em']:.0f}% of EM)",
                         C.GREEN if o["change"] > 0 else C.RED if o["change"] < 0 else ""))
        lines.append(kv("Direction", o["direction"].replace("_", " ").upper()))
        lines.append(f"  {C.DIM}{o['thesis']}{C.RESET}")

        # IV
        iv = result["iv_analysis"]
        lines.append(sub_header("PRE-MARKET VOLATILITY"))
        lines.append(kv("VIX", f"{iv['vix']:.2f}"))
        lines.append(kv("IV Rank", f"{iv['iv_rank']:.1f}%",
                         C.GREEN if iv["iv_rank"] > 50 else C.YELLOW))
        lines.append(kv("20-Day RV", f"{iv['realized_vol']:.2f}"))
        lines.append(kv("IV Premium", f"{iv['iv_premium']:+.2f}",
                         C.GREEN if iv["iv_premium"] > 2 else C.RED if iv["iv_premium"] < -2 else ""))
        lines.append(f"  {C.DIM}{iv['description']}{C.RESET}")

        # Economic calendar
        ec = result["economic_impact"]
        lines.append(sub_header("ECONOMIC CALENDAR"))
        risk_color = C.GREEN if ec["risk_level"] == "low" else C.YELLOW if ec["risk_level"] in ("moderate", "elevated") else C.RED
        lines.append(kv("Risk Level", ec["risk_level"].upper(), risk_color))
        for ev in ec.get("events", []):
            lines.append(f"  📅 {ev}")
        lines.append(f"  {C.DIM}{ec['strategy_impact']}{C.RESET}")

        # Earnings
        ee = result["earnings_exposure"]
        lines.append(sub_header("EARNINGS EXPOSURE"))
        lines.append(kv("Exposure Level", ee["exposure"].upper()))
        for co in ee.get("companies", []):
            lines.append(f"  📊 {co}")
        lines.append(f"  {C.DIM}{ee['impact']}{C.RESET}")

        # Globex range
        gr = result["globex_range"]
        lines.append(sub_header("GLOBEX RANGE"))
        lines.append(kv("High", f"{gr['high']:,.2f}"))
        lines.append(kv("Low", f"{gr['low']:,.2f}"))
        lines.append(kv("Range", f"{gr['range']:.1f} pts ({gr['range_vs_em']:.0f}% of EM)"))

        # Gap strategy
        gs = result["gap_strategy"]
        lines.append(sub_header("OPENING GAP STRATEGY"))
        lines.append(kv("Gap Size", gs["size"].upper()))
        lines.append(f"  {C.DIM}{gs['strategy']}{C.RESET}")
        if "action" in gs:
            lines.append(f"  {C.BOLD}→ {gs['action']}{C.RESET}")

        # IV crush
        ivc = result["iv_crush"]
        if ivc["opportunity"]:
            lines.append(sub_header("IV CRUSH OPPORTUNITY"))
            lines.append(f"  {C.GREEN}✅ {ivc['description']}{C.RESET}")
            lines.append(f"  {C.BOLD}→ {ivc['action']}{C.RESET}")

        # Previous close
        pc = result["prev_close_analysis"]
        lines.append(sub_header("PREVIOUS CLOSE"))
        lines.append(kv("Close Position", f"{pc.get('range_pct', 0):.0f}% of range ({pc['position']})"))
        lines.append(kv("Directional Bias", pc.get("bias", "neutral").replace("_", " ").upper()))
        lines.append(f"  {C.DIM}{pc.get('description', '')}{C.RESET}")

        # Key levels
        kl = result["key_levels"]
        lines.append(sub_header("KEY LEVELS FOR TODAY"))
        lines.append(kv("Resistance 3", f"{kl['resistance_3']:,.2f}", C.RED))
        lines.append(kv("Resistance 2 (EM+)", f"{kl['resistance_2']:,.2f}", C.RED))
        lines.append(kv("Resistance 1 (Prev High)", f"{kl['resistance_1']:,.2f}", C.RED))
        lines.append(kv("Pivot", f"{kl['pivot']:,.2f}", C.YELLOW))
        lines.append(kv("Support 1 (Prev Low)", f"{kl['support_1']:,.2f}", C.GREEN))
        lines.append(kv("Support 2 (EM-)", f"{kl['support_2']:,.2f}", C.GREEN))
        lines.append(kv("Support 3", f"{kl['support_3']:,.2f}", C.GREEN))

        # Trade plan scenarios
        tp = result["trade_plan"]
        lines.append(sub_header("PRE-MARKET TRADE PLAN"))
        lines.append(kv("Entry Window", tp["base"]["entry_time"]))
        lines.append(kv("Base Strategy", tp["base"]["strategy"]))

        for scenario_name, label in [("bull_scenario", "🟢 BULL"), ("bear_scenario", "🔴 BEAR"), ("neutral_scenario", "⚪ NEUTRAL")]:
            s = tp[scenario_name]
            lines.append(f"\n  {C.BOLD}{label} SCENARIO:{C.RESET}")
            lines.append(f"    Trigger: {s['trigger']}")
            lines.append(f"    Strategy: {s['strategy']}")
            lines.append(f"    Strikes: {s['strikes']}")
            lines.append(f"    Target: {s['target']}")

        lines.append(f"\n{C.CYAN}{'═' * 78}{C.RESET}\n")
        return "\n".join(lines)

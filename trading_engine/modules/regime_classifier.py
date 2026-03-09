"""
Module 2: Citadel Market Regime Classifier
==========================================
Classifies market conditions into GREEN/YELLOW/RED regimes before placing any trade.
"""

from datetime import datetime
from typing import Dict, Any

from ..models import (
    MarketSnapshot, MarketRegime, VIXRegime, TrendState, RegimeReport
)
from ..config import EngineConfig
from ..formatters import header, sub_header, kv, verdict_badge, C


class RegimeClassifier:
    """
    Citadel-style market regime classification.
    Analyzes 10 factors to determine if conditions are safe for selling premium.
    """

    def __init__(self, config: EngineConfig):
        self.config = config

    def classify(self, snap: MarketSnapshot) -> RegimeReport:
        """Run full regime classification."""
        report = RegimeReport(timestamp=datetime.now())

        report.vix_regime = self._classify_vix(snap.vix_level)
        report.vix_term_structure = self._classify_term_structure(snap)
        report.trend_state = self._classify_trend(snap)
        report.rv_vs_iv = self._classify_rv_iv(snap)
        report.correlation_regime = self._classify_correlation(snap)
        report.put_call_signal = self._classify_put_call(snap)
        report.breadth_signal = self._classify_breadth(snap)
        report.event_risk = self._classify_event_risk(snap)
        report.verdict = self._compute_verdict(report, snap)
        report.strategy_recommendation = self._recommend_strategy(report, snap)
        report.details = self._build_details(report, snap)

        return report

    # ── Factor Classification ──────────────────────────────────

    def _classify_vix(self, vix: float) -> VIXRegime:
        if vix < 15:
            return VIXRegime.LOW
        elif vix < 20:
            return VIXRegime.NORMAL
        elif vix < 30:
            return VIXRegime.ELEVATED
        return VIXRegime.CRISIS

    def _classify_term_structure(self, snap: MarketSnapshot) -> str:
        if snap.vix_futures_front and snap.vix_futures_second:
            if snap.vix_futures_front < snap.vix_futures_second:
                return "contango"
            elif snap.vix_futures_front > snap.vix_futures_second * 1.02:
                return "backwardation"
            return "flat"
        return snap.vix_term_structure or "contango"

    def _classify_trend(self, snap: MarketSnapshot) -> TrendState:
        if snap.spx_price <= 0 or snap.prev_close <= 0:
            return TrendState.RANGE_BOUND

        daily_return = (snap.spx_price - snap.prev_close) / snap.prev_close * 100
        range_position = 0.5
        if snap.prev_high > snap.prev_low:
            range_position = (snap.spx_price - snap.prev_low) / (snap.prev_high - snap.prev_low)

        if daily_return > 0.5 and range_position > 0.7:
            return TrendState.STRONG_UPTREND
        elif daily_return > 0.2:
            return TrendState.WEAK_UPTREND
        elif daily_return < -0.5 and range_position < 0.3:
            return TrendState.STRONG_DOWNTREND
        elif daily_return < -0.2:
            return TrendState.WEAK_DOWNTREND
        return TrendState.RANGE_BOUND

    def _classify_rv_iv(self, snap: MarketSnapshot) -> str:
        rv = snap.realized_vol_20d
        iv = snap.implied_vol_30d
        if rv <= 0 or iv <= 0:
            return "neutral"
        ratio = iv / rv
        if ratio > 1.2:
            return "IV_overpriced"
        elif ratio < 0.85:
            return "IV_underpriced"
        return "neutral"

    def _classify_correlation(self, snap: MarketSnapshot) -> str:
        ad = snap.advance_decline_ratio
        if ad > 3 or ad < 0.33:
            return "high"
        elif 0.7 < ad < 1.5:
            return "normal"
        return "low"

    def _classify_put_call(self, snap: MarketSnapshot) -> str:
        pcr = snap.put_call_ratio
        if pcr > 1.2:
            return "extreme_fear"
        elif pcr > 0.9:
            return "elevated_fear"
        elif pcr < 0.6:
            return "complacent"
        return "neutral"

    def _classify_breadth(self, snap: MarketSnapshot) -> str:
        ad = snap.advance_decline_ratio
        nh_nl = snap.new_highs - snap.new_lows
        if ad > 2.0 and nh_nl > 100:
            return "strong_bullish"
        elif ad > 1.2:
            return "bullish"
        elif ad < 0.5 and nh_nl < -100:
            return "strong_bearish"
        elif ad < 0.8:
            return "bearish"
        return "neutral"

    def _classify_event_risk(self, snap: MarketSnapshot) -> str:
        score = 0
        if snap.is_fed_day:
            score += 3
        if snap.is_cpi_day:
            score += 2
        if snap.is_opex_day:
            score += 1
        if len(snap.major_earnings_today) >= 3:
            score += 2
        elif len(snap.major_earnings_today) >= 1:
            score += 1
        if len(snap.economic_events) >= 3:
            score += 1
        if score >= 3:
            return "high"
        elif score >= 1:
            return "medium"
        return "low"

    def _compute_verdict(self, report: RegimeReport, snap: MarketSnapshot) -> MarketRegime:
        score = 0

        vix_scores = {VIXRegime.LOW: -1, VIXRegime.NORMAL: 2, VIXRegime.ELEVATED: 1, VIXRegime.CRISIS: -3}
        score += vix_scores.get(report.vix_regime, 0)

        if report.vix_term_structure == "contango":
            score += 2
        elif report.vix_term_structure == "backwardation":
            score -= 3

        trend_scores = {
            TrendState.RANGE_BOUND: 3, TrendState.WEAK_UPTREND: 1, TrendState.WEAK_DOWNTREND: 1,
            TrendState.STRONG_UPTREND: -1, TrendState.STRONG_DOWNTREND: -2,
        }
        score += trend_scores.get(report.trend_state, 0)

        if report.rv_vs_iv == "IV_overpriced":
            score += 2
        elif report.rv_vs_iv == "IV_underpriced":
            score -= 2

        if report.put_call_signal == "extreme_fear":
            score += 1
        elif report.put_call_signal == "complacent":
            score -= 1

        if report.event_risk == "high":
            score -= 2
        elif report.event_risk == "medium":
            score -= 1

        if report.vix_term_structure == "backwardation" and report.vix_regime == VIXRegime.CRISIS:
            return MarketRegime.RED

        if score >= 4:
            return MarketRegime.GREEN
        elif score >= 0:
            return MarketRegime.YELLOW
        return MarketRegime.RED

    def _recommend_strategy(self, report: RegimeReport, snap: MarketSnapshot) -> str:
        if report.verdict == MarketRegime.RED:
            return "SIT IN CASH. Conditions hostile for premium sellers. Buy hedges or stay flat."

        parts = []
        if report.verdict == MarketRegime.GREEN:
            if report.trend_state == TrendState.RANGE_BOUND:
                parts.append("Iron Condors on SPX (0DTE or weekly)")
                parts.append("Short strangles on high-IV ETFs")
            elif report.trend_state in (TrendState.WEAK_UPTREND, TrendState.WEAK_DOWNTREND):
                parts.append("Directional credit spreads (sell against trend)")
                parts.append("Jade lizards for extra premium")
            if report.rv_vs_iv == "IV_overpriced":
                parts.append("IV crush plays — sell overpriced expiration")
            if report.put_call_signal == "extreme_fear":
                parts.append("Aggressive put selling — fear premium elevated")
        else:
            parts.append("Put credit spreads only (one-sided, not iron condors)")
            parts.append("Wider wings (10pt spreads)")
            parts.append("Reduced position size (50% normal)")
            if report.event_risk != "low":
                parts.append("Wait until after economic data release")

        return " | ".join(parts)

    def _build_details(self, report: RegimeReport, snap: MarketSnapshot) -> dict:
        rv_pct = snap.realized_vol_20d * 100 if snap.realized_vol_20d < 1 else snap.realized_vol_20d
        iv_pct = snap.implied_vol_30d * 100 if snap.implied_vol_30d < 1 else snap.implied_vol_30d
        vix_descs = {
            VIXRegime.LOW: "Premium thin. Limited edge. Consider tighter strikes or sitting out.",
            VIXRegime.NORMAL: "Sweet spot. Good premium with manageable risk.",
            VIXRegime.ELEVATED: "Rich premium but movement is real. Wider strikes, smaller size.",
            VIXRegime.CRISIS: "DANGER. Tail risk extreme. Do NOT sell naked premium.",
        }
        ts_descs = {
            "contango": "Normal — VIX futures above spot. Safe to sell.",
            "backwardation": "⚠️ BACKWARDATION — Near-term fear exceeds long-term. Major red flag.",
            "flat": "Flat — neutral signal.",
        }
        trend_descs = {
            TrendState.STRONG_UPTREND: "Strong bull. Call spreads risky. Favor put credit spreads.",
            TrendState.WEAK_UPTREND: "Mild bullish. Iron condors OK with upward-skewed breakevens.",
            TrendState.RANGE_BOUND: "Range-bound. Ideal for iron condors and strangles.",
            TrendState.WEAK_DOWNTREND: "Mild bearish. Iron condors OK with downward-skewed breakevens.",
            TrendState.STRONG_DOWNTREND: "Strong selloff. Put spreads risky. Favor call credit spreads.",
        }
        rv_iv_descs = {
            "IV_overpriced": f"IV ({iv_pct:.1f}%) > RV ({rv_pct:.1f}%) — Options overpriced. Edge for sellers.",
            "IV_underpriced": f"IV ({iv_pct:.1f}%) < RV ({rv_pct:.1f}%) — Options underpriced. Danger zone.",
            "neutral": f"IV ({iv_pct:.1f}%) ≈ RV ({rv_pct:.1f}%) — Fair pricing.",
        }
        return {
            "vix_regime_desc": vix_descs.get(report.vix_regime, ""),
            "term_structure_desc": ts_descs.get(report.vix_term_structure, ""),
            "trend_desc": trend_descs.get(report.trend_state, ""),
            "rv_iv_desc": rv_iv_descs.get(report.rv_vs_iv, ""),
        }

    # ── Report Output ──────────────────────────────────────────

    def format_report(self, report: RegimeReport, snap: MarketSnapshot) -> str:
        lines = []
        lines.append(header(
            "CITADEL MARKET REGIME REPORT",
            f"{report.timestamp:%Y-%m-%d %H:%M} | SPX {snap.spx_price:,.2f} | VIX {snap.vix_level:.2f}"
        ))

        lines.append(sub_header("REGIME DASHBOARD"))
        lines.append(f"\n  {verdict_badge(report.verdict.value)}\n")
        lines.append(kv("VIX Regime", f"{report.vix_regime.value} ({snap.vix_level:.2f})",
                         C.GREEN if report.vix_regime == VIXRegime.NORMAL else
                         C.YELLOW if report.vix_regime == VIXRegime.ELEVATED else
                         C.RED if report.vix_regime == VIXRegime.CRISIS else C.BLUE))
        lines.append(kv("VIX Term Structure", report.vix_term_structure.upper(),
                         C.GREEN if report.vix_term_structure == "contango" else C.RED))
        lines.append(kv("Trend", report.trend_state.value,
                         C.GREEN if report.trend_state == TrendState.RANGE_BOUND else C.YELLOW))
        lines.append(kv("RV vs IV", report.rv_vs_iv.replace("_", " "),
                         C.GREEN if report.rv_vs_iv == "IV_overpriced" else
                         C.RED if report.rv_vs_iv == "IV_underpriced" else ""))
        lines.append(kv("Correlation", report.correlation_regime))
        lines.append(kv("Put/Call", report.put_call_signal.replace("_", " ")))
        lines.append(kv("Breadth", report.breadth_signal.replace("_", " ")))
        lines.append(kv("Event Risk", report.event_risk.upper(),
                         C.GREEN if report.event_risk == "low" else
                         C.YELLOW if report.event_risk == "medium" else C.RED))

        d = report.details
        lines.append(sub_header("DETAILED ANALYSIS"))
        lines.append(f"\n  {C.BOLD}VIX:{C.RESET} {d.get('vix_regime_desc', '')}")
        lines.append(f"  {C.BOLD}Term Structure:{C.RESET} {d.get('term_structure_desc', '')}")
        lines.append(f"  {C.BOLD}Trend:{C.RESET} {d.get('trend_desc', '')}")
        lines.append(f"  {C.BOLD}RV/IV:{C.RESET} {d.get('rv_iv_desc', '')}")

        if snap.expected_move_1d > 0:
            lines.append(f"\n  {C.BOLD}Expected Move Today:{C.RESET} ±{snap.expected_move_1d:.1f} pts "
                          f"({snap.spx_price - snap.expected_move_1d:.0f} — {snap.spx_price + snap.expected_move_1d:.0f})")

        if snap.economic_events or snap.major_earnings_today:
            lines.append(sub_header("EVENT CALENDAR"))
            for ev in snap.economic_events:
                lines.append(f"  📅 {ev}")
            for e in snap.major_earnings_today:
                lines.append(f"  📊 Earnings: {e}")
            if snap.is_fed_day:
                lines.append(f"  {C.RED}🏛️  FED DAY{C.RESET}")
            if snap.is_cpi_day:
                lines.append(f"  {C.YELLOW}📈 CPI Release{C.RESET}")

        lines.append(sub_header("STRATEGY RECOMMENDATION"))
        lines.append(f"\n  {report.strategy_recommendation}")
        lines.append(f"\n{C.CYAN}{'═' * 78}{C.RESET}\n")
        return "\n".join(lines)

"""
Module 8: Akuna Capital Volatility Skew Exploiter
==================================================
Profits from volatility skew — OTM puts priced more expensively than equivalent calls.
"""

from datetime import datetime, date
from typing import Dict, Any, Optional, List

from ..models import MarketSnapshot, OptionsChain, OptionContract, SpreadType
from ..config import EngineConfig
from ..market_data import MarketDataProvider
from ..black_scholes import bs_delta, implied_vol
from ..formatters import header, sub_header, kv, table, bar, C


class SkewExploiter:
    """
    Akuna Capital-style volatility skew analysis and exploitation.
    """

    def __init__(self, config: EngineConfig, data_provider: MarketDataProvider):
        self.config = config
        self.data = data_provider

    def analyze(self, snap: MarketSnapshot, underlying: str = "SPX",
                chain: Optional[OptionsChain] = None) -> Dict[str, Any]:
        """Run complete skew analysis."""
        result = {"underlying": underlying, "timestamp": datetime.now()}

        S = snap.spx_price if underlying == "SPX" else snap.spy_price
        if chain is None:
            chain = self.data.get_options_chain(underlying, date.today())

        # 1. Current skew measurement
        skew_data = self._measure_skew(chain, S)
        result["skew"] = skew_data

        # 2. Skew percentile (based on typical ranges)
        result["skew_percentile"] = self._skew_percentile(skew_data)

        # 3. Skew signal
        result["signal"] = self._skew_signal(skew_data, result["skew_percentile"])

        # 4. Strategy recommendations
        result["strategies"] = self._recommend_strategies(result, snap, chain, S)

        # 5. Term structure skew comparison
        result["term_structure"] = self._term_structure_skew(snap)

        # 6. Risk of skew expansion
        result["expansion_risk"] = self._expansion_risk(snap, result)

        return result

    def _measure_skew(self, chain: OptionsChain, S: float) -> Dict[str, Any]:
        """Measure IV difference between OTM puts and calls at same delta."""
        if not chain.puts or not chain.calls:
            iv = 0.18
            return {
                "put_iv_25d": round(iv * 1.15, 4),
                "call_iv_25d": round(iv * 0.95, 4),
                "put_iv_10d": round(iv * 1.25, 4),
                "call_iv_10d": round(iv * 0.90, 4),
                "skew_25d": round(iv * 0.20, 4),
                "skew_10d": round(iv * 0.35, 4),
                "atm_iv": round(iv, 4),
            }

        # Find contracts at specific deltas
        put_25d = self._find_at_delta(chain.puts, 0.25)
        put_10d = self._find_at_delta(chain.puts, 0.10)
        call_25d = self._find_at_delta(chain.calls, 0.25)
        call_10d = self._find_at_delta(chain.calls, 0.10)
        atm = self._find_at_delta(chain.calls, 0.50)

        put_iv_25 = put_25d.greeks.iv if put_25d else 0
        put_iv_10 = put_10d.greeks.iv if put_10d else 0
        call_iv_25 = call_25d.greeks.iv if call_25d else 0
        call_iv_10 = call_10d.greeks.iv if call_10d else 0
        atm_iv = atm.greeks.iv if atm else 0

        return {
            "put_iv_25d": round(put_iv_25, 4),
            "call_iv_25d": round(call_iv_25, 4),
            "put_iv_10d": round(put_iv_10, 4),
            "call_iv_10d": round(call_iv_10, 4),
            "skew_25d": round(put_iv_25 - call_iv_25, 4),
            "skew_10d": round(put_iv_10 - call_iv_10, 4),
            "atm_iv": round(atm_iv, 4),
        }

    def _find_at_delta(self, contracts: List[OptionContract], target_delta: float) -> Optional[OptionContract]:
        """Find contract closest to target delta."""
        if not contracts:
            return None
        return min(contracts, key=lambda c: abs(abs(c.greeks.delta) - target_delta))

    def _skew_percentile(self, skew_data: Dict) -> Dict[str, float]:
        """Estimate skew percentile based on historical norms."""
        # SPX typical 25d skew ranges: 2-8% IV points
        skew_25 = skew_data.get("skew_25d", 0)
        atm = skew_data.get("atm_iv", 0.18)

        # Normalize to percentage of ATM IV
        if atm > 0:
            skew_ratio = skew_25 / atm * 100
        else:
            skew_ratio = 0

        # Map to percentile (rough calibration for SPX)
        if skew_ratio > 30:
            pct = 95
        elif skew_ratio > 20:
            pct = 80
        elif skew_ratio > 10:
            pct = 50
        elif skew_ratio > 5:
            pct = 30
        else:
            pct = 10

        return {
            "percentile": pct,
            "skew_ratio": round(skew_ratio, 1),
            "regime": "steep" if pct > 75 else "normal" if pct > 25 else "flat",
        }

    def _skew_signal(self, skew_data: Dict, percentile: Dict) -> Dict[str, str]:
        """Generate trading signal from skew analysis."""
        regime = percentile["regime"]

        signals = {
            "steep": {
                "signal": "steep_fear",
                "description": "Put skew is steep — market pricing crash protection aggressively",
                "edge": "Sell OTM puts — they're overpriced relative to calls",
                "risk": "A real crash would make skew even steeper",
            },
            "normal": {
                "signal": "neutral",
                "description": "Skew is normal — no particular edge from skew",
                "edge": "Standard premium-selling strategies apply",
                "risk": "Monitor for skew expansion on selloffs",
            },
            "flat": {
                "signal": "flat_complacent",
                "description": "Skew is flat — unusual complacency in put pricing",
                "edge": "Buy cheap put protection. Call skew may offer selling opportunity.",
                "risk": "Flat skew can precede sharp moves — puts are cheap for a reason... or not",
            },
        }

        return signals.get(regime, signals["normal"])

    def _recommend_strategies(self, result: Dict, snap: MarketSnapshot,
                               chain: OptionsChain, S: float) -> List[Dict[str, Any]]:
        """Generate specific strategy recommendations based on skew."""
        strategies = []
        signal = result["signal"]["signal"]
        skew = result["skew"]

        if signal == "steep_fear":
            # Put skew is rich — sell puts
            strategies.append({
                "name": "Rich Put Credit Spread",
                "type": SpreadType.PUT_CREDIT,
                "description": "Sell OTM put spread to harvest inflated put premium",
                "short_delta": 0.12,
                "width": 5,
                "edge": f"Put IV ({skew['put_iv_25d']*100:.1f}%) >> Call IV ({skew['call_iv_25d']*100:.1f}%)",
                "priority": "HIGH",
            })

            # Jade lizard — sell put + call spread to eliminate upside risk
            strategies.append({
                "name": "Jade Lizard",
                "type": SpreadType.JADE_LIZARD,
                "description": "Sell OTM put + OTM call spread. No upside risk if call spread credit > put width.",
                "short_put_delta": 0.15,
                "short_call_delta": 0.10,
                "call_spread_width": 10,
                "edge": "Steep put skew funds the call spread — combined credit eliminates upside risk",
                "priority": "HIGH",
            })

        elif signal == "flat_complacent":
            # Puts are cheap — buy protection or sell calls
            strategies.append({
                "name": "Call Credit Spread",
                "type": SpreadType.CALL_CREDIT,
                "description": "Sell call spreads where skew is NOT protecting you",
                "short_delta": 0.12,
                "width": 5,
                "edge": "Flat skew means calls are relatively richer than usual",
                "priority": "MEDIUM",
            })

            strategies.append({
                "name": "Long OTM Put (Hedge)",
                "type": "LONG_PUT",
                "description": "Buy cheap OTM puts as portfolio insurance while skew is flat",
                "delta": 0.05,
                "edge": "Puts are cheap — tail risk protection at a discount",
                "priority": "HIGH",
            })

        # Skew mean-reversion trade
        pct = result["skew_percentile"]["percentile"]
        if pct > 85:
            strategies.append({
                "name": "Skew Mean-Reversion (Short Skew)",
                "type": "RATIO_SPREAD",
                "description": "Sell 2 OTM puts, buy 1 ATM put. Profits if skew normalizes.",
                "edge": f"Skew at {pct}th percentile — historically mean-reverts from here",
                "risk": "Loses if skew steepens further (crash scenario)",
                "priority": "MEDIUM",
            })
        elif pct < 15:
            strategies.append({
                "name": "Skew Mean-Reversion (Long Skew)",
                "type": "RATIO_SPREAD",
                "description": "Buy OTM puts, sell ATM puts. Profits if skew steepens.",
                "edge": f"Skew at {pct}th percentile — unusually flat, expect steepening",
                "priority": "MEDIUM",
            })

        # Broken wing butterfly
        strategies.append({
            "name": "Broken Wing Butterfly",
            "type": SpreadType.BROKEN_WING_BUTTERFLY,
            "description": "Asymmetric butterfly that profits from skew normalization + direction",
            "construction": "Buy 1 ATM put, sell 2 OTM puts, buy 1 far OTM put (skip strike on downside)",
            "edge": "Positive theta + profits if skew normalizes",
            "priority": "LOW",
        })

        return strategies

    def _term_structure_skew(self, snap: MarketSnapshot) -> Dict[str, Any]:
        """Compare skew between expirations for calendar opportunities."""
        return {
            "weekly_vs_monthly": "Weekly skew is typically steeper than monthly — "
                                 "consider selling weekly puts and buying monthly puts for calendar skew trade",
            "front_month_iv": round(snap.vix_level, 2),
            "opportunity": snap.vix_level > 20,  # More calendar opportunity in higher IV
        }

    def _expansion_risk(self, snap: MarketSnapshot, result: Dict) -> Dict[str, Any]:
        """Assess risk of further skew expansion."""
        risk_factors = []
        risk_score = 0

        if snap.vix_level > 25:
            risk_factors.append("Elevated VIX — skew can steepen further in selloffs")
            risk_score += 2
        if snap.put_call_ratio > 1.0:
            risk_factors.append("High put/call ratio — demand for put protection already elevated")
            risk_score += 1
        if snap.is_fed_day or snap.is_cpi_day:
            risk_factors.append("Event risk today — binary outcomes can spike skew")
            risk_score += 2
        if result["skew_percentile"]["percentile"] > 80:
            risk_factors.append("Skew already steep — could steepen further in panic")
            risk_score += 1

        return {
            "risk_level": "high" if risk_score >= 4 else "moderate" if risk_score >= 2 else "low",
            "factors": risk_factors,
            "recommendation": "Size down on skew-selling strategies" if risk_score >= 3 else "Normal sizing OK",
        }

    # ── Report Output ──────────────────────────────────────────

    def format_report(self, result: Dict, snap: MarketSnapshot) -> str:
        lines = []
        lines.append(header(
            "AKUNA CAPITAL VOLATILITY SKEW ANALYSIS",
            f"{result['underlying']} | {datetime.now():%Y-%m-%d %H:%M}"
        ))

        # Current skew
        skew = result["skew"]
        lines.append(sub_header("CURRENT SKEW MEASUREMENT"))
        lines.append(kv("25Δ Put IV", f"{skew['put_iv_25d']*100:.1f}%", C.RED))
        lines.append(kv("25Δ Call IV", f"{skew['call_iv_25d']*100:.1f}%", C.GREEN))
        lines.append(kv("25Δ Skew", f"{skew['skew_25d']*100:.1f}% IV points",
                         C.RED if skew["skew_25d"] > 0.05 else ""))
        lines.append(kv("10Δ Put IV", f"{skew['put_iv_10d']*100:.1f}%", C.RED))
        lines.append(kv("10Δ Call IV", f"{skew['call_iv_10d']*100:.1f}%", C.GREEN))
        lines.append(kv("10Δ Skew", f"{skew['skew_10d']*100:.1f}% IV points"))
        lines.append(kv("ATM IV", f"{skew['atm_iv']*100:.1f}%"))

        # Skew percentile
        pctile = result["skew_percentile"]
        lines.append(sub_header("SKEW PERCENTILE"))
        pct_color = C.RED if pctile["percentile"] > 75 else C.GREEN if pctile["percentile"] < 25 else C.YELLOW
        lines.append(kv("Percentile", f"{pctile['percentile']}th", pct_color))
        lines.append(kv("Regime", pctile["regime"].upper(), pct_color))
        lines.append(f"  {bar(pctile['percentile'], 100, 40)} {pctile['percentile']}%")

        # Signal
        sig = result["signal"]
        lines.append(sub_header("SKEW SIGNAL"))
        lines.append(kv("Signal", sig["signal"].replace("_", " ").upper()))
        lines.append(f"  {sig['description']}")
        lines.append(f"  {C.GREEN}Edge:{C.RESET} {sig['edge']}")
        lines.append(f"  {C.RED}Risk:{C.RESET} {sig['risk']}")

        # Strategy recommendations
        lines.append(sub_header("STRATEGY RECOMMENDATIONS"))
        for i, strat in enumerate(result["strategies"], 1):
            priority = strat.get("priority", "MEDIUM")
            p_color = C.GREEN if priority == "HIGH" else C.YELLOW if priority == "MEDIUM" else C.DIM
            lines.append(f"\n  {C.BOLD}{i}. {strat['name']}{C.RESET} [{p_color}{priority}{C.RESET}]")
            lines.append(f"     {strat['description']}")
            if "edge" in strat:
                lines.append(f"     {C.GREEN}Edge:{C.RESET} {strat['edge']}")
            if "risk" in strat:
                lines.append(f"     {C.RED}Risk:{C.RESET} {strat['risk']}")
            if "construction" in strat:
                lines.append(f"     Construction: {strat['construction']}")

        # Expansion risk
        exp = result["expansion_risk"]
        lines.append(sub_header("SKEW EXPANSION RISK"))
        risk_color = C.RED if exp["risk_level"] == "high" else C.YELLOW if exp["risk_level"] == "moderate" else C.GREEN
        lines.append(kv("Risk Level", exp["risk_level"].upper(), risk_color))
        for f in exp["factors"]:
            lines.append(f"  ⚠️  {f}")
        lines.append(f"  → {exp['recommendation']}")

        lines.append(f"\n{C.CYAN}{'═' * 78}{C.RESET}\n")
        return "\n".join(lines)

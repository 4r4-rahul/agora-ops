"""
Module 10: IMC Trading Earnings Theta Crusher
==============================================
Systematically sells options before earnings to profit from IV crush.
"""

from datetime import datetime, date, timedelta
from typing import Dict, Any, List, Optional

from ..models import MarketSnapshot, OptionsChain, SpreadType, TradeTicket
from ..config import EngineConfig
from ..market_data import MarketDataProvider
from ..black_scholes import expected_move, strike_at_delta, prob_between
from ..formatters import header, sub_header, kv, table, bar, pnl, C


class EarningsCrusher:
    """
    IMC Trading-style earnings IV crush strategy.
    Sells premium before earnings to capture the predictable IV collapse.
    """

    def __init__(self, config: EngineConfig, data_provider: MarketDataProvider):
        self.config = config
        self.data = data_provider

    def analyze(self, ticker: str, earnings_date: date,
                current_iv: float, stock_price: float,
                directional_bias: str = "neutral",
                snap: Optional[MarketSnapshot] = None) -> Dict[str, Any]:
        """
        Complete earnings IV crush analysis.

        Args:
            ticker: Stock symbol
            earnings_date: Date of earnings announcement
            current_iv: Current IV (as percentage, e.g., 65 for 65%)
            stock_price: Current stock price
            directional_bias: "neutral", "bullish", or "bearish"
        """
        iv = current_iv / 100 if current_iv > 1 else current_iv
        r = 0.045
        days_to_earnings = (earnings_date - date.today()).days
        T = max(days_to_earnings / 365, 1/365)

        result = {
            "ticker": ticker,
            "stock_price": stock_price,
            "earnings_date": earnings_date,
            "days_to_earnings": days_to_earnings,
            "current_iv": round(iv * 100, 1),
            "directional_bias": directional_bias,
        }

        # 1. Pre-earnings IV expansion timeline
        result["iv_expansion"] = self._iv_expansion_timeline(ticker, days_to_earnings, iv)

        # 2. Optimal entry timing
        result["entry_timing"] = self._optimal_entry(days_to_earnings)

        # 3. Historical IV crush estimate
        result["iv_crush"] = self._estimate_iv_crush(ticker, iv)

        # 4. Strategy selection
        result["strategy"] = self._select_strategy(directional_bias, iv, stock_price)

        # 5. Strike placement using expected move
        em = expected_move(stock_price, iv, T)
        result["expected_move"] = {
            "dollars": round(em, 2),
            "percent": round(em / stock_price * 100, 2),
            "range_low": round(stock_price - em, 2),
            "range_high": round(stock_price + em, 2),
        }

        # 6. Trade setup
        result["trade_setup"] = self._build_trade_setup(
            ticker, stock_price, iv, em, T, r, directional_bias, earnings_date
        )

        # 7. Premium analysis
        result["premium_analysis"] = self._analyze_premium(
            stock_price, iv, em, result["iv_crush"]
        )

        # 8. Position sizing
        result["position_sizing"] = self._position_size(result)

        # 9. Post-earnings management
        result["exit_protocol"] = self._exit_protocol()

        # 10. Upcoming earnings calendar
        result["upcoming"] = self._upcoming_opportunities()

        return result

    def _iv_expansion_timeline(self, ticker: str, days_out: int, current_iv: float) -> Dict[str, Any]:
        """Model how IV typically inflates before earnings."""
        # Standard IV expansion model: IV starts rising ~10 days before
        phases = []

        if days_out > 10:
            phases.append({"period": "10+ days out", "iv_change": "+0-5%",
                          "action": "Monitor — IV hasn't started expanding yet"})
        if days_out > 5:
            phases.append({"period": "5-10 days out", "iv_change": "+5-15%",
                          "action": "IV begins expanding. Consider early entry."})
        if days_out > 2:
            phases.append({"period": "2-5 days out", "iv_change": "+15-30%",
                          "action": "IV accelerating. OPTIMAL entry window."})
        if days_out > 0:
            phases.append({"period": "1-2 days out", "iv_change": "+25-50%",
                          "action": "IV near peak. Enter if haven't already."})
        phases.append({"period": "Earnings day", "iv_change": "PEAK → CRUSH",
                       "action": "IV collapses 30-60% after announcement."})

        return {
            "current_phase": f"{days_out} days to earnings",
            "phases": phases,
            "iv_peak_estimate": round(current_iv * 100 * 1.3, 1),
        }

    def _optimal_entry(self, days_out: int) -> Dict[str, Any]:
        """Determine optimal entry timing."""
        if days_out > 5:
            return {
                "recommendation": "WAIT",
                "ideal_entry": f"{days_out - 3} days from now (2-3 days before earnings)",
                "reason": "IV hasn't peaked yet. Entering now leaves premium on the table.",
            }
        elif days_out > 1:
            return {
                "recommendation": "ENTER TODAY",
                "ideal_entry": "Now",
                "reason": "IV is near or at peak. Optimal window for selling premium.",
            }
        elif days_out == 1:
            return {
                "recommendation": "ENTER TODAY (LAST CHANCE)",
                "ideal_entry": "Immediately",
                "reason": "Earnings tomorrow. Maximum IV but also maximum gamma risk.",
            }
        return {
            "recommendation": "TOO LATE",
            "ideal_entry": "N/A",
            "reason": "Earnings already passed or imminent. Wait for next opportunity.",
        }

    def _estimate_iv_crush(self, ticker: str, current_iv: float) -> Dict[str, Any]:
        """Estimate magnitude of post-earnings IV crush."""
        # Typical crush magnitudes by IV level
        if current_iv > 0.8:
            crush_pct = 55
            post_iv = current_iv * 0.45
        elif current_iv > 0.5:
            crush_pct = 45
            post_iv = current_iv * 0.55
        elif current_iv > 0.3:
            crush_pct = 35
            post_iv = current_iv * 0.65
        else:
            crush_pct = 25
            post_iv = current_iv * 0.75

        return {
            "expected_crush_pct": crush_pct,
            "pre_earnings_iv": round(current_iv * 100, 1),
            "post_earnings_iv_est": round(post_iv * 100, 1),
            "iv_drop": round((current_iv - post_iv) * 100, 1),
            "historical_note": (
                f"Typical {ticker.upper()} IV crush: {crush_pct-10}%-{crush_pct+10}%. "
                f"IV drops regardless of stock direction."
            ),
        }

    def _select_strategy(self, bias: str, iv: float, price: float) -> Dict[str, Any]:
        """Select strategy based on directional bias and IV level."""
        if bias == "neutral":
            if iv > 0.5:
                return {
                    "name": "Iron Condor",
                    "type": SpreadType.IRON_CONDOR,
                    "description": "Sell both sides — high IV gives wide profit range",
                    "delta": 0.15,
                }
            return {
                "name": "Short Iron Condor (narrow)",
                "type": SpreadType.IRON_CONDOR,
                "description": "Neutral stance — collect premium from both sides",
                "delta": 0.20,
            }
        elif bias == "bullish":
            return {
                "name": "Put Credit Spread",
                "type": SpreadType.PUT_CREDIT,
                "description": "Bullish bias — sell puts below expected support",
                "delta": 0.15,
            }
        else:
            return {
                "name": "Call Credit Spread",
                "type": SpreadType.CALL_CREDIT,
                "description": "Bearish bias — sell calls above expected resistance",
                "delta": 0.15,
            }

    def _build_trade_setup(self, ticker: str, S: float, iv: float, em: float,
                            T: float, r: float, bias: str,
                            earnings_date: date) -> Dict[str, Any]:
        """Build specific trade setup with strikes."""
        delta = 0.15

        put_K = strike_at_delta(S, T, r, iv, delta, "put")
        call_K = strike_at_delta(S, T, r, iv, delta, "call")

        # Determine strike interval based on price
        if S > 200:
            interval = 5
        elif S > 50:
            interval = 2.5
        else:
            interval = 1

        # Round to nearest interval
        put_short = round(put_K / interval) * interval
        put_long = put_short - interval * 2
        call_short = round(call_K / interval) * interval
        call_long = call_short + interval * 2

        setup = {
            "underlying": ticker.upper(),
            "expiration": earnings_date + timedelta(days=1),  # Day after earnings
            "expected_move": round(em, 2),
        }

        if bias == "neutral":
            setup["put_side"] = {"short": put_short, "long": put_long}
            setup["call_side"] = {"short": call_short, "long": call_long}
            setup["structure"] = f"{put_long}/{put_short}p — {call_short}/{call_long}c Iron Condor"
        elif bias == "bullish":
            setup["put_side"] = {"short": put_short, "long": put_long}
            setup["structure"] = f"{put_long}/{put_short}p Put Credit Spread"
        else:
            setup["call_side"] = {"short": call_short, "long": call_long}
            setup["structure"] = f"{call_short}/{call_long}c Call Credit Spread"

        return setup

    def _analyze_premium(self, S: float, iv: float, em: float,
                          crush: Dict) -> Dict[str, Any]:
        """Compare premium collected vs typical earnings move."""
        # Estimated credit from iron condor at 0.15 delta
        estimated_credit = em * 0.3  # Rough approximation

        return {
            "estimated_credit": round(estimated_credit, 2),
            "expected_move": round(em, 2),
            "premium_vs_move": round(estimated_credit / em * 100, 1) if em > 0 else 0,
            "crush_value": round(estimated_credit * crush["expected_crush_pct"] / 100, 2),
            "verdict": "RICH" if estimated_credit / em > 0.25 else "FAIR" if estimated_credit / em > 0.15 else "THIN",
        }

    def _position_size(self, result: Dict) -> Dict[str, Any]:
        """Earnings-specific position sizing — conservative."""
        acct = self.config.account.account_size
        max_risk = acct * self.config.trading.earnings_max_risk_pct  # 2% for earnings

        setup = result["trade_setup"]
        if "put_side" in setup and "call_side" in setup:
            width = max(
                setup["put_side"]["short"] - setup["put_side"]["long"],
                setup["call_side"]["long"] - setup["call_side"]["short"],
            )
        elif "put_side" in setup:
            width = setup["put_side"]["short"] - setup["put_side"]["long"]
        else:
            width = setup["call_side"]["long"] - setup["call_side"]["short"]

        max_loss_per = width * 100
        contracts = max(1, int(max_risk / max_loss_per)) if max_loss_per > 0 else 1

        return {
            "max_risk_pct": f"{self.config.trading.earnings_max_risk_pct*100:.0f}%",
            "max_risk_dollars": round(max_risk, 2),
            "width": width,
            "contracts": contracts,
            "total_risk": round(max_loss_per * contracts, 2),
            "note": "Earnings are binary events — ALWAYS use reduced sizing (1-2% max)",
        }

    def _exit_protocol(self) -> Dict[str, Any]:
        """Post-earnings exit protocol."""
        return {
            "rules": [
                {
                    "timing": "Next morning at open",
                    "action": "CLOSE immediately for IV crush profit",
                    "reason": "Capture the crush. Don't wait for more decay.",
                },
                {
                    "timing": "If stock gaps within your strikes",
                    "action": "CLOSE for partial loss",
                    "reason": "IV crush offsets some of the directional loss. Take what you can.",
                },
                {
                    "timing": "If stock gaps beyond your strikes",
                    "action": "CLOSE immediately — full loss",
                    "reason": "Don't hope for a reversal. Accept the loss and move on.",
                },
                {
                    "timing": "If assignment risk (American options)",
                    "action": "Close before earnings to avoid early assignment",
                    "reason": "Deep ITM options can be assigned early on earnings.",
                },
            ],
            "key_principle": "The play is IV crush, NOT direction. Close at the open regardless.",
        }

    def _upcoming_opportunities(self) -> List[Dict[str, Any]]:
        """Placeholder for earnings calendar integration."""
        return [
            {"note": "Connect to earnings calendar API for live upcoming events."},
            {"note": "Screen for: IV > 50%, IV Rank > 70%, Liquid options (>1000 OI)"},
            {"note": "Best candidates: Large caps with predictable IV crush patterns"},
        ]

    # ── Report Output ──────────────────────────────────────────

    def format_report(self, result: Dict) -> str:
        lines = []
        lines.append(header(
            "IMC TRADING EARNINGS THETA CRUSHER",
            f"{result['ticker'].upper()} | Earnings: {result['earnings_date']} | IV: {result['current_iv']:.1f}%"
        ))

        # IV expansion timeline
        exp = result["iv_expansion"]
        lines.append(sub_header("PRE-EARNINGS IV EXPANSION"))
        lines.append(kv("Current Phase", exp["current_phase"]))
        lines.append(kv("IV Peak Estimate", f"{exp['iv_peak_estimate']:.1f}%"))
        for phase in exp["phases"]:
            lines.append(f"  {'→' if 'OPTIMAL' in phase['action'] else '  '} "
                          f"{phase['period']:>20s}  |  IV: {phase['iv_change']:>8s}  |  {phase['action']}")

        # Entry timing
        entry = result["entry_timing"]
        lines.append(sub_header("OPTIMAL ENTRY"))
        entry_color = C.GREEN if entry["recommendation"] in ("ENTER TODAY", "ENTER TODAY (LAST CHANCE)") else C.YELLOW
        lines.append(kv("Recommendation", entry["recommendation"], entry_color))
        lines.append(kv("Ideal Entry", entry["ideal_entry"]))
        lines.append(f"  {C.DIM}{entry['reason']}{C.RESET}")

        # IV crush estimate
        crush = result["iv_crush"]
        lines.append(sub_header("EXPECTED IV CRUSH"))
        lines.append(kv("Pre-Earnings IV", f"{crush['pre_earnings_iv']:.1f}%"))
        lines.append(kv("Post-Earnings IV", f"{crush['post_earnings_iv_est']:.1f}%", C.GREEN))
        lines.append(kv("Expected Crush", f"-{crush['expected_crush_pct']}%", C.GREEN))
        lines.append(kv("IV Drop", f"-{crush['iv_drop']:.1f} points"))
        lines.append(f"  {C.DIM}{crush['historical_note']}{C.RESET}")

        # Strategy
        strat = result["strategy"]
        lines.append(sub_header("STRATEGY SELECTION"))
        lines.append(kv("Strategy", strat["name"], C.BOLD))
        lines.append(kv("Delta", f"{strat['delta']}"))
        lines.append(f"  {strat['description']}")

        # Expected move
        em = result["expected_move"]
        lines.append(sub_header("EXPECTED MOVE"))
        lines.append(kv("Expected Move", f"±${em['dollars']:.2f} ({em['percent']:.1f}%)"))
        lines.append(kv("Expected Range", f"${em['range_low']:.2f} — ${em['range_high']:.2f}"))

        # Trade setup
        setup = result["trade_setup"]
        lines.append(sub_header("TRADE SETUP"))
        lines.append(kv("Structure", setup["structure"], C.BOLD))
        lines.append(kv("Expiration", str(setup["expiration"])))
        if "put_side" in setup:
            lines.append(kv("Put Spread", f"{setup['put_side']['long']}/{setup['put_side']['short']}"))
        if "call_side" in setup:
            lines.append(kv("Call Spread", f"{setup['call_side']['short']}/{setup['call_side']['long']}"))

        # Premium analysis
        prem = result["premium_analysis"]
        lines.append(sub_header("PREMIUM ANALYSIS"))
        lines.append(kv("Estimated Credit", f"${prem['estimated_credit']:.2f}", C.GREEN))
        lines.append(kv("vs Expected Move", f"{prem['premium_vs_move']:.0f}%"))
        verdict_color = C.GREEN if prem["verdict"] == "RICH" else C.YELLOW if prem["verdict"] == "FAIR" else C.RED
        lines.append(kv("Verdict", prem["verdict"], verdict_color))

        # Position sizing
        ps = result["position_sizing"]
        lines.append(sub_header("POSITION SIZING"))
        lines.append(kv("Max Risk", f"{ps['max_risk_pct']} = ${ps['max_risk_dollars']:,.2f}"))
        lines.append(kv("Spread Width", f"${ps['width']:.2f}"))
        lines.append(kv("Contracts", ps["contracts"]))
        lines.append(kv("Total Risk", f"${ps['total_risk']:,.2f}", C.RED))
        lines.append(f"  {C.YELLOW}⚠️  {ps['note']}{C.RESET}")

        # Exit protocol
        exit_p = result["exit_protocol"]
        lines.append(sub_header("POST-EARNINGS EXIT PROTOCOL"))
        lines.append(f"\n  {C.BOLD}{exit_p['key_principle']}{C.RESET}\n")
        for rule in exit_p["rules"]:
            lines.append(f"  {C.BOLD}{rule['timing']}:{C.RESET}")
            lines.append(f"    Action: {rule['action']}")
            lines.append(f"    Reason: {rule['reason']}")

        lines.append(f"\n{C.CYAN}{'═' * 78}{C.RESET}\n")
        return "\n".join(lines)

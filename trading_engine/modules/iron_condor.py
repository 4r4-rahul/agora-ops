"""
Module 5: D.E. Shaw Iron Condor Income Machine
===============================================
Systematic iron condor strategy on indexes and ETFs for maximum probability income.
"""

from datetime import datetime, date
from typing import Dict, Any, Optional, List

from ..models import (
    MarketSnapshot, OptionsChain, IronCondor, CreditSpread, SpreadLeg,
    SpreadType, TradeTicket
)
from ..config import EngineConfig
from ..market_data import MarketDataProvider
from ..black_scholes import (
    strike_at_delta, prob_between, expected_move, bs_delta, bs_theta
)
from ..formatters import header, sub_header, kv, trade_ticket, pnl, C


class IronCondorMachine:
    """
    D.E. Shaw-style systematic iron condor builder.
    Selects optimal underlying, constructs both sides, and manages position.
    """

    def __init__(self, config: EngineConfig, data_provider: MarketDataProvider):
        self.config = config
        self.data = data_provider

    def build(self, snap: MarketSnapshot, underlying: str = "SPX",
              expiration_type: str = "0DTE",
              chain: Optional[OptionsChain] = None) -> Dict[str, Any]:
        """
        Build complete iron condor setup.

        Args:
            expiration_type: "0DTE", "weekly", or "monthly"
        """
        result = {
            "underlying": underlying,
            "expiration_type": expiration_type,
            "underlying_ranking": {},
            "iron_condor": None,
            "ticket": None,
        }

        # 1. Rank underlyings for iron condors today
        result["underlying_ranking"] = self._rank_underlyings(snap)

        # 2. Get price for selected underlying
        S = self._get_price(snap, underlying)
        iv = snap.vix_level / 100 if snap.vix_level > 1 else 0.18
        r = 0.045

        # 3. Set timeframe
        T_map = {"0DTE": 1/(252*6.5), "weekly": 5/252, "monthly": 30/365}
        T = T_map.get(expiration_type, 5/252)

        # 4. Calculate expected range
        em = expected_move(S, iv, T)
        result["expected_range"] = {
            "low": round(S - em, 2),
            "high": round(S + em, 2),
            "move": round(em, 2),
        }

        # 5. Get or build chain
        if chain is None:
            exp = date.today()
            chain = self.data.get_options_chain(underlying, exp)

        # 6. Construct put side
        tc = self.config.trading
        put_short_delta = (tc.short_delta_min + tc.short_delta_max) / 2
        put_short_K = strike_at_delta(S, T, r, iv, put_short_delta, "put")
        put_long_K = put_short_K - tc.spread_width_min

        # Credit for put side (from chain or BS)
        put_short = self._find_contract(chain.puts, put_short_K)
        put_long = self._find_contract(chain.puts, put_long_K)
        put_credit = (put_short.mark - put_long.mark) if put_short and put_long else 0
        put_credit = max(put_credit, 0.01)

        put_spread = CreditSpread(
            spread_type=SpreadType.PUT_CREDIT,
            short_leg=SpreadLeg(contract=put_short, quantity=-1, is_short=True) if put_short else SpreadLeg(),
            long_leg=SpreadLeg(contract=put_long, quantity=1, is_short=False) if put_long else SpreadLeg(),
            credit=round(put_credit, 2),
            probability_otm=round((1 - put_short_delta) * 100, 1),
        )
        # Manually set strikes if contracts weren't found
        if put_spread.short_leg.contract.strike == 0:
            put_spread.short_leg.contract.strike = put_short_K
            put_spread.long_leg.contract.strike = put_long_K
            put_spread.width = tc.spread_width_min
            put_spread.max_loss = tc.spread_width_min - put_credit
            put_spread.breakeven = put_short_K - put_credit

        # 7. Construct call side
        call_short_delta = put_short_delta
        call_short_K = strike_at_delta(S, T, r, iv, call_short_delta, "call")
        call_long_K = call_short_K + tc.spread_width_min

        call_short = self._find_contract(chain.calls, call_short_K)
        call_long = self._find_contract(chain.calls, call_long_K)
        call_credit = (call_short.mark - call_long.mark) if call_short and call_long else 0
        call_credit = max(call_credit, 0.01)

        call_spread = CreditSpread(
            spread_type=SpreadType.CALL_CREDIT,
            short_leg=SpreadLeg(contract=call_short, quantity=-1, is_short=True) if call_short else SpreadLeg(),
            long_leg=SpreadLeg(contract=call_long, quantity=1, is_short=False) if call_long else SpreadLeg(),
            credit=round(call_credit, 2),
            probability_otm=round((1 - call_short_delta) * 100, 1),
        )
        if call_spread.short_leg.contract.strike == 0:
            call_spread.short_leg.contract.strike = call_short_K
            call_spread.long_leg.contract.strike = call_long_K
            call_spread.width = tc.spread_width_min
            call_spread.max_loss = tc.spread_width_min - call_credit
            call_spread.breakeven = call_short_K + call_credit

        # 8. Combine into iron condor
        ic = IronCondor(put_spread=put_spread, call_spread=call_spread)

        # 9. Position sizing
        total_credit = ic.total_credit
        wider = max(put_spread.width, call_spread.width)
        max_loss_per = (wider - total_credit) * 100
        max_risk_dollars = self.config.account.account_size * self.config.account.max_risk_per_trade_pct
        num_contracts = max(1, int(max_risk_dollars / max_loss_per)) if max_loss_per > 0 else 1

        result["iron_condor"] = ic
        result["num_contracts"] = num_contracts

        # 10. Breakevens
        result["breakevens"] = {
            "lower": round(put_short_K - total_credit, 2),
            "upper": round(call_short_K + total_credit, 2),
        }

        # 11. Probability of profit
        p_profit = prob_between(S, result["breakevens"]["lower"],
                                result["breakevens"]["upper"], T, r, iv)
        result["prob_profit"] = round(p_profit * 100, 1)

        # 12. Adjustment triggers
        result["adjustments"] = {
            "put_roll_trigger": round(put_short_K + (S - put_short_K) * tc.ic_adjustment_trigger_pct, 2),
            "call_roll_trigger": round(call_short_K - (call_short_K - S) * tc.ic_adjustment_trigger_pct, 2),
            "protocol": (
                "If SPX touches trigger level: "
                "1) Close threatened side, "
                "2) Re-sell at new 0.12 delta strike, "
                "3) Keep unthreatened side for additional credit"
            ),
        }

        # 13. Generate trade ticket
        result["ticket"] = TradeTicket(
            id=f"IC-{underlying}-{date.today():%Y%m%d}",
            strategy=f"{expiration_type} Iron Condor",
            underlying=underlying,
            spread_type=SpreadType.IRON_CONDOR,
            short_strike=put_short_K,
            long_strike=put_long_K,
            short_strike_call=call_short_K,
            long_strike_call=call_long_K,
            expiration=date.today(),
            credit_per_contract=round(total_credit, 2),
            max_loss_per_contract=round(wider - total_credit, 2),
            num_contracts=num_contracts,
            total_credit=round(total_credit * num_contracts * 100, 2),
            total_max_loss=round((wider - total_credit) * num_contracts * 100, 2),
            probability_of_profit=result["prob_profit"],
            reward_to_risk=round(total_credit / (wider - total_credit), 2) if wider > total_credit else 0,
            vix_at_entry=snap.vix_level,
            spx_at_entry=snap.spx_price,
        )

        return result

    def _rank_underlyings(self, snap: MarketSnapshot) -> Dict[str, Dict]:
        """Rank SPX, SPY, QQQ, IWM for iron condor suitability."""
        rankings = {}

        candidates = [
            ("SPX", snap.spx_price, snap.vix_level),
            ("SPY", snap.spy_price, snap.vix_level * 0.95),
            ("QQQ", snap.qqq_price, snap.vix_level * 1.1),
            ("IWM", snap.iwm_price, snap.vix_level * 1.15),
        ]

        for sym, price, iv_est in candidates:
            if price <= 0:
                continue
            iv = iv_est / 100 if iv_est > 1 else iv_est
            em_pct = expected_move(price, iv, 1/252) / price * 100

            # Score: prefer high IV (more premium) + range-bound tendency
            score = iv_est * 0.6  # Higher IV = more premium
            if snap.vix_level < 25:
                score += 20  # Range-bound bonus
            if sym == "SPX":
                score += 15  # Liquidity bonus + cash-settled

            rankings[sym] = {
                "price": round(price, 2),
                "iv_estimate": round(iv_est, 1),
                "expected_move_pct": round(em_pct, 2),
                "score": round(score, 1),
                "notes": "Cash-settled, no assignment risk" if sym == "SPX" else "ETF, early assignment possible",
            }

        return dict(sorted(rankings.items(), key=lambda x: x[1]["score"], reverse=True))

    def _get_price(self, snap: MarketSnapshot, underlying: str) -> float:
        prices = {"SPX": snap.spx_price, "SPY": snap.spy_price,
                  "QQQ": snap.qqq_price, "IWM": snap.iwm_price}
        return prices.get(underlying, snap.spx_price)

    def _find_contract(self, contracts: list, target_strike: float):
        """Find nearest contract to target strike."""
        if not contracts:
            return None
        return min(contracts, key=lambda c: abs(c.strike - target_strike))

    # ── Report Output ──────────────────────────────────────────

    def format_report(self, result: Dict, snap: MarketSnapshot) -> str:
        lines = []
        underlying = result["underlying"]
        lines.append(header(
            "D.E. SHAW IRON CONDOR INCOME MACHINE",
            f"{underlying} | {result['expiration_type']} | {date.today():%Y-%m-%d}"
        ))

        # Underlying ranking
        lines.append(sub_header("UNDERLYING SELECTION"))
        for sym, data in result["underlying_ranking"].items():
            best = " ⭐" if sym == underlying else ""
            lines.append(kv(f"{sym}{best}",
                             f"${data['price']:,.2f} | IV≈{data['iv_estimate']:.1f} | "
                             f"EM={data['expected_move_pct']:.2f}% | Score={data['score']:.0f}"))

        # Expected range
        er = result["expected_range"]
        lines.append(sub_header("EXPECTED RANGE"))
        lines.append(kv("Expected Move", f"±{er['move']:.1f} pts"))
        lines.append(kv("Range", f"{er['low']:.0f} — {er['high']:.0f}"))

        # Iron condor details
        ic = result.get("iron_condor")
        if ic:
            lines.append(sub_header("IRON CONDOR CONSTRUCTION"))

            lines.append(f"\n  {C.BOLD}Put Side:{C.RESET}")
            lines.append(kv("  Short Put", f"{ic.put_spread.short_leg.contract.strike}  "
                             f"(Δ ≈ {abs(ic.put_spread.short_leg.contract.greeks.delta):.3f})", C.RED))
            lines.append(kv("  Long Put", str(ic.put_spread.long_leg.contract.strike), C.GREEN))
            lines.append(kv("  Put Credit", f"${ic.put_spread.credit:.2f}", C.GREEN))

            lines.append(f"\n  {C.BOLD}Call Side:{C.RESET}")
            lines.append(kv("  Short Call", f"{ic.call_spread.short_leg.contract.strike}  "
                             f"(Δ ≈ {abs(ic.call_spread.short_leg.contract.greeks.delta):.3f})", C.RED))
            lines.append(kv("  Long Call", str(ic.call_spread.long_leg.contract.strike), C.GREEN))
            lines.append(kv("  Call Credit", f"${ic.call_spread.credit:.2f}", C.GREEN))

            lines.append(f"\n  {C.BOLD}Combined:{C.RESET}")
            lines.append(kv("  Total Credit", f"${ic.total_credit:.2f}", C.GREEN))
            lines.append(kv("  Max Loss", f"${ic.max_loss:.2f}", C.RED))

        # Position sizing
        n = result.get("num_contracts", 1)
        lines.append(sub_header("POSITION SIZING"))
        lines.append(kv("Account Size", f"${self.config.account.account_size:,.2f}"))
        lines.append(kv("Max Risk per Trade", f"{self.config.account.max_risk_per_trade_pct*100:.0f}%"))
        lines.append(kv("Contracts", n))
        if ic:
            lines.append(kv("Total Credit", f"${ic.total_credit * n * 100:,.2f}", C.GREEN))
            lines.append(kv("Total Max Loss", f"${ic.max_loss * n * 100:,.2f}", C.RED))

        # Breakevens
        be = result.get("breakevens", {})
        lines.append(sub_header("BREAKEVEN & PROBABILITY"))
        lines.append(kv("Lower Breakeven", f"{be.get('lower', 0):.2f}"))
        lines.append(kv("Upper Breakeven", f"{be.get('upper', 0):.2f}"))
        lines.append(kv("P(Profit)", f"{result.get('prob_profit', 0):.1f}%", C.GREEN))

        # Payoff range description
        if ic and be:
            lines.append(sub_header("PAYOFF RANGE"))
            price = self._get_price(snap, underlying)
            lower_dist = price - be.get("lower", 0)
            upper_dist = be.get("upper", 0) - price
            lines.append(f"  Max Profit if {underlying} stays between {be['lower']:.0f} — {be['upper']:.0f}")
            lines.append(f"  Room to fall: {lower_dist:.0f} pts ({lower_dist/price*100:.1f}%)")
            lines.append(f"  Room to rise: {upper_dist:.0f} pts ({upper_dist/price*100:.1f}%)")

        # Adjustment protocol
        adj = result.get("adjustments", {})
        lines.append(sub_header("ADJUSTMENT PROTOCOL"))
        lines.append(kv("Put Roll Trigger", f"SPX < {adj.get('put_roll_trigger', 0):.0f}"))
        lines.append(kv("Call Roll Trigger", f"SPX > {adj.get('call_roll_trigger', 0):.0f}"))
        lines.append(f"\n  {adj.get('protocol', '')}")

        # Profit taking
        lines.append(sub_header("PROFIT TAKING RULES"))
        lines.append("  1. Close entire position at 50% of max profit")
        lines.append("  2. Or manage each side independently:")
        lines.append("     - Close profitable side when it hits 80% of its credit")
        lines.append("     - Let remaining side continue to decay")
        lines.append("  3. If one side reaches 2x its credit, close that side")
        lines.append("  4. Close all positions by 3:50 PM on expiration day")

        # Daily income projection
        if ic:
            daily = ic.total_credit * n * 100 * 0.85  # Assume 85% capture rate
            lines.append(sub_header("INCOME PROJECTION"))
            lines.append(kv("Expected Daily Income", pnl(daily), C.GREEN))
            lines.append(kv("Weekly (5 sessions)", pnl(daily * 5)))
            lines.append(kv("Monthly (21 sessions)", pnl(daily * 21)))

        lines.append(f"\n{C.CYAN}{'═' * 78}{C.RESET}\n")
        return "\n".join(lines)

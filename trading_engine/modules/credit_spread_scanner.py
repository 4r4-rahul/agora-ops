"""
Module 1: Tastytrade 0DTE SPX Credit Spread Scanner
====================================================
Scans for optimal 0DTE credit spread setups with exact strikes and risk parameters.
"""

from datetime import datetime, date, time
from typing import Optional, Tuple, List, Dict, Any

from ..models import (
    MarketSnapshot, OptionsChain, OptionContract, CreditSpread, IronCondor,
    SpreadLeg, SpreadType, TradeTicket, OptionType, MarketRegime, Greeks
)
from ..config import EngineConfig
from ..market_data import MarketDataProvider
from ..black_scholes import (
    strike_at_delta, prob_otm, expected_move_from_straddle,
    bs_delta, bs_put_price, bs_call_price, prob_between
)
from ..formatters import header, sub_header, kv, trade_ticket, pnl, C


class CreditSpreadScanner:
    """
    Tastytrade-style 0DTE SPX credit spread scanner.
    Finds optimal put and call credit spreads for daily income.
    """

    def __init__(self, config: EngineConfig, data_provider: MarketDataProvider):
        self.config = config
        self.data = data_provider
        self.tc = config.trading

    def scan(self, snap: MarketSnapshot, regime: MarketRegime = MarketRegime.GREEN,
             chain: Optional[OptionsChain] = None) -> Dict[str, Any]:
        """
        Run full 0DTE scan and return trade setups.

        Returns dict with keys:
          - market_ok: bool
          - expected_range: tuple (low, high)
          - put_spread: CreditSpread or None
          - call_spread: CreditSpread or None
          - iron_condor: IronCondor or None
          - tickets: list of TradeTicket
        """
        result = {
            "market_ok": False,
            "expected_range": (0, 0),
            "put_spread": None,
            "call_spread": None,
            "iron_condor": None,
            "tickets": [],
        }

        # 1. Market conditions check
        if not self._check_market_conditions(snap, regime):
            result["notes"] = "Market conditions not suitable for 0DTE trading today."
            return result
        result["market_ok"] = True

        # 2. Calculate expected range
        em = snap.expected_move_1d
        if em <= 0:
            em = snap.spx_price * (snap.vix_level / 100) / (252 ** 0.5)
        low = snap.spx_price - em
        high = snap.spx_price + em
        result["expected_range"] = (round(low, 2), round(high, 2))

        # 3. Get or build options chain
        if chain is None:
            chain = self.data.get_options_chain("SPX", date.today())

        if not chain.puts or not chain.calls:
            result["notes"] = "No options chain data available."
            return result

        # 4. Find put credit spread
        put_spread = self._find_put_spread(snap, chain)
        if put_spread:
            result["put_spread"] = put_spread

        # 5. Find call credit spread
        call_spread = self._find_call_spread(snap, chain)
        if call_spread:
            result["call_spread"] = call_spread

        # 6. Build iron condor if both sides available
        if put_spread and call_spread and regime == MarketRegime.GREEN:
            ic = IronCondor(put_spread=put_spread, call_spread=call_spread)
            if ic.total_credit >= self.tc.ic_min_total_credit:
                result["iron_condor"] = ic

        # 7. Generate trade tickets
        result["tickets"] = self._generate_tickets(result, snap)

        return result

    def _check_market_conditions(self, snap: MarketSnapshot, regime: MarketRegime) -> bool:
        """Check if today is suitable for 0DTE trading."""
        if regime == MarketRegime.RED:
            return False
        if snap.vix_level > 35:
            return False
        if snap.is_fed_day or snap.is_cpi_day:
            return False  # Too much event risk for 0DTE
        return True

    def _find_put_spread(self, snap: MarketSnapshot, chain: OptionsChain) -> Optional[CreditSpread]:
        """Find optimal put credit spread at 0.10-0.15 delta."""
        puts = sorted(chain.puts, key=lambda p: p.strike, reverse=True)

        short_put = None
        for p in puts:
            delta = abs(p.greeks.delta)
            if self.tc.short_delta_min <= delta <= self.tc.short_delta_max:
                short_put = p
                break

        if not short_put:
            # Fallback: find closest to target delta
            target = (self.tc.short_delta_min + self.tc.short_delta_max) / 2
            short_put = min(puts, key=lambda p: abs(abs(p.greeks.delta) - target))

        # Find long put 5-10 points below
        long_put = None
        for p in puts:
            distance = short_put.strike - p.strike
            if self.tc.spread_width_min <= distance <= self.tc.spread_width_max:
                long_put = p
                break

        if not long_put:
            # Use fixed width
            target_strike = short_put.strike - self.tc.spread_width_min
            long_put = min(puts, key=lambda p: abs(p.strike - target_strike))

        if not short_put or not long_put or short_put.strike <= long_put.strike:
            return None

        credit = short_put.mark - long_put.mark
        if credit < self.tc.zero_dte_min_credit:
            return None

        spread = CreditSpread(
            spread_type=SpreadType.PUT_CREDIT,
            short_leg=SpreadLeg(contract=short_put, quantity=-1, is_short=True),
            long_leg=SpreadLeg(contract=long_put, quantity=1, is_short=False),
            credit=round(credit, 2),
            probability_otm=round((1 - abs(short_put.greeks.delta)) * 100, 1),
        )
        return spread

    def _find_call_spread(self, snap: MarketSnapshot, chain: OptionsChain) -> Optional[CreditSpread]:
        """Find optimal call credit spread at 0.10-0.15 delta."""
        calls = sorted(chain.calls, key=lambda c: c.strike)

        short_call = None
        for c in calls:
            delta = abs(c.greeks.delta)
            if self.tc.short_delta_min <= delta <= self.tc.short_delta_max:
                short_call = c
                break

        if not short_call:
            target = (self.tc.short_delta_min + self.tc.short_delta_max) / 2
            short_call = min(calls, key=lambda c: abs(abs(c.greeks.delta) - target))

        long_call = None
        for c in calls:
            distance = c.strike - short_call.strike
            if self.tc.spread_width_min <= distance <= self.tc.spread_width_max:
                long_call = c
                break

        if not long_call:
            target_strike = short_call.strike + self.tc.spread_width_min
            long_call = min(calls, key=lambda c: abs(c.strike - target_strike))

        if not short_call or not long_call or long_call.strike <= short_call.strike:
            return None

        credit = short_call.mark - long_call.mark
        if credit < self.tc.zero_dte_min_credit:
            return None

        spread = CreditSpread(
            spread_type=SpreadType.CALL_CREDIT,
            short_leg=SpreadLeg(contract=short_call, quantity=-1, is_short=True),
            long_leg=SpreadLeg(contract=long_call, quantity=1, is_short=False),
            credit=round(credit, 2),
            probability_otm=round((1 - abs(short_call.greeks.delta)) * 100, 1),
        )
        return spread

    def _position_size(self, spread: CreditSpread) -> int:
        """Calculate number of contracts based on risk rules."""
        max_risk_dollars = self.config.account.account_size * self.config.account.max_risk_per_trade_pct
        max_loss_per = spread.max_loss * 100  # Per contract in dollars
        if max_loss_per <= 0:
            return 1
        contracts = int(max_risk_dollars / max_loss_per)
        return max(1, contracts)

    def _generate_tickets(self, result: Dict, snap: MarketSnapshot) -> List[TradeTicket]:
        """Generate trade tickets for the found setups."""
        tickets = []

        # Put credit spread ticket
        ps = result.get("put_spread")
        if ps:
            n = self._position_size(ps)
            tickets.append(TradeTicket(
                id=f"0DTE-PCS-{date.today():%Y%m%d}",
                strategy="0DTE Put Credit Spread",
                underlying="SPX",
                spread_type=SpreadType.PUT_CREDIT,
                short_strike=ps.short_leg.contract.strike,
                long_strike=ps.long_leg.contract.strike,
                expiration=date.today(),
                credit_per_contract=ps.credit,
                max_loss_per_contract=ps.max_loss,
                num_contracts=n,
                total_credit=round(ps.credit * n * 100, 2),
                total_max_loss=round(ps.max_loss * n * 100, 2),
                probability_of_profit=ps.probability_otm,
                reward_to_risk=ps.reward_to_risk,
                entry_time_start=self.tc.zero_dte_entry_start,
                entry_time_end=self.tc.zero_dte_entry_end,
                stop_loss_price=round(ps.credit * self.tc.zero_dte_stop_multiplier, 2),
                profit_target_price=round(ps.credit * self.tc.zero_dte_profit_target_pct, 2),
                exit_by_time="15:50",
                market_regime=result.get("regime", ""),
                vix_at_entry=snap.vix_level,
                spx_at_entry=snap.spx_price,
            ))

        # Call credit spread ticket
        cs = result.get("call_spread")
        if cs:
            n = self._position_size(cs)
            tickets.append(TradeTicket(
                id=f"0DTE-CCS-{date.today():%Y%m%d}",
                strategy="0DTE Call Credit Spread",
                underlying="SPX",
                spread_type=SpreadType.CALL_CREDIT,
                short_strike=cs.short_leg.contract.strike,
                long_strike=cs.long_leg.contract.strike,
                expiration=date.today(),
                credit_per_contract=cs.credit,
                max_loss_per_contract=cs.max_loss,
                num_contracts=n,
                total_credit=round(cs.credit * n * 100, 2),
                total_max_loss=round(cs.max_loss * n * 100, 2),
                probability_of_profit=cs.probability_otm,
                reward_to_risk=cs.reward_to_risk,
                entry_time_start=self.tc.zero_dte_entry_start,
                entry_time_end=self.tc.zero_dte_entry_end,
                stop_loss_price=round(cs.credit * self.tc.zero_dte_stop_multiplier, 2),
                profit_target_price=round(cs.credit * self.tc.zero_dte_profit_target_pct, 2),
                exit_by_time="15:50",
                vix_at_entry=snap.vix_level,
                spx_at_entry=snap.spx_price,
            ))

        # Iron condor ticket
        ic = result.get("iron_condor")
        if ic:
            n_put = self._position_size(ic.put_spread)
            n_call = self._position_size(ic.call_spread)
            n = min(n_put, n_call)
            tickets.append(TradeTicket(
                id=f"0DTE-IC-{date.today():%Y%m%d}",
                strategy="0DTE Iron Condor",
                underlying="SPX",
                spread_type=SpreadType.IRON_CONDOR,
                short_strike=ic.put_spread.short_leg.contract.strike,
                long_strike=ic.put_spread.long_leg.contract.strike,
                short_strike_call=ic.call_spread.short_leg.contract.strike,
                long_strike_call=ic.call_spread.long_leg.contract.strike,
                expiration=date.today(),
                credit_per_contract=ic.total_credit,
                max_loss_per_contract=ic.max_loss,
                num_contracts=n,
                total_credit=round(ic.total_credit * n * 100, 2),
                total_max_loss=round(ic.max_loss * n * 100, 2),
                probability_of_profit=round(
                    ic.put_spread.probability_otm * ic.call_spread.probability_otm / 100, 1
                ),
                reward_to_risk=round(ic.total_credit / ic.max_loss, 2) if ic.max_loss else 0,
                entry_time_start=self.tc.zero_dte_entry_start,
                entry_time_end=self.tc.zero_dte_entry_end,
                stop_loss_price=round(ic.total_credit * self.tc.zero_dte_stop_multiplier, 2),
                profit_target_price=round(ic.total_credit * self.tc.zero_dte_profit_target_pct, 2),
                exit_by_time="15:50",
                vix_at_entry=snap.vix_level,
                spx_at_entry=snap.spx_price,
            ))

        return tickets

    # ── Report Output ──────────────────────────────────────────

    def format_report(self, result: Dict, snap: MarketSnapshot) -> str:
        lines = []
        lines.append(header(
            "TASTYTRADE 0DTE SPX CREDIT SPREAD SCANNER",
            f"{date.today():%A, %B %d, %Y} | SPX {snap.spx_price:,.2f} | VIX {snap.vix_level:.2f}"
        ))

        # Market conditions
        lines.append(sub_header("MARKET CONDITIONS CHECK"))
        if result["market_ok"]:
            lines.append(f"  {C.GREEN}✅ Market conditions SUITABLE for 0DTE trading{C.RESET}")
        else:
            lines.append(f"  {C.RED}❌ Market conditions NOT suitable: {result.get('notes', '')}{C.RESET}")
            return "\n".join(lines)

        lines.append(kv("VIX Level", f"{snap.vix_level:.2f}",
                         C.GREEN if snap.vix_level < 25 else C.YELLOW))
        lines.append(kv("Overnight Futures", f"{snap.spx_futures_overnight_change:+.1f} pts"))

        # Expected range
        low, high = result["expected_range"]
        lines.append(sub_header("SPX EXPECTED MOVE"))
        lines.append(kv("Expected Range", f"{low:.0f} — {high:.0f}"))
        lines.append(kv("Expected Move", f"±{snap.expected_move_1d:.1f} pts ({snap.expected_move_1d/snap.spx_price*100:.2f}%)"))
        lines.append(kv("ATM Straddle (est)", f"${snap.atm_straddle_price:.2f}"))

        # Put credit spread
        ps = result.get("put_spread")
        if ps:
            lines.append(sub_header("PUT CREDIT SPREAD"))
            lines.append(kv("Short Put",
                             f"{ps.short_leg.contract.strike} (Δ {abs(ps.short_leg.contract.greeks.delta):.3f})", C.RED))
            lines.append(kv("Long Put",
                             f"{ps.long_leg.contract.strike}", C.GREEN))
            lines.append(kv("Width", f"{ps.width:.0f} pts"))
            lines.append(kv("Credit", f"${ps.credit:.2f}", C.GREEN))
            lines.append(kv("Max Loss", f"${ps.max_loss:.2f}", C.RED))
            lines.append(kv("Breakeven", f"{ps.breakeven:.2f}"))
            lines.append(kv("P(OTM)", f"{ps.probability_otm:.1f}%", C.GREEN))
            lines.append(kv("Reward:Risk", f"1:{1/ps.reward_to_risk:.1f}" if ps.reward_to_risk else "N/A"))

        # Call credit spread
        cs = result.get("call_spread")
        if cs:
            lines.append(sub_header("CALL CREDIT SPREAD"))
            lines.append(kv("Short Call",
                             f"{cs.short_leg.contract.strike} (Δ {abs(cs.short_leg.contract.greeks.delta):.3f})", C.RED))
            lines.append(kv("Long Call",
                             f"{cs.long_leg.contract.strike}", C.GREEN))
            lines.append(kv("Width", f"{cs.width:.0f} pts"))
            lines.append(kv("Credit", f"${cs.credit:.2f}", C.GREEN))
            lines.append(kv("Max Loss", f"${cs.max_loss:.2f}", C.RED))
            lines.append(kv("Breakeven", f"{cs.breakeven:.2f}"))
            lines.append(kv("P(OTM)", f"{cs.probability_otm:.1f}%", C.GREEN))

        # Iron condor
        ic = result.get("iron_condor")
        if ic:
            lines.append(sub_header("IRON CONDOR COMBINATION"))
            lines.append(kv("Put Spread", f"{ic.put_spread.long_leg.contract.strike}/{ic.put_spread.short_leg.contract.strike}"))
            lines.append(kv("Call Spread", f"{ic.call_spread.short_leg.contract.strike}/{ic.call_spread.long_leg.contract.strike}"))
            lines.append(kv("Total Credit", f"${ic.total_credit:.2f}", C.GREEN))
            lines.append(kv("Max Loss", f"${ic.max_loss:.2f}", C.RED))
            lines.append(kv("Profit Range", f"{ic.lower_breakeven:.0f} — {ic.upper_breakeven:.0f}"))

        # Trade tickets
        for ticket in result.get("tickets", []):
            td = {
                "strategy": ticket.strategy,
                "underlying": ticket.underlying,
                "date": ticket.expiration,
                "expiration": f"{ticket.expiration} (0DTE)",
                "credit": ticket.credit_per_contract,
                "max_loss": ticket.max_loss_per_contract,
                "contracts": ticket.num_contracts,
                "total_credit": ticket.total_credit,
                "total_max_loss": ticket.total_max_loss,
                "risk_reward": 1/ticket.reward_to_risk if ticket.reward_to_risk else 0,
                "prob_profit": ticket.probability_of_profit,
                "entry_window": f"{ticket.entry_time_start} — {ticket.entry_time_end}",
                "stop_loss": f"Close if spread reaches ${ticket.stop_loss_price:.2f} (2x credit)",
                "profit_target": f"Close at ${ticket.profit_target_price:.2f} (50% profit) before 2 PM",
                "time_exit": f"Close all positions by {ticket.exit_by_time}",
            }
            if ticket.spread_type == SpreadType.PUT_CREDIT:
                td["short_put"] = ticket.short_strike
                td["long_put"] = ticket.long_strike
                td["short_put_delta"] = f"{self.tc.short_delta_min}-{self.tc.short_delta_max}"
                td["breakevens"] = f"{ticket.short_strike - ticket.credit_per_contract:.2f}"
            elif ticket.spread_type == SpreadType.CALL_CREDIT:
                td["short_call"] = ticket.short_strike
                td["long_call"] = ticket.long_strike
                td["short_call_delta"] = f"{self.tc.short_delta_min}-{self.tc.short_delta_max}"
                td["breakevens"] = f"{ticket.short_strike + ticket.credit_per_contract:.2f}"
            elif ticket.spread_type == SpreadType.IRON_CONDOR:
                td["short_put"] = ticket.short_strike
                td["long_put"] = ticket.long_strike
                td["short_call"] = ticket.short_strike_call
                td["long_call"] = ticket.long_strike_call
                td["breakevens"] = (f"{ticket.short_strike - ticket.credit_per_contract:.0f} / "
                                     f"{ticket.short_strike_call + ticket.credit_per_contract:.0f}")
            lines.append(trade_ticket(td))

        return "\n".join(lines)

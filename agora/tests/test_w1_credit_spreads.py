"""
agora/tests/test_w1_credit_spreads.py — W1: unblock the high-win-rate engine (credit spreads).

The fix is NOT lowering the EV gate — it's constructing spreads that clear it. At 20Δ with the fixed
2-strike width, credit/width came in 0.08-0.13 (the 17/17-historical-losers zone) → rejected, so
credit spreads almost never traded. Selling ~30Δ lifts credit/width above the 0.30 gate → they
trade as positive-EV, ~70% POP structures. These tests pin (a) the config change, (b) the gate
stays as the EV guard, and (c) the EV math that justifies the threshold — so the reasoning can't
silently regress.
"""
from __future__ import annotations

from agora.core.config import get_settings


def _credit_spread_ev(credit_to_width: float, pop: float, width_dollars: float = 500.0) -> float:
    """Expectancy of a credit vertical given credit/width and probability-of-profit.
    credit = cr_w * width; max_gain = credit; max_loss = width - credit."""
    credit = credit_to_width * width_dollars
    max_gain = credit
    max_loss = width_dollars - credit
    return pop * max_gain - (1 - pop) * max_loss


# ── config: the W1 change + the EV guard it relies on ─────────────────────────
class TestW1Config:
    def test_credit_short_delta_raised(self):
        # ~30Δ short for credit verticals so credit/width clears the 0.30 EV gate (was 0.20)
        assert get_settings().credit_spread_short_delta >= 0.27

    def test_debit_short_delta_unchanged(self):
        # debit verticals + IC keep 0.20 — raising their short leg compresses the spread
        assert get_settings().short_delta_target == 0.20

    def test_ev_gate_unchanged(self):
        # the credit/width EV guard MUST stay — it's what makes the delta change safe
        assert get_settings().min_credit_to_width_ratio == 0.30


# ── the EV math that justifies the 0.30 credit/width line ─────────────────────
class TestCreditSpreadEV:
    def test_thin_spread_is_negative_ev(self):
        # the rejected zone: 0.13 credit/width even at a generous 80% POP loses (the 17/17 losers)
        assert _credit_spread_ev(0.13, pop=0.80) < 0

    def test_gate_line_is_about_breakeven(self):
        # 0.30 credit/width at ~70% POP (a 30Δ short) sits right around breakeven
        assert abs(_credit_spread_ev(0.30, pop=0.70)) < 20   # ~$0 on a $500-wide

    def test_above_gate_is_positive_ev(self):
        # a 30Δ short that collects 0.35 credit/width is clearly positive-EV
        assert _credit_spread_ev(0.35, pop=0.70) > 0

    def test_higher_credit_width_monotonically_better(self):
        evs = [_credit_spread_ev(cw, pop=0.70) for cw in (0.15, 0.25, 0.35, 0.45)]
        assert evs == sorted(evs)   # more credit per unit risk → higher EV

    def test_the_lone_historical_winner_was_positive(self):
        # the one bull_put winner sat at 0.61 credit/width — strongly positive even at 60% POP
        assert _credit_spread_ev(0.61, pop=0.60) > 0

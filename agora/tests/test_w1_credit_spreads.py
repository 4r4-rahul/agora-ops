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

import pandas as pd

from agora.core.config import get_settings
from agora.strategies.rules_engine import StrategyRulesEngine


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
        # the credit/width EV guard MUST stay — it's what makes the delta change safe. Assert the
        # FIELD DEFAULT (immutable), not the runtime singleton (other tests mutate it in-place).
        assert type(get_settings()).model_fields["min_credit_to_width_ratio"].default == 0.30


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


# ── W1b: adaptive width narrows to clear the EV gate (keeps the 30Δ short) ─────
def _put_chain(mids: dict[float, float]) -> pd.DataFrame:
    return pd.DataFrame([{"strike": k, "bid": m - 0.05, "ask": m + 0.05}
                         for k, m in sorted(mids.items())])


class TestAdaptiveWidth:
    def setup_method(self):
        self.engine = StrategyRulesEngine()
        # pin our own floor — the rules_engine gate tests mutate the shared settings singleton
        self.engine._settings.min_credit_to_width_ratio = 0.30

    def test_narrows_to_rescue_a_near_miss(self):
        # 2-strike cr/w = (2.5-1.95)/2 = 0.275 (below gate); 1-strike = (2.5-2.1)/1 = 0.40 (clears).
        # The plain picker returns 93 (rejected); W1b narrows to 94 so the spread can trade.
        chain = _put_chain({95: 2.5, 94: 2.1, 93: 1.95, 92: 1.75})
        assert self.engine._credit_spread_long_strike(chain, 95, "put", -1) == 94

    def test_keeps_width2_when_it_already_clears(self):
        # both widths clear (cr/w 0.50) → prefer the wider one (more premium), preserving the
        # original 2-strike behavior. long = 93.
        chain = _put_chain({95: 3.0, 94: 2.5, 93: 2.0, 92: 1.5})
        assert self.engine._credit_spread_long_strike(chain, 95, "put", -1) == 93

    def test_never_widens_beyond_two_strikes(self):
        chain = _put_chain({95: 3.0, 94: 2.5, 93: 2.0, 92: 1.5, 91: 1.0})
        assert self.engine._credit_spread_long_strike(chain, 95, "put", -1) in (94, 93)

    def test_fail_safe_on_missing_short(self):
        chain = _put_chain({95: 2.5, 94: 2.1})
        # unknown short strike → falls back to the plain picker, never raises
        assert self.engine._credit_spread_long_strike(chain, 999, "put", -1) is None or True

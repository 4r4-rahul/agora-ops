"""
RECALIBRATION 2026-06-27 — VOL_PREMIUM neutral routing.

Evidence: bull_put_spread won 10% (n=38, −$2,519), almost all in 'neutral' regime; the engine sold a
directionally-BULLISH credit spread on a NEUTRAL read. The expert root-cause + the data agree: a
neutral view must trade a NEUTRAL structure. This locks neutral → IRON_CONDOR (flag-gated, reversible)
and keeps bull_put_spread only for a genuine bullish read.
"""
import types

from agora.core.models import StrategyPillar, StrategyType
from agora.strategies.rules_engine import StrategyRulesEngine


def _select(direction, flag=True):
    stub = types.SimpleNamespace(
        _settings=types.SimpleNamespace(vol_premium_neutral_iron_condor=flag))
    conv = types.SimpleNamespace(pillar=StrategyPillar.VOL_PREMIUM)
    return StrategyRulesEngine._select_strategy(stub, conv, direction, None)[0]


class TestVolPremiumNeutralRouting:
    def test_neutral_routes_to_iron_condor_not_bull_put(self):
        # THE FIX: neutral → neutral structure, not a bullish credit spread (the 10%-win leak)
        assert _select("neutral") == StrategyType.IRON_CONDOR

    def test_bullish_still_bull_put_spread(self):
        assert _select("bullish") == StrategyType.BULL_PUT_SPREAD

    def test_bearish_still_bear_call_spread(self):
        assert _select("bearish") == StrategyType.BEAR_CALL_SPREAD

    def test_flag_off_restores_legacy(self):
        assert _select("neutral", flag=False) == StrategyType.BULL_PUT_SPREAD


def _paper(max_loss, *, mult=1.5, cap=800.0, floor=1, size_mult=1.0):
    s = types.SimpleNamespace(
        trading_mode="paper", risk_per_trade_dollars=150.0, max_contracts_per_trade=10,
        max_risk_per_trade_dollars=400.0, paper_min_contracts=floor,
        paper_contract_multiplier=mult, paper_max_risk_per_trade_dollars=cap)
    return StrategyRulesEngine._size_contracts(None, size_mult, max_loss, s)


class TestPaperRiskBackstop:
    """RECALIBRATION 2026-06-27: the live risk cap was DEAD in paper (uncapped sizing → the $800+
    catastrophe, n=37, −$6,176). Add a paper backstop (default $800) + drop the inflator 3.0→1.5.
    Causal, not confounded: corr(size,conviction)=0.10; large half loses more within every cell."""

    def test_paper_cap_clamps_total_risk(self):
        # $200/contract, big size_mult would buy many; cap $800 ⇒ at most 4 contracts (4×200=800).
        assert _paper(200.0, cap=800.0, size_mult=5.0) <= 4

    def test_paper_cap_skips_single_contract_over_cap(self):
        # one contract risks $900 > $800 cap ⇒ skip (0), matching live behavior. Was forced to >=floor.
        assert _paper(900.0, cap=800.0) == 0

    def test_paper_cap_keeps_profitable_zone(self):
        # a $500 single contract (the profitable $400-600 band) is NOT skipped — $400 cap would kill it.
        assert _paper(500.0, cap=800.0) >= 1

    def test_paper_cap_zero_disables(self):
        # cap=0 ⇒ legacy uncapped paper behavior (never returns 0 from the cap path).
        assert _paper(900.0, cap=0.0, floor=2) >= 2

    def test_per_ticker_risk_cap_overrides(self):
        # an explicit per-ticker risk_cap takes precedence over the global paper cap.
        s = types.SimpleNamespace(
            trading_mode="paper", risk_per_trade_dollars=150.0, max_contracts_per_trade=10,
            max_risk_per_trade_dollars=400.0, paper_min_contracts=1,
            paper_contract_multiplier=1.5, paper_max_risk_per_trade_dollars=800.0)
        # risk_cap=300 ⇒ a $400 single contract is skipped (over the per-ticker cap)
        assert StrategyRulesEngine._size_contracts(None, 1.0, 400.0, s, risk_cap=300.0) == 0

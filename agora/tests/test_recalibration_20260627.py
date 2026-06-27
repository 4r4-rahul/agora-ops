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

"""
agora/tests/test_tws_exit_marks.py — Stage 2: exit decisions mark off TWS/IBKR's exact per-leg
unrealizedPNL. Pins PositionManager._tws_unrealized: precise leg-by-leg matching (symbol+strike+
right+expiry), summed; None on any unmatched leg / absent feed (→ caller uses the yfinance fallback,
never a partial/guessed number). Tested via the unbound method on a stub — no db/manager needed.
"""
from __future__ import annotations

import types
from datetime import date, timedelta

from agora.core.models import OpenPosition, PositionStatus, SpreadLeg, StrategyPillar, StrategyType
from agora.lifecycle.position_manager import PositionManager

_EXP = date.today() + timedelta(days=30)
_EXPS = _EXP.strftime("%Y%m%d")


def _leg(option_type, strike, action="sell"):
    return SpreadLeg(option_type=option_type, strike=strike, expiration=_EXP, action=action,
                     contracts=1, mid_price=1.0)


def _pos(legs, ticker="IWM"):
    return OpenPosition(
        position_id="p1", ticker=ticker, strategy=StrategyType.BULL_PUT_SPREAD,
        pillar=StrategyPillar.DIRECTIONAL, status=PositionStatus.OPEN, legs=legs,
        contracts=1, entry_price=-1.2, current_price=-1.2, entry_date=date.today(),
        expiry_date=_EXP, target_close_date=date.today() + timedelta(days=9),
        max_loss_dollars=380.0, max_gain_dollars=120.0, unrealized_pnl=0.0)


def _item(symbol, strike, right, upnl):
    return {"symbol": symbol, "strike": strike, "right": right, "expiry": _EXPS,
            "unrealized_pnl": upnl}


def _call(getter, pos):
    return PositionManager._tws_unrealized(types.SimpleNamespace(_tws_pnl_getter=getter), pos)


class TestTwsUnrealized:
    def test_sums_matched_legs(self):
        # IWM 275P short / 277P long → TWS legs sum to the exact position P&L
        pos = _pos([_leg("put", 275.0, "sell"), _leg("put", 277.0, "buy")])
        items = [_item("IWM", 275.0, "P", 227.81), _item("IWM", 277.0, "P", -292.74)]
        assert _call(lambda: items, pos) == round(227.81 - 292.74, 2)   # -64.93

    def test_none_when_a_leg_unmatched(self):
        pos = _pos([_leg("put", 275.0), _leg("put", 277.0)])
        items = [_item("IWM", 275.0, "P", 227.81)]   # 277 leg missing
        assert _call(lambda: items, pos) is None

    def test_none_on_wrong_expiry(self):
        pos = _pos([_leg("put", 275.0)])
        items = [{"symbol": "IWM", "strike": 275.0, "right": "P",
                  "expiry": "20990101", "unrealized_pnl": 10.0}]
        assert _call(lambda: items, pos) is None

    def test_none_on_wrong_right(self):
        pos = _pos([_leg("put", 275.0)])
        items = [_item("IWM", 275.0, "C", 10.0)]   # call vs the put leg
        assert _call(lambda: items, pos) is None

    def test_none_when_no_getter_or_empty(self):
        pos = _pos([_leg("put", 275.0)])
        assert _call(None, pos) is None
        assert _call(lambda: [], pos) is None

    def test_safe_on_getter_error(self):
        pos = _pos([_leg("put", 275.0)])
        def _boom():
            raise RuntimeError("feed down")
        assert _call(_boom, pos) is None

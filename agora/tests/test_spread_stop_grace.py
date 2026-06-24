"""
test_spread_stop_grace.py — credit-spread stop grace (THE expectancy lever).

Credit spreads (biggest cohort) won 6% vs ~70% norm because the 2x-credit HARD STOP tripped on
DAY-1 mark noise (16/18 closed at 1.0d at a loss, entered 38-42 DTE). The grace suppresses the
hard stop on a credit spread within spread_stop_min_hold_days UNLESS a genuine blowout
(loss >= blowout_frac x max_loss). Verifies the 4 cases on the real _check_position_targets path.
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
from types import SimpleNamespace

from agora.core.config import AgoraSettings
from agora.core.models import OpenPosition, PositionStatus, SpreadLeg, StrategyPillar, StrategyType
from agora.lifecycle.position_manager import PositionManager


def _pm():
    s = AgoraSettings()
    s.spread_stop_min_hold_days = 3
    s.spread_stop_blowout_max_loss_frac = 0.85
    pm = PositionManager(settings=s)
    # Force the profit engine to HOLD so we deterministically reach the hard-stop block.
    pm._profit_engine.evaluate = lambda **k: SimpleNamespace(
        should_close=False, profit_pct=0.0, rule="HOLD", hwm_pct=0.0,
        effective_target=0.5, ratchet_stop_pct=-2.0, velocity_1h=0.0, theta_excess=0.0)
    pm._profit_engine.clear_position = lambda pid: None
    closed: list = []
    async def _close(pos, reason, **kw): closed.append(reason)
    async def _roll(pos): return False
    pm._close_position = _close
    pm._attempt_roll = _roll
    pm.get_realized_pnl_today = lambda: 0.0
    return pm, closed


def _credit_spread(*, held_days: int, unreal: float, max_loss: float = 380.0):
    exp = date.today() + timedelta(days=39)          # 39 DTE — well past the 21-DTE close
    leg = SpreadLeg(option_type="put", strike=95.0, expiration=exp, action="sell",
                    contracts=1, mid_price=1.2)
    return OpenPosition(
        position_id="cs1", ticker="NVDA", strategy=StrategyType.BULL_PUT_SPREAD,
        pillar=StrategyPillar.VOL_PREMIUM, direction="bullish", status=PositionStatus.OPEN,
        legs=[leg, SpreadLeg(option_type="put", strike=90.0, expiration=exp, action="buy",
                             contracts=1, mid_price=0.4)],
        contracts=1, entry_price=-1.20,              # credit collected -> negative
        current_price=2.4, entry_date=date.today() - timedelta(days=held_days),
        expiry_date=exp, target_close_date=exp - timedelta(days=18),
        max_loss_dollars=max_loss, max_gain_dollars=120.0, unrealized_pnl=unreal)


def _run(pm, pos):
    asyncio.new_event_loop().run_until_complete(pm._check_position_targets(pos))


def test_day0_2x_credit_loss_is_graced(pm_closed=None):
    pm, closed = _pm()
    # 2x-credit loss (-$240) on day 0, NOT near max_loss ($380) -> grace, no close
    _run(pm, _credit_spread(held_days=0, unreal=-240.0))
    assert closed == [], f"day-0 mark-noise stop should be graced, got {closed}"


def test_genuine_blowout_still_stops_within_grace():
    pm, closed = _pm()
    # loss near max_loss (-$340 >= 0.85*380=$323) on day 0 -> genuine blowout -> STOP
    _run(pm, _credit_spread(held_days=0, unreal=-340.0))
    # A genuine blowout MUST stop even in grace — now owned by the surveillance blowout backstop
    # (85% of max loss) which fires before the legacy hard stop. Either reason satisfies the intent.
    assert closed and ("Hard stop" in closed[0] or "Surveillance" in closed[0]), \
        f"blowout must stop even in grace, got {closed}"


def test_past_grace_stops_normally():
    pm, closed = _pm()
    # day 4 (>= grace 3), 2x-credit loss -> normal hard stop fires
    _run(pm, _credit_spread(held_days=4, unreal=-240.0))
    assert closed and "Hard stop" in closed[0], f"past grace should stop, got {closed}"


def test_debit_spread_not_graced():
    pm, closed = _pm()
    pos = _credit_spread(held_days=0, unreal=-240.0)
    pos.entry_price = 1.20            # DEBIT (positive) -> not a credit spread -> no grace
    _run(pm, pos)
    # No grace for debits → it stops immediately. With the adaptive structure stop LIVE (2026-06-24),
    # the surveillance debit stop now owns this exit and fires BEFORE the legacy hard stop (a debit down
    # 200% of premium is far past the per-ticker −65% stop). Either reason satisfies the intent.
    assert closed and ("Hard stop" in closed[0] or "Surveillance" in closed[0]), \
        f"debit spread should stop (no grace), got {closed}"

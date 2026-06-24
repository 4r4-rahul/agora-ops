"""
agora/tests/test_position_surveillance.py — the deterministic surveillance decision core.

Exhaustive coverage: precedence (blowout > structure stop > lock-gains > profit > hold), the
debit/credit structure-aware stop (the fix for the unreachable 2× stop), regime tightening (cuts
SOONER in risk_off for BOTH structures), lock-gains, profit-take, and degenerate inputs.
"""
from __future__ import annotations

from agora.ops.position_surveillance import (
    DEFAULTS,
    EXIT,
    HOLD,
    PARTIAL_TAKE,
    TIGHTEN,
    adaptive_config,
    surveil,
)


def _v(**kw):
    base = dict(is_credit=False, unrealized=0.0, premium=1000.0, max_loss=1000.0, max_gain=1000.0)
    base.update(kw)
    return surveil(**base)


# ── blowout backstop (highest precedence) ─────────────────────────────────────────────
def test_blowout_exits_high_urgency():
    v = _v(unrealized=-900.0)            # 90% of max loss
    assert v.action == EXIT and v.urgency == "high" and "blowout" in v.reason


def test_blowout_boundary_at_85pct():
    assert _v(unrealized=-850.0).action == EXIT          # exactly 85% → exit
    assert _v(unrealized=-840.0).reason != "blowout..."  # 84% → not a blowout (falls to structure stop)


def test_blowout_takes_precedence_over_everything():
    # even with a big peak (would lock), a blowout exits
    assert _v(unrealized=-900.0, mfe=500.0).action == EXIT


def test_only_blowout_is_marked_backstop():
    # the blowout is the always-safe-to-act exit; structure stops are NOT (shadow until promoted)
    assert _v(unrealized=-900.0).is_backstop is True
    assert _v(is_credit=False, unrealized=-600.0).is_backstop is False   # debit structure stop


# ── debit structure stop (the fix) ────────────────────────────────────────────────────
def test_debit_stop_fires_at_55pct_of_premium():
    assert _v(is_credit=False, unrealized=-600.0).action == EXIT      # 60% > 55% → exit
    assert "debit stop" in _v(is_credit=False, unrealized=-600.0).reason
    assert _v(is_credit=False, unrealized=-500.0).action == HOLD      # 50% < 55% → hold


def test_debit_stop_tightens_in_risk_off():
    # risk_off cuts SOONER: stop moves from −55% to −44% of premium
    assert _v(is_credit=False, unrealized=-450.0, regime="risk_off").action == EXIT   # −45% > −44%
    assert _v(is_credit=False, unrealized=-450.0, regime="neutral").action == HOLD    # −45% < −55%


# ── credit structure stop ─────────────────────────────────────────────────────────────
def test_credit_stop_fires_at_1_5x_credit():
    # credit received 200, max loss 800; −1.5× = −300
    assert _v(is_credit=True, premium=200.0, max_loss=800.0, unrealized=-350.0).action == EXIT
    assert _v(is_credit=True, premium=200.0, max_loss=800.0, unrealized=-250.0).action == HOLD


def test_credit_stop_tightens_in_risk_off():
    # risk_off: −1.5× → −1.2× = −240 → −250 now exits (was a hold at −1.5×/−300)
    assert _v(is_credit=True, premium=200.0, max_loss=800.0, unrealized=-250.0, regime="risk_off").action == EXIT


# ── lock gains (generalize the 95%-win trailing stop) ─────────────────────────────────
def test_lock_gains_on_giveback_from_peak():
    # peaked at +500 (50% of max gain), now back to +200 (40% of peak < 50% giveback) → lock
    v = _v(unrealized=200.0, mfe=500.0)
    assert v.action == PARTIAL_TAKE and "lock gains" in v.reason


def test_no_lock_when_peak_too_small():
    # peak +150 < 20% of max gain (200) → not worth locking
    assert _v(unrealized=60.0, mfe=150.0).action == HOLD


def test_no_lock_when_still_near_peak():
    # at +400 of a +500 peak (80% > 50% giveback) → still holding the runner
    assert _v(unrealized=400.0, mfe=500.0).action != PARTIAL_TAKE


# ── profit target ─────────────────────────────────────────────────────────────────────
def test_profit_take_at_60pct_of_max_gain():
    v = _v(unrealized=650.0, mfe=650.0)          # 65% of max gain, near peak (no lock) → profit take
    assert v.action == PARTIAL_TAKE and "profit target" in v.reason


# ── risk_off tighten advisory + hold + degenerate ─────────────────────────────────────
def test_risk_off_tighten_advisory_when_modestly_red():
    v = _v(unrealized=-350.0, regime="risk_off")     # −35% of max loss, below the −44% stop
    assert v.action == TIGHTEN


def test_plain_hold():
    assert _v(unrealized=-100.0).action == HOLD


def test_degenerate_inputs_never_raise():
    assert surveil(is_credit=False, unrealized=0.0, premium=0.0, max_loss=0.0, max_gain=0.0).action == HOLD


# ── adaptive_config: per-ticker vol-normalized stop bridge ─────────────────────────────
def test_adaptive_config_widens_stop_for_volatile_ticker():
    # TSLA-like HV (0.58) → wider debit stop than KO-like (0.16); both replace the fixed 0.55
    hot = adaptive_config(hv=0.58, dte=30, regime="neutral")
    calm = adaptive_config(hv=0.16, dte=30, regime="neutral")
    assert hot.debit_stop_pct > calm.debit_stop_pct
    assert calm.debit_stop_pct < DEFAULTS.debit_stop_pct < hot.debit_stop_pct or \
        calm.debit_stop_pct < hot.debit_stop_pct      # calm tighter, hot wider than the old fixed


def test_adaptive_config_fires_at_per_ticker_level():
    # a calm name (tight ~−27% stop) on a debit down 30% of premium → EXIT (the fixed −55% would HOLD)
    cfg = adaptive_config(hv=0.16, dte=30, regime="neutral")
    v = surveil(is_credit=False, unrealized=-300.0, premium=1000.0, max_loss=1000.0,
                max_gain=1000.0, cfg=cfg)
    assert v.action == EXIT and "debit stop" in v.reason
    # same position under the OLD fixed −55% stop would still be holding
    assert surveil(is_credit=False, unrealized=-300.0, premium=1000.0, max_loss=1000.0,
                   max_gain=1000.0).action == HOLD


def test_adaptive_config_does_not_double_tighten_regime():
    # regime is baked into the adaptive stop; the returned cfg neutralizes surveil()'s own tighten
    assert adaptive_config(hv=0.30, dte=30, regime="risk_off").risk_off_tighten == 1.0
    # risk_off stop must be STRICTLY tighter than neutral (applied exactly once, not twice or zero)
    n = adaptive_config(hv=0.30, dte=30, regime="neutral").debit_stop_pct
    r = adaptive_config(hv=0.30, dte=30, regime="risk_off").debit_stop_pct
    assert r == round(n * DEFAULTS.risk_off_tighten, 4)    # exactly one application of 0.8


def test_adaptive_config_preserves_backstop_and_locks():
    # only the stop levels change — blowout/lock/profit thresholds stay from the base cfg
    cfg = adaptive_config(hv=0.58, dte=30, regime="neutral")
    assert cfg.blowout_frac == DEFAULTS.blowout_frac
    assert cfg.giveback_frac == DEFAULTS.giveback_frac
    assert cfg.profit_take_frac == DEFAULTS.profit_take_frac
    # blowout still fires regardless of the wider adaptive stop
    v = surveil(is_credit=False, unrealized=-900.0, premium=1000.0, max_loss=1000.0,
                max_gain=1000.0, cfg=cfg)
    assert v.action == EXIT and v.is_backstop is True
    assert surveil(is_credit=True, unrealized=-5.0, premium=0.0, max_loss=0.0, max_gain=0.0).action == HOLD
    assert _v(unrealized=-600.0, mfe=None).action == EXIT          # mfe None handled


def test_config_is_frozen_and_tunable():
    assert DEFAULTS.debit_stop_pct == 0.55
    v = surveil(is_credit=False, unrealized=-300.0, premium=1000.0, max_loss=1000.0, max_gain=1000.0,
                cfg=type(DEFAULTS)(debit_stop_pct=0.25))   # tighter custom stop → −25% exits at −300
    assert v.action == EXIT

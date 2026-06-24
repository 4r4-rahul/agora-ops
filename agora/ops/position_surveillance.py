"""
agora/ops/position_surveillance.py — deterministic position-surveillance decision core.

The live exit logic is scattered across the 1,635-line PositionManager (a 2×-premium spread stop that
is UNREACHABLE for debits → losers rode to −92%, plus the profit engine, a separate long-option path,
and a stale-quote backstop). This module is the unified, PURE, zero-LLM decision core that watches
every open position each mark and fuses everything we know — structure (debit vs credit), the running
MFE/MAE excursion, market regime, per-ticker vol, and news — into one verdict:

    HOLD | TIGHTEN | PARTIAL_TAKE | EXIT   (+ reason, urgency, suggested stop level)

Design:
  • PURE — state in, verdict out. No IO, never raises on well-formed numerics → 99.99% unit-testable.
  • Grounded in the data: trailing/lock-gains is the ONLY profitable exit (95% win) → generalize it;
    the 2× stop is broken → a real structure-aware stop that actually fires; losers run in price not
    time → cut on price. Regime tightens the stop; blowout is an absolute backstop.
  • Shadow-first — PositionManager logs the verdict alongside its real action before anything acts.
"""
from __future__ import annotations

from dataclasses import dataclass

# Action constants
HOLD = "HOLD"
TIGHTEN = "TIGHTEN"
PARTIAL_TAKE = "PARTIAL_TAKE"
EXIT = "EXIT"


@dataclass(frozen=True)
class SurveillanceConfig:
    debit_stop_pct: float = 0.55       # exit a debit at −55% of premium (max loss IS the premium)
    credit_stop_mult: float = 1.5      # exit a credit at −1.5× the credit received
    blowout_frac: float = 0.85         # at ≥85% of max loss → exit regardless (absolute backstop)
    giveback_frac: float = 0.5         # gave back >50% of peak gain → lock it (the proven winner)
    min_peak_frac: float = 0.20        # only bother locking once peak reached ≥20% of max gain
    risk_off_tighten: float = 0.8      # in risk_off/high_vol, cut stops 20% sooner
    profit_take_frac: float = 0.6      # at ≥60% of max gain → scale out (partial)


DEFAULTS = SurveillanceConfig()
_RISK_OFF = {"risk_off", "high_volatility", "crisis"}


@dataclass(frozen=True)
class SurveillanceVerdict:
    action: str
    reason: str
    urgency: str = "low"          # "low" | "high"
    stop_level: float | None = None   # the dollar unrealized at which a stop would fire (debug/UI)
    is_backstop: bool = False     # True only for the blowout backstop — the always-safe-to-act exit


def surveil(*, is_credit: bool, unrealized: float, premium: float, max_loss: float,
            max_gain: float, mfe: float | None = None, regime: str = "neutral",
            cfg: SurveillanceConfig = DEFAULTS) -> SurveillanceVerdict:
    """Decide what to do with one open position. All dollar amounts are signed P&L from the position's
    own perspective (unrealized < 0 = losing). premium = debit paid or credit received (abs). mfe =
    peak unrealized P&L seen (running MFE); None if unknown. Pure — never raises on finite numbers."""
    premium = abs(premium or 0.0)
    max_loss = abs(max_loss or 0.0)
    max_gain = abs(max_gain or 0.0)
    tighten = cfg.risk_off_tighten if regime in _RISK_OFF else 1.0

    # 1. BLOWOUT backstop — near max loss, exit regardless of structure (catches the −92% riders).
    if max_loss > 0 and unrealized <= -cfg.blowout_frac * max_loss:
        return SurveillanceVerdict(EXIT, f"blowout: at {-unrealized/max_loss*100:.0f}% of max loss",
                                   "high", round(-cfg.blowout_frac * max_loss, 2), is_backstop=True)

    # 2. STRUCTURE-AWARE STOP — the fix. Debit: % of premium; credit: multiple of credit. Regime tightens.
    if not is_credit and premium > 0:
        stop = -cfg.debit_stop_pct * tighten * premium
        if unrealized <= stop:
            return SurveillanceVerdict(EXIT, f"debit stop: −{cfg.debit_stop_pct*tighten*100:.0f}% of premium",
                                       "high", round(stop, 2))
    elif is_credit and premium > 0:
        stop = -cfg.credit_stop_mult * tighten * premium
        if unrealized <= stop:
            return SurveillanceVerdict(EXIT, f"credit stop: −{cfg.credit_stop_mult*tighten:.1f}× credit",
                                       "high", round(stop, 2))

    # 3. LOCK GAINS — generalize the winning trailing stop: gave back >giveback_frac of a real peak.
    if mfe is not None and max_gain > 0 and mfe >= cfg.min_peak_frac * max_gain:
        if unrealized <= cfg.giveback_frac * mfe and unrealized > 0:
            return SurveillanceVerdict(PARTIAL_TAKE,
                                       f"lock gains: gave back to {unrealized/mfe*100:.0f}% of peak",
                                       "high")

    # 4. PROFIT TARGET — scale out a portion once meaningfully in profit.
    if max_gain > 0 and unrealized >= cfg.profit_take_frac * max_gain:
        return SurveillanceVerdict(PARTIAL_TAKE,
                                   f"profit target: {unrealized/max_gain*100:.0f}% of max gain", "low")

    # 5. Otherwise hold; in risk_off flag a TIGHTEN advisory once the position is modestly red.
    if regime in _RISK_OFF and max_loss > 0 and unrealized <= -0.3 * max_loss:
        return SurveillanceVerdict(TIGHTEN, "risk_off + position red — tighten management", "low")
    return SurveillanceVerdict(HOLD, "within tolerance", "low")

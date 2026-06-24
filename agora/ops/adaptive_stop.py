"""
agora/ops/adaptive_stop.py — per-ticker, volatility-normalized, time-aware stop engine (PURE).

Replaces the volatility-BLIND fixed −55% debit / −1.5× credit stop with a stop GENERATED per ticker
from its own realized vol, tightened as the option runs out of time (theta) and as the market turns
risk-off. The professional standard a real desk uses:

    • Stop WIDTH scales with the instrument's noise — a calm name (KO, HV 16%) is cut on a small
      adverse move; a volatile name (TSLA, HV 58%) is given room so normal noise doesn't whipsaw it.
    • PAIRED with a SIZE factor so every trade still risks the same fraction of capital (1R): the
      wider stop on a volatile name is offset by fewer contracts → constant $ risk.
    • TIGHTENED as expiry approaches (theta) — an option running out of time has less room to
      recover, so cut sooner. This is the "options run with time" axis, recomputed every mark.
    • TIGHTENED in risk_off regimes.

        debit_stop%      = clamp( base_stop × (HV / base_HV) × theta(DTE) × regime × iv )
        credit_stop_mult = clamp( base_mult × (HV / base_HV) × theta(DTE) × regime × iv )
        size_factor      = clamp( base_HV / HV )

PURE — numbers in, numbers out. No IO, never raises on finite input → 99.99% unit-testable. The IO
layer (surveillance / position-manager) resolves the per-ticker HV, DTE and regime and feeds them in.

HONEST SCOPE: HV (per-ticker), DTE (theta/time) and regime are REAL, reliably-available inputs.
A live intraday-IV factor is provided as an INERT hook (`iv_ratio`) — the system does not yet capture
per-position IV, so it defaults to 1.0 (no effect). It is NOT claimed to be active; wire it only once
per-position IV is actually captured. Faking it would corrupt the very stop it is meant to sharpen.
"""
from __future__ import annotations

from dataclasses import dataclass

_RISK_OFF = {"risk_off", "high_volatility", "crisis"}


@dataclass(frozen=True)
class AdaptiveStopConfig:
    base_stop_pct: float = 0.50       # debit stop (fraction of premium) at baseline vol
    base_credit_mult: float = 1.5     # credit stop (× credit received) at baseline vol
    base_hv: float = 0.30             # the "normal" annualized HV the base stop is calibrated to
    stop_floor: float = 0.25          # never tighter than −25% of premium (avoid noise whipsaw)
    stop_ceil: float = 0.65           # never wider than −65% (past this a debit is a near-blowout)
    credit_mult_floor: float = 0.9
    credit_mult_ceil: float = 2.2
    near_dte: int = 7                 # within this many days to expiry, start tightening for theta
    theta_floor: float = 0.7          # at expiry the stop is 70% of its far-dated width (cut sooner)
    risk_off_tighten: float = 0.8     # risk_off multiplies the stop width by this (cut 20% sooner)


DEFAULTS = AdaptiveStopConfig()


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def vol_factor(hv: float | None, cfg: AdaptiveStopConfig = DEFAULTS) -> float:
    """HV relative to baseline — the core per-ticker scaler. >1 widens for volatile names, <1 tightens
    for calm ones. Non-positive/None HV → 1.0 (no per-ticker adjustment)."""
    if not hv or hv <= 0:
        return 1.0
    return hv / cfg.base_hv


def theta_factor(dte: int | None, cfg: AdaptiveStopConfig = DEFAULTS) -> float:
    """Tighten as expiry approaches: an option running out of time has less room to recover → cut
    sooner. 1.0 when far from expiry (DTE ≥ near_dte), ramps linearly down to theta_floor at/after
    expiry. None DTE → 1.0 (no information). Monotonic non-decreasing in DTE."""
    if dte is None:
        return 1.0
    if dte >= cfg.near_dte:
        return 1.0
    if dte <= 0:
        return cfg.theta_floor
    return cfg.theta_floor + (1.0 - cfg.theta_floor) * (dte / cfg.near_dte)


def regime_factor(regime: str | None, cfg: AdaptiveStopConfig = DEFAULTS) -> float:
    """risk_off / high_volatility / crisis → tighten; everything else → 1.0. Case-insensitive."""
    return cfg.risk_off_tighten if str(regime or "").lower() in _RISK_OFF else 1.0


def iv_factor(iv_ratio: float | None) -> float:
    """INERT HOOK (no per-position IV capture yet → returns 1.0). When live IV exists, pass
    current_iv / entry_iv: a vega-driven loss (IV crush, ratio < 1) should NOT trip a directional
    stop (widen slightly); an IV spike (ratio > 1) adds risk (tighten). Bounded to [0.85, 1.15].
    Today it is inert by construction and is NOT a claimed live feature."""
    if iv_ratio is None or iv_ratio <= 0:
        return 1.0
    return _clamp(1.0 / (iv_ratio ** 0.25), 0.85, 1.15)


def adaptive_debit_stop_pct(hv: float | None, dte: int | None, regime: str | None, *,
                            iv_ratio: float | None = None,
                            cfg: AdaptiveStopConfig = DEFAULTS) -> float:
    """Per-position debit stop as a POSITIVE fraction of premium. Composes vol × theta × regime × iv,
    clamped to [stop_floor, stop_ceil]. Never raises on finite input."""
    raw = cfg.base_stop_pct * vol_factor(hv, cfg) * theta_factor(dte, cfg) \
        * regime_factor(regime, cfg) * iv_factor(iv_ratio)
    return round(_clamp(raw, cfg.stop_floor, cfg.stop_ceil), 4)


def adaptive_credit_stop_mult(hv: float | None, dte: int | None, regime: str | None, *,
                              iv_ratio: float | None = None,
                              cfg: AdaptiveStopConfig = DEFAULTS) -> float:
    """Per-position credit stop as a POSITIVE multiple of credit received. Composes the same factors,
    clamped to [credit_mult_floor, credit_mult_ceil]."""
    raw = cfg.base_credit_mult * vol_factor(hv, cfg) * theta_factor(dte, cfg) \
        * regime_factor(regime, cfg) * iv_factor(iv_ratio)
    return round(_clamp(raw, cfg.credit_mult_floor, cfg.credit_mult_ceil), 4)


def size_factor(hv: float | None, cfg: AdaptiveStopConfig = DEFAULTS) -> float:
    """Risk-parity sizing companion: position scales INVERSELY with vol so $ risk per trade is ~constant
    around the adaptive stop (a volatile name's wider stop is offset by fewer contracts; a calm name's
    tighter stop allows more). PURE vol-math — NO artificial floor/ceil clamp (the machine decides the
    multiplier from realized vol): a very calm name can scale up several×, a very volatile name down
    toward zero. The ONLY bound is downstream and PHYSICAL — max_contracts_per_trade (fillability /
    buying-power) and the 1-contract minimum in _size_contracts — NOT an adaptivity cap. Returns 1.0
    only when vol is unknown/garbage (hv ≤ 0)."""
    if not hv or hv <= 0:
        return 1.0
    return round(cfg.base_hv / hv, 3)


def explain(ticker: str, hv: float | None, dte: int | None, regime: str | None, *,
            iv_ratio: float | None = None, cfg: AdaptiveStopConfig = DEFAULTS) -> dict:
    """One dict describing the full adaptive stop for a ticker/position — for the UI and shadow logging.
    Exposes every factor so the decision is transparent (no black box). size_factor is the PURE vol-math
    multiplier (no adaptivity cap); the physical max_contracts bound is applied in _size_contracts."""
    return {
        "ticker": ticker,
        "hv": round(hv, 4) if hv else None,
        "dte": dte,
        "regime": regime,
        "debit_stop_pct": adaptive_debit_stop_pct(hv, dte, regime, iv_ratio=iv_ratio, cfg=cfg),
        "credit_stop_mult": adaptive_credit_stop_mult(hv, dte, regime, iv_ratio=iv_ratio, cfg=cfg),
        "size_factor": size_factor(hv, cfg),
        "vol_factor": round(vol_factor(hv, cfg), 3),
        "theta_factor": round(theta_factor(dte, cfg), 3),
        "regime_factor": round(regime_factor(regime, cfg), 3),
        "iv_factor": round(iv_factor(iv_ratio), 3),
    }

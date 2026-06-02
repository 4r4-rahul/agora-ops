"""
agora/ops/dynamic_params.py — Adaptive market parameter engine.

Computes session-level parameters that scale with the current market environment.
No LLM calls. Pure deterministic logic from market state + recent trade history.

Called once at session startup and refreshed every 2 hours (or on regime change).
All agents and the rules engine consume DynamicParams instead of hardcoded config values.

Parameters adapted:
  short_delta_target      IVR-based: high IV → go further OTM (0.15), low IV → closer (0.25)
  profit_target_pct       regime-based: trending → take profit early, slow → let theta run
  max_open_positions      correlation/regime: risk_off → 2, dispersed → 5
  daily_loss_limit_pct    win-rate adaptive: tighten after 3 consecutive losses
  target_dte_adjustment   vol term structure: inverted → shorten DTE, normal → extend
  sector_spike_cap_pct    VIX-based: VIX<18 → 6%, VIX>25 → 12%
  kelly_fraction          fractional Kelly after 30+ closed trades (else None)
  min_conviction_score    opportunity density: earnings season → 55, dry month → 65
  ivr_bypass_threshold    VIX-scaled: VIX>25 means you need higher IVR to justify bypass
  stop_loss_multiplier    GEX regime: negative GEX markets move fast → wider stops
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class DynamicParams:
    """Adaptive parameters computed from current market state."""

    # ── Strike selection ───────────────────────────────────────────
    short_delta_target: float = 0.20      # 0.15 (high IV) → 0.25 (low IV)

    # ── Profit targets ─────────────────────────────────────────────
    profit_target_pct: float = 0.50       # 0.35 (fast trend) → 0.65 (slow theta)
    profit_target_pct_short_dte: float = 0.75

    # ── Position limits ────────────────────────────────────────────
    max_open_positions: int = 4           # 2 (risk_off) → 5 (dispersed/normal)

    # ── Loss limits ────────────────────────────────────────────────
    daily_loss_limit_pct: float = 0.02    # tightens to 0.015 after 3 consecutive losses
    weekly_loss_limit_pct: float = 0.06

    # ── Stop loss ──────────────────────────────────────────────────
    stop_loss_multiplier: float = 2.0     # 1.5 (mean-reverting) → 2.5 (trending/GEX neg)

    # ── Conviction gates ───────────────────────────────────────────
    min_conviction_score: float = 60.0    # 55 (earnings season) → 65 (dry period)
    ivr_bypass_threshold: float = 60.0    # 55 (VIX<15) → 70 (VIX>25)

    # ── DTE ────────────────────────────────────────────────────────
    dte_adjustment: int = 0               # -7 (inverted term structure) → +7 (normal steep)

    # ── Sector momentum ────────────────────────────────────────────
    sector_spike_cap_pct: float = 8.0     # 6 (VIX<18) → 12 (VIX>25)

    # ── Kelly sizing ───────────────────────────────────────────────
    kelly_fraction: float | None = None   # None until 30+ closed trades

    # ── Diagnostics ────────────────────────────────────────────────
    computed_at: str = field(default_factory=lambda: datetime.now(tz=timezone.utc).isoformat())
    vix_level: float = 18.0
    ivr_level: float = 50.0
    regime_label: str = "normal"
    reasoning: list[str] = field(default_factory=list)


def compute_dynamic_params(
    vix: float | None,
    iv_rank: float | None,
    macro_stance: str,
    gex_regime: str,
    db_path: str,
) -> DynamicParams:
    """
    Compute all adaptive parameters from current market state + trade history.

    Args:
        vix:          Current VIX level (None → defaults to 18)
        iv_rank:      Current IV rank 0-100 (None → defaults to 50)
        macro_stance: "risk_on" | "risk_off" | "neutral" | "normal"
        gex_regime:   "positive" | "negative" | "neutral"
        db_path:      Path to AGORA SQLite database

    Returns:
        DynamicParams with all adaptive values set
    """
    vix_val  = vix if vix is not None else 18.0
    ivr_val  = iv_rank if iv_rank is not None else 50.0
    reasons: list[str] = []

    p = DynamicParams(vix_level=vix_val, ivr_level=ivr_val)

    # ── 1. Short delta target — IVR-based ─────────────────────────
    if ivr_val >= 70:
        p.short_delta_target = 0.15     # high IV: go further OTM, still collect good premium
        reasons.append(f"delta=0.15 (IVR={ivr_val:.0f}≥70: rich premium at lower delta)")
    elif ivr_val >= 50:
        p.short_delta_target = 0.18
        reasons.append(f"delta=0.18 (IVR={ivr_val:.0f}: moderate premium)")
    elif ivr_val >= 30:
        p.short_delta_target = 0.20     # default
        reasons.append(f"delta=0.20 (IVR={ivr_val:.0f}: normal)")
    else:
        p.short_delta_target = 0.25     # low IV: go closer to ATM to collect enough
        reasons.append(f"delta=0.25 (IVR={ivr_val:.0f}<30: thin premium, need higher delta)")

    # ── 2. Profit target — regime + VIX speed ─────────────────────
    if macro_stance in ("risk_on",) and vix_val < 16:
        p.profit_target_pct = 0.45      # trending fast — take profit at 45% (less theta left)
        p.profit_target_pct_short_dte = 0.65
        reasons.append("profit_target=45% (risk_on + VIX<16: fast-moving, take early)")
    elif macro_stance in ("risk_off", "high_volatility") or vix_val > 24:
        p.profit_target_pct = 0.40      # vol spikes mean faster decay — exit before reversal
        p.profit_target_pct_short_dte = 0.60
        reasons.append("profit_target=40% (risk_off/VIX>24: exit before reversal)")
    else:
        p.profit_target_pct = 0.50      # standard tastytrade 50% rule
        p.profit_target_pct_short_dte = 0.75

    # ── 3. Max open positions — regime/correlation ─────────────────
    if macro_stance == "risk_off" or vix_val > 26:
        p.max_open_positions = 2        # everything correlated in risk-off
        reasons.append("max_positions=2 (risk_off/VIX>26: high correlation)")
    elif macro_stance == "risk_on" and vix_val < 16:
        p.max_open_positions = 5        # low correlation, trend environment
        reasons.append("max_positions=5 (risk_on + VIX<16: low correlation)")
    else:
        p.max_open_positions = 4

    # ── 4. Daily loss limit — recent win rate ──────────────────────
    consecutive_losses = _count_consecutive_losses(db_path)
    if consecutive_losses >= 3:
        p.daily_loss_limit_pct = 0.015  # tighten after losing streak
        p.weekly_loss_limit_pct = 0.045
        reasons.append(f"loss_limit=1.5% ({consecutive_losses} consecutive losses — tightened)")
    elif consecutive_losses == 0:
        p.daily_loss_limit_pct = 0.025  # winning — can be slightly more aggressive
        p.weekly_loss_limit_pct = 0.07
    else:
        p.daily_loss_limit_pct = 0.020
        p.weekly_loss_limit_pct = 0.060

    # ── 5. Stop loss multiplier — GEX regime ──────────────────────
    if gex_regime == "negative":
        p.stop_loss_multiplier = 2.5    # trending/amplifying: moves are real, need wider stop
        reasons.append("stop_loss=2.5x (GEX negative: trending market, wider stops)")
    elif gex_regime == "positive":
        p.stop_loss_multiplier = 1.5    # mean-reverting: tight stops capture reversals
        reasons.append("stop_loss=1.5x (GEX positive: mean-reverting, tighter stops)")
    else:
        p.stop_loss_multiplier = 2.0

    # ── 6. Min conviction score — opportunity density ──────────────
    # Proxy for earnings season: Q1(Jan-Mar), Q2(Apr-Jun), Q3(Jul-Sep), Q4(Oct-Dec)
    # Peak earnings: weeks 3-6 of each quarter
    from datetime import date
    today = date.today()
    week_of_quarter = (today.month % 3) * 4 + today.day // 7
    in_earnings_season = 2 <= week_of_quarter <= 6
    if in_earnings_season:
        p.min_conviction_score = 55.0   # more catalysts available — lower bar
        reasons.append("conviction=55 (earnings season: more opportunities)")
    elif macro_stance == "risk_off":
        p.min_conviction_score = 65.0   # risk-off: be selective
        reasons.append("conviction=65 (risk_off: raised bar)")
    else:
        p.min_conviction_score = 60.0

    # ── 7. IVR bypass threshold — VIX-scaled ──────────────────────
    if vix_val > 25:
        p.ivr_bypass_threshold = 70.0   # high VIX: need higher IVR to justify credit
        reasons.append("ivr_bypass=70 (VIX>25: need richer premium vs realized)")
    elif vix_val < 14:
        p.ivr_bypass_threshold = 55.0   # low VIX: IVR 55 is already elevated
        reasons.append("ivr_bypass=55 (VIX<14: already elevated relative to realized)")
    else:
        p.ivr_bypass_threshold = 60.0

    # ── 8. DTE adjustment — vol term structure proxy ───────────────
    # When VIX > 20-day rolling avg (inverted term structure), shorten DTE to capture fast decay
    if vix_val > 23:
        p.dte_adjustment = -7           # shorter DTE during vol spikes
        reasons.append("dte_adj=-7 (VIX>23: inverted term structure likely, shorten DTE)")
    elif vix_val < 14:
        p.dte_adjustment = +7           # extend DTE in calm markets for more premium
        reasons.append("dte_adj=+7 (VIX<14: normal term structure, extend DTE for premium)")
    else:
        p.dte_adjustment = 0

    # ── 9. Sector spike cap — VIX-based ───────────────────────────
    if vix_val > 25:
        p.sector_spike_cap_pct = 12.0   # in high-vol markets, 12% moves are sector-specific
        reasons.append("spike_cap=12% (VIX>25: large moves are normal, not spikes)")
    elif vix_val < 16:
        p.sector_spike_cap_pct = 6.0    # low vol: any 6%+ sector move is unusual
        reasons.append("spike_cap=6% (VIX<16: 6%+ is outlier in calm market)")
    else:
        p.sector_spike_cap_pct = 8.0

    # ── 10. Kelly fraction — from closed trade history ─────────────
    p.kelly_fraction = _compute_kelly(db_path)
    if p.kelly_fraction is not None:
        reasons.append(f"kelly={p.kelly_fraction:.2f} (from closed trade history)")

    # ── Regime label ───────────────────────────────────────────────
    if vix_val > 28 or macro_stance == "risk_off":
        p.regime_label = "risk_off"
    elif vix_val < 16 and macro_stance in ("risk_on", "neutral"):
        p.regime_label = "risk_on"
    else:
        p.regime_label = macro_stance or "normal"

    p.reasoning = reasons
    logger.info(
        "DynamicParams: delta=%.2f profit=%.0f%% positions=%d "
        "loss_limit=%.1f%% stop=%.1fx conviction=%.0f "
        "ivr_bypass=%.0f dte_adj=%+d spike_cap=%.0f%% kelly=%s | VIX=%.1f IVR=%.0f regime=%s",
        p.short_delta_target, p.profit_target_pct * 100, p.max_open_positions,
        p.daily_loss_limit_pct * 100, p.stop_loss_multiplier, p.min_conviction_score,
        p.ivr_bypass_threshold, p.dte_adjustment, p.sector_spike_cap_pct,
        f"{p.kelly_fraction:.2f}" if p.kelly_fraction else "n/a",
        vix_val, ivr_val, p.regime_label,
    )
    return p


# ── SQLite helpers ────────────────────────────────────────────────────────────

def _count_consecutive_losses(db_path: str) -> int:
    """Count consecutive losses in the most recent closed trades."""
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                """
                SELECT outcome FROM decision_chains
                WHERE outcome IN ('filled', 'profit', 'loss', 'stopped')
                ORDER BY completed_at DESC
                LIMIT 10
                """
            ).fetchall()
        count = 0
        for (outcome,) in rows:
            if outcome in ("loss", "stopped"):
                count += 1
            else:
                break
        return count
    except Exception:
        return 0


def _compute_kelly(db_path: str) -> float | None:
    """
    Compute fractional Kelly from closed trade P&L history.
    Returns None if < 30 closed trades are available.
    Uses half-Kelly for conservatism.
    """
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                """
                SELECT pnl_pct FROM trade_outcomes
                WHERE pnl_pct IS NOT NULL
                ORDER BY closed_at DESC
                LIMIT 60
                """
            ).fetchall()
        if len(rows) < 30:
            return None

        pnls = [r[0] for r in rows]
        wins  = [p for p in pnls if p > 0]
        losses = [abs(p) for p in pnls if p < 0]

        if not wins or not losses:
            return None

        win_rate  = len(wins) / len(pnls)
        avg_win   = sum(wins) / len(wins)
        avg_loss  = sum(losses) / len(losses)
        if avg_loss == 0:
            return None

        # Kelly formula: W/L - (1-W)/W_ratio = p - (1-p)/b
        b = avg_win / avg_loss
        kelly = win_rate - (1 - win_rate) / b

        # Half-Kelly for conservatism, clamped to [0.25, 1.5]
        half_kelly = max(0.25, min(1.50, kelly * 0.5))
        return round(half_kelly, 2)
    except Exception:
        return None

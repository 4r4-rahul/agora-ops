"""
Professional Profit Management Engine for AGORA options positions.

Design philosophy (professional options desk standard):
  A position's profit comes from three sources — time decay (theta), IV compression
  (vega), and directional movement (delta). Each has different reliability and different
  optimal exit behavior. A fixed % target treats all profit the same. This engine doesn't.

Core capabilities:
  1. Profit decomposition   — separate theta P&L from directional/IV P&L
  2. Ratchet stop           — dynamic stop that tightens as profit accumulates (never widens)
  3. DTE-curve target       — smooth exit target as function of time remaining
  4. P&L velocity           — rate of change over last N readings (momentum aware)
  5. Regime-conditional     — headwind/tailwind adjusts targets based on current macro
  6. Greeks drift alert     — detects when spread delta/vega shifted beyond entry profile
  7. Lock-in tiers          — hard ceiling close levels (85%, 92%)
  8. Short-DTE flat target  — sector_momentum/event plays bypass the engine (flat 75%)
  9. Price target extension — when PriceTargetAgent bull/bear scenario implies ≥20% aligned
                              underlying move, DTE-curve target raised proportionally.
                              ≥100% upside (MSTR, RKLB moonshots) → hold to LOCK_IN ceiling.
 10. Discounted-entry bonus — actual IBKR fill better than mid-price recommendation raises all
                              targets by half the fill improvement (capped at +5%).

Ratchet stop schedule (fraction of max_gain, ratchets UP only):
  profit ≥ 0%:  stop at -200% (2× entry — hard floor only, before ratchet activates)
  profit ≥ 25%: ratchet to -100% (1× entry — reduce risk budget)
  profit ≥ 40%: ratchet to  -15% (near-breakeven — protect capital)
  profit ≥ 55%: ratchet to  +25% (lock in 25% of max)
  profit ≥ 70%: ratchet to  +45% (lock in 45% of max)
  profit ≥ 85%: ratchet to  +65% (lock in 65% — and also trigger LOCK_IN close)

Exit target curve (long-DTE positions, adjusts ±10% for headwind/tailwind):
  DTE > 38:       70%   (patient — theta still has 2/3 of work ahead)
  32 < DTE ≤ 38:  60%   (early acceleration — theta gaining speed)
  24 < DTE ≤ 32:  50%   (standard zone — maximum theta burn rate)
  DTE ≤ 24:       40%   (urgency — gamma risk rising, 21-DTE rule near)

Profit decomposition (directional excess):
  - Estimate theta P&L from entry daily theta × days held
  - If actual P&L > 1.5× theta P&L: directional excess (unreliable) → lower target by 10%
  - If actual P&L < 0.5× theta P&L: underperforming theta → underlying moved against, monitor
"""

from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from ..agents.macro_synthesizer import MacroContext
    from ..core.models import OpenPosition

logger = logging.getLogger(__name__)

# ── Short-DTE bypass (flat target, skip engine) ─────────────────────────────────
_SHORT_DTE_PILLARS    = {"sector_momentum", "event_fomc", "event_cpi", "catalyst"}
_SHORT_DTE_MAX        = 14      # positions with ≤14 DTE get flat target

# ── Exit target curve breakpoints (fraction of max_gain needed to close) ────────
_DTE_CURVE: list[tuple[int, float]] = [
    (38, 0.70),   # DTE > 38: very patient
    (32, 0.60),   # 32 < DTE ≤ 38: early acceleration
    (24, 0.50),   # 24 < DTE ≤ 32: standard
    (0,  0.40),   # DTE ≤ 24: urgency (21-DTE rule fires shortly)
]

# ── Ratchet stop schedule: (profit_pct_threshold, new_stop_pct_of_max_gain) ────
# Stop is fraction of max_gain. Negative = loss allowed; positive = min gain locked.
_RATCHET_SCHEDULE: list[tuple[float, float]] = [
    (0.85, 0.65),   # at 85% profit → lock in 65% (and also triggers LOCK_IN exit)
    (0.70, 0.45),   # at 70% profit → lock in 45% minimum
    (0.55, 0.25),   # at 55% profit → lock in 25% minimum
    (0.40, -0.15),  # at 40% profit → breakeven (allow -15% slippage only)
    (0.25, -1.00),  # at 25% profit → 1× entry risk (was 2×)
    (0.00, -2.00),  # baseline: 2× entry risk (matches hard stop setting)
]

# ── Directional excess threshold ────────────────────────────────────────────────
_DIRECTIONAL_EXCESS_RATIO = 1.5   # actual > 1.5× expected theta → lower target
_THETA_UNDERPERFORM_RATIO = 0.5   # actual < 0.5× expected theta → underlying drifting

# ── Regime adjustments ──────────────────────────────────────────────────────────
_HEADWIND_ADJ  = -0.10   # macro against position direction → lower target
_TAILWIND_ADJ  = +0.10   # macro with position direction → raise target
_LOCK_IN_PCT   = 0.85    # always close — last 15% not worth gamma risk

# ── Price target extension tiers (aligned_return_pct → DTE-curve adjustment) ────
# "aligned" means: bull return for bullish positions, abs(bear return) for bearish.
_PT_TIERS: list[tuple[float, float | None]] = [
    (1.00, None),   # ≥100% move expected → extend to LOCK_IN ceiling (diff from base)
    (0.50, 0.25),   # 50-100% → +25% to DTE-curve
    (0.20, 0.15),   # 20-50%  → +15% to DTE-curve
]
_FILL_BONUS_MAX_ADJ = 0.05   # cap fill-quality target raise at +5%

# ── P&L velocity window ─────────────────────────────────────────────────────────
_VELOCITY_WINDOW = 60   # keep last 60 lifecycle readings (~60 minutes at 60s ticks)
                        # velocity computation requires ≥30 min span — needs enough history


# ── Per-position state ──────────────────────────────────────────────────────────

@dataclass
class PositionState:
    """Mutable state tracked per-position across lifecycle ticks."""
    # High-water mark (0.0 → 1.0 fraction of max_gain)
    hwm: float = 0.0
    # Ratchet stop level (fraction of max_gain; negative means loss allowed)
    ratchet_stop: float = -2.0
    # Entry Greeks (captured at add_position time)
    entry_theta_daily: float = 0.0   # net daily theta collected per day (positive = we collect)
    entry_net_delta:   float = 0.0   # net delta at entry
    entry_net_vega:    float = 0.0   # net vega at entry
    dte_at_entry:      int   = 45    # DTE when position opened
    # P&L history for velocity calculation
    pnl_history: deque = field(default_factory=lambda: deque(maxlen=_VELOCITY_WINDOW))
    # Speed flag: True once target hit in < 7 days
    speed_held: bool = False
    # Spread type — set once at register_position from entry_theta_daily sign.
    # Credit spread (entry_theta_daily > 0): flat 50% target — theta works for us.
    # Debit spread  (entry_theta_daily ≤ 0): declining DTE-curve — we pay theta daily.
    is_credit_spread: bool = False
    # Price target intelligence (set from PriceTargetAgent post-fill)
    # aligned_pt_return: expected underlying move in the position's direction (e.g. 0.50 = 50%)
    # Always positive — caller normalises bull for bullish, abs(bear) for bearish.
    aligned_pt_return: float = 0.0
    entry_spot:        float = 0.0    # underlying spot at fill time (for logging)
    # Fill quality (set from IBKR fill confirmation)
    # Positive = we got a better-than-mid price (more credit / less debit).
    # Caller is responsible for sign: credit_spread → (fill-mid)/mid; debit → (mid-fill)/mid.
    fill_bonus_pct: float = 0.0
    # Cached last evaluate() result — used by dashboard to expose engine state
    last_decision: ProfitDecision | None = field(default=None, repr=False)


# ── Decision output ─────────────────────────────────────────────────────────────

@dataclass
class ProfitDecision:
    should_close:      bool
    reason:            str
    rule:              str    # LOCK_IN|RATCHET_STOP|DTE_CURVE|SPEED|RESCUE|SHORT_DTE|HOLD
    effective_target:  float  # target that triggered close (or current target if HOLD)
    profit_pct:        float
    hwm_pct:           float
    ratchet_stop_pct:  float  # current ratchet stop level
    velocity_1h:       float  # P&L change rate (%/hour), positive = improving
    theta_excess:      float  # actual_pnl / expected_theta_pnl ratio (>1.5 = directional win)


class _Metrics(NamedTuple):
    profit_pct:       float
    days_held:        int
    dte:              int
    theta_pnl_est:    float   # theta's theoretical contribution to profit (fraction of max_gain)
    theta_excess:     float   # actual / theta_expected ratio
    velocity_1h:      float   # %/hour (from pnl_history)
    current_delta:    float   # net portfolio delta of this position
    current_vega:     float   # net portfolio vega


# ── Engine ──────────────────────────────────────────────────────────────────────

class IntelligentProfitEngine:
    """
    Stateful per-session engine. Call register_position() at entry,
    evaluate() every lifecycle tick, clear_position() on close.
    """

    def __init__(self) -> None:
        self._states: dict[str, PositionState] = {}
        self._macro: MacroContext | None = None

    # ── Public API ──────────────────────────────────────────────────────────────

    def set_macro_context(self, ctx: MacroContext | None) -> None:
        self._macro = ctx

    def register_position(self, position: OpenPosition, is_existing: bool = False) -> None:
        """
        Capture entry-time Greeks. Safe to call multiple times — idempotent.

        Args:
            position:    The open position (new or loaded from DB).
            is_existing: True when loading from DB on startup — initialises HWM and
                         ratchet stop from current unrealized P&L so running positions
                         aren't artificially reset to zero protection.
        """
        if position.position_id in self._states:
            return
        state = PositionState()
        today = date.today()
        state.dte_at_entry = (position.expiry_date - today).days

        for leg in position.legs:
            sign = -1.0 if leg.action == "buy" else +1.0
            contracts_100 = position.contracts * leg.contracts * 100
            state.entry_theta_daily += sign * abs(leg.theta or 0.0) * contracts_100
            state.entry_net_delta   += sign * (leg.delta or 0.0) * contracts_100
            state.entry_net_vega    += sign * (leg.vega  or 0.0) * contracts_100

        # Credit spread: net theta > 0 (we collect premium daily).
        # Debit spread:  net theta ≤ 0 (we pay theta daily — need directional move).
        # This drives the entire exit target logic below.
        state.is_credit_spread = state.entry_theta_daily > 0

        # Existing positions: seed HWM from current P&L and advance ratchet stop
        # so positions already deep in-profit aren't unprotected after restart.
        if is_existing and position.max_gain_dollars > 0:
            current_pct = position.unrealized_pnl / position.max_gain_dollars
            if current_pct > 0:
                state.hwm          = current_pct
                state.ratchet_stop = self._advance_ratchet(-2.0, current_pct)
                logger.info(
                    "ProfitEngine: existing %s seeded HWM=%.0f%% ratchet_stop=%+.0f%%",
                    position.ticker, current_pct * 100, state.ratchet_stop * 100,
                )

        self._states[position.position_id] = state
        logger.debug(
            "ProfitEngine: registered %s theta=%.3f/day delta=%.2f vega=%.2f "
            "DTE@entry=%d existing=%s",
            position.ticker, state.entry_theta_daily,
            state.entry_net_delta, state.entry_net_vega, state.dte_at_entry, is_existing,
        )

    def clear_position(self, position_id: str) -> None:
        self._states.pop(position_id, None)

    def set_price_target(
        self,
        position_id: str,
        aligned_return_pct: float,
        entry_spot: float,
    ) -> None:
        """
        Attach PriceTargetAgent directional analysis to a registered position.

        Args:
            position_id:       Must match a registered position.
            aligned_return_pct: Expected underlying move in the position's direction (0-N).
                               Caller normalises: bull return for bullish positions,
                               abs(bear return) for bearish positions.
            entry_spot:        Underlying spot price at fill time (for logging/reference).
        """
        state = self._states.get(position_id)
        if state is None:
            return
        state.aligned_pt_return = max(0.0, aligned_return_pct)
        state.entry_spot = entry_spot
        tier = (
            f"MOONSHOT (+{aligned_return_pct*100:.0f}% underlying → spread at max profit)"
            if aligned_return_pct >= 1.00
            else f"+{aligned_return_pct*100:.0f}% aligned upside"
        )
        adj = self._pt_adj(aligned_return_pct, 0.50)  # preview adj at 50% DTE-curve base
        logger.info(
            "ProfitEngine: price target attached | %s | spot=$%.2f | "
            "DTE-curve extension +%.0f%% (example at 50%% base)",
            tier, entry_spot, adj * 100,
        )

    def set_fill_quality(self, position_id: str, fill_bonus_pct: float) -> None:
        """
        Record actual fill quality vs mid-price recommendation.

        Args:
            position_id:   Must match a registered position.
            fill_bonus_pct: Signed improvement fraction — caller is responsible for sign:
                            credit spread → (fill_price - mid_price) / mid_price
                            debit spread  → (mid_price  - fill_price) / mid_price
                            Positive = better fill = targets raised by fill_bonus_pct/2
                            (capped at +5%).
        """
        state = self._states.get(position_id)
        if state is None:
            return
        state.fill_bonus_pct = fill_bonus_pct
        adj = min(_FILL_BONUS_MAX_ADJ, max(0.0, fill_bonus_pct) / 2)
        if abs(fill_bonus_pct) >= 0.01:
            logger.info(
                "ProfitEngine: fill quality %+.1f%% (actual vs mid) → target raised +%.1f%%",
                fill_bonus_pct * 100, adj * 100,
            )

    def evaluate(
        self,
        position: OpenPosition,
        realized_pnl_today: float,
        short_dte_flat_target: float = 0.75,
        credit_spread_flat_target: float = 0.50,
        portfolio_daily_loss_limit: float = -999_999.0,
    ) -> ProfitDecision:
        """Public wrapper: calls _evaluate_inner() and caches the result for dashboard reads."""
        decision = self._evaluate_inner(
            position, realized_pnl_today,
            short_dte_flat_target, credit_spread_flat_target,
            portfolio_daily_loss_limit,
        )
        state = self._states.get(position.position_id)
        if state is not None:
            state.last_decision = decision
        return decision

    def get_last_decision(self, position_id: str) -> ProfitDecision | None:
        """Return the cached result of the last evaluate() call for a position."""
        state = self._states.get(position_id)
        return state.last_decision if state is not None else None

    def get_state_snapshot(self, position_id: str) -> dict | None:
        """Serialisable snapshot of engine state for API/dashboard consumption."""
        state = self._states.get(position_id)
        if state is None:
            return None
        d = state.last_decision
        return {
            "is_credit_spread":    state.is_credit_spread,
            "dte_at_entry":        state.dte_at_entry,
            "aligned_pt_return":   round(state.aligned_pt_return * 100, 1),
            "fill_bonus_pct":      round(state.fill_bonus_pct * 100, 1),
            "hwm_pct":             round((d.hwm_pct if d else state.hwm) * 100, 1),
            "ratchet_floor_pct":   round((d.ratchet_stop_pct if d else state.ratchet_stop) * 100, 1),
            "effective_target_pct": round((d.effective_target if d else 0.50) * 100, 1),
            "profit_pct":          round((d.profit_pct if d else 0.0) * 100, 1),
            "velocity_1h":         round(d.velocity_1h if d else 0.0, 4),
            "last_rule":           d.rule if d else "PENDING",
            "should_close":        d.should_close if d else False,
            "close_reason":        d.reason if (d and d.should_close) else None,
        }

    def _evaluate_inner(
        self,
        position: OpenPosition,
        realized_pnl_today: float,
        short_dte_flat_target: float = 0.75,
        credit_spread_flat_target: float = 0.50,
        portfolio_daily_loss_limit: float = -999_999.0,
    ) -> ProfitDecision:
        """
        Core decision function. Called every lifecycle tick (every 60 seconds).

        Args:
            position:                   Live position with refreshed unrealized_pnl and legs.
            realized_pnl_today:         Closed P&L so far today (for portfolio rescue).
            short_dte_flat_target:      Flat exit target for short-DTE / event pillars (75%).
            credit_spread_flat_target:  Flat exit target for credit spreads (50%). Tastytrade-
                                        validated: 50% outperforms any DTE-declining curve for
                                        theta-positive structures. DTE-curve applies to debits.
            portfolio_daily_loss_limit: Negative dollar amount — if today's realized P&L is
                                        worse than this AND position is ≥30% profitable, rescue
                                        fires. Pass -daily_loss_limit_dollars from config.
                                        Default -999,999 disables the rule.
        """
        today = date.today()
        dte        = (position.expiry_date - today).days
        max_gain   = position.max_gain_dollars
        pid        = position.position_id

        if max_gain <= 0:
            return self._hold(pid, 0.0, 0.0, 0.0, 0.0, 0.0)

        # No-data mark guard (defense-in-depth; primary fix is _refresh_position_price keeping the
        # last good mark). A spread mark of exactly 0.00 is the missing-quote sentinel, not a real
        # price — for a credit spread it fabricates unrealized=+max_gain → profit_pct=100% → a
        # spurious LOCK_IN close on day 1. Returning here BEFORE state.hwm/ratchet mutate also
        # prevents HWM being pinned at 100% (which would arm a spurious ratchet close on the next
        # real mark). A genuinely near-worthless spread marks at 0.01-0.05 (not exactly 0.00) and
        # still triggers normally; holding a truly-0.00 position one extra cycle costs nothing.
        if position.current_price == 0.0:
            return self._hold(pid, 0.0, 0.0, 0.0, 0.0, 0.0)

        profit_pct = position.unrealized_pnl / max_gain

        # ── Ensure state exists (positions opened before engine upgrade) ────────
        if pid not in self._states:
            self.register_position(position)
        state = self._states[pid]

        # ── Update HWM and ratchet stop ─────────────────────────────────────────
        state.hwm = max(state.hwm, profit_pct)
        state.ratchet_stop = self._advance_ratchet(state.ratchet_stop, state.hwm)

        # ── Record P&L history for velocity ────────────────────────────────────
        state.pnl_history.append((datetime.utcnow(), profit_pct))

        # ── Compute derived metrics ─────────────────────────────────────────────
        m = self._compute_metrics(position, state, dte, profit_pct, today)

        # ── Short-DTE fast path ─────────────────────────────────────────────────
        is_short_dte = (
            dte <= _SHORT_DTE_MAX
            or (position.pillar and position.pillar.value in _SHORT_DTE_PILLARS)
        )
        if is_short_dte:
            close = profit_pct >= short_dte_flat_target
            return ProfitDecision(
                should_close=close,
                reason=(
                    f"Short-DTE flat: {profit_pct:.0%} ≥ {short_dte_flat_target:.0%} "
                    f"(DTE={dte}, pillar={position.pillar.value if position.pillar else '?'})"
                    if close else ""
                ),
                rule="SHORT_DTE" if close else "HOLD",
                effective_target=short_dte_flat_target,
                profit_pct=profit_pct, hwm_pct=state.hwm,
                ratchet_stop_pct=state.ratchet_stop,
                velocity_1h=m.velocity_1h, theta_excess=m.theta_excess,
            )

        days_held = m.days_held

        # ── Rule 1: LOCK_IN ─────────────────────────────────────────────────────
        if profit_pct >= _LOCK_IN_PCT:
            return ProfitDecision(
                should_close=True,
                reason=f"Lock-in: {profit_pct:.0%} ≥ {_LOCK_IN_PCT:.0%} — last 15% not worth gamma risk",
                rule="LOCK_IN", effective_target=_LOCK_IN_PCT,
                profit_pct=profit_pct, hwm_pct=state.hwm,
                ratchet_stop_pct=state.ratchet_stop,
                velocity_1h=m.velocity_1h, theta_excess=m.theta_excess,
            )

        # ── Rule 2: RATCHET STOP ────────────────────────────────────────────────
        if profit_pct <= state.ratchet_stop and state.ratchet_stop > -2.0:
            return ProfitDecision(
                should_close=True,
                reason=(
                    f"Ratchet stop: profit {profit_pct:.0%} fell below "
                    f"locked floor {state.ratchet_stop:.0%} (HWM={state.hwm:.0%})"
                ),
                rule="RATCHET_STOP", effective_target=state.ratchet_stop,
                profit_pct=profit_pct, hwm_pct=state.hwm,
                ratchet_stop_pct=state.ratchet_stop,
                velocity_1h=m.velocity_1h, theta_excess=m.theta_excess,
            )

        # ── Rule 3: PORTFOLIO RESCUE ────────────────────────────────────────────
        # Use the account-level daily loss limit (e.g. -$500 for a $25k account at 2%).
        # Old per-position threshold (-1.5× max_gain) was arbitrary: a $200 max-gain
        # position would rescue at -$300 daily loss, completely disconnected from actual
        # account size. portfolio_daily_loss_limit comes from config.daily_loss_limit_dollars.
        rescue_threshold = portfolio_daily_loss_limit
        if realized_pnl_today < rescue_threshold and profit_pct >= 0.30:
            return ProfitDecision(
                should_close=True,
                reason=(
                    f"Portfolio rescue: today P&L ${realized_pnl_today:+.0f}, "
                    f"position profitable at {profit_pct:.0%} — take the win"
                ),
                rule="RESCUE", effective_target=0.30,
                profit_pct=profit_pct, hwm_pct=state.hwm,
                ratchet_stop_pct=state.ratchet_stop,
                velocity_1h=m.velocity_1h, theta_excess=m.theta_excess,
            )

        # ── Determine base exit target — credit vs debit spread ─────────────────
        # Credit spread (iron condor, put spread, call spread for premium):
        #   Flat 50% target. Theta works FOR us; the 21-DTE rule in position_manager
        #   is the time gate. DTE-declining curve doesn't apply — at DTE=24 a credit
        #   spread should still target 50%, not 40% (that would exit too early for theta).
        #   Tastytrade backtests: 50% closes win rate 81% vs 72% hold-to-expiry.
        # Debit spread (bull call spread, bear put spread, directional play):
        #   Declining DTE-curve. You pay theta daily; take profit while the move is on.
        #   Lower target near expiry because underlying can reverse and theta erodes value.
        if state.is_credit_spread:
            base_target = credit_spread_flat_target   # flat 50%, regime-adjusted below
        else:
            base_target = self._dte_target(dte)        # 70% → 40% as DTE shrinks

        # Regime adjustment (applies to both spread types)
        regime_adj = self._regime_adjustment(position)
        base_target = max(0.30, min(0.85, base_target + regime_adj))

        # Directional excess: only meaningful for CREDIT spreads.
        # If a credit spread profits much faster than theta explains, the underlying moved
        # in our favor — a reversal can take that back. Exit sooner.
        # For DEBIT spreads: directional profit IS the point. Never lower the target
        # because the trade is working — that would penalise the exact outcome we want.
        if state.is_credit_spread and m.theta_excess > _DIRECTIONAL_EXCESS_RATIO and profit_pct > 0:
            base_target = max(0.30, base_target - 0.10)
            logger.debug(
                "ProfitEngine %s: credit spread directional excess %.1fx theta → target adj to %.0f%%",
                position.ticker, m.theta_excess, base_target * 100,
            )

        # Price target extension: when PriceTargetAgent has a directional scenario
        # implying ≥20% aligned underlying move, the spread can approach max profit —
        # raise the DTE-curve target so we don't exit prematurely.
        # ≥100%: stock doubling → spread is at max profit; hold until LOCK_IN (85%).
        pt_adj = self._pt_adj(state.aligned_pt_return, base_target)
        if pt_adj > 0:
            old_base = base_target
            base_target = min(_LOCK_IN_PCT, base_target + pt_adj)
            logger.debug(
                "ProfitEngine %s: price target +%.0f%% aligned → DTE-curve %.0f%%→%.0f%%",
                position.ticker, state.aligned_pt_return * 100,
                old_base * 100, base_target * 100,
            )

        # Discounted entry bonus: if we got a better-than-mid fill, we captured more
        # edge at entry — allow holding to a slightly higher target to reflect the
        # better cost basis. Capped at +5% to avoid overriding risk rules.
        fill_adj = min(_FILL_BONUS_MAX_ADJ, max(0.0, state.fill_bonus_pct) / 2)
        if fill_adj > 0.005:
            base_target = min(_LOCK_IN_PCT, base_target + fill_adj)
            logger.debug(
                "ProfitEngine %s: fill bonus +%.1f%% → target raised to %.0f%%",
                position.ticker, fill_adj * 100, base_target * 100,
            )

        effective_target = base_target

        # ── Rule 4: DTE_CURVE (main profit close) ───────────────────────────────
        if profit_pct >= effective_target:
            # Speed gate: if target hit in < 7 days with DTE > 28 remaining,
            # and position has positive P&L velocity, let it run until day 7.
            is_fast = days_held < 7 and dte > 28
            if is_fast and m.velocity_1h >= 0:
                if not state.speed_held:
                    state.speed_held = True
                    logger.info(
                        "ProfitEngine %s: SPEED hold %.0f%% in %dd DTE=%d vel=%.2f%%/h — trailing",
                        position.ticker, profit_pct * 100, days_held, dte, m.velocity_1h,
                    )
                return ProfitDecision(
                    should_close=False,
                    reason=f"Speed hold: {profit_pct:.0%} in {days_held}d DTE={dte} — "
                           f"velocity {m.velocity_1h:+.2f}%/h positive, trailing",
                    rule="SPEED", effective_target=effective_target,
                    profit_pct=profit_pct, hwm_pct=state.hwm,
                    ratchet_stop_pct=state.ratchet_stop,
                    velocity_1h=m.velocity_1h, theta_excess=m.theta_excess,
                )

            # Velocity override: if P&L accelerating strongly upward, extend hold
            # Only if we're in early DTE zone and have a healthy HWM cushion
            if m.velocity_1h > 1.0 and dte > 28 and profit_pct < 0.80:
                logger.debug(
                    "ProfitEngine %s: positive velocity %.2f%%/h — extending hold",
                    position.ticker, m.velocity_1h,
                )
                return ProfitDecision(
                    should_close=False,
                    reason=f"Velocity extend: {profit_pct:.0%} at target {effective_target:.0%} "
                           f"but accelerating {m.velocity_1h:+.2f}%/h — hold",
                    rule="VELOCITY_HOLD", effective_target=effective_target,
                    profit_pct=profit_pct, hwm_pct=state.hwm,
                    ratchet_stop_pct=state.ratchet_stop,
                    velocity_1h=m.velocity_1h, theta_excess=m.theta_excess,
                )

            rule = "HEADWIND" if regime_adj < 0 else "DTE_CURVE"
            return ProfitDecision(
                should_close=True,
                reason=(
                    f"{rule}: {profit_pct:.0%} ≥ {effective_target:.0%} target "
                    f"| DTE={dte} days_held={days_held}"
                    + (f" | headwind {self._macro.macro_stance if self._macro else '?'}" if regime_adj < 0 else "")
                    + (f" | theta_excess={m.theta_excess:.1f}x" if m.theta_excess > 1.3 else "")
                    + (f" | vel={m.velocity_1h:+.2f}%/h" if abs(m.velocity_1h) > 0.1 else "")
                ),
                rule=rule, effective_target=effective_target,
                profit_pct=profit_pct, hwm_pct=state.hwm,
                ratchet_stop_pct=state.ratchet_stop,
                velocity_1h=m.velocity_1h, theta_excess=m.theta_excess,
            )

        # ── Hold ────────────────────────────────────────────────────────────────
        return self._hold(pid, profit_pct, state.hwm, state.ratchet_stop,
                          m.velocity_1h, m.theta_excess, effective_target)

    # ── Internal calculations ───────────────────────────────────────────────────

    def _compute_metrics(
        self,
        position: OpenPosition,
        state: PositionState,
        dte: int,
        profit_pct: float,
        today: date,
    ) -> _Metrics:
        days_held = max((today - position.entry_date).days, 0)

        # Theta-based expected P&L (fraction of max_gain)
        # Credit spreads: theta decay follows √(t/T) curve (quadratic option time value)
        # At entry: captured = 0; at expiry: captured = 100% of max (if OTM)
        dte_entry = max(state.dte_at_entry, 1)
        dte_rem   = max(dte, 0)
        theta_expected_pct = 1.0 - math.sqrt(dte_rem / dte_entry)

        # Compare actual profit to theta-expected (in terms of % of max_gain)
        # theta_excess > 1: profiting faster than theta explains → directional move
        # theta_excess < 1: underperforming theta → underlying drifted against us
        if theta_expected_pct > 0.01 and profit_pct > 0:
            theta_excess = profit_pct / theta_expected_pct
        elif profit_pct <= 0:
            theta_excess = 0.0
        else:
            theta_excess = 1.0   # insufficient data, assume on-track

        # P&L velocity: %/hour from history.
        # Require ≥30 minutes of data before trusting the number. With maxlen=6 and
        # 60-second ticks the window is only 6 minutes — one stale bid-ask refresh can
        # show 12%/h noise that would incorrectly fire VELOCITY_HOLD every tick.
        velocity = 0.0
        history = list(state.pnl_history)
        if len(history) >= 2:
            earliest_ts, earliest_pct = history[0]
            latest_ts,   latest_pct   = history[-1]
            hours = (latest_ts - earliest_ts).total_seconds() / 3600.0
            if hours >= 0.5:   # ≥30 minutes — enough signal to separate trend from noise
                velocity = (latest_pct - earliest_pct) / hours

        # Current net Greeks (live, from refreshed leg data)
        net_delta = 0.0
        net_vega  = 0.0
        for leg in position.legs:
            sign = -1.0 if leg.action == "buy" else +1.0
            c100 = position.contracts * leg.contracts * 100
            net_delta += sign * (leg.delta or 0.0) * c100
            net_vega  += sign * (leg.vega  or 0.0) * c100

        return _Metrics(
            profit_pct=profit_pct,
            days_held=days_held,
            dte=dte,
            theta_pnl_est=theta_expected_pct,
            theta_excess=round(theta_excess, 2),
            velocity_1h=round(velocity * 100, 3),  # as percentage points per hour
            current_delta=round(net_delta, 3),
            current_vega=round(net_vega, 3),
        )

    @staticmethod
    def _dte_target(dte: int) -> float:
        """Smooth DTE-curve exit target. Returns fraction of max_gain needed to close."""
        for threshold, target in _DTE_CURVE:
            if dte > threshold:
                return target
        return _DTE_CURVE[-1][1]

    @staticmethod
    def _pt_adj(aligned_return_pct: float, base_target: float) -> float:
        """
        Price target adjustment to the DTE-curve base target.

        For each tier in _PT_TIERS:
          - None adj → target jumps to LOCK_IN ceiling (moonshot: fill full spread width)
          - float adj → additive raise to base_target

        Returns the adjustment amount (always ≥ 0).
        """
        for threshold, adj in _PT_TIERS:
            if aligned_return_pct >= threshold:
                if adj is None:
                    return max(0.0, _LOCK_IN_PCT - base_target)
                return adj
        return 0.0

    @staticmethod
    def _advance_ratchet(current_stop: float, hwm: float) -> float:
        """
        Advance the ratchet stop to the highest applicable level based on HWM.
        Stop only moves UP — never resets downward.
        """
        best_stop = current_stop
        for profit_threshold, new_stop in _RATCHET_SCHEDULE:
            if hwm >= profit_threshold:
                best_stop = max(best_stop, new_stop)
                break   # schedule is ordered descending — first match is correct
        return best_stop

    def _regime_adjustment(self, position: OpenPosition) -> float:
        """Return signed target adjustment: positive = hold longer, negative = exit sooner."""
        if self._macro is None:
            return 0.0
        stance    = self._macro.macro_stance
        direction = getattr(position, "direction", "neutral")
        if direction == "bullish" and stance == "risk_on":
            return _TAILWIND_ADJ
        if direction == "bearish" and stance == "risk_off":
            return _TAILWIND_ADJ
        if direction == "bullish" and stance == "risk_off":
            return _HEADWIND_ADJ
        if direction == "bearish" and stance == "risk_on":
            return _HEADWIND_ADJ
        return 0.0

    @staticmethod
    def _hold(
        pid: str,
        profit_pct: float,
        hwm: float,
        ratchet_stop: float,
        velocity: float,
        theta_excess: float,
        effective_target: float = 0.50,
    ) -> ProfitDecision:
        return ProfitDecision(
            should_close=False,
            reason="",
            rule="HOLD",
            effective_target=effective_target,
            profit_pct=profit_pct,
            hwm_pct=hwm,
            ratchet_stop_pct=ratchet_stop,
            velocity_1h=velocity,
            theta_excess=theta_excess,
        )

"""
Premium Time Surface — Option Value Projection Engine
=========================================================
Projects option premium across a grid of (underlying_price × time_horizon)
to answer the question: "If SPX reaches target_price in N minutes, what is
the option worth?"

The Problem (user's exact insight):
  If SPX is at 5670 and we enter a call, and it hits 5680 in 5 minutes,
  the option gain is $X. But if it hits 5680 in 15 minutes, the gain is $Y
  (much less due to theta decay). We need to compute BOTH and find the
  optimal exit window.

The Solution:
  Build a 2D premium surface: rows = time horizons (5, 10, 15, 20, 30 min),
  cols = price levels (stop, entry, target, beyond). At each cell, forward-price
  the option using Black-Scholes with correct time remaining.

  This lets us:
  1. Find the "break-even time" — how long before theta kills the trade
  2. Compute the exact dollar P&L at each (price, time) combination
  3. Set smart time stops — "if not at +$3 by 12 min, the theta drag wins"
  4. Rank scenarios by net premium gain after slippage

Integration:
  - contract_picker uses this to find optimal hold window
  - backtester uses this to set dynamic time stops per trade
  - live engine uses this for real-time "cut or hold" decisions
"""

import math
import logging
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple

from ..black_scholes import (
    bs_call_price,
    bs_put_price,
    bs_delta,
    bs_gamma,
    bs_theta,
    bs_vega,
)

logger = logging.getLogger(__name__)


@dataclass
class PremiumPoint:
    """Single point on the premium surface."""
    time_minutes: float           # Minutes from now
    underlying_price: float       # UL price at this point
    premium: float                # Option premium (after exit slippage)
    premium_raw: float            # Before exit slippage
    pnl: float                    # Gain vs entry premium (after slippage)
    pnl_pct: float                # PnL as % of entry premium
    delta: float                  # Delta at this point
    theta: float                  # Theta at this point ($/day)
    time_remaining_years: float   # T remaining at this point


@dataclass
class TimeSurfaceResult:
    """Full premium time surface for a trade."""
    entry_premium: float              # Entry premium (after entry slippage)
    strike: float
    right: str                        # "C" or "P"
    iv: float
    current_price: float
    target_price: float
    stop_price: float

    # The surface: list of PremiumPoints
    surface: List[PremiumPoint] = field(default_factory=list)

    # Derived insights
    break_even_minutes: float = 0.0   # How long before theta kills the trade at entry price
    optimal_exit_minutes: float = 0.0  # Time with maximum PnL at target
    pnl_at_target_5m: float = 0.0     # PnL if target hit in 5 min
    pnl_at_target_10m: float = 0.0    # PnL if target hit in 10 min
    pnl_at_target_15m: float = 0.0    # PnL if target hit in 15 min
    pnl_at_target_30m: float = 0.0    # PnL if target hit in 30 min
    theta_kill_minutes: float = 999.0  # Minutes until theta eats 50% of potential gain

    # Optimal scenario
    best_pnl: float = 0.0             # Best achievable PnL across the surface
    best_pnl_price: float = 0.0       # UL price for best PnL
    best_pnl_time: float = 0.0        # Time for best PnL


class PremiumTimeSurface:
    """
    Builds a 2D premium projection surface for an option trade.

    Axis 1: Time horizons — [2, 5, 8, 10, 12, 15, 20, 25, 30, 45] minutes
    Axis 2: Price levels — [stop, stop+1/3, entry, target-1/3, target, target+50%]

    At each (time, price) intersection, forward-prices the option using BS.
    """

    # Standard time horizons (minutes from entry)
    TIME_HORIZONS = [2, 5, 8, 10, 12, 15, 20, 25, 30, 45]

    def __init__(
        self,
        risk_free_rate: float = 0.045,
        entry_slippage_pct: float = 0.0125,
        exit_slippage_pct: float = 0.0125,
    ):
        self.r = risk_free_rate
        self.entry_slippage_pct = entry_slippage_pct
        self.exit_slippage_pct = exit_slippage_pct

    def build_surface(
        self,
        underlying_price: float,
        strike: float,
        right: str,          # "C" or "P"
        iv: float,
        time_to_expiry: float,   # Current T in years
        target_price: float,
        stop_price: float,
        entry_premium: Optional[float] = None,
        time_horizons: Optional[List[float]] = None,
        price_levels: Optional[List[float]] = None,
    ) -> TimeSurfaceResult:
        """
        Build the full premium time surface.

        Args:
            underlying_price: Current UL price
            strike: Option strike
            right: "C" or "P"
            iv: Implied volatility (annualized)
            time_to_expiry: Current time to expiry in years
            target_price: Target underlying price
            stop_price: Stop price
            entry_premium: Entry premium (if known, e.g., from live chain)
            time_horizons: Custom time horizons in minutes (default: standard set)
            price_levels: Custom price levels (default: auto-generated from stop/target)

        Returns:
            TimeSurfaceResult with full surface and derived insights
        """
        # ── 1. Compute entry premium ────────────────────────────
        if entry_premium is None:
            raw_prem = self._price(underlying_price, strike, time_to_expiry, iv, right)
            entry_premium = raw_prem * (1 + self.entry_slippage_pct)

        if entry_premium <= 0:
            return TimeSurfaceResult(
                entry_premium=0, strike=strike, right=right, iv=iv,
                current_price=underlying_price, target_price=target_price,
                stop_price=stop_price,
            )

        # ── 2. Generate price levels ─────────────────────────────
        if price_levels is None:
            price_levels = self._generate_price_levels(
                underlying_price, target_price, stop_price
            )

        # ── 3. Generate time horizons ────────────────────────────
        horizons = time_horizons or self.TIME_HORIZONS
        # Clip to time available
        max_minutes = time_to_expiry * 252 * 390  # Convert years → trading minutes
        horizons = [t for t in horizons if t < max_minutes]
        if not horizons:
            horizons = [min(5, max_minutes * 0.5)]

        # ── 4. Build the surface ─────────────────────────────────
        surface = []
        target_pnls_by_time = {}

        for t_min in horizons:
            t_years = t_min / (252 * 390)
            T_remain = max(0.0001, time_to_expiry - t_years)

            for price_level in price_levels:
                raw_prem = self._price(price_level, strike, T_remain, iv, right)
                exit_prem = raw_prem * (1 - self.exit_slippage_pct)
                pnl = exit_prem - entry_premium
                pnl_pct = pnl / entry_premium if entry_premium > 0 else 0

                option_type = "call" if right == "C" else "put"
                delta_val = bs_delta(price_level, strike, T_remain, self.r, iv, option_type)
                theta_val = bs_theta(price_level, strike, T_remain, self.r, iv, option_type)

                point = PremiumPoint(
                    time_minutes=t_min,
                    underlying_price=price_level,
                    premium=exit_prem,
                    premium_raw=raw_prem,
                    pnl=pnl,
                    pnl_pct=pnl_pct,
                    delta=delta_val,
                    theta=theta_val,
                    time_remaining_years=T_remain,
                )
                surface.append(point)

                # Track target PnL at each time horizon
                if abs(price_level - target_price) < 0.01:
                    target_pnls_by_time[t_min] = pnl

        # ── 5. Derive insights ───────────────────────────────────
        result = TimeSurfaceResult(
            entry_premium=entry_premium,
            strike=strike,
            right=right,
            iv=iv,
            current_price=underlying_price,
            target_price=target_price,
            stop_price=stop_price,
            surface=surface,
        )

        # PnL at target for standard horizons
        result.pnl_at_target_5m = target_pnls_by_time.get(5, 0.0)
        result.pnl_at_target_10m = target_pnls_by_time.get(10, 0.0)
        result.pnl_at_target_15m = target_pnls_by_time.get(15, 0.0)
        result.pnl_at_target_30m = target_pnls_by_time.get(30, 0.0)

        # Best PnL across entire surface
        if surface:
            best_point = max(surface, key=lambda p: p.pnl)
            result.best_pnl = best_point.pnl
            result.best_pnl_price = best_point.underlying_price
            result.best_pnl_time = best_point.time_minutes

        # Optimal exit time at target price
        if target_pnls_by_time:
            best_time = max(target_pnls_by_time, key=target_pnls_by_time.get)
            result.optimal_exit_minutes = best_time

        # Break-even time: how long before option at ENTRY price turns negative
        entry_price_pnls = [
            (p.time_minutes, p.pnl) for p in surface
            if abs(p.underlying_price - underlying_price) < 0.01
        ]
        for t_min, pnl in sorted(entry_price_pnls):
            if pnl < 0:
                result.break_even_minutes = t_min
                break

        # Theta kill time: when does theta eat 50% of best target PnL?
        if result.pnl_at_target_5m > 0 and target_pnls_by_time:
            threshold = result.pnl_at_target_5m * 0.5
            for t_min in sorted(target_pnls_by_time.keys()):
                if target_pnls_by_time[t_min] < threshold:
                    result.theta_kill_minutes = t_min
                    break

        return result

    def compute_optimal_time_stop(
        self,
        surface_result: TimeSurfaceResult,
        min_acceptable_pnl_pct: float = 0.10,
    ) -> float:
        """
        From the premium surface, compute the optimal time stop.

        Logic: find the latest time horizon where PnL at the entry price
        is still above the minimum acceptable threshold. Beyond that,
        theta drag makes the trade a loser even if price doesn't move.

        Returns: recommended time stop in minutes.
        """
        entry_price = surface_result.current_price
        entry_premium = surface_result.entry_premium

        # Get PnL at entry price for each time horizon
        entry_price_points = sorted(
            [p for p in surface_result.surface
             if abs(p.underlying_price - entry_price) < 0.01],
            key=lambda p: p.time_minutes
        )

        # Find last time where PnL at entry price > -threshold
        max_loss = -entry_premium * min_acceptable_pnl_pct
        optimal_stop = 30.0  # Default

        for point in entry_price_points:
            if point.pnl < max_loss:
                # Time before this one was the last "safe" time
                optimal_stop = max(5, point.time_minutes - 2)
                break
            optimal_stop = point.time_minutes

        return optimal_stop

    def _generate_price_levels(
        self,
        current: float,
        target: float,
        stop: float,
    ) -> List[float]:
        """Generate meaningful price levels between stop and beyond target."""
        levels = set()

        # Always include key prices
        levels.add(round(current, 2))
        levels.add(round(target, 2))
        levels.add(round(stop, 2))

        # Add intermediate levels
        move_to_target = abs(target - current)
        move_to_stop = abs(stop - current)

        if target > current:
            # Bullish: current → target → beyond
            levels.add(round(current + move_to_target * 0.33, 2))
            levels.add(round(current + move_to_target * 0.67, 2))
            levels.add(round(current + move_to_target * 1.5, 2))
            # Between current and stop
            levels.add(round(current - move_to_stop * 0.5, 2))
        else:
            # Bearish: current → target → beyond
            levels.add(round(current - move_to_target * 0.33, 2))
            levels.add(round(current - move_to_target * 0.67, 2))
            levels.add(round(current - move_to_target * 1.5, 2))
            # Between current and stop
            levels.add(round(current + move_to_stop * 0.5, 2))

        return sorted(levels)

    def _price(self, S: float, K: float, T: float, iv: float, right: str) -> float:
        """Price an option using Black-Scholes."""
        if T <= 0:
            return max(0, S - K) if right == "C" else max(0, K - S)
        if right == "C":
            return bs_call_price(S, K, T, self.r, iv)
        return bs_put_price(S, K, T, self.r, iv)

    def format_surface(self, result: TimeSurfaceResult) -> str:
        """Format the surface as a readable table for logging."""
        if not result.surface:
            return "Empty surface"

        lines = []
        lines.append(f"Premium Time Surface: {result.strike}{result.right} "
                      f"@ ${result.entry_premium:.2f}")
        lines.append(f"UL: ${result.current_price:.2f} → "
                      f"Target: ${result.target_price:.2f}, "
                      f"Stop: ${result.stop_price:.2f}")
        lines.append("")

        # Group by time
        by_time = {}
        for p in result.surface:
            by_time.setdefault(p.time_minutes, []).append(p)

        # Header
        prices = sorted(set(p.underlying_price for p in result.surface))
        hdr = f"{'Time':>6} |"
        for px in prices:
            hdr += f" ${px:>9.2f} |"
        lines.append(hdr)
        lines.append("-" * len(hdr))

        # Rows
        for t_min in sorted(by_time.keys()):
            points = {p.underlying_price: p for p in by_time[t_min]}
            row = f"{t_min:>4.0f}m |"
            for px in prices:
                if px in points:
                    p = points[px]
                    sign = "+" if p.pnl >= 0 else ""
                    row += f"  {sign}${p.pnl:>6.2f} |"
                else:
                    row += f"  {'---':>7} |"
            lines.append(row)

        lines.append("")
        lines.append(f"Best scenario: +${result.best_pnl:.2f} at "
                      f"${result.best_pnl_price:.2f} in {result.best_pnl_time:.0f}min")
        lines.append(f"Target @ 5m: +${result.pnl_at_target_5m:.2f}, "
                      f"@ 15m: +${result.pnl_at_target_15m:.2f}, "
                      f"@ 30m: +${result.pnl_at_target_30m:.2f}")
        if result.theta_kill_minutes < 999:
            lines.append(f"⚠ Theta kills 50% of gain after {result.theta_kill_minutes:.0f}min")

        return "\n".join(lines)

"""
Smart Contract Picker — Pre-Trade Option Feasibility Engine
=============================================================
Validates that a selected option contract can realistically achieve
the profit target before entering a trade.

The Problem:
  Stop/target are set in UNDERLYING price space (e.g., SPY $570 → $573.50).
  But we trade OPTIONS. Nobody checks whether the option premium will move
  enough to make the trade worthwhile after theta decay and slippage.

The Solution:
  Before every entry, forward-price the option at the target underlying
  price using Black-Scholes. If the expected option P&L doesn't clear
  a minimum R:R threshold, reject the trade.

Checks performed:
  1. Forward-price option at target → compute expected premium gain
  2. Account for theta decay over expected hold time
  3. Account for entry slippage (pay ask) + exit slippage (hit bid)
  4. Compute net expected R:R after all costs
  5. Reject if R:R < minimum threshold
  6. Among multiple candidates, prefer highest gamma/premium ratio
     (profits accelerate faster on favorable moves)

Integration:
  - Live engine: called from _execute_entry() after _select_strike()
  - Backtester: called inline before each strategy opens a position
"""

import logging
import math
from dataclasses import dataclass, field
from typing import Optional, Dict, Tuple, List

from ..black_scholes import (
    bs_call_price,
    bs_put_price,
    bs_delta,
    bs_gamma,
    bs_theta,
)

logger = logging.getLogger(__name__)


@dataclass
class FeasibilityResult:
    """Result of option feasibility check."""
    feasible: bool                    # Can this contract achieve the target?
    entry_premium: float              # Entry premium (incl. slippage)
    premium_at_target: float          # Forward-priced premium at target UL price
    premium_at_stop: float            # Forward-priced premium at stop UL price
    expected_gain: float              # premium_at_target - entry_premium
    expected_loss: float              # entry_premium - premium_at_stop
    expected_rr: float                # expected_gain / expected_loss (R:R ratio)
    theta_cost: float                 # Theta decay over expected hold time
    net_gain_after_costs: float       # Gain after theta + slippage
    gamma_premium_ratio: float        # Gamma / entry_premium (higher = faster acceleration)
    delta_at_entry: float             # Delta at entry
    reject_reason: str = ""           # Why rejected (if not feasible)

    # ── Premium time surface insights (populated when surface is built) ──
    pnl_at_5m: float = 0.0           # PnL if target hit in 5 min
    pnl_at_10m: float = 0.0          # PnL if target hit in 10 min
    pnl_at_15m: float = 0.0          # PnL if target hit in 15 min
    pnl_at_30m: float = 0.0          # PnL if target hit in 30 min
    optimal_exit_minutes: float = 0.0  # Time with best PnL at target
    theta_kill_minutes: float = 999.0  # When theta eats 50% of gain
    recommended_time_stop: float = 0.0  # Suggested time stop (minutes)

    # ── S/R-adjusted target (if level detector provided one) ─────
    sr_adjusted_target: float = 0.0   # Target adjusted to nearest S/R
    sr_source: str = ""               # Where the S/R came from


class SmartContractPicker:
    """
    Pre-trade option feasibility engine.

    Given a strike/right/expiry and underlying stop/target levels,
    forward-prices the option to determine if the trade is viable.
    """

    def __init__(
        self,
        risk_free_rate: float = 0.045,
        min_expected_rr: float = 1.0,
        min_net_gain_pct: float = 0.15,
        max_theta_pct: float = 0.30,
        entry_slippage_pct: float = 0.0125,
        exit_slippage_pct: float = 0.0125,
    ):
        """
        Args:
            risk_free_rate: Annual risk-free rate for BS pricing
            min_expected_rr: Minimum expected R:R ratio (gain/loss)
            min_net_gain_pct: Minimum net gain as % of entry premium
            max_theta_pct: Max theta cost as % of entry premium (reject if higher)
            entry_slippage_pct: Entry slippage as fraction of premium (pay the ask)
            exit_slippage_pct: Exit slippage as fraction of premium (hit the bid)
        """
        self.r = risk_free_rate
        self.min_expected_rr = min_expected_rr
        self.min_net_gain_pct = min_net_gain_pct
        self.max_theta_pct = max_theta_pct
        self.entry_slippage_pct = entry_slippage_pct
        self.exit_slippage_pct = exit_slippage_pct

    def check_feasibility(
        self,
        underlying_price: float,
        strike: float,
        right: str,            # "C" or "P"
        iv: float,
        time_to_expiry: float,  # In years (BS convention)
        target_price: float,    # Target underlying price
        stop_price: float,      # Stop underlying price
        expected_hold_minutes: float = 30.0,
        entry_premium: Optional[float] = None,  # If already known (live chain)
    ) -> FeasibilityResult:
        """
        Check if an option trade can realistically hit its profit target.

        Forward-prices the option at:
          1. Target underlying price (with theta decay for expected hold)
          2. Stop underlying price (with partial theta decay)

        Returns FeasibilityResult with detailed analysis.
        """
        option_type = "call" if right == "C" else "put"

        # ── 1. Price option at entry ─────────────────────────────
        if entry_premium is None:
            raw_premium = self._price(underlying_price, strike, time_to_expiry, iv, right)
            entry_premium = raw_premium * (1 + self.entry_slippage_pct)
        else:
            raw_premium = entry_premium  # Already includes slippage from live chain ask

        if entry_premium <= 0:
            return FeasibilityResult(
                feasible=False, entry_premium=0, premium_at_target=0,
                premium_at_stop=0, expected_gain=0, expected_loss=0,
                expected_rr=0, theta_cost=0, net_gain_after_costs=0,
                gamma_premium_ratio=0, delta_at_entry=0,
                reject_reason="Zero entry premium",
            )

        # ── 2. Compute greeks at entry ───────────────────────────
        delta = abs(bs_delta(underlying_price, strike, time_to_expiry, self.r, iv, option_type))
        gamma = bs_gamma(underlying_price, strike, time_to_expiry, self.r, iv)
        theta_per_day = abs(bs_theta(underlying_price, strike, time_to_expiry, self.r, iv, option_type))

        # ── 3. Forward-price at target ───────────────────────────
        # Time decays during the hold
        hold_years = expected_hold_minutes / (252 * 390)  # minutes → years
        T_at_exit = max(0.0001, time_to_expiry - hold_years)  # Remaining time at exit

        raw_premium_at_target = self._price(target_price, strike, T_at_exit, iv, right)
        # Apply exit slippage (we hit the bid on exit)
        premium_at_target = raw_premium_at_target * (1 - self.exit_slippage_pct)

        # ── 4. Forward-price at stop ─────────────────────────────
        # Stop typically fires faster (half the expected hold time)
        stop_hold_minutes = expected_hold_minutes * 0.5
        stop_hold_years = stop_hold_minutes / (252 * 390)
        T_at_stop = max(0.0001, time_to_expiry - stop_hold_years)

        raw_premium_at_stop = self._price(stop_price, strike, T_at_stop, iv, right)
        premium_at_stop = raw_premium_at_stop * (1 - self.exit_slippage_pct)

        # ── 5. Compute expected gain/loss ────────────────────────
        expected_gain = max(0, premium_at_target - entry_premium)
        expected_loss = max(0.01, entry_premium - premium_at_stop)  # Floor to avoid div/0

        # R:R ratio
        expected_rr = expected_gain / expected_loss if expected_loss > 0 else 0

        # ── 6. Theta cost over hold period ───────────────────────
        # theta_per_day is negative (decay), we use absolute value
        theta_cost = theta_per_day * (expected_hold_minutes / (6.5 * 60))  # fraction of trading day

        # Net gain after all costs
        net_gain = expected_gain  # Slippage already baked into forward prices
        net_gain_pct = net_gain / entry_premium if entry_premium > 0 else 0
        theta_pct = theta_cost / entry_premium if entry_premium > 0 else 0

        # ── 7. Gamma/premium efficiency ratio ────────────────────
        gamma_prem_ratio = gamma / entry_premium if entry_premium > 0 else 0

        # ── 8. Feasibility checks ────────────────────────────────
        reject_reason = ""
        feasible = True

        if expected_rr < self.min_expected_rr:
            reject_reason = (
                f"R:R too low: {expected_rr:.2f} < {self.min_expected_rr:.2f} "
                f"(gain=${expected_gain:.3f} vs loss=${expected_loss:.3f})"
            )
            feasible = False

        elif net_gain_pct < self.min_net_gain_pct:
            reject_reason = (
                f"Net gain too small: {net_gain_pct:.1%} < {self.min_net_gain_pct:.1%} "
                f"(gain=${net_gain:.3f} on ${entry_premium:.3f} premium)"
            )
            feasible = False

        elif theta_pct > self.max_theta_pct:
            reject_reason = (
                f"Theta cost too high: {theta_pct:.1%} > {self.max_theta_pct:.1%} "
                f"(theta=${theta_cost:.3f} on ${entry_premium:.3f} premium over "
                f"{expected_hold_minutes:.0f}min)"
            )
            feasible = False

        return FeasibilityResult(
            feasible=feasible,
            entry_premium=entry_premium,
            premium_at_target=premium_at_target,
            premium_at_stop=premium_at_stop,
            expected_gain=expected_gain,
            expected_loss=expected_loss,
            expected_rr=expected_rr,
            theta_cost=theta_cost,
            net_gain_after_costs=net_gain,
            gamma_premium_ratio=gamma_prem_ratio,
            delta_at_entry=delta,
            reject_reason=reject_reason,
        )

    def pick_best_contract(
        self,
        candidates: list,
        underlying_price: float,
        iv: float,
        time_to_expiry: float,
        target_price: float,
        stop_price: float,
        expected_hold_minutes: float = 30.0,
    ) -> Tuple[Optional[Dict], Optional[FeasibilityResult]]:
        """
        From a list of candidate options (from live chain), pick the best one.

        Candidates: list of dicts with keys: strike, right, ask, bid, mid, delta, gamma, iv

        Selection logic:
          1. Filter to feasible contracts only
          2. Among feasible, rank by:
             a. Expected R:R (higher is better)
             b. Gamma/premium ratio (higher = faster profit acceleration)
             c. Weighted score = 0.6 × normalized_rr + 0.4 × normalized_gamma_ratio

        Returns: (best_candidate_dict, FeasibilityResult) or (None, None)
        """
        scored = []

        for cand in candidates:
            # Use the candidate's own IV if available, else fall back
            cand_iv = cand.get("iv", iv) or iv

            result = self.check_feasibility(
                underlying_price=underlying_price,
                strike=cand["strike"],
                right=cand["right"],
                iv=cand_iv,
                time_to_expiry=time_to_expiry,
                target_price=target_price,
                stop_price=stop_price,
                expected_hold_minutes=expected_hold_minutes,
                entry_premium=cand.get("ask"),  # Use live ask for accuracy
            )

            if result.feasible:
                scored.append((cand, result))
            else:
                logger.debug(
                    f"  Contract {cand['strike']}{cand['right']} rejected: "
                    f"{result.reject_reason}"
                )

        if not scored:
            return None, None

        if len(scored) == 1:
            return scored[0]

        # ── Rank by composite score ──────────────────────────────
        # Normalize R:R and gamma/premium across candidates
        rrs = [r.expected_rr for _, r in scored]
        gprs = [r.gamma_premium_ratio for _, r in scored]

        max_rr = max(rrs) if max(rrs) > 0 else 1
        max_gpr = max(gprs) if max(gprs) > 0 else 1

        best_score = -1
        best_pair = scored[0]

        for cand, result in scored:
            norm_rr = result.expected_rr / max_rr
            norm_gpr = result.gamma_premium_ratio / max_gpr
            score = 0.6 * norm_rr + 0.4 * norm_gpr
            if score > best_score:
                best_score = score
                best_pair = (cand, result)

        return best_pair

    def _price(self, S: float, K: float, T: float, iv: float, right: str) -> float:
        """Price an option using Black-Scholes."""
        if T <= 0:
            if right == "C":
                return max(0, S - K)
            return max(0, K - S)
        if right == "C":
            return bs_call_price(S, K, T, self.r, iv)
        return bs_put_price(S, K, T, self.r, iv)

    @staticmethod
    def expected_hold_for_tier(tier: str, minutes_since_open: int = 0) -> float:
        """
        Estimate expected hold time in minutes for each strategy tier.

        This is used for theta cost estimation. Based on backtested
        median hold times across all strategies.
        """
        if tier == "scalp":
            return 25.0          # Median scalp hold: ~25 min
        elif tier == "runner":
            # Runners only trade in power hour (330+ min since open)
            # Limited time remaining — they resolve faster than 60 min
            if minutes_since_open >= 330:
                return 25.0      # Power hour runners: ~25 min realistic hold
            return 45.0          # Early runners (hypothetical): ~45 min
        elif tier == "orb":
            return 40.0          # ORB breakouts develop: ~40 min
        elif tier == "range_fade":
            return 30.0          # Fades are quick: ~30 min
        elif tier == "vwap_mr":
            return 15.0          # VWAP snaps are fast: ~15 min
        elif tier == "mr":
            return 15.0          # Mean reversion: ~15 min
        else:
            return 30.0          # Default

    # ─────────────────────────────────────────────────────────────
    # Enhanced: Check with Premium Time Surface
    # ─────────────────────────────────────────────────────────────

    def check_feasibility_with_surface(
        self,
        underlying_price: float,
        strike: float,
        right: str,
        iv: float,
        time_to_expiry: float,
        target_price: float,
        stop_price: float,
        expected_hold_minutes: float = 30.0,
        entry_premium: Optional[float] = None,
    ) -> FeasibilityResult:
        """
        Enhanced feasibility check that also builds a premium time surface.

        Same as check_feasibility() but additionally:
        1. Projects option premium at target across multiple time horizons
        2. Finds optimal exit time (when PnL at target is maximized)
        3. Computes theta kill time (when theta eats 50% of the gain)
        4. Recommends a time stop based on the surface

        Use this for live trading where the extra computation is worth it.
        For backtesting, the basic check_feasibility() is faster.
        """
        from .premium_surface import PremiumTimeSurface

        # First, run the standard feasibility check
        result = self.check_feasibility(
            underlying_price=underlying_price,
            strike=strike,
            right=right,
            iv=iv,
            time_to_expiry=time_to_expiry,
            target_price=target_price,
            stop_price=stop_price,
            expected_hold_minutes=expected_hold_minutes,
            entry_premium=entry_premium,
        )

        # If not feasible, no need to build surface
        if not result.feasible:
            return result

        # Build the premium time surface
        try:
            surface_engine = PremiumTimeSurface(
                risk_free_rate=self.r,
                entry_slippage_pct=self.entry_slippage_pct,
                exit_slippage_pct=self.exit_slippage_pct,
            )
            surface = surface_engine.build_surface(
                underlying_price=underlying_price,
                strike=strike,
                right=right,
                iv=iv,
                time_to_expiry=time_to_expiry,
                target_price=target_price,
                stop_price=stop_price,
                entry_premium=result.entry_premium,
            )

            # Enrich the result with surface insights
            result.pnl_at_5m = surface.pnl_at_target_5m
            result.pnl_at_10m = surface.pnl_at_target_10m
            result.pnl_at_15m = surface.pnl_at_target_15m
            result.pnl_at_30m = surface.pnl_at_target_30m
            result.optimal_exit_minutes = surface.optimal_exit_minutes
            result.theta_kill_minutes = surface.theta_kill_minutes

            # Compute recommended time stop from surface
            result.recommended_time_stop = surface_engine.compute_optimal_time_stop(
                surface, min_acceptable_pnl_pct=0.10,
            )

            logger.debug(
                f"  Surface: target@5m=${surface.pnl_at_target_5m:+.2f}, "
                f"@15m=${surface.pnl_at_target_15m:+.2f}, "
                f"@30m=${surface.pnl_at_target_30m:+.2f} | "
                f"optimal_exit={surface.optimal_exit_minutes:.0f}m, "
                f"theta_kill={surface.theta_kill_minutes:.0f}m, "
                f"time_stop={result.recommended_time_stop:.0f}m"
            )

        except Exception as e:
            logger.warning(f"Surface build failed (non-fatal): {e}")

        return result

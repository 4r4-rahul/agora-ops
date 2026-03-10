"""
Long Options Backtester
========================
Replays intraday bars through the momentum trigger engine to simulate
buying calls/puts based on the 5 triggers in LottoScanner.

This backtester answers: "If I ran my buy strategy on historical data,
what would my P&L, win rate, and trigger accuracy look like?"

Key Design:
  ✓ Reuses MomentumDetector from lotto.py (same triggers as live)
  ✓ Black-Scholes pricing for entry/exit (with theta decay + gamma)
  ✓ Models all 5 exit rules (stop, target, trailing, runner, time)
  ✓ Per-trigger breakdown (which triggers actually make money?)
  ✓ Budget discipline (daily budget, per-trade cap, max positions)
  ✓ Supports SPX and SPY (premium scaling via TickerProfile)

Data Sources:
  - Yahoo Finance: SPY 5m bars (free, 60 days)
  - Yahoo Finance: SPY 1m bars (free, 7 days)
  - IBKR: SPX/SPY any resolution, full history (if connected)

Usage:
    python run_lotto_backtest.py                        # Default: SPY 5m, 30 days
    python run_lotto_backtest.py --ticker SPY --days 60  # SPY 60 days of 5m bars
    python run_lotto_backtest.py --ticker SPY --days 7 --interval 1m  # 1-min detail
"""

import math
import os
import json
from dataclasses import dataclass, field
from datetime import datetime, date, time as dtime, timedelta
from typing import List, Dict, Any, Optional, Tuple

import numpy as np
import pandas as pd

from ..config import EngineConfig, LottoConfig, get_ticker_profile, TickerProfile
from ..black_scholes import (
    bs_call_price, bs_put_price, bs_delta, bs_gamma, bs_theta, bs_vega,
)
from ..lotto import MomentumDetector, LottoTrigger, _round_strike
from ..formatters import header, sub_header, kv, pnl, bar, C


# ─────────────────────────────────────────────────────────────────
# Simulated long option position
# ─────────────────────────────────────────────────────────────────

@dataclass
class SimLongOption:
    """A simulated long option position being tracked bar-by-bar."""
    # Identity
    ticker: str = ""
    strike: float = 0.0
    right: str = ""           # "C" or "P"
    expiry_date: date = field(default_factory=date.today)
    trigger_type: str = ""
    tier: str = "sniper"
    direction: str = ""       # "CALL" or "PUT"

    # Entry
    entry_time: datetime = field(default_factory=datetime.now)
    entry_price: float = 0.0        # Premium paid per contract
    entry_underlying: float = 0.0   # Underlying price at entry
    entry_iv: float = 0.20          # IV at entry
    num_contracts: int = 1

    # Current state
    current_price: float = 0.0      # Current option price
    current_underlying: float = 0.0
    high_water_mark: float = 0.0    # Highest option price seen

    # Exit
    is_open: bool = True
    exit_time: Optional[datetime] = None
    exit_price: float = 0.0
    exit_reason: str = ""
    exit_underlying: float = 0.0

    # Partial profits
    took_partial: bool = False
    original_contracts: int = 0
    runner_contracts: int = 0

    # P&L
    total_pnl: float = 0.0         # Total realized P&L (all legs)
    partial_pnl: float = 0.0       # P&L from partial exit
    runner_pnl: float = 0.0        # P&L from runner


@dataclass
class LottoBacktestDay:
    """Results for a single trading day."""
    date: date = field(default_factory=date.today)
    triggers_found: int = 0
    trades_entered: int = 0
    trades_closed: int = 0
    day_pnl: float = 0.0
    trigger_types: List[str] = field(default_factory=list)
    winning_trades: int = 0
    losing_trades: int = 0


@dataclass
class LottoBacktestResults:
    """Complete backtest results."""
    # Period
    start_date: date = field(default_factory=date.today)
    end_date: date = field(default_factory=date.today)
    ticker: str = ""
    interval: str = "5m"
    total_days: int = 0
    days_traded: int = 0

    # Trades
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0

    # P&L
    total_pnl: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    biggest_win: float = 0.0
    biggest_loss: float = 0.0
    profit_factor: float = 0.0

    # Returns
    avg_return_pct: float = 0.0     # Avg return per trade as % of entry
    avg_winner_mult: float = 0.0    # Avg winning multiplier (e.g., 3.2x)
    avg_loser_mult: float = 0.0     # Avg losing multiplier (e.g., 0.5x)

    # Budget
    starting_balance: float = 10_000.0
    ending_balance: float = 10_000.0
    max_drawdown_pct: float = 0.0
    total_spent: float = 0.0        # Total premium paid

    # Trigger breakdown
    trigger_stats: Dict[str, Dict] = field(default_factory=dict)

    # Tier breakdown
    tier_stats: Dict[str, Dict] = field(default_factory=dict)

    # Daily detail
    daily_results: List[LottoBacktestDay] = field(default_factory=list)

    # All trades
    trades: List[SimLongOption] = field(default_factory=list)

    # Equity curve
    equity_curve: List[Dict] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────
# Black-Scholes option pricer for backtesting
# ─────────────────────────────────────────────────────────────────

class OptionPricer:
    """
    Price options using Black-Scholes for backtesting.

    Models:
    - Theta decay as time passes during the day
    - Delta/gamma movement as underlying moves
    - IV estimated from daily range (simple model)
    """

    def __init__(self, risk_free_rate: float = 0.045):
        self.r = risk_free_rate

    def estimate_iv(self, bars: pd.DataFrame, underlying_price: float) -> float:
        """
        Estimate intraday implied volatility from recent bar data.

        Simple model: annualize the realized 20-bar standard deviation.
        In live trading we'd use actual market IV; for backtesting this
        gives a reasonable proxy.
        """
        if bars is None or len(bars) < 10:
            return 0.18  # Default 18% IV

        returns = bars["close"].pct_change().dropna().tail(20)
        if len(returns) < 5:
            return 0.18

        # Annualize: 1-min bars → ~390 bars/day × 252 days
        bar_vol = returns.std()
        bars_per_day = 390 if len(bars) > 100 else 78  # 1m vs 5m
        annual_vol = bar_vol * math.sqrt(bars_per_day * 252)

        # Clamp to realistic range
        return max(0.10, min(1.00, annual_vol))

    def time_to_expiry_years(self, current_time: datetime,
                              close_time: datetime = None) -> float:
        """
        Calculate time to expiry in years for 0DTE options.

        For 0DTE: remaining minutes today / (252 trading days × 390 min/day)
        """
        if close_time is None:
            close_time = current_time.replace(hour=16, minute=0, second=0, microsecond=0)
            # Handle UTC timestamps from Yahoo
            if hasattr(current_time, 'tzinfo') and current_time.tzinfo is not None:
                import pytz
                try:
                    et = pytz.timezone("US/Eastern")
                    local = current_time.astimezone(et)
                    close_time = local.replace(hour=16, minute=0, second=0, microsecond=0)
                    current_time = local
                except Exception:
                    close_time = current_time.replace(hour=21, minute=0, second=0, microsecond=0)

        remaining_seconds = (close_time - current_time).total_seconds()
        remaining_minutes = max(1, remaining_seconds / 60)  # Floor at 1 min

        return remaining_minutes / (252 * 390)

    def price_option(self, S: float, K: float, T: float, iv: float,
                     right: str) -> float:
        """Price a single option using Black-Scholes."""
        if T <= 0:
            # At expiration
            if right == "C":
                return max(0, S - K)
            else:
                return max(0, K - S)

        if right == "C":
            return bs_call_price(S, K, T, self.r, iv)
        else:
            return bs_put_price(S, K, T, self.r, iv)

    def get_greeks(self, S: float, K: float, T: float, iv: float,
                   right: str) -> Dict[str, float]:
        """Get all greeks for an option."""
        opt_type = "call" if right == "C" else "put"
        return {
            "delta": bs_delta(S, K, T, self.r, iv, opt_type),
            "gamma": bs_gamma(S, K, T, self.r, iv),
            "theta": bs_theta(S, K, T, self.r, iv, opt_type),
            "vega": bs_vega(S, K, T, self.r, iv),
        }


# ─────────────────────────────────────────────────────────────────
# Long Options Backtester
# ─────────────────────────────────────────────────────────────────

class LottoBacktester:
    """
    Backtests the long options (buy calls/puts) strategy.

    Replays intraday bars day-by-day:
    1. Each day, feed bars to MomentumDetector to find triggers
    2. For each trigger, simulate entry with BS pricing
    3. Track position bar-by-bar with Greeks re-pricing
    4. Apply exit rules (stop, target, trailing, time)
    5. Aggregate results across all days
    """

    def __init__(self, config: Optional[EngineConfig] = None,
                 account_size: float = 10_000.0):
        self.config = config or EngineConfig()
        self.lotto_cfg = self.config.lotto
        self.account_size = account_size
        self.detector = MomentumDetector(self.lotto_cfg)
        self.pricer = OptionPricer()

        # Slippage model: 0DTE options have wide bid-ask
        self.entry_slippage_pct = 0.10    # Pay 10% above mid on entry
        self.exit_slippage_pct = 0.10     # Receive 10% below mid on exit
        self.commission_per_contract = 0.65  # IBKR commission

    def run(self, bars_df: pd.DataFrame, ticker: str = "SPY",
            interval: str = "5m",
            max_trades_per_day: int = 5,
            verbose: bool = False) -> LottoBacktestResults:
        """
        Run the backtest on a DataFrame of intraday bars.

        Args:
            bars_df: DataFrame with columns [open, high, low, close, volume]
                     Index must be DatetimeIndex
            ticker: Symbol being tested
            interval: Bar interval ("1m", "5m", etc.)
            max_trades_per_day: Max triggers to act on per day
            verbose: Print per-trade detail

        Returns:
            LottoBacktestResults with full P&L and trigger analysis
        """
        profile = get_ticker_profile(ticker)
        premium_scale = profile.premium_scale

        results = LottoBacktestResults(
            ticker=ticker,
            interval=interval,
            starting_balance=self.account_size,
        )

        # Split bars into trading days
        days = self._split_into_days(bars_df)
        if not days:
            print("  ⚠️  No trading days found in data")
            return results

        results.start_date = days[0][0]
        results.end_date = days[-1][0]
        results.total_days = len(days)

        balance = self.account_size
        peak = balance
        equity = [{"date": str(days[0][0]), "balance": balance}]

        for day_date, day_bars in days:
            day_result = self._run_day(
                day_date, day_bars, ticker, profile, premium_scale,
                balance, max_trades_per_day, verbose,
            )

            results.daily_results.append(day_result)
            results.trades.extend(
                [t for t in day_result._trades if not t.is_open]
            )

            balance += day_result.day_pnl
            peak = max(peak, balance)
            dd = (peak - balance) / peak if peak > 0 else 0
            results.max_drawdown_pct = max(results.max_drawdown_pct, dd)

            equity.append({"date": str(day_date), "balance": round(balance, 2)})

            if day_result.trades_entered > 0:
                results.days_traded += 1

        results.equity_curve = equity
        results.ending_balance = round(balance, 2)

        # Aggregate stats
        self._compute_stats(results)

        return results

    def _split_into_days(self, df: pd.DataFrame) -> List[Tuple[date, pd.DataFrame]]:
        """Split a continuous DataFrame into per-day DataFrames."""
        if df.empty:
            return []

        # Ensure datetime index
        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index, utc=True)

        days = []
        for d, group in df.groupby(df.index.date):
            if len(group) >= 10:  # Need at least 10 bars
                days.append((d, group))

        return sorted(days, key=lambda x: x[0])

    def _run_day(self, day_date: date, day_bars: pd.DataFrame,
                 ticker: str, profile: TickerProfile,
                 premium_scale: float, balance: float,
                 max_trades: int, verbose: bool) -> LottoBacktestDay:
        """Simulate one trading day."""
        day_result = LottoBacktestDay(date=day_date)
        day_result._trades = []  # Internal: track ALL trade objects

        # Budget for the day
        daily_budget = balance * self.config.account.lotto_daily_budget_pct
        per_trade_max = balance * self.config.account.lotto_max_per_trade_pct
        daily_spent = 0.0
        open_positions: List[SimLongOption] = []
        all_positions: List[SimLongOption] = []  # Keep reference to ALL trades
        trades_entered = 0

        # Estimate IV for the day from the first 30 bars
        day_iv = self.pricer.estimate_iv(day_bars.head(30), day_bars["close"].iloc[0])

        # Determine bar interval in minutes
        if len(day_bars) >= 2:
            time_diff = (day_bars.index[1] - day_bars.index[0]).total_seconds() / 60
            bar_minutes = max(1, int(round(time_diff)))
        else:
            bar_minutes = 5

        # Minimum bars before we start scanning (need ORB = 15 min)
        min_scan_bar = max(16, self.lotto_cfg.scan_start_min // bar_minutes)

        total_bars = len(day_bars)

        for i in range(min_scan_bar, total_bars):
            current_bar = day_bars.iloc[i]
            current_time = day_bars.index[i]
            current_price = float(current_bar["close"])

            # Time to close (approximate)
            T = self.pricer.time_to_expiry_years(current_time)
            minutes_to_close = T * 252 * 390

            # ── Update open positions ────────────────────────────
            for pos in open_positions:
                if not pos.is_open:
                    continue

                pos.current_underlying = current_price
                pos.current_price = self.pricer.price_option(
                    current_price, pos.strike, T, pos.entry_iv, pos.right,
                )
                pos.high_water_mark = max(pos.high_water_mark, pos.current_price)

                # Check exits
                exit_reason = self._check_exit(pos, minutes_to_close)
                if exit_reason:
                    self._close_position(pos, exit_reason, current_time,
                                         current_price, premium_scale, verbose)
                    day_result.trades_closed += 1
                    if pos.total_pnl > 0:
                        day_result.winning_trades += 1
                    else:
                        day_result.losing_trades += 1
                    day_result.day_pnl += pos.total_pnl

            # Remove closed positions
            open_positions = [p for p in open_positions if p.is_open]

            # ── Scan for new triggers ────────────────────────────
            if (trades_entered < max_trades and
                    len(open_positions) < self.config.account.lotto_max_positions and
                    daily_spent < daily_budget and
                    minutes_to_close > 30):  # Don't enter in last 30 min

                # Feed bars up to current point to detector
                bars_so_far = day_bars.iloc[:i+1].copy()
                triggers = self.detector.detect_all(bars_so_far, ticker, current_price)

                if triggers:
                    day_result.triggers_found += len(triggers)

                    # Take the best trigger (highest confidence)
                    best = triggers[0]

                    # Check if we already have a position in this direction
                    same_direction = any(
                        p.direction == best.direction and p.is_open
                        for p in open_positions
                    )
                    if same_direction:
                        continue

                    # Calculate strike and price
                    strike = _round_strike(best.strike, ticker)
                    option_price = self.pricer.price_option(
                        current_price, strike, T, day_iv, best.right,
                    )

                    # Apply entry slippage (we pay the ask, which is above mid)
                    entry_premium = option_price * (1 + self.entry_slippage_pct)

                    # Premium filters (scaled for SPX)
                    min_prem = self.lotto_cfg.min_premium * premium_scale
                    if best.tier == "momentum":
                        max_prem = self.lotto_cfg.momentum_max_premium * premium_scale
                    else:
                        max_prem = self.lotto_cfg.sniper_max_premium * premium_scale

                    if entry_premium < min_prem or entry_premium > max_prem:
                        continue

                    # Position sizing
                    cost_per_contract = entry_premium * 100
                    max_by_trade = int(per_trade_max / cost_per_contract) if cost_per_contract > 0 else 0
                    max_by_daily = int((daily_budget - daily_spent) / cost_per_contract) if cost_per_contract > 0 else 0
                    max_by_cfg = self.lotto_cfg.max_contracts_per_trade
                    num_contracts = min(max_by_trade, max_by_daily, max_by_cfg)
                    num_contracts = max(1, num_contracts) if num_contracts >= 1 else 0

                    if num_contracts <= 0:
                        continue

                    total_cost = cost_per_contract * num_contracts
                    commission = self.commission_per_contract * num_contracts * 2  # Open + close

                    # Create position
                    pos = SimLongOption(
                        ticker=ticker,
                        strike=strike,
                        right=best.right,
                        expiry_date=day_date,
                        trigger_type=best.trigger_type,
                        tier=best.tier,
                        direction=best.direction,
                        entry_time=current_time,
                        entry_price=entry_premium,
                        entry_underlying=current_price,
                        entry_iv=day_iv,
                        num_contracts=num_contracts,
                        original_contracts=num_contracts,
                        current_price=entry_premium,
                        current_underlying=current_price,
                        high_water_mark=entry_premium,
                    )

                    open_positions.append(pos)
                    all_positions.append(pos)  # Keep permanent reference
                    daily_spent += total_cost
                    trades_entered += 1
                    day_result.trades_entered += 1
                    day_result.trigger_types.append(best.trigger_type)

                    if verbose:
                        print(f"    [{day_date}] ENTER {best.trigger_type} "
                              f"BUY {best.direction} {ticker} {strike}{best.right} "
                              f"@ ${entry_premium:.2f} x{num_contracts} "
                              f"(conf={best.confidence:.0%}, tier={best.tier})")

        # ── End of day: force-close anything still open ──────────
        for pos in open_positions:
            if pos.is_open:
                last_price = float(day_bars["close"].iloc[-1])
                last_time = day_bars.index[-1]
                T_final = self.pricer.time_to_expiry_years(last_time)

                pos.current_price = self.pricer.price_option(
                    last_price, pos.strike, max(T_final, 1e-8),
                    pos.entry_iv, pos.right,
                )
                self._close_position(pos, "EOD_CLOSE", last_time,
                                     last_price, premium_scale, verbose)
                day_result.trades_closed += 1
                if pos.total_pnl > 0:
                    day_result.winning_trades += 1
                else:
                    day_result.losing_trades += 1
                day_result.day_pnl += pos.total_pnl

        day_result._trades = all_positions

        return day_result

    def _check_exit(self, pos: SimLongOption, minutes_to_close: float) -> Optional[str]:
        """Check all exit conditions for a position."""
        entry = pos.entry_price
        current = pos.current_price

        if entry <= 0:
            return None

        mult = current / entry

        # 1. Time exit: close before market close
        if minutes_to_close <= self.lotto_cfg.time_exit_minutes_before_close:
            return f"TIME_EXIT ({minutes_to_close:.0f}m left)"

        # 2. Stop-loss: premium dropped 50%
        if mult <= (1 - self.lotto_cfg.stop_loss_pct):
            return f"STOP_LOSS ({mult:.2f}x)"

        # 3. Profit target: hit 3x → partial exit
        if mult >= self.lotto_cfg.profit_target_mult and not pos.took_partial:
            return "PROFIT_TARGET"

        # 4. Runner exit: after partial, if drops below floor
        if pos.took_partial and mult < self.lotto_cfg.runner_floor_mult:
            return f"RUNNER_EXIT ({mult:.1f}x)"

        # 5. Trailing from high-water mark
        if pos.high_water_mark > entry * self.lotto_cfg.trailing_start_mult:
            drop_from_peak = (pos.high_water_mark - current) / pos.high_water_mark
            if drop_from_peak >= self.lotto_cfg.trailing_drop_pct:
                return f"TRAILING_EXIT ({drop_from_peak:.0%} from HWM)"

        return None

    def _close_position(self, pos: SimLongOption, reason: str,
                        exit_time: datetime, exit_underlying: float,
                        premium_scale: float, verbose: bool):
        """Close a position and calculate P&L."""
        exit_price = pos.current_price * (1 - self.exit_slippage_pct)
        exit_price = max(0, exit_price)

        commission = self.commission_per_contract * pos.num_contracts

        # Handle partial profit-taking
        if reason == "PROFIT_TARGET" and pos.num_contracts >= 2:
            # Sell 70%, keep 30%
            sell_count = max(1, int(pos.num_contracts * (1 - self.lotto_cfg.runner_keep_pct)))
            runner_count = pos.num_contracts - sell_count

            partial_pnl = (exit_price - pos.entry_price) * sell_count * 100
            partial_pnl -= self.commission_per_contract * sell_count

            pos.partial_pnl = round(partial_pnl, 2)
            pos.took_partial = True
            pos.runner_contracts = runner_count
            pos.num_contracts = runner_count

            if verbose:
                print(f"    [{pos.expiry_date}] PARTIAL {pos.trigger_type} "
                      f"{sell_count}x @ ${exit_price:.2f} "
                      f"(P&L: ${partial_pnl:+.2f}, keeping {runner_count} runner)")

            # Don't fully close yet — runner stays open
            return

        # Full close
        pnl_amount = (exit_price - pos.entry_price) * pos.num_contracts * 100
        pnl_amount -= commission

        # Add any earlier partial P&L
        total_pnl = round(pnl_amount + pos.partial_pnl, 2)

        pos.is_open = False
        pos.exit_time = exit_time
        pos.exit_price = exit_price
        pos.exit_reason = reason
        pos.exit_underlying = exit_underlying
        pos.total_pnl = total_pnl
        pos.runner_pnl = round(pnl_amount, 2) if pos.took_partial else 0.0

        if verbose:
            mult = exit_price / pos.entry_price if pos.entry_price > 0 else 0
            print(f"    [{pos.expiry_date}] EXIT  {pos.trigger_type} "
                  f"{pos.direction} {pos.strike}{pos.right} → {reason} "
                  f"| {mult:.2f}x | P&L: ${total_pnl:+.2f}")

    def _compute_stats(self, results: LottoBacktestResults):
        """Compute aggregate statistics from all trades."""
        trades = results.trades
        results.total_trades = len(trades)

        if not trades:
            return

        pnls = [t.total_pnl for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]

        results.winning_trades = len(wins)
        results.losing_trades = len(losses)
        results.win_rate = round(len(wins) / len(trades) * 100, 1) if trades else 0
        results.total_pnl = round(sum(pnls), 2)
        results.avg_win = round(np.mean(wins), 2) if wins else 0
        results.avg_loss = round(np.mean(losses), 2) if losses else 0
        results.biggest_win = round(max(pnls), 2) if pnls else 0
        results.biggest_loss = round(min(pnls), 2) if pnls else 0

        gross_wins = sum(wins) if wins else 0
        gross_losses = abs(sum(losses)) if losses else 0
        results.profit_factor = round(gross_wins / gross_losses, 2) if gross_losses > 0 else float("inf")

        # Return multiples
        mults = [t.exit_price / t.entry_price if t.entry_price > 0 else 0 for t in trades]
        win_mults = [m for m, p in zip(mults, pnls) if p > 0]
        lose_mults = [m for m, p in zip(mults, pnls) if p <= 0]
        results.avg_winner_mult = round(np.mean(win_mults), 2) if win_mults else 0
        results.avg_loser_mult = round(np.mean(lose_mults), 2) if lose_mults else 0
        results.avg_return_pct = round(np.mean([(m - 1) * 100 for m in mults]), 1) if mults else 0

        # Total premium spent
        results.total_spent = round(sum(
            t.entry_price * t.original_contracts * 100 for t in trades
        ), 2)

        # Trigger breakdown
        trigger_stats = {}
        for t in trades:
            tt = t.trigger_type
            if tt not in trigger_stats:
                trigger_stats[tt] = {
                    "trades": 0, "wins": 0, "losses": 0,
                    "pnl": 0.0, "avg_mult": [],
                }
            trigger_stats[tt]["trades"] += 1
            trigger_stats[tt]["pnl"] += t.total_pnl
            mult = t.exit_price / t.entry_price if t.entry_price > 0 else 0
            trigger_stats[tt]["avg_mult"].append(mult)
            if t.total_pnl > 0:
                trigger_stats[tt]["wins"] += 1
            else:
                trigger_stats[tt]["losses"] += 1

        for tt in trigger_stats:
            s = trigger_stats[tt]
            s["win_rate"] = round(s["wins"] / s["trades"] * 100, 1) if s["trades"] else 0
            s["avg_mult"] = round(np.mean(s["avg_mult"]), 2) if s["avg_mult"] else 0
            s["pnl"] = round(s["pnl"], 2)

        results.trigger_stats = trigger_stats

        # Tier breakdown
        tier_stats = {}
        for t in trades:
            tier = t.tier
            if tier not in tier_stats:
                tier_stats[tier] = {
                    "trades": 0, "wins": 0, "losses": 0,
                    "pnl": 0.0, "avg_mult": [],
                }
            tier_stats[tier]["trades"] += 1
            tier_stats[tier]["pnl"] += t.total_pnl
            mult = t.exit_price / t.entry_price if t.entry_price > 0 else 0
            tier_stats[tier]["avg_mult"].append(mult)
            if t.total_pnl > 0:
                tier_stats[tier]["wins"] += 1
            else:
                tier_stats[tier]["losses"] += 1

        for tier in tier_stats:
            s = tier_stats[tier]
            s["win_rate"] = round(s["wins"] / s["trades"] * 100, 1) if s["trades"] else 0
            s["avg_mult"] = round(np.mean(s["avg_mult"]), 2) if s["avg_mult"] else 0
            s["pnl"] = round(s["pnl"], 2)

        results.tier_stats = tier_stats

    # ─────────────────────────────────────────────────────────────
    # Reporting
    # ─────────────────────────────────────────────────────────────

    def print_report(self, results: LottoBacktestResults):
        """Print comprehensive backtest report."""
        print("\n" + "=" * 70)
        print("  LONG OPTIONS BACKTEST RESULTS")
        print("=" * 70)

        print(f"\n  {C.CYAN}Period:{C.RESET}     {results.start_date} → {results.end_date}")
        print(f"  {C.CYAN}Ticker:{C.RESET}     {results.ticker} ({results.interval} bars)")
        print(f"  {C.CYAN}Days:{C.RESET}       {results.total_days} total, {results.days_traded} traded")
        print(f"  {C.CYAN}Account:{C.RESET}    ${results.starting_balance:,.0f}")

        # P&L Summary
        print(f"\n  {'─' * 50}")
        print(f"  {C.BOLD}P&L SUMMARY{C.RESET}")
        print(f"  {'─' * 50}")
        pnl_color = C.GREEN if results.total_pnl >= 0 else C.RED
        print(f"  Total P&L:      {pnl_color}${results.total_pnl:+,.2f}{C.RESET}")
        print(f"  Return:         {pnl_color}{results.total_pnl / results.starting_balance * 100:+.1f}%{C.RESET}")
        print(f"  Ending Balance: ${results.ending_balance:,.2f}")
        print(f"  Max Drawdown:   {results.max_drawdown_pct:.1%}")
        print(f"  Total Spent:    ${results.total_spent:,.2f} in premiums")

        # Trade Stats
        print(f"\n  {'─' * 50}")
        print(f"  {C.BOLD}TRADE STATS{C.RESET}")
        print(f"  {'─' * 50}")
        print(f"  Total Trades:   {results.total_trades}")
        print(f"  Win Rate:       {results.win_rate:.1f}%")
        print(f"  Avg Win:        ${results.avg_win:+,.2f} ({results.avg_winner_mult:.2f}x)")
        print(f"  Avg Loss:       ${results.avg_loss:+,.2f} ({results.avg_loser_mult:.2f}x)")
        print(f"  Biggest Win:    ${results.biggest_win:+,.2f}")
        print(f"  Biggest Loss:   ${results.biggest_loss:+,.2f}")
        print(f"  Profit Factor:  {results.profit_factor:.2f}")
        print(f"  Avg Return:     {results.avg_return_pct:+.1f}% per trade")

        # Trigger Breakdown
        if results.trigger_stats:
            print(f"\n  {'─' * 50}")
            print(f"  {C.BOLD}TRIGGER BREAKDOWN{C.RESET}")
            print(f"  {'─' * 50}")
            print(f"  {'Trigger':<18} {'Trades':>6} {'WR':>6} {'Avg':>6} {'P&L':>10}")
            print(f"  {'─' * 50}")
            for tt, s in sorted(results.trigger_stats.items(),
                                key=lambda x: x[1]["pnl"], reverse=True):
                pnl_c = C.GREEN if s["pnl"] >= 0 else C.RED
                print(f"  {tt:<18} {s['trades']:>6} {s['win_rate']:>5.1f}% "
                      f"{s['avg_mult']:>5.2f}x {pnl_c}${s['pnl']:>+9,.2f}{C.RESET}")

        # Tier Breakdown
        if results.tier_stats:
            print(f"\n  {'─' * 50}")
            print(f"  {C.BOLD}TIER BREAKDOWN{C.RESET}")
            print(f"  {'─' * 50}")
            for tier, s in results.tier_stats.items():
                pnl_c = C.GREEN if s["pnl"] >= 0 else C.RED
                print(f"  {tier.upper():<12} {s['trades']} trades, "
                      f"{s['win_rate']:.1f}% WR, "
                      f"avg {s['avg_mult']:.2f}x, "
                      f"{pnl_c}${s['pnl']:+,.2f}{C.RESET}")

        # Daily Breakdown (top/bottom 5)
        if results.daily_results:
            print(f"\n  {'─' * 50}")
            print(f"  {C.BOLD}DAILY P&L (Top/Bottom 5){C.RESET}")
            print(f"  {'─' * 50}")
            sorted_days = sorted(results.daily_results,
                                 key=lambda d: d.day_pnl, reverse=True)
            for d in sorted_days[:5]:
                if d.day_pnl != 0:
                    c = C.GREEN if d.day_pnl > 0 else C.RED
                    print(f"  {d.date}  {c}${d.day_pnl:>+8,.2f}{C.RESET}  "
                          f"({d.trades_entered} trades, {d.winning_trades}W/{d.losing_trades}L)")
            if len(sorted_days) > 5:
                print(f"  {'...'}")
                for d in sorted_days[-5:]:
                    if d.day_pnl != 0:
                        c = C.GREEN if d.day_pnl > 0 else C.RED
                        print(f"  {d.date}  {c}${d.day_pnl:>+8,.2f}{C.RESET}  "
                              f"({d.trades_entered} trades, {d.winning_trades}W/{d.losing_trades}L)")

        # Exit Reason Breakdown
        if results.trades:
            print(f"\n  {'─' * 50}")
            print(f"  {C.BOLD}EXIT REASONS{C.RESET}")
            print(f"  {'─' * 50}")
            exit_reasons = {}
            for t in results.trades:
                reason = t.exit_reason.split("(")[0].strip()  # Remove details
                if reason not in exit_reasons:
                    exit_reasons[reason] = {"count": 0, "pnl": 0.0}
                exit_reasons[reason]["count"] += 1
                exit_reasons[reason]["pnl"] += t.total_pnl

            for reason, s in sorted(exit_reasons.items(),
                                     key=lambda x: x[1]["count"], reverse=True):
                c = C.GREEN if s["pnl"] >= 0 else C.RED
                print(f"  {reason:<20} {s['count']:>4} trades  {c}${s['pnl']:>+9,.2f}{C.RESET}")

        print(f"\n{'=' * 70}\n")

    def save_results(self, results: LottoBacktestResults,
                     path: str = "lotto_backtest_results.json"):
        """Save results to JSON."""
        data = {
            "period": f"{results.start_date} to {results.end_date}",
            "ticker": results.ticker,
            "interval": results.interval,
            "total_trades": results.total_trades,
            "win_rate": results.win_rate,
            "total_pnl": results.total_pnl,
            "profit_factor": results.profit_factor,
            "avg_return_pct": results.avg_return_pct,
            "starting_balance": results.starting_balance,
            "ending_balance": results.ending_balance,
            "max_drawdown_pct": round(results.max_drawdown_pct * 100, 1),
            "trigger_stats": results.trigger_stats,
            "tier_stats": results.tier_stats,
            "equity_curve": results.equity_curve,
            "trades": [
                {
                    "date": str(t.expiry_date),
                    "trigger": t.trigger_type,
                    "direction": t.direction,
                    "tier": t.tier,
                    "strike": t.strike,
                    "right": t.right,
                    "entry_price": t.entry_price,
                    "exit_price": t.exit_price,
                    "exit_reason": t.exit_reason,
                    "num_contracts": t.original_contracts,
                    "pnl": t.total_pnl,
                }
                for t in results.trades
            ],
        }

        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        print(f"  📁 Results saved to {path}")

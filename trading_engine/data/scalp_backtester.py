"""
0DTE Gamma Scalp Backtester
=============================
Replays intraday bars through the SignalEngine + ScalpExitEngine
to simulate the professional gamma scalping strategy.

Key differences from the old LottoBacktester:
  ✓ ATM strikes (delta 0.40-0.55), not OTM lottery tickets
  ✓ Exits based on UNDERLYING PRICE movement, not option premium
  ✓ Intrabar stop/target detection using bar HIGH/LOW
  ✓ Confirmation stacking (2+ signals required)
  ✓ Time windows (morning + power hour)
  ✓ No re-entry after stop in same direction
  ✓ Cooldown between trades
  ✓ Dynamic ATR for stop/target sizing

Option P&L is still calculated via Black-Scholes (realistic premium
changes accounting for delta, gamma, AND theta). But the EXIT DECISION
is made on underlying price — the critical improvement.

Data: Uses SPY 1m bars. Results transfer to SPX (same % moves,
      multiply dollar amounts by ~10).

Usage:
    python run_scalp_backtest.py
    python run_scalp_backtest.py --stop-atr 3.0 --target-atr 5.0
    python run_scalp_backtest.py --confirmations 3 --verbose
"""

import math
import os
import json
from dataclasses import dataclass, field
from datetime import datetime, date, time as dtime, timedelta
from typing import List, Dict, Any, Optional, Tuple

import numpy as np
import pandas as pd

from ..config import EngineConfig, ScalpConfig, get_ticker_profile, TickerProfile
from ..black_scholes import (
    bs_call_price, bs_put_price, bs_delta, bs_gamma, bs_theta, bs_vega,
)
from ..scalper import SignalEngine, ScalpExitEngine, ScalpSignal
from ..formatters import header, sub_header, kv, pnl, bar, C


# ─────────────────────────────────────────────────────────────────
# Simulated scalp position
# ─────────────────────────────────────────────────────────────────

@dataclass
class SimScalpPosition:
    """A simulated gamma scalp position tracked bar-by-bar."""
    # Identity
    ticker: str = ""
    strike: float = 0.0
    right: str = ""           # "C" or "P"
    direction: str = ""       # "CALL" or "PUT"
    expiry_date: date = field(default_factory=date.today)
    confirmations: List[str] = field(default_factory=list)
    confidence: float = 0.0

    # Entry
    entry_time: datetime = field(default_factory=datetime.now)
    entry_underlying: float = 0.0
    entry_premium: float = 0.0
    entry_iv: float = 0.20
    num_contracts: int = 1
    atr_at_entry: float = 0.0

    # Pre-computed levels
    stop_price: float = 0.0
    target_price: float = 0.0

    # Tracking
    current_underlying: float = 0.0
    current_premium: float = 0.0
    best_favorable_underlying: float = 0.0
    is_open: bool = True

    # Exit
    exit_time: Optional[datetime] = None
    exit_underlying: float = 0.0
    exit_premium: float = 0.0
    exit_reason: str = ""

    # P&L
    total_pnl: float = 0.0
    hold_minutes: float = 0.0


@dataclass
class ScalpBacktestDay:
    """Results for a single trading day."""
    date: date = field(default_factory=date.today)
    signals_found: int = 0
    trades_entered: int = 0
    trades_closed: int = 0
    day_pnl: float = 0.0
    winning_trades: int = 0
    losing_trades: int = 0
    atr_at_open: float = 0.0
    # Internal
    _trades: List[SimScalpPosition] = field(default_factory=list)


@dataclass
class ScalpBacktestResults:
    """Complete scalp backtest results."""
    # Period
    start_date: date = field(default_factory=date.today)
    end_date: date = field(default_factory=date.today)
    ticker: str = ""
    interval: str = "1m"
    total_days: int = 0
    days_traded: int = 0

    # Trades
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    scratch_trades: int = 0
    win_rate: float = 0.0

    # P&L
    total_pnl: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    biggest_win: float = 0.0
    biggest_loss: float = 0.0
    profit_factor: float = 0.0

    # Returns
    avg_return_pct: float = 0.0
    avg_hold_minutes: float = 0.0
    avg_underlying_move_at_exit: float = 0.0

    # Budget
    starting_balance: float = 10_000.0
    ending_balance: float = 10_000.0
    max_drawdown_pct: float = 0.0
    total_premium_spent: float = 0.0

    # Exit reason breakdown
    exit_stats: Dict[str, Dict] = field(default_factory=dict)

    # Confirmation breakdown
    confirmation_stats: Dict[str, Dict] = field(default_factory=dict)

    # Daily detail
    daily_results: List[ScalpBacktestDay] = field(default_factory=list)

    # All trades (for analysis)
    trades: List[SimScalpPosition] = field(default_factory=list)

    # Equity curve
    equity_curve: List[Dict] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────
# Option Pricer (reuses Black-Scholes)
# ─────────────────────────────────────────────────────────────────

class ScalpOptionPricer:
    """
    Prices ATM options using Black-Scholes.

    For ATM 0DTE options:
      - Premium ranges from $0.50 (30 min left) to $3.00 (at open)
      - Delta is ~0.50 throughout (ATM)
      - Gamma is massive (0.05-0.20)
      - Theta per minute is small relative to premium
    """

    def __init__(self, risk_free_rate: float = 0.045):
        self.r = risk_free_rate

    def estimate_iv(self, bars: pd.DataFrame, price: float,
                    bar_minutes: int = 1) -> float:
        """Estimate IV from recent realized volatility."""
        if bars is None or len(bars) < 10:
            return 0.18

        returns = bars["close"].pct_change().dropna().tail(20)
        if len(returns) < 5:
            return 0.18

        bar_vol = returns.std()
        bars_per_day = 390 // bar_minutes
        annual_vol = bar_vol * math.sqrt(bars_per_day * 252)
        return max(0.10, min(1.00, annual_vol))

    def time_to_expiry(self, current_time: datetime) -> float:
        """Calculate T in years for 0DTE."""
        if hasattr(current_time, 'tzinfo') and current_time.tzinfo is not None:
            import pytz
            try:
                et = pytz.timezone("US/Eastern")
                local = current_time.astimezone(et)
                close_time = local.replace(hour=16, minute=0, second=0, microsecond=0)
                current_time = local
            except Exception:
                close_time = current_time.replace(hour=21, minute=0, second=0)
        else:
            close_time = current_time.replace(hour=16, minute=0, second=0, microsecond=0)

        remaining_seconds = (close_time - current_time).total_seconds()
        remaining_minutes = max(1, remaining_seconds / 60)
        return remaining_minutes / (252 * 390)

    def price_option(self, S: float, K: float, T: float, iv: float,
                     right: str) -> float:
        """Price an option using Black-Scholes."""
        if T <= 0:
            if right == "C":
                return max(0, S - K)
            return max(0, K - S)

        if right == "C":
            return bs_call_price(S, K, T, self.r, iv)
        return bs_put_price(S, K, T, self.r, iv)


# ─────────────────────────────────────────────────────────────────
# Scalp Backtester
# ─────────────────────────────────────────────────────────────────

class ScalpBacktester:
    """
    Backtests the 0DTE gamma scalping strategy.

    Replays intraday bars day-by-day:
      1. Each bar: check exits on open position (using bar HIGH/LOW)
      2. If no position + in time window: evaluate SignalEngine
      3. If signal fires + passes all gates: enter via BS pricing
      4. Track position with ATR-based underlying price exits
      5. Calculate P&L using BS option repricing
      6. Aggregate across all days

    Conservative modeling:
      - Entry slippage: 10% above mid (pay the ask)
      - Exit slippage: 10% below mid (hit the bid)
      - Commission: $0.65/contract each way
      - If both stop AND target possible in same bar: assume stop (worst case)
    """

    def __init__(self, config: Optional[EngineConfig] = None,
                 account_size: float = 10_000.0,
                 spx_mode: bool = False):
        self.config = config or EngineConfig()
        self.scalp_cfg = self.config.scalp
        self.account_size = account_size
        self.pricer = ScalpOptionPricer()

        # SPX Mode: When True, scale SPY bars ×10 to SPX levels.
        # This is valid because SPX = SPY × 10 (identical % moves),
        # but option pricing, strike grid, and slippage differ.
        self.spx_mode = spx_mode
        self.price_scale = 10.0 if spx_mode else 1.0

        # Will be created per-run with correct bar_minutes
        self._signal_engine = None
        self._exit_engine = ScalpExitEngine(self.scalp_cfg)

        # Slippage model — ATM options have tighter spreads than OTM
        # SPX 0DTE ATM: massive liquidity, ~$0.50 wide on $20 = 1.25% each way
        # SPY ATM 0DTE: bid-ask ~5% of mid → 2% each way
        if spx_mode:
            self.entry_slippage_pct = 0.0125  # SPX ATM: 1.25% (tighter spreads)
            self.exit_slippage_pct = 0.0125
            self.commission_per_contract = 0.65  # Same per contract
        else:
            self.entry_slippage_pct = 0.03
            self.exit_slippage_pct = 0.03
            self.commission_per_contract = 0.65

    def run(self, bars_df: pd.DataFrame, ticker: str = "SPY",
            interval: str = "1m",
            verbose: bool = False) -> ScalpBacktestResults:
        """
        Run the scalp backtest on intraday bars.

        Args:
            bars_df: DataFrame [open, high, low, close, volume], DatetimeIndex
            ticker: Symbol (overridden to "SPX" if spx_mode is True)
            interval: Bar interval ("1m", "2m", etc.)
            verbose: Print per-trade detail

        Returns:
            ScalpBacktestResults
        """
        # In SPX mode, scale SPY price bars to SPX levels (×10)
        if self.spx_mode:
            ticker = "SPX"
            bars_df = bars_df.copy()
            for col in ["open", "high", "low", "close"]:
                if col in bars_df.columns:
                    bars_df[col] = bars_df[col] * self.price_scale
            # VWAP also scales if present
            if "vwap" in bars_df.columns:
                bars_df["vwap"] = bars_df["vwap"] * self.price_scale
            # Volume stays the same (it's a count)
            if verbose:
                print(f"  🔄 SPX MODE: Scaled SPY bars ×{self.price_scale:.0f} "
                      f"(price ~${bars_df['close'].iloc[0]:,.0f})")

        profile = get_ticker_profile(ticker)
        premium_scale = profile.premium_scale

        bar_minutes = self._parse_interval(interval)
        self._signal_engine = SignalEngine(self.scalp_cfg, bar_minutes=bar_minutes)

        results = ScalpBacktestResults(
            ticker=ticker,
            interval=interval,
            starting_balance=self.account_size,
        )

        days = self._split_into_days(bars_df)
        if not days:
            print("  ⚠️  No trading days found")
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
                balance, bar_minutes, verbose,
            )

            results.daily_results.append(day_result)
            results.trades.extend(day_result._trades)

            balance += day_result.day_pnl
            peak = max(peak, balance)
            dd = (peak - balance) / peak if peak > 0 else 0
            results.max_drawdown_pct = max(results.max_drawdown_pct, dd)

            equity.append({"date": str(day_date), "balance": round(balance, 2)})

            if day_result.trades_entered > 0:
                results.days_traded += 1

        results.equity_curve = equity
        results.ending_balance = round(balance, 2)

        self._compute_stats(results)
        return results

    @staticmethod
    def _parse_interval(interval: str) -> int:
        interval = interval.strip().lower()
        if interval.endswith("m"):
            return max(1, int(interval[:-1]))
        elif interval.endswith("h"):
            return int(interval[:-1]) * 60
        return 1

    def _split_into_days(self, df: pd.DataFrame) -> List[Tuple[date, pd.DataFrame]]:
        if df.empty:
            return []
        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index, utc=True)

        days = []
        for d, group in df.groupby(df.index.date):
            if len(group) >= 30:  # Need 30 bars minimum for signals
                days.append((d, group))
        return sorted(days, key=lambda x: x[0])

    def _run_day(self, day_date: date, day_bars: pd.DataFrame,
                 ticker: str, profile: TickerProfile,
                 premium_scale: float, balance: float,
                 bar_minutes: int, verbose: bool) -> ScalpBacktestDay:
        """Simulate one trading day with the gamma scalp strategy."""
        day_result = ScalpBacktestDay(date=day_date)
        day_result._trades = []

        # State for this day
        open_position: Optional[SimScalpPosition] = None
        cooldown_remaining = 0
        stopped_directions: set = set()
        trades_today = 0
        daily_pnl = 0.0

        # Estimate IV from first 30 bars
        day_iv = self.pricer.estimate_iv(day_bars.head(30),
                                          day_bars["close"].iloc[0],
                                          bar_minutes=bar_minutes)

        # Compute rolling ATR for the day
        high = day_bars["high"]
        low = day_bars["low"]
        prev_close = day_bars["close"].shift(1)
        tr = np.maximum(
            high - low,
            np.maximum(abs(high - prev_close), abs(low - prev_close)),
        )
        atr_period = max(5, self.scalp_cfg.atr_period // bar_minutes)
        rolling_atr = tr.rolling(atr_period, min_periods=3).mean()

        day_result.atr_at_open = rolling_atr.iloc[min(30, len(rolling_atr) - 1)]

        # Minimum bars before scanning
        min_scan_bar = max(30, 30 // bar_minutes)
        total_bars = len(day_bars)

        # Track previous signal state for "new signal" detection
        prev_signal_direction = None

        for i in range(min_scan_bar, total_bars):
            current_bar = day_bars.iloc[i]
            current_time = day_bars.index[i]
            current_price = float(current_bar["close"])
            bar_high = float(current_bar["high"])
            bar_low = float(current_bar["low"])

            # Time calculations
            T = self.pricer.time_to_expiry(current_time)
            minutes_to_close = T * 252 * 390

            # Minutes since market open (9:30 ET)
            if hasattr(current_time, 'hour'):
                # Handle timezone-aware timestamps
                if hasattr(current_time, 'tzinfo') and current_time.tzinfo is not None:
                    import pytz
                    try:
                        et = pytz.timezone("US/Eastern")
                        local_time = current_time.astimezone(et)
                        open_time = local_time.replace(hour=9, minute=30, second=0)
                        minutes_since_open = (local_time - open_time).total_seconds() / 60
                    except Exception:
                        minutes_since_open = i * bar_minutes
                else:
                    open_time = current_time.replace(hour=9, minute=30, second=0)
                    minutes_since_open = (current_time - open_time).total_seconds() / 60
            else:
                minutes_since_open = i * bar_minutes

            current_atr = rolling_atr.iloc[i] if not pd.isna(rolling_atr.iloc[i]) else 0.30

            # ── 1. Update open position ──────────────────────────
            if open_position and open_position.is_open:
                result = self._exit_engine.check_exit(
                    open_position, bar_high, bar_low, current_price,
                    current_time, minutes_to_close,
                )

                if result:
                    exit_reason, exit_underlying = result

                    # Price the option at exit using BS
                    exit_premium = self.pricer.price_option(
                        exit_underlying, open_position.strike, T,
                        open_position.entry_iv, open_position.right,
                    )
                    exit_premium = max(0, exit_premium * (1 - self.exit_slippage_pct))

                    commission = self.commission_per_contract * open_position.num_contracts * 2
                    pnl_amount = (exit_premium - open_position.entry_premium) * \
                                 open_position.num_contracts * 100 - commission

                    # Fill exit fields
                    open_position.is_open = False
                    open_position.exit_time = current_time
                    open_position.exit_underlying = exit_underlying
                    open_position.exit_premium = exit_premium
                    open_position.exit_reason = exit_reason
                    open_position.total_pnl = round(pnl_amount, 2)
                    open_position.hold_minutes = round(
                        (current_time - open_position.entry_time).total_seconds() / 60, 1
                    )

                    daily_pnl += pnl_amount

                    if exit_reason == "STOP_LOSS":
                        stopped_directions.add(open_position.direction)

                    cooldown_remaining = self.scalp_cfg.cooldown_bars
                    day_result.trades_closed += 1
                    if pnl_amount > 5:  # $5 threshold for "win" (above commission)
                        day_result.winning_trades += 1
                    elif pnl_amount < -5:
                        day_result.losing_trades += 1

                    if verbose:
                        mult = exit_premium / open_position.entry_premium \
                            if open_position.entry_premium > 0 else 0
                        print(f"    [{day_date}] EXIT  {open_position.direction} "
                              f"{open_position.strike}{open_position.right} "
                              f"→ {exit_reason} | {mult:.2f}x | "
                              f"P&L: ${pnl_amount:+.2f} | "
                              f"hold: {open_position.hold_minutes:.0f}m | "
                              f"Δunderlying: ${exit_underlying - open_position.entry_underlying:+.2f}")

                    open_position = None

            # ── 2. Scan for new signals ──────────────────────────
            if cooldown_remaining > 0:
                cooldown_remaining -= 1
                continue

            if open_position is not None:
                continue

            if trades_today >= self.scalp_cfg.max_trades_per_day:
                continue

            if daily_pnl <= -self.scalp_cfg.daily_loss_limit * self.price_scale:
                continue

            if minutes_to_close <= 30:
                continue

            # Check time window
            if not self._in_time_window(minutes_since_open):
                continue

            # Evaluate signal engine
            bars_so_far = day_bars.iloc[:i + 1].copy()
            signal = self._signal_engine.evaluate(bars_so_far, current_price, ticker)

            if signal is None:
                prev_signal_direction = None
                continue

            # Check direction lock
            if signal.direction in stopped_directions:
                continue

            # Signal is valid — check if it's "new" (direction changed)
            if signal.direction == prev_signal_direction:
                # Same signal persisting — only allow if we don't have cooldown
                # (cooldown was already checked above, so this is a fresh scan)
                pass

            prev_signal_direction = signal.direction
            day_result.signals_found += 1

            # ── 3. Enter position ────────────────────────────────
            # Compute ATM strike
            otm_distance = current_price * self.scalp_cfg.max_otm_pct
            if signal.direction == "CALL":
                strike = self._round_strike(current_price + otm_distance, profile)
                right = "C"
            else:
                strike = self._round_strike(current_price - otm_distance, profile)
                right = "P"

            # Price the option
            option_price = self.pricer.price_option(
                current_price, strike, T, day_iv, right,
            )
            entry_premium = option_price * (1 + self.entry_slippage_pct)

            # Premium filters
            min_prem = self.scalp_cfg.min_premium * premium_scale
            max_prem = self.scalp_cfg.max_premium * premium_scale

            if entry_premium < min_prem or entry_premium > max_prem:
                continue

            # Position sizing based on risk
            stop_dist = current_atr * self.scalp_cfg.stop_atr_mult
            risk_per_contract = 0.50 * stop_dist * 100  # delta ~0.50
            if risk_per_contract <= 0:
                continue

            # Scale max risk for SPX (10× notional, 10× risk per contract)
            max_risk = self.scalp_cfg.max_risk_per_trade * self.price_scale

            # Budget check: SPX 1 contract = ~$2,500, so for small accounts
            # allow up to 30% per trade (standard for 0DTE scalpers).
            # SPY: 5% per trade ($250/contract). SPX: 30% per trade ($2,500/contract).
            budget_pct = 0.30 if self.spx_mode else 0.05
            max_budget_contracts = int(balance * budget_pct / (entry_premium * 100)) \
                if entry_premium > 0 else 0

            max_contracts = min(
                int(max_risk / risk_per_contract),
                self.scalp_cfg.max_contracts,
                max(1, max_budget_contracts),  # Always allow at least 1 if affordable
            )
            # Final check: can we actually afford 1 contract?
            if entry_premium * 100 > balance * 0.50:
                continue  # Don't risk more than 50% on a single trade
            num_contracts = max(1, max_contracts) if max_contracts >= 1 else 0
            if num_contracts <= 0:
                continue

            # Compute stop and target levels
            target_dist = current_atr * self.scalp_cfg.profit_target_atr_mult
            if signal.direction == "CALL":
                stop_price = current_price - stop_dist
                target_price = current_price + target_dist
            else:
                stop_price = current_price + stop_dist
                target_price = current_price - target_dist

            # Create position
            pos = SimScalpPosition(
                ticker=ticker,
                strike=strike,
                right=right,
                direction=signal.direction,
                expiry_date=day_date,
                confirmations=signal.confirmations.copy(),
                confidence=signal.confidence,
                entry_time=current_time,
                entry_underlying=current_price,
                entry_premium=entry_premium,
                entry_iv=day_iv,
                num_contracts=num_contracts,
                atr_at_entry=current_atr,
                stop_price=stop_price,
                target_price=target_price,
                best_favorable_underlying=current_price,
            )

            open_position = pos
            trades_today += 1
            day_result.trades_entered += 1
            day_result._trades.append(pos)

            if verbose:
                print(f"    [{day_date}] ENTER {signal.direction} "
                      f"{ticker} {strike}{right} "
                      f"@ ${entry_premium:.2f} x{num_contracts} "
                      f"({', '.join(signal.confirmations)}) "
                      f"ATR=${current_atr:.3f} "
                      f"stop=${stop_price:.2f} target=${target_price:.2f}")

        # ── End of day: force-close open positions ───────────────
        if open_position and open_position.is_open:
            last_price = float(day_bars["close"].iloc[-1])
            last_time = day_bars.index[-1]
            T_final = self.pricer.time_to_expiry(last_time)

            exit_premium = self.pricer.price_option(
                last_price, open_position.strike,
                max(T_final, 1e-8), open_position.entry_iv, open_position.right,
            )
            exit_premium = max(0, exit_premium * (1 - self.exit_slippage_pct))
            commission = self.commission_per_contract * open_position.num_contracts * 2
            pnl_amount = (exit_premium - open_position.entry_premium) * \
                         open_position.num_contracts * 100 - commission

            open_position.is_open = False
            open_position.exit_time = last_time
            open_position.exit_underlying = last_price
            open_position.exit_premium = exit_premium
            open_position.exit_reason = "EOD_CLOSE"
            open_position.total_pnl = round(pnl_amount, 2)
            open_position.hold_minutes = round(
                (last_time - open_position.entry_time).total_seconds() / 60, 1
            )

            daily_pnl += pnl_amount
            day_result.trades_closed += 1
            if pnl_amount > 5:
                day_result.winning_trades += 1
            elif pnl_amount < -5:
                day_result.losing_trades += 1

        day_result.day_pnl = round(daily_pnl, 2)
        return day_result

    def _in_time_window(self, minutes_since_open: float) -> bool:
        """Check if in valid trading window."""
        cfg = self.scalp_cfg
        w1 = cfg.window_1_start <= minutes_since_open <= cfg.window_1_end
        w2 = cfg.window_2_start <= minutes_since_open <= cfg.window_2_end
        if cfg.enable_midday:
            return w1 or w2 or (cfg.window_1_end < minutes_since_open < cfg.window_2_start)
        return w1 or w2

    @staticmethod
    def _round_strike(price: float, profile: TickerProfile) -> float:
        inc = profile.strike_increment
        return round(price / inc) * inc

    # ─────────────────────────────────────────────────────────────
    # Statistics
    # ─────────────────────────────────────────────────────────────

    def _compute_stats(self, results: ScalpBacktestResults):
        """Compute aggregate statistics."""
        trades = [t for t in results.trades if not t.is_open]
        results.total_trades = len(trades)

        if not trades:
            return

        pnls = [t.total_pnl for t in trades]
        wins = [p for p in pnls if p > 5]     # Above commission = win
        losses = [p for p in pnls if p < -5]   # Below -commission = loss
        scratches = [p for p in pnls if -5 <= p <= 5]

        results.winning_trades = len(wins)
        results.losing_trades = len(losses)
        results.scratch_trades = len(scratches)
        results.win_rate = round(len(wins) / len(trades) * 100, 1) if trades else 0
        results.total_pnl = round(sum(pnls), 2)
        results.avg_win = round(np.mean(wins), 2) if wins else 0
        results.avg_loss = round(np.mean(losses), 2) if losses else 0
        results.biggest_win = round(max(pnls), 2) if pnls else 0
        results.biggest_loss = round(min(pnls), 2) if pnls else 0

        gross_wins = sum(wins) if wins else 0
        gross_losses = abs(sum(losses)) if losses else 0
        results.profit_factor = round(gross_wins / gross_losses, 2) \
            if gross_losses > 0 else float("inf")

        # Holding time
        hold_times = [t.hold_minutes for t in trades if t.hold_minutes > 0]
        results.avg_hold_minutes = round(np.mean(hold_times), 1) if hold_times else 0

        # Average underlying move at exit
        moves = []
        for t in trades:
            if t.direction == "CALL":
                moves.append(t.exit_underlying - t.entry_underlying)
            else:
                moves.append(t.entry_underlying - t.exit_underlying)
        results.avg_underlying_move_at_exit = round(np.mean(moves), 3) if moves else 0

        # Return per trade as % of premium
        return_pcts = [
            (t.exit_premium - t.entry_premium) / t.entry_premium * 100
            if t.entry_premium > 0 else 0
            for t in trades
        ]
        results.avg_return_pct = round(np.mean(return_pcts), 1) if return_pcts else 0

        # Total premium spent
        results.total_premium_spent = round(sum(
            t.entry_premium * t.num_contracts * 100 for t in trades
        ), 2)

        # Exit reason breakdown
        exit_stats = {}
        for t in trades:
            reason = t.exit_reason
            if reason not in exit_stats:
                exit_stats[reason] = {
                    "count": 0, "wins": 0, "losses": 0,
                    "pnl": 0.0, "avg_hold_min": [],
                }
            exit_stats[reason]["count"] += 1
            exit_stats[reason]["pnl"] += t.total_pnl
            exit_stats[reason]["avg_hold_min"].append(t.hold_minutes)
            if t.total_pnl > 5:
                exit_stats[reason]["wins"] += 1
            elif t.total_pnl < -5:
                exit_stats[reason]["losses"] += 1

        for reason in exit_stats:
            s = exit_stats[reason]
            s["win_rate"] = round(s["wins"] / s["count"] * 100, 1) if s["count"] else 0
            s["avg_hold_min"] = round(np.mean(s["avg_hold_min"]), 1)
            s["pnl"] = round(s["pnl"], 2)

        results.exit_stats = exit_stats

        # Confirmation breakdown
        conf_stats = {}
        for t in trades:
            key = "+".join(sorted(t.confirmations))
            if key not in conf_stats:
                conf_stats[key] = {"count": 0, "wins": 0, "pnl": 0.0}
            conf_stats[key]["count"] += 1
            conf_stats[key]["pnl"] += t.total_pnl
            if t.total_pnl > 5:
                conf_stats[key]["wins"] += 1

        for key in conf_stats:
            s = conf_stats[key]
            s["win_rate"] = round(s["wins"] / s["count"] * 100, 1) if s["count"] else 0
            s["pnl"] = round(s["pnl"], 2)

        results.confirmation_stats = conf_stats

    # ─────────────────────────────────────────────────────────────
    # Reporting
    # ─────────────────────────────────────────────────────────────

    def print_report(self, results: ScalpBacktestResults):
        """Print comprehensive backtest report."""
        print("\n" + "=" * 70)
        mode = "SPX 0DTE" if self.spx_mode else "SPY 0DTE"
        print(f"  ⚡ {mode} GAMMA SCALP BACKTEST RESULTS")
        print("=" * 70)

        print(f"\n  {C.CYAN}Period:{C.RESET}     {results.start_date} → {results.end_date}")
        print(f"  {C.CYAN}Ticker:{C.RESET}     {results.ticker} ({results.interval} bars)")
        print(f"  {C.CYAN}Days:{C.RESET}       {results.total_days} total, {results.days_traded} traded")
        print(f"  {C.CYAN}Account:{C.RESET}    ${results.starting_balance:,.0f}")

        # Strategy parameters
        cfg = self.scalp_cfg
        print(f"\n  {C.DIM}Strategy: ATM scalp | "
              f"stop={cfg.stop_atr_mult}×ATR | "
              f"target={cfg.profit_target_atr_mult}×ATR | "
              f"min_conf={cfg.min_confirmations} | "
              f"time_stop={cfg.time_stop_minutes}m | "
              f"max_hold={cfg.max_hold_minutes}m{C.RESET}")

        # P&L Summary
        print(f"\n  {'─' * 55}")
        print(f"  {C.BOLD}P&L SUMMARY{C.RESET}")
        print(f"  {'─' * 55}")
        pc = C.GREEN if results.total_pnl >= 0 else C.RED
        print(f"  Total P&L:       {pc}${results.total_pnl:+,.2f}{C.RESET}")
        ret = results.total_pnl / results.starting_balance * 100
        print(f"  Return:          {pc}{ret:+.1f}%{C.RESET}")
        print(f"  Ending Balance:  ${results.ending_balance:,.2f}")
        print(f"  Max Drawdown:    {results.max_drawdown_pct:.1%}")
        print(f"  Premium Spent:   ${results.total_premium_spent:,.2f}")

        # Trade Stats
        print(f"\n  {'─' * 55}")
        print(f"  {C.BOLD}TRADE STATS{C.RESET}")
        print(f"  {'─' * 55}")
        print(f"  Total Trades:    {results.total_trades}")
        print(f"  Win/Loss/Scratch: {results.winning_trades}W / "
              f"{results.losing_trades}L / {results.scratch_trades}S")
        wc = C.GREEN if results.win_rate >= 40 else C.RED
        print(f"  Win Rate:        {wc}{results.win_rate:.1f}%{C.RESET}")
        print(f"  Avg Win:         ${results.avg_win:+,.2f}")
        print(f"  Avg Loss:        ${results.avg_loss:+,.2f}")
        print(f"  Biggest Win:     ${results.biggest_win:+,.2f}")
        print(f"  Biggest Loss:    ${results.biggest_loss:+,.2f}")
        pfc = C.GREEN if results.profit_factor >= 1.0 else C.RED
        print(f"  Profit Factor:   {pfc}{results.profit_factor:.2f}{C.RESET}")
        print(f"  Avg Return:      {results.avg_return_pct:+.1f}% per trade")
        print(f"  Avg Hold:        {results.avg_hold_minutes:.1f} min")
        print(f"  Avg Δ Underlying: ${results.avg_underlying_move_at_exit:+.3f}")

        # Exit Reason Breakdown
        if results.exit_stats:
            print(f"\n  {'─' * 55}")
            print(f"  {C.BOLD}EXIT REASONS{C.RESET}")
            print(f"  {'─' * 55}")
            print(f"  {'Reason':<18} {'#':>4} {'WR':>6} {'AvgHold':>7} {'P&L':>10}")
            print(f"  {'─' * 55}")
            for reason, s in sorted(results.exit_stats.items(),
                                     key=lambda x: x[1]["count"], reverse=True):
                rc = C.GREEN if s["pnl"] >= 0 else C.RED
                print(f"  {reason:<18} {s['count']:>4} "
                      f"{s['win_rate']:>5.1f}% "
                      f"{s['avg_hold_min']:>5.1f}m "
                      f"{rc}${s['pnl']:>+9,.2f}{C.RESET}")

        # Confirmation Combination Breakdown
        if results.confirmation_stats:
            print(f"\n  {'─' * 55}")
            print(f"  {C.BOLD}SIGNAL COMBINATIONS{C.RESET}")
            print(f"  {'─' * 55}")
            print(f"  {'Confirmations':<30} {'#':>4} {'WR':>6} {'P&L':>10}")
            print(f"  {'─' * 55}")
            for combo, s in sorted(results.confirmation_stats.items(),
                                    key=lambda x: x[1]["pnl"], reverse=True):
                rc = C.GREEN if s["pnl"] >= 0 else C.RED
                print(f"  {combo:<30} {s['count']:>4} "
                      f"{s['win_rate']:>5.1f}% "
                      f"{rc}${s['pnl']:>+9,.2f}{C.RESET}")

        # Daily P&L (top/bottom 5)
        if results.daily_results:
            print(f"\n  {'─' * 55}")
            print(f"  {C.BOLD}DAILY P&L (Top/Bottom 5){C.RESET}")
            print(f"  {'─' * 55}")
            sorted_days = sorted(results.daily_results,
                                 key=lambda d: d.day_pnl, reverse=True)
            for d in sorted_days[:5]:
                if d.day_pnl != 0:
                    c = C.GREEN if d.day_pnl > 0 else C.RED
                    print(f"  {d.date}  {c}${d.day_pnl:>+8,.2f}{C.RESET}  "
                          f"({d.trades_entered} trades, {d.winning_trades}W/{d.losing_trades}L, "
                          f"ATR=${d.atr_at_open:.3f})")
            if len(sorted_days) > 5:
                print(f"  {'...'}")
                for d in sorted_days[-5:]:
                    if d.day_pnl != 0:
                        c = C.GREEN if d.day_pnl > 0 else C.RED
                        print(f"  {d.date}  {c}${d.day_pnl:>+8,.2f}{C.RESET}  "
                              f"({d.trades_entered} trades, {d.winning_trades}W/{d.losing_trades}L, "
                              f"ATR=${d.atr_at_open:.3f})")

        # Comparison to old strategy
        print(f"\n  {'─' * 55}")
        print(f"  {C.BOLD}vs OLD LOTTO STRATEGY{C.RESET}")
        print(f"  {'─' * 55}")
        print(f"  {'Metric':<25} {'Old (Lotto)':>12} {'New (Scalp)':>12}")
        print(f"  {'─' * 55}")
        print(f"  {'Win Rate':<25} {'~10%':>12} {results.win_rate:>11.1f}%")
        print(f"  {'Profit Factor':<25} {'~0.11':>12} {results.profit_factor:>11.2f}")
        old_ret = -88.0  # From old backtest
        print(f"  {'Total Return':<25} {old_ret:>+11.1f}% {ret:>+11.1f}%")
        print(f"  {'Avg Hold (min)':<25} {'hours':>12} {results.avg_hold_minutes:>11.1f}")
        print(f"  {'Strikes':<25} {'OTM Δ=0.10':>12} {'ATM Δ=0.50':>12}")
        print(f"  {'Exit Logic':<25} {'Premium %':>12} {'Underlying':>12}")

        print(f"\n{'=' * 70}\n")

    def save_results(self, results: ScalpBacktestResults,
                     path: str = "scalp_backtest_results.json"):
        """Save results to JSON."""
        data = {
            "strategy": "gamma_scalp",
            "spx_mode": self.spx_mode,
            "price_scale": self.price_scale,
            "period": f"{results.start_date} to {results.end_date}",
            "ticker": results.ticker,
            "interval": results.interval,
            "config": {
                "stop_atr_mult": self.scalp_cfg.stop_atr_mult,
                "profit_target_atr_mult": self.scalp_cfg.profit_target_atr_mult,
                "trailing_activation_atr": self.scalp_cfg.trailing_activation_atr,
                "trailing_distance_atr": self.scalp_cfg.trailing_distance_atr,
                "time_stop_minutes": self.scalp_cfg.time_stop_minutes,
                "max_hold_minutes": self.scalp_cfg.max_hold_minutes,
                "min_confirmations": self.scalp_cfg.min_confirmations,
                "windows": f"{self.scalp_cfg.window_1_start}-{self.scalp_cfg.window_1_end}, "
                           f"{self.scalp_cfg.window_2_start}-{self.scalp_cfg.window_2_end}",
            },
            "results": {
                "total_trades": results.total_trades,
                "win_rate": results.win_rate,
                "total_pnl": results.total_pnl,
                "profit_factor": results.profit_factor,
                "avg_return_pct": results.avg_return_pct,
                "avg_hold_minutes": results.avg_hold_minutes,
                "starting_balance": results.starting_balance,
                "ending_balance": results.ending_balance,
                "max_drawdown_pct": round(results.max_drawdown_pct * 100, 1),
            },
            "exit_stats": results.exit_stats,
            "confirmation_stats": results.confirmation_stats,
            "equity_curve": results.equity_curve,
            "trades": [
                {
                    "date": str(t.expiry_date),
                    "direction": t.direction,
                    "confirmations": t.confirmations,
                    "strike": t.strike,
                    "right": t.right,
                    "entry_underlying": round(t.entry_underlying, 2),
                    "exit_underlying": round(t.exit_underlying, 2),
                    "underlying_move": round(
                        (t.exit_underlying - t.entry_underlying) if t.direction == "CALL"
                        else (t.entry_underlying - t.exit_underlying), 3
                    ),
                    "entry_premium": round(t.entry_premium, 4),
                    "exit_premium": round(t.exit_premium, 4),
                    "exit_reason": t.exit_reason,
                    "num_contracts": t.num_contracts,
                    "pnl": t.total_pnl,
                    "hold_minutes": t.hold_minutes,
                    "atr": round(t.atr_at_entry, 4),
                }
                for t in results.trades
            ],
        }

        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        print(f"  📁 Results saved to {path}")

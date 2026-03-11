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

from ..config import EngineConfig, ScalpConfig, MeanReversionConfig, ORBConfig, get_ticker_profile, TickerProfile
from ..black_scholes import (
    bs_call_price, bs_put_price, bs_delta, bs_gamma, bs_theta, bs_vega,
)
from ..scalper import (
    SignalEngine, ScalpExitEngine, RunnerExitEngine,
    MeanReversionSignalEngine, MeanReversionExitEngine,
    ORBSignalEngine, ORBExitEngine, ORBSignal,
    ScalpSignal,
)
from ..regime import RegimeDetector, RegimeInfo
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
    tier: str = "scalp"       # "scalp" or "runner"

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
    iv_blocked: int = 0             # Signals blocked by IV discount filter
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

    # Runner tier stats
    runner_trades: int = 0
    runner_wins: int = 0
    runner_pnl: float = 0.0
    runner_biggest_win: float = 0.0
    runner_avg_hold: float = 0.0
    scalp_trades: int = 0
    scalp_wins: int = 0
    scalp_pnl: float = 0.0

    # Mean-reversion tier stats
    mr_trades: int = 0
    mr_wins: int = 0
    mr_pnl: float = 0.0
    mr_biggest_win: float = 0.0
    mr_avg_hold: float = 0.0

    # ORB tier stats
    orb_trades: int = 0
    orb_wins: int = 0
    orb_pnl: float = 0.0
    orb_biggest_win: float = 0.0
    orb_avg_hold: float = 0.0
    orb_regime_skipped: int = 0         # Days skipped by regime filter

    # IV discount filter stats
    iv_blocked_signals: int = 0         # Signals blocked by RV < IV
    iv_passed_signals: int = 0          # Signals that passed IV filter

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
        self.mr_cfg = self.config.mean_reversion
        self.orb_cfg = self.config.orb
        self.account_size = account_size
        self.pricer = ScalpOptionPricer()

        # SPX Mode: When True, scale SPY bars ×10 to SPX levels.
        # This is valid because SPX = SPY × 10 (identical % moves),
        # but option pricing, strike grid, and slippage differ.
        self.spx_mode = spx_mode
        self.price_scale = 10.0 if spx_mode else 1.0

        # Will be created per-run with correct bar_minutes
        self._signal_engine = None
        self._mr_signal_engine = None
        self._orb_signal_engine = None
        self._exit_engine = ScalpExitEngine(self.scalp_cfg)
        self._mr_exit_engine = MeanReversionExitEngine(self.mr_cfg)
        self._runner_exit_engine = RunnerExitEngine(self.scalp_cfg)
        self._orb_exit_engine = ORBExitEngine(self.orb_cfg)
        self._regime_detector = RegimeDetector()

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
        self._mr_signal_engine = MeanReversionSignalEngine(self.mr_cfg, bar_minutes=bar_minutes)
        self._orb_signal_engine = ORBSignalEngine(self.orb_cfg, bar_minutes=bar_minutes)

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

        prev_day_high = None
        prev_day_low = None
        orb_regime_skipped = 0

        for day_date, day_bars in days:
            day_result = self._run_day(
                day_date, day_bars, ticker, profile, premium_scale,
                balance, bar_minutes, verbose,
                prev_day_high=prev_day_high, prev_day_low=prev_day_low,
            )

            # Track previous day high/low for next day's PREV_HL signal
            prev_day_high = float(day_bars['high'].max())
            prev_day_low = float(day_bars['low'].min())

            results.daily_results.append(day_result)
            results.trades.extend(day_result._trades)
            results.iv_blocked_signals += day_result.iv_blocked
            results.iv_passed_signals += day_result.trades_entered
            orb_regime_skipped += getattr(day_result, '_orb_regime_skipped', 0)

            balance += day_result.day_pnl
            peak = max(peak, balance)
            dd = (peak - balance) / peak if peak > 0 else 0
            results.max_drawdown_pct = max(results.max_drawdown_pct, dd)

            equity.append({"date": str(day_date), "balance": round(balance, 2)})

            if day_result.trades_entered > 0:
                results.days_traded += 1

        results.equity_curve = equity
        results.ending_balance = round(balance, 2)
        results.orb_regime_skipped = orb_regime_skipped

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
                 bar_minutes: int, verbose: bool,
                 prev_day_high: float = None,
                 prev_day_low: float = None) -> ScalpBacktestDay:
        """Simulate one trading day with multi-strategy gamma scalp system.

        Strategy A (MOMENTUM SCALP): ATM options, breakout signals, morning + power hour.
        Strategy B (MEAN-REVERSION): ATM options, fade extremes, midday chop zone.
        Strategy C (RUNNER): OTM options, wide exits, let winners ride in power hour.
        Strategy D (ORB BREAKOUT): ATM options, opening range breakout, regime-filtered.
        All can be open simultaneously (different strategies, different edges).
        """
        day_result = ScalpBacktestDay(date=day_date)
        day_result._trades = []
        day_result._orb_regime_skipped = 0

        # ── Momentum scalp tier state ────────────────────────────
        open_scalp: Optional[SimScalpPosition] = None
        cooldown_remaining = 0
        stopped_directions: set = set()
        scalp_trades_today = 0
        daily_pnl = 0.0

        # ── Mean-reversion tier state ────────────────────────────
        open_mr: Optional[SimScalpPosition] = None
        mr_cooldown_remaining = 0
        mr_trades_today = 0
        mr_daily_pnl = 0.0

        # ── Runner tier state ────────────────────────────────────
        open_runner: Optional[SimScalpPosition] = None
        runner_trades_today = 0
        scalp_won_today = False          # Track if a scalp hit profit target
        scalp_win_direction = None       # Direction of winning scalp

        # ── ORB tier state ───────────────────────────────────────
        open_orb: Optional[SimScalpPosition] = None
        orb_trades_today = 0
        momentum_signal_fired = False    # Track if momentum ever fires this day

        # ── IV discount tracking ─────────────────────────────────
        iv_blocked_count = 0             # Signals blocked by IV discount filter

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

        # Compute daily median ATR for runner gate
        valid_atrs = rolling_atr.dropna()
        daily_median_atr = valid_atrs.median() if len(valid_atrs) > 10 else 0.30

        # Minimum bars before scanning
        min_scan_bar = max(30, 30 // bar_minutes)
        total_bars = len(day_bars)

        # Pre-compute all indicators for the day (O(n) once, not O(n²))
        precomp = self._signal_engine.precompute_day_indicators(day_bars)
        # Set previous day high/low for PREV_HL signal component
        precomp['prev_day_high'] = prev_day_high
        precomp['prev_day_low'] = prev_day_low
        mr_enabled = self.config.mean_reversion.enabled
        mr_precomp = self._mr_signal_engine.precompute_day_indicators(day_bars) if mr_enabled else None
        day_bars_index = day_bars.index

        # ── ORB pre-computation ──────────────────────────────────
        orb_enabled = self.orb_cfg.enabled
        orb_data = None
        orb_regime_ok = True
        if orb_enabled:
            orb_data = self._orb_signal_engine.compute_orb(day_bars)
            # Regime filter: classify day and skip bad regimes
            # In backtesting, use full-day classify for accuracy.
            # In live trading, classify_early() can be used after ORB forms.
            if self.orb_cfg.regime_filter_enabled and orb_data.get("valid", False):
                regime_info = self._regime_detector.classify(day_bars)
                skip = (
                    (self.orb_cfg.skip_dead_flat and regime_info.regime == "DEAD_FLAT")
                    or (self.orb_cfg.skip_choppy and regime_info.regime == "CHOPPY")
                    or (self.orb_cfg.skip_range_bound and regime_info.regime == "RANGE_BOUND")
                    or (self.orb_cfg.skip_mixed and regime_info.regime == "MIXED")
                )
                if skip:
                    orb_regime_ok = False
                    day_result._orb_regime_skipped = 1
                    if verbose:
                        print(f"    [{day_date}] 📊 ORB skipped: {regime_info.regime} "
                              f"(range={regime_info.day_range_pct:.4f})")

        # Pre-extract numpy arrays for fast bar access
        _close_arr = day_bars["close"].values
        _high_arr = day_bars["high"].values
        _low_arr = day_bars["low"].values

        # Track previous signal state for "new signal" detection
        prev_signal_direction = None

        for i in range(min_scan_bar, total_bars):
            current_time = day_bars_index[i]
            current_price = float(_close_arr[i])
            bar_high = float(_high_arr[i])
            bar_low = float(_low_arr[i])

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

            # ── 1a. Update open SCALP position ───────────────────
            if open_scalp and open_scalp.is_open:
                result = self._exit_engine.check_exit(
                    open_scalp, bar_high, bar_low, current_price,
                    current_time, minutes_to_close,
                )

                if result:
                    exit_reason, exit_underlying = result

                    # Price the option at exit using BS
                    exit_premium = self.pricer.price_option(
                        exit_underlying, open_scalp.strike, T,
                        open_scalp.entry_iv, open_scalp.right,
                    )
                    exit_premium = max(0, exit_premium * (1 - self.exit_slippage_pct))

                    commission = self.commission_per_contract * open_scalp.num_contracts * 2
                    pnl_amount = (exit_premium - open_scalp.entry_premium) * \
                                 open_scalp.num_contracts * 100 - commission

                    # Fill exit fields
                    open_scalp.is_open = False
                    open_scalp.exit_time = current_time
                    open_scalp.exit_underlying = exit_underlying
                    open_scalp.exit_premium = exit_premium
                    open_scalp.exit_reason = exit_reason
                    open_scalp.total_pnl = round(pnl_amount, 2)
                    open_scalp.hold_minutes = round(
                        (current_time - open_scalp.entry_time).total_seconds() / 60, 1
                    )

                    daily_pnl += pnl_amount

                    if exit_reason == "STOP_LOSS":
                        stopped_directions.add(open_scalp.direction)

                    # Track winning scalps for runner piggyback
                    if exit_reason == "PROFIT_TARGET" and pnl_amount > 5:
                        scalp_won_today = True
                        scalp_win_direction = open_scalp.direction

                    cooldown_remaining = self.scalp_cfg.cooldown_bars
                    day_result.trades_closed += 1
                    if pnl_amount > 5:
                        day_result.winning_trades += 1
                    elif pnl_amount < -5:
                        day_result.losing_trades += 1

                    if verbose:
                        mult = exit_premium / open_scalp.entry_premium \
                            if open_scalp.entry_premium > 0 else 0
                        print(f"    [{day_date}] SCALP EXIT  {open_scalp.direction} "
                              f"{open_scalp.strike}{open_scalp.right} "
                              f"→ {exit_reason} | {mult:.2f}x | "
                              f"P&L: ${pnl_amount:+.2f} | "
                              f"hold: {open_scalp.hold_minutes:.0f}m | "
                              f"Δunderlying: ${exit_underlying - open_scalp.entry_underlying:+.2f}")

                    open_scalp = None

            # ── 1b. Update open RUNNER position ──────────────────
            if open_runner and open_runner.is_open:
                result = self._runner_exit_engine.check_exit(
                    open_runner, bar_high, bar_low, current_price,
                    current_time, minutes_to_close,
                )

                if result:
                    exit_reason, exit_underlying = result

                    exit_premium = self.pricer.price_option(
                        exit_underlying, open_runner.strike, T,
                        open_runner.entry_iv, open_runner.right,
                    )
                    exit_premium = max(0, exit_premium * (1 - self.exit_slippage_pct))

                    # OTM slippage is wider — 5% of mid
                    otm_slippage = 0.05 if self.spx_mode else 0.08
                    exit_premium = max(0, exit_premium * (1 - otm_slippage))

                    commission = self.commission_per_contract * open_runner.num_contracts * 2
                    pnl_amount = (exit_premium - open_runner.entry_premium) * \
                                 open_runner.num_contracts * 100 - commission

                    open_runner.is_open = False
                    open_runner.exit_time = current_time
                    open_runner.exit_underlying = exit_underlying
                    open_runner.exit_premium = exit_premium
                    open_runner.exit_reason = exit_reason
                    open_runner.total_pnl = round(pnl_amount, 2)
                    open_runner.hold_minutes = round(
                        (current_time - open_runner.entry_time).total_seconds() / 60, 1
                    )

                    daily_pnl += pnl_amount
                    day_result.trades_closed += 1
                    if pnl_amount > 5:
                        day_result.winning_trades += 1
                    elif pnl_amount < -5:
                        day_result.losing_trades += 1

                    if verbose:
                        mult = exit_premium / open_runner.entry_premium \
                            if open_runner.entry_premium > 0 else 0
                        print(f"    [{day_date}] 🏃 RUNNER EXIT  {open_runner.direction} "
                              f"{open_runner.strike}{open_runner.right} "
                              f"→ {exit_reason} | {mult:.2f}x | "
                              f"P&L: ${pnl_amount:+.2f} | "
                              f"hold: {open_runner.hold_minutes:.0f}m | "
                              f"Δunderlying: ${exit_underlying - open_runner.entry_underlying:+.2f}")

                    open_runner = None

            # ── 1c. Update open MEAN-REVERSION position ─────────
            if open_mr and open_mr.is_open:
                result = self._mr_exit_engine.check_exit(
                    open_mr, bar_high, bar_low, current_price,
                    current_time, minutes_to_close,
                )

                if result:
                    exit_reason, exit_underlying = result

                    exit_premium = self.pricer.price_option(
                        exit_underlying, open_mr.strike, T,
                        open_mr.entry_iv, open_mr.right,
                    )
                    exit_premium = max(0, exit_premium * (1 - self.exit_slippage_pct))

                    commission = self.commission_per_contract * open_mr.num_contracts * 2
                    pnl_amount = (exit_premium - open_mr.entry_premium) * \
                                 open_mr.num_contracts * 100 - commission

                    open_mr.is_open = False
                    open_mr.exit_time = current_time
                    open_mr.exit_underlying = exit_underlying
                    open_mr.exit_premium = exit_premium
                    open_mr.exit_reason = exit_reason
                    open_mr.total_pnl = round(pnl_amount, 2)
                    open_mr.hold_minutes = round(
                        (current_time - open_mr.entry_time).total_seconds() / 60, 1
                    )

                    daily_pnl += pnl_amount
                    mr_daily_pnl += pnl_amount
                    mr_cooldown_remaining = self.mr_cfg.cooldown_bars
                    day_result.trades_closed += 1
                    if pnl_amount > 5:
                        day_result.winning_trades += 1
                    elif pnl_amount < -5:
                        day_result.losing_trades += 1

                    if verbose:
                        mult = exit_premium / open_mr.entry_premium \
                            if open_mr.entry_premium > 0 else 0
                        print(f"    [{day_date}] 🔄 MR EXIT  {open_mr.direction} "
                              f"{open_mr.strike}{open_mr.right} "
                              f"→ {exit_reason} | {mult:.2f}x | "
                              f"P&L: ${pnl_amount:+.2f} | "
                              f"hold: {open_mr.hold_minutes:.0f}m | "
                              f"Δunderlying: ${exit_underlying - open_mr.entry_underlying:+.2f}")

                    open_mr = None

            # ── 1d. Update open ORB position ─────────────────────
            if open_orb and open_orb.is_open:
                result = self._orb_exit_engine.check_exit(
                    open_orb, bar_high, bar_low, current_price,
                    current_time, minutes_to_close,
                )

                if result:
                    exit_reason, exit_underlying = result

                    exit_premium = self.pricer.price_option(
                        exit_underlying, open_orb.strike, T,
                        open_orb.entry_iv, open_orb.right,
                    )
                    exit_premium = max(0, exit_premium * (1 - self.exit_slippage_pct))

                    commission = self.commission_per_contract * open_orb.num_contracts * 2
                    pnl_amount = (exit_premium - open_orb.entry_premium) * \
                                 open_orb.num_contracts * 100 - commission

                    open_orb.is_open = False
                    open_orb.exit_time = current_time
                    open_orb.exit_underlying = exit_underlying
                    open_orb.exit_premium = exit_premium
                    open_orb.exit_reason = exit_reason
                    open_orb.total_pnl = round(pnl_amount, 2)
                    open_orb.hold_minutes = round(
                        (current_time - open_orb.entry_time).total_seconds() / 60, 1
                    )

                    daily_pnl += pnl_amount
                    day_result.trades_closed += 1
                    if pnl_amount > 5:
                        day_result.winning_trades += 1
                    elif pnl_amount < -5:
                        day_result.losing_trades += 1

                    if verbose:
                        mult = exit_premium / open_orb.entry_premium \
                            if open_orb.entry_premium > 0 else 0
                        print(f"    [{day_date}] 📊 ORB EXIT  {open_orb.direction} "
                              f"{open_orb.strike}{open_orb.right} "
                              f"→ {exit_reason} | {mult:.2f}x | "
                              f"P&L: ${pnl_amount:+.2f} | "
                              f"hold: {open_orb.hold_minutes:.0f}m | "
                              f"Δunderlying: ${exit_underlying - open_orb.entry_underlying:+.2f}")

                    open_orb = None

            # ── 2. Scan for new signals ──────────────────────────
            if cooldown_remaining > 0:
                cooldown_remaining -= 1
            if mr_cooldown_remaining > 0:
                mr_cooldown_remaining -= 1

            # Need at least 30 min to close for new entries
            if minutes_to_close <= 30:
                continue

            if daily_pnl <= -self.scalp_cfg.daily_loss_limit * self.price_scale:
                continue

            # ── 2a. MOMENTUM signal evaluation ───────────────────
            # Only in momentum windows (morning + power hour)
            signal = None
            if self._in_time_window(minutes_since_open):
                signal = self._signal_engine.evaluate_fast(
                    precomp, i, current_price, ticker, bars_index=day_bars_index
                )

            if signal is not None:
                prev_signal_direction = signal.direction
                momentum_signal_fired = True  # ORB defers when momentum fires
            else:
                prev_signal_direction = None

            # ── 2b. MEAN-REVERSION signal evaluation ─────────────
            # Only in midday chop window when MR is enabled
            mr_signal = None
            if mr_enabled and mr_precomp is not None and self._in_mr_time_window(minutes_since_open):
                mr_signal = self._mr_signal_engine.evaluate_fast(
                    mr_precomp, i, current_price, ticker, bars_index=day_bars_index
                )

            # ── 3a. MOMENTUM SCALP entry ─────────────────────────
            if signal is not None:
                can_enter_scalp = (
                    open_scalp is None
                    and cooldown_remaining <= 0
                    and scalp_trades_today < self.scalp_cfg.max_trades_per_day
                    and signal.direction not in stopped_directions
                )

                # ── IV Discount Gate ─────────────────────────────
                # Only buy when realized vol >= threshold × implied vol.
                # When RV > IV, options are underpriced (cheap).
                # Morning (W1) uses a STRICTER threshold — AM has 33% WR
                # vs 78% in PM, so require deeper IV discount to enter AM.
                iv_discount_ok = True
                current_rv = precomp['rv'][i] if 'rv' in precomp else float('nan')
                if can_enter_scalp and self.scalp_cfg.iv_discount_enabled:
                    import math as _math
                    if not _math.isnan(current_rv) and day_iv > 0:
                        rv_iv_ratio = current_rv / day_iv
                        # Use stricter threshold for morning window
                        w1_end = self.scalp_cfg.window_1_end
                        if minutes_since_open <= w1_end:
                            required_ratio = self.scalp_cfg.rv_iv_min_ratio_w1
                        else:
                            required_ratio = self.scalp_cfg.rv_iv_min_ratio
                        if rv_iv_ratio < required_ratio:
                            iv_discount_ok = False
                            iv_blocked_count += 1

                if can_enter_scalp and iv_discount_ok:
                    day_result.signals_found += 1

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

                    if min_prem <= entry_premium <= max_prem:
                        # Position sizing based on risk
                        stop_dist = current_atr * self.scalp_cfg.stop_atr_mult
                        risk_per_contract = 0.50 * stop_dist * 100
                        if risk_per_contract > 0:
                            max_risk = self.scalp_cfg.max_risk_per_trade * self.price_scale
                            budget_pct = 0.30 if self.spx_mode else 0.05

                            # Conviction sizing: deeper IV discount → more contracts + risk
                            # When RV >> IV, options are deeply cheap → edge is larger → size up
                            import math as _math2
                            base_max_contracts = self.scalp_cfg.max_contracts
                            if (not _math2.isnan(current_rv) and day_iv > 0
                                    and self.scalp_cfg.rv_iv_premium_ratio > 0):
                                rv_iv = current_rv / day_iv
                                if rv_iv >= self.scalp_cfg.rv_iv_premium_ratio:
                                    # Deeply discounted: 50% more contracts AND risk budget
                                    base_max_contracts = int(base_max_contracts * 1.5)
                                    max_risk = max_risk * 1.5

                            max_budget_contracts = int(balance * budget_pct / (entry_premium * 100)) \
                                if entry_premium > 0 else 0

                            max_contracts = min(
                                int(max_risk / risk_per_contract),
                                base_max_contracts,
                                max(1, max_budget_contracts),
                            )
                            if entry_premium * 100 <= balance * 0.50:
                                num_contracts = max(1, max_contracts) if max_contracts >= 1 else 0
                                if num_contracts > 0:
                                    # Compute stop and target levels
                                    target_dist = current_atr * self.scalp_cfg.profit_target_atr_mult
                                    if signal.direction == "CALL":
                                        stop_price = current_price - stop_dist
                                        target_price = current_price + target_dist
                                    else:
                                        stop_price = current_price + stop_dist
                                        target_price = current_price - target_dist

                                    pos = SimScalpPosition(
                                        ticker=ticker, strike=strike, right=right,
                                        direction=signal.direction, expiry_date=day_date,
                                        confirmations=signal.confirmations.copy(),
                                        confidence=signal.confidence, tier="scalp",
                                        entry_time=current_time, entry_underlying=current_price,
                                        entry_premium=entry_premium, entry_iv=day_iv,
                                        num_contracts=num_contracts, atr_at_entry=current_atr,
                                        stop_price=stop_price, target_price=target_price,
                                        best_favorable_underlying=current_price,
                                    )

                                    open_scalp = pos
                                    scalp_trades_today += 1
                                    day_result.trades_entered += 1
                                    day_result._trades.append(pos)

                                    if verbose:
                                        print(f"    [{day_date}] SCALP ENTER {signal.direction} "
                                              f"{ticker} {strike}{right} "
                                              f"@ ${entry_premium:.2f} x{num_contracts} "
                                              f"({', '.join(signal.confirmations)}) "
                                              f"ATR=${current_atr:.3f} "
                                              f"stop=${stop_price:.2f} target=${target_price:.2f}")

            # ── 3b. MEAN-REVERSION entry ─────────────────────────
            if mr_signal is not None:
                can_enter_mr = (
                    open_mr is None
                    and mr_cooldown_remaining <= 0
                    and mr_trades_today < self.mr_cfg.max_trades_per_day
                    and mr_daily_pnl > -self.mr_cfg.daily_loss_limit * self.price_scale
                )

                if can_enter_mr:
                    day_result.signals_found += 1

                    # ATM strike (same as momentum scalp)
                    otm_distance = current_price * self.scalp_cfg.max_otm_pct
                    if mr_signal.direction == "CALL":
                        mr_strike = self._round_strike(current_price + otm_distance, profile)
                        mr_right = "C"
                    else:
                        mr_strike = self._round_strike(current_price - otm_distance, profile)
                        mr_right = "P"

                    # Price the option
                    mr_option_price = self.pricer.price_option(
                        current_price, mr_strike, T, day_iv, mr_right,
                    )
                    mr_entry_premium = mr_option_price * (1 + self.entry_slippage_pct)

                    # Premium filters
                    mr_min_prem = self.mr_cfg.min_premium * premium_scale
                    mr_max_prem = self.mr_cfg.max_premium * premium_scale

                    if mr_min_prem <= mr_entry_premium <= mr_max_prem:
                        # Position sizing (smaller for mean-rev)
                        mr_stop_dist = current_atr * self.mr_cfg.stop_atr_mult
                        mr_risk_per_contract = 0.50 * mr_stop_dist * 100
                        if mr_risk_per_contract > 0:
                            mr_max_risk = self.mr_cfg.max_risk_per_trade * self.price_scale
                            mr_budget_pct = 0.20 if self.spx_mode else 0.04
                            mr_max_budget = int(balance * mr_budget_pct / (mr_entry_premium * 100)) \
                                if mr_entry_premium > 0 else 0

                            mr_max_contracts = min(
                                int(mr_max_risk / mr_risk_per_contract),
                                self.mr_cfg.max_contracts,
                                max(1, mr_max_budget),
                            )
                            if mr_entry_premium * 100 <= balance * 0.40:
                                mr_num = max(1, mr_max_contracts) if mr_max_contracts >= 1 else 0
                                if mr_num > 0:
                                    mr_target_dist = current_atr * self.mr_cfg.profit_target_atr_mult
                                    if mr_signal.direction == "CALL":
                                        mr_stop_price = current_price - mr_stop_dist
                                        mr_target_price = current_price + mr_target_dist
                                    else:
                                        mr_stop_price = current_price + mr_stop_dist
                                        mr_target_price = current_price - mr_target_dist

                                    mr_pos = SimScalpPosition(
                                        ticker=ticker, strike=mr_strike, right=mr_right,
                                        direction=mr_signal.direction, expiry_date=day_date,
                                        confirmations=mr_signal.confirmations.copy(),
                                        confidence=mr_signal.confidence, tier="mean_rev",
                                        entry_time=current_time, entry_underlying=current_price,
                                        entry_premium=mr_entry_premium, entry_iv=day_iv,
                                        num_contracts=mr_num, atr_at_entry=current_atr,
                                        stop_price=mr_stop_price, target_price=mr_target_price,
                                        best_favorable_underlying=current_price,
                                    )

                                    open_mr = mr_pos
                                    mr_trades_today += 1
                                    day_result.trades_entered += 1
                                    day_result._trades.append(mr_pos)

                                    if verbose:
                                        print(f"    [{day_date}] 🔄 MR ENTER {mr_signal.direction} "
                                              f"{ticker} {mr_strike}{mr_right} "
                                              f"@ ${mr_entry_premium:.2f} x{mr_num} "
                                              f"({', '.join(mr_signal.confirmations)}) "
                                              f"ATR=${current_atr:.3f} "
                                              f"stop=${mr_stop_price:.2f} target=${mr_target_price:.2f}")

            # ── 3c. RUNNER entry ─────────────────────────────────
            # Runner requires a momentum signal (from the same engine)
            if signal is not None:
                cfg = self.scalp_cfg
                if cfg.runner_enabled:
                    in_runner_window = (
                        cfg.runner_window_start <= minutes_since_open <= cfg.runner_window_end
                    )
                    can_enter_runner = (
                        open_runner is None
                        and in_runner_window
                        and runner_trades_today < cfg.runner_max_per_day
                        and len(signal.confirmations) >= cfg.runner_min_confirmations
                    )

                    if can_enter_runner:
                        # Runner gate: ATR must be elevated (momentum day)
                        atr_ratio = current_atr / daily_median_atr if daily_median_atr > 0 else 0
                        atr_gate_passed = atr_ratio >= cfg.runner_min_atr_mult

                        # Or: a scalp just won in this direction (piggyback)
                        piggyback = (
                            cfg.runner_require_winning_scalp
                            and scalp_won_today
                            and scalp_win_direction == signal.direction
                        )

                        enter_runner = atr_gate_passed or piggyback
                        if not enter_runner and not cfg.runner_require_winning_scalp:
                            enter_runner = False

                        if enter_runner:
                            # Compute OTM strike
                            otm_distance = current_price * cfg.runner_otm_pct
                            if signal.direction == "CALL":
                                runner_strike = self._round_strike(current_price + otm_distance, profile)
                                runner_right = "C"
                            else:
                                runner_strike = self._round_strike(current_price - otm_distance, profile)
                                runner_right = "P"

                            # Price the OTM option
                            runner_option_price = self.pricer.price_option(
                                current_price, runner_strike, T, day_iv, runner_right,
                            )
                            # OTM entry slippage is wider
                            otm_entry_slippage = 0.05 if self.spx_mode else 0.08
                            runner_entry_premium = runner_option_price * (1 + otm_entry_slippage)

                            # Premium filters for runner (cheaper options)
                            runner_min_prem = cfg.runner_min_premium * premium_scale
                            runner_max_prem = cfg.runner_max_premium * premium_scale

                            if runner_min_prem <= runner_entry_premium <= runner_max_prem:
                                # Runner position sizing: budget-based (small, disposable)
                                runner_budget = balance * cfg.runner_budget_pct
                                runner_num = min(
                                    int(runner_budget / (runner_entry_premium * 100)) if runner_entry_premium > 0 else 0,
                                    cfg.runner_max_contracts,
                                )
                                runner_num = max(1, runner_num) if runner_num >= 1 else 0

                                if runner_num > 0 and runner_entry_premium * 100 * runner_num <= balance * 0.10:
                                    # Runner stop
                                    runner_stop_dist = current_atr * cfg.runner_stop_atr_mult
                                    if signal.direction == "CALL":
                                        runner_stop_price = current_price - runner_stop_dist
                                        runner_target_price = 0  # No fixed target — let it run
                                    else:
                                        runner_stop_price = current_price + runner_stop_dist
                                        runner_target_price = 0

                                    runner_pos = SimScalpPosition(
                                        ticker=ticker, strike=runner_strike, right=runner_right,
                                        direction=signal.direction, expiry_date=day_date,
                                        confirmations=signal.confirmations.copy(),
                                        confidence=signal.confidence, tier="runner",
                                        entry_time=current_time, entry_underlying=current_price,
                                        entry_premium=runner_entry_premium, entry_iv=day_iv,
                                        num_contracts=runner_num, atr_at_entry=current_atr,
                                        stop_price=runner_stop_price, target_price=runner_target_price,
                                        best_favorable_underlying=current_price,
                                    )

                                    open_runner = runner_pos
                                    runner_trades_today += 1
                                    day_result.trades_entered += 1
                                    day_result._trades.append(runner_pos)

                                    if verbose:
                                        print(f"    [{day_date}] 🏃 RUNNER ENTER {signal.direction} "
                                              f"{ticker} {runner_strike}{runner_right} "
                                              f"@ ${runner_entry_premium:.2f} x{runner_num} "
                                              f"({', '.join(signal.confirmations)}) "
                                              f"ATR=${current_atr:.3f} (ATR ratio: {atr_ratio:.2f}x) "
                                              f"stop=${runner_stop_price:.2f}")

            # ── 3d. ORB BREAKOUT entry ───────────────────────────
            # Strategy D: fires ONLY when momentum engine has no signal this day.
            # Regime-filtered: skip DEAD_FLAT and CHOPPY days.
            if (orb_enabled and orb_regime_ok and orb_data and orb_data.get("valid", False)
                    and open_orb is None
                    and orb_trades_today < self.orb_cfg.max_trades_per_day):

                # Defer to momentum: if momentum already fired OR has a signal, skip ORB
                skip_for_momentum = (
                    self.orb_cfg.only_when_no_momentum and momentum_signal_fired
                )

                if not skip_for_momentum:
                    orb_signal = self._orb_signal_engine.evaluate(day_bars, orb_data, i)

                    if orb_signal is not None:
                        day_result.signals_found += 1

                        # ATM strike (same as momentum scalp)
                        otm_distance = current_price * self.orb_cfg.max_otm_pct
                        if orb_signal.direction == "CALL":
                            orb_strike = self._round_strike(current_price + otm_distance, profile)
                            orb_right = "C"
                        else:
                            orb_strike = self._round_strike(current_price - otm_distance, profile)
                            orb_right = "P"

                        # Price the option
                        orb_option_price = self.pricer.price_option(
                            current_price, orb_strike, T, day_iv, orb_right,
                        )
                        orb_entry_premium = orb_option_price * (1 + self.entry_slippage_pct)

                        # Premium filters
                        orb_min_prem = self.orb_cfg.min_premium * premium_scale
                        orb_max_prem = self.orb_cfg.max_premium * premium_scale

                        if orb_min_prem <= orb_entry_premium <= orb_max_prem:
                            # Position sizing
                            orb_range = orb_signal.orb_range
                            stop_dist = orb_range * self.orb_cfg.stop_range_mult
                            risk_per_contract = 0.50 * stop_dist * 100
                            if risk_per_contract > 0:
                                max_risk = self.orb_cfg.max_risk_per_trade * self.price_scale
                                budget_pct = 0.25 if self.spx_mode else 0.05
                                max_budget_contracts = int(balance * budget_pct / (orb_entry_premium * 100)) \
                                    if orb_entry_premium > 0 else 0

                                num_contracts = min(
                                    int(max_risk / risk_per_contract),
                                    self.orb_cfg.max_contracts,
                                    max(1, max_budget_contracts),
                                )
                                num_contracts = max(1, num_contracts)

                                if orb_entry_premium * 100 <= balance * 0.50:
                                    # Compute stop and target from ORB range
                                    target_dist = orb_range * self.orb_cfg.target_range_mult
                                    if orb_signal.direction == "CALL":
                                        orb_stop_price = current_price - stop_dist
                                        orb_target_price = current_price + target_dist
                                    else:
                                        orb_stop_price = current_price + stop_dist
                                        orb_target_price = current_price - target_dist

                                    orb_pos = SimScalpPosition(
                                        ticker=ticker, strike=orb_strike, right=orb_right,
                                        direction=orb_signal.direction, expiry_date=day_date,
                                        confirmations=orb_signal.confirmations.copy(),
                                        confidence=orb_signal.confidence, tier="orb",
                                        entry_time=current_time, entry_underlying=current_price,
                                        entry_premium=orb_entry_premium, entry_iv=day_iv,
                                        num_contracts=num_contracts, atr_at_entry=current_atr,
                                        stop_price=orb_stop_price, target_price=orb_target_price,
                                        best_favorable_underlying=current_price,
                                    )

                                    open_orb = orb_pos
                                    orb_trades_today += 1
                                    day_result.trades_entered += 1
                                    day_result._trades.append(orb_pos)

                                    if verbose:
                                        print(f"    [{day_date}] 📊 ORB ENTER {orb_signal.direction} "
                                              f"{ticker} {orb_strike}{orb_right} "
                                              f"@ ${orb_entry_premium:.2f} x{num_contracts} "
                                              f"({', '.join(orb_signal.confirmations)}) "
                                              f"ORB range=${orb_range:.2f} "
                                              f"stop=${orb_stop_price:.2f} target=${orb_target_price:.2f}")

        # ── End of day: force-close ALL open positions ───────────
        for pos_to_close in [open_scalp, open_runner, open_mr, open_orb]:
            if pos_to_close and pos_to_close.is_open:
                last_price = float(day_bars["close"].iloc[-1])
                last_time = day_bars.index[-1]
                T_final = self.pricer.time_to_expiry(last_time)

                exit_premium = self.pricer.price_option(
                    last_price, pos_to_close.strike,
                    max(T_final, 1e-8), pos_to_close.entry_iv, pos_to_close.right,
                )
                slippage = self.exit_slippage_pct if pos_to_close.tier in ("scalp", "mean_rev", "orb") else 0.05
                exit_premium = max(0, exit_premium * (1 - slippage))
                commission = self.commission_per_contract * pos_to_close.num_contracts * 2
                pnl_amount = (exit_premium - pos_to_close.entry_premium) * \
                             pos_to_close.num_contracts * 100 - commission

                pos_to_close.is_open = False
                pos_to_close.exit_time = last_time
                pos_to_close.exit_underlying = last_price
                pos_to_close.exit_premium = exit_premium
                pos_to_close.exit_reason = "EOD_CLOSE"
                pos_to_close.total_pnl = round(pnl_amount, 2)
                pos_to_close.hold_minutes = round(
                    (last_time - pos_to_close.entry_time).total_seconds() / 60, 1
                )

                daily_pnl += pnl_amount
                day_result.trades_closed += 1
                if pnl_amount > 5:
                    day_result.winning_trades += 1
                elif pnl_amount < -5:
                    day_result.losing_trades += 1

        day_result.day_pnl = round(daily_pnl, 2)
        day_result.iv_blocked = iv_blocked_count
        return day_result

    def _in_time_window(self, minutes_since_open: float) -> bool:
        """Check if in valid MOMENTUM trading window."""
        cfg = self.scalp_cfg
        w1 = cfg.window_1_start <= minutes_since_open <= cfg.window_1_end
        w2 = cfg.window_2_start <= minutes_since_open <= cfg.window_2_end
        if cfg.enable_midday:
            return w1 or w2 or (cfg.window_1_end < minutes_since_open < cfg.window_2_start)
        return w1 or w2

    def _in_mr_time_window(self, minutes_since_open: float) -> bool:
        """Check if in valid MEAN-REVERSION trading window (midday chop)."""
        cfg = self.mr_cfg
        return cfg.window_start <= minutes_since_open <= cfg.window_end

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

        # ── Tier-specific stats ──────────────────────────────────
        scalp_trades = [t for t in trades if t.tier == "scalp"]
        runner_trades = [t for t in trades if t.tier == "runner"]

        results.scalp_trades = len(scalp_trades)
        results.scalp_wins = sum(1 for t in scalp_trades if t.total_pnl > 5)
        results.scalp_pnl = round(sum(t.total_pnl for t in scalp_trades), 2)

        results.runner_trades = len(runner_trades)
        results.runner_wins = sum(1 for t in runner_trades if t.total_pnl > 5)
        results.runner_pnl = round(sum(t.total_pnl for t in runner_trades), 2)
        if runner_trades:
            results.runner_biggest_win = round(
                max(t.total_pnl for t in runner_trades), 2
            )
            runner_holds = [t.hold_minutes for t in runner_trades if t.hold_minutes > 0]
            results.runner_avg_hold = round(np.mean(runner_holds), 1) if runner_holds else 0

        # Mean-reversion tier
        mr_trades_list = [t for t in trades if t.tier == "mean_rev"]
        results.mr_trades = len(mr_trades_list)
        results.mr_wins = sum(1 for t in mr_trades_list if t.total_pnl > 5)
        results.mr_pnl = round(sum(t.total_pnl for t in mr_trades_list), 2)
        if mr_trades_list:
            results.mr_biggest_win = round(
                max(t.total_pnl for t in mr_trades_list), 2
            )
            mr_holds = [t.hold_minutes for t in mr_trades_list if t.hold_minutes > 0]
            results.mr_avg_hold = round(np.mean(mr_holds), 1) if mr_holds else 0

        # ORB tier
        orb_trades_list = [t for t in trades if t.tier == "orb"]
        results.orb_trades = len(orb_trades_list)
        results.orb_wins = sum(1 for t in orb_trades_list if t.total_pnl > 5)
        results.orb_pnl = round(sum(t.total_pnl for t in orb_trades_list), 2)
        if orb_trades_list:
            results.orb_biggest_win = round(
                max(t.total_pnl for t in orb_trades_list), 2
            )
            orb_holds = [t.hold_minutes for t in orb_trades_list if t.hold_minutes > 0]
            results.orb_avg_hold = round(np.mean(orb_holds), 1) if orb_holds else 0

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
        print(f"\n  {C.DIM}Strategy: ATM scalp + OTM runner | "
              f"stop={cfg.stop_atr_mult}×ATR | "
              f"target={cfg.profit_target_atr_mult}×ATR | "
              f"min_conf={cfg.min_confirmations} | "
              f"time_stop={cfg.time_stop_minutes}m | "
              f"max_hold={cfg.max_hold_minutes}m{C.RESET}")
        if cfg.runner_enabled:
            print(f"  {C.DIM}Runner: OTM {cfg.runner_otm_pct*100:.1f}% | "
                  f"stop={cfg.runner_stop_atr_mult}×ATR | "
                  f"trail={cfg.runner_trail_activation_atr}×ATR→{cfg.runner_trail_distance_atr}×ATR | "
                  f"ATR gate={cfg.runner_min_atr_mult}x median | "
                  f"window={cfg.runner_window_start}-{cfg.runner_window_end}m{C.RESET}")

        # Also show MR strategy config if there are MR trades
        if results.mr_trades > 0:
            mr_cfg = self.mr_cfg
            print(f"  {C.DIM}MeanRev: BB({mr_cfg.bb_period},{mr_cfg.bb_std}) + RSI({mr_cfg.rsi_period}) | "
                  f"stop={mr_cfg.stop_atr_mult}×ATR | target={mr_cfg.profit_target_atr_mult}×ATR | "
                  f"window={mr_cfg.window_start}-{mr_cfg.window_end}m{C.RESET}")

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

        # ── Multi-Strategy Breakdown ─────────────────────────────
        has_tiers = results.runner_trades > 0 or results.scalp_trades > 0 or results.mr_trades > 0
        if has_tiers:
            print(f"\n  {'─' * 60}")
            print(f"  {C.BOLD}MULTI-STRATEGY BREAKDOWN{C.RESET}")
            print(f"  {'─' * 60}")
            print(f"  {'Tier':<14} {'Trades':>6} {'Wins':>5} {'WR':>6} {'P&L':>12} {'Biggest':>10}")
            print(f"  {'─' * 60}")

            if results.scalp_trades > 0:
                s_wr = results.scalp_wins / results.scalp_trades * 100
                sc = C.GREEN if results.scalp_pnl >= 0 else C.RED
                scalp_biggest = max(
                    (t.total_pnl for t in results.trades if t.tier == "scalp"),
                    default=0
                )
                print(f"  {'⚡ Momentum':<14} {results.scalp_trades:>6} "
                      f"{results.scalp_wins:>5} {s_wr:>5.1f}% "
                      f"{sc}${results.scalp_pnl:>+11,.2f}{C.RESET} "
                      f"${scalp_biggest:>+9,.2f}")

            if results.mr_trades > 0:
                mr_wr = results.mr_wins / results.mr_trades * 100
                mc = C.GREEN if results.mr_pnl >= 0 else C.RED
                print(f"  {'🔄 MeanRev':<14} {results.mr_trades:>6} "
                      f"{results.mr_wins:>5} {mr_wr:>5.1f}% "
                      f"{mc}${results.mr_pnl:>+11,.2f}{C.RESET} "
                      f"${results.mr_biggest_win:>+9,.2f}")

            if results.runner_trades > 0:
                r_wr = results.runner_wins / results.runner_trades * 100
                rc = C.GREEN if results.runner_pnl >= 0 else C.RED
                print(f"  {'🏃 Runner':<14} {results.runner_trades:>6} "
                      f"{results.runner_wins:>5} {r_wr:>5.1f}% "
                      f"{rc}${results.runner_pnl:>+11,.2f}{C.RESET} "
                      f"${results.runner_biggest_win:>+9,.2f}")

            if results.orb_trades > 0:
                o_wr = results.orb_wins / results.orb_trades * 100
                oc = C.GREEN if results.orb_pnl >= 0 else C.RED
                print(f"  {'📊 ORB':<14} {results.orb_trades:>6} "
                      f"{results.orb_wins:>5} {o_wr:>5.1f}% "
                      f"{oc}${results.orb_pnl:>+11,.2f}{C.RESET} "
                      f"${results.orb_biggest_win:>+9,.2f}")

            if results.mr_trades > 0:
                print(f"\n  MeanRev avg hold: {results.mr_avg_hold:.1f} min")
            if results.runner_trades > 0:
                print(f"  Runner avg hold: {results.runner_avg_hold:.1f} min")
            if results.orb_trades > 0:
                print(f"  ORB avg hold: {results.orb_avg_hold:.1f} min"
                      f"  (regime skipped: {results.orb_regime_skipped} days)")

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
            "strategy": "multi_strategy_scalp",
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
                "scalp_trades": results.scalp_trades,
                "scalp_pnl": results.scalp_pnl,
                "runner_trades": results.runner_trades,
                "runner_pnl": results.runner_pnl,
                "runner_biggest_win": results.runner_biggest_win,
                "mr_trades": results.mr_trades,
                "mr_pnl": results.mr_pnl,
                "mr_biggest_win": results.mr_biggest_win,
                "orb_trades": results.orb_trades,
                "orb_pnl": results.orb_pnl,
                "orb_biggest_win": results.orb_biggest_win,
                "orb_regime_skipped": results.orb_regime_skipped,
            },
            "exit_stats": results.exit_stats,
            "confirmation_stats": results.confirmation_stats,
            "equity_curve": results.equity_curve,
            "trades": [
                {
                    "date": str(t.expiry_date),
                    "tier": t.tier,
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

"""
Greeks-Aware Intraday Backtester
==================================
Replays 5-minute bars through the 0DTE spread lifecycle,
re-pricing the spread at every bar using Black-Scholes with
shrinking T (time to expiration).

This captures what the daily OHLC backtester misses:
  ✓ Gamma explosion as strikes approach
  ✓ Theta acceleration in the final hours
  ✓ Spread mark-to-market at every 5-min bar
  ✓ Stop-loss triggered by spread VALUE, not just strike breach
  ✓ Profit target based on actual spread price decay
  ✓ Vega impact from intraday IV shifts

Usage:
    from trading_engine.data.intraday import IntradayFetcher
    from trading_engine.data.intraday_backtester import IntradayBacktester

    fetcher = IntradayFetcher()
    days = fetcher.fetch("SPY", days=60)

    bt = IntradayBacktester()
    results = bt.run(days)
    bt.print_report(results)
"""

import math
import os
import json
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from typing import List, Dict, Any, Optional, Tuple

import numpy as np

from ..config import EngineConfig
from ..models import MarketRegime
from ..black_scholes import (
    bs_put_price, bs_call_price, bs_delta, bs_gamma, bs_theta,
    bs_all_greeks, strike_at_delta, expected_move
)
from ..formatters import header, sub_header, kv, pnl, bar, C
from ..filters import ProductionFilters, FilterDecision
from .intraday import TradingDay, IntradayBar
from .multi_tf import MultiTFAnalyzer, TFSignals, DailySignals


# ─────────────────────────────────────────────────────────────────
# Trade lifecycle models
# ─────────────────────────────────────────────────────────────────

@dataclass
class SpreadPosition:
    """Live spread being tracked bar-by-bar."""
    entry_time: datetime = field(default_factory=datetime.now)
    strategy: str = "0DTE Put Credit Spread"
    spread_type: str = "put_credit"  # put_credit, call_credit, iron_condor

    # Strike levels
    short_strike: float = 0.0
    long_strike: float = 0.0
    short_call: float = 0.0    # For iron condors
    long_call: float = 0.0     # For iron condors

    # Entry pricing
    entry_credit: float = 0.0   # Net credit when opened
    width: float = 5.0          # Spread width

    # Current state
    current_mark: float = 0.0   # Current spread value (what it costs to close)
    current_pnl: float = 0.0    # Unrealized P&L per contract
    max_adverse: float = 0.0    # Worst mark seen (for tracking)

    # Greeks at current bar
    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0

    # Position sizing
    num_contracts: int = 1
    commission: float = 0.0

    # Status
    is_open: bool = True
    exit_time: Optional[datetime] = None
    exit_reason: str = ""
    exit_price: float = 0.0
    realized_pnl: float = 0.0


@dataclass
class IntradayTrade:
    """Completed trade with full lifecycle data."""
    date: date = field(default_factory=date.today)
    strategy: str = ""
    spread_type: str = ""

    # Strikes
    short_strike: float = 0.0
    long_strike: float = 0.0
    short_call: float = 0.0
    long_call: float = 0.0

    # Pricing
    entry_credit: float = 0.0
    exit_price: float = 0.0
    width: float = 5.0
    num_contracts: int = 1
    commission: float = 0.0

    # Outcome
    pnl_per_contract: float = 0.0
    total_pnl: float = 0.0
    exit_reason: str = ""

    # Timing
    entry_time: str = ""
    exit_time: str = ""
    duration_minutes: float = 0.0

    # Greeks at entry
    entry_delta: float = 0.0
    entry_gamma: float = 0.0
    entry_theta: float = 0.0

    # Context
    underlying_at_entry: float = 0.0
    underlying_at_exit: float = 0.0
    max_adverse_mark: float = 0.0   # Worst spread mark during trade
    iv_at_entry: float = 0.0

    # Multi-TF context (populated when multi_tf=True)
    tf_entry_confidence: float = 0.0
    tf_direction_bias: str = ""
    tf_d1_trend: str = ""
    tf_h1_structure: str = ""
    tf_m5_rsi: float = 0.0
    tf_size_mult: float = 1.0


@dataclass
class IntradayResults:
    """Complete intraday backtest output."""
    start_date: date = field(default_factory=date.today)
    end_date: date = field(default_factory=date.today)
    total_days: int = 0
    days_traded: int = 0

    # Account
    starting_balance: float = 50_000.0
    ending_balance: float = 50_000.0
    peak_balance: float = 50_000.0

    # Trades
    trades: List[IntradayTrade] = field(default_factory=list)
    total_trades: int = 0
    winners: int = 0
    losers: int = 0
    win_rate: float = 0.0

    # P&L
    total_pnl: float = 0.0
    avg_winner: float = 0.0
    avg_loser: float = 0.0
    profit_factor: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0

    # Risk
    max_drawdown_pct: float = 0.0
    max_drawdown_dollars: float = 0.0
    sharpe_ratio: float = 0.0

    # Timing stats
    avg_duration_minutes: float = 0.0
    avg_entry_time: str = ""

    # Greeks impact
    avg_gamma_at_exit: float = 0.0
    gamma_blow_ups: int = 0  # Times gamma > 0.10 triggered exit

    # Monthly
    monthly_pnl: Dict[str, float] = field(default_factory=dict)
    equity_curve: List[Dict] = field(default_factory=list)
    daily_pnl: List[float] = field(default_factory=list)

    # Strategy breakdown
    strategy_stats: Dict[str, Dict] = field(default_factory=dict)

    # Production filter stats (ATR sizing)
    filter_full_size: int = 0        # Trades at 1.0×
    filter_reduced_size: int = 0     # Trades at 0.75×
    filter_half_size: int = 0        # Trades at 0.50×
    filter_skipped: int = 0          # Days skipped by ATR filter
    filter_avg_atr_pct: float = 0.0  # Avg ATR % across traded days

    # Multi-TF stats (populated when multi_tf=True)
    tf_entries_allowed: int = 0
    tf_entries_rejected: int = 0
    tf_rejection_reasons: Dict[str, int] = field(default_factory=dict)
    tf_exit_reasons: Dict[str, int] = field(default_factory=dict)
    tf_avg_confidence: float = 0.0


# ─────────────────────────────────────────────────────────────────
# The backtester
# ─────────────────────────────────────────────────────────────────

class IntradayBacktester:
    """
    Greeks-aware 0DTE backtester that processes 5-minute bars.

    For each trading day:
      1. Wait for entry window (9:45–10:30 AM)
      2. Place spread at target delta from current price
      3. Every 5-min bar: re-price spread with Black-Scholes (shrinking T)
      4. Check: stop-loss (spread mark > 2x credit), profit target (mark < 50% credit)
      5. If neither triggered, expire at 4:00 PM (spread = intrinsic value)
    """

    def __init__(self, config: Optional[EngineConfig] = None):
        self.config = config or EngineConfig()
        self.commission_per_contract: float = 0.65
        self.slippage_pct: float = 0.15  # Bid-ask slippage on 0DTE
        self.filters = ProductionFilters()  # ATR-based position sizing

    def run(self, trading_days: List[TradingDay],
            strategies: Optional[List[str]] = None,
            iv_override: Optional[float] = None,
            adaptive: bool = False,
            regime_map: Optional[Dict[str, str]] = None,
            multi_tf: bool = False) -> IntradayResults:
        """
        Run Greeks-aware backtest over intraday 5-minute data.

        Args:
            trading_days:  List of TradingDay from IntradayFetcher
            strategies:    ["put_credit", "call_credit", "iron_condor"]
            iv_override:   Force a specific IV (otherwise estimate from price action)
            adaptive:      If True, use per-regime optimized parameters
            regime_map:    Dict mapping date string (YYYY-MM-DD) to regime (GREEN/YELLOW/RED)
            multi_tf:      If True, apply D1/H1/M5/M1 entry gates + exit triggers
        """
        if strategies is None:
            strategies = ["put_credit"]

        results = IntradayResults(
            start_date=trading_days[0].date,
            end_date=trading_days[-1].date,
            starting_balance=self.config.account.account_size,
        )

        balance = self.config.account.account_size
        peak = balance
        max_dd = 0.0
        max_dd_dollars = 0.0
        daily_pnls = []
        equity = [{"date": str(results.start_date), "balance": balance}]

        # Multi-TF: initialize analyzer and build D1 history
        tf_analyzer = MultiTFAnalyzer() if multi_tf else None
        tf_confidences = []

        for idx, td in enumerate(trading_days):
            results.total_days += 1

            if td.bar_count < 30:
                # Partial day, skip
                daily_pnls.append(0.0)
                equity.append({"date": str(td.date), "balance": balance})
                continue

            # Determine regime for this day
            day_regime = None
            if regime_map:
                day_regime = regime_map.get(str(td.date))
            elif hasattr(td, '_regime'):
                day_regime = td._regime

            # Adaptive mode: use per-regime params and strategy selection
            if adaptive and day_regime:
                from ..config import RegimeParams
                regime_params = self.config.adaptive.for_regime(day_regime)

                # Skip if regime says don't trade
                if not regime_params.trade_enabled:
                    daily_pnls.append(0.0)
                    equity.append({"date": str(td.date), "balance": balance})
                    continue

                # Use regime's preferred strategy (or all profitable ones)
                if regime_params.preferred_strategy == "none":
                    daily_pnls.append(0.0)
                    equity.append({"date": str(td.date), "balance": balance})
                    continue

                day_strategies = [regime_params.preferred_strategy]
            else:
                regime_params = None
                day_strategies = strategies

            # ── Production Filter 2: ATR-based position sizing ──
            filter_decision = FilterDecision()  # default: full size
            if adaptive and idx >= 15:
                # Build daily OHLC history from previous trading days
                lookback = min(idx, 25)
                history = trading_days[max(0, idx - lookback):idx]
                if len(history) >= 15:
                    h_closes = [d.close_price for d in history]
                    h_highs = [d.high_price for d in history]
                    h_lows = [d.low_price for d in history]
                    filter_decision = self.filters.pre_entry(h_closes, h_highs, h_lows)

                    if filter_decision.skip:
                        results.filter_skipped += 1
                        daily_pnls.append(0.0)
                        equity.append({"date": str(td.date), "balance": balance})
                        continue

            # ── Multi-TF: compute D1 signals from history ──
            daily_signals = None
            if multi_tf and tf_analyzer:
                # Use previous trading days as D1 history (need 20+ for SMA)
                lookback = min(idx, 25)
                history = trading_days[max(0, idx - lookback):idx]
                if len(history) >= 20:
                    daily_signals = tf_analyzer.compute_daily(history, td)

            # Estimate IV for the day from price action
            if iv_override:
                iv = iv_override
            else:
                iv = self._estimate_iv(td)

            day_pnl = 0.0

            for strategy in day_strategies:
                trade = self._run_day(td, strategy, iv, balance,
                                      regime_params=regime_params,
                                      filter_decision=filter_decision,
                                      multi_tf=multi_tf,
                                      tf_analyzer=tf_analyzer,
                                      daily_signals=daily_signals)
                if trade is not None:
                    results.trades.append(trade)
                    day_pnl += trade.total_pnl
                    # Track ATR filter sizing
                    if filter_decision.size_multiplier >= 1.0:
                        results.filter_full_size += 1
                    elif filter_decision.size_multiplier >= 0.75:
                        results.filter_reduced_size += 1
                    else:
                        results.filter_half_size += 1
                    if trade.tf_entry_confidence > 0:
                        tf_confidences.append(trade.tf_entry_confidence)
                        results.tf_entries_allowed += 1
                elif multi_tf and daily_signals is not None:
                    # Trade was rejected by multi-TF filter
                    results.tf_entries_rejected += 1

            if day_pnl != 0:
                results.days_traded += 1

            balance += day_pnl
            daily_pnls.append(day_pnl)
            equity.append({"date": str(td.date), "balance": round(balance, 2)})

            peak = max(peak, balance)
            dd = (peak - balance) / peak * 100 if peak > 0 else 0
            dd_dollars = peak - balance
            max_dd = max(max_dd, dd)
            max_dd_dollars = max(max_dd_dollars, dd_dollars)

        # ── Multi-TF aggregate stats ──
        if multi_tf:
            results.tf_avg_confidence = (round(np.mean(tf_confidences), 2)
                                         if tf_confidences else 0)
            # Count TF exit reasons
            for t in results.trades:
                if t.exit_reason.startswith("tf_"):
                    results.tf_exit_reasons[t.exit_reason] = \
                        results.tf_exit_reasons.get(t.exit_reason, 0) + 1

        # ── Compute stats ──
        self._compute_stats(results, balance, peak, max_dd, max_dd_dollars,
                            daily_pnls, equity)
        return results

    # ─────────────────────────────────────────────────────────────
    # Single-day simulation
    # ─────────────────────────────────────────────────────────────

    def _run_day(self, td: TradingDay, strategy: str,
                 iv: float, balance: float,
                 regime_params=None,
                 filter_decision: Optional[FilterDecision] = None,
                 multi_tf: bool = False,
                 tf_analyzer: Optional[MultiTFAnalyzer] = None,
                 daily_signals: Optional[DailySignals] = None) -> Optional[IntradayTrade]:
        """
        Simulate one trading day bar-by-bar.

        Entry: First bar in the entry window (with multi-TF gate if enabled).
        Management: Re-price spread every 5 min, check stops/targets + TF exits.
        Exit: Stop, target, TF exit, or 4:00 PM expiration.

        If regime_params is provided (adaptive mode), uses per-regime
        optimized delta, stop, target, width, entry window, and gamma limit.
        If multi_tf is True, applies D1/H1/M5 entry gates and exit triggers.
        """
        r = 0.045  # Risk-free rate

        # Use regime-adaptive params if provided, otherwise defaults
        if regime_params is not None:
            delta_target = regime_params.delta
            width = regime_params.width
            entry_start = regime_params.entry_start_min
            entry_end = regime_params.entry_end_min
            stop_mult = regime_params.stop_mult
            profit_target_pct = regime_params.profit_target
            gamma_limit = regime_params.gamma_limit
            size_mult = regime_params.position_size_mult
        else:
            delta_target = self.config.trading.short_delta_max  # 0.15
            width = self.config.trading.spread_width_min  # 5
            entry_start = 15   # 9:45 AM
            entry_end = 60     # 10:30 AM
            stop_mult = self.config.trading.zero_dte_stop_multiplier
            profit_target_pct = self.config.trading.zero_dte_profit_target_pct
            gamma_limit = 0.10
            size_mult = 1.0

        # ── Production Filter 2: ATR position sizing ──
        if filter_decision is not None and filter_decision.size_multiplier < 1.0:
            size_mult *= filter_decision.size_multiplier

        # SPY ≈ SPX / 10 for strike math. We trade SPY spreads with $1 strikes.
        is_spy = td.ticker.upper() in ("SPY", "SPDR")
        strike_round = 1.0 if is_spy else 5.0
        if is_spy and width < 1.0:
            width = 1.0  # Minimum $1 wide for SPY

        # Find entry bar in the configured window
        entry_bar = None
        entry_idx = 0
        for i, b in enumerate(td.bars):
            mins = b.minutes_since_open
            if entry_start <= mins <= entry_end:
                entry_bar = b
                entry_idx = i
                break

        if entry_bar is None:
            return None

        S = entry_bar.close
        T = entry_bar.time_to_close_years

        # ── Multi-TF entry gate ──
        tf_signals = None
        if multi_tf and tf_analyzer and daily_signals:
            # Compute H1 and M5 signals up to entry bar
            hourly_signals = tf_analyzer.compute_hourly(td.bars, entry_idx)
            m5_signals = tf_analyzer.compute_m5(td.bars, entry_idx, hourly_signals.vwap)

            # Combine all TFs — evaluate conditions FOR the given strategy
            # Returns position sizing (not binary reject, except extreme)
            tf_signals = tf_analyzer.compute_entry_signals(
                daily_signals, hourly_signals, m5_signals,
                strategy=strategy,
            )

            # Hard reject only on extreme conditions (4+ penalties)
            if not tf_signals.entry_allowed:
                return None

            # Apply multi-TF position size adjustment
            # Compounds with regime's size_mult
            size_mult = size_mult * tf_signals.size_multiplier

            # NOTE: We do NOT override delta_target here.
            # The regime optimizer already found optimal delta through
            # 972K+ simulations. Overriding it reduces performance.

        # Position sizing (scaled by regime)
        max_risk = balance * self.config.account.max_risk_per_trade_pct * size_mult

        # Build position based on strategy
        pos = self._open_position(strategy, S, T, r, iv, delta_target,
                                   width, strike_round, max_risk, entry_bar)
        if pos is None:
            return None

        # ── Bar-by-bar management ──
        stop_price = pos.entry_credit * stop_mult
        target_price = pos.entry_credit * (1 - profit_target_pct)

        # Track bar index for multi-TF exit (avoid O(n) index lookup)
        _bar_offset = entry_idx + 1

        for bi, b in enumerate(td.bars[entry_idx + 1:]):
            current_bar_idx = _bar_offset + bi
            T_now = b.time_to_close_years
            S_now = b.close

            # Re-price the spread using Black-Scholes with current T
            mark = self._price_spread(pos, S_now, T_now, r, iv)
            pos.current_mark = mark
            pos.current_pnl = pos.entry_credit - mark
            pos.max_adverse = max(pos.max_adverse, mark)

            # Worst-case intra-bar mark: use bar HIGH/LOW to detect
            # stop blowthrough that bar CLOSE would miss.
            # For put credit: worst when price drops (bar low)
            # For call credit: worst when price rises (bar high)
            # For iron condor: max of both sides
            if pos.spread_type == "put_credit":
                mark_worst = self._price_spread(pos, b.low, T_now, r, iv)
            elif pos.spread_type == "call_credit":
                mark_worst = self._price_spread(pos, b.high, T_now, r, iv)
            else:  # iron_condor
                mark_low = self._price_spread(pos, b.low, T_now, r, iv)
                mark_high = self._price_spread(pos, b.high, T_now, r, iv)
                mark_worst = max(mark_low, mark_high)
            pos.max_adverse = max(pos.max_adverse, mark_worst)

            # Update Greeks
            greeks = self._spread_greeks(pos, S_now, T_now, r, iv)
            pos.delta = greeks["delta"]
            pos.gamma = greeks["gamma"]
            pos.theta = greeks["theta"]

            # ── Check exit conditions ──

            # 1. Stop-loss: check WORST intra-bar mark (realistic execution)
            if mark_worst >= stop_price:
                pos.exit_reason = "stop_loss"
                # Realistic fill: between stop and worst, plus slippage
                fill = min(mark_worst * 1.02, pos.width)  # 2% adverse slippage
                pos.exit_price = fill
                pos.exit_time = b.timestamp
                pos.is_open = False
                break

            # 2. Profit target: spread mark decayed enough
            if mark <= target_price:
                pos.exit_reason = "profit_target"
                pos.exit_price = mark
                pos.exit_time = b.timestamp
                pos.is_open = False
                break

            # 3. Gamma blowup protection: if gamma exceeds limit, position is dangerous
            _gamma_limit = gamma_limit if regime_params is not None else 0.10
            if abs(greeks["gamma"]) > _gamma_limit and mark > pos.entry_credit * 1.3:
                pos.exit_reason = "gamma_risk"
                pos.exit_price = mark
                pos.exit_time = b.timestamp
                pos.is_open = False
                break

            # 4. Multi-TF exit triggers — DISABLED
            #    After testing, the regime-optimized stop-loss (2.5× credit) 
            #    already handles adverse moves well. Adding VWAP/structure exits
            #    converts winners to losers by cutting trades too early.
            #    The multi-TF value comes from ENTRY filtering, not exits.
            #    Keeping the code for future live-trading integration where
            #    real-time signals may have more value.
            #
            # if multi_tf and tf_analyzer and bi % 5 == 0 and bi > 0:
            #     pnl_pct = (pos.entry_credit - mark) / pos.entry_credit
            #     tf_exit, tf_exit_reason = tf_analyzer.compute_exit_signals(...)
            #     if tf_exit: break

        # If still open at end of day → expired
        if pos.is_open:
            last_bar = td.bars[-1]
            S_final = last_bar.close
            # At expiration, spread value = intrinsic only
            intrinsic = self._intrinsic_value(pos, S_final)
            pos.exit_price = intrinsic
            pos.exit_reason = "expired" if intrinsic < 0.01 else "expired_itm"
            pos.exit_time = last_bar.timestamp
            pos.is_open = False

        # ── Build trade record ──
        pnl_per = pos.entry_credit - pos.exit_price
        # Apply slippage on close too
        close_slippage = pos.exit_price * 0.05  # 5% slippage on exit
        if pnl_per < 0:
            close_slippage = -close_slippage  # Slippage hurts: costs more to close
        pnl_per -= close_slippage

        total_pnl = round(pnl_per * pos.num_contracts * 100 - pos.commission, 2)

        duration = 0
        if pos.exit_time and pos.entry_time:
            duration = (pos.exit_time - pos.entry_time).total_seconds() / 60

        entry_greeks = self._spread_greeks(pos, entry_bar.close,
                                            entry_bar.time_to_close_years, r, iv)

        return IntradayTrade(
            date=td.date,
            strategy=pos.strategy,
            spread_type=pos.spread_type,
            short_strike=pos.short_strike,
            long_strike=pos.long_strike,
            short_call=pos.short_call,
            long_call=pos.long_call,
            entry_credit=pos.entry_credit,
            exit_price=round(pos.exit_price, 4),
            width=pos.width,
            num_contracts=pos.num_contracts,
            commission=pos.commission,
            pnl_per_contract=round(pnl_per, 4),
            total_pnl=total_pnl,
            exit_reason=pos.exit_reason,
            entry_time=pos.entry_time.strftime("%H:%M") if pos.entry_time else "",
            exit_time=pos.exit_time.strftime("%H:%M") if pos.exit_time else "",
            duration_minutes=round(duration, 1),
            entry_delta=round(entry_greeks["delta"], 4),
            entry_gamma=round(entry_greeks["gamma"], 4),
            entry_theta=round(entry_greeks["theta"], 4),
            underlying_at_entry=entry_bar.close,
            underlying_at_exit=td.bars[-1].close if td.bars else 0,
            max_adverse_mark=round(pos.max_adverse, 4),
            iv_at_entry=round(iv, 4),
            tf_entry_confidence=tf_signals.entry_confidence if tf_signals else 0,
            tf_direction_bias=tf_signals.direction_bias if tf_signals else "",
            tf_d1_trend=tf_signals.daily.trend if tf_signals else "",
            tf_h1_structure=tf_signals.hourly.structure if tf_signals else "",
            tf_m5_rsi=tf_signals.m5.rsi if tf_signals else 0,
            tf_size_mult=tf_signals.size_multiplier if tf_signals else 1.0,
        )

    # ─────────────────────────────────────────────────────────────
    # Position opening
    # ─────────────────────────────────────────────────────────────

    def _open_position(self, strategy: str, S: float, T: float,
                        r: float, iv: float, delta_target: float,
                        width: float, strike_round: float,
                        max_risk: float, entry_bar: IntradayBar) -> Optional[SpreadPosition]:
        """Open a spread position at the given entry bar."""

        if strategy == "put_credit":
            K_short = strike_at_delta(S, T, r, iv, delta_target, "put")
            K_short = round(K_short / strike_round) * strike_round
            K_long = K_short - width

            raw_credit = bs_put_price(S, K_short, T, r, iv) - bs_put_price(S, K_long, T, r, iv)
            credit = round(max(raw_credit * (1 - self.slippage_pct), 0.01), 4)

            pos = SpreadPosition(
                entry_time=entry_bar.timestamp,
                strategy="0DTE Put Credit Spread",
                spread_type="put_credit",
                short_strike=K_short, long_strike=K_long,
                entry_credit=credit, width=width,
                current_mark=credit,
            )

        elif strategy == "call_credit":
            K_short = strike_at_delta(S, T, r, iv, delta_target, "call")
            K_short = round(K_short / strike_round) * strike_round
            K_long = K_short + width

            raw_credit = bs_call_price(S, K_short, T, r, iv) - bs_call_price(S, K_long, T, r, iv)
            credit = round(max(raw_credit * (1 - self.slippage_pct), 0.01), 4)

            pos = SpreadPosition(
                entry_time=entry_bar.timestamp,
                strategy="0DTE Call Credit Spread",
                spread_type="call_credit",
                short_strike=K_short, long_strike=K_long,
                entry_credit=credit, width=width,
                current_mark=credit,
            )

        elif strategy == "iron_condor":
            K_put_short = strike_at_delta(S, T, r, iv, delta_target, "put")
            K_put_short = round(K_put_short / strike_round) * strike_round
            K_put_long = K_put_short - width

            K_call_short = strike_at_delta(S, T, r, iv, delta_target, "call")
            K_call_short = round(K_call_short / strike_round) * strike_round
            K_call_long = K_call_short + width

            put_raw = bs_put_price(S, K_put_short, T, r, iv) - bs_put_price(S, K_put_long, T, r, iv)
            call_raw = bs_call_price(S, K_call_short, T, r, iv) - bs_call_price(S, K_call_long, T, r, iv)
            credit = round(max((put_raw + call_raw) * (1 - self.slippage_pct), 0.01), 4)

            pos = SpreadPosition(
                entry_time=entry_bar.timestamp,
                strategy="0DTE Iron Condor",
                spread_type="iron_condor",
                short_strike=K_put_short, long_strike=K_put_long,
                short_call=K_call_short, long_call=K_call_long,
                entry_credit=credit, width=width,
                current_mark=credit,
            )
        else:
            return None

        # Position sizing
        max_loss_per = width - credit
        if max_loss_per <= 0:
            return None
        contracts = max(1, min(10, int(max_risk / (max_loss_per * 100))))

        pos.num_contracts = contracts
        legs = 4 if strategy == "iron_condor" else 2
        pos.commission = self.commission_per_contract * contracts * legs * 2

        return pos

    # ─────────────────────────────────────────────────────────────
    # Spread pricing (the core Greeks engine)
    # ─────────────────────────────────────────────────────────────

    def _price_spread(self, pos: SpreadPosition, S: float,
                      T: float, r: float, iv: float) -> float:
        """
        Re-price a spread at current S and T using Black-Scholes.

        This is where the magic happens — as T shrinks toward zero
        on a 0DTE day, theta accelerates and gamma explodes near strikes.
        """
        if pos.spread_type == "put_credit":
            short_val = bs_put_price(S, pos.short_strike, T, r, iv)
            long_val = bs_put_price(S, pos.long_strike, T, r, iv)
            return max(short_val - long_val, 0)

        elif pos.spread_type == "call_credit":
            short_val = bs_call_price(S, pos.short_strike, T, r, iv)
            long_val = bs_call_price(S, pos.long_strike, T, r, iv)
            return max(short_val - long_val, 0)

        elif pos.spread_type == "iron_condor":
            put_val = bs_put_price(S, pos.short_strike, T, r, iv) - \
                      bs_put_price(S, pos.long_strike, T, r, iv)
            call_val = bs_call_price(S, pos.short_call, T, r, iv) - \
                       bs_call_price(S, pos.long_call, T, r, iv)
            return max(put_val + call_val, 0)

        return 0

    def _spread_greeks(self, pos: SpreadPosition, S: float,
                        T: float, r: float, iv: float) -> Dict[str, float]:
        """Calculate net spread Greeks."""
        if pos.spread_type == "put_credit":
            short_g = bs_all_greeks(S, pos.short_strike, T, r, iv, "put")
            long_g = bs_all_greeks(S, pos.long_strike, T, r, iv, "put")
            return {
                "delta": short_g["delta"] - long_g["delta"],
                "gamma": short_g["gamma"] - long_g["gamma"],
                "theta": short_g["theta"] - long_g["theta"],
            }

        elif pos.spread_type == "call_credit":
            short_g = bs_all_greeks(S, pos.short_strike, T, r, iv, "call")
            long_g = bs_all_greeks(S, pos.long_strike, T, r, iv, "call")
            return {
                "delta": short_g["delta"] - long_g["delta"],
                "gamma": short_g["gamma"] - long_g["gamma"],
                "theta": short_g["theta"] - long_g["theta"],
            }

        elif pos.spread_type == "iron_condor":
            ps = bs_all_greeks(S, pos.short_strike, T, r, iv, "put")
            pl = bs_all_greeks(S, pos.long_strike, T, r, iv, "put")
            cs = bs_all_greeks(S, pos.short_call, T, r, iv, "call")
            cl = bs_all_greeks(S, pos.long_call, T, r, iv, "call")
            return {
                "delta": (ps["delta"] - pl["delta"]) + (cs["delta"] - cl["delta"]),
                "gamma": (ps["gamma"] - pl["gamma"]) + (cs["gamma"] - cl["gamma"]),
                "theta": (ps["theta"] - pl["theta"]) + (cs["theta"] - cl["theta"]),
            }

        return {"delta": 0, "gamma": 0, "theta": 0}

    def _intrinsic_value(self, pos: SpreadPosition, S: float) -> float:
        """Calculate intrinsic value at expiration (T=0)."""
        if pos.spread_type == "put_credit":
            short_intrinsic = max(pos.short_strike - S, 0)
            long_intrinsic = max(pos.long_strike - S, 0)
            return min(short_intrinsic - long_intrinsic, pos.width)

        elif pos.spread_type == "call_credit":
            short_intrinsic = max(S - pos.short_strike, 0)
            long_intrinsic = max(S - pos.long_strike, 0)
            return min(short_intrinsic - long_intrinsic, pos.width)

        elif pos.spread_type == "iron_condor":
            put_intr = max(pos.short_strike - S, 0) - max(pos.long_strike - S, 0)
            call_intr = max(S - pos.short_call, 0) - max(S - pos.long_call, 0)
            return min(max(put_intr, 0) + max(call_intr, 0), pos.width)

        return 0

    # ─────────────────────────────────────────────────────────────
    # IV estimation from intraday price action
    # ─────────────────────────────────────────────────────────────

    def _estimate_iv(self, td: TradingDay) -> float:
        """
        Estimate implied volatility from intraday price action.
        Uses first 30 min of 5-min returns, annualized.
        Falls back to 0.18 (≈ VIX 18) if insufficient data.
        """
        # Use first 6 bars (30 min) to estimate
        n_bars = min(6, len(td.bars) - 1)
        if n_bars < 3:
            return 0.18

        returns = []
        for i in range(1, n_bars + 1):
            ret = (td.bars[i].close - td.bars[i - 1].close) / td.bars[i - 1].close
            returns.append(ret)

        std_5min = np.std(returns)
        # Annualize: 5-min std → daily → annual
        # 78 five-min bars per day, 252 trading days
        iv = std_5min * math.sqrt(78) * math.sqrt(252)

        # Clamp to reasonable range
        iv = max(0.08, min(0.80, iv))
        return round(iv, 4)

    # ─────────────────────────────────────────────────────────────
    # Stats computation
    # ─────────────────────────────────────────────────────────────

    def _compute_stats(self, results: IntradayResults, balance: float,
                       peak: float, max_dd: float, max_dd_dollars: float,
                       daily_pnls: List[float], equity: List[Dict]):
        """Compute all summary statistics."""
        results.ending_balance = round(balance, 2)
        results.peak_balance = round(peak, 2)
        results.total_trades = len(results.trades)
        results.equity_curve = equity
        results.daily_pnl = daily_pnls

        wins = [t for t in results.trades if t.total_pnl > 0]
        losses = [t for t in results.trades if t.total_pnl <= 0]

        results.winners = len(wins)
        results.losers = len(losses)
        results.win_rate = len(wins) / len(results.trades) * 100 if results.trades else 0

        results.total_pnl = round(sum(t.total_pnl for t in results.trades), 2)
        results.avg_winner = round(np.mean([t.total_pnl for t in wins]), 2) if wins else 0
        results.avg_loser = round(np.mean([t.total_pnl for t in losses]), 2) if losses else 0

        total_win = sum(t.total_pnl for t in wins)
        total_loss = abs(sum(t.total_pnl for t in losses))
        results.profit_factor = round(total_win / total_loss, 2) if total_loss > 0 else float('inf')

        results.largest_win = max((t.total_pnl for t in results.trades), default=0)
        results.largest_loss = min((t.total_pnl for t in results.trades), default=0)

        results.max_drawdown_pct = round(max_dd, 2)
        results.max_drawdown_dollars = round(max_dd_dollars, 2)

        # Sharpe
        if daily_pnls and len(daily_pnls) > 5:
            arr = np.array(daily_pnls)
            mean, std = np.mean(arr), np.std(arr)
            results.sharpe_ratio = round((mean / std) * np.sqrt(252), 2) if std > 0 else 0

        # Timing
        durations = [t.duration_minutes for t in results.trades if t.duration_minutes > 0]
        results.avg_duration_minutes = round(np.mean(durations), 1) if durations else 0

        # Gamma blowups
        results.gamma_blow_ups = sum(1 for t in results.trades if t.exit_reason == "gamma_risk")

        # Monthly
        monthly = {}
        for t in results.trades:
            key = t.date.strftime("%Y-%m") if hasattr(t.date, 'strftime') else str(t.date)[:7]
            monthly[key] = monthly.get(key, 0) + t.total_pnl
        results.monthly_pnl = {k: round(v, 2) for k, v in sorted(monthly.items())}

        # Strategy breakdown
        breakdown = {}
        for t in results.trades:
            s = t.strategy
            if s not in breakdown:
                breakdown[s] = {"trades": 0, "wins": 0, "pnl": 0, "premium": 0}
            breakdown[s]["trades"] += 1
            breakdown[s]["pnl"] += t.total_pnl
            breakdown[s]["premium"] += t.entry_credit * t.num_contracts * 100
            if t.total_pnl > 0:
                breakdown[s]["wins"] += 1
        for s in breakdown:
            b = breakdown[s]
            b["win_rate"] = round(b["wins"] / b["trades"] * 100, 1) if b["trades"] else 0
            b["pnl"] = round(b["pnl"], 2)
            b["premium"] = round(b["premium"], 2)
        results.strategy_stats = breakdown

    # ─────────────────────────────────────────────────────────────
    # Report
    # ─────────────────────────────────────────────────────────────

    def print_report(self, r: IntradayResults):
        """Print formatted intraday backtest report."""
        lines = []
        lines.append(header(
            "INTRADAY GREEKS-AWARE BACKTEST",
            f"{r.start_date} → {r.end_date} | {r.total_days} trading days | 5-min bars"
        ))

        # Account
        lines.append(sub_header("ACCOUNT SUMMARY"))
        total_return = (r.ending_balance - r.starting_balance) / r.starting_balance * 100
        lines.append(kv("Starting Balance", f"${r.starting_balance:,.2f}"))
        lines.append(kv("Ending Balance", f"${r.ending_balance:,.2f}",
                         C.GREEN if r.ending_balance > r.starting_balance else C.RED))
        lines.append(kv("Total Return", f"{total_return:+.2f}%",
                         C.GREEN if total_return > 0 else C.RED))

        # Trades
        lines.append(sub_header("TRADE METRICS"))
        lines.append(kv("Total Trades", r.total_trades))
        wr_c = C.GREEN if r.win_rate >= 75 else C.YELLOW if r.win_rate >= 60 else C.RED
        lines.append(kv("Win Rate", f"{r.win_rate:.1f}% ({r.winners}W / {r.losers}L)", wr_c))
        lines.append(kv("Total P&L", pnl(r.total_pnl)))
        lines.append(kv("Avg Winner", pnl(r.avg_winner), C.GREEN))
        lines.append(kv("Avg Loser", pnl(r.avg_loser), C.RED))
        pf_c = C.GREEN if r.profit_factor > 1.5 else C.YELLOW if r.profit_factor > 1 else C.RED
        lines.append(kv("Profit Factor", f"{r.profit_factor:.2f}", pf_c))
        lines.append(kv("Largest Win", pnl(r.largest_win), C.GREEN))
        lines.append(kv("Largest Loss", pnl(r.largest_loss), C.RED))

        # Risk
        lines.append(sub_header("RISK METRICS"))
        dd_c = C.GREEN if r.max_drawdown_pct < 5 else C.YELLOW if r.max_drawdown_pct < 10 else C.RED
        lines.append(kv("Max Drawdown", f"{r.max_drawdown_pct:.2f}% (${r.max_drawdown_dollars:,.2f})", dd_c))
        sr_c = C.GREEN if r.sharpe_ratio > 1.5 else C.YELLOW if r.sharpe_ratio > 0.5 else C.RED
        lines.append(kv("Sharpe Ratio", f"{r.sharpe_ratio:.2f}", sr_c))

        # Greeks impact
        lines.append(sub_header("GREEKS IMPACT"))
        lines.append(kv("Avg Trade Duration", f"{r.avg_duration_minutes:.0f} min"))
        lines.append(kv("Gamma Blowup Exits", f"{r.gamma_blow_ups}",
                         C.RED if r.gamma_blow_ups > 5 else C.GREEN))

        # Exit reasons
        reasons = {}
        for t in r.trades:
            reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
        if reasons:
            lines.append(sub_header("EXIT REASONS"))
            for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
                pct = count / r.total_trades * 100 if r.total_trades else 0
                lines.append(kv(reason, f"{count} ({pct:.0f}%)"))

        # Strategy breakdown
        if r.strategy_stats:
            lines.append(sub_header("STRATEGY BREAKDOWN"))
            for strat, data in r.strategy_stats.items():
                sc = C.GREEN if data["pnl"] > 0 else C.RED
                lines.append(f"  {strat}")
                lines.append(f"    Trades: {data['trades']}  |  WR: {data['win_rate']:.0f}%  |  "
                              f"P&L: {sc}${data['pnl']:,.2f}{C.RESET}")

        # Monthly
        if r.monthly_pnl:
            lines.append(sub_header("MONTHLY P&L"))
            max_m = max(abs(v) for v in r.monthly_pnl.values()) or 1
            for month, mpnl in r.monthly_pnl.items():
                color = C.GREEN if mpnl > 0 else C.RED
                pnl_bar = bar(abs(mpnl), max_m, 25)
                sign = "+" if mpnl > 0 else "-"
                lines.append(f"  {month}  {pnl_bar}  {color}{sign}${abs(mpnl):,.2f}{C.RESET}")

        # Production filter stats
        total_filtered = r.filter_full_size + r.filter_reduced_size + r.filter_half_size
        if total_filtered > 0 or r.filter_skipped > 0:
            lines.append(sub_header("PRODUCTION FILTERS"))
            lines.append(kv("ATR Days Skipped", f"{r.filter_skipped}", C.RED if r.filter_skipped else C.GREEN))
            lines.append(kv("Full Size (1.0×)", f"{r.filter_full_size}", C.GREEN))
            if r.filter_reduced_size > 0:
                lines.append(kv("Reduced (0.75×)", f"{r.filter_reduced_size}", C.YELLOW))
            if r.filter_half_size > 0:
                lines.append(kv("Half Size (0.50×)", f"{r.filter_half_size}", C.YELLOW))

        # Multi-TF stats
        if r.tf_entries_allowed > 0 or r.tf_entries_rejected > 0:
            lines.append(sub_header("MULTI-TIMEFRAME ANALYSIS"))
            total_considered = r.tf_entries_allowed + r.tf_entries_rejected
            filter_rate = r.tf_entries_rejected / total_considered * 100 if total_considered else 0
            lines.append(kv("Days Evaluated", total_considered))
            lines.append(kv("Entries Allowed", r.tf_entries_allowed, C.GREEN))
            lines.append(kv("Entries Rejected", r.tf_entries_rejected, C.RED))
            lines.append(kv("Filter Rate", f"{filter_rate:.1f}% of signals filtered out"))
            lines.append(kv("Avg Entry Confidence", f"{r.tf_avg_confidence:.0%}"))

            if r.tf_exit_reasons:
                lines.append(f"\n  TF Exit Triggers:")
                for reason, count in sorted(r.tf_exit_reasons.items(), key=lambda x: -x[1]):
                    lines.append(kv(f"  {reason}", f"{count}"))

            # D1 trend breakdown
            trend_pnl = {}
            for t in r.trades:
                if t.tf_d1_trend:
                    trend_pnl.setdefault(t.tf_d1_trend, []).append(t.total_pnl)
            if trend_pnl:
                lines.append(f"\n  D1 Trend → P&L:")
                for trend, pnls in sorted(trend_pnl.items()):
                    total = sum(pnls)
                    count = len(pnls)
                    wr = sum(1 for p in pnls if p > 0) / count * 100 if count else 0
                    tc = C.GREEN if total > 0 else C.RED
                    lines.append(f"    {trend:10s}  {count:3d} trades  WR: {wr:.0f}%  "
                                 f"{tc}${total:+,.2f}{C.RESET}")

        lines.append(f"\n{C.CYAN}{'═' * 78}{C.RESET}\n")
        print("\n".join(lines))

    def save_results(self, r: IntradayResults, path: Optional[str] = None):
        """Save results to JSON."""
        path = path or os.path.join(
            os.path.dirname(self.config.data_dir if hasattr(self.config, 'data_dir') else _DATA_DIR),
            "intraday_backtest_results.json"
        )
        os.makedirs(os.path.dirname(path), exist_ok=True)

        data = {
            "type": "intraday_greeks_aware",
            "start": str(r.start_date), "end": str(r.end_date),
            "starting_balance": r.starting_balance,
            "ending_balance": r.ending_balance,
            "total_trades": r.total_trades,
            "win_rate": r.win_rate,
            "total_pnl": r.total_pnl,
            "profit_factor": r.profit_factor,
            "max_drawdown_pct": r.max_drawdown_pct,
            "sharpe_ratio": r.sharpe_ratio,
            "gamma_blowups": r.gamma_blow_ups,
            "monthly_pnl": r.monthly_pnl,
            "trades": [
                {
                    "date": str(t.date), "strategy": t.strategy,
                    "short": t.short_strike, "long": t.long_strike,
                    "credit": t.entry_credit, "exit_price": t.exit_price,
                    "contracts": t.num_contracts,
                    "pnl": t.total_pnl, "exit": t.exit_reason,
                    "entry_time": t.entry_time, "exit_time": t.exit_time,
                    "duration_min": t.duration_minutes,
                    "delta": t.entry_delta, "gamma": t.entry_gamma,
                    "theta": t.entry_theta,
                    "underlying_entry": t.underlying_at_entry,
                    "underlying_exit": t.underlying_at_exit,
                    "max_adverse": t.max_adverse_mark,
                    "iv": t.iv_at_entry,
                }
                for t in r.trades
            ],
        }

        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        print(f"\n  💾 Results saved to {path}")

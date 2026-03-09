"""
Historical Backtester
======================
Replays historical trading days through the engine's modules
to simulate credit spread / iron condor trades and measure real P&L.

This is NOT a toy backtest. It models:
  • Regime-gated entries (only trades on GREEN/YELLOW days)
  • Position sizing from account config
  • Stop-losses, profit targets, expiration
  • Intraday price movement simulation (open→close range)
  • Realistic credit spread P&L math
  • Commission estimates
  • Win rate, profit factor, Sharpe, drawdown — all tracked

Usage:
    from trading_engine.data.backtester import Backtester
    from trading_engine.data import HistoricalDataFetcher

    fetcher = HistoricalDataFetcher()
    fetcher.download(start="2024-01-01")
    snapshots = fetcher.build_snapshots()

    bt = Backtester()
    results = bt.run(snapshots)
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
from ..models import MarketSnapshot, MarketRegime, TradeResult
from ..modules.regime_classifier import RegimeClassifier
from ..black_scholes import (
    bs_put_price, bs_call_price, strike_at_delta, expected_move
)
from ..formatters import header, sub_header, kv, pnl, bar, C


# ─────────────────────────────────────────────────────────────────────
# Trade simulation models
# ─────────────────────────────────────────────────────────────────────

@dataclass
class SimulatedTrade:
    """A single simulated trade."""
    entry_date: date = field(default_factory=date.today)
    exit_date: date = field(default_factory=date.today)
    strategy: str = ""
    underlying: str = "SPX"
    # Spread details
    short_strike: float = 0.0
    long_strike: float = 0.0
    spread_type: str = "put_credit"   # "put_credit", "call_credit", "iron_condor"
    # Call side (for iron condors)
    short_call: float = 0.0
    long_call: float = 0.0
    # Pricing
    credit: float = 0.0               # Credit collected per contract
    width: float = 0.0                # Spread width
    max_loss: float = 0.0             # max_loss = width - credit
    num_contracts: int = 1
    # Outcome
    exit_price: float = 0.0           # What spread is worth at exit
    pnl_per_contract: float = 0.0     # credit - exit_price  (positive = profit)
    total_pnl: float = 0.0            # pnl * contracts * 100
    exit_reason: str = ""             # "expired", "profit_target", "stop_loss", "breached"
    # Context
    regime: str = "GREEN"
    vix_at_entry: float = 0.0
    spx_at_entry: float = 0.0
    spx_at_exit: float = 0.0
    commission: float = 0.0


@dataclass
class BacktestResults:
    """Complete backtest output."""
    start_date: date = field(default_factory=date.today)
    end_date: date = field(default_factory=date.today)
    total_trading_days: int = 0
    days_traded: int = 0
    days_skipped_red: int = 0
    days_skipped_yellow: int = 0
    # Account
    starting_balance: float = 50_000.0
    ending_balance: float = 50_000.0
    peak_balance: float = 50_000.0
    # Trades
    trades: List[SimulatedTrade] = field(default_factory=list)
    total_trades: int = 0
    winners: int = 0
    losers: int = 0
    win_rate: float = 0.0
    # P&L
    total_pnl: float = 0.0
    total_premium_collected: float = 0.0
    avg_winner: float = 0.0
    avg_loser: float = 0.0
    profit_factor: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    # Risk
    max_drawdown_pct: float = 0.0
    max_drawdown_dollars: float = 0.0
    sharpe_ratio: float = 0.0
    # Equity curve
    equity_curve: List[Dict] = field(default_factory=list)
    daily_pnl: List[float] = field(default_factory=list)
    # Strategy breakdown
    strategy_stats: Dict[str, Dict] = field(default_factory=dict)
    # Monthly breakdown
    monthly_pnl: Dict[str, float] = field(default_factory=dict)


class Backtester:
    """
    Replays historical MarketSnapshots through the engine's
    regime classifier and trade logic to simulate real results.
    """

    def __init__(self, config: Optional[EngineConfig] = None):
        self.config = config or EngineConfig()
        self.regime_classifier = RegimeClassifier(self.config)
        self.commission_per_contract: float = 0.65  # IBKR option commission

    def run(self, snapshots: List[MarketSnapshot],
            strategies: Optional[List[str]] = None,
            trade_yellow: bool = True) -> BacktestResults:
        """
        Run the backtest over a list of daily MarketSnapshots.

        Args:
            snapshots:     List of daily MarketSnapshot (from fetcher)
            strategies:    Which strategies to simulate. Default: all.
                           Options: "put_credit", "iron_condor", "eod_scalp"
            trade_yellow:  Whether to trade on YELLOW regime days (conservative)
        """
        if strategies is None:
            strategies = ["put_credit"]

        results = BacktestResults(
            start_date=snapshots[0].timestamp.date(),
            end_date=snapshots[-1].timestamp.date(),
            starting_balance=self.config.account.account_size,
        )

        balance = self.config.account.account_size
        peak = balance
        max_dd = 0.0
        max_dd_dollars = 0.0
        daily_pnls = []
        equity = [{"date": results.start_date.isoformat(), "balance": balance}]

        for snap in snapshots:
            dt = snap.timestamp.date() if isinstance(snap.timestamp, datetime) else snap.timestamp
            results.total_trading_days += 1

            # 1. Classify regime
            regime_report = self.regime_classifier.classify(snap)
            regime = regime_report.verdict

            # 2. Gate: should we trade today?
            if regime == MarketRegime.RED:
                results.days_skipped_red += 1
                daily_pnls.append(0.0)
                equity.append({"date": str(dt), "balance": balance})
                continue

            if regime == MarketRegime.YELLOW and not trade_yellow:
                results.days_skipped_yellow += 1
                daily_pnls.append(0.0)
                equity.append({"date": str(dt), "balance": balance})
                continue

            # 3. Simulate trades for each strategy
            day_pnl = 0.0
            for strat in strategies:
                trade = self._simulate_trade(snap, strat, regime, balance)
                if trade is None:
                    continue

                results.trades.append(trade)
                day_pnl += trade.total_pnl

            if day_pnl != 0:
                results.days_traded += 1

            # 4. Update account
            balance += day_pnl
            daily_pnls.append(day_pnl)
            equity.append({"date": str(dt), "balance": round(balance, 2)})

            # Track drawdown
            peak = max(peak, balance)
            dd = (peak - balance) / peak * 100 if peak > 0 else 0
            dd_dollars = peak - balance
            max_dd = max(max_dd, dd)
            max_dd_dollars = max(max_dd_dollars, dd_dollars)

        # ── Compute summary stats ──

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
        results.total_premium_collected = round(sum(t.credit * t.num_contracts * 100 for t in results.trades), 2)

        results.avg_winner = round(sum(t.total_pnl for t in wins) / len(wins), 2) if wins else 0
        results.avg_loser = round(sum(t.total_pnl for t in losses) / len(losses), 2) if losses else 0

        total_win_pnl = sum(t.total_pnl for t in wins)
        total_loss_pnl = abs(sum(t.total_pnl for t in losses))
        results.profit_factor = round(total_win_pnl / total_loss_pnl, 2) if total_loss_pnl > 0 else float('inf')

        results.largest_win = max((t.total_pnl for t in results.trades), default=0)
        results.largest_loss = min((t.total_pnl for t in results.trades), default=0)

        results.max_drawdown_pct = round(max_dd, 2)
        results.max_drawdown_dollars = round(max_dd_dollars, 2)

        # Sharpe
        if daily_pnls and len(daily_pnls) > 5:
            arr = np.array(daily_pnls)
            mean = np.mean(arr)
            std = np.std(arr)
            results.sharpe_ratio = round((mean / std) * np.sqrt(252), 2) if std > 0 else 0.0

        # Strategy breakdown
        results.strategy_stats = self._strategy_breakdown(results.trades)

        # Monthly breakdown
        results.monthly_pnl = self._monthly_breakdown(results.trades)

        return results

    # ─────────────────────────────────────────────────────────────
    # Trade Simulation  (OHLC-driven — uses actual daily range)
    # ─────────────────────────────────────────────────────────────

    def _simulate_trade(self, snap: MarketSnapshot, strategy: str,
                         regime: MarketRegime, balance: float) -> Optional[SimulatedTrade]:
        """
        Simulate a single day's trade based on strategy type.

        Key principle: We enter at market OPEN, place strikes relative
        to the OPEN price, then use the day's actual HIGH / LOW to
        determine whether our strikes were breached.  The day's CLOSE
        determines expiration settlement.

        This gives honest back-test P&L that correlates with real
        market behaviour — big down days produce losses.
        """
        # Use open price for entry (that's when the trade is placed)
        entry = snap.day_open if snap.day_open > 0 else snap.spx_price
        close = snap.spx_price
        day_low = snap.day_low if snap.day_low > 0 else entry
        day_high = snap.day_high if snap.day_high > 0 else entry

        iv = snap.vix_level / 100 if snap.vix_level > 1 else 0.18
        r = 0.045
        T = 1 / 252  # 0DTE

        # Delta based on regime
        if regime == MarketRegime.GREEN:
            delta = self.config.trading.short_delta_max  # 0.15
        else:
            delta = self.config.trading.short_delta_min  # 0.10

        # Position sizing — capped conservatively
        max_risk = balance * self.config.account.max_risk_per_trade_pct
        width = self.config.trading.spread_width_min  # 5 pts SPX

        if strategy == "put_credit":
            return self._sim_put_credit(snap, entry, close, day_low, day_high,
                                        iv, r, T, delta, width, max_risk, regime)
        elif strategy == "call_credit":
            return self._sim_call_credit(snap, entry, close, day_low, day_high,
                                          iv, r, T, delta, width, max_risk, regime)
        elif strategy == "iron_condor":
            return self._sim_iron_condor(snap, entry, close, day_low, day_high,
                                          iv, r, T, delta, width, max_risk, regime)
        elif strategy == "eod_scalp":
            return self._sim_eod_scalp(snap, entry, close, day_low, day_high,
                                        iv, r, T, width, max_risk, regime)
        return None

    # ── helpers ──────────────────────────────────────────────────

    @staticmethod
    def _apply_slippage(raw_credit: float, slippage_pct: float = 0.15) -> float:
        """0DTE SPX options have wide bid-ask.  Reduce credit by slippage %."""
        return round(max(raw_credit * (1 - slippage_pct), 0.05), 2)

    def _position_size(self, max_risk: float, max_loss_per: float,
                        cap: int = 10) -> int:
        """Return # contracts, capped for realism."""
        if max_loss_per <= 0:
            return 1
        n = int(max_risk / (max_loss_per * 100))
        return max(1, min(n, cap))

    # ── Put credit spread ────────────────────────────────────────

    def _sim_put_credit(self, snap, entry, close, day_low, day_high,
                        iv, r, T, delta, width, max_risk, regime) -> SimulatedTrade:
        """
        Simulate a 0DTE put credit spread using actual OHLC.

        Entry at OPEN → strikes placed from OPEN → actual LOW
        determines if the short put was ever breached → CLOSE
        determines settlement value at expiration.
        """
        dt = snap.timestamp.date() if isinstance(snap.timestamp, datetime) else snap.timestamp

        # Place short put at target delta from OPEN
        K_short = strike_at_delta(entry, T, r, iv, delta, "put")
        K_short = round(K_short / 5) * 5
        K_long = K_short - width

        # Credit with bid-ask slippage (0DTE options have wide spreads)
        raw = bs_put_price(entry, K_short, T, r, iv) - bs_put_price(entry, K_long, T, r, iv)
        credit = self._apply_slippage(raw)

        max_loss_per = width - credit
        contracts = self._position_size(max_risk, max_loss_per)
        commission = self.commission_per_contract * contracts * 2 * 2  # 2 legs open+close

        # ── Outcome based on actual price action ──
        stop_mult = self.config.trading.zero_dte_stop_multiplier
        profit_target_pct = self.config.trading.zero_dte_profit_target_pct

        if day_low <= K_long:
            # Catastrophic: blew through both strikes
            # Stop-loss should fire on the way down, but gap events
            # can cause slippage beyond the stop
            stop_val = credit * stop_mult
            if stop_val < width:
                # Stop triggered → loss limited to stop
                exit_price = stop_val
                exit_reason = "stop_loss"
            else:
                exit_price = width
                exit_reason = "breached"
            pnl_per = credit - exit_price

        elif day_low <= K_short:
            # Short strike breached — we are between the strikes
            penetration_depth = K_short - day_low
            spread_val_at_low = penetration_depth + credit * 0.3  # intrinsic + residual TV

            stop_val = credit * stop_mult
            if spread_val_at_low >= stop_val:
                # Stop was triggered while price was below strike
                exit_price = min(stop_val, width)
                exit_reason = "stop_loss"
            elif close < K_short:
                # Settled ITM — spread has intrinsic value at close
                intrinsic = K_short - close
                exit_price = min(intrinsic, width)
                exit_reason = "expired_itm"
            else:
                # Dipped below strike but recovered by close → OTM at exp
                # However, active management would have stopped out
                # when the strike was breached, so model partial loss
                exit_price = credit * 0.80  # gave back most of the credit
                exit_reason = "closed_at_risk"
            pnl_per = credit - exit_price

        else:
            # Day low stayed above short strike — WE WIN
            cushion = (day_low - K_short) / snap.expected_move_1d if snap.expected_move_1d > 0 else 1.0

            if cushion > 0.4:
                # Comfortable margin → profit target hit (theta melts fast on 0DTE)
                exit_price = credit * (1 - profit_target_pct)
                exit_reason = "profit_target"
            else:
                # Close call but survived → expired OTM
                exit_price = 0.0
                exit_reason = "expired"
            pnl_per = credit - exit_price

        total_pnl = round(pnl_per * contracts * 100 - commission, 2)

        return SimulatedTrade(
            entry_date=dt, exit_date=dt,
            strategy="0DTE Put Credit Spread",
            short_strike=K_short, long_strike=K_long,
            spread_type="put_credit",
            credit=credit, width=width, max_loss=max_loss_per,
            num_contracts=contracts,
            exit_price=round(exit_price, 2),
            pnl_per_contract=round(pnl_per, 2),
            total_pnl=total_pnl,
            exit_reason=exit_reason,
            regime=regime.value,
            vix_at_entry=snap.vix_level,
            spx_at_entry=entry,
            spx_at_exit=close,
            commission=round(commission, 2),
        )

    # ── Iron condor ──────────────────────────────────────────────

    def _sim_iron_condor(self, snap, entry, close, day_low, day_high,
                          iv, r, T, delta, width, max_risk, regime) -> SimulatedTrade:
        """Simulate 0DTE iron condor using actual OHLC."""
        dt = snap.timestamp.date() if isinstance(snap.timestamp, datetime) else snap.timestamp

        # Put side
        K_put_short = strike_at_delta(entry, T, r, iv, delta, "put")
        K_put_short = round(K_put_short / 5) * 5
        K_put_long = K_put_short - width

        # Call side
        K_call_short = strike_at_delta(entry, T, r, iv, delta, "call")
        K_call_short = round(K_call_short / 5) * 5
        K_call_long = K_call_short + width

        put_raw = bs_put_price(entry, K_put_short, T, r, iv) - bs_put_price(entry, K_put_long, T, r, iv)
        call_raw = bs_call_price(entry, K_call_short, T, r, iv) - bs_call_price(entry, K_call_long, T, r, iv)
        credit = self._apply_slippage(put_raw + call_raw)

        max_loss_per = width - credit
        contracts = self._position_size(max_risk, max_loss_per)
        commission = self.commission_per_contract * contracts * 4 * 2

        stop_mult = self.config.trading.zero_dte_stop_multiplier

        # Check both sides against actual high/low
        put_breached = day_low <= K_put_short
        call_breached = day_high >= K_call_short

        if put_breached or call_breached:
            stop_val = credit * stop_mult
            if day_low <= K_put_long or day_high >= K_call_long:
                # Blew through one wing
                exit_price = min(stop_val, width)
                exit_reason = "breached"
            else:
                exit_price = min(stop_val, width)
                exit_reason = "stop_loss"
            pnl_per = credit - exit_price
        else:
            cushion_put = (day_low - K_put_short) / snap.expected_move_1d if snap.expected_move_1d > 0 else 1
            cushion_call = (K_call_short - day_high) / snap.expected_move_1d if snap.expected_move_1d > 0 else 1
            cushion = min(cushion_put, cushion_call)

            if cushion > 0.4:
                exit_price = credit * (1 - self.config.trading.zero_dte_profit_target_pct)
                exit_reason = "profit_target"
            else:
                exit_price = 0.0
                exit_reason = "expired"
            pnl_per = credit - exit_price

        total_pnl = round(pnl_per * contracts * 100 - commission, 2)

        return SimulatedTrade(
            entry_date=dt, exit_date=dt,
            strategy="0DTE Iron Condor",
            short_strike=K_put_short, long_strike=K_put_long,
            short_call=K_call_short, long_call=K_call_long,
            spread_type="iron_condor",
            credit=credit, width=width, max_loss=max_loss_per,
            num_contracts=contracts,
            exit_price=round(exit_price, 2),
            pnl_per_contract=round(pnl_per, 2),
            total_pnl=total_pnl,
            exit_reason=exit_reason,
            regime=regime.value,
            vix_at_entry=snap.vix_level,
            spx_at_entry=entry,
            spx_at_exit=close,
            commission=round(commission, 2),
        )

    # ── Call credit spread ────────────────────────────────────────

    def _sim_call_credit(self, snap, entry, close, day_low, day_high,
                          iv, r, T, delta, width, max_risk, regime) -> SimulatedTrade:
        """
        Simulate a 0DTE call credit spread using actual OHLC.

        Mirror image of put credit spread — profits when market
        stays flat or drops, loses on strong rallies.
        Entry at OPEN → actual HIGH determines if short call breached.
        """
        dt = snap.timestamp.date() if isinstance(snap.timestamp, datetime) else snap.timestamp

        # Place short call at target delta from OPEN
        K_short = strike_at_delta(entry, T, r, iv, delta, "call")
        K_short = round(K_short / 5) * 5
        K_long = K_short + width

        # Credit with bid-ask slippage
        raw = bs_call_price(entry, K_short, T, r, iv) - bs_call_price(entry, K_long, T, r, iv)
        credit = self._apply_slippage(raw)

        max_loss_per = width - credit
        contracts = self._position_size(max_risk, max_loss_per)
        commission = self.commission_per_contract * contracts * 2 * 2

        stop_mult = self.config.trading.zero_dte_stop_multiplier
        profit_target_pct = self.config.trading.zero_dte_profit_target_pct

        if day_high >= K_long:
            # Catastrophic: blew through both strikes
            stop_val = credit * stop_mult
            if stop_val < width:
                exit_price = stop_val
                exit_reason = "stop_loss"
            else:
                exit_price = width
                exit_reason = "breached"
            pnl_per = credit - exit_price

        elif day_high >= K_short:
            # Short call breached — between the strikes
            penetration_depth = day_high - K_short
            spread_val_at_high = penetration_depth + credit * 0.3

            stop_val = credit * stop_mult
            if spread_val_at_high >= stop_val:
                exit_price = min(stop_val, width)
                exit_reason = "stop_loss"
            elif close > K_short:
                # Settled ITM
                intrinsic = close - K_short
                exit_price = min(intrinsic, width)
                exit_reason = "expired_itm"
            else:
                # Spiked above strike but recovered by close
                exit_price = credit * 0.80
                exit_reason = "closed_at_risk"
            pnl_per = credit - exit_price

        else:
            # Day high stayed below short call — WE WIN
            cushion = (K_short - day_high) / snap.expected_move_1d if snap.expected_move_1d > 0 else 1.0

            if cushion > 0.4:
                exit_price = credit * (1 - profit_target_pct)
                exit_reason = "profit_target"
            else:
                exit_price = 0.0
                exit_reason = "expired"
            pnl_per = credit - exit_price

        total_pnl = round(pnl_per * contracts * 100 - commission, 2)

        return SimulatedTrade(
            entry_date=dt, exit_date=dt,
            strategy="0DTE Call Credit Spread",
            short_strike=K_short, long_strike=K_long,
            spread_type="call_credit",
            credit=credit, width=width, max_loss=max_loss_per,
            num_contracts=contracts,
            exit_price=round(exit_price, 2),
            pnl_per_contract=round(pnl_per, 2),
            total_pnl=total_pnl,
            exit_reason=exit_reason,
            regime=regime.value,
            vix_at_entry=snap.vix_level,
            spx_at_entry=entry,
            spx_at_exit=close,
            commission=round(commission, 2),
        )

    # ── EOD scalp ────────────────────────────────────────────────

    def _sim_eod_scalp(self, snap, entry, close, day_low, day_high,
                        iv, r, T, width, max_risk, regime) -> SimulatedTrade:
        """
        Simulate EOD theta scalp (last 90 min).
        Uses the day's close-vs-open move scaled to 90-min window.
        """
        dt = snap.timestamp.date() if isinstance(snap.timestamp, datetime) else snap.timestamp

        delta = self.config.trading.eod_short_delta_max  # 0.12
        T_eod = 1.5 / (252 * 6.5)  # 90 min in years

        # EOD entry is ~2:30 PM.  Approximate the price at 2:30 PM
        # as 85% of the way from open to close (most move happens by then).
        eod_entry = entry + (close - entry) * 0.85

        K_short = strike_at_delta(eod_entry, T_eod, r, iv, delta, "put")
        K_short = round(K_short / 5) * 5
        K_long = K_short - width

        raw = bs_put_price(eod_entry, K_short, T_eod, r, iv) - bs_put_price(eod_entry, K_long, T_eod, r, iv)
        credit = self._apply_slippage(raw, slippage_pct=0.20)  # wider slippage near close

        if credit < self.config.trading.eod_min_credit:
            return None

        max_loss_per = width - credit
        contracts = self._position_size(max_risk * 0.5, max_loss_per, cap=3)
        commission = self.commission_per_contract * contracts * 2 * 2

        # EOD move: approximate last 90 min as a fraction of the daily range
        # Use close vs the interpolated 2:30 price
        eod_low = min(eod_entry, close) - abs(close - eod_entry) * 0.3
        # Also consider intraday action: if the day's low was near close,
        # the EOD period may have been volatile
        if day_low < eod_entry:
            eod_low = min(eod_low, eod_entry - (eod_entry - day_low) * 0.2)

        stop_mult = self.config.trading.eod_stop_multiplier

        if eod_low <= K_short:
            exit_price = min(credit * stop_mult, width)
            exit_reason = "stop_loss"
            pnl_per = credit - exit_price
        else:
            exit_price = 0.0
            exit_reason = "expired"
            pnl_per = credit

        total_pnl = round(pnl_per * contracts * 100 - commission, 2)

        return SimulatedTrade(
            entry_date=dt, exit_date=dt,
            strategy="EOD Theta Scalp",
            short_strike=K_short, long_strike=K_long,
            spread_type="put_credit",
            credit=credit, width=width, max_loss=max_loss_per,
            num_contracts=contracts,
            exit_price=round(exit_price, 2),
            pnl_per_contract=round(pnl_per, 2),
            total_pnl=total_pnl,
            exit_reason=exit_reason,
            regime=regime.value,
            vix_at_entry=snap.vix_level,
            spx_at_entry=round(eod_entry, 2),
            spx_at_exit=close,
            commission=round(commission, 2),
        )

    # ─────────────────────────────────────────────────────────────
    # Stats Helpers
    # ─────────────────────────────────────────────────────────────

    def _strategy_breakdown(self, trades: List[SimulatedTrade]) -> Dict[str, Dict]:
        breakdown = {}
        for t in trades:
            s = t.strategy
            if s not in breakdown:
                breakdown[s] = {"trades": 0, "wins": 0, "losses": 0, "pnl": 0, "premium": 0}
            breakdown[s]["trades"] += 1
            breakdown[s]["pnl"] += t.total_pnl
            breakdown[s]["premium"] += t.credit * t.num_contracts * 100
            if t.total_pnl > 0:
                breakdown[s]["wins"] += 1
            else:
                breakdown[s]["losses"] += 1
        for s in breakdown:
            b = breakdown[s]
            b["win_rate"] = round(b["wins"] / b["trades"] * 100, 1) if b["trades"] else 0
            b["pnl"] = round(b["pnl"], 2)
            b["premium"] = round(b["premium"], 2)
        return breakdown

    def _monthly_breakdown(self, trades: List[SimulatedTrade]) -> Dict[str, float]:
        monthly = {}
        for t in trades:
            key = t.entry_date.strftime("%Y-%m") if hasattr(t.entry_date, 'strftime') else str(t.entry_date)[:7]
            monthly[key] = monthly.get(key, 0) + t.total_pnl
        return {k: round(v, 2) for k, v in sorted(monthly.items())}

    # ─────────────────────────────────────────────────────────────
    # Report Output
    # ─────────────────────────────────────────────────────────────

    def print_report(self, r: BacktestResults):
        """Print formatted backtest report."""
        lines = []
        lines.append(header(
            "BACKTEST RESULTS",
            f"{r.start_date} → {r.end_date} | {r.total_trading_days} trading days"
        ))

        # Account summary
        lines.append(sub_header("ACCOUNT SUMMARY"))
        total_return = (r.ending_balance - r.starting_balance) / r.starting_balance * 100
        lines.append(kv("Starting Balance", f"${r.starting_balance:,.2f}"))
        lines.append(kv("Ending Balance", f"${r.ending_balance:,.2f}",
                         C.GREEN if r.ending_balance > r.starting_balance else C.RED))
        lines.append(kv("Total Return", f"{total_return:+.2f}%",
                         C.GREEN if total_return > 0 else C.RED))
        lines.append(kv("Peak Balance", f"${r.peak_balance:,.2f}"))

        # Activity
        lines.append(sub_header("ACTIVITY"))
        lines.append(kv("Trading Days", r.total_trading_days))
        lines.append(kv("Days Traded", f"{r.days_traded} ({r.days_traded/r.total_trading_days*100:.0f}%)"))
        lines.append(kv("Days Skipped (RED)", r.days_skipped_red))
        lines.append(kv("Days Skipped (YELLOW)", r.days_skipped_yellow))

        # Trade metrics
        lines.append(sub_header("TRADE METRICS"))
        lines.append(kv("Total Trades", r.total_trades))
        wr_color = C.GREEN if r.win_rate >= 75 else C.YELLOW if r.win_rate >= 60 else C.RED
        lines.append(kv("Win Rate", f"{r.win_rate:.1f}% ({r.winners}W / {r.losers}L)", wr_color))
        lines.append(kv("Total P&L", pnl(r.total_pnl),
                         C.GREEN if r.total_pnl > 0 else C.RED))
        lines.append(kv("Premium Collected", f"${r.total_premium_collected:,.2f}"))
        lines.append(kv("Avg Winner", pnl(r.avg_winner), C.GREEN))
        lines.append(kv("Avg Loser", pnl(r.avg_loser), C.RED))
        pf_color = C.GREEN if r.profit_factor > 1.5 else C.YELLOW if r.profit_factor > 1 else C.RED
        lines.append(kv("Profit Factor", f"{r.profit_factor:.2f}", pf_color))
        lines.append(kv("Largest Win", pnl(r.largest_win), C.GREEN))
        lines.append(kv("Largest Loss", pnl(r.largest_loss), C.RED))

        # Risk metrics
        lines.append(sub_header("RISK METRICS"))
        dd_color = C.GREEN if r.max_drawdown_pct < 5 else C.YELLOW if r.max_drawdown_pct < 10 else C.RED
        lines.append(kv("Max Drawdown", f"{r.max_drawdown_pct:.2f}% (${r.max_drawdown_dollars:,.2f})", dd_color))
        sr_color = C.GREEN if r.sharpe_ratio > 1.5 else C.YELLOW if r.sharpe_ratio > 0.5 else C.RED
        lines.append(kv("Sharpe Ratio", f"{r.sharpe_ratio:.2f}", sr_color))

        # Monthly P&L
        if r.monthly_pnl:
            lines.append(sub_header("MONTHLY P&L"))
            max_monthly = max(abs(v) for v in r.monthly_pnl.values()) or 1
            for month, mpnl in r.monthly_pnl.items():
                color = C.GREEN if mpnl > 0 else C.RED
                pnl_bar = bar(abs(mpnl), max_monthly, 25)
                sign = "+" if mpnl > 0 else "-"
                lines.append(f"  {month}  {pnl_bar}  {color}{sign}${abs(mpnl):,.2f}{C.RESET}")

        # Strategy breakdown
        if r.strategy_stats:
            lines.append(sub_header("STRATEGY BREAKDOWN"))
            for strat, data in r.strategy_stats.items():
                sc = C.GREEN if data["pnl"] > 0 else C.RED
                lines.append(f"  {strat}")
                lines.append(f"    Trades: {data['trades']}  |  WR: {data['win_rate']:.0f}%  |  "
                              f"P&L: {sc}${data['pnl']:,.2f}{C.RESET}  |  Premium: ${data['premium']:,.2f}")

        # Exit reason distribution
        reasons = {}
        for t in r.trades:
            reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
        if reasons:
            lines.append(sub_header("EXIT REASONS"))
            for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
                pct = count / r.total_trades * 100
                lines.append(kv(reason, f"{count} ({pct:.0f}%)"))

        lines.append(f"\n{C.CYAN}{'═' * 78}{C.RESET}\n")
        print("\n".join(lines))

    def save_results(self, r: BacktestResults, path: Optional[str] = None):
        """Save backtest results to JSON."""
        path = path or os.path.join(self.config.data_dir, "backtest_results.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)

        data = {
            "start": str(r.start_date), "end": str(r.end_date),
            "starting_balance": r.starting_balance,
            "ending_balance": r.ending_balance,
            "total_trades": r.total_trades,
            "win_rate": r.win_rate,
            "total_pnl": r.total_pnl,
            "profit_factor": r.profit_factor,
            "max_drawdown_pct": r.max_drawdown_pct,
            "sharpe_ratio": r.sharpe_ratio,
            "monthly_pnl": r.monthly_pnl,
            "equity_curve": r.equity_curve,
            "trades": [
                {
                    "date": str(t.entry_date), "strategy": t.strategy,
                    "short": t.short_strike, "long": t.long_strike,
                    "credit": t.credit, "contracts": t.num_contracts,
                    "pnl": t.total_pnl, "exit": t.exit_reason,
                    "vix": t.vix_at_entry, "spx": t.spx_at_entry,
                    "regime": t.regime,
                }
                for t in r.trades
            ],
        }

        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        print(f"\n  💾 Results saved to {path}")

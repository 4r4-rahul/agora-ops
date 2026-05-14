"""
Data models for the walk-forward backtester.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any


class BacktestTradeStatus(str, Enum):
    OPEN = "open"
    CLOSED_PROFIT_TARGET = "closed_profit_target"
    CLOSED_STOP_LOSS = "closed_stop_loss"
    CLOSED_EXPIRY = "closed_expiry"
    CLOSED_MANUAL = "closed_manual"


@dataclass
class BacktestTrade:
    """One simulated trade from the pipeline recommendation."""

    session_id: str
    date_opened: date
    ticker: str
    strategy: str
    direction: str

    # Entry
    entry_price: float          # debit/credit per share (×100 per contract)
    contracts: int
    position_size_dollars: float
    max_loss_dollars: float
    profit_target: float        # option price target
    stop_loss: float            # option price stop
    expiration_dte: int         # DTE at entry
    expiration_date: date

    # Claude reasoning captured
    thesis: str
    reward_risk_ratio: float

    # Exit (filled in when trade closes)
    date_closed: date | None = None
    exit_price: float | None = None
    pnl_dollars: float | None = None          # net of commissions
    commission_dollars: float = 0.0           # round-trip commission paid
    status: BacktestTradeStatus = BacktestTradeStatus.OPEN

    # Underlying price at entry — needed for cumulative P&L tracking
    underlying_at_entry: float = 0.0

    # Raw recommendation payload
    recommendation: dict[str, Any] = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        return self.status == BacktestTradeStatus.OPEN

    @property
    def dte_remaining(self) -> int | None:
        if self.date_closed:
            return 0
        return self.expiration_dte  # simplified — would need current date


@dataclass
class BacktestResult:
    """Aggregate results from a completed backtest run."""

    ticker: str
    start_date: date
    end_date: date
    starting_balance: float

    trades: list[BacktestTrade] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)  # daily balance
    daily_dates: list[date] = field(default_factory=list)

    @property
    def total_trades(self) -> int:
        return len(self.trades)

    @property
    def closed_trades(self) -> list[BacktestTrade]:
        return [t for t in self.trades if not t.is_open]

    @property
    def winning_trades(self) -> list[BacktestTrade]:
        return [t for t in self.closed_trades if (t.pnl_dollars or 0) > 0]

    @property
    def losing_trades(self) -> list[BacktestTrade]:
        return [t for t in self.closed_trades if (t.pnl_dollars or 0) <= 0]

    @property
    def win_rate(self) -> float:
        if not self.closed_trades:
            return 0.0
        return len(self.winning_trades) / len(self.closed_trades)

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl_dollars or 0 for t in self.closed_trades)

    @property
    def ending_balance(self) -> float:
        return self.starting_balance + self.total_pnl

    @property
    def avg_win(self) -> float:
        if not self.winning_trades:
            return 0.0
        return sum(t.pnl_dollars or 0 for t in self.winning_trades) / len(self.winning_trades)

    @property
    def avg_loss(self) -> float:
        if not self.losing_trades:
            return 0.0
        return sum(t.pnl_dollars or 0 for t in self.losing_trades) / len(self.losing_trades)

    @property
    def profit_factor(self) -> float:
        gross_win = sum(t.pnl_dollars or 0 for t in self.winning_trades)
        gross_loss = abs(sum(t.pnl_dollars or 0 for t in self.losing_trades))
        return gross_win / gross_loss if gross_loss > 0 else float("inf")

    @property
    def max_drawdown_dollars(self) -> float:
        if not self.equity_curve:
            return 0.0
        peak = self.equity_curve[0]
        max_dd = 0.0
        for v in self.equity_curve:
            if v > peak:
                peak = v
            dd = peak - v
            if dd > max_dd:
                max_dd = dd
        return max_dd

    @property
    def max_drawdown_pct(self) -> float:
        if not self.equity_curve:
            return 0.0
        peak = self.equity_curve[0]
        max_dd_pct = 0.0
        for v in self.equity_curve:
            if v > peak:
                peak = v
            if peak > 0:
                dd_pct = (peak - v) / peak * 100
                if dd_pct > max_dd_pct:
                    max_dd_pct = dd_pct
        return max_dd_pct

    @property
    def sharpe_ratio(self) -> float:
        import math
        if len(self.equity_curve) < 2:
            return 0.0
        returns = [
            (self.equity_curve[i] - self.equity_curve[i - 1]) / self.equity_curve[i - 1]
            for i in range(1, len(self.equity_curve))
            if self.equity_curve[i - 1] > 0
        ]
        if len(returns) < 2:
            return 0.0
        mean_r = sum(returns) / len(returns)
        var = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
        std = math.sqrt(var) if var > 0 else 0.0
        if std == 0:
            return 0.0
        return (mean_r / std) * math.sqrt(252)

    @property
    def total_commissions(self) -> float:
        return sum(t.commission_dollars for t in self.closed_trades)

    @property
    def expectancy(self) -> float:
        if not self.closed_trades:
            return 0.0
        return self.total_pnl / len(self.closed_trades)

"""
AGORA backtest data models — extends APEX models with pillar tracking.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any


class AgoraTradeStatus(str, Enum):
    OPEN                = "open"
    CLOSED_PROFIT_TARGET = "closed_profit_target"
    CLOSED_STOP_LOSS    = "closed_stop_loss"
    CLOSED_DTE          = "closed_dte"           # 21-DTE management exit
    CLOSED_EXPIRY       = "closed_expiry"         # held to expiry (OTM)
    CLOSED_ASSIGNED     = "closed_assigned"       # short leg went ITM


@dataclass
class AgoraBacktestTrade:
    """One simulated AGORA trade with full pillar and signal metadata."""

    trade_id:           str
    date_opened:        date
    ticker:             str
    pillar:             str      # vol_premium | directional | event_fomc | event_cpi | catalyst
    strategy:           str      # bull_call_spread | bull_put_spread | iron_condor | etc.
    direction:          str

    # Synthetic spread legs (from BS pricing)
    short_strike:       float
    long_strike:        float
    expiration_date:    date
    dte_at_entry:       int

    # P&L parameters
    entry_credit_debit: float    # per-share (×100 = per contract)
    contracts:          int
    max_loss_dollars:   float
    max_gain_dollars:   float
    profit_target_price: float   # price at which we close for profit
    stop_loss_price:    float    # price at which we stop out

    underlying_at_entry: float
    iv_at_entry:         float   # HV proxy used for strike selection
    conviction_score:    float
    size_multiplier:     float

    # Exit (filled on close)
    date_closed:        date | None = None
    exit_price:         float | None = None
    pnl_dollars:        float | None = None
    commission_dollars: float = 0.0
    status:             AgoraTradeStatus = AgoraTradeStatus.OPEN
    close_reason:       str = ""

    # Capital reserved (max_loss_dollars * contracts — returned on close)
    position_size:      float = 0.0
    # Iron condor call leg strikes (0.0 for single-sided spreads)
    call_short_strike:  float = 0.0
    call_long_strike:   float = 0.0

    # Signal state at entry
    iv_premium_active:  bool = False
    gex_regime:         str = "neutral"
    vol_regime:         str = "normal"
    event_type:         str | None = None

    @property
    def is_open(self) -> bool:
        return self.status == AgoraTradeStatus.OPEN

    @property
    def spread_width(self) -> float:
        return abs(self.short_strike - self.long_strike)


@dataclass
class PillarStats:
    """Per-pillar performance summary."""
    pillar:         str
    trades:         int = 0
    wins:           int = 0
    total_pnl:      float = 0.0
    total_commission: float = 0.0
    best_trade:     float = 0.0
    worst_trade:    float = 0.0

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades > 0 else 0.0

    @property
    def avg_pnl(self) -> float:
        return self.total_pnl / self.trades if self.trades > 0 else 0.0

    @property
    def net_pnl(self) -> float:
        return self.total_pnl - self.total_commission


@dataclass
class AgoraBacktestResult:
    """Complete backtest results with per-pillar attribution."""

    start_date:       date
    end_date:         date
    starting_balance: float
    tickers:          list[str] = field(default_factory=list)

    trades:           list[AgoraBacktestTrade] = field(default_factory=list)
    equity_curve:     list[float] = field(default_factory=list)
    daily_dates:      list[date] = field(default_factory=list)

    @property
    def closed_trades(self) -> list[AgoraBacktestTrade]:
        return [t for t in self.trades if not t.is_open]

    @property
    def winning_trades(self) -> list[AgoraBacktestTrade]:
        return [t for t in self.closed_trades if (t.pnl_dollars or 0) > 0]

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl_dollars or 0 for t in self.closed_trades)

    @property
    def total_commissions(self) -> float:
        return sum(t.commission_dollars for t in self.closed_trades)

    @property
    def total_trades(self) -> int:
        return len(self.trades)

    @property
    def win_rate(self) -> float:
        closed = self.closed_trades
        if not closed:
            return 0.0
        return len(self.winning_trades) / len(closed)

    @property
    def ending_balance(self) -> float:
        return self.starting_balance + self.total_pnl - self.total_commissions

    @property
    def avg_win(self) -> float:
        if not self.winning_trades:
            return 0.0
        return sum(t.pnl_dollars or 0 for t in self.winning_trades) / len(self.winning_trades)

    @property
    def avg_loss(self) -> float:
        losers = [t for t in self.closed_trades if (t.pnl_dollars or 0) <= 0]
        if not losers:
            return 0.0
        return sum(t.pnl_dollars or 0 for t in losers) / len(losers)

    @property
    def profit_factor(self) -> float:
        gross_win  = sum(t.pnl_dollars or 0 for t in self.winning_trades)
        gross_loss = abs(sum(t.pnl_dollars or 0 for t in self.closed_trades if (t.pnl_dollars or 0) <= 0))
        return gross_win / gross_loss if gross_loss > 0 else float("inf")

    @property
    def max_drawdown_pct(self) -> float:
        if not self.equity_curve:
            return 0.0
        peak, max_dd = self.equity_curve[0], 0.0
        for v in self.equity_curve:
            if v > peak:
                peak = v
            if peak > 0:
                max_dd = max(max_dd, (peak - v) / peak * 100)
        return max_dd

    @property
    def sharpe_ratio(self) -> float:
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
        return (mean_r / std) * math.sqrt(252) if std > 0 else 0.0

    def pillar_attribution(self) -> dict[str, PillarStats]:
        stats: dict[str, PillarStats] = {}
        for t in self.closed_trades:
            if t.pillar not in stats:
                stats[t.pillar] = PillarStats(pillar=t.pillar)
            s = stats[t.pillar]
            pnl = t.pnl_dollars or 0
            s.trades += 1
            s.total_pnl += pnl
            s.total_commission += t.commission_dollars
            if pnl > 0:
                s.wins += 1
            s.best_trade  = max(s.best_trade, pnl)
            s.worst_trade = min(s.worst_trade, pnl)
        return stats

    def print_summary(self) -> None:
        print(f"\n{'='*60}")
        print(f"AGORA Walk-Forward Backtest: {self.start_date} → {self.end_date}")
        print(f"Tickers: {', '.join(self.tickers)}")
        print(f"{'='*60}")
        print(f"Starting balance:  ${self.starting_balance:>10,.0f}")
        print(f"Ending balance:    ${self.ending_balance:>10,.0f}")
        print(f"Total P&L:         ${self.total_pnl:>+10,.0f}")
        print(f"Total commissions: ${self.total_commissions:>10,.0f}")
        print(f"Total trades:      {self.total_trades:>10}")
        print(f"Win rate:          {self.win_rate:>9.1%}")
        print(f"Avg win:           ${self.avg_win:>+10,.0f}")
        print(f"Avg loss:          ${self.avg_loss:>+10,.0f}")
        print(f"Profit factor:     {self.profit_factor:>10.2f}")
        print(f"Max drawdown:      {self.max_drawdown_pct:>9.1f}%")
        print(f"Sharpe ratio:      {self.sharpe_ratio:>10.2f}")
        print(f"\n{'─'*60}")
        print("By Pillar:")
        print(f"{'Pillar':<22} {'Trades':>6} {'WR':>6} {'Net P&L':>10} {'PF':>6}")
        print(f"{'─'*22} {'─'*6} {'─'*6} {'─'*10} {'─'*6}")
        for pillar, s in sorted(self.pillar_attribution().items(), key=lambda x: -x[1].net_pnl):
            pf = (
                s.net_pnl / max(0.01, abs(s.worst_trade * max(1, s.trades - s.wins)))
                if s.trades > s.wins else float("inf")
            )
            print(f"{pillar:<22} {s.trades:>6} {s.win_rate:>5.0%} ${s.net_pnl:>+9,.0f} {pf:>6.2f}")
        print(f"{'='*60}\n")

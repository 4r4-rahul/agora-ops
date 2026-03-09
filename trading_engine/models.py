"""
Core data models for the Options Trading Engine.
"""

from dataclasses import dataclass, field
from datetime import datetime, date, time
from enum import Enum
from typing import Optional


# ─────────────────────────────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────────────────────────────

class OptionType(Enum):
    CALL = "CALL"
    PUT = "PUT"


class SpreadType(Enum):
    PUT_CREDIT = "PUT_CREDIT_SPREAD"
    CALL_CREDIT = "CALL_CREDIT_SPREAD"
    IRON_CONDOR = "IRON_CONDOR"
    JADE_LIZARD = "JADE_LIZARD"
    BROKEN_WING_BUTTERFLY = "BROKEN_WING_BUTTERFLY"
    RATIO_SPREAD = "RATIO_SPREAD"
    STRANGLE = "SHORT_STRANGLE"
    STRADDLE = "SHORT_STRADDLE"


class MarketRegime(Enum):
    GREEN = "GREEN"      # Sell premium aggressively
    YELLOW = "YELLOW"    # Sell conservatively, wider strikes
    RED = "RED"          # Sit in cash


class VIXRegime(Enum):
    LOW = "LOW"              # < 15
    NORMAL = "NORMAL"        # 15-20
    ELEVATED = "ELEVATED"    # 20-30
    CRISIS = "CRISIS"        # 30+


class TrendState(Enum):
    STRONG_UPTREND = "STRONG_UPTREND"
    WEAK_UPTREND = "WEAK_UPTREND"
    RANGE_BOUND = "RANGE_BOUND"
    WEAK_DOWNTREND = "WEAK_DOWNTREND"
    STRONG_DOWNTREND = "STRONG_DOWNTREND"


class DayOfWeek(Enum):
    MONDAY = 0
    TUESDAY = 1
    WEDNESDAY = 2
    THURSDAY = 3
    FRIDAY = 4


class TradeStatus(Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    EXPIRED = "EXPIRED"
    STOPPED = "STOPPED"
    ADJUSTED = "ADJUSTED"


# ─────────────────────────────────────────────────────────────────────
# Market Data Models
# ─────────────────────────────────────────────────────────────────────

@dataclass
class MarketSnapshot:
    """Real-time market conditions snapshot."""
    timestamp: datetime = field(default_factory=datetime.now)
    # Index levels
    spx_price: float = 0.0
    spy_price: float = 0.0
    qqq_price: float = 0.0
    iwm_price: float = 0.0
    # Volatility
    vix_level: float = 0.0
    vix_1d_change: float = 0.0
    vix_term_structure: str = "contango"  # contango or backwardation
    vix_futures_front: float = 0.0
    vix_futures_second: float = 0.0
    # Futures
    spx_futures_price: float = 0.0
    spx_futures_overnight_change: float = 0.0
    globex_high: float = 0.0
    globex_low: float = 0.0
    # Volatility metrics
    iv_rank: float = 0.0          # 0-100 percentile
    iv_percentile: float = 0.0    # 0-100 percentile
    realized_vol_20d: float = 0.0
    implied_vol_30d: float = 0.0
    # Breadth
    advance_decline_ratio: float = 1.0
    put_call_ratio: float = 0.80
    new_highs: int = 0
    new_lows: int = 0
    # Expected move
    atm_straddle_price: float = 0.0
    expected_move_1d: float = 0.0
    expected_move_1w: float = 0.0
    expected_move_1m: float = 0.0
    # Previous session
    prev_close: float = 0.0
    prev_high: float = 0.0
    prev_low: float = 0.0
    # Intraday OHLC — actual session data (for backtesting)
    day_open: float = 0.0
    day_high: float = 0.0
    day_low: float = 0.0
    # Economic
    economic_events: list = field(default_factory=list)
    is_fed_day: bool = False
    is_cpi_day: bool = False
    is_opex_day: bool = False
    major_earnings_today: list = field(default_factory=list)


@dataclass
class Greeks:
    """Option greeks."""
    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0
    rho: float = 0.0
    iv: float = 0.0


@dataclass
class OptionContract:
    """Single option contract."""
    symbol: str = ""
    underlying: str = ""
    strike: float = 0.0
    expiration: date = field(default_factory=date.today)
    option_type: OptionType = OptionType.CALL
    bid: float = 0.0
    ask: float = 0.0
    mid: float = 0.0
    last: float = 0.0
    volume: int = 0
    open_interest: int = 0
    greeks: Greeks = field(default_factory=Greeks)
    dte: int = 0

    @property
    def mark(self) -> float:
        return (self.bid + self.ask) / 2 if self.bid and self.ask else self.mid


@dataclass
class OptionsChain:
    """Full options chain for an underlying."""
    underlying: str = ""
    underlying_price: float = 0.0
    expiration: date = field(default_factory=date.today)
    calls: list = field(default_factory=list)   # List[OptionContract]
    puts: list = field(default_factory=list)     # List[OptionContract]


# ─────────────────────────────────────────────────────────────────────
# Trade Structure Models
# ─────────────────────────────────────────────────────────────────────

@dataclass
class SpreadLeg:
    """Single leg of a spread."""
    contract: OptionContract = field(default_factory=OptionContract)
    quantity: int = 0       # Positive = long, negative = short
    is_short: bool = False


@dataclass
class CreditSpread:
    """A credit spread (put or call)."""
    spread_type: SpreadType = SpreadType.PUT_CREDIT
    short_leg: SpreadLeg = field(default_factory=SpreadLeg)
    long_leg: SpreadLeg = field(default_factory=SpreadLeg)
    credit: float = 0.0          # Premium collected per contract
    max_loss: float = 0.0        # Width - credit per contract
    width: float = 0.0           # Distance between strikes
    breakeven: float = 0.0
    probability_otm: float = 0.0 # Probability of expiring OTM
    reward_to_risk: float = 0.0  # Credit / max_loss

    def __post_init__(self):
        if self.short_leg.contract.strike and self.long_leg.contract.strike:
            self.width = abs(self.short_leg.contract.strike - self.long_leg.contract.strike)
            if self.credit > 0:
                self.max_loss = self.width - self.credit
                self.reward_to_risk = self.credit / self.max_loss if self.max_loss > 0 else 0
            if self.spread_type == SpreadType.PUT_CREDIT:
                self.breakeven = self.short_leg.contract.strike - self.credit
            else:
                self.breakeven = self.short_leg.contract.strike + self.credit


@dataclass
class IronCondor:
    """Iron condor = put credit spread + call credit spread."""
    put_spread: CreditSpread = field(default_factory=CreditSpread)
    call_spread: CreditSpread = field(default_factory=CreditSpread)
    total_credit: float = 0.0
    max_loss: float = 0.0
    lower_breakeven: float = 0.0
    upper_breakeven: float = 0.0
    probability_profit: float = 0.0

    def __post_init__(self):
        self.total_credit = self.put_spread.credit + self.call_spread.credit
        wider_spread = max(self.put_spread.width, self.call_spread.width)
        self.max_loss = wider_spread - self.total_credit
        self.lower_breakeven = self.put_spread.breakeven
        self.upper_breakeven = self.call_spread.breakeven


# ─────────────────────────────────────────────────────────────────────
# Trade Ticket & Position Models
# ─────────────────────────────────────────────────────────────────────

@dataclass
class TradeTicket:
    """Complete trade setup ready for execution."""
    id: str = ""
    timestamp: datetime = field(default_factory=datetime.now)
    strategy: str = ""
    underlying: str = "SPX"
    spread_type: SpreadType = SpreadType.PUT_CREDIT
    # Legs
    short_strike: float = 0.0
    long_strike: float = 0.0
    short_strike_call: float = 0.0   # For iron condors
    long_strike_call: float = 0.0    # For iron condors
    expiration: date = field(default_factory=date.today)
    # Pricing
    credit_per_contract: float = 0.0
    max_loss_per_contract: float = 0.0
    num_contracts: int = 1
    total_credit: float = 0.0
    total_max_loss: float = 0.0
    # Probabilities
    probability_of_profit: float = 0.0
    reward_to_risk: float = 0.0
    # Rules
    entry_time_start: str = ""
    entry_time_end: str = ""
    stop_loss_price: float = 0.0     # Close if spread reaches this
    profit_target_price: float = 0.0 # Close at this profit level
    exit_by_time: str = ""           # Hard time exit
    # Context
    market_regime: str = ""
    vix_at_entry: float = 0.0
    spx_at_entry: float = 0.0
    notes: str = ""


@dataclass
class Position:
    """An open position being tracked."""
    ticket: TradeTicket = field(default_factory=TradeTicket)
    status: TradeStatus = TradeStatus.OPEN
    entry_time: datetime = field(default_factory=datetime.now)
    current_value: float = 0.0       # Current mark of the spread
    unrealized_pnl: float = 0.0
    theta_earned: float = 0.0
    highest_profit: float = 0.0
    adjustments: list = field(default_factory=list)


@dataclass
class TradeResult:
    """Completed trade record for journaling."""
    ticket: TradeTicket = field(default_factory=TradeTicket)
    entry_time: datetime = field(default_factory=datetime.now)
    exit_time: datetime = field(default_factory=datetime.now)
    entry_credit: float = 0.0
    exit_debit: float = 0.0
    realized_pnl: float = 0.0
    pnl_per_contract: float = 0.0
    holding_time_minutes: int = 0
    exit_reason: str = ""          # "expired", "profit_target", "stop_loss", "manual"
    strategy: str = ""
    notes: str = ""


# ─────────────────────────────────────────────────────────────────────
# Analysis Report Models
# ─────────────────────────────────────────────────────────────────────

@dataclass
class RegimeReport:
    """Market regime classification output."""
    timestamp: datetime = field(default_factory=datetime.now)
    verdict: MarketRegime = MarketRegime.YELLOW
    vix_regime: VIXRegime = VIXRegime.NORMAL
    vix_term_structure: str = "contango"
    trend_state: TrendState = TrendState.RANGE_BOUND
    rv_vs_iv: str = "neutral"         # "IV_overpriced", "IV_underpriced", "neutral"
    correlation_regime: str = "normal"  # "high", "normal", "low"
    put_call_signal: str = "neutral"
    breadth_signal: str = "neutral"
    event_risk: str = "low"           # "low", "medium", "high"
    strategy_recommendation: str = ""
    details: dict = field(default_factory=dict)


@dataclass
class ThetaReport:
    """Theta decay analysis output."""
    timestamp: datetime = field(default_factory=datetime.now)
    portfolio_theta: float = 0.0
    hourly_decay: dict = field(default_factory=dict)  # hour -> decay $
    daily_income: float = 0.0
    weekly_projection: float = 0.0
    monthly_projection: float = 0.0
    theta_to_delta_ratio: float = 0.0
    gamma_risk_alert: bool = False
    positions: list = field(default_factory=list)


@dataclass
class SkewReport:
    """Volatility skew analysis output."""
    timestamp: datetime = field(default_factory=datetime.now)
    underlying: str = ""
    put_iv_25d: float = 0.0
    call_iv_25d: float = 0.0
    skew_value: float = 0.0
    skew_percentile: float = 0.0
    skew_signal: str = "neutral"  # "steep_fear", "flat_complacent", "inverted"
    recommended_strategies: list = field(default_factory=list)


@dataclass
class PerformanceReport:
    """Monthly performance dashboard output."""
    period_start: date = field(default_factory=date.today)
    period_end: date = field(default_factory=date.today)
    total_premium_collected: float = 0.0
    total_realized_pnl: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    avg_winner: float = 0.0
    avg_loser: float = 0.0
    profit_factor: float = 0.0
    max_drawdown: float = 0.0
    sharpe_estimate: float = 0.0
    theta_available: float = 0.0
    theta_captured: float = 0.0
    best_trade: Optional[TradeResult] = None
    worst_trade: Optional[TradeResult] = None
    strategy_breakdown: dict = field(default_factory=dict)
    daily_pnl: list = field(default_factory=list)
    equity_curve: list = field(default_factory=list)

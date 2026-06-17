"""
AGORA core data models — trades, signals, catalysts, positions.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

# ── Enums ─────────────────────────────────────────────────────────────────────

class StrategyType(StrEnum):
    BULL_CALL_SPREAD    = "bull_call_spread"
    BEAR_PUT_SPREAD     = "bear_put_spread"
    BULL_PUT_SPREAD     = "bull_put_spread"     # credit — sell put spread
    BEAR_CALL_SPREAD    = "bear_call_spread"    # credit — sell call spread
    IRON_CONDOR         = "iron_condor"
    IRON_BUTTERFLY      = "iron_butterfly"
    CALENDAR_SPREAD     = "calendar_spread"
    LONG_CALL           = "long_call"
    LONG_PUT            = "long_put"
    CASH_SECURED_PUT    = "cash_secured_put"
    NAKED_PUT           = "naked_put"           # sell uncovered put — theta + vol premium
    NAKED_CALL          = "naked_call"          # sell uncovered call — directional/vol


class StrategyPillar(StrEnum):
    VOL_PREMIUM       = "vol_premium"       # IV > realized → sell credit spreads
    DIRECTIONAL       = "directional"       # GEX negative + momentum → debit spreads
    EVENT_FOMC        = "event_fomc"        # FOMC T-5 drift play
    EVENT_CPI         = "event_cpi"         # CPI T-1 IV premium condor
    POST_EARNINGS     = "post_earnings"     # Skew reversion T+1 to T+3
    CATALYST          = "catalyst"          # EDGAR 8-K discovery
    CONGRESSIONAL     = "congressional"     # Congressional trade signal
    SMART_MONEY       = "smart_money"       # 13D/13G/Form4 cluster
    SECTOR_MOMENTUM   = "sector_momentum"   # 3+ peers moving >3% same direction intraday


class CatalystType(StrEnum):
    EARNINGS_BEAT       = "earnings_beat"
    EARNINGS_MISS       = "earnings_miss"
    CONTRACT_WIN        = "contract_win"
    FUNDING_ROUND       = "funding_round"
    FDA_APPROVAL        = "fda_approval"
    FDA_REJECTION       = "fda_rejection"
    ACTIVIST_13D        = "activist_13d"
    INSIDER_CLUSTER     = "insider_cluster"
    MA_TARGET           = "ma_target"
    PARTNERSHIP         = "partnership"
    GUIDANCE_RAISE      = "guidance_raise"
    GUIDANCE_CUT        = "guidance_cut"


class Regime(StrEnum):
    LOW_VOL     = "low_volatility"
    NORMAL      = "normal"
    HIGH_VOL    = "high_volatility"
    CRISIS      = "crisis"


class GexRegime(StrEnum):
    POSITIVE = "positive"   # dealers net long gamma → mean-reversion
    NEGATIVE = "negative"   # dealers net short gamma → trending/amplifying
    NEUTRAL  = "neutral"


class PositionStatus(StrEnum):
    OPEN        = "open"
    TESTED      = "tested"     # underlying through short strike
    ROLLED      = "rolled"
    CLOSED      = "closed"
    EXPIRED     = "expired"
    ASSIGNED    = "assigned"


# ── Signal models ──────────────────────────────────────────────────────────────

class VolRegimeSignal(BaseModel):
    regime: Regime
    confidence: float = Field(ge=0.0, le=1.0)
    iv_rank: float | None = None
    atm_iv: float | None = None
    vix: float | None = None
    vix_vix3m_ratio: float | None = None
    hv10_hv30_ratio: float | None = None


class IvPremiumSignal(BaseModel):
    ticker: str
    atm_iv_30d: float
    hv_21d: float
    premium_ratio: float          # (atm_iv - hv) / hv
    days_above_threshold: int
    signal_active: bool           # True if premium_ratio > threshold for min_days


class GexSignal(BaseModel):
    ticker: str
    gex_total: float
    regime: GexRegime
    dominant_strike: float | None = None
    flip_level: float | None = None


class MomentumSignal(BaseModel):
    ticker: str
    price: float
    sma20: float | None = None
    sma50: float | None = None
    rsi14: float | None = None
    score: float = Field(ge=0.0, le=1.0)    # 0=bearish, 0.5=neutral, 1=bullish
    direction: str = "neutral"               # "bullish" | "bearish" | "neutral"


class EventSignal(BaseModel):
    event_type: str          # "fomc_drift" | "cpi_condor" | "post_earnings_skew"
    ticker: str
    days_to_event: int
    direction: str           # "bullish" | "bearish" | "neutral"
    confidence: float = Field(ge=0.0, le=1.0)


# ── Conviction model ───────────────────────────────────────────────────────────

class ConvictionScore(BaseModel):
    session_id: str
    ticker: str
    total_score: float = Field(ge=0.0, le=100.0)
    size_multiplier: float = 1.0    # from Disagreement Resolver: 0.5 | 1.0 | 1.5

    # Component scores
    vol_premium_score:  float = 0.0   # /30
    gex_score:          float = 0.0   # /20
    regime_score:       float = 0.0   # /20
    event_score:        float = 0.0   # /15
    macro_score:        float = 0.0   # /5
    smart_money_score:  float = 0.0   # /5
    info_speed_score:   float = 0.0   # /5

    gate: str = "no_trade"           # "high" | "standard" | "low" | "no_trade"
    reasoning: str = ""
    pillar: StrategyPillar | None = None


# ── Catalyst models ────────────────────────────────────────────────────────────

class Catalyst(BaseModel):
    ticker: str
    catalyst_type: CatalystType
    filing_time: datetime
    headline: str
    direction: str                   # "bullish" | "bearish" | "neutral"
    strength: str                    # "strong" | "moderate" | "weak"
    market_cap: float | None = None
    contract_value: float | None = None
    funding_amount: float | None = None
    derivative_tickers: list[str] = Field(default_factory=list)
    raw_text: str = ""
    source: str = ""                 # "edgar_8k" | "pr_newswire" | "globenewswire"


# ── Trade models ───────────────────────────────────────────────────────────────

class SpreadLeg(BaseModel):
    option_type: str           # "call" | "put"
    strike: float
    expiration: date
    action: str                # "buy" | "sell"
    contracts: int = 1
    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0
    mid_price: float = 0.0


class TradeRecommendation(BaseModel):
    session_id: str
    ticker: str
    strategy: StrategyType
    pillar: StrategyPillar
    direction: str

    legs: list[SpreadLeg]
    contracts: int = 1

    entry_debit_credit: float       # positive = debit paid, negative = credit received
    max_loss_dollars: float
    max_gain_dollars: float
    reward_risk_ratio: float
    breakeven_price: float | None = None

    stop_loss_pct: float = 2.0      # exit if position down 2x initial debit/credit
    target_dte_close: int = 21

    conviction_score: float = 0.0
    size_multiplier: float = 1.0
    reasoning: str = ""
    # Quantified macro-event (FOMC/CPI/NFP) context set by the surgical event gate and read
    # by the advocate, so it reasons on real event/days/expected-move instead of assumptions.
    event_mitigation: str = ""
    # Entry IVR, set on long-options recs so the advocate can recognise a low-IVR long-vega single
    # leg (where "IV crush" is the wrong risk). Optional field — BaseModel forbids ad-hoc attrs.
    entry_ivr: float = 0.0
    timestamp: datetime = Field(default_factory=datetime.utcnow)


# ── Position lifecycle ─────────────────────────────────────────────────────────

class OpenPosition(BaseModel):
    position_id: str
    ticker: str
    strategy: StrategyType
    pillar: StrategyPillar
    direction: str = "neutral"           # "bullish" | "bearish" | "neutral"
    status: PositionStatus = PositionStatus.OPEN

    legs: list[SpreadLeg]
    contracts: int
    entry_price: float
    current_price: float = 0.0
    entry_date: date
    expiry_date: date
    target_close_date: date         # 21 DTE from entry

    max_loss_dollars: float
    max_gain_dollars: float
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0

    rolled_count: int = 0
    last_reviewed: datetime = Field(default_factory=datetime.utcnow)
    ibkr_order_ids: list[int] = Field(default_factory=list)
    notes: str = ""

    # Decision context — stored at entry, read back at close for attribution
    conviction_at_entry: float = 0.0
    regime_at_entry: str = ""          # macro_stance at time of entry

    # Precise broker (TWS) fill timestamps — exact execution time, to the second (NOT just a date).
    entry_ts_utc: str = ""             # f.execution.time of the entry fill; exit set on close.

    # Pre-earnings tracking — set for positions opened T-7 to T-1 before earnings
    earnings_date: date | None = None  # the actual earnings date
    is_pre_earnings: bool = False      # True → close T-1 to avoid IV crush


# ── Performance attribution ────────────────────────────────────────────────────

class TradeRecord(BaseModel):
    trade_id: str
    ticker: str
    strategy: StrategyType
    pillar: StrategyPillar

    entry_date: date
    close_date: date | None = None
    expiry_date: date

    entry_price: float
    close_price: float | None = None
    contracts: int

    realized_pnl: float | None = None
    commission: float = 0.0
    slippage: float = 0.0          # expected_mid - actual_fill

    regime_at_entry: Regime | None = None
    conviction_at_entry: float | None = None
    signal_hash: str = ""          # hash of input signals for drift analysis
    notes: str = ""

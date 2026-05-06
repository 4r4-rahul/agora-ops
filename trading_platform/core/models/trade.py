"""Trade models — recommendations, journal entries, execution records."""

from __future__ import annotations

from datetime import date, datetime, timezone
from enum import StrEnum

def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class Direction(StrEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"
    VOLATILE = "volatile"


class OptionsStrategyType(StrEnum):
    LONG_CALL = "long_call"
    LONG_PUT = "long_put"
    BULL_CALL_SPREAD = "bull_call_spread"
    BEAR_PUT_SPREAD = "bear_put_spread"
    IRON_CONDOR = "iron_condor"
    IRON_BUTTERFLY = "iron_butterfly"
    STRANGLE = "strangle"
    STRADDLE = "straddle"
    COVERED_CALL = "covered_call"
    CASH_SECURED_PUT = "cash_secured_put"
    CALENDAR_SPREAD = "calendar_spread"
    DIAGONAL_SPREAD = "diagonal_spread"


class TradeDecision(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    WATCHLIST = "WATCHLIST"
    PENDING_APPROVAL = "PENDING_APPROVAL"


class SpreadLeg(BaseModel):
    """One leg of a multi-leg options strategy."""

    option_type: str
    strike: float
    expiration: date
    action: str
    quantity: int = 1
    bid: float = 0.0
    ask: float = 0.0

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


class TradeRecommendation(BaseModel):
    """
    Complete, self-contained trade recommendation.
    Every field must be populated — agents should never emit a partial recommendation.
    """

    model_config = ConfigDict(validate_assignment=False)

    id: UUID = Field(default_factory=uuid4)
    created_at: datetime = Field(default_factory=_utcnow)
    session_id: str

    # ── What & Why ─────────────────────────────────────────────────
    ticker: str
    direction: Direction
    thesis: str = Field(..., min_length=20, description="Market thesis in plain English")

    # ── Strategy ───────────────────────────────────────────────────
    strategy: OptionsStrategyType
    expiration: date
    legs: list[SpreadLeg] = Field(default_factory=list)
    strike_selection_logic: str = Field(
        ..., description="Why these specific strikes were chosen"
    )

    # ── Execution ──────────────────────────────────────────────────
    entry_trigger: str = Field(..., description="Specific price/signal condition to enter")
    entry_price: float = Field(..., description="Net debit or credit at mid")
    stop_loss: float = Field(..., description="Price at which position is closed for a loss")
    stop_loss_logic: str
    profit_target: float = Field(..., description="Price at which partial or full profit is taken")
    profit_target_logic: str

    # ── Risk ───────────────────────────────────────────────────────
    max_loss_dollars: float = Field(..., gt=0)
    max_gain_dollars: float = Field(..., gt=0)
    reward_risk_ratio: float = Field(..., gt=0)
    contracts: int = Field(default=1, ge=1)
    position_size_dollars: float = Field(..., gt=0)

    # ── Checks ─────────────────────────────────────────────────────
    liquidity_ok: bool
    liquidity_notes: str = ""
    iv_rank: float | None = None
    iv_rank_ok: bool = True
    iv_crush_risk: str = Field(
        default="none", description="none | low | moderate | high | extreme"
    )
    earnings_risk: str = Field(
        default="none", description="none | within_expiry | day_before | same_day"
    )
    regime_confirms: bool = True
    regime_notes: str = ""

    # ── Agents' verdicts ───────────────────────────────────────────
    risk_assessment_summary: str = ""
    reviewer_notes: str = ""
    final_decision: TradeDecision = TradeDecision.PENDING_APPROVAL
    rejection_reasons: list[str] = Field(default_factory=list)

    # ── Human approval ─────────────────────────────────────────────
    approved_by: str | None = None
    approved_at: datetime | None = None

    def approve(self, user: str = "human") -> None:
        self.final_decision = TradeDecision.APPROVED
        self.approved_by = user
        self.approved_at = datetime.now(timezone.utc).replace(tzinfo=None)

    def reject(self, reasons: list[str]) -> None:
        self.final_decision = TradeDecision.REJECTED
        self.rejection_reasons = reasons


class TradeStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class TradeJournalEntry(BaseModel):
    """Persistent record of a trade from entry to exit."""

    id: UUID = Field(default_factory=uuid4)
    recommendation_id: UUID
    session_id: str
    ticker: str
    strategy: OptionsStrategyType
    direction: Direction
    opened_at: datetime = Field(default_factory=_utcnow)
    closed_at: datetime | None = None
    status: TradeStatus = TradeStatus.OPEN

    # Entry
    entry_price: float
    contracts: int
    position_size_dollars: float
    max_loss_dollars: float
    max_gain_dollars: float
    stop_loss: float
    profit_target: float

    # Exit
    exit_price: float | None = None
    realized_pnl: float | None = None
    exit_reason: str | None = None

    # Notes
    thesis: str = ""
    lessons_learned: str = ""
    tags: list[str] = Field(default_factory=list)
    raw_recommendation: dict[str, Any] = Field(default_factory=dict)

    def close(self, exit_price: float, reason: str) -> None:
        self.exit_price = exit_price
        self.closed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        self.status = TradeStatus.CLOSED
        self.exit_reason = reason
        # P&L: positive for debit spread means price went up; negative = loss
        self.realized_pnl = (exit_price - self.entry_price) * self.contracts * 100

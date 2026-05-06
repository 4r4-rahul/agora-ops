"""Agent communication models — message bus types and session state."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class AgentTopic(StrEnum):
    """All message topics that flow through the message bus."""

    ANALYSIS_REQUEST = "analysis.request"
    MARKET_DATA_RESULT = "market_data.result"
    REGIME_RESULT = "regime.result"
    TECHNICAL_RESULT = "technical.result"
    NEWS_RESULT = "news.result"
    STRATEGY_CANDIDATES = "strategy.candidates"
    RISK_ASSESSMENT = "risk.assessment"
    REVIEW_RESULT = "review.result"
    RECOMMENDATION_READY = "recommendation.ready"
    EXECUTION_STATUS = "execution.status"
    POSITION_UPDATE = "position.update"
    ALERT = "alert"
    ERROR = "error"


class AgentStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    WAITING = "waiting"
    ERROR = "error"
    STOPPED = "stopped"


class AgentMessage(BaseModel):
    """Typed envelope for all inter-agent communication."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    topic: AgentTopic
    session_id: str
    sender: str
    payload: dict[str, Any]
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    correlation_id: str | None = None

    @classmethod
    def create(
        cls,
        topic: AgentTopic,
        session_id: str,
        sender: str,
        payload: dict[str, Any],
        correlation_id: str | None = None,
    ) -> "AgentMessage":
        return cls(
            topic=topic,
            session_id=session_id,
            sender=sender,
            payload=payload,
            correlation_id=correlation_id,
        )


class AnalysisRequest(BaseModel):
    """Payload for ANALYSIS_REQUEST messages."""

    ticker: str
    tickers: list[str] = Field(default_factory=list)
    force_refresh: bool = False
    context: dict[str, Any] = Field(default_factory=dict)


class MarketRegime(StrEnum):
    BULL_TREND = "bull_trend"
    BEAR_TREND = "bear_trend"
    RANGING = "ranging"
    HIGH_VOLATILITY = "high_volatility"
    LOW_VOLATILITY = "low_volatility"
    CRISIS = "crisis"


class RegimeResult(BaseModel):
    """Payload for REGIME_RESULT messages."""

    regime: MarketRegime
    confidence: float = Field(..., ge=0.0, le=1.0)
    vix: float | None = None
    vix_trend: str = "flat"
    trend_direction: str = "neutral"
    breadth_score: float = 0.0
    reasoning: str = ""
    regime_suitable_strategies: list[str] = Field(default_factory=list)


class TechnicalResult(BaseModel):
    """Payload for TECHNICAL_RESULT messages."""

    ticker: str
    signal: str
    signal_strength: float = Field(..., ge=0.0, le=1.0)
    support_levels: list[float] = Field(default_factory=list)
    resistance_levels: list[float] = Field(default_factory=list)
    key_levels: dict[str, float] = Field(default_factory=dict)
    indicators: dict[str, float] = Field(default_factory=dict)
    notes: str = ""


class NewsResult(BaseModel):
    """Payload for NEWS_RESULT messages."""

    ticker: str
    sentiment: str
    sentiment_score: float = Field(..., ge=-1.0, le=1.0)
    catalyst_type: str = "none"
    has_earnings_risk: bool = False
    earnings_date: str | None = None
    headline_count: int = 0
    key_headlines: list[str] = Field(default_factory=list)
    summary: str = ""


class AnalysisSession(BaseModel):
    """Complete state of one analysis session (one ticker, one pipeline run)."""

    session_id: str = Field(default_factory=lambda: str(uuid4()))
    ticker: str
    requested_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: datetime | None = None
    status: str = "pending"
    error: str | None = None

    # Results accumulated as pipeline runs
    market_snapshot: dict[str, Any] | None = None
    regime_result: dict[str, Any] | None = None
    technical_result: dict[str, Any] | None = None
    news_result: dict[str, Any] | None = None
    strategy_candidates: list[dict[str, Any]] = Field(default_factory=list)
    risk_assessment: dict[str, Any] | None = None
    review_result: dict[str, Any] | None = None
    final_recommendation: dict[str, Any] | None = None

    def mark_complete(self) -> None:
        self.status = "complete"
        self.completed_at = datetime.utcnow()

    def mark_failed(self, error: str) -> None:
        self.status = "failed"
        self.error = error
        self.completed_at = datetime.utcnow()

"""Risk and Greeks models."""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel, Field


class Greeks(BaseModel):
    """Option Greeks for a single contract or spread."""

    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0
    rho: float = 0.0
    implied_vol: float = 0.0

    def __add__(self, other: "Greeks") -> "Greeks":
        return Greeks(
            delta=self.delta + other.delta,
            gamma=self.gamma + other.gamma,
            theta=self.theta + other.theta,
            vega=self.vega + other.vega,
            rho=self.rho + other.rho,
            implied_vol=(self.implied_vol + other.implied_vol) / 2,
        )


class LiquidityCheck(BaseModel):
    """Liquidity assessment for an option or spread."""

    ticker: str
    passed: bool
    open_interest: int = 0
    volume: int = 0
    bid_ask_spread_pct: float = 0.0
    bid_ask_spread_dollars: float = 0.0
    notes: str = ""

    MIN_OPEN_INTEREST: ClassVar[int] = 100
    MIN_VOLUME: ClassVar[int] = 50
    MAX_SPREAD_PCT: ClassVar[float] = 0.15

    @classmethod
    def evaluate(
        cls,
        ticker: str,
        open_interest: int,
        volume: int,
        bid: float,
        ask: float,
    ) -> "LiquidityCheck":
        mid = (bid + ask) / 2
        spread_pct = (ask - bid) / mid if mid > 0 else float("inf")
        spread_dollars = ask - bid
        passed = (
            open_interest >= cls.MIN_OPEN_INTEREST
            and volume >= cls.MIN_VOLUME
            and spread_pct <= cls.MAX_SPREAD_PCT
        )
        notes_parts = []
        if open_interest < cls.MIN_OPEN_INTEREST:
            notes_parts.append(f"OI {open_interest} < {cls.MIN_OPEN_INTEREST}")
        if volume < cls.MIN_VOLUME:
            notes_parts.append(f"vol {volume} < {cls.MIN_VOLUME}")
        if spread_pct > cls.MAX_SPREAD_PCT:
            notes_parts.append(f"spread {spread_pct:.1%} > {cls.MAX_SPREAD_PCT:.1%}")

        return cls(
            ticker=ticker,
            passed=passed,
            open_interest=open_interest,
            volume=volume,
            bid_ask_spread_pct=spread_pct,
            bid_ask_spread_dollars=spread_dollars,
            notes="; ".join(notes_parts) if notes_parts else "OK",
        )


class IVAnalysis(BaseModel):
    """IV analysis for a ticker."""

    ticker: str
    current_iv: float
    iv_rank: float = Field(..., ge=0.0, le=100.0, description="0–100 percentile of current IV")
    iv_percentile: float = Field(..., ge=0.0, le=100.0)
    hist_vol_30: float = 0.0
    iv_hv_ratio: float = 0.0
    iv_crush_risk: Literal["none", "low", "moderate", "high", "extreme"] = "none"
    notes: str = ""

    @property
    def is_elevated(self) -> bool:
        return self.iv_rank > 50

    @property
    def is_compressed(self) -> bool:
        return self.iv_rank < 30


class PositionSizing(BaseModel):
    """Kelly / fixed-fraction position sizing result."""

    ticker: str
    max_position_dollars: float
    recommended_contracts: int
    max_loss_per_contract: float
    total_max_loss: float
    account_risk_pct: float
    notes: str = ""

    @classmethod
    def calculate(
        cls,
        ticker: str,
        max_loss_per_contract: float,
        account_size: float,
        max_position_pct: float = 0.05,
        max_risk_pct: float = 0.02,
    ) -> "PositionSizing":
        max_position_dollars = account_size * max_position_pct
        max_risk_dollars = account_size * max_risk_pct

        if max_loss_per_contract <= 0:
            contracts = 0
        else:
            contracts_by_position = int(max_position_dollars / (max_loss_per_contract * 100))
            contracts_by_risk = int(max_risk_dollars / max_loss_per_contract)
            contracts = max(1, min(contracts_by_position, contracts_by_risk))

        total_max_loss = contracts * max_loss_per_contract
        account_risk_pct = total_max_loss / account_size * 100

        return cls(
            ticker=ticker,
            max_position_dollars=max_position_dollars,
            recommended_contracts=contracts,
            max_loss_per_contract=max_loss_per_contract,
            total_max_loss=total_max_loss,
            account_risk_pct=account_risk_pct,
        )


class VetoReason(BaseModel):
    code: str
    description: str
    severity: Literal["warning", "reject"]


class RiskAssessment(BaseModel):
    """Full risk assessment for a trade candidate."""

    session_id: str
    ticker: str
    approved: bool
    veto_reasons: list[VetoReason] = Field(default_factory=list)
    liquidity: LiquidityCheck | None = None
    iv_analysis: IVAnalysis | None = None
    position_sizing: PositionSizing | None = None
    net_greeks: Greeks | None = None
    max_loss_dollars: float = 0.0
    max_gain_dollars: float = 0.0
    reward_risk_ratio: float = 0.0
    portfolio_delta: float = 0.0
    correlation_risk: str = "unknown"
    summary: str = ""

    def add_veto(self, code: str, description: str, severity: str = "reject") -> None:
        self.veto_reasons.append(VetoReason(code=code, description=description, severity=severity))
        if severity == "reject":
            self.approved = False

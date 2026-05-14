"""Risk and Greeks models."""

from __future__ import annotations

import math
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


def compute_spread_greeks(
    underlying: float,
    legs: list[dict],
    implied_vol: float,
    dte_remaining: int,
    risk_free_rate: float = 0.04,
) -> "Greeks":
    """
    Black-Scholes Greeks for a multi-leg spread.

    legs: list of dicts with keys: action (buy|sell), strike (float), option_type (call|put)
    implied_vol: annualised σ decimal (e.g. 0.25 for 25%)
    dte_remaining: calendar days to expiration
    """
    from scipy.stats import norm  # lazy import — keeps startup fast when scipy unused

    T = max(dte_remaining, 0) / 365.0
    if T <= 0 or implied_vol <= 0 or underlying <= 0 or not legs:
        return Greeks()

    S, r, sigma = underlying, risk_free_rate, implied_vol
    sqrtT = math.sqrt(T)

    agg_delta = agg_gamma = agg_theta = agg_vega = 0.0

    for leg in legs:
        try:
            K = float(leg.get("strike", S))
            is_call = str(leg.get("option_type", "call")).lower().startswith("c")
            sign = 1.0 if str(leg.get("action", "buy")).lower() == "buy" else -1.0

            if K <= 0:
                continue

            d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * sqrtT)
            d2 = d1 - sigma * sqrtT
            pdf_d1 = norm.pdf(d1)

            if is_call:
                delta = norm.cdf(d1)
                theta = (
                    -S * pdf_d1 * sigma / (2 * sqrtT)
                    - r * K * math.exp(-r * T) * norm.cdf(d2)
                ) / 365
            else:
                delta = norm.cdf(d1) - 1.0
                theta = (
                    -S * pdf_d1 * sigma / (2 * sqrtT)
                    + r * K * math.exp(-r * T) * norm.cdf(-d2)
                ) / 365

            gamma = pdf_d1 / (S * sigma * sqrtT)
            vega = S * pdf_d1 * sqrtT / 100  # per 1% IV move

            agg_delta += sign * delta
            agg_gamma += sign * gamma
            agg_theta += sign * theta
            agg_vega += sign * vega
        except Exception:
            continue

    return Greeks(
        delta=agg_delta,
        gamma=agg_gamma,
        theta=agg_theta,
        vega=agg_vega,
        implied_vol=sigma,
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
        kelly_fraction: float = 0.0,
    ) -> "PositionSizing":
        """
        Calculate position sizing.

        If kelly_fraction > 0 (from performance_feedback.json), use half-Kelly
        to dynamically scale position size: size up during winning periods,
        size down during drawdowns.  Hard-capped at max_position_pct (5%).
        Floor at 1% to avoid sizing to zero even in poor-performance phases.
        """
        if kelly_fraction > 0:
            # Half-Kelly: divide by 2 for safety, cap at configured max
            half_kelly_pct = kelly_fraction / 2.0
            effective_position_pct = max(0.01, min(max_position_pct, half_kelly_pct))
            notes = f"Kelly-adjusted: half_kelly={half_kelly_pct:.1%} → effective={effective_position_pct:.1%}"
        else:
            effective_position_pct = max_position_pct
            notes = f"Fixed sizing: {effective_position_pct:.1%} of account"

        max_position_dollars = account_size * effective_position_pct
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
            notes=notes,
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

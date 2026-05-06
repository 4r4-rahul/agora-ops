"""Market data models — immutable snapshots passed between agents."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class Bar(BaseModel):
    """Single OHLCV bar."""

    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int

    @model_validator(mode="after")
    def check_ohlc(self) -> "Bar":
        if self.high < self.low:
            raise ValueError("high must be >= low")
        return self


class MarketSnapshot(BaseModel):
    """Complete market context for one ticker at one point in time."""

    ticker: str
    timestamp: datetime
    price: float
    volume: int
    vwap: float | None = None
    day_open: float | None = None
    day_high: float | None = None
    day_low: float | None = None
    prev_close: float | None = None
    atr_14: float | None = None
    rsi_14: float | None = None
    sma_20: float | None = None
    sma_50: float | None = None
    sma_200: float | None = None
    vix: float | None = None
    iv_rank: float | None = None
    iv_percentile: float | None = None
    hist_vol_30: float | None = None
    bars_1m: list[Bar] = Field(default_factory=list)
    bars_5m: list[Bar] = Field(default_factory=list)
    bars_15m: list[Bar] = Field(default_factory=list)
    bars_daily: list[Bar] = Field(default_factory=list)

    @property
    def change_pct(self) -> float | None:
        if self.prev_close and self.prev_close > 0:
            return (self.price - self.prev_close) / self.prev_close * 100
        return None


class OptionContract(BaseModel):
    """Single option contract with market data."""

    ticker: str
    expiration: date
    strike: float
    option_type: Literal["call", "put"]
    bid: float
    ask: float
    last: float = 0.0
    volume: int = 0
    open_interest: int = 0
    implied_vol: float = 0.0
    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0
    rho: float = 0.0

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def spread_pct(self) -> float:
        if self.mid > 0:
            return self.spread / self.mid
        return float("inf")

    @property
    def dte(self) -> int:
        return (self.expiration - date.today()).days

    @property
    def is_liquid(self) -> bool:
        return (
            self.open_interest >= 100
            and self.volume >= 50
            and self.spread_pct <= 0.15
        )


class OptionsChain(BaseModel):
    """Full options chain for a ticker at a given expiration."""

    ticker: str
    underlying_price: float
    timestamp: datetime
    expiration: date
    calls: list[OptionContract] = Field(default_factory=list)
    puts: list[OptionContract] = Field(default_factory=list)

    @property
    def dte(self) -> int:
        return (self.expiration - date.today()).days

    def atm_call(self, n: int = 1) -> OptionContract | None:
        """Return the n-th call closest to ATM."""
        sorted_calls = sorted(self.calls, key=lambda c: abs(c.strike - self.underlying_price))
        return sorted_calls[n - 1] if len(sorted_calls) >= n else None

    def atm_put(self, n: int = 1) -> OptionContract | None:
        sorted_puts = sorted(self.puts, key=lambda p: abs(p.strike - self.underlying_price))
        return sorted_puts[n - 1] if len(sorted_puts) >= n else None

    def get_call(self, strike: float) -> OptionContract | None:
        return next((c for c in self.calls if c.strike == strike), None)

    def get_put(self, strike: float) -> OptionContract | None:
        return next((p for p in self.puts if p.strike == strike), None)

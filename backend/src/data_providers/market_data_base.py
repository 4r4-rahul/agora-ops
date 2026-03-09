"""Shared typing + helpers for streaming options market data providers."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Literal, Optional, Protocol

OptionRight = Literal["call", "put"]


def monotonic_ts() -> float:
    """Return unix timestamp with second precision for cache tagging."""
    return time.time()


@dataclass(slots=True)
class OptionContract:
    symbol: str
    expiry: str
    strike: float
    right: OptionRight

    def cache_key(self) -> str:
        return f"{self.symbol}:{self.expiry}:{self.strike}:{self.right}"


@dataclass(slots=True)
class GreeksSnapshot:
    contract: OptionContract
    iv: float
    delta: float
    gamma: float
    theta: float
    vega: float
    rho: float
    timestamp: float = field(default_factory=monotonic_ts)


@dataclass(slots=True)
class OptionQuote:
    contract: OptionContract
    bid: float
    ask: float
    mark: float
    last: float
    open_interest: int
    volume: int
    greeks: Optional[GreeksSnapshot] = None
    midpoint_iv: Optional[float] = None
    timestamp: float = field(default_factory=monotonic_ts)


@dataclass(slots=True)
class OptionsChain:
    symbol: str
    expiry: Optional[str]
    quotes: List[OptionQuote]
    fetched_at: float = field(default_factory=monotonic_ts)
    provider_meta: Dict[str, str] = field(default_factory=dict)


class MarketDataProvider(Protocol):
    """Protocol every market data provider implementation must satisfy."""

    name: str

    async def connect(self) -> None:
        ...

    async def disconnect(self) -> None:
        ...

    def is_connected(self) -> bool:
        ...

    async def warmup(self) -> None:
        """Optional prefetch before serving requests."""

    async def get_options_chain(self, symbol: str, expiry: Optional[str] = None) -> OptionsChain:
        ...

    async def get_greeks(self, contract: OptionContract) -> GreeksSnapshot:
        ...


class ProviderError(RuntimeError):
    """Internal provider failure wrapper so FastAPI can map to HTTP errors."""

    def __init__(self, provider: str, message: str):
        super().__init__(f"[{provider}] {message}")


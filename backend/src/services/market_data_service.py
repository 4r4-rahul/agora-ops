"""Caching + orchestration layer for market data providers."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, Optional, Tuple

from ..data_providers.market_data_base import (
    MarketDataProvider,
    OptionsChain,
    OptionContract,
    GreeksSnapshot,
    ProviderError,
)

logger = logging.getLogger(__name__)


class MarketDataService:
    def __init__(
        self,
        provider: MarketDataProvider,
        cache_ttl: float = 3.0,
        stale_tolerance: float = 15.0,
    ) -> None:
        self.provider = provider
        self.cache_ttl = cache_ttl
        self.stale_tolerance = stale_tolerance
        self._chain_cache: Dict[str, Tuple[OptionsChain, float]] = {}
        self._greeks_cache: Dict[str, Tuple[GreeksSnapshot, float]] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    async def startup(self) -> None:
        await self.provider.connect()
        await self.provider.warmup()
        logger.info("Market data provider %s ready", self.provider.name)

    async def shutdown(self) -> None:
        await self.provider.disconnect()
        logger.info("Market data provider %s disconnected", self.provider.name)

    def _cache_key(self, symbol: str, expiry: Optional[str]) -> str:
        return f"{symbol.upper()}:{expiry or 'ALL'}"

    def _lock_for(self, key: str) -> asyncio.Lock:
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    def _is_fresh(self, ts: float) -> bool:
        return (time.time() - ts) <= self.cache_ttl

    def _is_within_tolerance(self, ts: float) -> bool:
        return (time.time() - ts) <= self.stale_tolerance

    async def get_options_chain(
        self,
        symbol: str,
        expiry: Optional[str] = None,
        force_refresh: bool = False,
    ) -> OptionsChain:
        key = self._cache_key(symbol, expiry)
        cached = self._chain_cache.get(key)
        if cached and (self._is_fresh(cached[1]) or not force_refresh):
            if self._is_fresh(cached[1]):
                return cached[0]
            if self._is_within_tolerance(cached[1]):
                logger.debug("Serving slightly stale chain for %s while refreshing", key)
        lock = self._lock_for(key)
        async with lock:
            cached = self._chain_cache.get(key)
            if cached and self._is_fresh(cached[1]):
                return cached[0]
            chain = await self.provider.get_options_chain(symbol, expiry)
            self._chain_cache[key] = (chain, time.time())
            return chain

    async def get_greeks(self, contract: OptionContract, force_refresh: bool = False) -> GreeksSnapshot:
        key = contract.cache_key()
        cached = self._greeks_cache.get(key)
        if cached and (self._is_fresh(cached[1]) or not force_refresh):
            if self._is_fresh(cached[1]):
                return cached[0]
            if self._is_within_tolerance(cached[1]):
                logger.debug("Serving slightly stale greeks for %s while refreshing", key)
        lock = self._lock_for(key)
        async with lock:
            cached = self._greeks_cache.get(key)
            if cached and self._is_fresh(cached[1]):
                return cached[0]
            snapshot = await self.provider.get_greeks(contract)
            self._greeks_cache[key] = (snapshot, time.time())
            return snapshot

    def connection_state(self) -> Dict[str, str]:
        return {
            "provider": self.provider.name,
            "connected": str(self.provider.is_connected()),
            "cache_items": str(len(self._chain_cache) + len(self._greeks_cache)),
        }

    def get_cached_chain(self, symbol: str, expiry: Optional[str] = None) -> Optional[OptionsChain]:
        key = self._cache_key(symbol, expiry)
        item = self._chain_cache.get(key)
        if not item:
            return None
        if not self._is_within_tolerance(item[1]):
            return None
        return item[0]

    def get_cached_greeks(self, contract: OptionContract) -> Optional[GreeksSnapshot]:
        key = contract.cache_key()
        item = self._greeks_cache.get(key)
        if not item:
            return None
        if not self._is_within_tolerance(item[1]):
            return None
        return item[0]


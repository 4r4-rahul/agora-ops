"""IBKR-backed market data provider stub with session lifecycle management."""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence

try:
    import ib_insync  # type: ignore
except Exception:  # pragma: no cover - deferred optional dependency
    ib_insync = None

from .market_data_base import (
    MarketDataProvider,
    OptionContract,
    OptionsChain,
    GreeksSnapshot,
    OptionQuote,
    ProviderError,
)

logger = logging.getLogger(__name__)


def _env(name: str, default: Optional[str] = None) -> str:
    value = os.getenv(name, default)
    if value is None:
        raise RuntimeError(f"Missing required environment variable {name}")
    return value


@dataclass(slots=True)
class IBKRConfig:
    host: str
    port: int
    client_id: int
    account: Optional[str]
    request_timeout: float = 10.0
    max_strikes: int = 5
    max_contracts: int = 20
    currency: str = "USD"
    exchange: str = "SMART"

    @classmethod
    def from_env(cls) -> "IBKRConfig":
        host = _env("IBKR_HOST", "127.0.0.1")
        port = int(_env("IBKR_PORT", "7497"))
        client_id = int(_env("IBKR_CLIENT_ID", "32"))
        account = os.getenv("IBKR_ACCOUNT")
        timeout = float(os.getenv("IBKR_TIMEOUT", "10.0"))
        max_strikes = int(os.getenv("IBKR_MAX_STRIKES", "5"))
        max_contracts = int(os.getenv("IBKR_MAX_CONTRACTS", "20"))
        currency = os.getenv("IBKR_CURRENCY", "USD")
        exchange = os.getenv("IBKR_EXCHANGE", "SMART")
        return cls(
            host=host,
            port=port,
            client_id=client_id,
            account=account,
            request_timeout=timeout,
            max_strikes=max(1, max_strikes),
            max_contracts=max(2, max_contracts),
            currency=currency,
            exchange=exchange,
        )


class IBKRMarketDataProvider(MarketDataProvider):
    """Async wrapper over ib_insync for options chain + greeks queries."""

    name = "ibkr"

    def __init__(self, config: Optional[IBKRConfig] = None) -> None:
        self.config = config or IBKRConfig.from_env()
        self._client: Optional[Any] = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        if ib_insync is None:
            raise ProviderError(self.name, "ib_insync is not installed. Add ib-insync to requirements.")
        if self.is_connected():
            return
        async with self._lock:
            if self.is_connected():
                return
            logger.info("Connecting to IBKR TWS/Gateway at %s:%s", self.config.host, self.config.port)
            ib = ib_insync.IB()
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: ib.connect(self.config.host, self.config.port, clientId=self.config.client_id, timeout=self.config.request_timeout),
                )
            except Exception as exc:  # pragma: no cover - network path
                raise ProviderError(self.name, f"Failed to connect: {exc}")
            self._client = ib

    async def disconnect(self) -> None:
        if not self.is_connected():
            return
        async with self._lock:
            if self._client:
                await asyncio.get_event_loop().run_in_executor(None, self._client.disconnect)
            self._client = None

    def is_connected(self) -> bool:
        return bool(self._client and self._client.isConnected())

    async def warmup(self) -> None:
        # Placeholder: fetch server time or account summary to ensure ready state.
        if not self.is_connected():
            await self.connect()

    async def _require_client(self) -> Any:
        if not self.is_connected():
            await self.connect()
        if self._client is None:
            raise ProviderError(self.name, "Client handle unavailable after connect")
        return self._client

    async def _run_blocking(self, fn, *args, **kwargs):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))

    async def _qualify(self, *contracts: Any) -> Sequence[Any]:
        if not contracts:
            return []
        ib = await self._require_client()
        await self._run_blocking(ib.qualifyContracts, *contracts)
        return contracts

    def _stock_contract(self, symbol: str) -> Any:
        return ib_insync.Stock(symbol.upper(), self.config.exchange, self.config.currency)

    async def _underlying_price(self, ib: Any, stock: Any) -> Optional[float]:
        try:
            tickers = await self._run_blocking(ib.reqTickers, stock)
        except Exception as exc:  # pragma: no cover - network path
            logger.warning("IBKR failed to fetch stock ticker: %s", exc)
            return None
        if not tickers:
            return None
        price = tickers[0].marketPrice()  # type: ignore[attr-defined]
        return price if isinstance(price, (int, float)) and price > 0 else None

    async def get_options_chain(self, symbol: str, expiry: Optional[str] = None) -> OptionsChain:
        if ib_insync is None:
            raise ProviderError(self.name, "ib_insync not installed")
        ib = await self._require_client()
        stock = self._stock_contract(symbol)
        await self._run_blocking(ib.qualifyContracts, stock)
        params_list = await self._run_blocking(
            ib.reqSecDefOptParams,
            stock.symbol,
            "",
            stock.secType,
            stock.conId,
        )
        if not params_list:
            raise ProviderError(self.name, f"No option parameters for {symbol}")
        params = next((p for p in params_list if p.exchange == self.config.exchange), params_list[0])
        expiries = sorted(params.expirations)
        if not expiries:
            raise ProviderError(self.name, f"No expiries available for {symbol}")
        chosen_expiry = expiry or expiries[0]
        if chosen_expiry not in expiries:
            raise ProviderError(self.name, f"Expiry {expiry} not listed for {symbol}")
        strikes = sorted(params.strikes)
        if not strikes:
            raise ProviderError(self.name, f"No strikes returned for {symbol}")
        underlying_price = await self._underlying_price(ib, stock)
        strikes_subset = self._select_strikes(strikes, underlying_price, self.config.max_strikes)
        ib_expiry = chosen_expiry.replace("-", "")
        contracts: List[Any] = []
        for right in ("C", "P"):
            for strike in strikes_subset:
                option = ib_insync.Option(
                    stock.symbol,
                    ib_expiry,
                    round(strike, 2),
                    right,
                    self.config.exchange,
                    self.config.currency,
                )
                contracts.append(option)
        if len(contracts) > self.config.max_contracts:
            contracts = contracts[: self.config.max_contracts]
        await self._qualify(*contracts)
        try:
            tickers = await self._run_blocking(ib.reqTickers, *contracts)
        except Exception as exc:  # pragma: no cover - network path
            raise ProviderError(self.name, f"Failed to fetch option tickers: {exc}")
        quotes: List[OptionQuote] = []
        for ticker in tickers:
            q = self._ticker_to_quote(ticker)
            if q:
                quotes.append(q)
        if not quotes:
            raise ProviderError(self.name, f"No quotes returned for {symbol}")
        provider_meta = {
            "underlying_price": str(underlying_price) if underlying_price else "unknown",
            "contracts_requested": str(len(contracts)),
            "contracts_returned": str(len(quotes)),
        }
        return OptionsChain(symbol=symbol.upper(), expiry=chosen_expiry, quotes=quotes, provider_meta=provider_meta)

    async def get_greeks(self, contract: OptionContract) -> GreeksSnapshot:
        if ib_insync is None:
            raise ProviderError(self.name, "ib_insync not installed")
        ib = await self._require_client()
        ib_contract = ib_insync.Option(
            contract.symbol,
            contract.expiry.replace("-", ""),
            contract.strike,
            "C" if contract.right == "call" else "P",
            self.config.exchange,
            self.config.currency,
        )
        await self._qualify(ib_contract)
        try:
            tickers = await self._run_blocking(ib.reqTickers, ib_contract)
        except Exception as exc:  # pragma: no cover
            raise ProviderError(self.name, f"Greeks request failed: {exc}")
        if not tickers:
            raise ProviderError(self.name, "No ticker data for contract")
        quote = self._ticker_to_quote(tickers[0])
        if not quote or quote.greeks is None:
            raise ProviderError(self.name, "Provider returned empty greeks")
        return quote.greeks

    def _select_strikes(self, strikes: Iterable[float], underlying_price: Optional[float], max_count: int) -> List[float]:
        strikes_list = sorted(set(strikes))
        if underlying_price is None:
            return strikes_list[:max_count]
        strikes_list.sort(key=lambda x: abs(x - underlying_price))
        return strikes_list[:max_count]

    def _ticker_to_quote(self, ticker: Any) -> Optional[OptionQuote]:
        contract = getattr(ticker, "contract", None)
        if contract is None or contract.secType != "OPT":
            return None
        option_contract = OptionContract(
            symbol=contract.symbol,
            expiry=self._ib_expiry_to_iso(contract.lastTradeDateOrContractMonth),
            strike=float(contract.strike),
            right="call" if contract.right == "C" else "put",
        )
        greeks = None
        mg = getattr(ticker, "modelGreeks", None)
        if mg:
            greeks = GreeksSnapshot(
                contract=option_contract,
                iv=float(mg.impliedVol) if mg.impliedVol else 0.0,
                delta=float(mg.delta) if mg.delta else 0.0,
                gamma=float(mg.gamma) if mg.gamma else 0.0,
                theta=float(mg.theta) if mg.theta else 0.0,
                vega=float(mg.vega) if mg.vega else 0.0,
                rho=float(mg.rho) if mg.rho else 0.0,
            )
        midpoint = self._midpoint(ticker)
        return OptionQuote(
            contract=option_contract,
            bid=float(ticker.bid or 0.0),
            ask=float(ticker.ask or 0.0),
            mark=midpoint,
            last=float(ticker.last or ticker.close or midpoint or 0.0),
            open_interest=int(ticker.openInterest or 0),
            volume=int(ticker.volume or 0),
            greeks=greeks,
            midpoint_iv=(greeks.iv if greeks else None),
        )

    def _midpoint(self, ticker: Any) -> float:
        bid = float(ticker.bid or 0.0)
        ask = float(ticker.ask or 0.0)
        if bid and ask:
            return round((bid + ask) / 2.0, 4)
        return float(ticker.last or ticker.close or 0.0)

    @staticmethod
    def _ib_expiry_to_iso(expiry: str) -> str:
        if len(expiry) == 8:
            return f"{expiry[0:4]}-{expiry[4:6]}-{expiry[6:8]}"
        if len(expiry) == 6:
            return f"{expiry[0:4]}-{expiry[4:6]}-01"
        return expiry


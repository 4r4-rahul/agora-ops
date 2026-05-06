"""
MarketDataAgent — fetches OHLCV, options chains, and IV data via yfinance.

Subscribes to: ANALYSIS_REQUEST
Publishes:     MARKET_DATA_RESULT
"""

from __future__ import annotations

import logging
from datetime import datetime

from ..core.models.agent import AgentMessage, AgentTopic, AnalysisRequest
from ..core.models.market import MarketSnapshot
from ..services.market_data.yfinance_provider import YFinanceProvider
from .base import BaseAgent

logger = logging.getLogger(__name__)


class MarketDataAgent(BaseAgent):
    name = "market_data"
    subscriptions = [AgentTopic.ANALYSIS_REQUEST]

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._provider = YFinanceProvider()

    async def handle(self, message: AgentMessage) -> None:
        req = AnalysisRequest.model_validate(message.payload)
        ticker = req.ticker
        session_id = message.session_id

        self._log.info("[%s] fetching market data for %s", session_id, ticker)

        try:
            snapshot = await self._provider.get_snapshot(ticker)
        except Exception as exc:
            self._log.error("[%s] market data fetch failed for %s: %s", session_id, ticker, exc)
            await self._publish_error(session_id, f"MarketData: {exc}")
            return

        await self._state.update(
            session_id,
            market_snapshot=snapshot.model_dump(mode="json"),
        )

        await self.publish(
            AgentTopic.MARKET_DATA_RESULT,
            session_id=session_id,
            payload=snapshot.model_dump(mode="json"),
        )
        self._log.info(
            "[%s] %s @ $%.2f | VIX=%.1f | IV_rank=%.0f",
            session_id,
            ticker,
            snapshot.price,
            snapshot.vix or 0,
            snapshot.iv_rank or 0,
        )

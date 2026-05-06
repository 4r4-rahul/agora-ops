"""
TechnicalAnalysisAgent — deterministic indicator computation, no LLM.

Subscribes to: MARKET_DATA_RESULT
Publishes:     TECHNICAL_RESULT

Deterministic — no Claude call. Fast, always-reproducible signals.
"""

from __future__ import annotations

import logging
import statistics
from typing import Any

from ..core.models.agent import AgentMessage, AgentTopic, TechnicalResult
from ..core.models.market import Bar, MarketSnapshot
from .base import BaseAgent

logger = logging.getLogger(__name__)


class TechnicalAnalysisAgent(BaseAgent):
    name = "technical"
    subscriptions = [AgentTopic.MARKET_DATA_RESULT]

    async def handle(self, message: AgentMessage) -> None:
        session_id = message.session_id
        snapshot = MarketSnapshot.model_validate(message.payload)
        ticker = snapshot.ticker

        self._log.info("[%s] computing technicals for %s", session_id, ticker)

        indicators = self._compute_indicators(snapshot)
        signal, strength = self._generate_signal(indicators, snapshot)
        support, resistance = self._find_levels(snapshot)

        result = TechnicalResult(
            ticker=ticker,
            signal=signal,
            signal_strength=strength,
            support_levels=support,
            resistance_levels=resistance,
            key_levels={
                "day_open": snapshot.day_open or snapshot.price,
                "prev_close": snapshot.prev_close or snapshot.price,
                "sma_20": snapshot.sma_20 or snapshot.price,
                "sma_50": snapshot.sma_50 or snapshot.price,
                "sma_200": snapshot.sma_200 or snapshot.price,
            },
            indicators=indicators,
            notes=self._build_notes(signal, strength, indicators),
        )

        await self._state.update(
            session_id,
            technical_result=result.model_dump(mode="json"),
        )

        await self.publish(
            AgentTopic.TECHNICAL_RESULT,
            session_id=session_id,
            payload=result.model_dump(mode="json"),
        )

        self._log.info(
            "[%s] %s signal=%s strength=%.2f",
            session_id,
            ticker,
            signal,
            strength,
        )

    # ── Signal Generation ─────────────────────────────────────────

    def _compute_indicators(self, snapshot: MarketSnapshot) -> dict[str, float]:
        indicators: dict[str, float] = {}

        price = snapshot.price
        indicators["price"] = price

        if snapshot.rsi_14 is not None:
            indicators["rsi_14"] = snapshot.rsi_14

        if snapshot.atr_14 is not None:
            indicators["atr_14"] = snapshot.atr_14
            indicators["atr_pct"] = snapshot.atr_14 / price * 100

        # Trend scores: each +1 for bullish, -1 for bearish
        trend_score = 0.0
        if snapshot.sma_20 and price > snapshot.sma_20:
            trend_score += 1
        elif snapshot.sma_20 and price < snapshot.sma_20:
            trend_score -= 1

        if snapshot.sma_50 and price > snapshot.sma_50:
            trend_score += 1
        elif snapshot.sma_50 and price < snapshot.sma_50:
            trend_score -= 1

        if snapshot.sma_200 and price > snapshot.sma_200:
            trend_score += 1
        elif snapshot.sma_200 and price < snapshot.sma_200:
            trend_score -= 1

        indicators["trend_score"] = trend_score

        # Distance from SMAs (%)
        if snapshot.sma_20:
            indicators["dist_sma20_pct"] = (price - snapshot.sma_20) / snapshot.sma_20 * 100
        if snapshot.sma_50:
            indicators["dist_sma50_pct"] = (price - snapshot.sma_50) / snapshot.sma_50 * 100

        # Intraday range usage
        if snapshot.day_high and snapshot.day_low and snapshot.day_high > snapshot.day_low:
            day_range = snapshot.day_high - snapshot.day_low
            indicators["day_range_pct"] = day_range / snapshot.day_low * 100
            indicators["price_in_range"] = (price - snapshot.day_low) / day_range

        # Momentum from 15m bars
        if len(snapshot.bars_15m) >= 3:
            closes = [b.close for b in snapshot.bars_15m[-3:]]
            indicators["momentum_3bar"] = (closes[-1] - closes[0]) / closes[0] * 100

        return indicators

    def _generate_signal(
        self, indicators: dict[str, float], snapshot: MarketSnapshot
    ) -> tuple[str, float]:
        trend_score = indicators.get("trend_score", 0)
        rsi = indicators.get("rsi_14", 50)
        price_in_range = indicators.get("price_in_range", 0.5)
        momentum = indicators.get("momentum_3bar", 0)

        # Combine signals
        bull_signals = 0
        bear_signals = 0
        total = 0

        if trend_score > 0:
            bull_signals += trend_score
        elif trend_score < 0:
            bear_signals += abs(trend_score)
        total += 3  # 3 MA signals

        if rsi > 60:
            bull_signals += 1
        elif rsi < 40:
            bear_signals += 1
        total += 1

        if momentum > 0.1:
            bull_signals += 1
        elif momentum < -0.1:
            bear_signals += 1
        total += 1

        if total == 0:
            return "neutral", 0.5

        bull_pct = bull_signals / total
        bear_pct = bear_signals / total

        if bull_pct > 0.6:
            return "bullish", round(bull_pct, 2)
        elif bear_pct > 0.6:
            return "bearish", round(bear_pct, 2)
        else:
            return "neutral", 0.5

    def _find_levels(self, snapshot: MarketSnapshot) -> tuple[list[float], list[float]]:
        """Simple pivot-based support/resistance from daily bars."""
        support: list[float] = []
        resistance: list[float] = []

        bars = snapshot.bars_daily[-20:] if snapshot.bars_daily else []
        for bar in bars:
            # Pivot low → support
            if bar.low < snapshot.price * 0.995:
                support.append(round(bar.low, 2))
            # Pivot high → resistance
            if bar.high > snapshot.price * 1.005:
                resistance.append(round(bar.high, 2))

        # Add static levels
        if snapshot.day_open:
            if snapshot.day_open < snapshot.price:
                support.append(round(snapshot.day_open, 2))
            else:
                resistance.append(round(snapshot.day_open, 2))

        if snapshot.sma_20:
            if snapshot.sma_20 < snapshot.price:
                support.append(round(snapshot.sma_20, 2))
            else:
                resistance.append(round(snapshot.sma_20, 2))

        # Deduplicate and sort
        support = sorted(set(support), reverse=True)[:3]
        resistance = sorted(set(resistance))[:3]

        return support, resistance

    def _build_notes(
        self, signal: str, strength: float, indicators: dict[str, float]
    ) -> str:
        rsi = indicators.get("rsi_14", 50)
        trend_score = indicators.get("trend_score", 0)

        notes = []
        notes.append(f"Signal: {signal.upper()} (strength={strength:.0%})")

        if trend_score > 0:
            notes.append(f"Above {int(trend_score)}/3 key MAs")
        elif trend_score < 0:
            notes.append(f"Below {int(abs(trend_score))}/3 key MAs")

        if rsi > 70:
            notes.append(f"RSI overbought ({rsi:.0f})")
        elif rsi < 30:
            notes.append(f"RSI oversold ({rsi:.0f})")
        else:
            notes.append(f"RSI neutral ({rsi:.0f})")

        return " | ".join(notes)

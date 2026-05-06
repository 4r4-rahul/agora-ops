"""
RegimeAgent — classifies market regime using Claude.

Subscribes to: MARKET_DATA_RESULT
Publishes:     REGIME_RESULT

Uses claude-opus-4-7 with adaptive thinking and prompt caching.
"""

from __future__ import annotations

import json
import logging

from pydantic import BaseModel, Field

from ..core.models.agent import AgentMessage, AgentTopic, MarketRegime, RegimeResult
from ..core.models.market import MarketSnapshot
from .base import BaseAgent

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a quantitative market regime analyst with expertise in options trading.
Your job is to classify the current market regime based on price action, volatility,
and breadth. The regime classification drives which options strategies are viable.

Regime classifications:
- bull_trend: sustained uptrend, VIX < 18, breadth positive, trend-following works
- bear_trend: sustained downtrend, VIX rising, breadth negative, defensive strategies
- ranging: no directional trend, mean-reversion works, iron condors viable
- high_volatility: VIX > 25, large intraday swings, widen stops, reduce size
- low_volatility: VIX < 13, compressed moves, long gamma cheap, spreads tight
- crisis: VIX > 40, tail risk, close most positions, cash is a position

You MUST call the structured_output tool with your analysis.
Be precise and evidence-based. Do not hedge — pick the primary regime.
"""


class RegimeOutput(BaseModel):
    regime: MarketRegime
    confidence: float = Field(..., ge=0.0, le=1.0)
    vix_trend: str = Field(..., description="rising | falling | flat")
    trend_direction: str = Field(..., description="bullish | bearish | neutral")
    breadth_score: float = Field(..., ge=-1.0, le=1.0, description="-1=all down, +1=all up")
    reasoning: str = Field(..., min_length=50)
    regime_suitable_strategies: list[str] = Field(
        ..., description="List of strategies that work in this regime"
    )


class RegimeAgent(BaseAgent):
    name = "regime"
    subscriptions = [AgentTopic.MARKET_DATA_RESULT]

    async def handle(self, message: AgentMessage) -> None:
        session_id = message.session_id
        snapshot = MarketSnapshot.model_validate(message.payload)

        self._log.info("[%s] classifying regime for %s", session_id, snapshot.ticker)

        user_message = self._build_prompt(snapshot)

        try:
            output = await self._call_claude_structured(
                system_prompt=_SYSTEM_PROMPT,
                user_message=user_message,
                output_schema=RegimeOutput,
                tool_name="structured_output",
                max_tokens=2048,
            )
        except Exception as exc:
            self._log.error("[%s] regime classification failed: %s", session_id, exc)
            await self._publish_error(session_id, f"RegimeAgent: {exc}")
            return

        result = RegimeResult(
            regime=output.regime,
            confidence=output.confidence,
            vix=snapshot.vix,
            vix_trend=output.vix_trend,
            trend_direction=output.trend_direction,
            breadth_score=output.breadth_score,
            reasoning=output.reasoning,
            regime_suitable_strategies=output.regime_suitable_strategies,
        )

        await self._state.update(
            session_id,
            regime_result=result.model_dump(mode="json"),
        )

        await self.publish(
            AgentTopic.REGIME_RESULT,
            session_id=session_id,
            payload=result.model_dump(mode="json"),
        )

        self._log.info(
            "[%s] regime=%s confidence=%.0f%%",
            session_id,
            result.regime,
            result.confidence * 100,
        )

    def _build_prompt(self, snapshot: MarketSnapshot) -> str:
        bars_summary = ""
        if snapshot.bars_daily:
            last_5 = snapshot.bars_daily[-5:]
            bars_summary = "\n".join(
                f"  {b.ts.date()}: O={b.open:.2f} H={b.high:.2f} L={b.low:.2f} C={b.close:.2f} V={b.volume:,}"
                for b in last_5
            )

        def _fmt(v: float | None, fmt: str = ".2f", prefix: str = "", suffix: str = "") -> str:
            return f"{prefix}{v:{fmt}}{suffix}" if v is not None else "N/A"

        return f"""Classify the market regime for {snapshot.ticker}.

Current conditions:
  Price: ${snapshot.price:.2f}
  Day change: {_fmt(snapshot.change_pct, suffix='%')} vs prev close {_fmt(snapshot.prev_close, prefix='$')}
  Day range: {_fmt(snapshot.day_low, prefix='$')} – {_fmt(snapshot.day_high, prefix='$')}
  VIX: {_fmt(snapshot.vix, '.1f')}
  ATR(14): {_fmt(snapshot.atr_14, prefix='$')}
  RSI(14): {_fmt(snapshot.rsi_14, '.1f')}
  SMA20: {_fmt(snapshot.sma_20, prefix='$')} | SMA50: {_fmt(snapshot.sma_50, prefix='$')} | SMA200: {_fmt(snapshot.sma_200, prefix='$')}
  IV Rank: {_fmt(snapshot.iv_rank, '.0f', suffix='/100')}
  IV Percentile: {_fmt(snapshot.iv_percentile, '.0f', suffix='/100')}
  HV30: {_fmt(snapshot.hist_vol_30, '.1%')}

Last 5 daily bars:
{bars_summary if bars_summary else "  Not available"}

Determine the market regime and which options strategies are most viable right now."""

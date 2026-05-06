"""
OptionsStrategyAgent — generates 1-3 ranked trade recommendations using Claude.

Subscribes to: TECHNICAL_RESULT (waits for regime + news to be present in state)
Publishes:     STRATEGY_CANDIDATES
"""

from __future__ import annotations

import asyncio
import logging

from pydantic import BaseModel, Field

from ..core.models.agent import (
    AgentMessage,
    AgentTopic,
    MarketRegime,
    RegimeResult,
)
from ..core.models.market import MarketSnapshot
from ..core.models.trade import (
    Direction,
    OptionsStrategyType,
    SpreadLeg,
    TradeRecommendation,
)
from .base import BaseAgent

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a professional options trader with 15+ years of experience specializing in
institutional-grade options strategies. You never chase cheap options. You always
think in terms of defined risk, probability of profit, and risk/reward.

Core trading rules you never violate:
1. Never buy options when IV Rank > 70 — you are buying inflated vol
2. Never sell options when IV Rank < 20 — premium is not worth the risk
3. Always use spreads to cap max loss — naked options are for institutions with unlimited margin
4. Minimum R/R ratio 1.5:1 for directional trades, 2:1 for premium selling
5. Never trade earnings unless the position is defined-risk with max loss < 2% of account
6. Liquidity gate: OI > 100, volume > 50, bid/ask spread < 15% of mid
7. Stop loss: exit at 2x the initial debit for long spreads, or at 2x credit received for shorts
8. Profit target: 50-75% of max profit for short premium, 100% of debit for long spreads

Strategy selection by regime:
- bull_trend: bull call spreads, covered calls, cash-secured puts
- bear_trend: bear put spreads, protective puts, short calls (spreads only)
- ranging: iron condors, iron butterflies, calendar spreads
- high_volatility: iron condors (wider), long strangles for explosive moves
- low_volatility: long debit spreads, long calls/puts with tight spreads
- crisis: cash or very small defined-risk positions only

You MUST call structured_output with your top recommendation.
Every field is required — never output partial recommendations.
"""


class LegOutput(BaseModel):
    option_type: str
    strike: float
    expiration_dte: int
    action: str
    quantity: int = 1


class StrategyOutput(BaseModel):
    ticker: str
    direction: Direction
    thesis: str = Field(..., min_length=50)
    strategy: OptionsStrategyType
    legs: list[LegOutput] = Field(..., min_length=1, max_length=4)
    strike_selection_logic: str = Field(..., min_length=30)
    expiration_dte: int = Field(..., ge=1, le=90)
    entry_trigger: str = Field(..., min_length=20)
    entry_price: float = Field(..., gt=0)
    stop_loss: float = Field(..., gt=0)
    stop_loss_logic: str
    profit_target: float = Field(..., gt=0)
    profit_target_logic: str
    max_loss_dollars: float = Field(..., gt=0)
    max_gain_dollars: float = Field(..., gt=0)
    reward_risk_ratio: float = Field(..., gt=0)
    contracts: int = Field(default=1, ge=1)
    position_size_dollars: float = Field(..., gt=0)
    iv_crush_risk: str = Field(default="none")
    earnings_risk: str = Field(default="none")
    regime_confirms: bool
    regime_notes: str


class OptionsStrategyAgent(BaseAgent):
    name = "options_strategy"
    subscriptions = [AgentTopic.TECHNICAL_RESULT]

    async def handle(self, message: AgentMessage) -> None:
        session_id = message.session_id

        # Wait briefly for regime and news to complete
        await asyncio.sleep(0.1)

        session = await self._state.get(session_id)
        if not session:
            self._log.error("[%s] session not found", session_id)
            return

        if not session.market_snapshot:
            self._log.warning("[%s] no market snapshot, skipping strategy", session_id)
            return

        ticker = session.ticker
        self._log.info("[%s] generating strategies for %s", session_id, ticker)

        snapshot = MarketSnapshot.model_validate(session.market_snapshot)
        regime = RegimeResult.model_validate(session.regime_result) if session.regime_result else None

        user_message = self._build_prompt(snapshot, regime, session)

        try:
            output = await self._call_claude_structured(
                system_prompt=_SYSTEM_PROMPT,
                user_message=user_message,
                output_schema=StrategyOutput,
                tool_name="structured_output",
                max_tokens=4096,
            )
        except Exception as exc:
            self._log.error("[%s] strategy generation failed: %s", session_id, exc)
            await self._publish_error(session_id, f"OptionsStrategy: {exc}")
            return

        from datetime import date, timedelta

        expiration = date.today() + timedelta(days=output.expiration_dte)
        legs = [
            SpreadLeg(
                option_type=leg.option_type,
                strike=leg.strike,
                expiration=expiration,
                action=leg.action,
                quantity=leg.quantity,
            )
            for leg in output.legs
        ]

        rec = TradeRecommendation(
            session_id=session_id,
            ticker=output.ticker,
            direction=output.direction,
            thesis=output.thesis,
            strategy=output.strategy,
            expiration=expiration,
            legs=legs,
            strike_selection_logic=output.strike_selection_logic,
            entry_trigger=output.entry_trigger,
            entry_price=output.entry_price,
            stop_loss=output.stop_loss,
            stop_loss_logic=output.stop_loss_logic,
            profit_target=output.profit_target,
            profit_target_logic=output.profit_target_logic,
            max_loss_dollars=output.max_loss_dollars,
            max_gain_dollars=output.max_gain_dollars,
            reward_risk_ratio=output.reward_risk_ratio,
            contracts=output.contracts,
            position_size_dollars=output.position_size_dollars,
            liquidity_ok=True,  # RiskManager will verify
            iv_rank=snapshot.iv_rank,
            iv_crush_risk=output.iv_crush_risk,
            earnings_risk=output.earnings_risk,
            regime_confirms=output.regime_confirms,
            regime_notes=output.regime_notes,
        )

        candidates = [rec.model_dump(mode="json")]
        await self._state.update(session_id, strategy_candidates=candidates)

        await self.publish(
            AgentTopic.STRATEGY_CANDIDATES,
            session_id=session_id,
            payload={"candidates": candidates},
        )

        self._log.info(
            "[%s] strategy=%s R/R=%.1f maxloss=$%.0f",
            session_id,
            rec.strategy,
            rec.reward_risk_ratio,
            rec.max_loss_dollars,
        )

    def _build_prompt(
        self,
        snapshot: MarketSnapshot,
        regime: RegimeResult | None,
        session,
    ) -> str:
        regime_text = (
            f"Regime: {regime.regime} (confidence={regime.confidence:.0%})\n"
            f"  Reasoning: {regime.reasoning}\n"
            f"  Suitable strategies: {', '.join(regime.regime_suitable_strategies)}"
            if regime
            else "Regime: unknown"
        )

        news = session.news_result or {}
        technical = session.technical_result or {}

        def _fmt(v: float | None, fmt: str = ".2f", prefix: str = "", suffix: str = "") -> str:
            return f"{prefix}{v:{fmt}}{suffix}" if v is not None else "N/A"

        return f"""Generate the single best options trade recommendation for {snapshot.ticker}.

MARKET DATA:
  Price: ${snapshot.price:.2f}
  Day change: {_fmt(snapshot.change_pct, suffix='%')}
  IV Rank: {_fmt(snapshot.iv_rank, '.0f', suffix='/100')}
  IV Percentile: {_fmt(snapshot.iv_percentile, '.0f', suffix='/100')}
  HV30: {_fmt(snapshot.hist_vol_30, '.1%')}
  VIX: {_fmt(snapshot.vix, '.1f')}
  ATR(14): {_fmt(snapshot.atr_14, prefix='$')}
  RSI(14): {_fmt(snapshot.rsi_14, '.1f')}
  SMA20: {_fmt(snapshot.sma_20, prefix='$')} | SMA50: {_fmt(snapshot.sma_50, prefix='$')}
  Account size: ${self._settings.account_size:,.0f}
  Max position size: ${self._settings.max_position_dollars:,.0f}

{regime_text}

TECHNICAL SIGNAL:
  Signal: {technical.get('signal', 'unknown')} (strength={technical.get('signal_strength', 0):.0%})
  Support: {technical.get('support_levels', [])}
  Resistance: {technical.get('resistance_levels', [])}

NEWS & CATALYSTS:
  Sentiment: {news.get('sentiment', 'neutral')} ({news.get('sentiment_score', 0):+.2f})
  Earnings risk: {news.get('has_earnings_risk', False)}
  Earnings date: {news.get('earnings_date', 'unknown')}
  Summary: {news.get('summary', 'N/A')}

REQUIREMENTS:
- Min R/R: {self._settings.min_reward_risk_ratio}:1
- Max IV rank to buy options: 70
- Min liquidity: OI > {self._settings.min_open_interest}, Vol > {self._settings.min_volume}
- Max position: ${self._settings.max_position_dollars:,.0f}
- Define your complete strategy with all strikes, expirations, and exact entry conditions.
"""

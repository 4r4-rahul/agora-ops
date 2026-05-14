"""
ConvictionAgent — synthesises all pipeline signals into a 0-100 conviction score.

Subscribes to: TECHNICAL_RESULT (fires once regime + news + technical are in state)
Publishes:     CONVICTION_SCORE → triggers OptionsStrategyAgent

Score breakdown (100 pts total):
  Regime confidence      25 pts  — how certain is the regime classification
  Technical strength     25 pts  — signal strength from TechnicalAnalysisAgent
  News alignment         20 pts  — news sentiment aligned with trade direction
  IV environment         15 pts  — IV rank in sweet spot for the strategy type
  Macro calendar         10 pts  — no high-risk events in window
  Session quality         5 pts  — time of day / day of week multiplier

Score gates:
  >= 80  → High conviction — unlock long calls/puts, up to 3 contracts
  60-79  → Standard conviction — vertical spreads, 1-2 contracts
  40-59  → Low conviction — spreads only, 1 contract, tighter stops
  < 40   → No trade — skip this session entirely

The score is stored in session state and used by OptionsStrategyAgent and
RiskManagerAgent to scale position sizing and unlock strategy types.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from ..core.models.agent import AgentMessage, AgentTopic, MarketRegime, RegimeResult
from ..core.models.trade import TradeRecommendation
from .base import BaseAgent

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")


class ConvictionScore(BaseModel):
    session_id: str
    ticker: str
    total_score: float = Field(ge=0, le=100)
    gate: str  # "high" | "standard" | "low" | "no_trade"

    # Component scores (out of their max)
    regime_score: float = 0.0       # /25
    technical_score: float = 0.0    # /25
    news_score: float = 0.0         # /20
    iv_score: float = 0.0           # /15
    macro_score: float = 0.0        # /10
    session_score: float = 0.0      # /5

    # Context
    regime: str = ""
    direction: str = ""
    iv_rank: float | None = None
    reasoning: str = ""

    # Strategy unlocks
    max_contracts: int = 1
    allow_long_options: bool = False  # True only at high conviction + low IV


class ConvictionAgent(BaseAgent):
    """
    Deterministic conviction scorer — no Claude call needed.
    Reads all session data and scores purely from numbers.
    Loads learned weight adjustments from performance_feedback.json on startup.
    """

    name = "conviction"
    subscriptions = [AgentTopic.TECHNICAL_RESULT]

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._learned_weights: dict = self._load_learned_weights()
        if self._learned_weights:
            self._log.info(
                "Loaded learned weight adjustments: %s",
                list(self._learned_weights.keys()),
            )

    @staticmethod
    def _load_learned_weights() -> dict:
        """Read performance_feedback.json and return weight_adjustments dict."""
        import json
        from pathlib import Path
        feedback_path = Path("./performance_feedback.json")
        if not feedback_path.exists():
            return {}
        try:
            data = json.loads(feedback_path.read_text())
            return data.get("weight_adjustments", {})
        except Exception:
            return {}

    async def handle(self, message: AgentMessage) -> None:
        session_id = message.session_id

        # Poll for regime result (Claude call typically takes 8–20 s).
        # Wait up to 45 s before scoring with whatever data is available.
        deadline = asyncio.get_event_loop().time() + 45.0
        while asyncio.get_event_loop().time() < deadline:
            session = await self._state.get(session_id)
            if session and session.regime_result:
                break
            await asyncio.sleep(1.0)
        else:
            session = await self._state.get(session_id)

        if not session:
            return

        score = self._compute_score(session_id, session)
        await self._state.update(
            session_id,
            conviction_score=score.model_dump(mode="json"),
        )
        await self.publish(
            AgentTopic.CONVICTION_SCORE,
            session_id=session_id,
            payload=score.model_dump(mode="json"),
        )
        self._log.info(
            "[%s] conviction=%d/100 gate=%s regime=%s IV=%.0f",
            session_id, score.total_score, score.gate,
            score.regime, score.iv_rank or 0,
        )

    def _compute_score(self, session_id: str, session) -> ConvictionScore:
        regime_data    = session.regime_result or {}
        technical_data = session.technical_result or {}
        news_data      = session.news_result or {}
        snapshot_data  = session.market_snapshot or {}
        # strategy_candidates not yet available (conviction fires before strategy generation)
        candidates: list = []

        # ── 1. Regime score (25 pts) ──────────────────────────────────────
        regime_confidence = float(regime_data.get("confidence", 0))
        regime_name = regime_data.get("regime", "unknown")

        if regime_name == "crisis":
            regime_score = 0.0
        else:
            # Base: confidence × 25
            regime_score = regime_confidence * 25.0
            # Bonus: clean trending regimes (not ranging/high_vol) are more actionable
            if regime_name in ("bull_trend", "bear_trend"):
                regime_score = min(25.0, regime_score * 1.1)

        # Penalty: uncertain regime (confidence < 0.5) — don't act on ambiguous signals
        if regime_name != "crisis" and regime_confidence < 0.5:
            regime_score = max(0.0, regime_score - 10.0)

        # ── 2. Technical score (25 pts) ───────────────────────────────────
        signal_strength = float(technical_data.get("signal_strength", 0))
        signal = technical_data.get("signal", "neutral").lower()
        tech_score = signal_strength * 25.0

        # ── 3. News alignment score (20 pts) ─────────────────────────────
        news_sentiment = news_data.get("sentiment", "neutral").lower()
        news_score_raw = float(news_data.get("sentiment_score", 0))  # -1 to +1
        has_earnings = news_data.get("has_earnings_risk", False)
        catalyst_type = news_data.get("catalyst_type", "none")

        if has_earnings:
            news_score = 0.0  # earnings risk kills news score
        elif catalyst_type in ("fda", "macro"):
            news_score = 5.0  # binary events halve the score
        else:
            # Map -1→+1 to 0→20
            news_score = (news_score_raw + 1.0) / 2.0 * 20.0

        # ── 4. IV environment score (15 pts) ──────────────────────────────
        iv_rank = snapshot_data.get("iv_rank")
        strategy = ""
        direction = ""
        if candidates:
            first = candidates[0]
            strategy = first.get("strategy", "")
            direction = first.get("direction", "")

        buying_strategies = {
            "long_call", "long_put", "bull_call_spread",
            "bear_put_spread", "straddle", "strangle", "calendar_spread",
        }
        selling_strategies = {
            "iron_condor", "iron_butterfly", "covered_call",
            "cash_secured_put", "bull_put_spread", "bear_call_spread",
        }

        if iv_rank is None:
            iv_score = 7.5  # neutral — no data
        elif not strategy:
            # Strategy not yet generated — pre-classify from regime + IV rank
            # Trending regime → likely debit spreads (buying); Range-bound → credit (selling)
            likely_buying = regime_name in ("bull_trend", "bear_trend", "low_volatility") or iv_rank <= 40
            likely_selling = regime_name in ("ranging", "high_volatility") or iv_rank >= 50
            if likely_buying and not likely_selling:
                # Treat as buying: sweet spot 20-40
                iv_score = 15.0 if 20 <= iv_rank <= 40 else (10.0 if iv_rank <= 55 else 3.0)
            elif likely_selling and not likely_buying:
                # Treat as selling: sweet spot 50-70
                iv_score = 15.0 if iv_rank >= 50 else (7.0 if iv_rank >= 35 else 2.0)
            else:
                iv_score = 10.0  # balanced conditions
        elif strategy in buying_strategies:
            # For buying: sweet spot 20-50. Penalty above 50, cliff above 70.
            if iv_rank <= 20:
                iv_score = 10.0   # cheap but not signal
            elif iv_rank <= 35:
                iv_score = 15.0   # ideal buying zone
            elif iv_rank <= 50:
                iv_score = 12.0   # acceptable
            elif iv_rank <= 70:
                iv_score = 5.0    # expensive
            else:
                iv_score = 0.0    # veto territory
        elif strategy in selling_strategies:
            # For selling: sweet spot 50-70. Penalty below 30.
            if iv_rank >= 50:
                iv_score = 15.0   # ideal selling zone
            elif iv_rank >= 35:
                iv_score = 10.0   # acceptable
            elif iv_rank >= 20:
                iv_score = 5.0    # cheap premium
            else:
                iv_score = 0.0    # not worth the risk
        else:
            iv_score = 10.0  # neutral strategy

        # ── 5. Macro calendar score (10 pts) ──────────────────────────────
        from ..services.macro_calendar import get_macro_calendar
        cal = get_macro_calendar()
        risk_level = cal.get_risk_level(date.today())
        macro_score = {"clear": 10.0, "caution": 4.0, "avoid": 0.0}[risk_level]

        # Days to next event also matters
        days_to_event, _ = cal.days_to_next_event(date.today())
        if days_to_event <= 1:
            macro_score = min(macro_score, 3.0)  # event tomorrow
        elif days_to_event <= 3:
            macro_score = min(macro_score, 6.0)  # event this week

        # ── 6. Session quality score (5 pts) ──────────────────────────────
        from ..scripts.scheduler import get_session_confidence
        session_mult = get_session_confidence(datetime.now(tz=ET))
        session_score = session_mult * 5.0

        # Apply learned weight adjustments from PerformanceFeedbackAgent
        et_now = datetime.now(tz=ET)
        day_name = ["mon", "tue", "wed", "thu", "fri"][et_now.weekday()] if et_now.weekday() < 5 else "mon"
        penalty_key = f"dow_{day_name}_penalty"
        bonus_key   = f"dow_{day_name}_bonus"
        if penalty_key in self._learned_weights:
            session_score = max(0.0, session_score - self._learned_weights[penalty_key] * 5.0)
        if bonus_key in self._learned_weights:
            session_score = min(5.0, session_score + self._learned_weights[bonus_key] * 5.0)

        total = (
            regime_score + tech_score + news_score
            + iv_score + macro_score + session_score
        )

        # ── Signal stack bonus (+10 pts) ─────────────────────────────────
        # All three primary signals aligned in the same direction → extra conviction
        regime_is_bullish  = regime_name == "bull_trend"
        regime_is_bearish  = regime_name == "bear_trend"
        signal_is_bullish  = signal in ("bullish", "buy", "long", "breakout_up")
        signal_is_bearish  = signal in ("bearish", "sell", "short", "breakout_down")
        news_is_positive   = news_score_raw >= 0.3
        news_is_negative   = news_score_raw <= -0.3
        stack_bonus = 0.0
        if not has_earnings:
            if regime_is_bullish and signal_is_bullish and news_is_positive:
                stack_bonus = 10.0
            elif regime_is_bearish and signal_is_bearish and news_is_negative:
                stack_bonus = 10.0
        total += stack_bonus

        total = round(min(100.0, max(0.0, total)), 1)

        # ── Gate classification ───────────────────────────────────────────
        if total >= 80:
            gate = "high"
            max_contracts = 3
            # Allow long calls/puts only if IV is cheap (IV rank < 40)
            allow_long = (iv_rank or 100) < 40
        elif total >= 60:
            gate = "standard"
            max_contracts = 2
            allow_long = False
        elif total >= 40:
            gate = "low"
            max_contracts = 1
            allow_long = False
        else:
            gate = "no_trade"
            max_contracts = 0
            allow_long = False

        # Apply tier cap — use effective_trade_tier so PDT-exempt accounts
        # are not penalised by the raw account_tier (starter → intermediate).
        tier_cap = {"starter": 1, "intermediate": 2, "advanced": 5, "professional": 10}
        tier = self._settings.effective_trade_tier
        max_contracts = min(max_contracts, tier_cap.get(tier, 1))

        conf_penalty_note = (
            f" [regime_conf_penalty: regime_confidence={regime_confidence:.0%}<0.5]"
            if regime_name != "crisis" and regime_confidence < 0.5 else ""
        )
        stack_note = " [stack_bonus:+10 all signals aligned]" if stack_bonus > 0 else ""
        reasoning = (
            f"Regime({regime_score:.0f}/25)"
            f"{conf_penalty_note} "
            f"Technical({tech_score:.0f}/25) "
            f"News({news_score:.0f}/20) "
            f"IV({iv_score:.0f}/15) "
            f"Macro({macro_score:.0f}/10) "
            f"Session({session_score:.0f}/5)"
            f"{stack_note} "
            f"= {total}/100 [{gate.upper()}]"
        )

        return ConvictionScore(
            session_id=session_id,
            ticker=session.ticker,
            total_score=total,
            gate=gate,
            regime_score=regime_score,
            technical_score=tech_score,
            news_score=news_score,
            iv_score=iv_score,
            macro_score=macro_score,
            session_score=session_score,
            regime=regime_name,
            direction=direction,
            iv_rank=iv_rank,
            reasoning=reasoning,
            max_contracts=max_contracts,
            allow_long_options=allow_long,
        )

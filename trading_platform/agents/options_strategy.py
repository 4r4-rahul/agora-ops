"""
OptionsStrategyAgent — generates 1-3 ranked trade recommendations using Claude.

Subscribes to: CONVICTION_SCORE (fires after ConvictionAgent scores all signals)
Publishes:     STRATEGY_CANDIDATES

Pipeline order: TECHNICAL_RESULT → ConvictionAgent → OptionsStrategy → RiskManager → Reviewer
"""

from __future__ import annotations

import logging
from datetime import date

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

# Tier constraints fed into every prompt — Claude must respect these.
_TIER_RULES: dict[str, dict] = {
    "starter": {
        "label": "Starter (<$25k) — defined-risk verticals only",
        "allowed": "bull_call_spread, bear_put_spread, bull_put_spread, bear_call_spread, long_call, long_put (rare, high conviction only)",
        "forbidden": "iron_condor, iron_butterfly, calendar_spread, covered_call, ratio_spread, naked options",
        "dte": "21–35 DTE. Never buy weeklies — theta destroys small accounts that are early on direction.",
        "max_contracts": 1,
        "notes": (
            "Under $25k: focus on defined-risk verticals with tight sizing. "
            "Master ONE strategy (debit vertical spreads) across 20-30 trades before adding complexity. "
            "Kelly position sizing applies — never risk more than 2% per trade."
        ),
    },
    "intermediate": {
        "label": "Intermediate ($25k–$50k) — PDT lifted, iron condors now viable",
        "allowed": "bull_call_spread, bear_put_spread, bull_put_spread, bear_call_spread, iron_condor, long_call, long_put, cash_secured_put",
        "forbidden": "calendar_spread, iron_butterfly, ratio_spread, naked options",
        "dte": "21–45 DTE",
        "max_contracts": 3,
        "notes": (
            "PDT restriction lifted — can actively manage iron condors intraday. "
            "Iron condors viable in ranging regimes. Calendar spreads still not recommended "
            "without larger account for vega adjustment room."
        ),
    },
    "advanced": {
        "label": "Advanced ($50k–$100k) — full defined-risk toolkit, diversify 6-10 underlyings",
        "allowed": "all defined-risk strategies including calendars, diagonals, covered calls, iron butterflies",
        "forbidden": "naked options, ratio spreads with undefined risk",
        "dte": "14–60 DTE depending on strategy type",
        "max_contracts": 5,
        "notes": (
            "Track portfolio-level Greeks (delta, theta, vega). "
            "No single name should exceed 10% of account. "
            "Size toward half-Kelly fraction based on actual trade history."
        ),
    },
    "professional": {
        "label": "Professional ($100k+) — portfolio margin eligible, systematic premium selling",
        "allowed": "all strategies including ratio spreads and jade lizards",
        "forbidden": "naked short calls with undefined risk",
        "dte": "7–90 DTE — use term structure strategically",
        "max_contracts": 10,
        "notes": (
            "Portfolio margin changes buying power calculation significantly. "
            "Must run a Greeks dashboard. Delta-neutral management required. "
            "Consider vol arbitrage and dispersion at this level."
        ),
    },
}

_SYSTEM_PROMPT = """\
You are a professional options trader with 15+ years of experience specializing in
institutional-grade options strategies. You never chase cheap options. You always
think in terms of defined risk, probability of profit, and risk/reward.

━━━ IV RANK DECISION TREE (follow this exactly) ━━━
  IVR > 70  → NEVER buy options. Sell premium only (credit spreads, iron condors)
  IVR 50-70 → Lean SELL. Credit spreads preferred. Debit spreads too expensive.
  IVR 35-50 → BALANCED. Both debit and credit spreads viable.
  IVR 20-35 → Lean BUY. Debit spreads preferred. IDEAL for directional plays.
  IVR < 20  → NEVER sell premium. Long options / debit spreads or no trade.

━━━ SHORT STRIKE DELTA (critical for credit spreads) ━━━
  Use 20-25 delta for short strikes on credit spreads and iron condors.
  NEVER use 10-15 delta — premium collected is too small after commissions.
  Historical max-expectancy zone: 20-25 delta (79-74% OTM probability, $0.70-1.60 premium).

━━━ DTE SELECTION ━━━
  Credit spreads: 21-35 DTE (enter in theta acceleration zone, exit at 50% profit)
  Debit spreads: 21-30 DTE (enough time for thesis to play out, delta still high)
  Iron condors: 30-45 DTE (wide enough, exit at 21 DTE to avoid gamma risk)
  NEVER: buy weeklies for directional plays (theta destroys small accounts)

━━━ CORE TRADING RULES ━━━
1. Never buy options when IV Rank > 70 — you are buying inflated vol
2. Never sell options when IV Rank < 20 — premium is not worth the risk
3. Always use spreads to cap max loss — naked options only at high conviction + IVR < 40
4. Minimum R/R ratio 1.5:1 for directional trades
5. Never trade earnings unless position expires AFTER earnings AND max loss < 2% of account
6. Liquidity gate: OI > 100, volume > 50, bid/ask spread < 15% of mid
7. Stop loss: exit at 2x the initial debit for long spreads, 200% of credit received for shorts
8. Profit target: 50% of credit received for credit spreads, 80-100% of width for debit spreads
9. Always set trailing stop: once at 50% of max profit, never give back below breakeven

━━━ STRATEGY SELECTION BY REGIME ━━━
  bull_trend:       bull call spreads (IVR<50) or bull put spreads (IVR>40)
  bear_trend:       bear put spreads (IVR<50) or bear call spreads (IVR>40)
  ranging:          iron condors (IVR>50) — ONLY when regime confirmed ranging
  high_volatility:  iron condors (wider strikes) — elevated premium compensates gamma
  low_volatility:   long debit spreads — cheap vol, directional conviction required
  crisis:           CASH ONLY or tiny defined-risk positions

━━━ PDT AWARENESS ━━━
  PDT-exempt account — no day-trade frequency restriction.
  You MAY design intraday entries and exits freely.
  ORB30 breakouts, VWAP mean-reversion scalps, and same-day profit-taking are all permitted.
  Still prioritise multi-day holds when the thesis requires it, but never avoid same-day exits
  out of PDT concern — it does not apply here.

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
    subscriptions = [AgentTopic.CONVICTION_SCORE]

    async def handle(self, message: AgentMessage) -> None:
        session_id = message.session_id

        # Conviction score already waited for regime + news — read state immediately
        session = await self._state.get(session_id)
        if not session:
            self._log.error("[%s] session not found", session_id)
            return

        if not session.market_snapshot:
            self._log.warning("[%s] no market snapshot, skipping strategy", session_id)
            return

        # Conviction gate — skip strategy generation if score is too low
        conviction_pre = session.conviction_score or {}
        if conviction_pre.get("gate") == "no_trade":
            self._log.warning(
                "[%s] conviction gate=no_trade (score=%.0f) — skipping strategy generation",
                session_id,
                conviction_pre.get("total_score", 0),
            )
            await self._publish_error(
                session_id,
                f"Conviction score {conviction_pre.get('total_score', 0):.0f}/100 below no-trade threshold",
            )
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

        from datetime import timedelta

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

        # Snap to real strikes in live/paper mode — prevents IBKR qualifyContractsAsync failures
        if self._settings.trading_mode in ("live", "paper"):
            legs = await self._snap_strikes_to_chain(ticker, expiration, legs)

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

    async def _snap_strikes_to_chain(
        self, ticker: str, expiration: date, legs: list[SpreadLeg]
    ) -> list[SpreadLeg]:
        """
        Replace each leg's strike with the nearest real strike from the live
        options chain.  Prevents qualifyContractsAsync failures when Claude
        picks a strike that doesn't exist on IBKR.  No-ops gracefully when the
        chain fetch fails (rate-limited, market closed, etc.).
        """
        from ..services.market_data.yfinance_provider import YFinanceProvider

        try:
            provider = YFinanceProvider()
            chain = await provider.get_options_chain(ticker, expiration)
        except Exception as exc:
            self._log.warning("[%s] chain fetch failed — using Claude strikes as-is: %s", ticker, exc)
            return legs

        if chain is None:
            return legs

        calls_strikes = sorted({c.strike for c in chain.calls})
        puts_strikes = sorted({p.strike for p in chain.puts})

        snapped: list[SpreadLeg] = []
        for leg in legs:
            available = calls_strikes if leg.option_type == "call" else puts_strikes
            if not available:
                snapped.append(leg)
                continue
            nearest = min(available, key=lambda s, t=leg.strike: abs(s - t))
            if nearest != leg.strike:
                self._log.info(
                    "[%s] snapping %s strike %.1f → %.1f (chain)",
                    ticker, leg.option_type, leg.strike, nearest,
                )
                leg = leg.model_copy(update={"strike": nearest})
            snapped.append(leg)
        return snapped

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
        premarket = session.premarket_context or {}

        # Options chain intelligence (max pain + unusual flow) — fast, no Claude
        from ..services.options_flow import detect_unusual_flow, get_max_pain
        try:
            max_pain_data = get_max_pain(snapshot.ticker)
            unusual_flow  = detect_unusual_flow(snapshot.ticker)
        except Exception:
            max_pain_data = {}
            unusual_flow  = {}

        def _fmt(v: float | None, fmt: str = ".2f", prefix: str = "", suffix: str = "") -> str:
            return f"{prefix}{v:{fmt}}{suffix}" if v is not None else "N/A"

        tier = self._settings.account_tier
        t = _TIER_RULES[tier]

        # Read conviction score if available — unlocks additional strategies
        conviction = session.conviction_score or {}
        conv_total = conviction.get("total_score", 50)
        conv_gate = conviction.get("gate", "standard")
        conv_max_contracts = conviction.get("max_contracts", 1)
        allow_long_options = conviction.get("allow_long_options", False)
        conv_reasoning = conviction.get("reasoning", "")

        conviction_block = (
            f"  Conviction score: {conv_total}/100 [{conv_gate.upper()}]\n"
            f"  Max contracts (conviction-gated): {conv_max_contracts}\n"
            f"  Long calls/puts allowed: {'YES — high conviction + cheap IV' if allow_long_options else 'NO — use spreads'}\n"
            f"  Scoring: {conv_reasoning}"
        )

        tier_block = (
            f"  Tier: {t['label']}\n"
            f"  Allowed strategies: {t['allowed']}\n"
            f"  FORBIDDEN strategies: {t['forbidden']}\n"
            f"  DTE guidance: {t['dte']}\n"
            f"  Max contracts (tier cap): {t['max_contracts']}\n"
            f"  Context: {t['notes']}"
        )

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

PRE-MARKET CONTEXT:
  Opening gap: {premarket.get('gap_type', 'unknown')} ({premarket.get('gap_pct', 0)*100:+.2f}%) {premarket.get('gap_direction', '')}
  Gap-fill probability: {premarket.get('gap_fill_probability', 'N/A')}
  ES futures move: {premarket.get('es_futures_move_pct', 'N/A')}%
  VIX spot: {premarket.get('vix_spot', 'N/A')} | Term structure: {premarket.get('vix_term_structure', 'unknown')}
  Opening bias: {premarket.get('opening_bias', 'neutral')} (strength={premarket.get('bias_strength', 0.5):.0%})
  Macro risk today: {premarket.get('macro_risk', 'unknown')} | Events: {premarket.get('macro_events_today', [])}

OPTIONS CHAIN INTELLIGENCE:
  Max pain strike: ${max_pain_data.get('max_pain_strike', 'N/A')}
  Put/call ratio: {max_pain_data.get('put_call_ratio', 'N/A')}
  Unusual flow: {unusual_flow.get('urgency', 'normal')} | Dominant: {unusual_flow.get('dominant_side', 'neutral')}
  Call vol ratio: {unusual_flow.get('call_vol_ratio', 'N/A')}x | Put vol ratio: {unusual_flow.get('put_vol_ratio', 'N/A')}x
  Note: price tends to gravitate toward max pain strike near expiration.
  Note: unusual call flow (>2x) supports bullish thesis; unusual put flow supports bearish.

ACCOUNT TIER CONSTRAINTS (MANDATORY — do not violate):
{tier_block}

CONVICTION SCORE (determines strategy unlocks):
{conviction_block}

REQUIREMENTS:
- Min R/R: {self._settings.min_reward_risk_ratio}:1
- Max IV rank to buy options: 70
- Min liquidity: OI > {self._settings.min_open_interest}, Vol > {self._settings.min_volume}
- Max position: ${self._settings.max_position_dollars:,.0f}
- Contracts: use EXACTLY {conv_max_contracts} contract(s) — conviction-gated cap, do not exceed.
- Strategy unlock: {'Long calls or puts are ALLOWED (high conviction + cheap IV). You MAY recommend a naked long call or put instead of a spread if the setup is exceptional.' if allow_long_options else 'Use spreads only — conviction or IV conditions not met for naked long options.'}
- PDT status: {'PDT EXEMPT — no day-trade frequency limit. Intraday entries and same-day exits are freely permitted.' if getattr(self._settings, 'pdt_exempt', False) else ('PDT ACTIVE — account < $25K on margin. Design position to hold OVERNIGHT. Short strikes MUST be 20-25 delta for any credit spread.' if self._settings.account_size < 25_000 else 'PDT not restricted — account >= $25K.')}
- Short strike delta (credit spreads): MUST be 20-25 delta — never 10-15 delta.
- Define your complete strategy with all strikes, expirations, and exact entry conditions.
"""

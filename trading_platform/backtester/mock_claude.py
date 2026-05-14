"""
Deterministic mock Claude client for backtesting.

Replaces the live Anthropic API so backtests run fast (<1 second per day)
without burning API credits. Responses are generated from the market data
itself using rule-based heuristics that approximate what Claude would say.

This is intentionally simple — the goal is to test the pipeline mechanics
and position sizing, not to simulate Claude's intelligence.
"""

from __future__ import annotations

import math
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from ..core.models.market import MarketSnapshot


def _tool_response(payload: dict[str, Any]) -> MagicMock:
    block = MagicMock()
    block.type = "tool_use"
    block.name = "structured_output"
    block.input = payload
    resp = MagicMock()
    resp.content = [block]
    return resp


def _classify_regime(snapshot: MarketSnapshot) -> dict[str, Any]:
    """Rule-based regime classification matching RegimeOutput schema."""
    price = snapshot.price
    sma20 = snapshot.sma_20 or price
    sma50 = snapshot.sma_50 or price
    sma200 = snapshot.sma_200 or price
    vix = snapshot.vix or 18.0
    rsi = snapshot.rsi_14 or 50.0

    above_sma20 = price > sma20
    above_sma50 = price > sma50
    above_sma200 = price > sma200
    trend_count = sum([above_sma20, above_sma50, above_sma200])

    if vix > 40:
        regime = "crisis"
        strategies = ["long_put", "bear_put_spread"]
    elif vix > 25:
        regime = "high_volatility"
        strategies = ["iron_condor", "strangle", "bear_put_spread"]
    elif vix < 13:
        regime = "low_volatility"
        strategies = ["long_call", "long_put", "calendar_spread"]
    elif trend_count >= 2 and rsi < 70:
        regime = "bull_trend"
        strategies = ["bull_call_spread", "cash_secured_put", "diagonal_spread"]
    elif trend_count <= 1 and rsi > 30:
        regime = "bear_trend"
        strategies = ["bear_put_spread", "long_put"]
    else:
        regime = "ranging"
        strategies = ["iron_condor", "iron_butterfly", "cash_secured_put"]

    vix_trend = "flat"
    trend_dir = "bullish" if trend_count >= 2 else ("bearish" if trend_count <= 1 else "neutral")
    breadth = min(1.0, max(-1.0, (trend_count - 1.5) / 1.5))
    confidence = 0.65 + (0.2 if trend_count in (0, 3) else 0.0)

    return {
        "regime": regime,
        "confidence": round(confidence, 2),
        "vix_trend": vix_trend,
        "trend_direction": trend_dir,
        "breadth_score": round(breadth, 2),
        "reasoning": (
            f"Price {'above' if above_sma50 else 'below'} SMA50 (${sma50:.2f}), "
            f"VIX={vix:.1f}, RSI={rsi:.1f}. "
            f"Trend alignment: {trend_count}/3 MAs. "
            f"Regime: {regime}."
        ),
        "regime_suitable_strategies": strategies,
    }


def _build_strategy(snapshot: MarketSnapshot, regime_result: dict | None, account_size: float = 10_000.0) -> dict[str, Any]:
    """Rule-based strategy generation matching StrategyOutput schema."""
    price = snapshot.price
    atr = snapshot.atr_14 or (price * 0.01)
    iv_rank = snapshot.iv_rank or 50.0
    vix = snapshot.vix or 18.0
    regime = (regime_result or {}).get("regime", "ranging") if regime_result else "ranging"
    dte = 21  # 3-week standard expiry

    # Strategy selection by regime
    if regime == "bull_trend" and iv_rank < 60:
        strategy = "bull_call_spread"
        direction = "bullish"
        buy_strike = round(price * 1.005 / 5) * 5   # slight OTM
        sell_strike = buy_strike + round(atr * 1.5 / 5) * 5
        entry = round(atr * 0.35, 2)
        stop = round(entry * 0.5, 2)
        target = round((sell_strike - buy_strike - entry) * 0.8, 2)
        max_loss = entry * 100
        max_gain = (sell_strike - buy_strike - entry) * 100
        iv_risk = "low" if iv_rank < 40 else "moderate"

    elif regime == "bear_trend" and iv_rank < 60:
        strategy = "bear_put_spread"
        direction = "bearish"
        buy_strike = round(price * 0.995 / 5) * 5
        sell_strike = buy_strike - round(atr * 1.5 / 5) * 5
        entry = round(atr * 0.35, 2)
        stop = round(entry * 0.5, 2)
        target = round((buy_strike - sell_strike - entry) * 0.8, 2)
        max_loss = entry * 100
        max_gain = (buy_strike - sell_strike - entry) * 100
        iv_risk = "low" if iv_rank < 40 else "moderate"

    else:  # ranging or high IV → iron condor
        strategy = "iron_condor"
        direction = "neutral"
        otm_width = round(atr * 1.2 / 5) * 5
        call_sell = round((price + otm_width) / 5) * 5
        call_buy = call_sell + round(atr * 0.8 / 5) * 5
        put_sell = round((price - otm_width) / 5) * 5
        put_buy = put_sell - round(atr * 0.8 / 5) * 5
        wing = max(5.0, call_buy - call_sell)  # wing width must be positive
        # Cap credit at 35% of wing so max_loss is always positive
        entry = round(min(atr * 0.20, wing * 0.35), 2)
        stop = round(entry * 2.0, 2)   # 2× credit = max loss trigger
        target = round(entry * 0.5, 2)
        buy_strike = put_buy
        sell_strike = call_sell
        max_loss = round((wing - entry) * 100, 2)   # always > 0 now
        max_gain = round(entry * 100, 2)
        iv_risk = "high" if iv_rank > 70 else "moderate"

    rr = round(max_gain / max_loss, 2) if max_loss > 0 else 1.5
    # Size to 2% account risk per trade — true compound sizing
    risk_budget = account_size * 0.02
    contracts = max(1, int(risk_budget / max(1.0, max_loss)))

    return {
        "ticker": snapshot.ticker,
        "direction": direction,
        "thesis": (
            f"{snapshot.ticker} in {regime} regime. "
            f"Price ${price:.2f}, {'above' if snapshot.sma_50 and price > snapshot.sma_50 else 'below'} SMA50. "
            f"IV rank {iv_rank:.0f}/100, VIX {vix:.1f}. "
            f"ATR(14) ${atr:.2f}. "
            f"Strategy: {strategy} targeting defined-risk setup with {rr:.1f}:1 R/R."
        ),
        "strategy": strategy,
        "legs": [
            {"option_type": "call" if direction == "bullish" else "put",
             "strike": float(buy_strike), "expiration_dte": dte,
             "action": "buy", "quantity": contracts},
            {"option_type": "call" if direction == "bullish" else "put",
             "strike": float(sell_strike), "expiration_dte": dte,
             "action": "sell", "quantity": contracts},
        ],
        "strike_selection_logic": (
            f"Buy {'ATM' if direction != 'neutral' else 'OTM put'} strike, "
            f"sell {'1-ATR wing' if direction != 'neutral' else 'OTM call'} to cap risk."
        ),
        "expiration_dte": dte,
        "entry_trigger": f"{snapshot.ticker} price holds {'above' if direction == 'bullish' else 'below'} ${buy_strike:.2f}",
        "entry_price": float(entry),
        "stop_loss": float(stop),
        "stop_loss_logic": "Exit at 50% loss on debit, or 2× credit for condor",
        "profit_target": float(target),
        "profit_target_logic": "Exit at 80% of max profit",
        "max_loss_dollars": float(max_loss),
        "max_gain_dollars": float(max_gain),
        "reward_risk_ratio": rr,
        "contracts": contracts,
        "position_size_dollars": float(entry * contracts * 100),
        "iv_crush_risk": iv_risk,
        "earnings_risk": "none",
        "regime_confirms": True,
        "regime_notes": f"{regime} confirmed",
    }


def _assess_risk(strategy: dict, settings_dict: dict) -> dict[str, Any]:
    """Rule-based risk assessment matching RiskOutput schema."""
    rr = strategy.get("reward_risk_ratio", 0)
    iv_rank = strategy.get("iv_crush_risk", "low")
    max_loss = strategy.get("max_loss_dollars", 0)
    min_rr = settings_dict.get("min_reward_risk_ratio", 1.5)
    # max_position cap scales with account — 5% of current balance
    account_size = settings_dict.get("account_size", 10_000.0)
    max_pos = settings_dict.get("max_position_dollars", account_size * 0.05)

    vetos = []
    if rr < min_rr:
        vetos.append(f"R/R {rr:.1f} below minimum {min_rr}")
    if max_loss > max_pos:
        vetos.append(f"Max loss ${max_loss:.0f} exceeds limit ${max_pos:.0f}")

    approved = len(vetos) == 0
    return {
        "approved": approved,
        "veto_reasons": vetos,
        "portfolio_delta": 0.15 if strategy.get("direction") == "bullish" else (-0.15 if strategy.get("direction") == "bearish" else 0.0),
        "correlation_risk": "low",
        "tail_risk_notes": "Defined risk — max loss is capped at entry debit.",
        "liquidity_assessment": "ETF options — highly liquid.",
        "summary": (
            "Trade approved — all risk checks pass." if approved
            else f"Trade rejected: {'; '.join(vetos)}."
        ),
    }


def _review(strategy: dict, risk: dict) -> dict[str, Any]:
    """Rule-based review matching ReviewOutput schema."""
    if not risk.get("approved", False):
        return {
            "decision": "REJECTED",
            "confidence": 0.9,
            "approval_notes": f"Risk manager vetoed: {risk.get('summary', '')}",
            "rejection_reasons": risk.get("veto_reasons", []),
            "suggested_modifications": [],
            "watchlist_conditions": "",
        }

    rr = strategy.get("reward_risk_ratio", 0)
    # Approve if R/R ≥ 2.0, watchlist if 1.5–2.0
    if rr >= 2.0:
        decision = "APPROVED"
        confidence = 0.80
        notes = f"Strong setup: R/R {rr:.1f}:1, defined risk, regime confirms."
    else:
        decision = "WATCHLIST"
        confidence = 0.65
        notes = f"R/R {rr:.1f}:1 acceptable but below 2:1 preference. Monitor for better entry."

    return {
        "decision": decision,
        "confidence": confidence,
        "approval_notes": notes,
        "rejection_reasons": [],
        "suggested_modifications": [],
        "watchlist_conditions": "" if decision != "WATCHLIST" else "Wait for IV contraction or better R/R entry.",
    }


def make_mock_client(snapshot_ref: dict) -> MagicMock:
    """
    Build a mock AsyncAnthropic client whose responses are derived
    from the market snapshot stored in snapshot_ref["snap"].

    snapshot_ref is a mutable dict so the caller can update it
    before each agent call — all agents share one client instance.
    """
    regime_cache: dict[str, Any] = {}

    async def _create(**kwargs) -> MagicMock:
        system = kwargs.get("system", [])
        text = " ".join(
            (s.get("text", "") if isinstance(s, dict) else str(s))
            for s in (system if isinstance(system, list) else [system])
        ).lower()

        snap: MarketSnapshot = snapshot_ref.get("snap")
        account_size: float = snapshot_ref.get("account_size", 10_000.0)

        if "quantitative market regime" in text:
            result = _classify_regime(snap)
            regime_cache.update(result)
            return _tool_response(result)

        if "professional options trader" in text:
            result = _build_strategy(snap, regime_cache, account_size=account_size)
            regime_cache["_last_strategy"] = result  # share with risk + reviewer
            return _tool_response(result)

        if "systematic risk manager" in text:
            strategy = regime_cache.get("_last_strategy", {"reward_risk_ratio": 2.0, "max_loss_dollars": 200})
            result = _assess_risk(
                strategy,
                {"min_reward_risk_ratio": 1.5, "account_size": account_size},
            )
            regime_cache["_risk_result"] = result  # share with reviewer
            return _tool_response(result)

        if "senior options trading desk reviewer" in text:
            strategy = regime_cache.get("_last_strategy", {})
            risk = regime_cache.get("_risk_result", {"approved": True, "summary": "", "veto_reasons": []})
            result = _review(strategy, risk)
            return _tool_response(result)

        # News (shouldn't reach here — news agent short-circuits if no headlines)
        if "news catalyst" in text:
            return _tool_response({
                "sentiment": "neutral",
                "sentiment_score": 0.0,
                "catalyst_type": "none",
                "has_earnings_risk": False,
                "earnings_date": None,
                "headline_count": 0,
                "key_headlines": [],
                "summary": "No catalyst detected — neutral backdrop for options strategies.",
            })

        raise RuntimeError(f"mock_claude: unmatched system prompt: {text[:80]!r}")

    client = MagicMock()
    client.messages.create = AsyncMock(side_effect=_create)
    return client

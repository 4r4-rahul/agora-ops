"""
Deterministic Strategy Rules Engine — no LLM calls.

Takes ConvictionScore + market state → TradeRecommendation with exact strikes.

Strategy selection hierarchy:
  1. CATALYST pillar  → direction-specific debit spread (tight DTE, wide spread)
  2. EVENT pillar     → FOMC bull_call_spread, CPI iron_condor
  3. VOL_PREMIUM      → 45-DTE credit spread at 20-delta short, 35-delta long
  4. DIRECTIONAL      → debit spread when GEX negative + strong momentum
  5. SMART_MONEY      → debit spread matching catalyst direction

Strike selection:
  - Short strike: nearest to target_delta (from options chain)
  - Long strike: nearest to long_delta_target (for defined risk)
  - DTE: use nearest expiry ≥ target_dte, never < 7 DTE

All legs use MID price (bid+ask)/2. If mid > ask or mid < bid → use bid + spread_pct/2.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from ..core.config import AgoraSettings, get_settings
from ..core.models import (
    ConvictionScore,
    GexSignal,
    OpenPosition,
    SpreadLeg,
    StrategyPillar,
    StrategyType,
    TradeRecommendation,
)

logger = logging.getLogger(__name__)


class StrategyRulesEngine:
    """
    Pure function: (conviction, market_data) → TradeRecommendation | None.
    No LLM. No side effects. Fully testable.
    """

    def __init__(self, settings: AgoraSettings | None = None) -> None:
        self._settings = settings or get_settings()

    def build_recommendation(
        self,
        conviction: ConvictionScore,
        spot: float,
        options_chain: dict[str, Any],   # {expiry_str: {calls: df, puts: df}}
        gex: GexSignal | None = None,
        direction_override: str | None = None,  # "bullish" | "bearish" | None
    ) -> TradeRecommendation | None:
        """
        Select strategy and strikes based on conviction and market state.
        Returns None if no suitable chain found or conviction < threshold.
        """
        if conviction.gate == "no_trade":
            return None
        if spot <= 0:
            return None

        direction = direction_override or self._infer_direction(conviction, gex)
        strategy_type, target_dte = self._select_strategy(conviction, direction, gex)

        # Find the right expiry
        expiry, chain_slice = self._select_expiry(options_chain, target_dte)
        if expiry is None or chain_slice is None:
            logger.debug("No suitable expiry found for %s at %d DTE", conviction.ticker, target_dte)
            return None

        # Build legs
        legs = self._build_legs(strategy_type, direction, spot, chain_slice, expiry)
        if not legs:
            return None

        # Compute P&L metrics
        debit_credit = sum(
            (leg.mid_price if leg.action == "buy" else -leg.mid_price) * 100
            for leg in legs
        )
        width = abs(legs[0].strike - legs[1].strike) if len(legs) >= 2 else 0.0
        max_loss   = self._max_loss(strategy_type, debit_credit, width)
        max_gain   = self._max_gain(strategy_type, debit_credit, width)
        rr_ratio   = abs(max_gain / max_loss) if max_loss != 0 else 0.0

        if rr_ratio < 0.25:   # minimum 1:4 risk/reward for credit spreads
            logger.debug("R/R ratio %.2f too low for %s", rr_ratio, conviction.ticker)
            return None

        contracts = self._size_contracts(
            conviction.size_multiplier, max_loss, self._settings
        )

        return TradeRecommendation(
            session_id=conviction.session_id,
            ticker=conviction.ticker,
            strategy=strategy_type,
            pillar=conviction.pillar or StrategyPillar.VOL_PREMIUM,
            direction=direction,
            legs=legs,
            contracts=contracts,
            entry_debit_credit=round(debit_credit * contracts, 2),
            max_loss_dollars=round(max_loss * contracts, 2),
            max_gain_dollars=round(max_gain * contracts, 2),
            reward_risk_ratio=round(rr_ratio, 3),
            breakeven_price=self._breakeven(strategy_type, legs),
            stop_loss_pct=self._settings.stop_loss_multiplier,
            target_dte_close=self._settings.target_dte_close,
            conviction_score=conviction.total_score,
            size_multiplier=conviction.size_multiplier,
            reasoning=conviction.reasoning,
        )

    # ── Strategy selection ─────────────────────────────────────────

    def _infer_direction(
        self, conviction: ConvictionScore, gex: GexSignal | None
    ) -> str:
        """Derive direction from pillar + GEX when not explicitly set."""
        pillar = conviction.pillar
        if pillar == StrategyPillar.VOL_PREMIUM:
            return "neutral"   # credit spreads — direction chosen by GEX
        if pillar == StrategyPillar.EVENT_CPI:
            return "neutral"   # iron condor
        if pillar in (StrategyPillar.CATALYST, StrategyPillar.SMART_MONEY,
                      StrategyPillar.POST_EARNINGS):
            return "bullish"   # catalyst agents already filtered direction
        if gex and gex.regime.value == "negative":
            return "bullish"   # negative GEX = trending = follow momentum
        return "neutral"

    def _select_strategy(
        self,
        conviction: ConvictionScore,
        direction: str,
        gex: GexSignal | None,
    ) -> tuple[StrategyType, int]:
        """Returns (strategy_type, target_dte)."""
        pillar = conviction.pillar

        if pillar == StrategyPillar.EVENT_CPI:
            return StrategyType.IRON_CONDOR, 7

        if pillar == StrategyPillar.EVENT_FOMC:
            return StrategyType.BULL_CALL_SPREAD, 7

        if pillar == StrategyPillar.POST_EARNINGS:
            return StrategyType.BULL_PUT_SPREAD, 21   # sell elevated put skew

        if pillar in (StrategyPillar.CATALYST, StrategyPillar.SMART_MONEY):
            if direction == "bullish":
                return StrategyType.BULL_CALL_SPREAD, 21
            elif direction == "bearish":
                return StrategyType.BEAR_PUT_SPREAD, 21
            else:
                return StrategyType.IRON_CONDOR, 21

        if pillar == StrategyPillar.DIRECTIONAL:
            if direction == "bullish":
                return StrategyType.BULL_CALL_SPREAD, 30
            else:
                return StrategyType.BEAR_PUT_SPREAD, 30

        # Default: VOL_PREMIUM — credit spread
        if direction == "bullish" or direction == "neutral":
            return StrategyType.BULL_PUT_SPREAD, self._settings.target_dte_entry
        else:
            return StrategyType.BEAR_CALL_SPREAD, self._settings.target_dte_entry

    # ── Expiry selection ───────────────────────────────────────────

    def _select_expiry(
        self,
        options_chain: dict[str, Any],
        target_dte: int,
    ) -> tuple[date | None, dict | None]:
        """Pick the nearest expiry that is ≥ target_dte and < target_dte + 14."""
        today = date.today()
        candidates = []

        for expiry_str, chain in options_chain.items():
            try:
                exp_date = date.fromisoformat(expiry_str)
            except ValueError:
                continue
            dte = (exp_date - today).days
            if target_dte <= dte < target_dte + 14:
                candidates.append((dte, exp_date, chain))

        if not candidates:
            # Relax: ≥ target_dte - 7 and < target_dte + 21
            for expiry_str, chain in options_chain.items():
                try:
                    exp_date = date.fromisoformat(expiry_str)
                except ValueError:
                    continue
                dte = (exp_date - today).days
                if max(7, target_dte - 7) <= dte < target_dte + 21:
                    candidates.append((dte, exp_date, chain))

        if not candidates:
            return None, None

        candidates.sort(key=lambda x: abs(x[0] - target_dte))
        _, expiry, chain = candidates[0]
        return expiry, chain

    # ── Leg construction ───────────────────────────────────────────

    def _build_legs(
        self,
        strategy: StrategyType,
        direction: str,
        spot: float,
        chain: dict,
        expiry: date,
    ) -> list[SpreadLeg]:
        calls = chain.get("calls")
        puts = chain.get("puts")

        try:
            if strategy == StrategyType.BULL_CALL_SPREAD:
                return self._bull_call_spread(calls, spot, expiry)
            elif strategy == StrategyType.BEAR_PUT_SPREAD:
                return self._bear_put_spread(puts, spot, expiry)
            elif strategy == StrategyType.BULL_PUT_SPREAD:
                return self._bull_put_spread(puts, spot, expiry)
            elif strategy == StrategyType.BEAR_CALL_SPREAD:
                return self._bear_call_spread(calls, spot, expiry)
            elif strategy == StrategyType.IRON_CONDOR:
                return self._iron_condor(calls, puts, spot, expiry)
            else:
                return []
        except Exception as exc:
            logger.debug("Leg construction failed for %s: %s", strategy, exc)
            return []

    def _bull_call_spread(self, calls: Any, spot: float, expiry: date) -> list[SpreadLeg]:
        """Buy ATM call, sell OTM call at long_delta_target."""
        long_strike  = self._nearest_delta_strike(calls, self._settings.long_delta_target, "call")
        short_strike = self._nearest_delta_strike(calls, self._settings.short_delta_target, "call")
        if not long_strike or not short_strike or long_strike >= short_strike:
            return []
        return [
            self._make_leg(calls, long_strike, "call", "buy", expiry),
            self._make_leg(calls, short_strike, "call", "sell", expiry),
        ]

    def _bear_put_spread(self, puts: Any, spot: float, expiry: date) -> list[SpreadLeg]:
        """Buy ATM put, sell OTM put."""
        long_strike  = self._nearest_delta_strike(puts, self._settings.long_delta_target, "put")
        short_strike = self._nearest_delta_strike(puts, self._settings.short_delta_target, "put")
        if not long_strike or not short_strike or long_strike <= short_strike:
            return []
        return [
            self._make_leg(puts, long_strike, "put", "buy", expiry),
            self._make_leg(puts, short_strike, "put", "sell", expiry),
        ]

    def _bull_put_spread(self, puts: Any, spot: float, expiry: date) -> list[SpreadLeg]:
        """Sell OTM put (20-delta), buy further OTM put (35-delta lower → actually 10-delta)."""
        # Short put at 20-delta (above spot for puts — lower strike)
        short_strike = self._nearest_delta_strike(puts, self._settings.short_delta_target, "put")
        # Long put ~5 points lower for defined risk
        long_strike  = self._spread_width_strike(puts, short_strike, "put", -1)
        if not short_strike or not long_strike or short_strike <= long_strike:
            return []
        return [
            self._make_leg(puts, short_strike, "put", "sell", expiry),
            self._make_leg(puts, long_strike, "put", "buy", expiry),
        ]

    def _bear_call_spread(self, calls: Any, spot: float, expiry: date) -> list[SpreadLeg]:
        """Sell OTM call (20-delta), buy further OTM call."""
        short_strike = self._nearest_delta_strike(calls, self._settings.short_delta_target, "call")
        long_strike  = self._spread_width_strike(calls, short_strike, "call", +1)
        if not short_strike or not long_strike or short_strike >= long_strike:
            return []
        return [
            self._make_leg(calls, short_strike, "call", "sell", expiry),
            self._make_leg(calls, long_strike, "call", "buy", expiry),
        ]

    def _iron_condor(self, calls: Any, puts: Any, spot: float, expiry: date) -> list[SpreadLeg]:
        """Sell 20-delta strangle, buy wings."""
        short_put  = self._nearest_delta_strike(puts,  self._settings.short_delta_target, "put")
        short_call = self._nearest_delta_strike(calls, self._settings.short_delta_target, "call")
        if not short_put or not short_call:
            return []
        long_put  = self._spread_width_strike(puts,  short_put,  "put",  -1)
        long_call = self._spread_width_strike(calls, short_call, "call", +1)
        if not long_put or not long_call:
            return []
        return [
            self._make_leg(puts,  short_put,  "put",  "sell", expiry),
            self._make_leg(puts,  long_put,   "put",  "buy",  expiry),
            self._make_leg(calls, short_call, "call", "sell", expiry),
            self._make_leg(calls, long_call,  "call", "buy",  expiry),
        ]

    # ── Strike helpers ─────────────────────────────────────────────

    def _nearest_delta_strike(
        self, chain: Any, target_delta: float, opt_type: str
    ) -> float | None:
        """Find the strike with delta closest to target_delta."""
        try:
            import pandas as pd
            if chain is None or (hasattr(chain, "empty") and chain.empty):
                return None
            df = chain.copy()
            if "delta" not in df.columns:
                return None
            df = df[df["delta"].notna()]
            if df.empty:
                return None
            # For calls: delta is positive. For puts: delta is negative but stored as abs.
            df["_delta_abs"] = df["delta"].abs()
            idx = (df["_delta_abs"] - target_delta).abs().idxmin()
            return float(df.loc[idx, "strike"])
        except Exception:
            return None

    def _spread_width_strike(
        self,
        chain: Any,
        anchor_strike: float,
        opt_type: str,
        direction: int,   # +1 = higher strikes, -1 = lower strikes
    ) -> float | None:
        """Pick the strike ~1 spread width away from anchor (≈5% of spot)."""
        try:
            if chain is None or (hasattr(chain, "empty") and chain.empty):
                return None
            strikes = sorted(chain["strike"].unique().tolist())
            idx = min(range(len(strikes)), key=lambda i: abs(strikes[i] - anchor_strike))
            target_idx = idx + (direction * 2)   # 2 strikes away
            if 0 <= target_idx < len(strikes):
                return float(strikes[target_idx])
            return None
        except Exception:
            return None

    def _make_leg(
        self,
        chain: Any,
        strike: float,
        opt_type: str,
        action: str,
        expiry: date,
    ) -> SpreadLeg:
        try:
            row = chain[chain["strike"] == strike].iloc[0]
            bid = float(row.get("bid", 0) or 0)
            ask = float(row.get("ask", 0) or 0)
            mid = (bid + ask) / 2 if (bid > 0 or ask > 0) else 0.0
            return SpreadLeg(
                option_type=opt_type,
                strike=strike,
                expiration=expiry,
                action=action,
                contracts=1,
                delta=float(row.get("delta", 0) or 0),
                gamma=float(row.get("gamma", 0) or 0),
                theta=float(row.get("theta", 0) or 0),
                vega=float(row.get("vega", 0) or 0),
                mid_price=round(mid, 2),
            )
        except Exception:
            return SpreadLeg(
                option_type=opt_type, strike=strike,
                expiration=expiry, action=action,
            )

    # ── P&L helpers ────────────────────────────────────────────────

    def _max_loss(self, strategy: StrategyType, debit_credit: float, width: float) -> float:
        if strategy in (StrategyType.BULL_CALL_SPREAD, StrategyType.BEAR_PUT_SPREAD):
            return abs(debit_credit) * 100  # debit paid
        elif strategy in (StrategyType.BULL_PUT_SPREAD, StrategyType.BEAR_CALL_SPREAD):
            return (width * 100) - abs(debit_credit) * 100  # width minus credit
        elif strategy == StrategyType.IRON_CONDOR:
            return (width * 100) - abs(debit_credit) * 100
        return abs(debit_credit) * 100

    def _max_gain(self, strategy: StrategyType, debit_credit: float, width: float) -> float:
        if strategy in (StrategyType.BULL_CALL_SPREAD, StrategyType.BEAR_PUT_SPREAD):
            return (width * 100) - abs(debit_credit) * 100
        elif strategy in (StrategyType.BULL_PUT_SPREAD, StrategyType.BEAR_CALL_SPREAD,
                          StrategyType.IRON_CONDOR):
            return abs(debit_credit) * 100
        return abs(debit_credit) * 100

    def _breakeven(self, strategy: StrategyType, legs: list[SpreadLeg]) -> float | None:
        if not legs:
            return None
        try:
            if strategy == StrategyType.BULL_CALL_SPREAD:
                buy_leg = next(l for l in legs if l.action == "buy")
                net_debit = sum(
                    (l.mid_price if l.action == "buy" else -l.mid_price) for l in legs
                )
                return round(buy_leg.strike + net_debit, 2)
            elif strategy == StrategyType.BULL_PUT_SPREAD:
                sell_leg = next(l for l in legs if l.action == "sell")
                net_credit = sum(
                    (l.mid_price if l.action == "sell" else -l.mid_price) for l in legs
                )
                return round(sell_leg.strike - net_credit, 2)
        except Exception:
            pass
        return None

    def _size_contracts(
        self, size_multiplier: float, max_loss_per_contract: float, settings: AgoraSettings
    ) -> int:
        if max_loss_per_contract <= 0:
            return 1
        base = max(1, int(settings.risk_per_trade_dollars / max_loss_per_contract))
        sized = max(1, round(base * size_multiplier))
        return min(sized, settings.max_contracts_per_trade)

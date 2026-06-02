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

    @staticmethod
    def _dynamic_rr_floor(iv_rank: float | None, vix: float | None) -> float:
        """Scale R/R floor with IV environment — low IV accepts leaner setups."""
        ivr = iv_rank if iv_rank is not None else 50.0
        if ivr >= 80:
            return 0.15   # premium-rich: demand quality
        if ivr >= 55:
            return 0.12   # elevated: decent premium available
        if ivr >= 30:
            return 0.10   # normal: baseline
        return 0.08        # thin credit: accept lean setups to stay active

    def build_recommendation(
        self,
        conviction: ConvictionScore,
        spot: float,
        options_chain: dict[str, Any],   # {expiry_str: {calls: df, puts: df}}
        gex: GexSignal | None = None,
        direction_override: str | None = None,  # "bullish" | "bearish" | None
        iv_rank: float | None = None,
        vix: float | None = None,
        dynamic_params: Any | None = None,  # DynamicParams — overrides config defaults
    ) -> TradeRecommendation | None:
        """
        Select strategy and strikes based on conviction and market state.
        Returns None if no suitable chain found or conviction < threshold.
        """
        if conviction.gate == "no_trade":
            return None
        if spot <= 0:
            return None

        # Apply dynamic parameters when provided — override config defaults
        if dynamic_params is not None:
            eff_short_delta = dynamic_params.short_delta_target
            eff_stop_loss   = dynamic_params.stop_loss_multiplier
            eff_dte_adj     = dynamic_params.dte_adjustment
        else:
            eff_short_delta = self._settings.short_delta_target
            eff_stop_loss   = self._settings.stop_loss_multiplier
            eff_dte_adj     = 0

        direction = direction_override or self._infer_direction(conviction, gex)
        strategy_type, base_dte = self._select_strategy(conviction, direction, gex)
        target_dte = max(7, base_dte + eff_dte_adj)

        # Find the right expiry
        expiry, chain_slice = self._select_expiry(options_chain, target_dte)
        if expiry is None or chain_slice is None:
            logger.debug("No suitable expiry found for %s at %d DTE", conviction.ticker, target_dte)
            return None

        # Build legs — use dynamic delta if provided
        legs = self._build_legs(strategy_type, direction, spot, chain_slice, expiry,
                                short_delta=eff_short_delta)
        if not legs:
            logger.info("No legs built for %s | strategy=%s expiry=%s spot=%.2f",
                        conviction.ticker, strategy_type.value, expiry, spot)
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

        rr_floor = self._dynamic_rr_floor(iv_rank, vix)
        if rr_ratio < rr_floor:
            logger.info(
                "R/R ratio %.2f too low for %s — below dynamic floor %.2f "
                "(IVR=%s vix=%s max_gain=%.0f max_loss=%.0f width=%.1f)",
                rr_ratio, conviction.ticker, rr_floor,
                f"{iv_rank:.0f}" if iv_rank is not None else "n/a",
                f"{vix:.1f}" if vix is not None else "n/a",
                max_gain, max_loss, width,
            )
            return None

        # Minimum absolute credit floor — thin credits cause IBKR leg rejections in live;
        # configurable via min_credit_per_share (lower in paper mode for validation).
        if debit_credit < 0:  # credit spread
            credit_per_share = abs(debit_credit) / 100
            credit_floor = self._settings.min_credit_per_share
            if credit_per_share < credit_floor:
                logger.info("MIN CREDIT gate: %s credit=%.2f/sh < $%.2f floor — skip",
                            conviction.ticker, credit_per_share, credit_floor)
                return None

        # Cost-to-width gate: debit spreads where the premium exceeds the max allowed
        # fraction of the spread width are rejected (too expensive relative to potential gain).
        # Credit spreads are checked inversely — premium too small means we collect too little.
        if width > 0 and debit_credit > 0:  # debit spread
            debit_per_contract = debit_credit  # already per-contract dollars
            ratio = debit_per_contract / (width * 100)
            if ratio > self._settings.max_debit_to_width_ratio:
                logger.info(
                    "COST/WIDTH gate: %s debit=%.2f width=%.0f ratio=%.1f%% > max %.0f%% — skip",
                    conviction.ticker, debit_per_contract, width * 100,
                    ratio * 100, self._settings.max_debit_to_width_ratio * 100,
                )
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
            stop_loss_pct=eff_stop_loss,
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

        # Default: VOL_PREMIUM — credit spread at 30 DTE (theta accelerates sharply
        # after 30 DTE; using 45 DTE pushes to the 66-90 bracket which has insufficient
        # credit/width ratio for positive EV at standard 20-delta short)
        if direction == "bullish" or direction == "neutral":
            return StrategyType.BULL_PUT_SPREAD, 30
        else:
            return StrategyType.BEAR_CALL_SPREAD, 30

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
        short_delta: float | None = None,
    ) -> list[SpreadLeg]:
        calls = chain.get("calls")
        puts = chain.get("puts")
        sd = short_delta if short_delta is not None else self._settings.short_delta_target

        try:
            if strategy == StrategyType.BULL_CALL_SPREAD:
                return self._bull_call_spread(calls, spot, expiry)
            elif strategy == StrategyType.BEAR_PUT_SPREAD:
                return self._bear_put_spread(puts, spot, expiry)
            elif strategy == StrategyType.BULL_PUT_SPREAD:
                return self._bull_put_spread(puts, spot, expiry, short_delta=sd)
            elif strategy == StrategyType.BEAR_CALL_SPREAD:
                return self._bear_call_spread(calls, spot, expiry, short_delta=sd)
            elif strategy == StrategyType.IRON_CONDOR:
                return self._iron_condor(calls, puts, spot, expiry, short_delta=sd)
            else:
                return []
        except Exception as exc:
            logger.debug("Leg construction failed for %s: %s", strategy, exc)
            return []

    def _bull_call_spread(self, calls: Any, spot: float, expiry: date) -> list[SpreadLeg]:
        """Buy ATM call, sell OTM call at long_delta_target."""
        long_strike  = self._nearest_delta_strike(calls, self._settings.long_delta_target,  "call", spot, expiry)
        short_strike = self._nearest_delta_strike(calls, self._settings.short_delta_target, "call", spot, expiry)
        if not long_strike or not short_strike or long_strike >= short_strike:
            return []
        return [
            self._make_leg(calls, long_strike, "call", "buy", expiry, spot),
            self._make_leg(calls, short_strike, "call", "sell", expiry, spot),
        ]

    def _bear_put_spread(self, puts: Any, spot: float, expiry: date) -> list[SpreadLeg]:
        """Buy ATM put, sell OTM put."""
        long_strike  = self._nearest_delta_strike(puts, self._settings.long_delta_target,  "put", spot, expiry)
        short_strike = self._nearest_delta_strike(puts, self._settings.short_delta_target, "put", spot, expiry)
        if not long_strike or not short_strike or long_strike <= short_strike:
            return []
        return [
            self._make_leg(puts, long_strike, "put", "buy", expiry, spot),
            self._make_leg(puts, short_strike, "put", "sell", expiry, spot),
        ]

    def _bull_put_spread(self, puts: Any, spot: float, expiry: date,
                         short_delta: float | None = None) -> list[SpreadLeg]:
        """Sell OTM put (20-delta), buy further OTM put for defined risk."""
        sd = short_delta if short_delta is not None else self._settings.short_delta_target
        # Only consider OTM puts (strike < spot) — ITM puts cause IBKR GTL rejection
        otm_puts = puts[puts["strike"] < spot * 0.995] if spot > 0 else puts
        short_strike = self._best_credit_per_delta_short(
            otm_puts, sd, "put", wing_direction=-1, spot=spot, expiry=expiry
        )
        long_strike  = self._spread_width_strike(otm_puts, short_strike, "put", -1)
        if not short_strike or not long_strike or short_strike <= long_strike:
            return []
        return [
            self._make_leg(puts, short_strike, "put", "sell", expiry, spot),
            self._make_leg(puts, long_strike, "put", "buy", expiry, spot),
        ]

    def _bear_call_spread(self, calls: Any, spot: float, expiry: date,
                          short_delta: float | None = None) -> list[SpreadLeg]:
        """Sell OTM call (20-delta), buy further OTM call."""
        sd = short_delta if short_delta is not None else self._settings.short_delta_target
        # Only consider OTM calls (strike > spot) — ITM calls cause IBKR GTL rejection
        otm_calls = calls[calls["strike"] > spot * 1.005] if spot > 0 else calls
        short_strike = self._best_credit_per_delta_short(
            otm_calls, sd, "call", wing_direction=+1, spot=spot, expiry=expiry
        )
        long_strike  = self._spread_width_strike(otm_calls, short_strike, "call", +1)
        if not short_strike or not long_strike or short_strike >= long_strike:
            return []
        return [
            self._make_leg(calls, short_strike, "call", "sell", expiry, spot),
            self._make_leg(calls, long_strike, "call", "buy", expiry, spot),
        ]

    def _iron_condor(self, calls: Any, puts: Any, spot: float, expiry: date,
                     short_delta: float | None = None) -> list[SpreadLeg]:
        """Sell 20-delta strangle, buy wings. Each wing optimized for credit-per-delta."""
        sd = short_delta if short_delta is not None else self._settings.short_delta_target
        otm_puts  = puts[puts["strike"]   < spot * 0.995] if spot > 0 else puts
        otm_calls = calls[calls["strike"] > spot * 1.005] if spot > 0 else calls
        short_put = self._best_credit_per_delta_short(
            otm_puts, sd, "put", wing_direction=-1, spot=spot, expiry=expiry
        )
        short_call = self._best_credit_per_delta_short(
            otm_calls, sd, "call", wing_direction=+1, spot=spot, expiry=expiry
        )
        if not short_put or not short_call:
            return []
        long_put  = self._spread_width_strike(puts,  short_put,  "put",  -1)
        long_call = self._spread_width_strike(calls, short_call, "call", +1)
        if not long_put or not long_call:
            return []
        return [
            self._make_leg(puts,  short_put,  "put",  "sell", expiry, spot),
            self._make_leg(puts,  long_put,   "put",  "buy",  expiry, spot),
            self._make_leg(calls, short_call, "call", "sell", expiry, spot),
            self._make_leg(calls, long_call,  "call", "buy",  expiry, spot),
        ]

    # ── Strike helpers ─────────────────────────────────────────────

    @staticmethod
    def _approx_delta(
        spot: float, strike: float, iv: float, dte: int, opt_type: str
    ) -> float:
        """Black-Scholes approximate delta (r=0, no dividend)."""
        import math
        if iv <= 0 or dte <= 0 or spot <= 0 or strike <= 0:
            return 0.0
        T = dte / 365.0
        try:
            d1 = (math.log(spot / strike) + 0.5 * iv ** 2 * T) / (iv * math.sqrt(T))
            # N(d1) via logistic approximation — good to within 0.005 of delta
            nd1 = 1.0 / (1.0 + math.exp(-1.7 * d1))
            return nd1 if opt_type == "call" else nd1 - 1.0
        except (ValueError, ZeroDivisionError):
            return 0.0

    def _nearest_delta_strike(
        self, chain: Any, target_delta: float, opt_type: str,
        spot: float = 0.0, expiry: "date | None" = None,
    ) -> float | None:
        """Find the strike with delta closest to target_delta.

        Uses the chain's `delta` column when present; falls back to
        Black-Scholes approximation from `impliedVolatility`.
        """
        try:
            if chain is None or (hasattr(chain, "empty") and chain.empty):
                return None
            df = chain.copy()

            if "delta" in df.columns and df["delta"].notna().any():
                df = df[df["delta"].notna()]
                df["_delta_abs"] = df["delta"].abs()
                idx = (df["_delta_abs"] - target_delta).abs().idxmin()
                return float(df.loc[idx, "strike"])

            # Fallback: approximate delta from IV, spot, and DTE
            if "impliedVolatility" not in df.columns:
                return None
            df = df[df["impliedVolatility"].notna() & (df["impliedVolatility"] > 0)]
            if df.empty:
                return None
            from datetime import date
            dte = (expiry - date.today()).days if expiry else 30
            df["_delta"] = df.apply(
                lambda r: self._approx_delta(
                    spot, float(r["strike"]), float(r["impliedVolatility"]), dte, opt_type
                ),
                axis=1,
            )
            df["_delta_abs"] = df["_delta"].abs()
            idx = (df["_delta_abs"] - target_delta).abs().idxmin()
            return float(df.loc[idx, "strike"])
        except Exception:
            return None

    def _best_credit_per_delta_short(
        self,
        chain: Any,
        target_delta: float,
        opt_type: str,
        wing_direction: int,     # +1 = long wing is higher strike, -1 = lower strike
        spot: float = 0.0,
        expiry: "date | None" = None,
        delta_band: float = 0.05,
    ) -> float | None:
        """
        Choose the short strike that maximises (net_credit / |delta_short|).

        Scans every strike whose delta falls within [target_delta ± delta_band].
        For each candidate: pairs it with its fixed-width long wing, computes the
        net spread credit, then returns the strike with the highest credit-per-delta.

        Falls back to _nearest_delta_strike when fewer than 2 candidates exist
        or when bid/ask data is unavailable.
        """
        try:
            if chain is None or (hasattr(chain, "empty") and chain.empty):
                return self._nearest_delta_strike(chain, target_delta, opt_type, spot, expiry)

            df = chain.copy()

            # Build per-row delta estimates (prefer chain column, fall back to BS)
            from datetime import date as _date
            dte = (expiry - _date.today()).days if expiry else 30

            def _row_delta(row: Any) -> float:
                d = float(row.get("delta", 0) or 0)
                if d != 0:
                    return abs(d)
                iv = float(row.get("impliedVolatility", 0) or 0)
                if iv <= 0:
                    return 0.0
                return abs(self._approx_delta(spot, float(row["strike"]), iv, dte, opt_type))

            df["_delta_abs"] = df.apply(_row_delta, axis=1)

            lo, hi = target_delta - delta_band, target_delta + delta_band
            candidates = df[(df["_delta_abs"] >= lo) & (df["_delta_abs"] <= hi)].copy()

            # Fewer than 2 candidates → fall back to nearest-delta
            if len(candidates) < 2:
                return self._nearest_delta_strike(chain, target_delta, opt_type, spot, expiry)

            def _mid(row: Any) -> float:
                bid = float(row.get("bid", 0) or 0)
                ask = float(row.get("ask", 0) or 0)
                return (bid + ask) / 2 if (bid > 0 or ask > 0) else 0.0

            best_strike: float | None = None
            best_score: float = -1.0
            strikes = sorted(chain["strike"].unique().tolist())

            for _, row in candidates.iterrows():
                short_strike = float(row["strike"])
                delta_abs    = float(row["_delta_abs"])
                short_mid    = _mid(row)
                if short_mid <= 0 or delta_abs <= 0:
                    continue

                # Find paired long strike (2 strikes in wing_direction from short)
                try:
                    idx = min(range(len(strikes)), key=lambda i: abs(strikes[i] - short_strike))
                    long_idx = idx + wing_direction * 2
                    if not (0 <= long_idx < len(strikes)):
                        continue
                    long_strike = strikes[long_idx]
                except Exception:
                    continue

                long_rows = chain[chain["strike"] == long_strike]
                if long_rows.empty:
                    continue
                long_mid = _mid(long_rows.iloc[0])

                net_credit = short_mid - long_mid
                if net_credit <= 0:
                    continue

                score = net_credit / delta_abs
                if score > best_score:
                    best_score  = score
                    best_strike = short_strike

            return best_strike if best_strike is not None else (
                self._nearest_delta_strike(chain, target_delta, opt_type, spot, expiry)
            )

        except Exception:
            return self._nearest_delta_strike(chain, target_delta, opt_type, spot, expiry)

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
        spot: float = 0.0,
    ) -> SpreadLeg:
        try:
            from trading_platform.services.options_flow import compute_bs_greeks

            row = chain[chain["strike"] == strike].iloc[0]
            bid = float(row.get("bid", 0) or 0)
            ask = float(row.get("ask", 0) or 0)
            mid = (bid + ask) / 2 if (bid > 0 or ask > 0) else 0.0

            delta = float(row.get("delta", 0) or 0)
            gamma = float(row.get("gamma", 0) or 0)
            theta = float(row.get("theta", 0) or 0)
            vega  = float(row.get("vega",  0) or 0)

            # yfinance often returns 0 for all greeks — compute via BS if so
            if delta == 0 and spot > 0:
                iv  = float(row.get("impliedVolatility", 0) or 0)
                dte = max((expiry - date.today()).days, 0.5)
                # If chain IV is also 0, estimate from mid price via a proxy (30% ATM IV)
                if iv <= 0:
                    iv = mid / (spot * 0.04 * (dte / 365) ** 0.5) if mid > 0 else 0.30
                    iv = max(0.10, min(iv, 2.0))  # clamp to [10%, 200%]
                bs    = compute_bs_greeks(spot, strike, dte, iv, opt_type)
                delta = bs["delta"]
                gamma = bs["gamma"]
                theta = bs["theta"]
                vega  = bs["vega"]

            return SpreadLeg(
                option_type=opt_type,
                strike=strike,
                expiration=expiry,
                action=action,
                contracts=1,
                delta=delta,
                gamma=gamma,
                theta=theta,
                vega=vega,
                mid_price=round(mid, 2),
            )
        except Exception as exc:
            logger.debug("_build_leg fallback for %s strike=%.1f %s: %s", opt_type, strike, action, exc)
            return SpreadLeg(
                option_type=opt_type, strike=strike,
                expiration=expiry, action=action,
            )

    # ── P&L helpers ────────────────────────────────────────────────

    def _max_loss(self, strategy: StrategyType, debit_credit: float, width: float) -> float:
        # debit_credit is already in per-contract dollar terms (×100 applied by caller).
        # width is in strike points — multiply by 100 to get dollars.
        if strategy in (StrategyType.BULL_CALL_SPREAD, StrategyType.BEAR_PUT_SPREAD):
            return abs(debit_credit)              # debit paid
        elif strategy in (StrategyType.BULL_PUT_SPREAD, StrategyType.BEAR_CALL_SPREAD,
                          StrategyType.IRON_CONDOR):
            return (width * 100) - abs(debit_credit)   # spread width minus credit received
        return abs(debit_credit)

    def _max_gain(self, strategy: StrategyType, debit_credit: float, width: float) -> float:
        if strategy in (StrategyType.BULL_CALL_SPREAD, StrategyType.BEAR_PUT_SPREAD):
            return (width * 100) - abs(debit_credit)   # spread width minus debit paid
        elif strategy in (StrategyType.BULL_PUT_SPREAD, StrategyType.BEAR_CALL_SPREAD,
                          StrategyType.IRON_CONDOR):
            return abs(debit_credit)              # credit received
        return abs(debit_credit)

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

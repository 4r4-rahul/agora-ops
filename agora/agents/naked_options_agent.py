"""
agora/agents/naked_options_agent.py — Naked Put/Call Specialist

Deterministic (zero LLM calls). Scans the options universe every 30 minutes
on its own independent cycle, separate from the spread pipeline.

Philosophy:
  • Naked puts: sell OTM put at 16Δ, collect theta. Bullish/neutral bias.
  • Naked calls: sell OTM call at 16Δ, directional conviction required.
  • No spread protection — maximum premium capture, paper-mode validation only.
  • All risk gates bypassed except earnings blackout + duplicate-ticker.

DTE selection (regime + IVR conditioned):
  IVR > 60            → 21 DTE   capture IV crush fast
  IVR 40-60           → 28 DTE   standard theta burn zone
  IVR 20-40           → 38 DTE   need more DTE to collect premium
  IVR < 20            → 45 DTE   thin IV, go longer for any premium
  High-beta stock (β>1.5) → compress DTE by 20% (floor 14)
  Event detected within DTE window → skip (earnings blackout enforced by caller)

Exit rules (enforced by PositionManager / ExitManagement):
  Profit target: 50% of premium collected (buy back at half price)
  Hard stop:     2× premium collected (buy back at 2× what we sold for)
  DTE floor:     21 DTE remaining → close (same as spreads)
"""

from __future__ import annotations

import logging
import math
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from agora.core.config import AgoraSettings, get_settings
from agora.core.models import (
    SpreadLeg,
    StrategyPillar,
    StrategyType,
    TradeRecommendation,
)

logger = logging.getLogger(__name__)

# ── DTE lookup table — (IVR_min, IVR_max, base_dte) ──────────────────────────
_DTE_TABLE = [
    (60.0, 100.0, 21),
    (40.0,  60.0, 28),
    (20.0,  40.0, 38),
    ( 0.0,  20.0, 45),
]
_HIGH_BETA_THRESHOLD = 1.5
_HIGH_BETA_DTE_MULTIPLIER = 0.80
_DTE_FLOOR = 14

# ── Known beta approximations for liquid tickers ─────────────────────────────
# Used only for DTE compression. Source: 1-year rolling beta estimates.
# Missing tickers default to beta=1.0 (no compression).
_BETA_MAP: dict[str, float] = {
    "TSLA": 2.1, "NVDA": 1.9, "AMD":  1.8, "MSTR": 3.5, "IREN": 2.8,
    "RKLB": 2.4, "PLTR": 2.0, "UPST": 2.6, "HOOD": 2.2, "CIFR": 3.0,
    "ASTS": 2.5, "OKLO": 2.3, "META": 1.4, "AAPL": 1.2, "MSFT": 1.1,
    "SPY":  1.0, "QQQ":  1.1, "IWM":  1.2, "GLD":  0.2, "TLT":  0.1,
}


# ── Output ────────────────────────────────────────────────────────────────────

@dataclass
class NakedDecision:
    ticker:        str
    strategy:      str          # "naked_put" | "naked_call" | "skip"
    outcome:       str          # "filled" | "skipped" | "blocked" | "error"
    block_reason:  str
    recommendation: TradeRecommendation | None
    # Diagnostic fields logged to naked_journal
    dte:           int   = 0
    dte_reason:    str   = ""
    strike:        float = 0.0
    delta_approx:  float = 0.0
    premium:       float = 0.0
    ivr:           float = 0.0
    vix:           float = 0.0
    regime:        str   = ""
    flow_direction: str  = ""


# ── Agent ─────────────────────────────────────────────────────────────────────

class NakedOptionsAgent:
    """
    Deterministic naked put/call selector.

    Inputs  : ticker, spot, options_chain, macro_context, flow_signals
    Outputs : NakedDecision (recommendation=TradeRecommendation | None)

    No LLM calls. Fully testable. Side effect: writes naked_journal row.
    """

    def __init__(self, settings: AgoraSettings | None = None) -> None:
        self._settings = settings or get_settings()

    # ── Public API ────────────────────────────────────────────────────────────

    def evaluate(
        self,
        ticker:        str,
        spot:          float,
        options_chain: dict[str, Any],   # {expiry_str: {calls: df, puts: df}}
        macro_context: Any | None,       # MacroContext from MacroSynthesizer
        flow_signals:  Any | None,       # FlowSignals from flow_detector
        session_id:    str = "",
        open_positions: list[Any] | None = None,
    ) -> NakedDecision:
        """Evaluate one ticker and return a NakedDecision."""
        try:
            return self._evaluate_inner(
                ticker, spot, options_chain, macro_context,
                flow_signals, session_id, open_positions or [],
            )
        except Exception as exc:
            logger.warning("NakedOptionsAgent error [%s]: %s", ticker, exc)
            return NakedDecision(
                ticker=ticker, strategy="skip", outcome="error",
                block_reason=str(exc), recommendation=None,
            )

    def _evaluate_inner(
        self,
        ticker:        str,
        spot:          float,
        options_chain: dict[str, Any],
        macro_context: Any | None,
        flow_signals:  Any | None,
        session_id:    str,
        open_positions: list[Any],
    ) -> NakedDecision:

        ivr    = float(getattr(macro_context, "iv_rank",      50.0) or 50.0)
        vix    = float(getattr(macro_context, "vix",          18.0) or 18.0)
        regime = str(  getattr(macro_context, "macro_stance", "normal") or "normal")

        # Crisis kill switch
        if regime == "crisis":
            return NakedDecision(ticker=ticker, strategy="skip", outcome="skipped",
                                 block_reason="crisis regime — no new positions",
                                 recommendation=None, ivr=ivr, vix=vix, regime=regime)

        # Determine option type (puts default; calls only on bearish macro + flow)
        flow_dir = "neutral"
        if flow_signals:
            flow_dir = str(getattr(flow_signals, "direction", "neutral") or "neutral")
        macro_stance = str(getattr(macro_context, "macro_stance", "neutral") or "neutral")

        if macro_stance == "risk_off" and flow_dir == "bearish":
            opt_type  = "call"
            direction = "bearish"
            strategy  = StrategyType.NAKED_CALL
        else:
            opt_type  = "put"
            direction = "bullish"
            strategy  = StrategyType.NAKED_PUT

        # DTE selection
        target_dte, dte_reason = self._select_dte(ticker, ivr, regime)

        # Find expiry
        expiry, chain_slice = self._select_expiry(options_chain, target_dte)
        if expiry is None or chain_slice is None:
            return NakedDecision(
                ticker=ticker, strategy=str(strategy), outcome="skipped",
                block_reason=f"no suitable expiry for DTE={target_dte}",
                recommendation=None, dte=target_dte, dte_reason=dte_reason,
                ivr=ivr, vix=vix, regime=regime, flow_direction=flow_dir,
            )

        dte = (expiry - date.today()).days
        chain_df = chain_slice.get("puts" if opt_type == "put" else "calls")
        if chain_df is None or (hasattr(chain_df, "empty") and chain_df.empty):
            return NakedDecision(
                ticker=ticker, strategy=str(strategy), outcome="skipped",
                block_reason=f"empty {opt_type} chain for {expiry}",
                recommendation=None, dte=dte, dte_reason=dte_reason,
                ivr=ivr, vix=vix, regime=regime, flow_direction=flow_dir,
            )

        # Strike selection: nearest to target delta, OTM only
        target_delta = self._settings.naked_options_target_delta
        strike, delta_approx = self._select_strike(
            chain_df, opt_type, spot, expiry, target_delta
        )
        if strike is None:
            return NakedDecision(
                ticker=ticker, strategy=str(strategy), outcome="skipped",
                block_reason="could not find suitable strike near target delta",
                recommendation=None, dte=dte, dte_reason=dte_reason,
                ivr=ivr, vix=vix, regime=regime, flow_direction=flow_dir,
            )

        # Validate OTM: puts must be below spot, calls above
        if opt_type == "put" and strike >= spot:
            return NakedDecision(
                ticker=ticker, strategy=str(strategy), outcome="skipped",
                block_reason=f"strike {strike} is not OTM for put (spot={spot:.2f})",
                recommendation=None, dte=dte, dte_reason=dte_reason,
                ivr=ivr, vix=vix, regime=regime, flow_direction=flow_dir,
            )
        if opt_type == "call" and strike <= spot:
            return NakedDecision(
                ticker=ticker, strategy=str(strategy), outcome="skipped",
                block_reason=f"strike {strike} is not OTM for call (spot={spot:.2f})",
                recommendation=None, dte=dte, dte_reason=dte_reason,
                ivr=ivr, vix=vix, regime=regime, flow_direction=flow_dir,
            )

        # Get premium (mid price of the option)
        premium_per_sh = self._get_mid(chain_df, strike)
        if premium_per_sh <= 0:
            return NakedDecision(
                ticker=ticker, strategy=str(strategy), outcome="skipped",
                block_reason=f"zero/missing premium for {ticker} {strike}{opt_type[0].upper()}",
                recommendation=None, dte=dte, dte_reason=dte_reason,
                ivr=ivr, vix=vix, regime=regime, flow_direction=flow_dir,
            )

        # Minimum premium gate (per contract = per_sh × 100)
        premium_per_contract = premium_per_sh * 100
        if premium_per_contract < self._settings.naked_options_min_premium:
            return NakedDecision(
                ticker=ticker, strategy=str(strategy), outcome="skipped",
                block_reason=(
                    f"premium ${premium_per_contract:.0f}/contract < "
                    f"${self._settings.naked_options_min_premium:.0f} floor"
                ),
                recommendation=None, dte=dte, dte_reason=dte_reason,
                ivr=ivr, vix=vix, regime=regime, flow_direction=flow_dir,
                strike=strike, delta_approx=delta_approx, premium=premium_per_sh,
            )

        # Liquidity gate: bid-ask spread should be < 20% of mid
        bid_ask_pct = self._bid_ask_pct(chain_df, strike)
        if bid_ask_pct > 0.20:
            return NakedDecision(
                ticker=ticker, strategy=str(strategy), outcome="skipped",
                block_reason=f"bid-ask spread {bid_ask_pct:.0%} > 20% — illiquid",
                recommendation=None, dte=dte, dte_reason=dte_reason,
                ivr=ivr, vix=vix, regime=regime, flow_direction=flow_dir,
                strike=strike, delta_approx=delta_approx, premium=premium_per_sh,
            )

        # Build leg
        leg = SpreadLeg(
            option_type=opt_type,
            strike=strike,
            expiration=expiry,
            action="sell",
            contracts=1,
            delta=delta_approx,
            mid_price=premium_per_sh,
        )

        # Max loss: put → strike × 100 (stock to zero); call → spot × 2 × 100 (proxy)
        if opt_type == "put":
            max_loss_dollars = round(strike * 100, 2)
        else:
            max_loss_dollars = round(spot * 2.0 * 100, 2)
        max_gain_dollars = round(premium_per_sh * 100, 2)

        rec = TradeRecommendation(
            session_id=session_id,
            ticker=ticker,
            strategy=strategy,
            pillar=StrategyPillar.VOL_PREMIUM,
            direction=direction,
            legs=[leg],
            contracts=1,
            entry_debit_credit=round(-premium_per_sh * 100, 2),   # credit received (negative)
            max_loss_dollars=max_loss_dollars,
            max_gain_dollars=max_gain_dollars,
            reward_risk_ratio=round(max_gain_dollars / max_loss_dollars, 4),
            breakeven_price=(
                round(strike - premium_per_sh, 2) if opt_type == "put"
                else round(strike + premium_per_sh, 2)
            ),
            stop_loss_pct=2.0,
            target_dte_close=21,
            conviction_score=50.0,   # fixed — gates are bypassed in naked mode
            size_multiplier=1.0,
            reasoning=(
                f"Naked {opt_type} | IVR={ivr:.0f} | {dte_reason} | "
                f"strike={strike} ({delta_approx:.2f}Δ) | premium=${premium_per_sh:.2f}/sh"
            ),
        )

        logger.info(
            "NakedOptions [%s] %s %s%.0f exp=%s DTE=%d Δ=%.2f prem=$%.2f/sh | %s",
            ticker, str(strategy).upper(), opt_type[0].upper(),
            strike, expiry, dte, abs(delta_approx),
            premium_per_sh, dte_reason,
        )

        return NakedDecision(
            ticker=ticker,
            strategy=str(strategy),
            outcome="filled",        # caller records actual fill; this is "proceed"
            block_reason="",
            recommendation=rec,
            dte=dte,
            dte_reason=dte_reason,
            strike=strike,
            delta_approx=delta_approx,
            premium=premium_per_sh,
            ivr=ivr,
            vix=vix,
            regime=regime,
            flow_direction=flow_dir,
        )

    # ── DTE logic ─────────────────────────────────────────────────────────────

    def _select_dte(self, ticker: str, ivr: float, regime: str) -> tuple[int, str]:
        base_dte = 45  # fallback
        label = "low_ivr_default"
        for ivr_min, ivr_max, dte in _DTE_TABLE:
            if ivr_min <= ivr <= ivr_max:
                base_dte = dte
                label = f"IVR={ivr:.0f}→{dte}DTE"
                break

        beta = _BETA_MAP.get(ticker.upper(), 1.0)
        if beta > _HIGH_BETA_THRESHOLD:
            compressed = max(_DTE_FLOOR, int(base_dte * _HIGH_BETA_DTE_MULTIPLIER))
            label += f" β={beta:.1f}→{compressed}DTE"
            return compressed, label

        return base_dte, label

    # ── Expiry selection (reuses rules_engine logic) ──────────────────────────

    @staticmethod
    def _select_expiry(
        options_chain: dict[str, Any], target_dte: int
    ) -> tuple[date | None, dict | None]:
        today = date.today()
        candidates = []
        for expiry_str, chain in options_chain.items():
            try:
                exp_date = date.fromisoformat(expiry_str)
            except ValueError:
                continue
            dte = (exp_date - today).days
            if max(7, target_dte - 10) <= dte <= target_dte + 14:
                candidates.append((dte, exp_date, chain))
        if not candidates:
            return None, None
        candidates.sort(key=lambda x: abs(x[0] - target_dte))
        _, expiry, chain = candidates[0]
        return expiry, chain

    # ── Strike selection ──────────────────────────────────────────────────────

    @staticmethod
    def _approx_delta(spot: float, strike: float, iv: float, dte: int, opt_type: str) -> float:
        if iv <= 0 or dte <= 0 or spot <= 0 or strike <= 0:
            return 0.0
        T = dte / 365.0
        try:
            d1 = (math.log(spot / strike) + 0.5 * iv ** 2 * T) / (iv * math.sqrt(T))
            nd1 = 1.0 / (1.0 + math.exp(-1.7 * d1))
            return nd1 if opt_type == "call" else nd1 - 1.0
        except (ValueError, ZeroDivisionError):
            return 0.0

    def _select_strike(
        self,
        chain_df: Any,
        opt_type: str,
        spot: float,
        expiry: date,
        target_delta: float,
    ) -> tuple[float | None, float]:
        try:
            import pandas as pd
            df = chain_df.copy()
            dte = (expiry - date.today()).days

            # Filter OTM only
            if opt_type == "put":
                df = df[df["strike"] < spot * 0.999]
            else:
                df = df[df["strike"] > spot * 1.001]

            if df.empty:
                return None, 0.0

            # Prefer chain delta column if present
            if "delta" in df.columns and df["delta"].notna().any():
                df = df[df["delta"].notna()].copy()
                df["_delta_abs"] = df["delta"].abs()
            else:
                if "impliedVolatility" not in df.columns:
                    return None, 0.0
                df = df[df["impliedVolatility"].notna() & (df["impliedVolatility"] > 0)].copy()
                df["_delta_abs"] = df.apply(
                    lambda r: abs(self._approx_delta(
                        spot, float(r["strike"]), float(r["impliedVolatility"]), dte, opt_type
                    )),
                    axis=1,
                )

            if df.empty:
                return None, 0.0

            idx = (df["_delta_abs"] - target_delta).abs().idxmin()
            row = df.loc[idx]
            strike = float(row["strike"])
            delta  = float(row["_delta_abs"])
            return strike, (-delta if opt_type == "put" else delta)
        except Exception as exc:
            logger.debug("NakedOptions strike selection error: %s", exc)
            return None, 0.0

    # ── Price helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _get_mid(chain_df: Any, strike: float) -> float:
        try:
            rows = chain_df[chain_df["strike"] == strike]
            if rows.empty:
                return 0.0
            r = rows.iloc[0]
            bid = float(r.get("bid", 0) or 0)
            ask = float(r.get("ask", 0) or 0)
            if bid > 0 and ask > 0:
                return round((bid + ask) / 2, 4)
            return float(r.get("lastPrice", 0) or 0)
        except Exception:
            return 0.0

    @staticmethod
    def _bid_ask_pct(chain_df: Any, strike: float) -> float:
        try:
            rows = chain_df[chain_df["strike"] == strike]
            if rows.empty:
                return 0.0
            r = rows.iloc[0]
            bid = float(r.get("bid", 0) or 0)
            ask = float(r.get("ask", 0) or 0)
            mid = (bid + ask) / 2
            if mid <= 0:
                return 0.0
            return (ask - bid) / mid
        except Exception:
            return 0.0

    # ── Journal ───────────────────────────────────────────────────────────────

    def journal(self, decision: NakedDecision, db_path: str, position_id: str = "") -> None:
        try:
            with sqlite3.connect(db_path, timeout=10) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                rec = decision.recommendation
                conn.execute("""
                    INSERT INTO naked_journal (
                        position_id, ticker, strategy, direction, decided_at_utc,
                        spot, strike, expiry, dte, delta_approx, premium_per_sh,
                        ivr, vix, regime, flow_direction, dte_reason,
                        outcome, block_reason, max_loss_dollars, max_gain_dollars, contracts
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    position_id or "",
                    decision.ticker,
                    decision.strategy,
                    rec.direction if rec else "",
                    datetime.now(timezone.utc).isoformat(),
                    rec.legs[0].mid_price * 100 if rec else 0,   # spot not stored directly
                    decision.strike,
                    str(rec.legs[0].expiration) if rec else "",
                    decision.dte,
                    decision.delta_approx,
                    decision.premium,
                    decision.ivr,
                    decision.vix,
                    decision.regime,
                    decision.flow_direction,
                    decision.dte_reason,
                    decision.outcome,
                    decision.block_reason,
                    rec.max_loss_dollars  if rec else 0,
                    rec.max_gain_dollars  if rec else 0,
                    rec.contracts         if rec else 0,
                ))
        except Exception as exc:
            logger.debug("naked_journal write error: %s", exc)

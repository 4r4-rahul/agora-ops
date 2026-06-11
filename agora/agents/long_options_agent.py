"""
agora/agents/long_options_agent.py — Long Call / Long Put Swing Specialist

Directional swing trader. Buys calls (bullish) or puts (bearish) with a hard
5-day time stop. Zero LLM calls. Independent 15-minute scan cycle.

Strategy: Momentum Burst + Flow Confirmation  (classic prop-desk flow-momentum confluence)
  • Buy 14-30 DTE options at ~0.42Δ (conviction-scaled) — gamma-responsive over the
    ~5-day hold without overpaying for theta the time stop would discard
  • Per-ticker IVR preferred (≤ IVR cap) — buy cheap before IV expands, not after
  • 2+ confirming signals required (see Signal Stack below)
  • Exit: profit target OR conviction-scaled trailing stop OR 50% flat stop OR 5-day time stop
    (winners run via the trailing stop — the floor widens with conviction to keep the
    convex right tail; see PositionManager._check_long_options_targets)

Why 35Δ for 5-day swings:
  On a 3% underlying move a 35Δ option captures ~35bp × 3% ≈ 105% of
  its premium in delta gain alone.  Gamma acceleration adds more.
  50% profit on a modest directional move is consistently achievable.

DTE Selection (professional 4-factor formula):
  1. Per-ticker IVR base: low IV=buy time(60), high IV=shorter(21) to avoid overpaying vega
  2. Beta compression: high-beta names move fast, shorter DTE to capture earlier
  3. Catalyst ceiling: don't buy through earnings; cap DTE at catalyst-2 days
  4. Hold floor: DTE ≥ hold_days × 3 to ensure enough time to be right

Signal Stack (additive scoring):
  Flow sweep (same direction)         +2   ← institutional intent, highest weight
  Flow directional (no sweep)         +1
  Momentum (RSI+SMA confirmation)     +1
  Negative GEX regime (trending)      +1   ← amplifier: market structure favors moves
  Macro stance alignment              +1
  Relative Strength (10d outperform)  +1   ← price leadership signal
  Volume surge (vol > 2× 20d avg)     +1   ← adds to dominant side only
  News flag (UW premium news)         +1   ← advisory; never sole entry reason

  Net bullish score ≥ 2  →  LONG CALL
  Net bearish score  ≥ 2  →  LONG PUT
  Otherwise               →  SKIP

RSI Extremes Filter (discipline: don't chase):
  RSI > rsi_overbought (72) → block LONG CALL entries (extended — mean reversion risk)
  RSI < rsi_oversold  (28) → block LONG PUT  entries (oversold — bounce risk)

OI Liquidity Gate:
  Min open interest at selected strike = 200 contracts.
  Ensures marketable quotes and avoids wide bid-ask on low-interest strikes.

Contract Sizing (conviction-based, max 3):
  Conviction 2 → 1 contract (minimum directional bet)
  Conviction 3 → 2 contracts (high-confidence signal stack)
  Conviction 4+ → 3 contracts (full-stack confirmation)

IVR Gate (critical for buyers):
  Per-ticker IVR ≤ long_options_ivr_cap (default 60) — options are cheap enough to buy
  IVR > cap → skip; refuse to buy overpriced premium

References:
  • Natenberg, "Option Volatility & Pricing" — delta/gamma mechanics ch.9-11
  • McMillan, "Options as a Strategic Investment" — directional swing plays ch.3
  • Augen, "The Volatility Edge in Options Trading" — IVR timing ch.5
  • Sinclair, "Volatility Trading" — per-ticker IV/HV ratio construction ch.4
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

import numpy as np

from agora.core.config import AgoraSettings, get_settings
from agora.core.models import (
    SpreadLeg,
    StrategyPillar,
    StrategyType,
    TradeRecommendation,
)

logger = logging.getLogger(__name__)

# ── DTE window ────────────────────────────────────────────────────────────────
# v2: this is a directional MOMENTUM swing held ~5 days, so the option must be
# responsive (gamma) over that horizon. The old 21-60 DTE window paid for time a
# 5-day stop discarded and under-geared the bet. 14-30 DTE matches gamma to the hold
# while keeping theta tolerable (14+ DTE is off the steep part of the decay curve).
_DTE_MIN = 14
_DTE_MAX = 30

# ── Beta map (for DTE compression on high-beta names) ────────────────────────
_BETA_MAP: dict[str, float] = {
    "TSLA": 2.1, "NVDA": 1.9, "AMD":  1.8, "MSTR": 3.5, "IREN": 2.8,
    "RKLB": 2.4, "PLTR": 2.0, "UPST": 2.6, "HOOD": 2.2, "CIFR": 3.0,
    "ASTS": 2.5, "OKLO": 2.3, "META": 1.4, "AAPL": 1.2, "MSFT": 1.1,
    "SPY":  1.0, "QQQ":  1.1, "IWM":  1.2, "GLD":  0.2, "TLT":  0.1,
    "AMZN": 1.3, "GOOGL": 1.2, "AVGO": 1.5, "SMTC": 1.6, "KTOS": 1.7,
}


# ── Output ────────────────────────────────────────────────────────────────────

@dataclass
class LongDecision:
    ticker:        str
    strategy:      str           # "long_call" | "long_put" | "skip"
    outcome:       str           # "proceed" | "skipped" | "blocked" | "error"
    block_reason:  str
    recommendation: TradeRecommendation | None

    # Diagnostic snapshot
    dte:             int   = 0
    strike:          float = 0.0
    delta_approx:    float = 0.0
    premium:         float = 0.0
    ivr:             float = 0.0        # portfolio-level (from MacroContext)
    per_ticker_ivr:  float = 0.0        # per-ticker: ATM IV / HV20 proxy
    vix:             float = 0.0
    regime:          str   = ""
    flow_direction:  str   = ""
    momentum_score:  float = 0.0        # RSI14 normalised [0,1]
    conviction:      int   = 0          # net signal score (bull - bear)
    signal_quality:  float = 0.0        # weighted quality of signal stack (0-6+)
    signal_stack:    dict  = field(default_factory=dict)
    dte_reason:      str   = ""         # human-readable DTE selection explanation
    contracts:       int   = 1          # conviction-based contract count
    profit_target_pct: float = 0.50    # conviction-dynamic profit target


# ── Agent ─────────────────────────────────────────────────────────────────────

class LongOptionsAgent:
    """
    Deterministic long call/put swing selector.

    Inputs  : ticker, spot, options_chain, macro_context, flow_signals, momentum,
              per_ticker_ivr, days_to_catalyst
    Outputs : LongDecision (recommendation=TradeRecommendation | None)

    No LLM calls. Fully testable. Side effects: writes long_journal row.
    """

    def __init__(self, settings: AgoraSettings | None = None) -> None:
        self._settings = settings or get_settings()
        self._db_path  = str(getattr(self._settings, "db_path", "trade_journal.db"))

    # ── Signal calibration (deterministic learning loop) ───────────────────────
    # signal_stats is written on every position close (update_signal_stats). Here we
    # read it back so the live scorer self-adjusts: a signal that has historically lost
    # contributes LESS to sizing quality; a proven winner contributes more. This is the
    # auto-applying half of the hybrid learning policy — pure math on realized P&L, no
    # LLM, no human gate. The LLM vetter (gated) sees the same win-rates in its prompt.

    def _load_signal_perf(self) -> dict[tuple[str, str], tuple[float, int]]:
        """
        Return {(signal_name, direction): (win_rate, total_trades)} from signal_stats.
        Empty dict on any error → scorer falls back to neutral (no calibration).
        """
        try:
            import sqlite3
            with sqlite3.connect(self._db_path, timeout=5) as conn:
                rows = conn.execute(
                    "SELECT signal_name, direction, win_rate, total_trades FROM signal_stats"
                ).fetchall()
            return {(r[0], r[1]): (float(r[2] or 0.0), int(r[3] or 0)) for r in rows}
        except Exception as exc:
            logger.debug("_load_signal_perf: %s", exc)
            return {}

    @staticmethod
    def _perf_mult(
        signal_name: str,
        direction:   str,
        perf:        dict[tuple[str, str], tuple[float, int]] | None,
    ) -> float:
        """
        Quality multiplier for one signal based on its historical win rate.

        Bayesian-shrunk toward a 0.5 coin-flip baseline by a pseudo-count k so a
        small sample cannot swing sizing — a signal needs a real track record before
        it moves the needle. Bounded to [0.6, 1.4] so no single signal's history can
        dominate or zero out the live read. Returns 1.0 (neutral) when no data.
        """
        if not perf:
            return 1.0
        rec = perf.get((signal_name, direction))
        if not rec:
            return 1.0
        win_rate, n = rec
        if n < 3:
            return 1.0                      # too few closes to trust
        baseline = 0.5
        k        = 8.0                      # pseudo-count: trades needed to overcome prior
        wins     = win_rate * n
        shrunk   = (wins + k * baseline) / (n + k)
        return max(0.6, min(1.4, shrunk / baseline))

    # Signal quality weights — must match the additive weights used in _score_direction.
    _SIGNAL_WEIGHTS = {"flow": 2.0, "rel_strength": 1.0, "momentum": 0.8,
                       "vol_surge": 0.7, "macro": 0.6, "gex": 0.5, "news": 0.3}

    @staticmethod
    def _score2_dominant_loser(
        direction: str, stack: dict, signal_perf: dict | None,
        floor: float, min_n: int = 8,
    ) -> str | None:
        """Deterministic auto-skip for minimum-conviction (score-2) longs (expert Call A).
        Returns the dominant signal's name when the highest-weight FIRING signal on the winning
        side is a proven loser (win_rate < floor at n>=min_n), else None. Pure math, no LLM —
        this only ever removes a low-conviction entry whose own track record says it loses, and
        it lies dormant until signal_stats shows such a signal."""
        if not signal_perf or floor <= 0:
            return None
        fired = [
            (w, name) for name, w in LongOptionsAgent._SIGNAL_WEIGHTS.items()
            if direction in str(stack.get(name, ""))   # stack annotates 'bullish'/'bearish'
        ]
        if not fired:
            return None
        fired.sort(reverse=True)
        dom = fired[0][1]
        rec = signal_perf.get((dom, direction))
        if rec and rec[1] >= min_n and rec[0] < floor:
            return dom
        return None

    # ── Public API ────────────────────────────────────────────────────────────

    def evaluate(
        self,
        ticker:             str,
        spot:               float,
        options_chain:      dict[str, Any],
        macro_context:      Any | None,
        flow_signals:       Any | None,
        momentum:           dict | None = None,
        gex_regime:         str = "neutral",
        session_id:         str = "",
        per_ticker_ivr:     float | None = None,  # pre-computed: ATM IV / HV20 proxy
        days_to_catalyst:   int | None = None,    # days until earnings / catalyst
        pre_earnings_drift: bool = False,         # T-7 to T-3 window: lower conviction floor
        news_context:       Any | None = None,    # NewsContext from NewsSignalBus
    ) -> LongDecision:
        try:
            return self._evaluate_inner(
                ticker, spot, options_chain, macro_context,
                flow_signals, momentum or {}, gex_regime, session_id,
                per_ticker_ivr, days_to_catalyst, pre_earnings_drift, news_context,
            )
        except Exception as exc:
            logger.warning("LongOptionsAgent error [%s]: %s", ticker, exc, exc_info=True)
            return LongDecision(
                ticker=ticker, strategy="skip", outcome="error",
                block_reason=str(exc), recommendation=None,
            )

    # ── Per-ticker IVR computation ────────────────────────────────────────────

    @staticmethod
    def compute_per_ticker_ivr(
        options_chain: dict[str, Any],
        spot:          float,
        closes:        Any,            # pd.Series or list of recent daily closes
    ) -> float | None:
        """
        Per-ticker IVR proxy: calibrated 0-100 scale from chain ATM IV vs 20-day HV.

        Formula (Sinclair ch.4):
            iv_ratio = atm_iv / hv20
            ivr_proxy = (iv_ratio - 0.7) / 1.3 × 100   # clipped [0, 100]

        Calibration rationale:
            iv_ratio = 0.7 → IVR = 0   (IV deeply below realised vol — very cheap)
            iv_ratio = 1.0 → IVR = 23  (IV at par with HV — below average)
            iv_ratio = 1.3 → IVR = 46  (IV elevated — approaching cap)
            iv_ratio = 2.0 → IVR = 100 (IV well above HV — very expensive to buy)

        Returns None if ATM IV or HV cannot be computed.
        """
        try:
            # Step 1: ATM IV from the first available expiry in [21, 60] DTE
            atm_iv: float | None = None
            today_d = date.today()
            for expiry_str, chain in sorted(options_chain.items()):
                try:
                    exp_date = date.fromisoformat(expiry_str)
                except ValueError:
                    continue
                dte = (exp_date - today_d).days
                if not (_DTE_MIN <= dte <= _DTE_MAX):
                    continue

                calls_df = chain.get("calls")
                puts_df  = chain.get("puts")
                for df in (calls_df, puts_df):
                    if df is None or (hasattr(df, "empty") and df.empty):
                        continue
                    if "impliedVolatility" not in df.columns:
                        continue
                    df_valid = df[df["impliedVolatility"].notna() & (df["impliedVolatility"] > 0)].copy()
                    if df_valid.empty:
                        continue
                    # Row nearest to ATM
                    df_valid["_dist"] = (df_valid["strike"] - spot).abs()
                    atm_row = df_valid.loc[df_valid["_dist"].idxmin()]
                    iv_val = float(atm_row["impliedVolatility"])
                    if 0.02 < iv_val < 3.0:   # sanity: 2% to 300% annualised IV
                        atm_iv = iv_val
                        break
                if atm_iv is not None:
                    break

            if atm_iv is None:
                return None

            # Step 2: 20-day historical volatility (annualised)
            try:
                import pandas as pd
                if isinstance(closes, (list, np.ndarray)):
                    closes_s = pd.Series(closes, dtype=float)
                else:
                    closes_s = pd.Series(closes.values, dtype=float)
                if len(closes_s) < 21:
                    return None
                log_ret = np.log(closes_s / closes_s.shift(1)).dropna()
                hv20 = float(log_ret.tail(20).std() * math.sqrt(252))
                if hv20 <= 0:
                    return None
            except Exception:
                return None

            iv_ratio  = atm_iv / hv20
            ivr_proxy = (iv_ratio - 0.7) / 1.3 * 100.0
            return float(max(0.0, min(100.0, ivr_proxy)))

        except Exception as exc:
            logger.debug("compute_per_ticker_ivr error: %s", exc)
            return None

    # ── Core logic ────────────────────────────────────────────────────────────

    def _evaluate_inner(
        self,
        ticker:             str,
        spot:               float,
        options_chain:      dict[str, Any],
        macro_context:      Any | None,
        flow_signals:       Any | None,
        momentum:           dict,
        gex_regime:         str,
        session_id:         str,
        per_ticker_ivr:     float | None,
        days_to_catalyst:   int | None,
        pre_earnings_drift: bool = False,
        news_context:       Any | None = None,
    ) -> LongDecision:

        # Portfolio-level IVR from MacroContext (SPY-wide, may be None)
        _ivr_raw   = getattr(macro_context, "iv_rank", None)
        macro_ivr  = float(_ivr_raw) if _ivr_raw is not None else None
        vix        = float(getattr(macro_context, "vix",          18.0) or 18.0)
        regime     = str(  getattr(macro_context, "macro_stance", "normal") or "normal")
        # For display: prefer per-ticker IVR; fall back to portfolio IVR; fall back to 0
        ivr_display = per_ticker_ivr if per_ticker_ivr is not None else (
            macro_ivr if macro_ivr is not None else 0.0
        )

        # ── 1. Crisis kill switch ─────────────────────────────────────────────
        if regime == "crisis":
            return LongDecision(
                ticker=ticker, strategy="skip", outcome="skipped",
                block_reason="crisis regime — no directional bets",
                recommendation=None, ivr=ivr_display, vix=vix, regime=regime,
                per_ticker_ivr=per_ticker_ivr or 0.0,
            )

        # ── 2. IVR gate — don't overpay for premium ───────────────────────────
        # Prefer per-ticker IVR (more accurate); fall back to portfolio IVR.
        # Only enforce when we have real data.
        ivr_cap    = self._settings.long_options_ivr_cap
        active_ivr = per_ticker_ivr if per_ticker_ivr is not None else macro_ivr
        if active_ivr is not None and active_ivr > ivr_cap:
            return LongDecision(
                ticker=ticker, strategy="skip", outcome="skipped",
                block_reason=f"IVR={active_ivr:.0f} > cap {ivr_cap:.0f} — options too expensive",
                recommendation=None, ivr=ivr_display, vix=vix, regime=regime,
                per_ticker_ivr=per_ticker_ivr or 0.0,
            )

        # ── 3. Signal scoring ─────────────────────────────────────────────────
        rsi_ob      = self._settings.long_options_rsi_overbought
        rsi_os      = self._settings.long_options_rsi_oversold
        min_conv    = self._settings.long_options_min_conviction
        # Pre-earnings drift: T-7 to T-3 with positive RS → lower conviction floor to 1
        # (stock often drifts toward earnings catalyst — buy the drift, not the event)
        _ret_10d    = momentum.get("ret_10d", 0.0)
        _is_drift   = pre_earnings_drift and _ret_10d > 0.03
        if _is_drift:
            min_conv = 1
        # News flag: resolve ticker-specific direction from NewsSignalBus
        _news_flag: str | None = None
        if news_context is not None:
            _flag = getattr(news_context, "ticker_flags", {}).get(ticker.upper())
            if _flag is not None:
                _news_flag = getattr(_flag, "direction", None)
        # Deterministic learning: load per-signal historical win-rates so the scorer
        # weights proven signals up and chronic losers down (auto-applying channel).
        signal_perf = self._load_signal_perf()
        direction, strategy, signal_stack, conviction, flow_dir, quality = self._score_direction(
            macro_context, flow_signals, momentum, gex_regime, rsi_ob, rsi_os, min_conv,
            news_flag=_news_flag, signal_perf=signal_perf,
            score2_floor=getattr(self._settings, "long_options_score2_min_signal_winrate", 0.0),
            rsi_capitulation_floor=getattr(self._settings, "long_options_rsi_capitulation_floor", 0),
            counter_trend_flow_damp=getattr(self._settings, "long_options_counter_trend_flow_damp", False),
        )
        if _is_drift and direction is not None:
            signal_stack["pre_earnings_drift"] = f"floor→1(dtc={days_to_catalyst}d,rs={_ret_10d:.1%})"
        if direction is None:
            return LongDecision(
                ticker=ticker, strategy="skip", outcome="skipped",
                block_reason=f"insufficient conviction (score={conviction}): {signal_stack}",
                recommendation=None, ivr=ivr_display, vix=vix, regime=regime,
                per_ticker_ivr=per_ticker_ivr or 0.0,
                flow_direction=flow_dir, conviction=conviction, signal_stack=signal_stack,
                momentum_score=momentum.get("rsi_norm", 0.5),
            )

        opt_type = "call" if direction == "bullish" else "put"

        # ── 4. DTE selection — 4-factor professional formula ──────────────────
        hold_days  = self._settings.long_options_max_hold_days
        hv5        = float(momentum.get("hv5",  0.0) or 0.0)
        hv20       = float(momentum.get("hv20", 0.0) or 0.0)
        target_dte, dte_reason = self._select_dte(
            ticker, active_ivr or 30.0, hold_days, days_to_catalyst, hv5, hv20
        )

        # ── 5. Expiry selection ───────────────────────────────────────────────
        expiry, chain_slice = self._select_expiry(options_chain, target_dte)
        if expiry is None:
            return LongDecision(
                ticker=ticker, strategy=str(strategy), outcome="skipped",
                block_reason=f"no expiry in [{_DTE_MIN},{_DTE_MAX}] DTE window",
                recommendation=None, dte=target_dte, ivr=ivr_display, vix=vix, regime=regime,
                per_ticker_ivr=per_ticker_ivr or 0.0,
                flow_direction=flow_dir, conviction=conviction, signal_stack=signal_stack,
                dte_reason=dte_reason,
            )

        dte     = (expiry - date.today()).days
        chain_df = chain_slice.get("calls" if opt_type == "call" else "puts")
        if chain_df is None or (hasattr(chain_df, "empty") and chain_df.empty):
            return LongDecision(
                ticker=ticker, strategy=str(strategy), outcome="skipped",
                block_reason=f"empty {opt_type} chain for {expiry}",
                recommendation=None, dte=dte, ivr=ivr_display, vix=vix, regime=regime,
                per_ticker_ivr=per_ticker_ivr or 0.0,
                flow_direction=flow_dir, conviction=conviction, signal_stack=signal_stack,
                dte_reason=dte_reason,
            )

        # ── 6. Strike selection — conviction-responsive delta, OTM only ─────────
        # Higher conviction → closer to ATM (more intrinsic value, more reliable).
        # Borderline entries → more OTM (lower cost if the weak signal is wrong).
        target_delta = self._conviction_delta(conviction, self._settings.long_options_target_delta)
        strike, delta_approx = self._select_strike(chain_df, opt_type, spot, expiry, target_delta)
        if strike is None:
            return LongDecision(
                ticker=ticker, strategy=str(strategy), outcome="skipped",
                block_reason="no suitable OTM strike near target delta",
                recommendation=None, dte=dte, ivr=ivr_display, vix=vix, regime=regime,
                per_ticker_ivr=per_ticker_ivr or 0.0,
                flow_direction=flow_dir, conviction=conviction, signal_stack=signal_stack,
                dte_reason=dte_reason,
            )

        # OTM validation
        if opt_type == "call" and strike <= spot:
            return LongDecision(
                ticker=ticker, strategy=str(strategy), outcome="skipped",
                block_reason=f"call strike {strike} not OTM (spot={spot:.2f})",
                recommendation=None, dte=dte, ivr=ivr_display, vix=vix, regime=regime,
                per_ticker_ivr=per_ticker_ivr or 0.0,
                flow_direction=flow_dir, conviction=conviction, signal_stack=signal_stack,
                strike=strike, delta_approx=delta_approx, dte_reason=dte_reason,
            )
        if opt_type == "put" and strike >= spot:
            return LongDecision(
                ticker=ticker, strategy=str(strategy), outcome="skipped",
                block_reason=f"put strike {strike} not OTM (spot={spot:.2f})",
                recommendation=None, dte=dte, ivr=ivr_display, vix=vix, regime=regime,
                per_ticker_ivr=per_ticker_ivr or 0.0,
                flow_direction=flow_dir, conviction=conviction, signal_stack=signal_stack,
                strike=strike, delta_approx=delta_approx, dte_reason=dte_reason,
            )

        # ── 7. OI liquidity gate — try adjacent strikes before rejecting ─────────
        # If the nearest-delta strike is illiquid, scan ±2 strikes within ±5Δ
        # tolerance and promote the most liquid alternative.
        min_oi = self._settings.long_options_min_oi
        oi_at_strike = self._get_open_interest(chain_df, strike)
        if oi_at_strike < min_oi:
            alt_strike, alt_delta, alt_oi = self._find_liquid_adjacent(
                chain_df, opt_type, spot, expiry, strike, delta_approx, target_delta, min_oi
            )
            if alt_strike is not None:
                logger.debug(
                    "LongOptions [%s] OI upgrade: %s→%s (OI %d→%d)",
                    ticker, strike, alt_strike, oi_at_strike, alt_oi,
                )
                strike, delta_approx, oi_at_strike = alt_strike, alt_delta, alt_oi
            else:
                return LongDecision(
                    ticker=ticker, strategy=str(strategy), outcome="skipped",
                    block_reason=f"OI={oi_at_strike} < {min_oi} at {strike} and no liquid adjacent strike",
                    recommendation=None, dte=dte, ivr=ivr_display, vix=vix, regime=regime,
                    per_ticker_ivr=per_ticker_ivr or 0.0,
                    flow_direction=flow_dir, conviction=conviction, signal_stack=signal_stack,
                    strike=strike, delta_approx=delta_approx, dte_reason=dte_reason,
                )

        # ── 8. Premium fetch ──────────────────────────────────────────────────
        premium_per_sh = self._get_mid(chain_df, strike)
        if premium_per_sh <= 0:
            return LongDecision(
                ticker=ticker, strategy=str(strategy), outcome="skipped",
                block_reason=f"zero premium for {ticker} {strike}{opt_type[0].upper()}",
                recommendation=None, dte=dte, ivr=ivr_display, vix=vix, regime=regime,
                per_ticker_ivr=per_ticker_ivr or 0.0,
                flow_direction=flow_dir, conviction=conviction, signal_stack=signal_stack,
                strike=strike, delta_approx=delta_approx, dte_reason=dte_reason,
            )

        # ── 9. Minimum premium gate ───────────────────────────────────────────
        premium_per_contract = premium_per_sh * 100
        min_prem = self._settings.long_options_min_premium
        if premium_per_contract < min_prem:
            return LongDecision(
                ticker=ticker, strategy=str(strategy), outcome="skipped",
                block_reason=f"premium ${premium_per_contract:.0f} < ${min_prem:.0f} floor",
                recommendation=None, dte=dte, ivr=ivr_display, vix=vix, regime=regime,
                per_ticker_ivr=per_ticker_ivr or 0.0,
                flow_direction=flow_dir, conviction=conviction, signal_stack=signal_stack,
                strike=strike, delta_approx=delta_approx, premium=premium_per_sh,
                dte_reason=dte_reason,
            )

        # ── 10. Bid-ask spread gate ───────────────────────────────────────────
        bid_ask_pct = self._bid_ask_pct(chain_df, strike)
        if bid_ask_pct > 0.25:
            return LongDecision(
                ticker=ticker, strategy=str(strategy), outcome="skipped",
                block_reason=f"bid-ask {bid_ask_pct:.0%} > 25% — illiquid",
                recommendation=None, dte=dte, ivr=ivr_display, vix=vix, regime=regime,
                per_ticker_ivr=per_ticker_ivr or 0.0,
                flow_direction=flow_dir, conviction=conviction, signal_stack=signal_stack,
                strike=strike, delta_approx=delta_approx, premium=premium_per_sh,
                dte_reason=dte_reason,
            )

        # ── 11. Quality-based contract sizing ────────────────────────────────
        # Uses continuous signal quality score rather than raw conviction step.
        # quality < 2.0 → 1 contract  (minimum; one or two weak signals)
        # quality 2.0-3.4 → 2 contracts (good setup; multiple reliable signals)
        # quality ≥ 3.5 → max contracts (institutional-grade confluence)
        max_contracts = self._settings.long_options_max_contracts
        if quality >= 3.5:
            contracts = max_contracts
        elif quality >= 2.0:
            contracts = min(max_contracts, 2)
        else:
            contracts = 1

        # ── Per-trade dollar risk cap (v2) ────────────────────────────────────
        # No single directional long trade may risk more than max_premium_pct of the
        # account, regardless of contract count. Bounds per-trade concentration and
        # prevents the oversized-contract class of error. If even one contract exceeds
        # the budget, the option is too expensive for this account → skip.
        _max_premium = self._settings.account_size * getattr(
            self._settings, "long_options_max_premium_pct", 0.15)
        if premium_per_contract > 0:
            _affordable = int(_max_premium // premium_per_contract)
            if _affordable < 1:
                return LongDecision(
                    ticker=ticker, strategy=str(strategy), outcome="skipped",
                    block_reason=(f"premium ${premium_per_contract:.0f}/contract > per-trade "
                                  f"risk cap ${_max_premium:.0f} ({getattr(self._settings, 'long_options_max_premium_pct', 0.15):.0%})"),
                    recommendation=None, dte=dte, ivr=ivr_display, vix=vix, regime=regime,
                    per_ticker_ivr=per_ticker_ivr or 0.0,
                    flow_direction=flow_dir, conviction=conviction, signal_stack=signal_stack,
                    strike=strike, delta_approx=delta_approx, dte_reason=dte_reason,
                )
            contracts = min(contracts, _affordable)

        # Conviction-dynamic profit target — let high-conviction winners run further
        profit_target_pct = self._conviction_profit_target(conviction)

        # ── 12. Build recommendation ──────────────────────────────────────────
        leg = SpreadLeg(
            option_type=opt_type,
            strike=strike,
            expiration=expiry,
            action="buy",
            contracts=contracts,
            delta=delta_approx,
            mid_price=premium_per_sh,
        )

        max_loss_dollars = round(premium_per_sh * 100 * contracts, 2)
        # Long-option upside is theoretically unbounded; report the EXIT-POLICY target
        # gain (profit_target × premium) and the target reward:risk below — NOT a
        # fabricated 3:1 — so downstream sizing/attribution isn't fed fictional numbers.
        _sl_pct = float(getattr(self._settings, "long_options_stop_loss_pct", 0.50)) or 0.50
        max_gain_dollars = round(max_loss_dollars * profit_target_pct, 2)

        signals_readable = json.dumps(signal_stack)
        _pt_ivr_str = f"{per_ticker_ivr:.0f}" if per_ticker_ivr is not None else "N/A"
        rec = TradeRecommendation(
            session_id=session_id,
            ticker=ticker,
            strategy=strategy,
            pillar=StrategyPillar.DIRECTIONAL,
            direction=direction,
            legs=[leg],
            contracts=contracts,
            entry_debit_credit=round(premium_per_sh * 100 * contracts, 2),
            max_loss_dollars=max_loss_dollars,
            max_gain_dollars=max_gain_dollars,
            reward_risk_ratio=round(profit_target_pct / _sl_pct, 2),
            breakeven_price=(
                round(strike + premium_per_sh, 2) if opt_type == "call"
                else round(strike - premium_per_sh, 2)
            ),
            stop_loss_pct=self._settings.long_options_stop_loss_pct,
            # Long options exit on the 5-day time stop (entry + max_hold), not a DTE-remaining
            # rule; the session sets target_close_date = entry + max_hold. 21 here is a spread
            # convention carried for schema compatibility and is not the governing exit.
            target_dte_close=21,
            conviction_score=float(min(conviction * 20, 100)),
            size_multiplier=float(contracts),
            reasoning=(
                f"Long {opt_type} | score={conviction} qual={quality:.1f} | "
                f"contracts={contracts} | PT={profit_target_pct:.0%} | "
                f"Δtarget={target_delta:.2f} | "
                f"IVR={ivr_display:.0f} (ptIVR={_pt_ivr_str}) | "
                f"DTE={dte} ({dte_reason}) | "
                f"strike={strike} ({delta_approx:.2f}Δ) | prem=${premium_per_sh:.2f}/sh | "
                f"OI={oi_at_strike} | signals={signals_readable}"
            ),
        )

        logger.info(
            "LongOptions [%s] %s %s%.0f exp=%s DTE=%d Δ=%.2f prem=$%.2f "
            "contracts=%d score=%d ptIVR=%.0f | %s",
            ticker, str(strategy).upper(), opt_type[0].upper(),
            strike, expiry, dte, abs(delta_approx),
            premium_per_sh, contracts, conviction,
            per_ticker_ivr if per_ticker_ivr is not None else 0.0,
            dte_reason,
        )

        return LongDecision(
            ticker=ticker,
            strategy=str(strategy),
            outcome="proceed",
            block_reason="",
            recommendation=rec,
            dte=dte,
            strike=strike,
            delta_approx=delta_approx,
            premium=premium_per_sh,
            ivr=ivr_display,
            per_ticker_ivr=per_ticker_ivr or 0.0,
            vix=vix,
            regime=regime,
            flow_direction=flow_dir,
            momentum_score=momentum.get("rsi_norm", 0.5),
            conviction=conviction,
            signal_quality=quality,
            signal_stack=signal_stack,
            dte_reason=dte_reason,
            contracts=contracts,
            profit_target_pct=profit_target_pct,
        )

    # ── Signal scoring ────────────────────────────────────────────────────────

    @staticmethod
    def _score_direction(
        macro_context:  Any,
        flow_signals:   Any,
        momentum:       dict,
        gex_regime:     str,
        rsi_overbought: int = 72,
        rsi_oversold:   int = 28,
        min_conviction: int = 2,
        news_flag:      str | None = None,
        signal_perf:    dict | None = None,
        score2_floor:   float = 0.0,
        rsi_capitulation_floor: int = 0,   # 0 = disabled (plain oversold/overbought veto)
        counter_trend_flow_damp: bool = False,
    ) -> tuple[str | None, StrategyType | None, dict, int, str, float]:
        """
        Returns (direction, strategy, signal_stack, net_score, flow_dir, quality_score).

        quality_score is a continuous weighted measure of signal reliability — distinct
        from the binary conviction count. Used for sizing: high quality → more contracts.

        signal_perf (optional) maps (signal_name, direction) → (win_rate, n) from the
        signal_stats calibration table. Each signal's quality contribution is scaled by
        its historical win-rate multiplier (_perf_mult), so the deterministic scorer
        self-improves on every close. Conviction (the integer entry gate) is left
        untouched — only sizing quality adapts here; entry-level adaptation flows through
        the gated LLM vetter, which is shown the same win-rates.

        Signal quality weights (based on reliability hierarchy):
          flow sweep:       2.0  — institutional intent, hardest to fake
          flow directional: 1.0  — directional but no sweep premium
          momentum:         0.8  — confirms trend but lags the move
          rel_strength:     1.0  — price leadership is contemporaneous
          vol_surge:        0.7  — noisy but real demand signal
          gex amplifier:    0.5  — structural tailwind, not a standalone signal
          macro:            0.6  — slow-moving but persistent
          news:             0.3  — advisory only; weakest weight
        """
        bull  = 0
        bear  = 0
        # Track quality PER DIRECTION so an opposing-side signal can't inflate the
        # winning side's size (e.g. bullish flow + bearish momentum previously summed
        # into one `qual`, over-sizing a conflicted bullish trade). Sizing uses only
        # the winning side's quality.
        qual_bull = 0.0
        qual_bear = 0.0
        stack: dict[str, str] = {}

        # Per-signal win-rate calibration (deterministic learning loop).
        def _pm(name: str, d: str) -> float:
            return LongOptionsAgent._perf_mult(name, d, signal_perf)

        def _wtag(name: str, d: str) -> str:
            """Annotate the stack with the signal's track record when it's material."""
            if not signal_perf:
                return ""
            rec = signal_perf.get((name, d))
            if not rec or rec[1] < 3:
                return ""
            return f"[{rec[0]*100:.0f}%/{rec[1]}]"

        # Price-action trend, computed up front so flow can be made trend-aware. A "confirmed"
        # trend needs BOTH SMAs and the 10d return to agree — the same bar the RSI carve-out uses.
        _t_sma20 = momentum.get("above_sma20", False)
        _t_sma50 = momentum.get("above_sma50", False)
        _t_ret10 = momentum.get("ret_10d", 0.0)
        _trend_down = (not _t_sma20) and (not _t_sma50) and _t_ret10 < -0.03
        _trend_up   = _t_sma20 and _t_sma50 and _t_ret10 > 0.03

        # ── Flow signals (highest weight) ─────────────────────────────────────
        flow_dir = "neutral"
        if flow_signals:
            flow_dir    = str(getattr(flow_signals, "direction", "neutral") or "neutral")
            sweeps      = getattr(flow_signals, "sweeps", []) or []
            sweep_count = len(sweeps)
            weight = 2 if sweep_count > 0 else 1
            q_flow = 2.0 if sweep_count > 0 else 1.0
            # Counter-trend damp: dip-buying flow that fights a confirmed trend gets 1 less weight
            # (sweep 2→1, non-sweep 1→0) so it can't cancel genuine trend signals. Symmetric.
            _counter = counter_trend_flow_damp and (
                (flow_dir == "bullish" and _trend_down) or (flow_dir == "bearish" and _trend_up))
            if _counter:
                weight = max(0, weight - 1)
                q_flow = max(0.0, q_flow - 1.0)
            _ct_tag = "(counter-trend-damped)" if _counter else ""
            if flow_dir == "bullish":
                bull += weight
                qual_bull += q_flow * _pm("flow", "bullish")
                stack["flow"] = f"bullish+{weight}{'(sweep)' if sweep_count > 0 else ''}{_ct_tag}{_wtag('flow','bullish')}"
            elif flow_dir == "bearish":
                bear += weight
                qual_bear += q_flow * _pm("flow", "bearish")
                stack["flow"] = f"bearish+{weight}{'(sweep)' if sweep_count > 0 else ''}{_ct_tag}{_wtag('flow','bearish')}"
            else:
                stack["flow"] = "neutral"

        # ── Momentum (RSI + SMA) ──────────────────────────────────────────────
        rsi       = momentum.get("rsi", 50.0)
        sma20_ok  = momentum.get("above_sma20", False)
        sma50_ok  = momentum.get("above_sma50", False)
        if rsi > 55 and sma20_ok and sma50_ok:
            bull += 1
            qual_bull += 0.8 * _pm("momentum", "bullish")
            stack["momentum"] = f"bullish(RSI={rsi:.0f}){_wtag('momentum','bullish')}"
        elif rsi < 45 and not sma20_ok and not sma50_ok:
            bear += 1
            qual_bear += 0.8 * _pm("momentum", "bearish")
            stack["momentum"] = f"bearish(RSI={rsi:.0f}){_wtag('momentum','bearish')}"
        else:
            stack["momentum"] = f"neutral(RSI={rsi:.0f})"

        # ── Relative Strength (10-day return alignment) ───────────────────────
        ret_10d = momentum.get("ret_10d", 0.0)
        if ret_10d > 0.03:
            bull += 1
            qual_bull += 1.0 * _pm("rel_strength", "bullish")
            stack["rel_strength"] = f"outperform+1(ret10d={ret_10d:.1%}){_wtag('rel_strength','bullish')}"
        elif ret_10d < -0.03:
            bear += 1
            qual_bear += 1.0 * _pm("rel_strength", "bearish")
            stack["rel_strength"] = f"underperform+1(ret10d={ret_10d:.1%}){_wtag('rel_strength','bearish')}"
        else:
            stack["rel_strength"] = f"neutral(ret10d={ret_10d:.1%})"

        # ── Volume surge (+1 to dominant direction when volume > 2× 20-day avg) ─
        if momentum.get("vol_surge", False):
            if bull > bear:
                bull += 1
                qual_bull += 0.7 * _pm("vol_surge", "bullish")
                stack["vol_surge"] = f"surge+1(bull){_wtag('vol_surge','bullish')}"
            elif bear > bull:
                bear += 1
                qual_bear += 0.7 * _pm("vol_surge", "bearish")
                stack["vol_surge"] = f"surge+1(bear){_wtag('vol_surge','bearish')}"
            else:
                stack["vol_surge"] = "surge(no_dominant)"
        else:
            stack["vol_surge"] = "normal"

        # ── News flag (advisory, weakest weight) ──────────────────────────────
        if news_flag == "bullish":
            bull += 1
            qual_bull += 0.3 * _pm("news", "bullish")
            stack["news"] = f"bullish+1(uw_news){_wtag('news','bullish')}"
        elif news_flag == "bearish":
            bear += 1
            qual_bear += 0.3 * _pm("news", "bearish")
            stack["news"] = f"bearish+1(uw_news){_wtag('news','bearish')}"
        else:
            stack["news"] = "none"

        # ── GEX regime (structural amplifier) ─────────────────────────────────
        if gex_regime == "negative":
            if bull > bear:
                bull += 1
                qual_bull += 0.5 * _pm("gex", "bullish")
                stack["gex"] = f"negative(amplify_bull){_wtag('gex','bullish')}"
            elif bear > bull:
                bear += 1
                qual_bear += 0.5 * _pm("gex", "bearish")
                stack["gex"] = f"negative(amplify_bear){_wtag('gex','bearish')}"
            else:
                stack["gex"] = "negative(no_dominant)"
        else:
            stack["gex"] = gex_regime

        # ── Macro stance (staleness gate: treat as neutral if context > 4h old) ──
        from datetime import datetime as _dt, timezone as _tz
        _macro_ts = getattr(macro_context, "timestamp", None)
        _macro_stale = False
        if _macro_ts:
            try:
                _age_h = (_dt.now(tz=_tz.utc) - _macro_ts).total_seconds() / 3600
                _macro_stale = _age_h > 4.0
            except Exception:
                pass
        stance = "neutral" if _macro_stale else str(getattr(macro_context, "macro_stance", "neutral") or "neutral")
        if _macro_stale:
            stack["macro"] = f"stale(age>{4}h)→neutral"
        elif stance == "risk_on":
            bull += 1
            qual_bull += 0.6 * _pm("macro", "bullish")
            stack["macro"] = f"risk_on+1{_wtag('macro','bullish')}"
        elif stance == "risk_off":
            bear += 1
            qual_bear += 0.6 * _pm("macro", "bearish")
            stack["macro"] = f"risk_off+1{_wtag('macro','bearish')}"
        else:
            stack["macro"] = stance

        # ── RSI extremes filter (trend-aware) ─────────────────────────────────
        # The blanket oversold/overbought veto is a MEAN-REVERSION rule; applied to a
        # trend-following swing book it killed exactly the strongest-trend entries (357
        # trend-confirmed puts vetoed in one bear week). Carve-out: in a CONFIRMED trend
        # (price below/above BOTH SMAs + 10d under/out-performance) the extreme is trend
        # CONFIRMATION, so only veto on true capitulation/blow-off; counter-trend entries
        # keep the normal bounce veto. Symmetric for both sides — not a bear-week patch.
        _confirmed_down = (not sma20_ok) and (not sma50_ok) and ret_10d < -0.03
        _confirmed_up   = sma20_ok and sma50_ok and ret_10d > 0.03
        _cap = rsi_capitulation_floor if rsi_capitulation_floor > 0 else 0
        if bull >= min_conviction and bull > bear:
            _call_ceiling = (100 - _cap) if (_cap and _confirmed_up) else rsi_overbought
            if rsi > _call_ceiling:
                _tag = "[blowoff]" if _call_ceiling != rsi_overbought else ""
                stack["rsi_filter"] = f"BLOCK_CALL(RSI={rsi:.0f}>{_call_ceiling}{_tag})"
                return None, None, stack, bull, flow_dir, qual_bull
            if bull <= 2 and (_dom := LongOptionsAgent._score2_dominant_loser(
                    "bullish", stack, signal_perf, score2_floor)):
                stack["score2_floor"] = f"SKIP(dominant '{_dom}' winrate<{score2_floor:.0%}@n>=8)"
                return None, None, stack, bull, flow_dir, qual_bull
            return "bullish", StrategyType.LONG_CALL, stack, bull, flow_dir, qual_bull
        if bear >= min_conviction and bear > bull:
            _put_floor = _cap if (_cap and _confirmed_down) else rsi_oversold
            if rsi < _put_floor:
                _tag = "[capit]" if _put_floor != rsi_oversold else ""
                stack["rsi_filter"] = f"BLOCK_PUT(RSI={rsi:.0f}<{_put_floor}{_tag})"
                return None, None, stack, bear, flow_dir, qual_bear
            if bear <= 2 and (_dom := LongOptionsAgent._score2_dominant_loser(
                    "bearish", stack, signal_perf, score2_floor)):
                stack["score2_floor"] = f"SKIP(dominant '{_dom}' winrate<{score2_floor:.0%}@n>=8)"
                return None, None, stack, bear, flow_dir, qual_bear
            return "bearish", StrategyType.LONG_PUT, stack, bear, flow_dir, qual_bear
        return None, None, stack, max(bull, bear), flow_dir, max(qual_bull, qual_bear)

    # ── DTE selection — 4-factor professional formula ─────────────────────────

    @staticmethod
    def _select_dte(
        ticker:           str,
        ivr:              float,
        hold_days:        int,
        days_to_catalyst: int | None,
        hv5:              float = 0.0,
        hv20:             float = 0.0,
    ) -> tuple[int, str]:
        """
        Returns (target_dte, reason_str).

        Factor 1 — IVR base (primary driver for option buyers):
            Low IV  → buy time; high IV → shorter DTE to avoid overpaying vega.

        Factor 2 — Beta compression:
            High-beta names make their move faster; don't need as much time.

        Factor 3 — Catalyst ceiling:
            Don't buy through earnings/catalyst; cap DTE to expire before event.

        Factor 4 — Hold floor:
            DTE must be ≥ hold_days × 3 to ensure sufficient time to be right.

        Factor 5 — HV5/HV20 breakout compression:
            When recent vol is accelerating (HV5 > 1.5× HV20), the move is
            happening now — shorter DTE captures the burst and avoids overpaying
            for theta on a move that may revert. Multiply base by 0.75.
        """
        reasons = []

        # Factor 1: IVR base — scaled to the ACTUAL [14,30] DTE window (v2 gamma swing).
        # The old 45-60d bases were vestigial: a 5-day-hold option exits at the time stop
        # with most of its DTE unused, and every base > 30 simply clamped to _DTE_MAX, so the
        # IVR nuance was silently flattened. Keep the read — cheap IV → top of the window (buy
        # the most time we'll actually use); expensive IV → bottom (minimise theta/vega bleed).
        if ivr < 20:
            base = 30
            reasons.append(f"IVR={ivr:.0f}<20→30d(cheap_vega)")
        elif ivr < 35:
            base = 26
            reasons.append(f"IVR={ivr:.0f}<35→26d(normal)")
        elif ivr < 50:
            base = 21
            reasons.append(f"IVR={ivr:.0f}<50→21d(elevated)")
        else:
            base = 16
            reasons.append(f"IVR={ivr:.0f}≥50→16d(expensive)")

        # Factor 2: Beta compression
        beta = _BETA_MAP.get(ticker.upper(), 1.0)
        if beta >= 2.5:
            base = int(base * 0.70)
            reasons.append(f"β={beta:.1f}→×0.70→{base}d")
        elif beta >= 1.8:
            base = int(base * 0.80)
            reasons.append(f"β={beta:.1f}→×0.80→{base}d")
        elif beta >= 1.3:
            base = int(base * 0.90)
            reasons.append(f"β={beta:.1f}→×0.90→{base}d")

        # Factor 3: Catalyst ceiling — expire before catalyst to avoid binary risk
        if days_to_catalyst is not None and 5 <= days_to_catalyst < base:
            orig_base = base
            base = max(hold_days + 3, days_to_catalyst - 2)
            reasons.append(f"catalyst={days_to_catalyst}d→ceil→{base}d")

        # Factor 4: Hold floor — minimum DTE = hold_days × 3 (pro rule: 3× hold)
        hold_floor = hold_days * 3
        if base < hold_floor:
            reasons.append(f"hold_floor={hold_floor}d")
            base = hold_floor

        # Factor 5: HV5/HV20 vol breakout — compress DTE when recent vol is accelerating.
        # HV5 > 1.5× HV20 means the last 5 days moved 50% faster than the 20-day baseline.
        # Shorter DTE maximises gamma sensitivity on the current burst (fewer wasted theta days).
        if hv5 > 0 and hv20 > 0 and hv5 > 1.5 * hv20:
            base = int(base * 0.75)
            reasons.append(f"HV5/HV20={hv5:.0%}/{hv20:.0%}(breakout)→×0.75→{base}d")

        # Hard clamp to window
        dte = max(_DTE_MIN, min(_DTE_MAX, base))
        return dte, " | ".join(reasons)

    # ── Conviction-responsive delta and profit target ─────────────────────────

    @staticmethod
    def _conviction_delta(conviction: int, base_delta: float) -> float:
        """
        Scale target delta by conviction strength.

        Stronger setups → higher delta → more intrinsic value, less lottery.
        Borderline entries → more OTM → lower cost if wrong.

          conviction 2  → base - 0.07  (e.g. 0.28Δ: lower cost, higher reward)
          conviction 3  → base          (e.g. 0.35Δ: standard sweet-spot)
          conviction 4  → base + 0.05  (e.g. 0.40Δ: closer to ATM, more reliable)
          conviction 5+ → base + 0.10  (e.g. 0.45Δ: near ATM on full-stack setups)
        """
        if conviction >= 5:
            return min(0.50, base_delta + 0.10)
        if conviction >= 4:
            return min(0.45, base_delta + 0.05)
        if conviction >= 3:
            return base_delta
        return max(0.22, base_delta - 0.07)

    @staticmethod
    def _conviction_profit_target(conviction: int) -> float:
        """
        Scale profit target by conviction: let stronger setups run further.

          conviction 2  → 40%   conservative: lock in borderline-entry gains quickly
          conviction 3  → 50%   standard swing target (current default)
          conviction 4  → 75%   strong setup: hold through normal retracements
          conviction 5+ → 100%  full-stack: double your money on best-of-year setups
        """
        if conviction >= 5:
            return 1.00
        if conviction >= 4:
            return 0.75
        if conviction >= 3:
            return 0.50
        return 0.40

    # ── Expiry selection ──────────────────────────────────────────────────────

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
            if _DTE_MIN <= dte <= _DTE_MAX:
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
        chain_df:     Any,
        opt_type:     str,
        spot:         float,
        expiry:       date,
        target_delta: float,
    ) -> tuple[float | None, float]:
        try:
            df  = chain_df.copy()
            dte = (expiry - date.today()).days

            if opt_type == "call":
                df = df[df["strike"] > spot * 1.001]
            else:
                df = df[df["strike"] < spot * 0.999]

            if df.empty:
                return None, 0.0

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

            idx    = (df["_delta_abs"] - target_delta).abs().idxmin()
            row    = df.loc[idx]
            strike = float(row["strike"])
            delta  = float(row["_delta_abs"])
            return strike, (delta if opt_type == "call" else -delta)
        except Exception as exc:
            logger.debug("LongOptions strike selection error: %s", exc)
            return None, 0.0

    # ── Price / OI helpers ────────────────────────────────────────────────────

    def _find_liquid_adjacent(
        self,
        chain_df:     Any,
        opt_type:     str,
        spot:         float,
        expiry:       date,
        orig_strike:  float,
        orig_delta:   float,
        target_delta: float,
        min_oi:       int,
        max_delta_tol: float = 0.05,   # ±5Δ tolerance from target
    ) -> tuple[float | None, float, int]:
        """
        Scan up to ±4 strikes from orig_strike for one with OI ≥ min_oi
        and delta within max_delta_tol of target_delta.
        Returns (strike, delta, oi) of the best candidate or (None, 0, 0).
        """
        try:
            df  = chain_df.copy()
            dte = (expiry - date.today()).days

            # Build OTM-only subset with delta approximations
            if opt_type == "call":
                df = df[df["strike"] > spot * 1.001]
            else:
                df = df[df["strike"] < spot * 0.999]

            if "delta" in df.columns and df["delta"].notna().any():
                df = df[df["delta"].notna()].copy()
                df["_delta_abs"] = df["delta"].abs()
            elif "impliedVolatility" in df.columns:
                df = df[df["impliedVolatility"].notna() & (df["impliedVolatility"] > 0)].copy()
                df["_delta_abs"] = df.apply(
                    lambda r: abs(self._approx_delta(
                        spot, float(r["strike"]), float(r["impliedVolatility"]), dte, opt_type
                    )), axis=1,
                )
            else:
                return None, 0.0, 0

            if "openInterest" not in df.columns:
                return None, 0.0, 0

            df["_oi"] = df["openInterest"].fillna(0).astype(int)

            # Filter to liquid strikes within delta tolerance
            delta_lo = target_delta - max_delta_tol
            delta_hi = target_delta + max_delta_tol
            candidates = df[
                (df["_delta_abs"] >= delta_lo) &
                (df["_delta_abs"] <= delta_hi) &
                (df["_oi"] >= min_oi) &
                (df["strike"] != orig_strike)
            ]

            if candidates.empty:
                return None, 0.0, 0

            # Prefer highest OI among candidates
            best = candidates.loc[candidates["_oi"].idxmax()]
            delta_sign = 1.0 if opt_type == "call" else -1.0
            return float(best["strike"]), float(best["_delta_abs"]) * delta_sign, int(best["_oi"])
        except Exception as exc:
            logger.debug("_find_liquid_adjacent error: %s", exc)
            return None, 0.0, 0

    @staticmethod
    def _get_mid(chain_df: Any, strike: float) -> float:
        try:
            rows = chain_df[chain_df["strike"] == strike]
            if rows.empty:
                return 0.0
            r   = rows.iloc[0]
            bid = float(r.get("bid",  0) or 0)
            ask = float(r.get("ask",  0) or 0)
            if bid > 0 and ask > 0:
                return round((bid + ask) / 2, 4)
            # No live two-sided quote -> not executable. Don't price off a stale
            # lastPrice (which let quote-less strikes pass the premium gate).
            return 0.0
        except Exception:
            return 0.0

    @staticmethod
    def _bid_ask_pct(chain_df: Any, strike: float) -> float:
        # Returns the bid-ask spread as a fraction of mid. A missing strike or a
        # one-sided/empty quote returns a huge spread so the liquidity gate REJECTS it
        # (previously returned 0.0, which passed `spread > max` and let quote-less
        # strikes through — a real execution-quality hazard for an options buyer).
        try:
            rows = chain_df[chain_df["strike"] == strike]
            if rows.empty:
                return 999.0
            r   = rows.iloc[0]
            bid = float(r.get("bid", 0) or 0)
            ask = float(r.get("ask", 0) or 0)
            if bid <= 0 or ask <= 0:
                return 999.0
            mid = (bid + ask) / 2
            return (ask - bid) / mid
        except Exception:
            return 999.0

    @staticmethod
    def _get_open_interest(chain_df: Any, strike: float) -> int:
        try:
            rows = chain_df[chain_df["strike"] == strike]
            if rows.empty:
                return 0
            r  = rows.iloc[0]
            oi = r.get("openInterest", 0) or 0
            return int(oi)
        except Exception:
            return 0

    # ── Journal ───────────────────────────────────────────────────────────────

    def journal(self, decision: LongDecision, db_path: str, position_id: str = "") -> None:
        try:
            with sqlite3.connect(db_path, timeout=10) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                rec = decision.recommendation
                conn.execute("""
                    INSERT INTO long_journal (
                        position_id, ticker, strategy, direction, decided_at_utc,
                        strike, expiry, dte, delta_approx, premium_per_sh,
                        ivr, vix, regime, flow_direction,
                        momentum_score, conviction_score, signal_stack,
                        outcome, block_reason, max_loss_dollars, max_gain_dollars, contracts
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    position_id or "",
                    decision.ticker,
                    decision.strategy,
                    rec.direction if rec else "",
                    datetime.now(timezone.utc).isoformat(),
                    decision.strike,
                    str(rec.legs[0].expiration) if rec else "",
                    decision.dte,
                    decision.delta_approx,
                    decision.premium,
                    decision.per_ticker_ivr or decision.ivr,
                    decision.vix,
                    decision.regime,
                    decision.flow_direction,
                    decision.momentum_score,
                    decision.conviction,
                    json.dumps(decision.signal_stack),
                    decision.outcome,
                    decision.block_reason,
                    rec.max_loss_dollars if rec else 0,
                    rec.max_gain_dollars if rec else 0,
                    decision.contracts,
                ))
        except Exception as exc:
            logger.debug("long_journal write error: %s", exc)

    @staticmethod
    def update_signal_stats(
        db_path:      str,
        position_id:  str,
        realized_pnl: float,
    ) -> None:
        """
        Called on position close. Reads the long_journal entry for this position
        to determine which signals fired, then updates win_rate/pnl in signal_stats.
        Skips silently if no journal entry found (e.g., legacy position).
        """
        try:
            with sqlite3.connect(db_path, timeout=10) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                row = conn.execute(
                    "SELECT signal_stack, direction FROM long_journal "
                    "WHERE position_id=? AND outcome='proceed' ORDER BY journal_id DESC LIMIT 1",
                    (position_id,),
                ).fetchone()
                if row is None:
                    return
                try:
                    stack: dict = json.loads(row[0] or "{}")
                except Exception:
                    return
                direction = row[1] or "bullish"
                is_win    = realized_pnl > 0
                now_utc   = datetime.now(timezone.utc).isoformat()

                for signal_name in stack:
                    # Upsert into signal_stats
                    conn.execute("""
                        INSERT INTO signal_stats (signal_name, direction, total_trades, wins, losses,
                            total_pnl, avg_pnl, win_rate, last_updated_utc)
                        VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(signal_name, direction) DO UPDATE SET
                            total_trades     = total_trades + 1,
                            wins             = wins + excluded.wins,
                            losses           = losses + excluded.losses,
                            total_pnl        = total_pnl + excluded.total_pnl,
                            avg_pnl          = (total_pnl + excluded.total_pnl) / (total_trades + 1),
                            win_rate         = CAST(wins + excluded.wins AS REAL) / (total_trades + 1),
                            last_updated_utc = excluded.last_updated_utc
                    """, (
                        signal_name, direction,
                        1 if is_win else 0,
                        0 if is_win else 1,
                        realized_pnl, realized_pnl,
                        1.0 if is_win else 0.0,
                        now_utc,
                    ))
        except Exception as exc:
            logger.debug("update_signal_stats error: %s", exc)

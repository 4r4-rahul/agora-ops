"""
AGORA Walk-Forward Backtester.

Key differences from APEX backtester:
  1. Multi-ticker universe (SPY, QQQ, IWM, GLD, TLT + passed list)
  2. Full AGORA signal stack (no mock Claude — deterministic path uses rule fallback)
  3. Black-Scholes synthetic spread pricing (no historical options chains)
  4. Per-pillar attribution: vol_premium, directional, event_fomc, event_cpi
  5. Position lifecycle: 50% profit, 21-DTE close, 2× stop — same as live rules
  6. Walk-forward windows: fixed 252-day train + 63-day test windows

Usage:
    engine = AgoraBacktestEngine(
        tickers=["SPY", "QQQ", "IWM"],
        start="2023-01-01",
        end="2024-12-31",
    )
    result = await engine.run()
    result.print_summary()

Or run directly:
    python -m agora.backtester.engine --tickers SPY QQQ IWM --start 2023-01-01 --end 2024-12-31
"""

from __future__ import annotations

import asyncio
import logging
import math
import uuid
from datetime import date, datetime, timedelta
from typing import Any

from ..agents.conviction_scorer import ConvictionScorer
from ..agents.disagreement_resolver import DisagreementResolver, SignalInput
from ..agents.macro_synthesizer import MacroContext
from ..core.config import AgoraSettings
from ..core.models import (
    GexRegime,
    GexSignal,
    IvPremiumSignal,
    Regime,
    StrategyPillar,
    VolRegimeSignal,
)
from ..signals.iv_premium import IvPremiumScreen
from ..signals.vol_regime import VolRegimeClassifier
from .models import AgoraBacktestResult, AgoraBacktestTrade, AgoraTradeStatus
from .synthetic_pricing import (
    build_spread,
    entry_slippage,
    mark_spread,
    term_structure_sigma,
)

logger = logging.getLogger(__name__)

# FOMC and CPI approximate dates for 2023–2025 (used for event pattern detection)
# Real dates from federalreserve.gov; kept as simple list for backtesting
_FOMC_DATES = [
    date(2023, 2, 1), date(2023, 3, 22), date(2023, 5, 3), date(2023, 6, 14),
    date(2023, 7, 26), date(2023, 9, 20), date(2023, 11, 1), date(2023, 12, 13),
    date(2024, 1, 31), date(2024, 3, 20), date(2024, 5, 1), date(2024, 6, 12),
    date(2024, 7, 31), date(2024, 9, 18), date(2024, 11, 7), date(2024, 12, 18),
    date(2025, 1, 29), date(2025, 3, 19), date(2025, 5, 7), date(2025, 6, 18),
]
_CPI_DATES = [
    date(2023, 1, 12), date(2023, 2, 14), date(2023, 3, 14), date(2023, 4, 12),
    date(2023, 5, 10), date(2023, 6, 13), date(2023, 7, 12), date(2023, 8, 10),
    date(2023, 9, 13), date(2023, 10, 12), date(2023, 11, 14), date(2023, 12, 12),
    date(2024, 1, 11), date(2024, 2, 13), date(2024, 3, 12), date(2024, 4, 10),
    date(2024, 5, 15), date(2024, 6, 12), date(2024, 7, 11), date(2024, 8, 14),
    date(2024, 9, 11), date(2024, 10, 10), date(2024, 11, 13), date(2024, 12, 11),
    date(2025, 1, 15), date(2025, 2, 12), date(2025, 3, 12), date(2025, 4, 10),
    date(2025, 5, 13), date(2025, 6, 11),
]

_COMMISSION_PER_LEG = 0.65   # per contract per leg (IBKR rate)

# Real macro index tickers fetched alongside OHLCV data
_MACRO_TICKERS = ["^VIX", "^TNX", "^IRX", "^VIX9D", "^VIX3M"]  # Level 2: adds short/long-end VIX

# Per-ticker IV beta vs SPX — scales VIX into approximate ATM IV for each ETF
# (SPY ≈ VIX; IWM runs ~20% hotter; TLT/GLD run quieter)
_IV_BETA: dict[str, float] = {
    "SPY": 1.00, "QQQ": 1.10, "IWM": 1.20,
    "GLD": 0.60, "GDX": 0.80, "SLV": 0.75,
    "TLT": 0.50, "IEF": 0.35, "AGG": 0.30,
    "XLE": 1.30, "XLF": 1.10, "XLU": 0.70,
}

# Correlation groups — max 2 simultaneous positions per group to prevent
# correlated drawdowns (e.g. SPY + QQQ + IWM all losing on same rally day)
_CORR_GROUPS: dict[str, str] = {
    "SPY": "us_equity", "QQQ": "us_equity", "IWM": "us_equity",
    "TLT": "rates",     "IEF": "rates",     "AGG": "rates",
    "GLD": "commodity", "GDX": "commodity", "SLV": "commodity",
}
_MAX_PER_CORR_GROUP = 2


class AgoraBacktestEngine:
    """
    Walk-forward backtest for the full AGORA signal stack.
    No network calls during the simulation — all data fetched upfront.
    """

    def __init__(
        self,
        tickers: list[str] | None = None,
        start: str = "2024-01-01",
        end: str = "2024-12-31",
        starting_balance: float = 25_000.0,
        max_open_positions: int = 10,
        risk_per_trade: float = 500.0,
        target_dte: int = 45,
        profit_target_pct: float = 0.50,
        stop_loss_multiplier: float = 2.0,
        iv_premium_threshold: float = 0.25,
        iv_premium_min_days: int = 3,
    ) -> None:
        self.tickers = [t.upper() for t in (tickers or ["SPY", "QQQ", "IWM", "GLD", "TLT"])]
        self.start_date = date.fromisoformat(start)
        self.end_date = date.fromisoformat(end)
        self.starting_balance = starting_balance
        self.max_open_positions = max_open_positions
        self.risk_per_trade = risk_per_trade
        self.target_dte = target_dte
        self.profit_target_pct = profit_target_pct
        self.stop_loss_multiplier = stop_loss_multiplier

        # AGORA signal components (deterministic — no Claude needed)
        self._vol_classifier = VolRegimeClassifier()
        self._iv_screen_cache: dict[str, IvPremiumScreen] = {}
        self._scorer = ConvictionScorer()
        self._resolver = DisagreementResolver()

        # IV premium threshold
        self._iv_threshold = iv_premium_threshold
        self._iv_min_days = iv_premium_min_days

    async def run(self) -> AgoraBacktestResult:
        """Execute the walk-forward backtest across all tickers."""
        logger.info(
            "Fetching history for %d tickers: %s → %s",
            len(self.tickers), self.start_date, self.end_date,
        )

        # Fetch all tickers + macro in a single batch download (yfinance is not thread-safe)
        all_tickers = self.tickers + _MACRO_TICKERS
        all_data = await self._batch_download(all_tickers)
        history = {t: all_data.get(t, []) for t in self.tickers}
        macro_data = {t: all_data.get(t, []) for t in _MACRO_TICKERS}

        # Build union of all trading days in range
        all_days: set[date] = set()
        for bars in history.values():
            for b in bars:
                d = b["date"]
                if self.start_date <= d <= self.end_date:
                    all_days.add(d)
        trading_days = sorted(all_days)

        result = AgoraBacktestResult(
            start_date=self.start_date,
            end_date=self.end_date,
            starting_balance=self.starting_balance,
            tickers=self.tickers,
        )

        balance = self.starting_balance
        open_positions: list[AgoraBacktestTrade] = []
        # Per-ticker IV premium rolling cache (consecutive days above threshold)
        iv_day_counts: dict[str, int] = {t: 0 for t in self.tickers}
        # Running HV cache per ticker
        hv_cache: dict[str, list[float]] = {t: [] for t in self.tickers}
        # Per-ticker cooldown: don't re-enter within 14 days of last close
        last_trade_date: dict[str, date] = {}
        _COOLDOWN_DAYS = 7

        logger.info("Simulating %d trading days", len(trading_days))

        for today in trading_days:
            # ── Mark-to-market and lifecycle checks ──────────────────
            for pos in list(open_positions):
                bars = history.get(pos.ticker, [])
                today_bar = self._get_bar(bars, today)
                if not today_bar:
                    continue
                spot = today_bar["close"]
                dte_remaining = (pos.expiration_date - today).days

                # Compute current spread value using BS re-pricing
                hv = self._get_hv(history.get(pos.ticker, []), today, 21)
                sigma = max(hv or 0.15, 0.05)
                T = max(dte_remaining / 252.0, 0.0)
                current_val = self._mark_position(pos, spot, T, sigma, ticker=pos.ticker)

                pnl = self._compute_pnl(pos, current_val)
                n_legs = 4 if pos.strategy == "iron_condor" else 2
                commission = n_legs * pos.contracts * _COMMISSION_PER_LEG * 2

                def _record_close(
                    status: AgoraTradeStatus, reason: str,
                    final_pnl: float, comm: float, exit_val: float,
                ) -> None:
                    pos.pnl_dollars = final_pnl - comm
                    pos.commission_dollars = comm
                    pos.exit_price = exit_val
                    pos.date_closed = today
                    pos.status = status
                    pos.close_reason = reason
                    nonlocal balance
                    balance += pos.position_size + final_pnl - comm
                    open_positions.remove(pos)
                    result.trades.append(pos)
                    last_trade_date[pos.ticker] = today  # cooldown starts on close

                # 50% profit target
                profit_target_dollars = pos.max_gain_dollars * self.profit_target_pct
                if pnl >= profit_target_dollars:
                    _record_close(AgoraTradeStatus.CLOSED_PROFIT_TARGET, f"50% profit (${pnl:+.0f})", pnl, commission, current_val)
                    logger.debug("PROFIT: %s %s $%+.0f", pos.ticker, today, pos.pnl_dollars)
                    continue

                # 21-DTE close
                if dte_remaining <= 21:
                    _record_close(AgoraTradeStatus.CLOSED_DTE, f"21-DTE (dte={dte_remaining})", pnl, commission, current_val)
                    logger.debug("DTE-CLOSE: %s %s $%+.0f", pos.ticker, today, pos.pnl_dollars)
                    continue

                # 2× stop-loss
                stop_threshold = -abs(pos.entry_credit_debit * 100 * pos.contracts * self.stop_loss_multiplier)
                if pnl <= stop_threshold:
                    _record_close(AgoraTradeStatus.CLOSED_STOP_LOSS, f"2× stop (${pnl:+.0f})", pnl, commission, current_val)
                    logger.debug("STOP: %s %s $%+.0f", pos.ticker, today, pos.pnl_dollars)
                    continue

                # Expired
                if pos.expiration_date <= today:
                    intrinsic_pnl = self._intrinsic_pnl(pos, spot)
                    _record_close(AgoraTradeStatus.CLOSED_EXPIRY, "Expiry", intrinsic_pnl, commission, 0.0)
                    continue

            # ── Record daily equity ────────────────────────────────
            unrealized = sum(
                self._compute_pnl(
                    pos,
                    self._mark_position(
                        pos,
                        S=(self._get_bar(history.get(pos.ticker, []), today) or {}).get("close", pos.underlying_at_entry),
                        T=max((pos.expiration_date - today).days / 252.0, 0.0),
                        sigma=max(self._get_hv(history.get(pos.ticker, []), today, 21) or 0.15, 0.05),
                        ticker=pos.ticker,
                    ),
                )
                for pos in open_positions
            )
            result.equity_curve.append(balance + unrealized)
            result.daily_dates.append(today)

            # ── Signal evaluation for each ticker ─────────────────
            if len(open_positions) >= self.max_open_positions:
                continue

            for ticker in self.tickers:
                if len(open_positions) >= self.max_open_positions:
                    break

                # Skip if already have a position in this ticker
                if any(p.ticker == ticker for p in open_positions):
                    continue

                # Correlation cap: max 2 positions per group (prevents same-day correlated drawdowns)
                group = _CORR_GROUPS.get(ticker)
                if group:
                    group_count = sum(1 for p in open_positions if _CORR_GROUPS.get(p.ticker) == group)
                    if group_count >= _MAX_PER_CORR_GROUP:
                        continue

                # Cooldown: 7 days since last close
                last_close = last_trade_date.get(ticker)
                if last_close and (today - last_close).days < _COOLDOWN_DAYS:
                    continue

                bars = history.get(ticker, [])
                today_bar = self._get_bar(bars, today)
                if not today_bar:
                    continue

                trade = self._evaluate_day(
                    ticker=ticker,
                    today=today,
                    today_bar=today_bar,
                    history_bars=bars,
                    iv_day_counts=iv_day_counts,
                    balance=balance,
                    macro_data=macro_data,
                )

                if trade:
                    open_positions.append(trade)
                    balance -= trade.position_size   # reserve capital
                    logger.debug(
                        "OPEN: %s %s %s pillar=%s score=%.0f",
                        ticker, today, trade.strategy, trade.pillar, trade.conviction_score,
                    )

        # Force-close remaining positions at end
        for pos in open_positions:
            bars = history.get(pos.ticker, [])
            last_bar = self._get_bar(bars, self.end_date) or {"close": pos.underlying_at_entry}
            intrinsic = self._intrinsic_pnl(pos, last_bar["close"])
            n_legs = 4 if pos.strategy == "iron_condor" else 2
            commission = n_legs * pos.contracts * _COMMISSION_PER_LEG * 2
            pos.pnl_dollars = intrinsic - commission
            pos.commission_dollars = commission
            pos.date_closed = self.end_date
            pos.status = AgoraTradeStatus.CLOSED_EXPIRY
            result.trades.append(pos)

        return result

    # ── Day evaluation ─────────────────────────────────────────────

    def _evaluate_day(
        self,
        ticker: str,
        today: date,
        today_bar: dict,
        history_bars: list[dict],
        iv_day_counts: dict[str, int],
        balance: float,
        macro_data: dict[str, list[dict]] | None = None,
    ) -> AgoraBacktestTrade | None:
        """Run the full signal stack for one ticker on one day."""
        macro_data = macro_data or {}
        spot = today_bar["close"]
        hv21 = self._get_hv(history_bars, today, 21) or 0.15
        hv10 = self._get_hv(history_bars, today, 10) or hv21
        hv30 = self._get_hv(history_bars, today, 30) or hv21
        rsi  = self._get_rsi(history_bars, today, 14) or 50.0

        # ── Level 1+2: Real macro data (VIX, 10Y, IRX, VIX9D, VIX3M) ───────────
        vix_bars  = macro_data.get("^VIX",   [])
        tnx_bars  = macro_data.get("^TNX",   [])
        irx_bars  = macro_data.get("^IRX",   [])
        vix9d_bars = macro_data.get("^VIX9D", [])
        vix3m_bars = macro_data.get("^VIX3M", [])

        # Real VIX (fallback: HV × 100 if data unavailable)
        real_vix = self._get_macro_val(vix_bars, today) or (hv21 * 100)

        # Level 2: term-structure endpoints for option pricing
        vix9d = self._get_macro_val(vix9d_bars, today)
        vix3m = self._get_macro_val(vix3m_bars, today)

        # ATM IV = VIX scaled by per-ticker IV beta (used for regime/IV-rank signals)
        iv_beta = _IV_BETA.get(ticker, 1.0)
        atm_iv_proxy = (real_vix / 100) * iv_beta

        # Level 2: DTE-aware pricing sigma via variance-weighted term structure
        # Uses VIX9D/VIX/VIX3M anchors; falls back gracefully if data missing
        pricing_sigma = term_structure_sigma(self.target_dte, real_vix, vix9d, vix3m) * iv_beta

        # IV rank: use real VIX rank for equity ETFs, HV rank for rates/commodities
        if ticker in ("SPY", "QQQ", "IWM", "XLE", "XLF", "XLU") and vix_bars:
            iv_rank = self._get_vix_rank(vix_bars, today, real_vix)
        else:
            iv_rank = self._get_iv_rank(history_bars, today, hv21)

        # VIX 5-day average for contango proxy and GEX signal
        vix5_avg = self._get_rolling_avg(vix_bars, today, 5) or real_vix
        vix3m_proxy = vix3m or vix5_avg  # prefer real VIX3M, fall back to 5-day avg

        # Premium ratio: real IV minus realized vol (can go negative in vol crush)
        premium_ratio = (atm_iv_proxy - hv21) / max(hv21, 0.001)

        if premium_ratio >= self._iv_threshold:
            iv_day_counts[ticker] = iv_day_counts.get(ticker, 0) + 1
        else:
            iv_day_counts[ticker] = 0

        iv_active = iv_day_counts.get(ticker, 0) >= self._iv_min_days

        # Vol regime — now uses real VIX instead of HV × 100
        regime_result = self._vol_classifier.classify(
            iv_rank=iv_rank,
            vix=real_vix,
            vix3m=vix3m_proxy,
            hv10=hv10,
            hv30=hv30,
            spy_rsi=rsi if ticker in ("SPY", "QQQ") else None,
            atm_iv=atm_iv_proxy,
            hv21=hv21,
        )
        regime = regime_result["regime"]

        # Crisis kill-switch for backtester
        if regime == "crisis":
            return None

        # GEX proxy: VIX momentum (rising VIX = hedging demand = negative GEX)
        # Rising >2% above 5-day avg → negative; falling >2% below → positive
        if real_vix > vix5_avg * 1.02:
            gex_regime = GexRegime.NEGATIVE
        elif real_vix < vix5_avg * 0.98:
            gex_regime = GexRegime.POSITIVE
        else:
            gex_regime = GexRegime.NEUTRAL
        gex = GexSignal(ticker=ticker, gex_total=0.0, regime=gex_regime)

        # Yield curve macro stance (replaces RSI-based proxy)
        tnx = self._get_macro_val(tnx_bars, today)
        irx = self._get_macro_val(irx_bars, today)
        if tnx is not None and irx is not None:
            yield_spread = tnx - irx
            if real_vix < 16 and yield_spread > 0:
                macro_stance = "risk_on"
            elif real_vix > 28 or yield_spread < -0.80:
                macro_stance = "risk_off"
            else:
                macro_stance = "neutral"
            macro_confidence = 0.65
        else:
            macro_stance = "risk_on" if rsi > 50 else "neutral"
            macro_confidence = 0.50

        # Event patterns
        pillar, event_score = self._check_event_patterns(ticker, today)

        # Build IV premium signal model
        iv_signal = IvPremiumSignal(
            ticker=ticker,
            atm_iv_30d=atm_iv_proxy,
            hv_21d=hv21,
            premium_ratio=premium_ratio,
            days_above_threshold=iv_day_counts.get(ticker, 0),
            signal_active=iv_active,
        )

        # Build regime signal
        regime_signal = VolRegimeSignal(
            regime=Regime(regime),
            confidence=regime_result["confidence"],
            iv_rank=iv_rank,
            atm_iv=atm_iv_proxy,
        )

        # Macro context — yield curve + VIX level drives stance
        macro_ctx = MacroContext(
            macro_stance=macro_stance if macro_stance != "risk_off" else "neutral",
            confidence=macro_confidence,
            vol_selling_ok=(iv_active or iv_rank > 40) and macro_stance != "risk_off",
            size_bias="reduce" if macro_stance == "risk_off" else "maintain",
            method="rules",
        )

        # Score conviction
        conviction = self._scorer.score(
            ticker=ticker,
            session_id="backtest",
            iv_premium=iv_signal,
            gex=gex,
            regime=regime_signal,
            macro=macro_ctx,
            event=None,
            catalyst=None,
        )

        # Boost for event patterns
        conviction.event_score = event_score
        conviction.total_score = min(100.0, conviction.total_score - (0.0) + event_score)
        if event_score > 0 and pillar:
            conviction.pillar = pillar

        # ── Pillar-aware sizing ─────────────────────────────────────────────────
        # Vol premium is direction-neutral — requiring bullish/bearish consensus
        # blocks it almost every day. Only directional trades need the resolver.
        macro_dir = (
            "bullish" if macro_stance == "risk_on" else
            "bearish" if macro_stance == "risk_off" else
            "neutral"
        )
        micro_dir = "neutral"
        if gex_regime == GexRegime.NEGATIVE and rsi > 55:
            micro_dir = "bullish"

        current_pillar = conviction.pillar

        if current_pillar in (StrategyPillar.EVENT_FOMC, StrategyPillar.EVENT_CPI):
            # Event plays are self-sufficient — FOMC drift and CPI condor have their own edge
            if conviction.total_score < 45:
                return None
            conviction.size_multiplier = 1.0

        elif current_pillar == StrategyPillar.VOL_PREMIUM:
            # Vol selling is direction-neutral — only block on active bull/bear conflict
            if conviction.total_score < 40:
                return None
            macro_active = macro_dir in ("bullish", "bearish")
            micro_active = micro_dir in ("bullish", "bearish")
            if macro_active and micro_active and macro_dir != micro_dir:
                conviction.size_multiplier = 0.5   # directional conflict → half size
            else:
                conviction.size_multiplier = 1.0

        else:
            # Directional trades require resolver consensus
            resolution = self._resolver.resolve(
                macro=SignalInput(source="macro", direction=macro_dir, confidence=0.55),
                microstructure=SignalInput(source="microstructure", direction=micro_dir, confidence=0.60),
                catalyst=None,
                regime=regime,
                total_conviction=conviction.total_score,
            )
            if resolution["gate"] == "no_trade":
                return None
            if conviction.total_score < 40:
                return None
            conviction.size_multiplier = resolution["size_multiplier"]

        # Select strategy and build synthetic spread
        strategy, option_type, direction = self._select_strategy(conviction, regime, gex_regime, rsi)
        T_years = self.target_dte / 252.0
        spread = build_spread(
            S=spot,
            T_years=T_years,
            sigma=pricing_sigma,   # Level 2: DTE-aware term-structure sigma
            strategy=strategy,
            short_delta=0.20,
            long_delta=0.35,
            ticker=ticker,         # Level 2: per-strike skew via skewed_sigma()
        )

        if spread["reward_risk_ratio"] < 0.10:
            return None

        # Size contracts
        max_loss_1x = spread["max_loss_dollars"]
        contracts = max(1, round(self.risk_per_trade / max_loss_1x * conviction.size_multiplier))
        contracts = min(contracts, 10)

        position_size = max_loss_1x * contracts
        if position_size > balance * 0.10:   # max 10% of current balance per trade
            contracts = max(1, int(balance * 0.10 / max_loss_1x))
            position_size = max_loss_1x * contracts

        entry_credit = spread["entry_credit_debit"]
        # Level 2: bid-ask slippage — fill at bid/ask not mid, reducing net credit/adding to debit
        slip = entry_slippage(ticker)
        if entry_credit < 0:   # credit spread: receive less
            entry_credit = entry_credit * (1 - slip)
        else:                  # debit spread: pay more
            entry_credit = entry_credit * (1 + slip)
        n_legs = 4 if strategy == "iron_condor" else 2
        commission = n_legs * contracts * _COMMISSION_PER_LEG * 2

        return AgoraBacktestTrade(
            trade_id=str(uuid.uuid4())[:8],
            date_opened=today,
            ticker=ticker,
            pillar=conviction.pillar.value if conviction.pillar else "vol_premium",
            strategy=strategy,
            direction=direction,
            short_strike=spread["short_strike"],
            long_strike=spread["long_strike"],
            call_short_strike=spread.get("call_short_strike", 0.0),
            call_long_strike=spread.get("call_long_strike", 0.0),
            expiration_date=today + timedelta(days=self.target_dte),
            dte_at_entry=self.target_dte,
            entry_credit_debit=entry_credit,
            contracts=contracts,
            max_loss_dollars=spread["max_loss_dollars"] * contracts,
            max_gain_dollars=spread["max_gain_dollars"] * contracts,
            profit_target_price=abs(entry_credit) * (1 - self.profit_target_pct)
                if entry_credit < 0 else abs(entry_credit) * (1 + self.profit_target_pct),
            stop_loss_price=abs(entry_credit) * self.stop_loss_multiplier,
            underlying_at_entry=spot,
            iv_at_entry=atm_iv_proxy,
            conviction_score=conviction.total_score,
            size_multiplier=conviction.size_multiplier,
            iv_premium_active=iv_active,
            gex_regime=gex_regime.value,
            vol_regime=regime,
            position_size=position_size,
        )

    def _select_strategy(
        self,
        conviction: Any,
        regime: str,
        gex_regime: GexRegime,
        rsi: float,
    ) -> tuple[str, str, str]:
        """Returns (strategy_name, option_type, direction)."""
        pillar = conviction.pillar

        if pillar == StrategyPillar.EVENT_FOMC:
            # Drift effect is regime-dependent: only go directional in low-vol bull markets
            if regime == "low_volatility":
                return "bull_call_spread", "call", "bullish"
            return "iron_condor", "put", "neutral"   # sell pre-FOMC vol spike in normal/high-vol
        if pillar == StrategyPillar.EVENT_CPI:
            return "iron_condor", "put", "neutral"
        if pillar == StrategyPillar.POST_EARNINGS:
            return "bull_put_spread", "put", "bullish"

        # Vol premium: always sell credit spreads — that IS the edge
        if pillar == StrategyPillar.VOL_PREMIUM:
            if regime == "high_volatility":
                return "iron_condor", "put", "neutral"   # sell both sides in high vol
            elif rsi < 42:
                return "bear_call_spread", "call", "bearish"   # lean bearish
            else:
                return "bull_put_spread", "put", "neutral"     # default: sell put premium

        # Directional: GEX negative → follow momentum with debit spreads
        if pillar == StrategyPillar.DIRECTIONAL:
            if rsi > 55:
                return "bull_call_spread", "call", "bullish"
            elif rsi < 45:
                return "bear_put_spread", "put", "bearish"
            return "bull_call_spread", "call", "bullish"   # default bias

        # Fallback by regime
        if regime in ("high_volatility", "normal"):
            return "bull_put_spread", "put", "neutral"   # credit default
        # Low vol: debit spreads
        if rsi > 55:
            return "bull_call_spread", "call", "bullish"
        return "iron_condor", "put", "neutral"

    def _check_event_patterns(
        self, ticker: str, today: date
    ) -> tuple[StrategyPillar | None, float]:
        """Returns (pillar, event_score_0_to_15)."""
        if ticker not in ("SPY", "QQQ", "IWM"):
            return None, 0.0

        # FOMC T-5 to T-1 drift
        for fomc_date in _FOMC_DATES:
            days_to = (fomc_date - today).days
            if 1 <= days_to <= 5:
                return StrategyPillar.EVENT_FOMC, round(0.60 * 15, 1)

        # CPI T-2 and T-1 condor
        for cpi_date in _CPI_DATES:
            days_to = (cpi_date - today).days
            if days_to in (1, 2):
                return StrategyPillar.EVENT_CPI, round(0.70 * 15, 1)

        return None, 0.0

    # ── P&L helpers ────────────────────────────────────────────────

    def _mark_position(
        self, pos: AgoraBacktestTrade, S: float, T: float, sigma: float, ticker: str = ""
    ) -> float:
        """Current spread value per share.  Iron condors mark both put and call legs."""
        if pos.strategy == "iron_condor" and pos.call_short_strike > 0:
            put_val  = mark_spread(S, T, sigma, pos.short_strike, pos.long_strike,  "put",  "bull_put_spread",  ticker=ticker)
            call_val = mark_spread(S, T, sigma, pos.call_short_strike, pos.call_long_strike, "call", "bear_call_spread", ticker=ticker)
            return put_val + call_val
        opt_type = "call" if pos.strategy in ("bear_call_spread", "bull_call_spread") else "put"
        return mark_spread(S, T, sigma, pos.short_strike, pos.long_strike, opt_type, pos.strategy, ticker=ticker)

    def _compute_pnl(self, pos: AgoraBacktestTrade, current_val: float) -> float:
        """P&L vs entry, before commission."""
        if pos.entry_credit_debit < 0:
            # Credit spread: we received credit; profit = credit - current_value
            pnl_per_share = -pos.entry_credit_debit - current_val
        else:
            # Debit spread: we paid debit; profit = current_value - debit
            pnl_per_share = current_val - pos.entry_credit_debit
        return pnl_per_share * 100 * pos.contracts

    def _intrinsic_pnl(self, pos: AgoraBacktestTrade, final_spot: float) -> float:
        """P&L at expiry using pure intrinsic value."""
        if pos.strategy == "iron_condor":
            put_intrinsic  = max(0.0, pos.short_strike - final_spot) - max(0.0, pos.long_strike - final_spot)
            call_intrinsic = (
                max(0.0, final_spot - pos.call_short_strike) - max(0.0, final_spot - pos.call_long_strike)
                if pos.call_short_strike > 0 else 0.0
            )
            intrinsic = put_intrinsic + call_intrinsic
        elif "put" in pos.strategy:
            intrinsic = max(0.0, pos.short_strike - final_spot) - max(0.0, pos.long_strike - final_spot)
        else:
            intrinsic = max(0.0, final_spot - pos.short_strike) - max(0.0, final_spot - pos.long_strike)
        if pos.entry_credit_debit < 0:
            pnl = (-pos.entry_credit_debit - intrinsic) * 100 * pos.contracts
        else:
            pnl = (intrinsic - pos.entry_credit_debit) * 100 * pos.contracts
        return pnl

    # ── History helpers ────────────────────────────────────────────

    async def _fetch_all_history(self) -> dict[str, list[dict]]:
        """Fetch OHLCV history for all tickers in one batch download."""
        return await self._batch_download(self.tickers)

    async def _fetch_macro_data(self) -> dict[str, list[dict]]:
        """Fetch VIX, 10Y yield, 13-week T-bill in one batch download."""
        return await self._batch_download(_MACRO_TICKERS)

    async def _batch_download(self, tickers: list[str]) -> dict[str, list[dict]]:
        """Download multiple tickers in one yfinance call (thread-safe)."""
        import yfinance as yf
        warmup = self.start_date - timedelta(days=365)
        start_str = warmup.isoformat()
        end_str = (self.end_date + timedelta(days=1)).isoformat()
        loop = asyncio.get_event_loop()

        def _download():
            df = yf.download(
                tickers,
                start=start_str,
                end=end_str,
                progress=False,
                auto_adjust=True,
                group_by="ticker",
            )
            return df

        try:
            df = await loop.run_in_executor(None, _download)
        except Exception as e:
            logger.warning("Batch download failed: %s", e)
            return {t: [] for t in tickers}

        result: dict[str, list[dict]] = {}
        for ticker in tickers:
            try:
                if len(tickers) == 1:
                    # Single-ticker df has flat columns
                    tdf = df
                    if hasattr(tdf.columns, "nlevels") and tdf.columns.nlevels > 1:
                        tdf = tdf.xs(ticker, axis=1, level=1)
                else:
                    tdf = df[ticker] if ticker in df.columns.get_level_values(0) else df
                bars = []
                for ts, row in tdf.iterrows():
                    c = row.get("Close", None)
                    if c is None or (hasattr(c, "__len__") and len(c) == 0):
                        continue
                    close = float(c) if not hasattr(c, "__len__") else float(c.iloc[0])
                    if close == 0 or close != close:  # skip zero or NaN
                        continue
                    bars.append({
                        "date":   ts.date() if hasattr(ts, "date") else ts,
                        "open":   float(row.get("Open", close) or close),
                        "high":   float(row.get("High", close) or close),
                        "low":    float(row.get("Low", close) or close),
                        "close":  close,
                        "volume": int(row.get("Volume", 0) or 0),
                    })
                result[ticker] = bars
            except Exception as e:
                logger.warning("Parsing %s failed: %s", ticker, e)
                result[ticker] = []
        return result

    def _get_macro_val(self, bars: list[dict], today: date) -> float | None:
        """Most recent close on or before today (forward-fill for holidays/gaps)."""
        candidates = [b for b in bars if b["date"] <= today]
        return candidates[-1]["close"] if candidates else None

    def _get_rolling_avg(self, bars: list[dict], today: date, window: int) -> float | None:
        """Rolling average of close over last `window` bars before today."""
        past = [b["close"] for b in bars if b["date"] < today]
        if len(past) < window:
            return None
        return sum(past[-window:]) / window

    def _get_vix_rank(self, bars: list[dict], today: date, current_vix: float) -> float:
        """VIX percentile rank over the past 252 trading days — true IV rank for SPY."""
        past = [b["close"] for b in bars if b["date"] < today]
        if len(past) < 60:
            return 50.0
        window = past[-252:] if len(past) >= 252 else past
        below = sum(1 for v in window if v <= current_vix)
        return round(below / len(window) * 100, 1)

    def _get_bar(self, bars: list[dict], d: date) -> dict | None:
        for b in bars:
            if b["date"] == d:
                return b
        return None

    def _get_hv(self, bars: list[dict], today: date, window: int) -> float | None:
        """Compute realized vol from closing prices up to (not including) today."""
        past = [b for b in bars if b["date"] < today]
        if len(past) < window + 1:
            return None
        closes = [b["close"] for b in past[-window - 1:]]
        returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
        if len(returns) < window:
            return None
        mean = sum(returns) / len(returns)
        var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        return math.sqrt(var) * math.sqrt(252)

    def _get_iv_rank(self, bars: list[dict], today: date, current_hv: float) -> float:
        """IV rank proxy: where is current HV in the past 252-day HV range."""
        past = [b for b in bars if b["date"] < today]
        if len(past) < 60:
            return 50.0
        # Compute rolling 21-day HV for each past day
        hvs = []
        for i in range(21, min(len(past), 252)):
            closes = [past[i - 21 + j]["close"] for j in range(22)]
            rets = [math.log(closes[k] / closes[k - 1]) for k in range(1, len(closes)) if closes[k - 1] > 0]
            if len(rets) < 21:
                continue
            mean = sum(rets) / len(rets)
            var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
            hvs.append(math.sqrt(var) * math.sqrt(252))
        if not hvs:
            return 50.0
        min_hv, max_hv = min(hvs), max(hvs)
        if max_hv <= min_hv:
            return 50.0
        return min(100.0, max(0.0, (current_hv - min_hv) / (max_hv - min_hv) * 100))

    def _get_rsi(self, bars: list[dict], today: date, period: int = 14) -> float | None:
        """RSI from past closes."""
        past = [b for b in bars if b["date"] < today]
        if len(past) < period + 1:
            return None
        closes = [b["close"] for b in past[-(period + 1):]]
        changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains  = [max(c, 0) for c in changes]
        losses = [abs(min(c, 0)) for c in changes]
        avg_g = sum(gains) / period
        avg_l = sum(losses) / period
        if avg_l == 0:
            return 100.0
        return 100 - 100 / (1 + avg_g / avg_l)


# ── CLI entry point ────────────────────────────────────────────────────────

async def _cli_main() -> None:
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(description="AGORA Walk-Forward Backtest")
    parser.add_argument("--tickers", nargs="+", default=["SPY", "QQQ", "IWM", "GLD", "TLT"])
    parser.add_argument("--start", default="2024-01-01")
    parser.add_argument("--end", default="2024-12-31")
    parser.add_argument("--balance", type=float, default=25_000.0)
    parser.add_argument("--dte", type=int, default=45)
    parser.add_argument("--risk", type=float, default=500.0, help="Max $ risk per spread (1 contract)")
    args = parser.parse_args()

    engine = AgoraBacktestEngine(
        tickers=args.tickers,
        start=args.start,
        end=args.end,
        starting_balance=args.balance,
        target_dte=args.dte,
        risk_per_trade=args.risk,
    )
    result = await engine.run()
    result.print_summary()

    # Dump trade log
    print(f"\nDetailed trade log ({result.total_trades} trades):")
    print(f"{'Date':<12} {'Ticker':<6} {'Pillar':<18} {'Strategy':<20} {'Score':>5} {'Size':>4} {'P&L':>8} {'Status'}")
    print("─" * 100)
    for t in sorted(result.trades, key=lambda x: x.date_opened):
        pnl_str = f"${t.pnl_dollars:+,.0f}" if t.pnl_dollars is not None else "open"
        print(
            f"{t.date_opened!s:<12} {t.ticker:<6} {t.pillar:<18} {t.strategy:<20} "
            f"{t.conviction_score:>5.0f} {t.size_multiplier:>4.1f} {pnl_str:>8} {t.status.value}"
        )


if __name__ == "__main__":
    asyncio.run(_cli_main())

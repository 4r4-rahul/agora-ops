"""
Live Scalp Engine — Multi-Strategy 0DTE Execution Bridge
==========================================================
Connects the proven backtested strategies (Scalp, Runner, ORB, RangeFade)
to live IBKR execution.

This replaces the old LottoScanner in run_live.py.

Architecture:
  ┌─────────────────────────────────────────────────────────────┐
  │  IBKRDataProvider.get_historical_bars()  (1m bars, 1 day)   │
  │        ↓                                                     │
  │  precompute_day_indicators()  (O(n) once per refresh)        │
  │        ↓                                                     │
  │  Signal Engines × 4:                                         │
  │    A. SignalEngine         → momentum scalp (morning+PH)     │
  │    C. RunnerExitEngine     → OTM runner (power hour)         │
  │    D. ORBSignalEngine      → opening range breakout          │
  │    E. RangeFadeSignalEngine→ range boundary fades            │
  │        ↓                                                     │
  │  Hybrid Position Sizer (LW 10% + Vol Target 15%)            │
  │        ↓                                                     │
  │  OrderExecutor.buy_option()  (IBKR, human-confirmed)        │
  │        ↓                                                     │
  │  Exit Engines × 4 (per-bar monitoring via check_exit())      │
  │        ↓                                                     │
  │  OrderExecutor.sell_option() (close positions)               │
  └─────────────────────────────────────────────────────────────┘

Strategy priority (same as backtester):
  - Momentum scalp (A) is primary — fires in morning + power hour windows
  - ORB (D) only fires when momentum has NOT fired that day
  - RangeFade (E) only fires on RANGE_BOUND / MIXED regime days
  - Runner (C) piggybacks on momentum signals in power hour

Validated on 169 trades, PF=4.21, PnL=$192,220, MaxDD=23.8%.
"""

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Any

import numpy as np
import pandas as pd

from .config import (
    EngineConfig, ScalpConfig, ORBConfig, RangeFadeConfig,
    PositionSizingConfig, ContractPickerConfig, get_ticker_profile, TickerProfile,
)
from .modules.contract_picker import SmartContractPicker, FeasibilityResult
from .scalper import (
    SignalEngine, ScalpExitEngine, RunnerExitEngine,
    ORBSignalEngine, ORBExitEngine,
    RangeFadeSignalEngine, RangeFadeExitEngine,
    ScalpSignal, ORBSignal, RangeFadeSignal,
    ScalpPosition,
)
from .regime import RegimeDetector, RegimeInfo
from .execution import OrderExecutor, OrderStatus, FillResult

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# Live Position (mirrors SimScalpPosition for live tracking)
# ─────────────────────────────────────────────────────────────────

@dataclass
class LiveScalpPosition:
    """A live option position tracked by the engine."""
    # Identity
    ticker: str = ""
    strike: float = 0.0
    right: str = ""               # "C" or "P"
    direction: str = ""           # "CALL" or "PUT"
    expiry: str = ""              # YYYYMMDD
    tier: str = "scalp"           # "scalp", "runner", "orb", "range_fade"
    confirmations: List[str] = field(default_factory=list)
    confidence: float = 0.0

    # Entry
    entry_time: datetime = field(default_factory=datetime.now)
    entry_underlying: float = 0.0
    entry_premium: float = 0.0
    num_contracts: int = 1
    atr_at_entry: float = 0.0

    # Commission tracking (entry commission deducted from realized P&L at exit)
    entry_commission: float = 0.0

    # Pre-computed exit levels (underlying price)
    stop_price: float = 0.0
    target_price: float = 0.0

    # ORB-specific
    orb_high: float = 0.0
    orb_low: float = 0.0
    orb_range: float = 0.0

    # Range-fade-specific
    range_high: float = 0.0
    range_low: float = 0.0

    # Tracking
    best_favorable_underlying: float = 0.0
    is_open: bool = True

    # Exit
    exit_time: Optional[datetime] = None
    exit_underlying: float = 0.0
    exit_premium: float = 0.0
    exit_reason: str = ""
    total_pnl: float = 0.0

    def to_dict(self) -> dict:
        """Serialize for state persistence."""
        return {
            "ticker": self.ticker,
            "strike": self.strike,
            "right": self.right,
            "direction": self.direction,
            "expiry": self.expiry,
            "tier": self.tier,
            "confirmations": self.confirmations,
            "confidence": self.confidence,
            "entry_time": self.entry_time.isoformat(),
            "entry_underlying": self.entry_underlying,
            "entry_premium": self.entry_premium,
            "num_contracts": self.num_contracts,
            "entry_commission": self.entry_commission,
            "atr_at_entry": self.atr_at_entry,
            "stop_price": self.stop_price,
            "target_price": self.target_price,
            "best_favorable_underlying": self.best_favorable_underlying,
            "is_open": self.is_open,
        }


# ─────────────────────────────────────────────────────────────────
# Live Scalp Engine
# ─────────────────────────────────────────────────────────────────

class LiveScalpEngine:
    """
    Multi-strategy 0DTE live execution engine.

    Replaces LottoScanner with the proven backtested strategy stack.
    Manages 4 independent strategy tiers, each with its own
    signal engine, exit engine, and position slot.

    Lifecycle (called from LiveTradingLoop):
      1. reset_daily()    → Clear state at start of each day
      2. scan(ticker, ms) → Evaluate all strategies, enter if signal fires
      3. check_exits(ttc) → Monitor open positions for exit triggers
      4. print_status()   → Display current state

    Each scan() call:
      a. Fetches fresh 1m bars from IBKR (full today)
      b. Pre-computes indicators once (O(n))
      c. Evaluates signal engines at the latest bar
      d. If signal → select ATM strike → size via hybrid sizer → execute
    """

    def __init__(
        self,
        provider,               # IBKRDataProvider
        executor: OrderExecutor,
        config: EngineConfig,
        account_size: float = 10_000.0,
        state_manager=None,     # StateManager for P&L persistence
    ):
        self.provider = provider
        self.executor = executor
        self.config = config
        self.account_size = account_size
        self.state_manager = state_manager  # Wired to persist P&L

        # Strategy configs
        self.scalp_cfg = config.scalp
        self.orb_cfg = config.orb
        self.rf_cfg = config.range_fade
        self.sizing_cfg = config.sizing

        # Ticker profile (set per-ticker in scan)
        self._profile: Optional[TickerProfile] = None
        self._premium_scale: float = 1.0

        # ── Signal Engines ───────────────────────────────────────
        self._signal_engine = SignalEngine(self.scalp_cfg, bar_minutes=1)
        self._orb_signal_engine = ORBSignalEngine(self.orb_cfg, bar_minutes=1)
        self._rf_signal_engine = RangeFadeSignalEngine(self.rf_cfg, bar_minutes=1)

        # ── Exit Engines ─────────────────────────────────────────
        self._scalp_exit = ScalpExitEngine(self.scalp_cfg)
        self._runner_exit = RunnerExitEngine(self.scalp_cfg)
        self._orb_exit = ORBExitEngine(self.orb_cfg)
        self._rf_exit = RangeFadeExitEngine(self.rf_cfg)

        # ── Regime Detector ──────────────────────────────────────
        self._regime = RegimeDetector()

        # ── Smart Contract Picker (pre-trade feasibility) ────────
        cp_cfg = config.contract_picker
        self._contract_picker = SmartContractPicker(
            min_expected_rr=cp_cfg.min_expected_rr,
            min_net_gain_pct=cp_cfg.min_net_gain_pct,
            max_theta_pct=cp_cfg.max_theta_pct,
            entry_slippage_pct=cp_cfg.entry_slippage_pct,
            exit_slippage_pct=cp_cfg.exit_slippage_pct,
        )
        self._cp_cfg = cp_cfg

        # ── Per-Day State (reset each morning) ───────────────────
        self.open_scalp: Optional[LiveScalpPosition] = None
        self.open_runner: Optional[LiveScalpPosition] = None
        self.open_orb: Optional[LiveScalpPosition] = None
        self.open_rf: Optional[LiveScalpPosition] = None

        self.scalp_trades_today: int = 0
        self.runner_trades_today: int = 0
        self.orb_trades_today: int = 0
        self.rf_trades_today: int = 0

        self.daily_pnl: float = 0.0
        self.stopped_directions: set = set()
        self.cooldown_remaining: int = 0
        self.momentum_fired_today: bool = False
        self.scalp_won_today: bool = False
        self.scalp_win_direction: Optional[str] = None

        # ── ORB / RF pre-computed data (computed once per day) ───
        self._orb_data: Optional[dict] = None
        self._orb_regime_ok: bool = True
        self._rf_data: Optional[dict] = None
        self._rf_regime_ok: bool = False
        self._regime_info: Optional[RegimeInfo] = None
        self._regime_classified: bool = False

        # ── Precomputed indicators (refreshed each scan) ─────────
        self._precomp: Optional[dict] = None
        self._last_bars: Optional[pd.DataFrame] = None

        # ── Hybrid Sizer State ───────────────────────────────────
        self._trade_history: list = []   # Closed trade PnLs for LW lookback
        self._atr_history: list = []     # ATR at entry for vol targeting

        # ── Previous day state (for PREV_HL signals) ────────────
        self.prev_day_high: Optional[float] = None
        self.prev_day_low: Optional[float] = None

    # ═════════════════════════════════════════════════════════════
    # Daily Reset
    # ═════════════════════════════════════════════════════════════

    def reset_daily(self):
        """Reset all per-day state. Call at start of each trading day."""
        # Save prev day H/L before reset
        if self._last_bars is not None and len(self._last_bars) > 0:
            self.prev_day_high = float(self._last_bars["high"].max())
            self.prev_day_low = float(self._last_bars["low"].min())

        self.open_scalp = None
        self.open_runner = None
        self.open_orb = None
        self.open_rf = None

        self.scalp_trades_today = 0
        self.runner_trades_today = 0
        self.orb_trades_today = 0
        self.rf_trades_today = 0

        self.daily_pnl = 0.0
        self.stopped_directions.clear()
        self.cooldown_remaining = 0
        self.momentum_fired_today = False
        self.scalp_won_today = False
        self.scalp_win_direction = None

        self._orb_data = None
        self._orb_regime_ok = True
        self._rf_data = None
        self._rf_regime_ok = False
        self._regime_info = None
        self._regime_classified = False
        self._precomp = None
        self._last_bars = None

        logger.info("Daily state reset")

    # ═════════════════════════════════════════════════════════════
    # Main Scan — Evaluate All Strategies
    # ═════════════════════════════════════════════════════════════

    def scan(
        self,
        ticker: str,
        minutes_since_open: float,
        dry_run: bool = False,
    ) -> Optional[LiveScalpPosition]:
        """
        Scan all strategy tiers for entry signals on one ticker.

        Called every ~20-30 seconds from the live loop.
        Fetches fresh bars, pre-computes indicators, evaluates
        signal engines, and enters if conditions are met.

        Returns the position entered (if any), or None.
        """
        # ── Pre-checks ──────────────────────────────────────────
        if self.daily_pnl <= -self.scalp_cfg.daily_loss_limit:
            return None

        if self.cooldown_remaining > 0:
            self.cooldown_remaining -= 1
            return None

        # Need at least 30 min to close for new entries
        if minutes_since_open > 360:
            return None

        # ── Set ticker profile ───────────────────────────────────
        self._profile = get_ticker_profile(ticker)
        self._premium_scale = self._profile.premium_scale

        # ── Fetch fresh 1m bars ──────────────────────────────────
        try:
            bars = self.provider.get_historical_bars(
                ticker, days=1, interval="1m"
            )
            if bars is None or len(bars) < 30:
                return None
        except Exception as e:
            logger.warning(f"Failed to get bars for {ticker}: {e}")
            return None

        self._last_bars = bars
        current_price = float(bars["close"].iloc[-1])
        bar_idx = len(bars) - 1

        # ── Pre-compute indicators (O(n) once) ──────────────────
        self._precomp = self._signal_engine.precompute_day_indicators(bars)
        self._precomp['prev_day_high'] = self.prev_day_high
        self._precomp['prev_day_low'] = self.prev_day_low

        # ── Compute rolling ATR ──────────────────────────────────
        atr_arr = self._precomp['atr']
        current_atr = float(atr_arr[bar_idx]) if not np.isnan(atr_arr[bar_idx]) else 0.30
        valid_atrs = atr_arr[~np.isnan(atr_arr)]
        daily_median_atr = float(np.median(valid_atrs)) if len(valid_atrs) > 10 else 0.30

        # ── Classify regime (once per day, after ORB forms) ──────
        if not self._regime_classified and bar_idx >= 60:
            self._classify_regime(bars)

        # ── Compute ORB data (once per day, after bar 30) ────────
        if self._orb_data is None and self.orb_cfg.enabled and bar_idx >= 30:
            self._orb_data = self._orb_signal_engine.compute_orb(bars)
            # Apply regime filter
            if self._orb_data.get("valid", False) and self._regime_classified:
                ri = self._regime_info
                skip = (
                    (self.orb_cfg.skip_dead_flat and ri.regime == "DEAD_FLAT")
                    or (self.orb_cfg.skip_choppy and ri.regime == "CHOPPY")
                    or (self.orb_cfg.skip_range_bound and ri.regime == "RANGE_BOUND")
                    or (self.orb_cfg.skip_mixed and ri.regime == "MIXED")
                )
                self._orb_regime_ok = not skip
                if skip:
                    logger.info(f"ORB skipped: {ri.regime} day")

        # ── Compute RF data (once per day, after bar 60) ─────────
        if self._rf_data is None and self.rf_cfg.enabled and bar_idx >= 60:
            self._rf_data = self._rf_signal_engine.compute_range(bars)
            if self._rf_data and self._rf_data.get("valid", False) and self._regime_classified:
                ri = self._regime_info
                self._rf_regime_ok = (
                    ri.regime == "RANGE_BOUND"
                    or (self.rf_cfg.also_trade_mixed and ri.regime == "MIXED")
                )
                if self._rf_regime_ok:
                    logger.info(f"Range-fade enabled: {ri.regime} day")

        # ── Strategy A: Momentum Scalp ───────────────────────────
        entered = self._scan_momentum_scalp(
            ticker, bars, bar_idx, current_price, current_atr,
            daily_median_atr, minutes_since_open, dry_run,
        )
        if entered:
            return entered

        # ── Strategy C: Runner (power hour, piggyback on momentum)
        entered = self._scan_runner(
            ticker, bars, bar_idx, current_price, current_atr,
            daily_median_atr, minutes_since_open, dry_run,
        )
        if entered:
            return entered

        # ── Strategy D: ORB Breakout ─────────────────────────────
        entered = self._scan_orb(
            ticker, bars, bar_idx, current_price, current_atr,
            daily_median_atr, minutes_since_open, dry_run,
        )
        if entered:
            return entered

        # ── Strategy E: Range Fade ───────────────────────────────
        entered = self._scan_range_fade(
            ticker, bars, bar_idx, current_price, current_atr,
            daily_median_atr, minutes_since_open, dry_run,
        )
        if entered:
            return entered

        return None

    # ═════════════════════════════════════════════════════════════
    # Strategy A — Momentum Scalp
    # ═════════════════════════════════════════════════════════════

    def _scan_momentum_scalp(
        self, ticker, bars, bar_idx, price, atr, median_atr,
        minutes_since_open, dry_run,
    ) -> Optional[LiveScalpPosition]:
        """Evaluate momentum scalp signal at the current bar."""
        if self.open_scalp is not None:
            return None
        if self.scalp_trades_today >= self.scalp_cfg.max_trades_per_day:
            return None

        # Time window check
        cfg = self.scalp_cfg
        in_w1 = cfg.window_1_start <= minutes_since_open <= cfg.window_1_end
        in_w2 = cfg.window_2_start <= minutes_since_open <= cfg.window_2_end
        if not (in_w1 or in_w2):
            return None

        # Evaluate signal using precomputed indicators
        signal = self._signal_engine.evaluate_fast(
            self._precomp, bar_idx, price, ticker,
            bars_index=bars.index,
        )

        if signal is None:
            return None

        self.momentum_fired_today = True

        if signal.direction in self.stopped_directions:
            return None

        # ── IV Discount Gate ─────────────────────────────────────
        if cfg.iv_discount_enabled:
            rv = self._precomp.get('rv')
            if rv is not None and not np.isnan(rv[bar_idx]):
                day_iv = self._estimate_iv(bars)
                if day_iv > 0:
                    rv_iv_ratio = rv[bar_idx] / day_iv
                    threshold = (
                        cfg.rv_iv_min_ratio_w1 if in_w1
                        else cfg.rv_iv_min_ratio
                    )
                    if rv_iv_ratio < threshold:
                        return None

        # ── Enter the trade ──────────────────────────────────────
        stop_dist = atr * cfg.stop_atr_mult
        target_dist = atr * cfg.profit_target_atr_mult

        if signal.direction == "CALL":
            stop_price = price - stop_dist
            target_price = price + target_dist
        else:
            stop_price = price + stop_dist
            target_price = price - target_dist

        # Conviction multiplier (deep IV discount → 1.5× size)
        conviction = 1.0
        rv = self._precomp.get('rv')
        if rv is not None and not np.isnan(rv[bar_idx]):
            day_iv = self._estimate_iv(bars)
            if day_iv > 0 and rv[bar_idx] / day_iv >= cfg.rv_iv_premium_ratio:
                conviction = 1.5

        pos = self._execute_entry(
            ticker=ticker,
            signal_direction=signal.direction,
            confirmations=signal.confirmations,
            confidence=signal.confidence,
            price=price,
            atr=atr,
            median_atr=median_atr,
            stop_price=stop_price,
            target_price=target_price,
            tier="scalp",
            dry_run=dry_run,
            conviction_mult=conviction,
        )

        if pos:
            self.open_scalp = pos
            self.scalp_trades_today += 1
            logger.info(
                f"⚡ SCALP ENTER {pos.direction} {ticker} {pos.strike}{pos.right} "
                f"({', '.join(pos.confirmations)}) "
                f"stop=${stop_price:.2f} target=${target_price:.2f}"
            )
        return pos

    # ═════════════════════════════════════════════════════════════
    # Strategy C — Runner (Power Hour OTM)
    # ═════════════════════════════════════════════════════════════

    def _scan_runner(
        self, ticker, bars, bar_idx, price, atr, median_atr,
        minutes_since_open, dry_run,
    ) -> Optional[LiveScalpPosition]:
        """Evaluate runner entry — requires momentum signal in power hour."""
        cfg = self.scalp_cfg
        if not cfg.runner_enabled:
            return None
        if self.open_runner is not None:
            return None
        if self.runner_trades_today >= cfg.runner_max_per_day:
            return None

        # Runner window only
        if not (cfg.runner_window_start <= minutes_since_open <= cfg.runner_window_end):
            return None

        # Need a fresh momentum signal
        signal = self._signal_engine.evaluate_fast(
            self._precomp, bar_idx, price, ticker,
            bars_index=bars.index,
        )
        if signal is None:
            return None
        if len(signal.confirmations) < cfg.runner_min_confirmations:
            return None

        # ATR gate: must be elevated (momentum day)
        atr_ratio = atr / median_atr if median_atr > 0 else 0
        if atr_ratio < cfg.runner_min_atr_mult:
            # Piggyback: a scalp just won in same direction
            if not (cfg.runner_require_winning_scalp
                    and self.scalp_won_today
                    and self.scalp_win_direction == signal.direction):
                return None

        # Runner: OTM strike, wider stop
        stop_dist = atr * cfg.runner_stop_atr_mult
        if signal.direction == "CALL":
            stop_price = price - stop_dist
            target_price = 0.0  # No fixed target — let it run
        else:
            stop_price = price + stop_dist
            target_price = 0.0

        pos = self._execute_entry(
            ticker=ticker,
            signal_direction=signal.direction,
            confirmations=signal.confirmations,
            confidence=signal.confidence,
            price=price,
            atr=atr,
            median_atr=median_atr,
            stop_price=stop_price,
            target_price=target_price,
            tier="runner",
            dry_run=dry_run,
        )

        if pos:
            self.open_runner = pos
            self.runner_trades_today += 1
            logger.info(
                f"🏃 RUNNER ENTER {pos.direction} {ticker} {pos.strike}{pos.right} "
                f"({', '.join(pos.confirmations)}) stop=${stop_price:.2f}"
            )
        return pos

    # ═════════════════════════════════════════════════════════════
    # Strategy D — ORB Breakout
    # ═════════════════════════════════════════════════════════════

    def _scan_orb(
        self, ticker, bars, bar_idx, price, atr, median_atr,
        minutes_since_open, dry_run,
    ) -> Optional[LiveScalpPosition]:
        """Evaluate ORB breakout signal."""
        if not self.orb_cfg.enabled:
            return None
        if self.open_orb is not None:
            return None
        if self.orb_trades_today >= self.orb_cfg.max_trades_per_day:
            return None
        if not self._orb_regime_ok:
            return None
        if self._orb_data is None or not self._orb_data.get("valid", False):
            return None

        # ORB defers to momentum
        if self.orb_cfg.only_when_no_momentum and self.momentum_fired_today:
            return None

        orb_signal = self._orb_signal_engine.evaluate(bars, self._orb_data, bar_idx)
        if orb_signal is None:
            return None

        # ORB exits are range-multiple based
        orb_range = self._orb_data["orb_range"]
        orb_high = self._orb_data["orb_high"]
        orb_low = self._orb_data["orb_low"]

        if orb_signal.direction == "CALL":
            stop_price = orb_high - orb_range * self.orb_cfg.stop_range_mult
            target_price = orb_high + orb_range * self.orb_cfg.target_range_mult
        else:
            stop_price = orb_low + orb_range * self.orb_cfg.stop_range_mult
            target_price = orb_low - orb_range * self.orb_cfg.target_range_mult

        pos = self._execute_entry(
            ticker=ticker,
            signal_direction=orb_signal.direction,
            confirmations=orb_signal.confirmations,
            confidence=orb_signal.confidence,
            price=price,
            atr=atr,
            median_atr=median_atr,
            stop_price=stop_price,
            target_price=target_price,
            tier="orb",
            dry_run=dry_run,
        )

        if pos:
            pos.orb_high = orb_high
            pos.orb_low = orb_low
            pos.orb_range = orb_range
            self.open_orb = pos
            self.orb_trades_today += 1
            logger.info(
                f"📊 ORB ENTER {pos.direction} {ticker} {pos.strike}{pos.right} "
                f"({', '.join(pos.confirmations)}) "
                f"ORB=[{orb_low:.2f}-{orb_high:.2f}] "
                f"stop=${stop_price:.2f} target=${target_price:.2f}"
            )
        return pos

    # ═════════════════════════════════════════════════════════════
    # Strategy E — Range Fade
    # ═════════════════════════════════════════════════════════════

    def _scan_range_fade(
        self, ticker, bars, bar_idx, price, atr, median_atr,
        minutes_since_open, dry_run,
    ) -> Optional[LiveScalpPosition]:
        """Evaluate range-fade signal at boundary zones."""
        if not self.rf_cfg.enabled:
            return None
        if self.open_rf is not None:
            return None
        if self.rf_trades_today >= self.rf_cfg.max_trades_per_day:
            return None
        if not self._rf_regime_ok:
            return None
        if self._rf_data is None or not self._rf_data.get("valid", False):
            return None

        # Range-fade defers to momentum
        if self.rf_cfg.only_when_no_momentum and self.momentum_fired_today:
            return None

        rf_signal = self._rf_signal_engine.evaluate(bars, self._rf_data, bar_idx)
        if rf_signal is None:
            return None

        # RF exits are range-based
        range_high = self._rf_data["range_high"]
        range_low = self._rf_data["range_low"]
        range_size = self._rf_data["range_size"]
        range_mid = self._rf_data["range_mid"]

        if rf_signal.direction == "CALL":
            # Buying at range low, target is midpoint
            stop_price = range_low - range_size * self.rf_cfg.stop_range_pct
            target_price = range_low + range_size * self.rf_cfg.target_range_pct
        else:
            # Buying at range high, target is midpoint
            stop_price = range_high + range_size * self.rf_cfg.stop_range_pct
            target_price = range_high - range_size * self.rf_cfg.target_range_pct

        pos = self._execute_entry(
            ticker=ticker,
            signal_direction=rf_signal.direction,
            confirmations=rf_signal.confirmations,
            confidence=rf_signal.confidence,
            price=price,
            atr=atr,
            median_atr=median_atr,
            stop_price=stop_price,
            target_price=target_price,
            tier="range_fade",
            dry_run=dry_run,
        )

        if pos:
            pos.range_high = range_high
            pos.range_low = range_low
            self.open_rf = pos
            self.rf_trades_today += 1
            logger.info(
                f"🔃 RF ENTER {pos.direction} {ticker} {pos.strike}{pos.right} "
                f"({', '.join(pos.confirmations)}) "
                f"Range=[{range_low:.2f}-{range_high:.2f}] "
                f"stop=${stop_price:.2f} target=${target_price:.2f}"
            )
        return pos

    # ═════════════════════════════════════════════════════════════
    # Exit Monitoring
    # ═════════════════════════════════════════════════════════════

    def check_exits(self, minutes_to_close: float) -> List[LiveScalpPosition]:
        """
        Check all open positions for exit conditions.

        Called every ~30 seconds from the live loop.
        Fetches current underlying price and checks each exit engine.

        Returns list of positions that were closed.
        """
        closed = []

        # Check each tier's open position
        for tier, pos_attr, exit_engine in [
            ("scalp", "open_scalp", self._scalp_exit),
            ("runner", "open_runner", self._runner_exit),
            ("orb", "open_orb", self._orb_exit),
            ("range_fade", "open_rf", self._rf_exit),
        ]:
            pos = getattr(self, pos_attr)
            if pos is None or not pos.is_open:
                continue

            result = self._check_single_exit(
                pos, exit_engine, minutes_to_close,
            )
            if result:
                closed.append(pos)
                setattr(self, pos_attr, None)

        return closed

    def _check_single_exit(
        self,
        pos: LiveScalpPosition,
        exit_engine,
        minutes_to_close: float,
    ) -> bool:
        """
        Check one position for exit. Returns True if closed.

        Fetches current price, runs exit engine, executes sell if triggered.
        """
        try:
            bars = self.provider.get_historical_bars(
                pos.ticker, days=1, interval="1m"
            )
            if bars is None or len(bars) < 1:
                return False

            last_bar = bars.iloc[-1]
            bar_high = float(last_bar["high"])
            bar_low = float(last_bar["low"])
            bar_close = float(last_bar["close"])
            current_time = bars.index[-1]
        except Exception as e:
            logger.warning(f"Failed to get bars for exit check ({pos.ticker}): {e}")
            return False

        result = exit_engine.check_exit(
            pos, bar_high, bar_low, bar_close,
            current_time, minutes_to_close,
        )

        if not result:
            return False

        exit_reason, exit_underlying = result

        # ── Execute the close ────────────────────────────────────
        exit_premium = 0.0
        try:
            chain = self.provider.get_options_chain(
                pos.ticker, expiry=pos.expiry, strikes_around_atm=10,
            )
            opt = next(
                (o for o in chain
                 if abs(o["strike"] - pos.strike) < 0.01 and o["right"] == pos.right),
                None,
            )
            if opt:
                exit_premium = opt.get("bid", 0)
        except Exception:
            pass

        # Sell the option
        fill = self.executor.sell_option(
            ticker=pos.ticker,
            expiry=pos.expiry,
            strike=pos.strike,
            right=pos.right,
            num_contracts=pos.num_contracts,
            limit_price=exit_premium if exit_premium > 0 else None,
        )

        realized_pnl = 0.0
        if fill.status == OrderStatus.FILLED:
            actual_exit = abs(fill.avg_fill_price)
            realized_pnl = (actual_exit - pos.entry_premium) * pos.num_contracts * 100
            realized_pnl -= fill.commission            # Exit commission
            realized_pnl -= pos.entry_commission        # Entry commission (P0-5)
        elif exit_premium > 0:
            # Estimate even if not filled
            realized_pnl = (exit_premium - pos.entry_premium) * pos.num_contracts * 100

        # Update position
        pos.is_open = False
        pos.exit_time = current_time if isinstance(current_time, datetime) else datetime.now()
        pos.exit_underlying = exit_underlying
        pos.exit_premium = exit_premium
        pos.exit_reason = exit_reason
        pos.total_pnl = round(realized_pnl, 2)

        # Update engine state
        self.daily_pnl += realized_pnl
        self._record_trade_pnl(realized_pnl, pos.atr_at_entry)

        # Persist to StateManager (daily/weekly/monthly P&L limits)
        if self.state_manager:
            self.state_manager.record_fill(fill, realized_pnl)
            logger.info(
                f"State updated: daily=${self.state_manager.state.daily_pnl:+.2f} "
                f"weekly=${self.state_manager.state.weekly_pnl:+.2f}"
            )

        if exit_reason == "STOP_LOSS" and pos.tier == "scalp":
            self.stopped_directions.add(pos.direction)

        if (exit_reason == "PROFIT_TARGET" and pos.tier == "scalp"
                and realized_pnl > 5):
            self.scalp_won_today = True
            self.scalp_win_direction = pos.direction

        if pos.tier == "scalp":
            self.cooldown_remaining = self.scalp_cfg.cooldown_bars

        mult = exit_premium / pos.entry_premium if pos.entry_premium > 0 else 0
        tier_icon = {"scalp": "⚡", "runner": "🏃", "orb": "📊", "range_fade": "🔃"}.get(pos.tier, "●")
        print(
            f"  {tier_icon} {pos.tier.upper()} EXIT: {pos.ticker} {pos.strike}{pos.right} "
            f"→ {exit_reason} | {mult:.2f}x | P&L: ${realized_pnl:+.2f}"
        )
        logger.info(
            f"{pos.tier.upper()} EXIT {pos.direction} {pos.ticker} {pos.strike}{pos.right} "
            f"→ {exit_reason} P&L=${realized_pnl:+.2f}"
        )

        return True

    # ═════════════════════════════════════════════════════════════
    # Trade Execution (shared by all strategies)
    # ═════════════════════════════════════════════════════════════

    def _execute_entry(
        self,
        ticker: str,
        signal_direction: str,
        confirmations: list,
        confidence: float,
        price: float,
        atr: float,
        median_atr: float,
        stop_price: float,
        target_price: float,
        tier: str,
        dry_run: bool = False,
        conviction_mult: float = 1.0,
    ) -> Optional[LiveScalpPosition]:
        """
        Execute entry: select strike → size → place order.

        Shared by all strategy tiers. Returns LiveScalpPosition if filled.
        """
        profile = self._profile
        premium_scale = self._premium_scale
        expiry = date.today().strftime("%Y%m%d")

        # ── Select strike ────────────────────────────────────────
        if tier == "runner":
            # OTM strike for runner
            otm_distance = price * self.scalp_cfg.runner_otm_pct
            delta_min = self.scalp_cfg.runner_target_delta_min
            delta_max = self.scalp_cfg.runner_target_delta_max
            min_prem = self.scalp_cfg.runner_min_premium * premium_scale
            max_prem = self.scalp_cfg.runner_max_premium * premium_scale
        else:
            # ATM strike for scalp, ORB, RF
            otm_distance = price * self.scalp_cfg.max_otm_pct
            delta_min = self.scalp_cfg.target_delta_min
            delta_max = self.scalp_cfg.target_delta_max
            # Premium limits depend on tier
            if tier == "orb":
                min_prem = self.orb_cfg.min_premium * premium_scale
                max_prem = self.orb_cfg.max_premium * premium_scale
            elif tier == "range_fade":
                min_prem = self.rf_cfg.min_premium * premium_scale
                max_prem = self.rf_cfg.max_premium * premium_scale
            else:
                min_prem = self.scalp_cfg.min_premium * premium_scale
                max_prem = self.scalp_cfg.max_premium * premium_scale

        right = "C" if signal_direction == "CALL" else "P"

        # ── Get live chain and select best strike ────────────────
        strike_info = self._select_strike(
            ticker, expiry, right, delta_min, delta_max,
            min_prem, max_prem, premium_scale,
        )
        if not strike_info:
            logger.info(f"No suitable strike for {tier} {signal_direction} on {ticker}")
            return None

        strike = strike_info["strike"]
        ask = strike_info["ask"]
        delta = strike_info["delta"]

        # ── Pre-trade feasibility check ──────────────────────────
        # Forward-price the option at target to verify it can deliver
        tier_enabled_map = {
            "scalp": self._cp_cfg.scalp_enabled,
            "runner": self._cp_cfg.runner_enabled,
            "orb": self._cp_cfg.orb_enabled,
            "range_fade": self._cp_cfg.range_fade_enabled,
        }
        if self._cp_cfg.enabled and tier_enabled_map.get(tier, True):
            # Compute time to expiry (0DTE → close at 4:00 PM ET)
            now = datetime.now()
            minutes_to_close = max(1, (16 * 60) - (now.hour * 60 + now.minute))
            T = minutes_to_close / (252 * 390)

            # Estimate IV from recent bars
            iv = strike_info.get("iv", 0) or self._estimate_iv(self._last_bars)

            # Expected hold time depends on tier
            minutes_since_open = max(0, (now.hour * 60 + now.minute) - (9 * 60 + 30))
            expected_hold = SmartContractPicker.expected_hold_for_tier(tier, minutes_since_open)

            # Use tier-specific thresholds for runners
            picker = self._contract_picker
            if tier == "runner":
                picker = SmartContractPicker(
                    min_expected_rr=self._cp_cfg.min_expected_rr_runner,
                    min_net_gain_pct=self._cp_cfg.min_net_gain_pct_runner,
                    max_theta_pct=self._cp_cfg.max_theta_pct_runner,
                    entry_slippage_pct=self._cp_cfg.entry_slippage_pct,
                    exit_slippage_pct=self._cp_cfg.exit_slippage_pct,
                )
                # Runners are late-day momentum plays — no additional theta tightening
            else:
                # Late-day theta tightening for non-runner tiers
                if minutes_since_open >= self._cp_cfg.late_cutoff_minutes:
                    picker.max_theta_pct = self._cp_cfg.max_theta_pct_late

            feasibility = picker.check_feasibility(
                underlying_price=price,
                strike=strike,
                right=right,
                iv=iv,
                time_to_expiry=T,
                target_price=target_price,
                stop_price=stop_price,
                expected_hold_minutes=expected_hold,
                entry_premium=ask,
            )

            if not feasibility.feasible:
                logger.info(
                    f"🚫 CONTRACT REJECTED [{tier}] {strike}{right} @ ${ask:.2f}: "
                    f"{feasibility.reject_reason}"
                )
                print(
                    f"  🚫 {tier.upper()} CONTRACT REJECTED: {strike}{right} @ ${ask:.2f}\n"
                    f"     {feasibility.reject_reason}"
                )
                return None

            # Log feasibility details
            logger.info(
                f"✅ CONTRACT OK [{tier}] {strike}{right}: "
                f"R:R={feasibility.expected_rr:.2f}, "
                f"gain=${feasibility.expected_gain:.3f} ({feasibility.net_gain_after_costs/ask*100:.0f}%), "
                f"theta=${feasibility.theta_cost:.3f} ({feasibility.theta_cost/ask*100:.0f}%), "
                f"Δ={feasibility.delta_at_entry:.3f}, γ/prem={feasibility.gamma_premium_ratio:.4f}"
            )

        # ── Position sizing via hybrid sizer ─────────────────────
        num_contracts = self._compute_position_size(
            self.account_size + self.daily_pnl,
            ask,
            atr,
            median_atr,
            tier,
            conviction_mult=conviction_mult,
        )

        if num_contracts <= 0:
            return None

        total_cost = ask * num_contracts * 100

        # ── Display ──────────────────────────────────────────────
        tier_icon = {"scalp": "⚡", "runner": "🏃", "orb": "📊", "range_fade": "🔃"}.get(tier, "●")
        print(f"\n  {tier_icon} {tier.upper()} ENTRY:")
        print(f"    Signal:     {' + '.join(confirmations)} ({confidence:.0%})")
        print(f"    Direction:  BUY {signal_direction}")
        print(f"    Strike:     {ticker} {strike}{right} (Δ={delta:.3f})")
        print(f"    ATR:        ${atr:.3f}")
        print(f"    Stop:       ${stop_price:.2f}")
        print(f"    Target:     ${target_price:.2f}")
        print(f"    Ask:        ${ask:.2f}/contract × {num_contracts}")
        print(f"    Total:      ${total_cost:.2f}")

        if dry_run:
            print(f"  🧪 DRY RUN — no order placed")
            # Still create a position for tracking
            pos = LiveScalpPosition(
                ticker=ticker, strike=strike, right=right,
                direction=signal_direction, expiry=expiry, tier=tier,
                confirmations=confirmations, confidence=confidence,
                entry_time=datetime.now(), entry_underlying=price,
                entry_premium=ask, num_contracts=num_contracts,
                atr_at_entry=atr,
                stop_price=stop_price, target_price=target_price,
                best_favorable_underlying=price,
            )
            return pos

        # ── Place order ──────────────────────────────────────────
        fill = self.executor.buy_option(
            ticker=ticker,
            expiry=expiry,
            strike=strike,
            right=right,
            num_contracts=num_contracts,
            limit_price=ask,
        )

        if fill.status == OrderStatus.FILLED:
            entry_price = abs(fill.avg_fill_price)
            pos = LiveScalpPosition(
                ticker=ticker, strike=strike, right=right,
                direction=signal_direction, expiry=expiry, tier=tier,
                confirmations=confirmations, confidence=confidence,
                entry_time=datetime.now(), entry_underlying=price,
                entry_premium=entry_price, num_contracts=fill.num_filled,
                entry_commission=fill.commission,
                atr_at_entry=atr,
                stop_price=stop_price, target_price=target_price,
                best_favorable_underlying=price,
            )
            print(f"  ✅ {tier.upper()} FILLED: {fill.num_filled}x @ ${entry_price:.2f} (comm=${fill.commission:.2f})")
            return pos

        # Handle partial fills — track whatever got filled
        if fill.status == OrderStatus.PARTIAL and fill.num_filled > 0:
            entry_price = abs(fill.avg_fill_price)
            pos = LiveScalpPosition(
                ticker=ticker, strike=strike, right=right,
                direction=signal_direction, expiry=expiry, tier=tier,
                confirmations=confirmations, confidence=confidence,
                entry_time=datetime.now(), entry_underlying=price,
                entry_premium=entry_price, num_contracts=fill.num_filled,
                entry_commission=fill.commission,
                atr_at_entry=atr,
                stop_price=stop_price, target_price=target_price,
                best_favorable_underlying=price,
            )
            logger.warning(
                f"Partial fill: {fill.num_filled}/{num_contracts} @ ${entry_price:.2f}"
            )
            print(f"  ⚠️  {tier.upper()} PARTIAL FILL: {fill.num_filled}/{num_contracts} "
                  f"@ ${entry_price:.2f}")
            return pos

        print(f"  ❌ Not filled: {fill.status.value}")
        return None

    # ═════════════════════════════════════════════════════════════
    # Strike Selection (live chain)
    # ═════════════════════════════════════════════════════════════

    def _select_strike(
        self,
        ticker: str,
        expiry: str,
        right: str,
        delta_min: float,
        delta_max: float,
        min_prem: float,
        max_prem: float,
        premium_scale: float,
    ) -> Optional[Dict]:
        """Select ATM/OTM strike from live options chain."""
        if not self.provider:
            return None

        try:
            chain = self.provider.get_options_chain(
                ticker, expiry=expiry, strikes_around_atm=10,
            )
        except Exception as e:
            logger.warning(f"Failed to get options chain: {e}")
            return None

        if not chain:
            return None

        options = [o for o in chain if o.get("right") == right]
        if not options:
            return None

        candidates = []
        for opt in options:
            d = abs(opt.get("delta", 0))
            ask = opt.get("ask", 0)
            bid = opt.get("bid", 0)
            mid = opt.get("mid", (bid + ask) / 2 if bid and ask else 0)

            if d < delta_min or d > delta_max:
                continue
            if ask <= 0 or ask < min_prem or ask > max_prem:
                continue
            # Reasonable spread
            if mid > 0 and (ask - bid) / mid > 0.30:
                continue

            candidates.append({
                "strike": opt["strike"],
                "right": right,
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "delta": d,
                "gamma": opt.get("gamma", 0),
                "iv": opt.get("iv", 0),
            })

        if not candidates:
            return None

        # Best = closest to ATM (delta ~0.50 for ATM, lower for OTM)
        target_delta = 0.50 if delta_min >= 0.35 else (delta_min + delta_max) / 2
        candidates.sort(key=lambda c: abs(c["delta"] - target_delta))
        return candidates[0]

    # ═════════════════════════════════════════════════════════════
    # Hybrid Position Sizer (mirrors backtester exactly)
    # ═════════════════════════════════════════════════════════════

    def _compute_position_size(
        self,
        balance: float,
        entry_premium: float,
        current_atr: float,
        daily_median_atr: float,
        strategy_tier: str,
        conviction_mult: float = 1.0,
    ) -> int:
        """
        Hybrid Larry Williams + Vol Target position sizer.

        Exactly mirrors ScalpBacktester._compute_position_size().
        """
        sc = self.sizing_cfg
        cost_per_contract = entry_premium * 100

        if cost_per_contract <= 0 or balance <= 0:
            return 0

        # ── Runner: budget-based sizing ──────────────────────────
        if strategy_tier == "runner":
            runner_budget = balance * sc.runner_budget_pct
            runner_num = int(runner_budget / cost_per_contract) if cost_per_contract > 0 else 0
            runner_num = min(runner_num, self.scalp_cfg.runner_max_contracts)
            runner_num = max(1, runner_num) if runner_num >= 1 else 0
            if runner_num > 0 and cost_per_contract * runner_num > balance * sc.runner_balance_gate:
                return 0
            return runner_num

        # ── 1. Larry Williams ────────────────────────────────────
        lookback = sc.lw_lookback
        recent_losses = [
            abs(pnl) for pnl in self._trade_history[-lookback:]
            if pnl < 0
        ]
        worst_loss = max(recent_losses) if recent_losses else sc.lw_default_loss
        if not recent_losses:
            worst_loss *= self._premium_scale  # Scale for SPX/NDX

        lw_risk_dollars = balance * sc.lw_risk_pct
        lw_contracts = lw_risk_dollars / cost_per_contract if cost_per_contract > 0 else 0

        # ── 2. Vol Target ────────────────────────────────────────
        atr_ratio = (current_atr / daily_median_atr) if daily_median_atr > 0 else 1.0
        atr_ratio = max(0.5, min(atr_ratio, 3.0))
        vol_risk_dollars = (balance * sc.vol_target_pct) / atr_ratio
        vol_contracts = vol_risk_dollars / cost_per_contract if cost_per_contract > 0 else 0

        # ── 3. Absolute max ──────────────────────────────────────
        abs_max_dollars = balance * sc.absolute_max_pct
        abs_contracts = abs_max_dollars / cost_per_contract if cost_per_contract > 0 else 0

        # ── Minimum of all three ─────────────────────────────────
        raw_contracts = min(lw_contracts, vol_contracts, abs_contracts)
        raw_contracts *= conviction_mult

        contracts = int(raw_contracts)
        contracts = max(sc.min_contracts, contracts)

        # ── Balance gate ─────────────────────────────────────────
        if cost_per_contract * contracts > balance * sc.balance_gate_pct:
            max_affordable = int(balance * sc.balance_gate_pct / cost_per_contract)
            if max_affordable >= sc.min_contracts:
                contracts = max_affordable
            else:
                return 0

        return contracts

    def _record_trade_pnl(self, pnl: float, atr: float):
        """Record closed trade for hybrid sizer's lookback."""
        self._trade_history.append(pnl)
        self._atr_history.append(atr)

    # ═════════════════════════════════════════════════════════════
    # Helpers
    # ═════════════════════════════════════════════════════════════

    def _classify_regime(self, bars: pd.DataFrame):
        """Classify the day's regime using available bars."""
        if len(bars) >= 60:
            self._regime_info = self._regime.classify_early(bars, orb_bars=60)
        else:
            self._regime_info = self._regime.classify(bars)
        self._regime_classified = True
        logger.info(
            f"Regime classified: {self._regime_info.regime} "
            f"(range={self._regime_info.day_range_pct:.4f})"
        )

    def _estimate_iv(self, bars: pd.DataFrame) -> float:
        """Estimate IV from realized volatility of recent bars."""
        if bars is None or len(bars) < 10:
            return 0.18
        returns = bars["close"].pct_change().dropna().tail(20)
        if len(returns) < 5:
            return 0.18
        bar_vol = returns.std()
        annual_vol = bar_vol * math.sqrt(390 * 252)
        return max(0.10, min(1.00, annual_vol))

    # ═════════════════════════════════════════════════════════════
    # Properties
    # ═════════════════════════════════════════════════════════════

    @property
    def open_positions(self) -> List[LiveScalpPosition]:
        """All currently open positions across all tiers."""
        positions = []
        for attr in [self.open_scalp, self.open_runner, self.open_orb, self.open_rf]:
            if attr is not None and attr.is_open:
                positions.append(attr)
        return positions

    @property
    def trades_today(self) -> int:
        """Total trades entered today across all tiers."""
        return (self.scalp_trades_today + self.runner_trades_today
                + self.orb_trades_today + self.rf_trades_today)

    @property
    def daily_realized_pnl(self) -> float:
        """Alias for daily_pnl."""
        return self.daily_pnl

    # ═════════════════════════════════════════════════════════════
    # Status Display
    # ═════════════════════════════════════════════════════════════

    def print_status(self):
        """Print current engine status."""
        print(f"\n  ⚡ LIVE SCALP ENGINE STATUS")
        print(f"  {'─' * 55}")
        print(f"  Daily P&L:     ${self.daily_pnl:+.2f}")
        print(f"  Trades today:  {self.trades_today}")
        print(f"    Scalp:  {self.scalp_trades_today}/{self.scalp_cfg.max_trades_per_day}")
        print(f"    Runner: {self.runner_trades_today}/{self.scalp_cfg.runner_max_per_day}")
        print(f"    ORB:    {self.orb_trades_today}/{self.orb_cfg.max_trades_per_day}")
        print(f"    RF:     {self.rf_trades_today}/{self.rf_cfg.max_trades_per_day}")
        print(f"  Stopped dirs:  {', '.join(self.stopped_directions) or 'none'}")
        print(f"  Cooldown:      {self.cooldown_remaining} bars")

        if self._regime_info:
            print(f"  Regime:        {self._regime_info.regime} "
                  f"(range={self._regime_info.day_range_pct:.4f})")

        for tier, pos in [
            ("SCALP", self.open_scalp),
            ("RUNNER", self.open_runner),
            ("ORB", self.open_orb),
            ("RF", self.open_rf),
        ]:
            if pos and pos.is_open:
                print(f"  {tier} Position:")
                print(f"    {pos.direction} {pos.ticker} {pos.strike}{pos.right}")
                print(f"    Entry: ${pos.entry_underlying:.2f} (prem ${pos.entry_premium:.2f})")
                print(f"    Stop:  ${pos.stop_price:.2f}  Target: ${pos.target_price:.2f}")
                print(f"    Signals: {', '.join(pos.confirmations)}")

    def can_enter(self) -> tuple:
        """Check if engine can accept new entries. Returns (can_enter, reason)."""
        if self.daily_pnl <= -self.scalp_cfg.daily_loss_limit:
            return False, f"Daily loss limit (${self.daily_pnl:.2f})"
        return True, "OK"

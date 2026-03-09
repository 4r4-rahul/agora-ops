#!/usr/bin/env python3
"""
Live Trading Loop — 0DTE Credit Spreads on IBKR
=================================================
Production execution loop that:
  1. Connects to IBKR
  2. Classifies market regime (VIX-based)
  3. Runs production filter stack (ATR sizing)
  4. Scans options chains for optimal strikes
  5. Passes through risk manager pre-trade checks
  6. Executes via OrderExecutor with human confirmation
  7. Monitors positions for stop/target/gamma exits
  8. Persists all state to survive restarts

Safety layers:
  ┌──────────────────────────────────────────────────────────┐
  │  Layer 1: VIX Regime Gate      (RED = no trading)        │
  │  Layer 2: ATR Position Sizing  (>2% = skip day)          │
  │  Layer 3: StateManager limits  (daily/weekly/monthly)    │
  │  Layer 4: RiskManager checks   (7 pre-trade rules)       │
  │  Layer 5: Human Confirmation   (y/N prompt before order)  │
  │  Layer 6: Kill Switch          (flatten_all on demand)    │
  └──────────────────────────────────────────────────────────┘

Usage:
    # Paper trading (default)
    python run_live.py

    # Skip confirmation gate (automated mode)
    python run_live.py --no-confirm

    # Specific tickers
    python run_live.py --tickers SPY QQQ

    # Kill switch (flatten everything NOW)
    python run_live.py --kill

    # Dry run (simulate without placing orders)
    python run_live.py --dry-run

    # Status check only
    python run_live.py --status
"""

import os
import sys
import time
import signal
import argparse
import logging
from datetime import datetime, date, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import pandas as pd
import numpy as np

# ── Project imports ──
from trading_engine.config import EngineConfig, AdaptiveConfig, AccountConfig
from trading_engine.filters import ProductionFilters, FilterDecision
from trading_engine.execution import OrderExecutor, OrderStatus, FillResult, LivePosition
from trading_engine.execution.state import StateManager
from trading_engine.execution.safety import SafetyMonitor, AlertLevel

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# Market Hours
# ─────────────────────────────────────────────────────────────────

def market_is_open() -> bool:
    """Check if US equity market is currently open."""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo("US/Eastern"))

    # Weekend check
    if now.weekday() >= 5:
        return False

    # Market hours: 9:30 AM - 4:00 PM ET
    open_time = now.replace(hour=9, minute=30, second=0, microsecond=0)
    close_time = now.replace(hour=16, minute=0, second=0, microsecond=0)

    return open_time <= now <= close_time


def minutes_since_open() -> int:
    """Minutes elapsed since 9:30 AM ET."""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo("US/Eastern"))
    open_time = now.replace(hour=9, minute=30, second=0, microsecond=0)
    return int((now - open_time).total_seconds() / 60)


def minutes_to_close() -> int:
    """Minutes remaining until 4:00 PM ET."""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo("US/Eastern"))
    close_time = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return int((close_time - now).total_seconds() / 60)


def eastern_now() -> datetime:
    """Current time in US/Eastern."""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("US/Eastern"))


# ─────────────────────────────────────────────────────────────────
# VIX Fetch (lightweight — no full MarketSnapshot needed)
# ─────────────────────────────────────────────────────────────────

def get_current_vix(ibkr_provider) -> float:
    """Fetch current VIX level from IBKR."""
    try:
        ib = ibkr_provider._ib
        ib_insync = ibkr_provider._ib_insync

        vix_contract = ib_insync.Index("VIX", "CBOE")
        ib.qualifyContracts(vix_contract)
        ticker = ib.reqMktData(vix_contract, "", False, False)
        ib.sleep(2)  # Wait for data

        vix = ticker.marketPrice()
        if vix and not np.isnan(vix) and vix > 0:
            ib.cancelMktData(vix_contract)
            return float(vix)

        # Fallback: last price
        vix = ticker.last
        if vix and not np.isnan(vix) and vix > 0:
            ib.cancelMktData(vix_contract)
            return float(vix)

        ib.cancelMktData(vix_contract)
    except Exception as e:
        logger.warning(f"Failed to get VIX from IBKR: {e}")

    # Fallback: use yfinance
    try:
        import yfinance as yf
        vix_data = yf.download("^VIX", period="1d", progress=False)
        if not vix_data.empty:
            return float(vix_data["Close"].iloc[-1])
    except Exception:
        pass

    logger.warning("Could not get VIX, defaulting to 18.0")
    return 18.0


def classify_regime(vix: float) -> str:
    """Classify VIX into regime. Matches AdaptiveConfig thresholds."""
    if vix < 18:
        return "GREEN"
    elif vix < 25:
        return "YELLOW"
    else:
        return "RED"


# ─────────────────────────────────────────────────────────────────
# Strike Selection
# ─────────────────────────────────────────────────────────────────

def find_optimal_strikes(
    chain: List[Dict],
    right: str,
    target_delta: float,
    spread_width: float,
    underlying_price: float,
) -> Optional[Dict]:
    """
    Find optimal short + long strikes from a live options chain.

    Args:
        chain:            Options chain from ibkr_provider.get_options_chain()
        right:            "C" for calls, "P" for puts
        target_delta:     Target delta for short strike (e.g., 0.15)
        spread_width:     Desired width in dollars (e.g., 2.0)
        underlying_price: Current price of underlying

    Returns:
        Dict with short_strike, long_strike, credit, delta, etc.
        or None if no suitable strikes found.
    """
    # Filter to the right side
    options = [o for o in chain if o.get("right") == right.upper()]
    if not options:
        return None

    # Sort by strike
    options.sort(key=lambda o: o["strike"])

    # Find the short strike closest to target delta
    best_short = None
    best_delta_diff = float("inf")

    for opt in options:
        delta = abs(opt.get("delta", 0))
        if delta <= 0:
            continue

        diff = abs(delta - target_delta)
        if diff < best_delta_diff:
            best_delta_diff = diff
            best_short = opt

    if not best_short:
        return None

    short_strike = best_short["strike"]

    # Find the long strike (further OTM by spread_width)
    if right.upper() == "P":
        long_strike = short_strike - spread_width
    else:
        long_strike = short_strike + spread_width

    # Find actual contract for the long leg
    best_long = None
    best_strike_diff = float("inf")
    for opt in options:
        diff = abs(opt["strike"] - long_strike)
        if diff < best_strike_diff:
            best_strike_diff = diff
            best_long = opt

    if not best_long:
        return None

    # Estimate credit (short bid - long ask, conservative)
    credit = best_short.get("bid", 0) - best_long.get("ask", 0)
    if credit <= 0:
        credit = (best_short.get("mid", 0) - best_long.get("mid", 0)) * 0.85

    actual_width = abs(best_short["strike"] - best_long["strike"])
    max_loss = actual_width - credit if credit > 0 else actual_width

    return {
        "short_strike": best_short["strike"],
        "long_strike": best_long["strike"],
        "credit": round(credit, 2),
        "max_loss": round(max_loss, 2),
        "short_delta": abs(best_short.get("delta", 0)),
        "short_gamma": abs(best_short.get("gamma", 0)),
        "short_iv": best_short.get("iv", 0),
        "short_bid": best_short.get("bid", 0),
        "short_ask": best_short.get("ask", 0),
        "long_bid": best_long.get("bid", 0),
        "long_ask": best_long.get("ask", 0),
        "width": actual_width,
    }


# ─────────────────────────────────────────────────────────────────
# Position Monitor
# ─────────────────────────────────────────────────────────────────

class PositionMonitor:
    """
    Monitors open positions and triggers exits.

    Exit conditions (from backtester validation):
      1. Stop-loss:     spread value > entry_credit × stop_mult
      2. Profit target: spread value < entry_credit × (1 - profit_target)
      3. Gamma blowup:  gamma > gamma_limit (0.15)
      4. Time exit:     < 15 min to close
    """

    def __init__(self, executor: OrderExecutor, state: StateManager, safety: Optional[SafetyMonitor] = None):
        self.executor = executor
        self.state = state
        self._safety_ref = safety
        self.positions: List[Dict] = []

    def add_position(self, pos_data: Dict):
        """Track a new position for monitoring."""
        self.positions.append(pos_data)

    def check_exits(self, ibkr_provider, regime_params) -> List[Dict]:
        """
        Check all open positions for exit triggers.

        Returns list of positions that were closed.
        """
        if not self.positions:
            return []

        closed = []
        remaining = []

        for pos in self.positions:
            exit_reason = self._check_single_exit(pos, ibkr_provider, regime_params)

            if exit_reason:
                # Execute the exit
                fill = self.executor.close_credit_spread(
                    ticker=pos["ticker"],
                    expiry=pos["expiry"],
                    short_strike=pos["short_strike"],
                    long_strike=pos["long_strike"],
                    right=pos["right"],
                    num_contracts=pos["num_contracts"],
                )

                realized_pnl = 0.0
                if fill.status == OrderStatus.FILLED:
                    entry_credit = pos.get("entry_credit", 0)
                    exit_debit = abs(fill.avg_fill_price)
                    realized_pnl = (entry_credit - exit_debit) * fill.num_filled * 100
                    realized_pnl -= fill.commission

                    print(f"  📊 Closed: {pos['ticker']} {pos['short_strike']}/{pos['long_strike']}"
                          f"{pos['right']} → {exit_reason} | P&L: ${realized_pnl:+.2f}")

                    self.state.record_fill(fill, realized_pnl)
                    self.state.remove_position(
                        pos["ticker"], pos["short_strike"], pos["right"]
                    )

                    # Notify via safety webhook
                    if self._safety_ref:
                        strike_str = f"{pos['short_strike']}/{pos['long_strike']}{pos['right']}"
                        self._safety_ref.send_trade_alert(
                            "CLOSE", pos["ticker"], strike_str,
                            0.0, realized_pnl,
                        )

                pos["exit_reason"] = exit_reason
                pos["realized_pnl"] = realized_pnl
                closed.append(pos)
            else:
                remaining.append(pos)

        self.positions = remaining
        return closed

    def _check_single_exit(self, pos: Dict, ibkr_provider, regime_params) -> Optional[str]:
        """Check if a single position should be closed."""
        # 1. Time exit: < 15 min to close
        ttc = minutes_to_close()
        if ttc <= 15:
            return f"TIME_EXIT ({ttc}min to close)"

        # 2. Get current spread value
        try:
            chain = ibkr_provider.get_options_chain(
                pos["ticker"],
                expiry=pos["expiry"],
                right=pos["right"],
                strikes_around_atm=20,
            )
            short_opt = next((o for o in chain if abs(o["strike"] - pos["short_strike"]) < 0.01), None)
            long_opt = next((o for o in chain if abs(o["strike"] - pos["long_strike"]) < 0.01), None)

            if short_opt and long_opt:
                current_spread = short_opt.get("mid", 0) - long_opt.get("mid", 0)
                entry_credit = pos.get("entry_credit", 0)

                # Stop-loss
                stop_mult = getattr(regime_params, "stop_mult", 2.5)
                stop_price = entry_credit * stop_mult
                if current_spread >= stop_price:
                    return f"STOP_LOSS (spread={current_spread:.2f} >= {stop_price:.2f})"

                # Profit target
                profit_target = getattr(regime_params, "profit_target", 0.75)
                target_value = entry_credit * (1.0 - profit_target)
                if current_spread <= target_value:
                    return f"PROFIT_TARGET (spread={current_spread:.2f} <= {target_value:.2f})"

                # Gamma blowup
                gamma_limit = getattr(regime_params, "gamma_limit", 0.15)
                short_gamma = abs(short_opt.get("gamma", 0))
                if short_gamma > gamma_limit:
                    return f"GAMMA_EXIT (γ={short_gamma:.4f} > {gamma_limit})"

        except Exception as e:
            logger.warning(f"Could not check exit for {pos['ticker']}: {e}")

        return None


# ─────────────────────────────────────────────────────────────────
# Main Live Loop
# ─────────────────────────────────────────────────────────────────

class LiveTradingLoop:
    """
    Main live trading loop.

    Lifecycle:
      pre_open()   → Load state, connect, sync positions
      scan_entry() → Find and place new trades
      monitor()    → Check exits on open positions
      post_close() → Save state, log summary
    """

    def __init__(
        self,
        tickers: List[str] = None,
        config: EngineConfig = None,
        dry_run: bool = False,
        require_confirmation: bool = True,
        account_size: float = 50_000.0,
    ):
        self.tickers = tickers or os.getenv("TRADE_TICKERS", "SPY").split(",")
        self.config = config or EngineConfig()
        self.dry_run = dry_run
        self.account_size = account_size

        # Components
        self.adaptive = AdaptiveConfig()
        self.filters = ProductionFilters()
        self.state = StateManager(account_size=account_size)
        self.executor = None
        self.provider = None
        self.monitor = None
        self.safety: Optional[SafetyMonitor] = None

        # Confirmation gate
        self.require_confirmation = require_confirmation

        # Loop control
        self._running = False
        self._entry_placed_today = set()  # Track tickers we've entered

        # Timing
        self.scan_interval_sec = 60       # Scan for entries every 60s
        self.monitor_interval_sec = 30    # Check exits every 30s

    # ─── Setup ───────────────────────────────────────────────────

    def setup(self) -> bool:
        """Initialize all components. Returns True if ready."""
        print("\n" + "═" * 60)
        print("  0DTE LIVE TRADING ENGINE")
        print("═" * 60)
        print(f"  Tickers:      {', '.join(self.tickers)}")
        print(f"  Account:      ${self.account_size:,.0f}")
        print(f"  Dry Run:      {'YES ⚠️' if self.dry_run else 'NO — LIVE ORDERS'}")
        print(f"  Confirmation: {'Required' if self.require_confirmation else 'Auto'}")
        print("═" * 60)

        # 1. Load persisted state
        print("\n  📂 Loading state...")
        self.state.load()
        self.state.print_status()

        # Check if we can trade today
        can_trade, reason = self.state.can_trade()
        if not can_trade:
            print(f"\n  ❌ CANNOT TRADE TODAY: {reason}")
            return False

        # 2. Connect to IBKR
        print("\n  🔌 Connecting to IBKR...")
        if not self.dry_run:
            from trading_engine.data.ibkr_provider import IBKRDataProvider
            self.provider = IBKRDataProvider()
            if not self.provider.connect():
                print("  ❌ Failed to connect to IBKR")
                return False

            self.executor = OrderExecutor(
                ibkr_provider=self.provider,
                require_confirmation=self.require_confirmation,
            )
            self.executor.connect()
        else:
            print("  🧪 DRY RUN — no IBKR connection needed")

        # 3. Safety monitor (before position monitor so it can be passed in)
        webhook_url = os.getenv("ALERT_WEBHOOK_URL")
        self.safety = SafetyMonitor(
            state_manager=self.state,
            executor=self.executor,
            account_size=self.account_size,
            webhook_url=webhook_url,
        )
        if webhook_url:
            print(f"  🔔 Webhook alerts: ENABLED")
            self.safety.send_alert(
                f"🟢 Trading engine starting — {', '.join(self.tickers)} | "
                f"${self.account_size:,.0f} | {'DRY RUN' if self.dry_run else 'LIVE'}",
                AlertLevel.INFO,
            )
        else:
            print(f"  🔔 Webhook alerts: OFF (set ALERT_WEBHOOK_URL to enable)")

        # 4. Position monitor
        self.monitor = PositionMonitor(
            executor=self.executor,
            state=self.state,
            safety=self.safety,
        )

        # 5. Sync with IBKR positions
        if self.provider and not self.dry_run:
            print("\n  🔄 Syncing positions with IBKR...")
            sync = self.state.sync_positions(self.provider)

            # Load existing positions into monitor
            for pos in self.state.state.open_positions:
                self.monitor.add_position(pos)

        print("\n  ✅ Setup complete — ready for trading")
        return True

    # ─── Entry Logic ─────────────────────────────────────────────

    def scan_entry(self):
        """
        Scan for new trade entries.

        Flow:
          VIX → regime → ATR filter → options chain → strike selection
          → risk check → order placement
        """
        now = eastern_now()
        msopen = minutes_since_open()
        print(f"\n  🔍 Entry Scan [{now.strftime('%H:%M:%S')} ET, {msopen}min since open]")

        # 0. Safety pre-check
        if self.safety:
            safe, alerts = self.safety.check()
            if not safe:
                print(f"  🚨 SAFETY HALT — kill switch triggered")
                self._running = False
                return
            for a in alerts:
                if "[WARNING]" in a:
                    print(f"  ⚠️  {a}")

        # 1. Get VIX and classify regime
        vix = get_current_vix(self.provider) if self.provider else 18.0
        regime = classify_regime(vix)
        params = self.adaptive.for_regime(regime)

        # VIX spike detection
        if self.safety:
            spike_alert = self.safety.check_vix_spike(vix)
            if spike_alert:
                print(f"  {spike_alert}")
                print(f"  🚨 VIX spike detected — aborting entry scan")
                return

        print(f"  VIX: {vix:.1f} → Regime: {regime}")

        # RED regime = no trading
        if not params.trade_enabled:
            print(f"  🔴 {regime} regime — trading disabled")
            return

        # 2. Check entry window
        if msopen < params.entry_start_min:
            print(f"  ⏳ Too early: {msopen}min < {params.entry_start_min}min window start")
            return
        if msopen > params.entry_end_min:
            print(f"  ⏳ Too late: {msopen}min > {params.entry_end_min}min window end")
            return

        # 3. ATR filter (need recent daily bars)
        filter_decision = self._run_atr_filter()
        if filter_decision and filter_decision.skip:
            print(f"  📊 ATR filter: SKIP — {filter_decision.reason}")
            return

        # Compound size multipliers: regime × ATR × recovery
        size_mult = params.position_size_mult
        if filter_decision:
            size_mult *= filter_decision.size_multiplier
        size_mult *= self.state.get_size_multiplier()

        if size_mult <= 0:
            print(f"  📊 Combined size multiplier = 0 — skipping")
            return

        # 4. Scan each ticker
        for ticker in self.tickers:
            if ticker in self._entry_placed_today:
                print(f"  {ticker}: Already entered today — skip")
                continue

            self._try_entry(ticker, params, vix, size_mult)

    def _run_atr_filter(self) -> Optional[FilterDecision]:
        """Run ATR filter using cached or fresh daily bars."""
        try:
            if self.provider:
                # Fetch recent daily bars for ATR computation
                daily = self.provider.get_historical_bars(
                    self.tickers[0], days=30, interval="1d"
                )
                if daily is not None and len(daily) >= 15:
                    decision = self.filters.pre_entry(
                        history_closes=daily["close"].tolist(),
                        history_highs=daily["high"].tolist(),
                        history_lows=daily["low"].tolist(),
                    )
                    print(f"  📊 ATR filter: {decision.reason} (mult={decision.size_multiplier})")
                    return decision
        except Exception as e:
            logger.warning(f"ATR filter failed: {e}")

        return None

    def _try_entry(self, ticker: str, params, vix: float, size_mult: float):
        """Attempt to enter a trade for one ticker."""
        print(f"\n  🎯 {ticker}: Scanning for {params.preferred_strategy}...")

        if self.dry_run:
            print(f"  🧪 DRY RUN — would scan {ticker} options chain")
            print(f"     Strategy:  {params.preferred_strategy}")
            print(f"     Delta:     {params.delta}")
            print(f"     Width:     ${params.width}")
            print(f"     Size mult: {size_mult:.2f}")
            self._entry_placed_today.add(ticker)
            return

        # Get today's expiry (0DTE)
        expiry = date.today().strftime("%Y%m%d")

        # Get options chain
        try:
            chain = self.provider.get_options_chain(
                ticker, expiry=expiry, strikes_around_atm=15,
            )
        except Exception as e:
            print(f"  ⚠️  Failed to get options chain for {ticker}: {e}")
            return

        if not chain:
            print(f"  ⚠️  Empty options chain for {ticker}")
            return

        # Determine trade direction
        strategy = params.preferred_strategy
        if strategy == "call_credit":
            right = "C"
        elif strategy == "put_credit":
            right = "P"
        elif strategy == "iron_condor":
            self._try_iron_condor(ticker, chain, params, vix, size_mult, expiry)
            return
        else:
            print(f"  ⚠️  Unknown strategy: {strategy}")
            return

        # Find optimal strikes
        # Get underlying price
        underlying_price = chain[0].get("underlying_price", 0) if chain else 0
        if underlying_price <= 0:
            # Fetch from IBKR
            try:
                bars = self.provider.get_historical_bars(ticker, days=1, interval="1m")
                underlying_price = bars["close"].iloc[-1]
            except Exception:
                print(f"  ⚠️  Cannot determine {ticker} price")
                return

        strikes = find_optimal_strikes(
            chain, right, params.delta, params.width, underlying_price,
        )

        if not strikes:
            print(f"  ⚠️  No suitable strikes found for {ticker} {right}")
            return

        if strikes["credit"] < 0.20:
            print(f"  ⚠️  Credit too low: ${strikes['credit']:.2f} < $0.20 minimum")
            return

        # Calculate position size
        base_contracts = max(1, int(self.account_size * 0.03 /
                                    (strikes["width"] * 100)))
        num_contracts = max(1, int(base_contracts * size_mult))

        total_risk = strikes["width"] * num_contracts * 100
        total_credit = strikes["credit"] * num_contracts * 100

        print(f"  Strike Selection:")
        print(f"    Short: {strikes['short_strike']} (Δ={strikes['short_delta']:.3f})")
        print(f"    Long:  {strikes['long_strike']}")
        print(f"    Credit: ${strikes['credit']:.2f}/contract")
        print(f"    Contracts: {num_contracts} (base={base_contracts}, mult={size_mult:.2f})")
        print(f"    Total credit: ${total_credit:.2f}")
        print(f"    Total risk:   ${total_risk:.2f}")

        # Pre-trade risk check via state manager
        can_trade, reason = self.state.can_trade()
        if not can_trade:
            print(f"  ❌ Risk check failed: {reason}")
            return

        # Place the order
        fill = self.executor.place_credit_spread(
            ticker=ticker,
            expiry=expiry,
            short_strike=strikes["short_strike"],
            long_strike=strikes["long_strike"],
            right=right,
            num_contracts=num_contracts,
            limit_credit=strikes["credit"],
        )

        if fill.status == OrderStatus.FILLED:
            print(f"  ✅ FILLED: {fill.num_filled}x @ ${abs(fill.avg_fill_price):.2f}")

            # Track in state + monitor
            pos_data = {
                "ticker": ticker,
                "short_strike": strikes["short_strike"],
                "long_strike": strikes["long_strike"],
                "right": right,
                "expiry": expiry,
                "num_contracts": fill.num_filled,
                "entry_credit": abs(fill.avg_fill_price),
                "entry_time": datetime.now().isoformat(),
                "vix_at_entry": vix,
                "regime": classify_regime(vix),
            }
            self.state.add_position(pos_data)
            self.monitor.add_position(pos_data)
            self.state.record_fill(fill, 0.0)  # Entry, no realized PnL yet
            self._entry_placed_today.add(ticker)

            # Send webhook alert
            if self.safety:
                strike_str = f"{strikes['short_strike']}/{strikes['long_strike']}{right}"
                self.safety.send_trade_alert(
                    "OPEN", ticker, strike_str, abs(fill.avg_fill_price)
                )

        elif fill.status == OrderStatus.PARTIAL:
            print(f"  ⚠️  Partial fill: {fill.num_filled}/{fill.num_contracts}")
        else:
            print(f"  ❌ Not filled: {fill.status.value} — {fill.error_msg}")

    def _try_iron_condor(self, ticker, chain, params, vix, size_mult, expiry):
        """Try to enter an iron condor."""
        underlying_price = chain[0].get("underlying_price", 0) if chain else 0

        put_strikes = find_optimal_strikes(
            chain, "P", params.delta, params.width, underlying_price
        )
        call_strikes = find_optimal_strikes(
            chain, "C", params.delta, params.width, underlying_price
        )

        if not put_strikes or not call_strikes:
            print(f"  ⚠️  Cannot build iron condor — missing strikes")
            return

        total_credit = put_strikes["credit"] + call_strikes["credit"]
        if total_credit < 0.40:
            print(f"  ⚠️  IC credit too low: ${total_credit:.2f}")
            return

        base_contracts = max(1, int(self.account_size * 0.03 /
                                    (max(put_strikes["width"], call_strikes["width"]) * 100)))
        num_contracts = max(1, int(base_contracts * size_mult))

        print(f"  Iron Condor:")
        print(f"    Put spread:  {put_strikes['short_strike']}/{put_strikes['long_strike']}P "
              f"(${put_strikes['credit']:.2f})")
        print(f"    Call spread: {call_strikes['short_strike']}/{call_strikes['long_strike']}C "
              f"(${call_strikes['credit']:.2f})")
        print(f"    Contracts:   {num_contracts}")
        print(f"    Total credit: ${total_credit * num_contracts * 100:.2f}")

        put_fill, call_fill = self.executor.place_iron_condor(
            ticker=ticker, expiry=expiry,
            put_short=put_strikes["short_strike"],
            put_long=put_strikes["long_strike"],
            call_short=call_strikes["short_strike"],
            call_long=call_strikes["long_strike"],
            num_contracts=num_contracts,
            limit_credit=total_credit,
        )

        # Track filled legs
        for fill, strikes, right in [
            (put_fill, put_strikes, "P"),
            (call_fill, call_strikes, "C"),
        ]:
            if fill.status == OrderStatus.FILLED:
                pos_data = {
                    "ticker": ticker,
                    "short_strike": strikes["short_strike"],
                    "long_strike": strikes["long_strike"],
                    "right": right,
                    "expiry": expiry,
                    "num_contracts": fill.num_filled,
                    "entry_credit": abs(fill.avg_fill_price),
                    "entry_time": datetime.now().isoformat(),
                    "vix_at_entry": vix,
                }
                self.state.add_position(pos_data)
                self.monitor.add_position(pos_data)

        self._entry_placed_today.add(ticker)

    # ─── Monitoring Loop ─────────────────────────────────────────

    def monitor_positions(self):
        """Check all positions for exit triggers."""
        if not self.monitor or not self.monitor.positions:
            return

        vix = get_current_vix(self.provider) if self.provider else 18.0
        regime = classify_regime(vix)
        params = self.adaptive.for_regime(regime)

        closed = self.monitor.check_exits(self.provider, params)
        if closed:
            print(f"\n  📊 Closed {len(closed)} position(s)")
            for c in closed:
                print(f"    {c['ticker']} {c['short_strike']}/{c['long_strike']}{c['right']}"
                      f" → {c['exit_reason']} (${c.get('realized_pnl', 0):+.2f})")

    # ─── Main Loop ───────────────────────────────────────────────

    def run(self):
        """
        Main trading loop. Runs until market close or interrupted.

        Timeline:
          9:30 → 10:00:  Wait (let opening volatility settle)
          10:00 → 10:30: Entry window (scan for trades)
          10:30 → 15:45: Monitor positions
          15:45 → 16:00: Time-exit any remaining positions
        """
        if not self.setup():
            return

        self._running = True

        # Install signal handler for clean shutdown
        def signal_handler(sig, frame):
            print(f"\n\n  🛑 Shutdown requested (signal {sig})")
            self._running = False

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        print(f"\n  🚀 Live loop starting...")
        print(f"  Press Ctrl+C to stop gracefully\n")

        last_scan = 0
        last_monitor = 0
        entry_done = False

        try:
            while self._running:
                if not market_is_open():
                    if entry_done:
                        # Market closed after we were trading
                        print(f"\n  🏁 Market closed")
                        break
                    else:
                        print(f"  ⏳ Waiting for market open... ({eastern_now().strftime('%H:%M')} ET)")
                        time.sleep(30)
                        continue

                now = time.time()
                msopen = minutes_since_open()
                ttc = minutes_to_close()

                # Entry scan (during entry window, once per scan_interval)
                if not entry_done and now - last_scan >= self.scan_interval_sec:
                    if msopen >= 30:  # At least 30 min after open
                        self.scan_entry()
                        last_scan = now

                        # Check if we've passed the entry window for all tickers
                        params = self.adaptive.for_regime(
                            classify_regime(
                                get_current_vix(self.provider) if self.provider else 18.0
                            )
                        )
                        if msopen > params.entry_end_min:
                            entry_done = True
                            remaining = [t for t in self.tickers
                                         if t not in self._entry_placed_today]
                            if remaining:
                                print(f"\n  ⏰ Entry window closed. No entry for: {', '.join(remaining)}")
                            else:
                                print(f"\n  ✅ All entries placed. Switching to monitor mode.")

                # Safety check each monitoring cycle
                if now - last_monitor >= self.monitor_interval_sec:
                    if self.safety:
                        safe, alerts = self.safety.check()
                        if not safe:
                            print(f"\n  🚨 SAFETY: Kill switch triggered during monitoring")
                            self._running = False
                            break

                        # VIX spike check during monitoring phase
                        if self.provider:
                            current_vix = get_current_vix(self.provider)
                            spike = self.safety.check_vix_spike(current_vix)
                            if spike:
                                print(f"\n  {spike}")

                    # Position monitoring (continuous after entries)
                    if self.monitor and self.monitor.positions:
                        self.monitor_positions()
                    last_monitor = now

                # Time exit warning
                if ttc <= 20 and self.monitor and self.monitor.positions:
                    print(f"\n  ⏱️  {ttc} min to close — checking time exits...")
                    self.monitor_positions()

                # Sleep between iterations
                time.sleep(5)

        except Exception as e:
            logger.error(f"Unexpected error in main loop: {e}", exc_info=True)
            print(f"\n  ❌ ERROR: {e}")

        finally:
            self._shutdown()

    def _shutdown(self):
        """Clean shutdown: save state, log summary, disconnect."""
        print(f"\n  🔒 Shutting down...")

        # Save state
        self.state.save()
        print(f"  💾 State saved")

        # Save order log
        if self.executor:
            self.executor.save_order_log()
            self.executor.print_session_summary()

        # Print daily summary
        self.state.print_status()

        # Send daily summary alert
        if self.safety:
            self.safety.send_daily_summary()
            self.safety.send_alert(
                "🔴 Trading engine shutting down", AlertLevel.INFO
            )

        # Disconnect
        if self.provider:
            self.provider.disconnect()

        print(f"\n  👋 Done. See you next trading day!\n")


# ─────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="0DTE Live Trading Engine")
    parser.add_argument("--tickers", nargs="+", default=["SPY"],
                        help="Tickers to trade (default: SPY)")
    parser.add_argument("--account-size", type=float, default=50_000,
                        help="Account size in dollars")
    parser.add_argument("--no-confirm", action="store_true",
                        help="Skip order confirmation prompts")
    parser.add_argument("--dry-run", action="store_true",
                        help="Simulate without placing real orders")
    parser.add_argument("--kill", action="store_true",
                        help="KILL SWITCH: flatten all positions immediately")
    parser.add_argument("--status", action="store_true",
                        help="Show current state and exit")
    parser.add_argument("--validate", action="store_true",
                        help="Run end-to-end paper validation (1 cycle, then exit)")
    parser.add_argument("--validate-dry", action="store_true",
                        help="Validation without placing orders")
    parser.add_argument("--verbose", action="store_true",
                        help="Enable debug logging")

    args = parser.parse_args()

    # Setup logging
    level = logging.DEBUG if args.verbose else logging.INFO

    # Ensure log directory exists
    os.makedirs("logs", exist_ok=True)

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(f"logs/live_{date.today().isoformat()}.log"),
        ],
    )

    # ── Status mode ──
    if args.status:
        state = StateManager(account_size=args.account_size)
        state.load()
        state.print_status()
        return

    # ── Validation mode ──
    if args.validate or args.validate_dry:
        from scripts.paper_validation import run_validation
        report = run_validation(
            ticker=args.tickers[0],
            account_size=args.account_size,
            dry_run=args.validate_dry,
        )
        sys.exit(0 if all(s.passed for s in report.steps) else 1)

    # ── Kill switch mode ──
    if args.kill:
        print("\n  🚨 KILL SWITCH MODE 🚨")
        from trading_engine.data.ibkr_provider import IBKRDataProvider
        provider = IBKRDataProvider()
        if not provider.connect():
            print("  ❌ Cannot connect to IBKR")
            return
        executor = OrderExecutor(ibkr_provider=provider, require_confirmation=False)
        executor.connect()
        executor.flatten_all(reason="Manual kill switch via CLI")
        executor.save_order_log()
        provider.disconnect()
        return

    # ── Live trading mode ──
    loop = LiveTradingLoop(
        tickers=args.tickers,
        dry_run=args.dry_run,
        require_confirmation=not args.no_confirm,
        account_size=args.account_size,
    )
    loop.run()


if __name__ == "__main__":
    main()

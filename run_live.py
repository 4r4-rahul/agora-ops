#!/usr/bin/env python3
"""
Live Trading Loop — 0DTE Multi-Strategy Engine on IBKR
========================================================
Executes the proven backtested strategies in real-time:

  Strategy A — MOMENTUM SCALP:
    ATM options, 3+ confirmation signals, morning + power hour.
    Hybrid LW/VolTarget position sizing.

  Strategy C — RUNNER:
    OTM power-hour plays. Piggyback on momentum signals.
    Trailing stop, let winners run.

  Strategy D — ORB BREAKOUT:
    Opening range breakout, regime-filtered (trending days only).
    Defers to momentum when it fires.

  Strategy E — RANGE FADE:
    Fade range boundaries on RANGE_BOUND / MIXED days.
    Mean-reversion targets.

Validated: 169 trades, PF=4.21, PnL=$192,220, MaxDD=23.8%.

Production execution loop:
  1. Connects to IBKR
  2. Classifies day regime (DEAD_FLAT / RANGE_BOUND / TRENDING / etc.)
  3. Runs ATR production filters
  4. Evaluates all 4 signal engines per tick
  5. Sizes via hybrid Larry Williams + Vol Target sizer
  6. Executes via OrderExecutor with human confirmation
  7. Monitors positions with strategy-specific exit engines
  8. Persists all state to survive restarts

Safety layers:
  ┌──────────────────────────────────────────────────────────┐
  │  Layer 1: VIX Regime Gate      (RED = no entry scans)    │
  │  Layer 2: ATR Production Filter (>2% = skip day)         │
  │  Layer 3: StateManager limits  (daily/weekly/monthly)    │
  │  Layer 4: IV Discount Gate     (RV > IV = cheap options) │
  │  Layer 5: Human Confirmation   (y/N prompt before order)  │
  │  Layer 6: Kill Switch          (flatten_all on demand)    │
  │  Layer 7: Daily Loss Limit     ($500 hard stop)           │
  └──────────────────────────────────────────────────────────┘

Usage:
    # Live trading (default, $10K account)
    python run_live.py

    # Dry run (simulate without placing orders) — RECOMMENDED first
    python run_live.py --dry-run

    # Skip confirmation gate (automated mode)
    python run_live.py --no-confirm

    # Specific tickers (SPY and/or QQQ)
    python run_live.py --tickers SPY QQQ

    # Kill switch (flatten everything NOW)
    python run_live.py --kill

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
from trading_engine.config import EngineConfig
from trading_engine.filters import ProductionFilters, FilterDecision
from trading_engine.execution import OrderExecutor
from trading_engine.execution.state import StateManager
from trading_engine.execution.safety import SafetyMonitor, AlertLevel
from trading_engine.live_engine import LiveScalpEngine

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
        account_size: float = 10_000.0,
    ):
        self.tickers = tickers or os.getenv("TRADE_TICKERS", "SPY").split(",")
        self.config = config or EngineConfig()
        self.dry_run = dry_run
        self.account_size = account_size

        # Components
        self.filters = ProductionFilters()
        self.state = StateManager(account_size=account_size)
        self.executor = None
        self.provider = None
        self.safety: Optional[SafetyMonitor] = None
        self.scalp_engine: Optional[LiveScalpEngine] = None  # Multi-strategy engine

        # Confirmation gate
        self.require_confirmation = require_confirmation

        # Loop control
        self._running = False

        # Timing
        self.scan_interval_sec = 30       # Scan for entries every 30s
        self.monitor_interval_sec = 30    # Check exits every 30s

    # ─── Setup ───────────────────────────────────────────────────

    def setup(self) -> bool:
        """Initialize all components. Returns True if ready."""
        print("\n" + "═" * 60)
        print("  0DTE MULTI-STRATEGY LIVE ENGINE")
        print("═" * 60)
        print(f"  Strategies:   Scalp (A) + Runner (C) + ORB (D) + RangeFade (E)")
        print(f"  Tickers:      {', '.join(self.tickers)}")
        print(f"  Account:      ${self.account_size:,.0f}")
        print(f"  Dry Run:      {'YES ⚠️' if self.dry_run else 'NO — LIVE ORDERS'}")
        print(f"  Confirmation: {'Required' if self.require_confirmation else 'Auto'}")
        print(f"  Sizer:        Hybrid LW(10%) + VolTarget(15%)")
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

        # 3. Safety monitor
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
                f"🟢 Multi-strategy engine starting — {', '.join(self.tickers)} | "
                f"${self.account_size:,.0f} | {'DRY RUN' if self.dry_run else 'LIVE'}",
                AlertLevel.INFO,
            )
        else:
            print(f"  🔔 Webhook alerts: OFF (set ALERT_WEBHOOK_URL to enable)")

        # 4. Multi-strategy scalp engine (replaces old LottoScanner)
        # Use QQQ config for QQQ ticker, default for SPY
        if len(self.tickers) == 1 and self.tickers[0].upper() == "QQQ":
            engine_config = EngineConfig.for_qqq()
        else:
            engine_config = self.config

        self.scalp_engine = LiveScalpEngine(
            provider=self.provider,
            executor=self.executor,
            config=engine_config,
            account_size=self.account_size,
        )
        print(f"  ⚡ Multi-strategy engine: ENABLED")
        print(f"     Scalp(A): {'ON' if True else 'OFF'}")
        print(f"     Runner(C): {'ON' if engine_config.scalp.runner_enabled else 'OFF'}")
        print(f"     ORB(D): {'ON' if engine_config.orb.enabled else 'OFF'}")
        print(f"     RangeFade(E): {'ON' if engine_config.range_fade.enabled else 'OFF'}")

        # 5. Sync with IBKR positions
        if self.provider and not self.dry_run:
            print("\n  🔄 Syncing positions with IBKR...")
            self.state.sync_positions(self.provider)

        print("\n  ✅ Setup complete — ready for trading")
        return True

    # ─── Entry Logic ─────────────────────────────────────────────

    def scan_entry(self):
        """
        Scan for new trade entries via the multi-strategy engine.

        Flow:
          Safety check → VIX gate → ATR filter → LiveScalpEngine.scan()
          (engine handles: signal evaluation → strike selection → sizing → execution)
        """
        now = eastern_now()
        msopen = minutes_since_open()
        print(f"\n  🔍 Strategy Scan [{now.strftime('%H:%M:%S')} ET, {msopen}min since open]")

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

        # 1. Get VIX and check for RED regime (no trading)
        vix = get_current_vix(self.provider) if self.provider else 18.0
        regime = classify_regime(vix)

        # VIX spike detection
        if self.safety:
            spike_alert = self.safety.check_vix_spike(vix)
            if spike_alert:
                print(f"  {spike_alert}")
                print(f"  🚨 VIX spike detected — aborting entry scan")
                return

        print(f"  VIX: {vix:.1f} → Regime: {regime}")

        if regime == "RED":
            print(f"  🔴 RED regime — no new entries")
            return

        # 2. ATR production filter
        filter_decision = self._run_atr_filter()
        if filter_decision and filter_decision.skip:
            print(f"  📊 ATR filter: SKIP — {filter_decision.reason}")
            return

        # 3. Can the engine accept entries?
        if self.scalp_engine:
            can, reason = self.scalp_engine.can_enter()
            if not can:
                print(f"  ⛔ Engine gate: {reason}")
                return

        # 4. Scan each ticker through all strategy tiers
        for ticker in self.tickers:
            if not self.scalp_engine:
                continue

            print(f"\n  🎯 {ticker}: Scanning strategies "
                  f"[A=Scalp, C=Runner, D=ORB, E=RangeFade]...")

            entered = self.scalp_engine.scan(
                ticker=ticker,
                minutes_since_open=msopen,
                dry_run=self.dry_run,
            )

            if entered:
                # Track in state manager
                pos_data = entered.to_dict()
                self.state.add_position(pos_data)

                # Send webhook alert
                if self.safety:
                    strike_str = f"{entered.strike}{entered.right}"
                    self.safety.send_trade_alert(
                        "OPEN", entered.ticker, strike_str,
                        entered.entry_premium,
                    )
            else:
                print(f"  {ticker}: No signals fired")

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

    # ─── Monitoring Loop ─────────────────────────────────────────

    def monitor_positions(self):
        """Check all open scalp-engine positions for exit triggers."""
        if not self.scalp_engine:
            return

        positions = self.scalp_engine.open_positions
        if not positions:
            return

        ttc = minutes_to_close()
        closed = self.scalp_engine.check_exits(ttc)

        if closed:
            print(f"\n  📊 Closed {len(closed)} position(s)")
            for pos in closed:
                # Update state manager
                self.state.remove_position(
                    pos.ticker, pos.strike, pos.right
                )
                # Send webhook
                if self.safety:
                    strike_str = f"{pos.strike}{pos.right}"
                    self.safety.send_trade_alert(
                        "CLOSE", pos.ticker, strike_str,
                        0.0, pos.total_pnl,
                    )

    # ─── Main Loop ───────────────────────────────────────────────

    def run(self):
        """
        Main trading loop.  Runs until market close or interrupted.

        Timeline (all 4 strategy windows overlap):
          9:30           Market open — accumulate bars
          9:30 + 16 min  Scalp window starts (A/C)
          9:30 + 30 min  ORB / RangeFade windows open
          15:15          Runner power-hour window
          15:45          Hard time-exit begins
          16:00          Market close → shutdown
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

        last_scan = 0.0
        last_monitor = 0.0
        entered_trading = False

        try:
            while self._running:
                if not market_is_open():
                    if entered_trading:
                        print(f"\n  🏁 Market closed")
                        break
                    else:
                        print(f"  ⏳ Waiting for market open... "
                              f"({eastern_now().strftime('%H:%M')} ET)")
                        time.sleep(30)
                        continue

                entered_trading = True
                now = time.time()
                msopen = minutes_since_open()
                ttc = minutes_to_close()

                # ── Reset engine at day start (once) ──
                if self.scalp_engine and msopen < 2:
                    self.scalp_engine.reset_daily()

                # ── Entry scan (every scan_interval_sec) ──
                if now - last_scan >= self.scan_interval_sec:
                    if msopen >= 10:  # Need ≥10 bars for indicators
                        self.scan_entry()
                    last_scan = now

                # ── Safety + position monitoring ──
                if now - last_monitor >= self.monitor_interval_sec:
                    # Safety monitor
                    if self.safety:
                        safe, alerts = self.safety.check()
                        if not safe:
                            print(f"\n  🚨 SAFETY: Kill switch triggered")
                            self._running = False
                            break

                        if self.provider:
                            current_vix = get_current_vix(self.provider)
                            spike = self.safety.check_vix_spike(current_vix)
                            if spike:
                                print(f"\n  {spike}")

                    # Strategy exit monitoring
                    self.monitor_positions()
                    last_monitor = now

                # ── Approaching close — force check ──
                if ttc <= 20 and self.scalp_engine and self.scalp_engine.open_positions:
                    print(f"\n  ⏱️  {ttc:.0f} min to close — checking time exits...")
                    self.monitor_positions()

                time.sleep(5)

        except Exception as e:
            logger.error(f"Unexpected error in main loop: {e}", exc_info=True)
            print(f"\n  ❌ ERROR: {e}")

        finally:
            self._shutdown()

    # ─── Shutdown ────────────────────────────────────────────────

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

        # Scalp engine summary
        if self.scalp_engine:
            self.scalp_engine.print_status()

        # Send daily summary alert
        if self.safety:
            engine_info = ""
            if self.scalp_engine:
                t = self.scalp_engine.trades_today
                pnl = self.scalp_engine.daily_realized_pnl
                engine_info = f" | {t} trades, ${pnl:+.2f}"
            self.safety.send_daily_summary()
            self.safety.send_alert(
                f"🔴 Trading engine shutting down{engine_info}",
                AlertLevel.INFO,
            )

        # Disconnect
        if self.provider:
            self.provider.disconnect()

        print(f"\n  👋 Done. See you next trading day!\n")


# ─────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="0DTE Multi-Strategy Live Trading Engine"
    )
    parser.add_argument(
        "--tickers", nargs="+", default=["SPY"],
        help="Tickers to trade (default: SPY)",
    )
    parser.add_argument(
        "--account-size", type=float,
        default=float(os.getenv("ACCOUNT_SIZE", "10000")),
        help="Account size in dollars (default: $10,000)",
    )
    parser.add_argument(
        "--no-confirm", action="store_true",
        help="Skip order confirmation prompts",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Simulate without placing real orders",
    )
    parser.add_argument(
        "--kill", action="store_true",
        help="KILL SWITCH: flatten all positions immediately",
    )
    parser.add_argument(
        "--status", action="store_true",
        help="Show current state and exit",
    )
    parser.add_argument(
        "--validate", action="store_true",
        help="Run end-to-end paper validation (1 cycle, then exit)",
    )
    parser.add_argument(
        "--validate-dry", action="store_true",
        help="Validation without placing orders",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()

    # Setup logging
    level = logging.DEBUG if args.verbose else logging.INFO
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
        executor = OrderExecutor(
            ibkr_provider=provider, require_confirmation=False,
        )
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

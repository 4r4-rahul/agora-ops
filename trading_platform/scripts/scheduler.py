"""
Market-hours analysis scheduler.

Runs the full agent pipeline for configured tickers on a recurring schedule
during US equity market hours (M-F, 09:30–16:00 ET). Designed to run as a
long-lived background process alongside the FastAPI server.

Usage:
    # Run the scheduler (blocks until interrupted)
    python -m trading_platform.scripts.scheduler

    # Custom tickers and interval
    python -m trading_platform.scripts.scheduler --tickers SPY QQQ NVDA --interval 60

The scheduler respects:
  - Market open (09:30 ET) / close (16:00 ET)
  - US market holidays (skips days when market is closed)
  - Configurable analysis interval (default: 60 minutes)
  - Min interval between analyses per ticker (prevents hammering the API)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date, datetime, time, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# Load .env from project root before importing settings
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
MARKET_OPEN  = time(9, 30)
MARKET_CLOSE = time(16, 0)

# Entry blackout windows — never enter during these windows
# Opening: first 30 min too volatile, fake moves, wide spreads
# Closing: last 30 min end-of-day games, pinning, wide spreads
_ENTRY_BLACKOUT: list[tuple[time, time]] = [
    (time(9, 30),  time(10, 0)),   # Opening volatility window
    (time(15, 30), time(16, 0)),   # Closing window
]

# Best entry windows — prioritise analysis during these times
_PRIME_WINDOWS: list[tuple[time, time]] = [
    (time(10, 0),  time(11, 30)),  # Post-open: trend established, spreads tight
    (time(13, 0),  time(15, 30)),  # Afternoon: institutional activity resumes
]

# Day-of-week multipliers for position sizing confidence
# 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri
_DOW_MULTIPLIER: dict[int, float] = {
    0: 0.5,   # Monday: gap fills, fade-the-open behaviour, lower confidence
    1: 1.0,   # Tuesday: best directional follow-through
    2: 1.0,   # Wednesday: best day overall
    3: 0.75,  # Thursday: pre-OPEX positioning, afternoon vol spikes
    4: 0.5,   # Friday: OPEX pinning, max pain gravity, time decay accelerates
}

# Approximate US market holidays for 2025-2026 (NYSE schedule)
_HOLIDAYS: frozenset[tuple[int, int]] = frozenset({
    (1, 1),    # New Year's Day
    (1, 20),   # MLK Day 2025
    (2, 17),   # Presidents' Day 2025
    (4, 18),   # Good Friday 2025
    (5, 26),   # Memorial Day 2025
    (6, 19),   # Juneteenth
    (7, 4),    # Independence Day
    (9, 1),    # Labor Day 2025
    (11, 27),  # Thanksgiving 2025
    (12, 25),  # Christmas
})


def _is_market_open(now: datetime) -> bool:
    """Return True if the NYSE is currently open for trading."""
    et = now.astimezone(ET)
    if et.weekday() >= 5:
        return False
    if (et.month, et.day) in _HOLIDAYS:
        return False
    t = et.time()
    return MARKET_OPEN <= t < MARKET_CLOSE


def _is_entry_blackout(now: datetime) -> tuple[bool, str]:
    """Return (True, reason) if we are in an entry blackout window."""
    et = now.astimezone(ET)
    t = et.time()
    for start, end in _ENTRY_BLACKOUT:
        if start <= t < end:
            return True, f"Entry blackout {start.strftime('%H:%M')}–{end.strftime('%H:%M')} ET"
    return False, ""


def _is_prime_entry_window(now: datetime) -> bool:
    """Return True if now is in a prime entry window."""
    et = now.astimezone(ET)
    t = et.time()
    return any(start <= t < end for start, end in _PRIME_WINDOWS)


def get_session_confidence(now: datetime) -> float:
    """
    Return a 0.0–1.0 multiplier for analysis confidence based on
    time of day and day of week. Used to scale conviction scores.
    """
    et = now.astimezone(ET)
    dow_mult = _DOW_MULTIPLIER.get(et.weekday(), 0.75)
    time_mult = 1.0 if _is_prime_entry_window(now) else 0.7
    return round(dow_mult * time_mult, 2)


def _seconds_until_next_open(now: datetime) -> float:
    """Compute seconds until the next market open (9:30 ET on a trading day)."""
    et = now.astimezone(ET)
    candidate = et.replace(hour=9, minute=30, second=0, microsecond=0)

    # If already past 9:30 today, start from tomorrow
    if et.time() >= MARKET_OPEN:
        candidate += timedelta(days=1)

    # Advance past weekends and holidays
    while candidate.weekday() >= 5 or (candidate.month, candidate.day) in _HOLIDAYS:
        candidate += timedelta(days=1)

    return (candidate - et).total_seconds()


class AnalysisScheduler:
    """
    Runs `orchestrator.analyze(ticker)` on an interval during market hours.

    Does not manage agent lifecycle — expects agents to be started externally
    (e.g. by the FastAPI lifespan or a separate startup script).
    """

    def __init__(
        self,
        orchestrator,
        tickers: list[str],
        interval_minutes: int = 60,
        alert_webhook_url: str | None = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._tickers = [t.upper() for t in tickers]
        self._interval = interval_minutes * 60  # seconds
        self._webhook = alert_webhook_url
        self._running = False
        self._last_run: dict[str, datetime] = {}
        self._last_feedback_date: date | None = None  # weekly feedback loop tracker

    async def run(self) -> None:
        """Block until cancelled. Runs analyses on schedule."""
        self._running = True
        logger.info(
            "Scheduler started — tickers=%s interval=%dm",
            self._tickers, self._interval // 60,
        )

        while self._running:
            now = datetime.now(tz=ET)

            if not _is_market_open(now):
                wait = _seconds_until_next_open(now)
                et_open = now + timedelta(seconds=wait)
                logger.info(
                    "Market closed — sleeping %.0fm until %s ET",
                    wait / 60,
                    et_open.strftime("%Y-%m-%d %H:%M"),
                )
                await asyncio.sleep(min(wait, 3600))  # wake up at least hourly
                continue

            # Weekly feedback loop — runs once on Friday after 15:30 ET
            if now.weekday() == 4 and now.time() >= time(15, 30):
                if self._last_feedback_date != now.date():
                    self._last_feedback_date = now.date()
                    asyncio.create_task(self._run_feedback(), name="weekly_feedback")

            # Market is open — run analysis for each ticker that's due
            tasks = []
            for ticker in self._tickers:
                last = self._last_run.get(ticker)
                if last is None or (now - last).total_seconds() >= self._interval:
                    tasks.append(self._analyze(ticker))

            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

            # Sleep until next check (1 minute minimum to avoid tight loops)
            await asyncio.sleep(60)

    async def _analyze(self, ticker: str) -> None:
        now = datetime.now(tz=ET)
        self._last_run[ticker] = now

        # Check entry blackout
        in_blackout, blackout_reason = _is_entry_blackout(now)
        if in_blackout:
            logger.info("Skipping %s analysis — %s", ticker, blackout_reason)
            return

        # Check macro calendar
        from ..services.macro_calendar import get_macro_calendar
        cal = get_macro_calendar()
        can_trade, macro_reason = cal.should_trade(now.date())
        if not can_trade:
            logger.warning("Skipping %s — macro calendar: %s", ticker, macro_reason)
            return

        session_conf = get_session_confidence(now)
        logger.info(
            "Scheduled analysis: %s @ %s ET (session_confidence=%.0f%%, dow=%s, prime=%s)",
            ticker, now.strftime("%H:%M"), session_conf * 100,
            ["Mon","Tue","Wed","Thu","Fri"][now.weekday()],
            _is_prime_entry_window(now),
        )

        try:
            result = await self._orchestrator.analyze(ticker, timeout=120.0)
            decision = result.get("final_decision", "unknown")
            logger.info(
                "Analysis complete: %s → %s (R/R %.1f:1)",
                ticker,
                decision,
                result.get("reward_risk_ratio", 0),
            )

            # Alert on any non-rejected decision worth knowing about
            if self._webhook and decision not in ("REJECTED",):
                from ..services.alerts import alert_recommendation_ready
                await alert_recommendation_ready(
                    self._webhook,
                    ticker=ticker,
                    strategy=result.get("strategy", "unknown"),
                    direction=result.get("direction", "unknown"),
                    reward_risk_ratio=result.get("reward_risk_ratio", 0),
                    max_loss_dollars=result.get("max_loss_dollars", 0),
                    final_decision=decision,
                    session_id=result.get("session_id", ""),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Scheduled analysis failed for %s: %s", ticker, exc)
            if self._webhook:
                from ..services.alerts import alert_pipeline_error
                await alert_pipeline_error(
                    self._webhook,
                    ticker=ticker,
                    error=str(exc),
                    session_id="scheduler",
                )

    async def _run_feedback(self) -> None:
        """Run the weekly performance feedback loop (Friday close)."""
        logger.info("Running weekly performance feedback loop...")
        try:
            from ..agents.performance_feedback import run_feedback_loop
            result = await run_feedback_loop(use_claude=True)
            logger.info(
                "Feedback loop complete — trades=%d win_rate=%.0f%% lessons=%d",
                result.get("total_trades", 0),
                result.get("win_rate", 0) * 100,
                len(result.get("lessons", [])),
            )
        except Exception as exc:
            logger.error("Weekly feedback loop failed: %s", exc)

    def stop(self) -> None:
        self._running = False


# ── Standalone entry point ────────────────────────────────────────────────

async def _main(args: argparse.Namespace) -> None:
    """Bootstrap the full platform and run the scheduler."""
    from trading_platform.core.bus import MessageBus
    from trading_platform.core.config import get_settings
    from trading_platform.core.state import SharedStateStore
    from trading_platform.agents.conviction import ConvictionAgent
    from trading_platform.agents.execution import ExecutionAgent
    from trading_platform.agents.premarket import PreMarketAgent
    from trading_platform.agents.setup_watcher import SetupWatcherAgent
    from trading_platform.agents.universe_screener import UniverseScreenerAgent
    from trading_platform.agents.journal import TradeJournalAgent
    from trading_platform.agents.market_data import MarketDataAgent
    from trading_platform.agents.monitor import MonitorAgent
    from trading_platform.agents.news import NewsCatalystAgent
    from trading_platform.agents.options_strategy import OptionsStrategyAgent
    from trading_platform.agents.orchestrator import OrchestratorAgent
    from trading_platform.agents.regime import RegimeAgent
    from trading_platform.agents.reviewer import ReviewerAgent
    from trading_platform.agents.risk_manager import RiskManagerAgent
    from trading_platform.agents.technical import TechnicalAnalysisAgent

    settings = get_settings()
    tickers = [t.upper() for t in args.tickers] if args.tickers else settings.tier_tickers
    logger.info(
        "Account tier=%s → trading %d tickers: %s",
        settings.account_tier, len(tickers), tickers,
    )

    bus = MessageBus()
    state_store = SharedStateStore()
    kwargs = {"bus": bus, "state_store": state_store, "settings": settings}

    all_agents = [
        PreMarketAgent(**kwargs),
        UniverseScreenerAgent(**kwargs),
        MarketDataAgent(**kwargs),
        RegimeAgent(**kwargs),
        TechnicalAnalysisAgent(**kwargs),
        NewsCatalystAgent(**kwargs),
        ConvictionAgent(**kwargs),
        OptionsStrategyAgent(**kwargs),
        RiskManagerAgent(**kwargs),
        ReviewerAgent(**kwargs),
        ExecutionAgent(**kwargs),
        TradeJournalAgent(**kwargs),
        MonitorAgent(**kwargs, poll_interval_seconds=args.monitor_interval),
    ]

    setup_watcher = SetupWatcherAgent(
        bus=bus,
        state_store=state_store,
        settings=settings,
        poll_interval_seconds=300.0,
    )

    logger.info("Starting %d agents + SetupWatcher...", len(all_agents))
    await asyncio.gather(*[a.start() for a in all_agents])
    await setup_watcher.start()

    orchestrator = OrchestratorAgent(bus=bus, state_store=state_store, settings=settings)
    scheduler = AnalysisScheduler(
        orchestrator=orchestrator,
        tickers=tickers,
        interval_minutes=args.interval,
        alert_webhook_url=settings.alert_webhook_url,
    )

    try:
        await scheduler.run()
    except asyncio.CancelledError:
        logger.info("Scheduler cancelled — shutting down")
    finally:
        await setup_watcher.stop()
        await asyncio.gather(*[a.stop() for a in all_agents], return_exceptions=True)
        logger.info("All agents stopped")


def main() -> None:
    parser = argparse.ArgumentParser(description="Market-hours analysis scheduler")
    parser.add_argument(
        "--tickers", nargs="*",
        help="Ticker symbols to analyse (default: settings.default_tickers)",
    )
    parser.add_argument(
        "--interval", type=int, default=60,
        help="Minutes between analyses per ticker (default: 60)",
    )
    parser.add_argument(
        "--monitor-interval", type=float, default=300.0,
        help="Seconds between position monitor polls (default: 300)",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    try:
        asyncio.run(_main(args))
    except KeyboardInterrupt:
        logger.info("Interrupted")


if __name__ == "__main__":
    main()

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
from datetime import datetime, time, timezone, timedelta
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
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)

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
    # Weekend
    if et.weekday() >= 5:
        return False
    # Holiday (month, day) — approximate
    if (et.month, et.day) in _HOLIDAYS:
        return False
    # Pre/after hours
    t = et.time()
    return MARKET_OPEN <= t < MARKET_CLOSE


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
        logger.info("Scheduled analysis: %s @ %s ET", ticker, now.strftime("%H:%M"))

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

    def stop(self) -> None:
        self._running = False


# ── Standalone entry point ────────────────────────────────────────────────

async def _main(args: argparse.Namespace) -> None:
    """Bootstrap the full platform and run the scheduler."""
    from trading_platform.core.bus import MessageBus
    from trading_platform.core.config import get_settings
    from trading_platform.core.state import SharedStateStore
    from trading_platform.agents.execution import ExecutionAgent
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
    tickers = [t.upper() for t in args.tickers] if args.tickers else settings.default_tickers

    bus = MessageBus()
    state_store = SharedStateStore()
    kwargs = {"bus": bus, "state_store": state_store, "settings": settings}

    all_agents = [
        MarketDataAgent(**kwargs),
        RegimeAgent(**kwargs),
        TechnicalAnalysisAgent(**kwargs),
        NewsCatalystAgent(**kwargs),
        OptionsStrategyAgent(**kwargs),
        RiskManagerAgent(**kwargs),
        ReviewerAgent(**kwargs),
        ExecutionAgent(**kwargs),
        TradeJournalAgent(**kwargs),
        MonitorAgent(**kwargs, poll_interval_seconds=args.monitor_interval),
    ]

    logger.info("Starting %d agents...", len(all_agents))
    await asyncio.gather(*[a.start() for a in all_agents])

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

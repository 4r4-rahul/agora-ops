"""
CLI entrypoint — run a full analysis pipeline from the terminal.

Usage:
    python -m trading_platform.main SPY
    python -m trading_platform.main SPY QQQ AAPL --no-approve
    python -m trading_platform.main --help

Set ANTHROPIC_API_KEY in env or .env file before running.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path


async def run(tickers: list[str], no_approve: bool, timeout: float) -> None:
    # Import here so env/settings load after arg parsing
    from trading_platform.core.bus import MessageBus
    from trading_platform.core.config import get_settings
    from trading_platform.core.state import SharedStateStore
    from trading_platform.agents.orchestrator import OrchestratorAgent
    from trading_platform.agents.market_data import MarketDataAgent
    from trading_platform.agents.regime import RegimeAgent
    from trading_platform.agents.technical import TechnicalAnalysisAgent
    from trading_platform.agents.news import NewsCatalystAgent
    from trading_platform.agents.options_strategy import OptionsStrategyAgent
    from trading_platform.agents.risk_manager import RiskManagerAgent
    from trading_platform.agents.reviewer import ReviewerAgent
    from trading_platform.agents.execution import ExecutionAgent
    from trading_platform.agents.journal import TradeJournalAgent
    from trading_platform.agents.monitor import MonitorAgent

    settings = get_settings()

    if no_approve:
        settings.__dict__["require_human_approval"] = False

    logging.basicConfig(
        level=getattr(logging, settings.log_level),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        stream=sys.stderr,
    )

    bus = MessageBus()
    state_store = SharedStateStore()
    kwargs = {"bus": bus, "state_store": state_store, "settings": settings}

    agents = [
        MarketDataAgent(**kwargs),
        RegimeAgent(**kwargs),
        TechnicalAnalysisAgent(**kwargs),
        NewsCatalystAgent(**kwargs),
        OptionsStrategyAgent(**kwargs),
        RiskManagerAgent(**kwargs),
        ReviewerAgent(**kwargs),
        ExecutionAgent(**kwargs),
        TradeJournalAgent(**kwargs),
        MonitorAgent(**kwargs, poll_interval_seconds=3600.0),
    ]

    for agent in agents:
        await agent.start()

    orchestrator = OrchestratorAgent(bus=bus, state_store=state_store, settings=settings)

    print(f"\nAnalyzing: {', '.join(tickers)}")
    print(f"Mode: {settings.trading_mode} | Account: ${settings.account_size:,.0f}\n")

    try:
        if len(tickers) == 1:
            result = await orchestrator.analyze(tickers[0], timeout=timeout)
            _print_result(result)
        else:
            results = await orchestrator.analyze_batch(tickers, concurrency=3)
            for ticker, result in zip(tickers, results):
                print(f"\n{'─' * 50}")
                _print_result(result)
    finally:
        for agent in agents:
            await agent.stop()


def _print_result(result: dict) -> None:
    if "error" in result and result["error"]:
        print(f"\n  ERROR: {result['error']}")
        return

    decision = result.get("final_decision", "unknown")
    ticker = result.get("ticker", "?")
    strategy = result.get("strategy", "?")
    rr = result.get("reward_risk_ratio", 0)
    max_loss = result.get("max_loss_dollars", 0)

    icons = {
        "APPROVED": "✓",
        "PENDING_APPROVAL": "⏳",
        "REJECTED": "✗",
        "WATCHLIST": "◎",
    }
    icon = icons.get(decision, "?")

    print(f"  {icon} {ticker} {strategy} — {decision}")
    print(f"     R/R: {rr:.1f}:1  |  Max loss: ${max_loss:,.0f}")
    print(f"     Thesis: {result.get('thesis', '')[:120]}...")
    if result.get("rejection_reasons"):
        for r in result["rejection_reasons"]:
            print(f"     Rejection: {r}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Options Trading Platform — multi-agent analysis CLI"
    )
    parser.add_argument(
        "tickers",
        nargs="+",
        help="Ticker symbols to analyze (e.g. SPY QQQ AAPL)",
    )
    parser.add_argument(
        "--no-approve",
        action="store_true",
        help="Skip human approval prompt (paper mode only)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="Pipeline timeout per ticker in seconds (default: 120)",
    )
    parser.add_argument(
        "--env",
        type=str,
        default=".env",
        help="Path to .env file (default: .env in current dir)",
    )
    args = parser.parse_args()

    # Load .env before settings are instantiated
    env_path = Path(args.env)
    if env_path.exists():
        from dotenv import load_dotenv
        load_dotenv(env_path)

    tickers = [t.upper() for t in args.tickers]
    asyncio.run(run(tickers, args.no_approve, args.timeout))


if __name__ == "__main__":
    main()

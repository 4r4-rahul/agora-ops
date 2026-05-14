"""
FastAPI application — mounts all routes and initializes the agent platform.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from ..core.bus import MessageBus
from ..core.config import get_settings
from ..core.state import SharedStateStore
from ..agents.conviction import ConvictionAgent
from ..agents.execution import ExecutionAgent
from ..agents.premarket import PreMarketAgent
from ..agents.setup_watcher import SetupWatcherAgent
from ..agents.universe_screener import UniverseScreenerAgent
from ..agents.journal import TradeJournalAgent
from ..agents.market_data import MarketDataAgent
from ..agents.monitor import MonitorAgent
from ..agents.news import NewsCatalystAgent
from ..agents.options_strategy import OptionsStrategyAgent
from ..agents.orchestrator import OrchestratorAgent
from ..agents.regime import RegimeAgent
from ..agents.reviewer import ReviewerAgent
from ..agents.risk_manager import RiskManagerAgent
from ..agents.technical import TechnicalAnalysisAgent
from .deps import clear_platform, set_platform
from .routes import agents as agents_router
from .routes import analysis, dashboard, trades

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    logger.info("Starting Options Trading Platform (mode=%s)", settings.trading_mode)

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
        MonitorAgent(**kwargs, poll_interval_seconds=60.0),
    ]

    await asyncio.gather(*[agent.start() for agent in all_agents])

    setup_watcher = SetupWatcherAgent(bus=bus, state_store=state_store, settings=settings)
    await setup_watcher.start()

    orchestrator = OrchestratorAgent(bus=bus, state_store=state_store, settings=settings)
    set_platform(bus, state_store, orchestrator, all_agents)

    logger.info(
        "Platform ready — %d agents + SetupWatcher started | http://%s:%d",
        len(all_agents),
        settings.api_host,
        settings.api_port,
    )

    yield

    logger.info("Shutting down platform...")
    await setup_watcher.stop()
    await asyncio.gather(*[agent.stop() for agent in all_agents], return_exceptions=True)
    clear_platform()
    logger.info("Platform stopped")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Options Trading Platform",
        description="Multi-agent options analysis and execution platform",
        version="1.0.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(analysis.router, prefix="/api/v1/analysis", tags=["analysis"])
    app.include_router(trades.router, prefix="/api/v1/trades", tags=["trades"])
    app.include_router(agents_router.router, prefix="/api/v1/agents", tags=["agents"])
    app.include_router(dashboard.router, prefix="/api/v1/dashboard", tags=["dashboard"])

    # Serve static files (dashboard HTML, JS, CSS)
    _static = Path(__file__).parent.parent / "static"
    if _static.exists():
        app.mount("/static", StaticFiles(directory=str(_static)), name="static")

    @app.get("/dashboard", include_in_schema=False)
    async def dashboard_redirect() -> RedirectResponse:
        return RedirectResponse(url="/static/dashboard.html")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "mode": settings.trading_mode}

    return app


app = create_app()

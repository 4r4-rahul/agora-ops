"""
AGORA FastAPI application.

Run:
    uvicorn agora.api.app:app --host 0.0.0.0 --port 8001 --reload

Or via the CLI:
    python -m agora.api.app

Endpoints:
    GET  /agora/positions    open positions + unrealized P&L
    GET  /agora/signals      latest macro / PSI signal state
    GET  /agora/attribution  per-pillar P&L attribution
    GET  /agora/health       kill-switch state + session info
    POST /agora/kill         trip kill switch
    DELETE /agora/kill       reset kill switch
    WS   /agora/ws           live event stream
    GET  /docs               Swagger UI
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from .routes import attach_log_handler, router
from .state import set_session

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    # Attach WebSocket log handler before session starts
    attach_log_handler()

    # Start the AGORA trading session in the background
    from ..session import AgoraSession
    session = AgoraSession()
    set_session(session)

    session_task = asyncio.create_task(session.run())
    logger.info("AGORA session started in background (mode=%s)", session._settings.trading_mode)

    yield  # API is live

    logger.info("Shutting down AGORA session…")
    await session.stop()
    session_task.cancel()
    try:
        await session_task
    except asyncio.CancelledError:
        pass


app = FastAPI(
    title="AGORA Trading Dashboard",
    description="Real-time options trading monitor and control panel",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/docs")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("agora.api.app:app", host="0.0.0.0", port=8001, reload=True)

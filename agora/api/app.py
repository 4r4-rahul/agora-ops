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
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .routes import attach_log_handler, router
from .state import set_session

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    # ib_insync logs harmless IBKR subscription codes at ERROR level.
    # Filter them so they don't drown out real errors.
    class _IBKRNoiseFilter(logging.Filter):
        # 10091 = "market data requires additional subscription" for the UNDERLYING equity that
        # IBKR needs for option greeks — we only subscribe OPRA (options) and use yfinance for
        # equity data, so it's expected and was flooding the log (~17k lines/day, 2026-06-09).
        # Option OPRA pricing is unaffected (reprice/enrich still function); if OPRA itself
        # lapsed it would surface as missing pricing, not this code.
        _HARMLESS = {"10197", "10090", "10089", "10168", "10091",
                     "2104", "2106", "2158", "2103", "2109"}
        def filter(self, record: logging.LogRecord) -> bool:
            msg = record.getMessage()
            return not any(f"Error {c}" in msg or f"Error {c}," in msg
                           for c in self._HARMLESS)

    for _name in ("ib_insync.wrapper", "ib_insync.client"):
        logging.getLogger(_name).addFilter(_IBKRNoiseFilter())

    # yfinance logs 401s at ERROR level for: (a) stale crumb — our code retries+falls
    # back to IV cache, (b) Yahoo Premium endpoints we don't need. Neither is actionable.
    # Filter these specific strings so real yfinance failures still surface.
    class _YFNoiseFilter(logging.Filter):
        _SUPPRESS = frozenset(["Invalid Crumb", "User is unable to access this feature"])
        def filter(self, record: logging.LogRecord) -> bool:
            msg = record.getMessage()
            return not any(s in msg for s in self._SUPPRESS)

    _yf_filter = _YFNoiseFilter()
    for _yf_name in ("yfinance", "yfinance.base", "yfinance.data", "yfinance.utils"):
        logging.getLogger(_yf_name).addFilter(_yf_filter)

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
    try:
        await session.stop()
    except Exception as exc:
        logger.warning("Session stop error (non-fatal): %s", exc)
    session_task.cancel()
    try:
        await session_task
    except (asyncio.CancelledError, Exception):
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

_STATIC_DIR = Path(__file__).parent.parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
async def root():
    return FileResponse(str(_STATIC_DIR / "dashboard.html"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("agora.api.app:app", host="0.0.0.0", port=8001, reload=True)

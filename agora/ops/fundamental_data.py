"""
IBKR Fundamental Data — earnings date lookup via CalendarReport.

Uses reqFundamentalData("CalendarReport") which returns an XML document
listing scheduled earnings, dividends, and splits.  This is more reliable
than yfinance .calendar because the data comes directly from IBKR's
fundamental data provider (Refinitiv) and is not subject to Yahoo Finance
anti-scraping measures or delayed calendar updates.

Connection: opens a short-lived IB connection on clientId=13 (dedicated so
it never collides with trading clients on 1/2 or the persistent SetupWatcher
on 10).  Disconnects immediately after the data is received.

ib_insync event loop isolation
--------------------------------
ib_insync manages its own internal event loop and is not safe to call from
inside uvicorn's asyncio loop — the same issue described in ibkr_bridge.py.
All IBKR calls here are offloaded to _fetch_earnings_sync(), which creates
a brand-new event loop in the calling thread, bypassing the "Future attached
to a different loop" restriction.

Public API
----------
get_next_earnings_ibkr(ticker, host, port) -> Coroutine[Optional[date]]
    Await this from any async context; internally uses asyncio.to_thread.

prefetch_earnings_dates(tickers, ...) -> Coroutine[dict[str, Optional[date]]]
    Batch version with semaphore-limited concurrency.
"""

from __future__ import annotations

import asyncio
import logging
import xml.etree.ElementTree as ET
from datetime import date

logger = logging.getLogger(__name__)

# clientId reserved for fundamental data queries — never overlaps with trading clients.
_FUNDAMENTAL_CLIENT_ID = 13
_CONNECT_TIMEOUT = 10.0   # seconds
_DATA_TIMEOUT = 15.0      # seconds to wait for IBKR to return the XML

# Set to True when IBKR returns error 10358 (no Refinitiv subscription).
# Prevents pointless reconnect attempts for every ticker in the universe.
_ibkr_unavailable: bool = False


# ── Synchronous IBKR worker (runs in a thread with a fresh event loop) ─────────

def _fetch_earnings_sync(ticker: str, host: str, port: int) -> date | None:
    """
    Blocking worker — creates its own asyncio event loop so it can safely use
    ib_insync regardless of which loop the caller is running in.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_fetch_earnings_async(ticker, host, port))
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        loop.close()
        asyncio.set_event_loop(None)


async def _fetch_earnings_async(ticker: str, host: str, port: int) -> date | None:
    """Async IBKR fetcher — must run inside a fresh loop (see _fetch_earnings_sync)."""
    global _ibkr_unavailable

    try:
        from ib_insync import IB, Stock
    except ImportError:
        logger.debug("ib_insync not available — skipping CalendarReport for %s", ticker)
        return None

    ib = IB()
    # Track whether error 10358 (no Refinitiv subscription) fires for this request.
    _got_10358 = False

    def _on_error(reqId, errorCode, errorString, contract):
        nonlocal _got_10358
        if errorCode == 10358:
            _got_10358 = True

    ib.errorEvent += _on_error

    try:
        await asyncio.wait_for(
            ib.connectAsync(host, port, clientId=_FUNDAMENTAL_CLIENT_ID, timeout=_CONNECT_TIMEOUT),
            timeout=_CONNECT_TIMEOUT + 2,
        )
    except Exception as exc:
        logger.debug("IBKR CalendarReport connect failed for %s: %s", ticker, exc)
        return None

    try:
        contract = Stock(ticker, "SMART", "USD")
        qualified = await ib.qualifyContractsAsync(contract)
        if not qualified:
            logger.debug("Could not qualify contract for %s", ticker)
            return None

        try:
            xml_str = await asyncio.wait_for(
                ib.reqFundamentalDataAsync(qualified[0], "CalendarReport"),
                timeout=_DATA_TIMEOUT,
            )
        except TimeoutError:
            logger.debug("IBKR CalendarReport timed out for %s", ticker)
            return None

        if _got_10358:
            _ibkr_unavailable = True
            logger.warning(
                "IBKR CalendarReport unavailable (Error 10358: no Refinitiv subscription). "
                "Falling back to yfinance for all tickers."
            )
            return None

        if not xml_str:
            logger.debug("Empty CalendarReport for %s", ticker)
            return None

        return _parse_next_earnings(xml_str, ticker)

    except Exception as exc:
        logger.debug("IBKR CalendarReport error for %s: %s", ticker, exc)
        return None
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass
        # Give TWS time to release the clientId slot before the next sequential connect.
        await asyncio.sleep(1.5)


# ── XML parser ─────────────────────────────────────────────────────────────────

def _parse_next_earnings(xml_str: str, ticker: str) -> date | None:
    """
    Parse IBKR CalendarReport XML and return the earliest future earnings date.

    Two schemas observed in the wild:
    1. <CalendarEvent Type="Earnings" StartDate="YYYY-MM-DD" EndDate="YYYY-MM-DD"/>
    2. <Event EventType="Earnings" StartDate="YYYY-MM-DD" EndDate="YYYY-MM-DD"/>

    We collect StartDate and EndDate from both, keep only future dates,
    and return the minimum (i.e., the earliest upcoming earnings event).
    """
    today = date.today()
    candidates: list[date] = []

    try:
        # noqa rationale: xml_str is an IBKR CalendarReport from the authenticated broker API (a
        # trusted source, not user input). Python's stdlib ElementTree does not resolve external
        # entities, so the XXE class does not apply here.
        root = ET.fromstring(xml_str)  # noqa: S314
    except ET.ParseError as exc:
        logger.debug("CalendarReport XML parse error for %s: %s", ticker, exc)
        return None

    for elem in root.iter("CalendarEvent"):
        if elem.get("Type", "").lower() != "earnings":
            continue
        for attr in ("StartDate", "EndDate"):
            raw = elem.get(attr, "")
            if raw:
                try:
                    d = date.fromisoformat(raw[:10])
                    if d >= today:
                        candidates.append(d)
                except ValueError:
                    pass

    for elem in root.iter("Event"):
        if elem.get("EventType", "").lower() != "earnings":
            continue
        for attr in ("StartDate", "EndDate"):
            raw = elem.get(attr, "")
            if raw:
                try:
                    d = date.fromisoformat(raw[:10])
                    if d >= today:
                        candidates.append(d)
                except ValueError:
                    pass

    if not candidates:
        logger.debug("No future earnings events in CalendarReport for %s", ticker)
        return None

    result = min(candidates)
    logger.info("IBKR CalendarReport earnings for %s: %s", ticker, result)
    return result


# ── Public async API (safe to call from uvicorn's loop) ───────────────────────

async def get_next_earnings_ibkr(
    ticker: str,
    host: str = "127.0.0.1",
    port: int = 7497,
) -> date | None:
    """
    Fetch the next scheduled earnings date for *ticker* from IBKR CalendarReport.

    Safe to await from any async context including uvicorn — the IBKR call
    runs in a thread with a fresh event loop via asyncio.to_thread().
    Returns None if IBKR is unavailable or the ticker has no upcoming earnings.
    Short-circuits immediately if a prior call determined that this account
    has no Refinitiv subscription (Error 10358).
    """
    if _ibkr_unavailable:
        return None
    return await asyncio.to_thread(_fetch_earnings_sync, ticker, host, port)


async def prefetch_earnings_dates(
    tickers: list[str],
    host: str = "127.0.0.1",
    port: int = 7497,
    concurrency: int = 3,  # kept for API compat; IBKR fetches are always sequential
) -> dict[str, date | None]:
    """
    Batch-fetch earnings dates for multiple tickers.

    IBKR fetches run sequentially (one at a time) regardless of *concurrency*
    because all connections share clientId=13 — parallel connects produce
    Error 326 "client id already in use".

    Short-circuits after the first Error 10358 (no Refinitiv subscription):
    remaining tickers get None immediately without issuing further IBKR calls.
    In that case the caller's yfinance fallback handles each ticker on demand.
    """
    results: dict[str, date | None] = {}
    for t in tickers:
        if _ibkr_unavailable:
            results[t] = None
            continue
        try:
            results[t] = await get_next_earnings_ibkr(t, host=host, port=port)
        except Exception:
            results[t] = None
    return results

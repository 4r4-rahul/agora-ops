"""Chart renderer — generates candlestick charts for Claude Vision input in SwingJudge."""

from __future__ import annotations

import asyncio
import base64
import io
import logging

import matplotlib
matplotlib.use("Agg")  # Non-interactive backend; must be set before any other matplotlib import

import matplotlib.pyplot as plt  # noqa: E402 (after backend set)
import mplfinance as mpf          # noqa: E402
import yfinance as yf             # noqa: E402

logger = logging.getLogger(__name__)


def _render_sync(
    ticker: str,
    period: str,
    sma_periods: tuple[int, int],
) -> str | None:
    """
    Synchronous worker: fetch OHLCV data and render a candlestick chart.

    Returns a base64-encoded PNG string, or None on any failure.
    Runs inside asyncio.to_thread() so it is safe to perform blocking I/O here.
    """
    try:
        # --- 1. Fetch data ---------------------------------------------------
        df = yf.download(ticker, period=period, auto_adjust=True, progress=False)

        if df is None or df.empty:
            logger.debug("chart_renderer: no data returned for %s", ticker)
            return None

        # yfinance may return a MultiIndex when multiple tickers are requested;
        # flatten to a plain single-level column index.
        if isinstance(df.columns, type(df.columns)) and hasattr(df.columns, "levels"):
            # MultiIndex — drop the ticker level
            try:
                df.columns = df.columns.droplevel(1)
            except Exception:
                pass  # already flat; continue

        # Normalise column names to Title Case expected by mplfinance
        df.columns = [c.strip().title() for c in df.columns]

        # Ensure the required columns are present
        required = {"Open", "High", "Low", "Close", "Volume"}
        missing = required - set(df.columns)
        if missing:
            logger.debug(
                "chart_renderer: missing columns %s for %s", missing, ticker
            )
            return None

        # Drop rows with any NaN in OHLCV columns
        df = df[list(required)].dropna()

        # Minimum data guard: need at least max(sma_periods) rows to draw SMAs,
        # but hard floor at 20 as specified.
        min_rows = max(20, max(sma_periods))
        if len(df) < min_rows:
            logger.debug(
                "chart_renderer: insufficient rows (%d < %d) for %s",
                len(df),
                min_rows,
                ticker,
            )
            return None

        # Ensure the index is a DatetimeIndex (mplfinance requirement)
        if not hasattr(df.index, "freq"):
            df.index = df.index.astype("datetime64[ns]")

        # --- 2. Build mplfinance kwargs ---------------------------------------
        buf = io.BytesIO()

        mpf.plot(
            df,
            type="candle",
            style="nightclouds",
            volume=True,
            mav=sma_periods,
            figsize=(10, 6),
            # Suppress axes titles / y-axis labels for minimal chrome
            tight_layout=True,
            show_nontrading=False,
            savefig=dict(fname=buf, dpi=100, bbox_inches="tight"),
        )

        # --- 3. Encode -------------------------------------------------------
        buf.seek(0)
        encoded = base64.b64encode(buf.read()).decode("utf-8")

        logger.debug("chart_renderer: rendered %s (%d bytes)", ticker, len(encoded))
        return encoded

    except Exception as exc:  # noqa: BLE001
        logger.debug("chart_renderer: failed for %s — %s", ticker, exc)
        return None
    finally:
        # Always close any figures left open by mplfinance to prevent memory leaks.
        plt.close("all")


async def render_chart_base64(
    ticker: str,
    period: str = "3mo",
    sma_periods: tuple[int, int] = (20, 50),
) -> str | None:
    """
    Fetch OHLCV data via yfinance and render a candlestick chart.

    The heavy work (network I/O + matplotlib rendering) runs in a thread pool
    via asyncio.to_thread so the event loop is never blocked.

    Args:
        ticker:      Equity ticker symbol, e.g. ``"SPY"``.
        period:      yfinance period string, e.g. ``"3mo"``, ``"6mo"``, ``"1y"``.
        sma_periods: Tuple of two integers for SMA overlays, default ``(20, 50)``.

    Returns:
        Base64-encoded PNG string suitable for an Anthropic image content block,
        or ``None`` if the chart could not be produced for any reason.
    """
    return await asyncio.to_thread(_render_sync, ticker, period, sma_periods)

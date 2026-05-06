"""
YFinanceProvider — async wrapper around yfinance for market data.

Production replacement: swap this for IBKR TWS or Polygon.io.
All yfinance calls are offloaded to a thread pool (yfinance is synchronous).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from functools import partial
from typing import Any

import yfinance as yf

from ...core.models.market import Bar, MarketSnapshot, OptionContract, OptionsChain

logger = logging.getLogger(__name__)


class YFinanceProvider:
    """
    Async market data provider backed by yfinance.

    Thread pool is used for all yf calls to avoid blocking the event loop.
    """

    def __init__(self, executor=None) -> None:
        self._executor = executor  # None = default ThreadPoolExecutor

    async def _run(self, fn, *args, **kwargs):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._executor, partial(fn, *args, **kwargs)
        )

    async def get_snapshot(self, ticker: str) -> MarketSnapshot:
        """
        Fetch current market snapshot for a ticker.

        Includes price, technical indicators, VIX, and IV data.
        """
        try:
            stock_info, history, vix_price, iv_rank = await asyncio.gather(
                self._run(self._fetch_info, ticker),
                self._run(self._fetch_history, ticker),
                self._run(self._fetch_vix),
                self._run(self._estimate_iv_rank, ticker),
            )
        except Exception as exc:
            logger.error("yfinance snapshot failed for %s: %s", ticker, exc)
            raise

        bars_daily = self._to_bars(history)
        recent = bars_daily[-1] if bars_daily else None

        price = stock_info.get("currentPrice") or stock_info.get("regularMarketPrice", 0.0)
        prev_close = stock_info.get("previousClose") or stock_info.get("regularMarketPreviousClose", 0.0)

        # Compute indicators from history
        closes = [b.close for b in bars_daily]
        sma_20 = self._sma(closes, 20)
        sma_50 = self._sma(closes, 50)
        sma_200 = self._sma(closes, 200)
        rsi_14 = self._rsi(closes, 14)
        atr_14 = self._atr(bars_daily, 14)
        hv_30 = self._hv(closes, 30)

        return MarketSnapshot(
            ticker=ticker.upper(),
            timestamp=datetime.utcnow(),
            price=price or (recent.close if recent else 0.0),
            volume=int(stock_info.get("regularMarketVolume", 0)),
            vwap=None,
            day_open=stock_info.get("open") or (recent.open if recent else None),
            day_high=stock_info.get("dayHigh") or stock_info.get("regularMarketDayHigh"),
            day_low=stock_info.get("dayLow") or stock_info.get("regularMarketDayLow"),
            prev_close=prev_close or None,
            atr_14=atr_14,
            rsi_14=rsi_14,
            sma_20=sma_20,
            sma_50=sma_50,
            sma_200=sma_200,
            vix=vix_price,
            iv_rank=iv_rank.get("rank"),
            iv_percentile=iv_rank.get("percentile"),
            hist_vol_30=hv_30,
            bars_daily=bars_daily[-30:],
        )

    async def get_options_chain(
        self, ticker: str, expiration: date | None = None
    ) -> OptionsChain | None:
        """Fetch the options chain for the nearest (or specified) expiration."""
        try:
            result = await self._run(self._fetch_chain, ticker, expiration)
            return result
        except Exception as exc:
            logger.error("options chain fetch failed for %s: %s", ticker, exc)
            return None

    # ── Synchronous fetch helpers (run in thread pool) ─────────────

    def _fetch_info(self, ticker: str) -> dict[str, Any]:
        try:
            return yf.Ticker(ticker).info or {}
        except Exception:
            return {}

    def _fetch_history(self, ticker: str, period: str = "14mo") -> Any:
        # 14 months gives ~280 trading days — enough for SMA200
        df = yf.download(ticker, period=period, progress=False, auto_adjust=True)
        # yfinance ≥0.2.31 returns multi-level columns for single tickers; flatten them
        if hasattr(df, "columns") and hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
            df.columns = df.columns.get_level_values(0)
        return df

    def _fetch_vix(self) -> float | None:
        try:
            vix = yf.Ticker("^VIX")
            info = vix.info
            return info.get("regularMarketPrice") or info.get("currentPrice")
        except Exception:
            return None

    def _estimate_iv_rank(self, ticker: str) -> dict[str, float | None]:
        """
        Estimate IV rank from HV30 as a proxy.
        Production: use IBKR or Polygon for real IV history.
        """
        try:
            hist = yf.download(ticker, period="1y", progress=False, auto_adjust=True)
            if hasattr(hist, "columns") and hasattr(hist.columns, "nlevels") and hist.columns.nlevels > 1:
                hist.columns = hist.columns.get_level_values(0)
            if hist.empty:
                return {"rank": None, "percentile": None}

            closes = hist["Close"].values.tolist()
            if len(closes) < 30:
                return {"rank": None, "percentile": None}

            # Rolling 30-day HV as IV proxy
            hvs = []
            for i in range(30, len(closes)):
                window = closes[i - 30 : i]
                hv = self._hv_from_prices(window)
                if hv:
                    hvs.append(hv)

            if not hvs:
                return {"rank": None, "percentile": None}

            current_hv = hvs[-1]
            min_hv, max_hv = min(hvs), max(hvs)

            iv_rank = (
                (current_hv - min_hv) / (max_hv - min_hv) * 100
                if max_hv > min_hv
                else 50.0
            )
            below = sum(1 for h in hvs if h <= current_hv)
            iv_percentile = below / len(hvs) * 100

            return {"rank": round(iv_rank, 1), "percentile": round(iv_percentile, 1)}
        except Exception as exc:
            logger.debug("IV rank estimation failed for %s: %s", ticker, exc)
            return {"rank": None, "percentile": None}

    def _fetch_chain(
        self, ticker: str, expiration: date | None
    ) -> OptionsChain | None:
        stock = yf.Ticker(ticker)
        exps = stock.options
        if not exps:
            return None

        exp_str: str
        if expiration:
            exp_str = expiration.strftime("%Y-%m-%d")
            if exp_str not in exps:
                # Find nearest
                exp_str = min(exps, key=lambda e: abs(
                    (datetime.strptime(e, "%Y-%m-%d").date() - expiration).days
                ))
        else:
            exp_str = exps[0]  # nearest expiration

        chain = stock.option_chain(exp_str)
        exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()

        info = stock.info or {}
        underlying_price = (
            info.get("regularMarketPrice")
            or info.get("currentPrice")
            or 0.0
        )

        calls = self._parse_contracts(chain.calls, ticker, exp_date, "call")
        puts = self._parse_contracts(chain.puts, ticker, exp_date, "put")

        return OptionsChain(
            ticker=ticker.upper(),
            underlying_price=underlying_price,
            timestamp=datetime.utcnow(),
            expiration=exp_date,
            calls=calls,
            puts=puts,
        )

    # ── Pandas → model helpers ────────────────────────────────────

    def _parse_contracts(
        self, df: Any, ticker: str, expiration: date, option_type: str
    ) -> list[OptionContract]:
        contracts = []
        for _, row in df.iterrows():
            try:
                contracts.append(
                    OptionContract(
                        ticker=ticker,
                        expiration=expiration,
                        strike=float(row.get("strike", 0)),
                        option_type=option_type,
                        bid=float(row.get("bid", 0)),
                        ask=float(row.get("ask", 0)),
                        last=float(row.get("lastPrice", 0)),
                        volume=int(row.get("volume", 0) or 0),
                        open_interest=int(row.get("openInterest", 0) or 0),
                        implied_vol=float(row.get("impliedVolatility", 0) or 0),
                        delta=float(row.get("delta", 0) or 0),
                        gamma=float(row.get("gamma", 0) or 0),
                        theta=float(row.get("theta", 0) or 0),
                        vega=float(row.get("vega", 0) or 0),
                    )
                )
            except Exception:
                continue
        return contracts

    def _to_bars(self, history: Any) -> list[Bar]:
        bars = []
        if history is None or (hasattr(history, "empty") and history.empty):
            return bars
        for ts, row in history.iterrows():
            try:
                # Handle both flat and multi-level column DataFrames
                def _get(col: str) -> float:
                    try:
                        v = row[col] if col in row.index else row.get(col, 0)
                        return float(v) if v is not None else 0.0
                    except Exception:
                        return 0.0

                bars.append(Bar(
                    ts=ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else datetime.utcnow(),
                    open=_get("Open"),
                    high=_get("High"),
                    low=_get("Low"),
                    close=_get("Close"),
                    volume=int(_get("Volume")),
                ))
            except Exception:
                continue
        return bars

    # ── Technical indicator helpers ───────────────────────────────

    @staticmethod
    def _sma(closes: list[float], period: int) -> float | None:
        if len(closes) < period:
            return None
        return sum(closes[-period:]) / period

    @staticmethod
    def _rsi(closes: list[float], period: int = 14) -> float | None:
        if len(closes) < period + 1:
            return None
        changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains = [max(c, 0) for c in changes[-period:]]
        losses = [abs(min(c, 0)) for c in changes[-period:]]
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - 100 / (1 + rs)

    @staticmethod
    def _atr(bars: list[Bar], period: int = 14) -> float | None:
        if len(bars) < period + 1:
            return None
        trs = []
        for i in range(1, len(bars)):
            prev_close = bars[i - 1].close
            tr = max(
                bars[i].high - bars[i].low,
                abs(bars[i].high - prev_close),
                abs(bars[i].low - prev_close),
            )
            trs.append(tr)
        return sum(trs[-period:]) / period

    @staticmethod
    def _hv(closes: list[float], period: int = 30) -> float | None:
        if len(closes) < period + 1:
            return None
        import math
        returns = [
            math.log(closes[i] / closes[i - 1])
            for i in range(1, len(closes))
            if closes[i - 1] > 0 and closes[i] > 0
        ]
        if len(returns) < period:
            return None
        window = returns[-period:]
        mean = sum(window) / len(window)
        variance = sum((r - mean) ** 2 for r in window) / (len(window) - 1)
        return math.sqrt(variance) * math.sqrt(252)

    @staticmethod
    def _hv_from_prices(prices: list[float]) -> float | None:
        import math
        if len(prices) < 2:
            return None
        returns = [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))]
        if not returns:
            return None
        mean = sum(returns) / len(returns)
        var = sum((r - mean) ** 2 for r in returns) / max(1, len(returns) - 1)
        return math.sqrt(var) * math.sqrt(252)

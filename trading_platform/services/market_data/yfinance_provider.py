"""
YFinanceProvider — async wrapper around yfinance for market data.

Production replacement: swap this for IBKR TWS or Polygon.io.
All yfinance calls are offloaded to a thread pool (yfinance is synchronous).
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import date, datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from typing import Any

import yfinance as yf

from ...core.models.market import Bar, MarketSnapshot, OptionContract, OptionsChain

logger = logging.getLogger(__name__)

# yfinance options chain endpoint is not thread-safe (shared crumb/cookie state).
# Serialise all calls that touch tk.options / tk.option_chain() behind this lock.
_YF_OPTIONS_LOCK = threading.Lock()

# Serialise the IBKR ATM-IV fetches (industry-standard upgrade #1): snapshots run concurrently
# across tickers, but the dedicated IBKR clientId can serve only one connect at a time → without
# this lock concurrent fetches collide (Error 326). Once/ticker is cheap, so serialising is fine.
_IBKR_IV_LOCK = threading.Lock()


def _ibkr_atm_iv(ticker: str, expiry_yyyymmdd: str, atm_strike: float) -> float | None:
    """REAL IBKR/OPRA ATM implied vol for the IV-rank input (industry-standard #1). OFF unless
    IV_RANK_USE_IBKR is truthy in the environment. Reuses trading_platform.fetch_chain_quotes on a
    dedicated, serialised clientId with a hard timeout. Returns decimal IV (e.g. 0.25) or None — the
    caller falls back to the yfinance ATM IV, so this can NEVER break the snapshot."""
    import os
    if os.environ.get("IV_RANK_USE_IBKR", "").strip().lower() not in ("1", "true", "yes", "on"):
        return None
    try:
        from trading_platform.services.ibkr_client import fetch_chain_quotes
        host = os.environ.get("IBKR_HOST", "127.0.0.1")
        port = int(os.environ.get("IBKR_PORT", "7497") or 7497)
        cid  = int(os.environ.get("IBKR_IV_CLIENT_ID", "19") or 19)
        mdt  = int(os.environ.get("IBKR_MARKET_DATA_TYPE", "1") or 1)
        with _IBKR_IV_LOCK:
            # ib_insync binds its IB() to the current event loop via get_event_loop(), so we set a
            # fresh loop for the call. RESTORE the prior loop afterward (not None) — in production
            # this runs in a worker thread with no loop (restores None, fine), but if ever called on
            # a thread that already has a loop, nulling it would break that thread's async work.
            try:
                _prev_loop = asyncio.get_event_loop()
            except RuntimeError:
                _prev_loop = None
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                quotes = loop.run_until_complete(asyncio.wait_for(
                    fetch_chain_quotes(
                        ticker=ticker, expiry=expiry_yyyymmdd,
                        call_strikes=[float(atm_strike)], put_strikes=[float(atm_strike)],
                        market_data_type=mdt, host=host, port=port, client_id=cid),
                    timeout=8.0))
            finally:
                loop.close()
                try:
                    asyncio.set_event_loop(_prev_loop)
                except Exception:
                    pass
        for key in ((float(atm_strike), "C"), (float(atm_strike), "P")):
            q = (quotes or {}).get(key)
            if q and float(q.get("iv", 0) or 0) > 0:
                return float(q["iv"])
        return None
    except Exception as exc:
        logger.debug("IBKR ATM IV fetch failed for %s: %s", ticker, exc)
        return None


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
                self._run(self._get_real_iv_rank, ticker),
            )
        except Exception as exc:
            logger.error("yfinance snapshot failed for %s: %s", ticker, exc)
            raise

        bars_daily = self._to_bars(history)
        recent = bars_daily[-1] if bars_daily else None

        price = stock_info.get("currentPrice") or stock_info.get("regularMarketPrice", 0.0)
        prev_close = stock_info.get("previousClose") or stock_info.get("regularMarketPreviousClose", 0.0)

        # Fall back to most recent bar close if info API returns nothing
        if not price and bars_daily:
            price = bars_daily[-1].close

        if not price or price <= 0:
            raise ValueError(
                f"yfinance returned no price data for {ticker} — "
                "possible rate-limit or market closure"
            )

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
            timestamp=datetime.now(timezone.utc).replace(tzinfo=None),
            price=price or (recent.close if recent else 0.0),
            volume=int(stock_info.get("regularMarketVolume") or 0),
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
            fi = yf.Ticker("^VIX").fast_info
            try:
                price = float(fi.last_price or 0)
            except AttributeError:
                price = float(fi.get("lastPrice", 0) or 0)
            return price if price > 0 else None
        except Exception:
            return None

    # IV cache directory — shared across all provider instances
    _IV_CACHE_DIR = Path(".agora/iv_cache")

    def _get_real_iv_rank(self, ticker: str) -> dict[str, float | None]:
        """
        Compute IV rank from REAL ATM implied volatility from the options chain.

        Strategy:
          1. Pull ATM IV from the nearest ≥7 DTE expiry (real market-implied vol).
          2. Cache each day's ATM IV in .agora/iv_cache/{ticker}.json.
          3. IV rank = (current_atm_iv - 52w_min) / (52w_max - 52w_min) * 100.
          4. If live fetch fails (401/stale crumb), falls back to most recent cached IV.
          5. Requires ≥5 cached data points; rank is approximate until ≥20 days.
        """
        try:
            atm_iv = None
            _atm_strike: float | None = None   # captured for the optional IBKR ATM-IV override
            _yf_expiry: str | None = None
            today = date.today()
            # Retry once on 401/stale crumb — yfinance global session can expire mid-session
            for _attempt in range(2):
                with _YF_OPTIONS_LOCK:
                    tk = yf.Ticker(ticker)
                    exps = tk.options
                    if not exps:
                        if _attempt == 0:
                            import time as _t; _t.sleep(1.0)
                            continue
                        break  # fall through to cached fallback

                    # Pick nearest expiry with ≥7 DTE so we get meaningful IV
                    expiry = None
                    for exp in exps:
                        if (date.fromisoformat(exp) - today).days >= 7:
                            expiry = exp
                            break
                    if not expiry:
                        expiry = exps[0]

                    chain = tk.option_chain(expiry)
                    calls, puts = chain.calls, chain.puts
                    if calls.empty or puts.empty:
                        break  # fall through to cached fallback

                    # Current underlying price (use fast_info to avoid extra API hit)
                    spot = float(tk.fast_info.get("lastPrice") or calls["strike"].median())

                    # ATM call IV — strike closest to spot
                    atm_row = calls.iloc[(calls["strike"] - spot).abs().argsort()[:1]]
                    atm_iv = float(atm_row["impliedVolatility"].iloc[0]) if not atm_row.empty else None

                    # Fall back to put ATM if call IV missing/zero
                    if not atm_iv or atm_iv <= 0:
                        atm_row_p = puts.iloc[(puts["strike"] - spot).abs().argsort()[:1]]
                        atm_iv = float(atm_row_p["impliedVolatility"].iloc[0]) if not atm_row_p.empty else None
                    if not atm_row.empty:
                        _atm_strike = float(atm_row["strike"].iloc[0])
                        _yf_expiry = expiry
                break  # success

            # Industry-standard #1: PREFER the real IBKR/OPRA ATM IV over yfinance's (flag-gated;
            # no-op + instant return when IV_RANK_USE_IBKR is off). The 252-day cache + percentile
            # machinery below is unchanged — only the current-IV INPUT becomes broker-grade.
            if atm_iv and _atm_strike and _yf_expiry:
                _ib_iv = _ibkr_atm_iv(ticker, _yf_expiry.replace("-", ""), _atm_strike)
                if _ib_iv and 0.005 <= _ib_iv <= 5.0:
                    atm_iv = _ib_iv

            # Load IV cache — needed whether we got live data or not
            self._IV_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache_path = self._IV_CACHE_DIR / f"{ticker.upper()}.json"
            cache: dict = {"dates": [], "atm_ivs": []}
            if cache_path.exists():
                try:
                    cache = json.loads(cache_path.read_text())
                except Exception:
                    cache = {"dates": [], "atm_ivs": []}

            if atm_iv and 0.005 <= atm_iv <= 5.0:
                # Fresh fetch succeeded — update cache with today's reading.
                # Guard: reject implausible IV (< 0.5% or > 500%) — yfinance glitch protection.
                today_str = today.isoformat()
                if not cache["dates"] or cache["dates"][-1] != today_str:
                    cache["dates"].append(today_str)
                    cache["atm_ivs"].append(round(atm_iv, 6))
                cache["dates"]  = cache["dates"][-252:]
                cache["atm_ivs"] = cache["atm_ivs"][-252:]
                # Also drop any existing bad entries (retroactive cleanup)
                pairs = [(d, v) for d, v in zip(cache["dates"], cache["atm_ivs"]) if 0.005 <= v <= 5.0]
                cache["dates"]  = [p[0] for p in pairs]
                cache["atm_ivs"] = [p[1] for p in pairs]
                cache_path.write_text(json.dumps(cache))
                atm_iv = cache["atm_ivs"][-1]  # use stored (possibly rounded) value
            elif cache.get("atm_ivs"):
                # Live fetch failed — use most recent cached IV (stale by at most 1 day)
                atm_iv = cache["atm_ivs"][-1]
                logger.debug("IV rank for %s: live fetch failed, using cached IV=%.3f", ticker, atm_iv)
            else:
                return {"rank": None, "percentile": None, "atm_iv": None}

            ivs = cache["atm_ivs"]
            if len(ivs) < 3:
                # Need at least 3 data points for a min/max range (rank improves past 20 days)
                logger.debug("IV cache for %s has only %d days — rank unavailable", ticker, len(ivs))
                return {"rank": None, "percentile": None, "atm_iv": round(atm_iv * 100, 1)}

            min_iv, max_iv = min(ivs), max(ivs)
            iv_rank = (
                (atm_iv - min_iv) / (max_iv - min_iv) * 100
                if max_iv > min_iv else 50.0
            )
            iv_rank = max(0.0, min(100.0, iv_rank))   # clamp — live IV can exceed cached max
            below = sum(1 for v in ivs if v <= atm_iv)
            iv_percentile = below / len(ivs) * 100

            return {
                "rank":       round(iv_rank, 1),
                "percentile": round(iv_percentile, 1),
                "atm_iv":     round(atm_iv * 100, 1),  # in % terms
            }

        except Exception as exc:
            logger.debug("Real IV rank failed for %s: %s", ticker, exc)
            return {"rank": None, "percentile": None, "atm_iv": None}

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
            timestamp=datetime.now(timezone.utc).replace(tzinfo=None),
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

                close = _get("Close")
                if not close or close <= 0:
                    continue
                bars.append(Bar(
                    ts=ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else datetime.now(timezone.utc).replace(tzinfo=None),
                    open=_get("Open"),
                    high=_get("High"),
                    low=_get("Low"),
                    close=close,
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

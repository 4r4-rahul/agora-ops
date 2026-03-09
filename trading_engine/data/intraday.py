"""
Intraday Data Fetcher
======================
Pulls 5-minute SPY/SPX bars for Greeks-aware 0DTE backtesting.

Data sources (ranked by coverage):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Source              Cost    Lookback    Bar Size    Notes
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Yahoo Finance       FREE    60 days     1m–90m     Best free option
  Polygon.io Free     FREE    2 yrs       5m+        Delayed 15 min
  Polygon.io Paid     $29/mo  Full hist   1m+        Real-time
  Alpaca Markets      FREE    5+ yrs      1m+        Requires account
  IBKR               ~$10/mo  Full hist   1s+        Best quality
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

This module supports multiple providers with automatic fallback:
  1. Local CSV cache (already downloaded)
  2. IBKR TWS/Gateway (best quality, full history, real options data)
  3. Yahoo Finance (free, last 60 days of 5-min data)
  4. Polygon.io (free tier: delayed, or paid: real-time)
  5. Alpaca (free with account)

Usage:
    from trading_engine.data.intraday import IntradayFetcher

    fetcher = IntradayFetcher()
    bars = fetcher.fetch("SPY", days=30)             # Yahoo Finance (auto)
    bars = fetcher.fetch("SPY", days=365,             # IBKR (full history)
                         provider="ibkr")
    bars = fetcher.fetch("SPY", days=365,             # Polygon (needs key)
                         provider="polygon")
"""

import os
import json
import logging
from datetime import datetime, date, timedelta, timezone
from typing import Optional, List, Dict, Any, Literal
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

_DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data", "intraday"
)

# ─────────────────────────────────────────────────────────────────
# Intraday bar model
# ─────────────────────────────────────────────────────────────────

@dataclass
class IntradayBar:
    """Single 5-minute price bar."""
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int = 0
    vwap: float = 0.0

    @property
    def date(self) -> date:
        return self.timestamp.date()

    @property
    def time_str(self) -> str:
        return self.timestamp.strftime("%H:%M")

    @property
    def minutes_since_open(self) -> float:
        """Minutes since 9:30 AM ET."""
        market_open = self.timestamp.replace(hour=9, minute=30, second=0, microsecond=0)
        if hasattr(self.timestamp, 'tzinfo') and self.timestamp.tzinfo is not None:
            # Convert UTC timestamps to ET (UTC-5 / UTC-4 DST)
            # Yahoo returns UTC, market hours are 14:30-21:00 UTC
            market_open = self.timestamp.replace(hour=14, minute=30, second=0, microsecond=0)
        return (self.timestamp - market_open).total_seconds() / 60

    @property
    def time_to_close_years(self) -> float:
        """Time remaining until 4:00 PM close, in years (for Black-Scholes T)."""
        total_minutes = 390  # 6.5 hours
        elapsed = max(0, self.minutes_since_open)
        remaining = max(1, total_minutes - elapsed)  # At least 1 min to avoid T=0
        return remaining / (252 * 390)  # Convert to annualized


@dataclass
class TradingDay:
    """All 5-min bars for a single trading day."""
    date: date
    bars: List[IntradayBar] = field(default_factory=list)
    ticker: str = "SPY"

    @property
    def open_price(self) -> float:
        return self.bars[0].open if self.bars else 0

    @property
    def close_price(self) -> float:
        return self.bars[-1].close if self.bars else 0

    @property
    def high_price(self) -> float:
        return max(b.high for b in self.bars) if self.bars else 0

    @property
    def low_price(self) -> float:
        return min(b.low for b in self.bars) if self.bars else 0

    @property
    def bar_count(self) -> int:
        return len(self.bars)


# ─────────────────────────────────────────────────────────────────
# Multi-provider intraday fetcher
# ─────────────────────────────────────────────────────────────────

class IntradayFetcher:
    """
    Downloads and caches intraday 5-minute bars from multiple sources.
    Automatically uses the best available provider.
    """

    def __init__(self, data_dir: str = _DATA_DIR):
        self.data_dir = data_dir
        os.makedirs(self.data_dir, exist_ok=True)

    def fetch(self, ticker: str = "SPY", days: int = 60,
              interval: str = "5m",
              provider: str = "auto",
              force: bool = False) -> List[TradingDay]:
        """
        Fetch intraday bars and return as list of TradingDay objects.

        Args:
            ticker:    Symbol (SPY, QQQ, etc.)
            days:      How many calendar days back to fetch
            interval:  Bar size: "1m", "5m", "15m"
            provider:  "yahoo", "polygon", "alpaca", "csv", or "auto"
            force:     Force re-download even if cached

        Returns:
            List of TradingDay, one per trading day, sorted chronologically.
        """
        cache_path = os.path.join(self.data_dir, f"{ticker}_{interval}_{days}d.csv")

        # Check cache first
        if os.path.exists(cache_path) and not force:
            logger.info(f"Loading cached intraday data from {cache_path}")
            return self._load_cache(cache_path, ticker)

        # Determine provider
        if provider == "auto":
            provider = self._pick_provider(days)

        print(f"  📥 Fetching {ticker} {interval} bars ({days} days) via {provider} ...")

        if provider == "ibkr":
            df = self._fetch_ibkr(ticker, days, interval)
        elif provider == "yahoo":
            df = self._fetch_yahoo(ticker, days, interval)
        elif provider == "polygon":
            df = self._fetch_polygon(ticker, days, interval)
        elif provider == "alpaca":
            df = self._fetch_alpaca(ticker, days, interval)
        elif provider == "csv":
            return self._load_tradingview_csvs(ticker)
        else:
            raise ValueError(f"Unknown provider: {provider}")

        if df is None or df.empty:
            raise RuntimeError(f"No data returned from {provider} for {ticker}")

        # Cache
        df.to_csv(cache_path)
        print(f"    ✅ {len(df)} bars cached to {cache_path}")

        return self._df_to_trading_days(df, ticker)

    def _pick_provider(self, days: int) -> str:
        """Auto-select the best available provider."""
        ibkr_available = self._ibkr_available()
        polygon_key = os.getenv("POLYGON_API_KEY")
        alpaca_key = os.getenv("ALPACA_API_KEY")

        # IBKR is always preferred if available (best quality + full history)
        if ibkr_available:
            return "ibkr"
        elif days <= 60:
            return "yahoo"  # Free, no key needed, up to 60 days
        elif polygon_key:
            return "polygon"  # Has Polygon key, can go further back
        elif alpaca_key:
            return "alpaca"
        else:
            print(f"    ⚠️  {days} days requested but only Yahoo (60 days) available.")
            print(f"    Set POLYGON_API_KEY or ALPACA_API_KEY for longer history.")
            print(f"    Or connect IBKR TWS/Gateway for full history + real options data.")
            return "yahoo"

    def _ibkr_available(self) -> bool:
        """Check if IBKR TWS/Gateway is reachable."""
        try:
            import socket
            host = os.getenv("IBKR_HOST", "127.0.0.1")
            port = int(os.getenv("IBKR_PORT", "7497"))  # 7497=TWS paper, 7496=TWS live, 4001=GW paper, 4002=GW live
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1.0)
            result = sock.connect_ex((host, port))
            sock.close()
            return result == 0
        except Exception:
            return False

    # ── IBKR (BEST QUALITY, full history) ──────────────────────────

    def _fetch_ibkr(self, ticker: str, days: int, interval: str) -> pd.DataFrame:
        """
        Fetch historical bars from Interactive Brokers TWS/Gateway.

        IBKR is the gold standard for market data:
        - Full history (decades for stocks)
        - 1-second resolution available
        - VWAP included
        - Real options data (not synthetic)
        - Costs: ~$4.50/mo for US equities bundle, $10/mo minimum activity waiver

        Requires:
        - TWS or IB Gateway running (paper or live)
        - pip install ib_insync
        - IBKR_HOST (default 127.0.0.1)
        - IBKR_PORT (default 7497 for TWS paper, 7496 live, 4001/4002 for Gateway)
        - IBKR_CLIENT_ID (default 10, use different ID from other connections)
        """
        try:
            import ib_insync
        except ImportError:
            raise RuntimeError(
                "ib_insync not installed. Run: pip install ib_insync\n"
                "Then start TWS or IB Gateway and enable API connections."
            )

        host = os.getenv("IBKR_HOST", "127.0.0.1")
        port = int(os.getenv("IBKR_PORT", "7497"))
        client_id = int(os.getenv("IBKR_CLIENT_ID", "10"))

        # Map our interval strings to IBKR barSizeSetting
        bar_size_map = {
            "1s": "1 secs", "5s": "5 secs", "10s": "10 secs", "30s": "30 secs",
            "1m": "1 min", "2m": "2 mins", "5m": "5 mins", "15m": "15 mins",
            "30m": "30 mins", "60m": "1 hour", "1h": "1 hour",
            "1d": "1 day",
        }
        bar_size = bar_size_map.get(interval)
        if not bar_size:
            raise ValueError(
                f"IBKR does not support interval '{interval}'. "
                f"Supported: {list(bar_size_map.keys())}"
            )

        # Map days to IBKR durationStr
        if days <= 1:
            duration = "1 D"
        elif days <= 7:
            duration = f"{days} D"
        elif days <= 365:
            weeks = max(1, days // 7)
            duration = f"{weeks} W"
        else:
            years = max(1, days // 365)
            duration = f"{years} Y"

        ib = ib_insync.IB()
        try:
            print(f"    🔌 Connecting to IBKR at {host}:{port} (client {client_id}) ...")
            ib.connect(host, port, clientId=client_id, timeout=10)

            # Create the stock contract
            contract = ib_insync.Stock(ticker.upper(), "SMART", "USD")
            ib.qualifyContracts(contract)

            print(f"    📊 Requesting {duration} of {bar_size} bars for {ticker} ...")
            bars = ib.reqHistoricalData(
                contract,
                endDateTime="",           # Empty = up to now
                durationStr=duration,
                barSizeSetting=bar_size,
                whatToShow="TRADES",
                useRTH=True,              # Regular trading hours only
                formatDate=1,             # yyyyMMdd HH:mm:ss format
                keepUpToDate=False,
            )

            if not bars:
                raise RuntimeError(f"IBKR returned no bars for {ticker} ({duration}, {bar_size})")

            # Convert to DataFrame
            records = []
            for bar in bars:
                records.append({
                    "timestamp": pd.Timestamp(bar.date),
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                    "vwap": getattr(bar, "average", 0) or 0,
                })

            df = pd.DataFrame(records).set_index("timestamp")

            # Ensure timezone-aware timestamps
            if df.index.tz is None:
                df.index = df.index.tz_localize("US/Eastern")

            print(f"    ✅ IBKR: {len(df)} bars ({df.index[0]} → {df.index[-1]})")
            return df

        finally:
            if ib.isConnected():
                ib.disconnect()

    # ── Yahoo Finance (FREE, last 60 days) ────────────────────────

    def _fetch_yahoo(self, ticker: str, days: int, interval: str) -> pd.DataFrame:
        """Fetch intraday bars from Yahoo Finance."""
        import yfinance as yf

        # Yahoo limits: 1m = 7 days, 2m/5m/15m = 60 days, 60m/90m = 730 days
        max_days = {"1m": 7, "2m": 60, "5m": 60, "15m": 60, "30m": 60,
                    "60m": 730, "90m": 730}
        limit = max_days.get(interval, 60)
        if days > limit:
            print(f"    ⚠️  Yahoo limits {interval} to {limit} days. Fetching {limit}d.")
            days = limit

        df = yf.download(ticker, period=f"{days}d", interval=interval,
                         auto_adjust=True, progress=False)

        if df.empty:
            return df

        # Flatten MultiIndex columns if present
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        # Normalize column names
        df.columns = [c.lower() for c in df.columns]

        # Keep only OHLCV
        keep = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
        df = df[keep]

        print(f"    ✅ Yahoo: {len(df)} bars ({df.index[0]} → {df.index[-1]})")
        return df

    # ── Polygon.io (FREE delayed / PAID real-time) ────────────────

    def _fetch_polygon(self, ticker: str, days: int, interval: str) -> pd.DataFrame:
        """
        Fetch from Polygon.io REST API.
        FREE tier: 5 API calls/min, delayed 15 min, 2 years history.
        PAID ($29/mo): unlimited, real-time, full history.

        Requires: POLYGON_API_KEY environment variable.
        """
        import requests

        api_key = os.getenv("POLYGON_API_KEY")
        if not api_key:
            raise RuntimeError(
                "POLYGON_API_KEY not set. Get a free key at https://polygon.io/dashboard/signup"
            )

        # Map interval to Polygon multiplier/timespan
        interval_map = {
            "1m": (1, "minute"), "5m": (5, "minute"), "15m": (15, "minute"),
            "30m": (30, "minute"), "60m": (1, "hour"),
        }
        multiplier, timespan = interval_map.get(interval, (5, "minute"))

        end_date = date.today()
        start_date = end_date - timedelta(days=days)

        all_bars = []
        current_start = start_date

        while current_start < end_date:
            # Polygon returns max 50,000 results per call
            chunk_end = min(current_start + timedelta(days=30), end_date)

            url = (f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/"
                   f"{multiplier}/{timespan}/{current_start}/{chunk_end}"
                   f"?adjusted=true&sort=asc&limit=50000&apiKey={api_key}")

            resp = requests.get(url, timeout=30)
            data = resp.json()

            if data.get("status") == "OK" and data.get("results"):
                for bar in data["results"]:
                    all_bars.append({
                        "timestamp": pd.Timestamp(bar["t"], unit="ms", tz="UTC"),
                        "open": bar["o"],
                        "high": bar["h"],
                        "low": bar["l"],
                        "close": bar["c"],
                        "volume": bar.get("v", 0),
                        "vwap": bar.get("vw", 0),
                    })

            current_start = chunk_end + timedelta(days=1)

        if not all_bars:
            return pd.DataFrame()

        df = pd.DataFrame(all_bars).set_index("timestamp")
        print(f"    ✅ Polygon: {len(df)} bars ({df.index[0]} → {df.index[-1]})")
        return df

    # ── Alpaca Markets (FREE with account) ────────────────────────

    def _fetch_alpaca(self, ticker: str, days: int, interval: str) -> pd.DataFrame:
        """
        Fetch from Alpaca Markets REST API.
        FREE tier: requires brokerage account (no funding needed).
        5+ years of 1-min bars.

        Requires: ALPACA_API_KEY and ALPACA_SECRET_KEY env vars.
        """
        import requests

        api_key = os.getenv("ALPACA_API_KEY")
        secret_key = os.getenv("ALPACA_SECRET_KEY")
        if not api_key or not secret_key:
            raise RuntimeError(
                "ALPACA_API_KEY / ALPACA_SECRET_KEY not set. "
                "Sign up free at https://app.alpaca.markets/signup"
            )

        # Map interval
        tf_map = {"1m": "1Min", "5m": "5Min", "15m": "15Min",
                  "30m": "30Min", "60m": "1Hour"}
        timeframe = tf_map.get(interval, "5Min")

        end = datetime.now(tz=timezone.utc)
        start = end - timedelta(days=days)

        headers = {
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": secret_key,
        }

        url = (f"https://data.alpaca.markets/v2/stocks/{ticker}/bars"
               f"?timeframe={timeframe}"
               f"&start={start.isoformat()}"
               f"&end={end.isoformat()}"
               f"&limit=10000&adjustment=split&feed=sip")

        all_bars = []
        page_token = None

        while True:
            req_url = url + (f"&page_token={page_token}" if page_token else "")
            resp = requests.get(req_url, headers=headers, timeout=30)
            data = resp.json()

            for bar in data.get("bars", []):
                all_bars.append({
                    "timestamp": pd.Timestamp(bar["t"]),
                    "open": bar["o"],
                    "high": bar["h"],
                    "low": bar["l"],
                    "close": bar["c"],
                    "volume": bar.get("v", 0),
                    "vwap": bar.get("vw", 0),
                })

            page_token = data.get("next_page_token")
            if not page_token:
                break

        if not all_bars:
            return pd.DataFrame()

        df = pd.DataFrame(all_bars).set_index("timestamp")
        print(f"    ✅ Alpaca: {len(df)} bars ({df.index[0]} → {df.index[-1]})")
        return df

    # ── TradingView CSV import ────────────────────────────────────

    def _load_tradingview_csvs(self, ticker: str) -> List[TradingDay]:
        """
        Load intraday data from existing TradingView CSV exports
        (the BATS_SPY, 1.csv files already in the workspace).
        """
        import glob
        base_dir = os.path.dirname(os.path.dirname(self.data_dir))
        pattern = os.path.join(base_dir, f"BATS_{ticker}*.csv")
        files = sorted(glob.glob(pattern))

        if not files:
            raise FileNotFoundError(
                f"No TradingView CSVs found for {ticker}. "
                f"Expected files like 'BATS_{ticker}, 1.csv' in workspace root."
            )

        all_bars = []
        for f in files:
            try:
                df = pd.read_csv(f, header=0)
                # TradingView format: first column is unix timestamp
                first_col = df.columns[0]
                for _, row in df.iterrows():
                    ts = datetime.fromtimestamp(int(row[first_col]))
                    all_bars.append(IntradayBar(
                        timestamp=ts,
                        open=float(row.iloc[1]),
                        high=float(row.iloc[2]),
                        low=float(row.iloc[3]),
                        close=float(row.iloc[4]),
                        volume=int(row.iloc[13]) if len(row) > 13 else 0,
                    ))
            except Exception as e:
                logger.warning(f"Failed to parse {f}: {e}")
                continue

        print(f"    ✅ TradingView CSVs: {len(all_bars)} bars from {len(files)} files")

        # Group into trading days
        days_dict: Dict[date, List[IntradayBar]] = {}
        for bar in sorted(all_bars, key=lambda b: b.timestamp):
            d = bar.date
            if d not in days_dict:
                days_dict[d] = []
            days_dict[d].append(bar)

        return [TradingDay(date=d, bars=bars, ticker=ticker)
                for d, bars in sorted(days_dict.items())]

    # ── Cache helpers ──────────────────────────────────────────────

    def _load_cache(self, path: str, ticker: str) -> List[TradingDay]:
        """Load cached CSV back into TradingDay objects."""
        df = pd.read_csv(path, index_col=0, parse_dates=True)
        df.columns = [c.lower() for c in df.columns]
        print(f"  ✅ Loaded {len(df)} cached bars from {path}")
        return self._df_to_trading_days(df, ticker)

    def _df_to_trading_days(self, df: pd.DataFrame, ticker: str) -> List[TradingDay]:
        """Convert a DataFrame of intraday bars into TradingDay objects."""
        df.columns = [c.lower() for c in df.columns]

        days_dict: Dict[date, List[IntradayBar]] = {}

        for ts, row in df.iterrows():
            if hasattr(ts, 'date'):
                d = ts.date() if callable(ts.date) else ts.date
            else:
                d = pd.Timestamp(ts).date()

            bar = IntradayBar(
                timestamp=pd.Timestamp(ts).to_pydatetime(),
                open=float(row.get("open", 0)),
                high=float(row.get("high", 0)),
                low=float(row.get("low", 0)),
                close=float(row.get("close", 0)),
                volume=int(row.get("volume", 0)),
                vwap=float(row.get("vwap", 0)) if "vwap" in row else 0,
            )
            if d not in days_dict:
                days_dict[d] = []
            days_dict[d].append(bar)

        trading_days = [TradingDay(date=d, bars=bars, ticker=ticker)
                        for d, bars in sorted(days_dict.items())
                        if len(bars) >= 10]  # Filter out partial days

        print(f"  📊 {len(trading_days)} complete trading days, "
              f"~{sum(td.bar_count for td in trading_days) / max(len(trading_days), 1):.0f} bars/day")

        return trading_days

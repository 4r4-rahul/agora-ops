"""
Free Historical Market Data Fetcher
=====================================
Pulls SPX, VIX, SPY, QQQ, IWM from Yahoo Finance + CBOE.
Builds MarketSnapshot objects for every trading day so the engine
can be backtested without a broker connection.

Data sources (all free, no API key):
  • Yahoo Finance (yfinance)  — OHLCV for SPY, QQQ, IWM, ^VIX, ^GSPC
  • CBOE                      — VIX term structure CSV (daily)
  • FRED (via yfinance)       — Risk-free rate proxy (^IRX = 13-week T-bill)

Usage:
    from trading_engine.data.fetcher import HistoricalDataFetcher
    fetcher = HistoricalDataFetcher()
    fetcher.download(start="2024-01-01", end="2025-12-31")
    snapshots = fetcher.build_snapshots()   # list[MarketSnapshot]
"""

import os
import json
import logging
from datetime import datetime, date, timedelta
from typing import Optional, List, Dict, Any, Tuple

import numpy as np
import pandas as pd
import yfinance as yf

from ..models import MarketSnapshot
from ..black_scholes import expected_move

logger = logging.getLogger(__name__)

# Where we cache downloaded data
_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data")


class HistoricalDataFetcher:
    """
    Downloads and caches free historical market data.
    Converts it into MarketSnapshot objects that every engine module understands.
    """

    def __init__(self, data_dir: str = _DATA_DIR):
        self.data_dir = data_dir
        os.makedirs(self.data_dir, exist_ok=True)

        # Raw dataframes after download
        self._prices: Optional[pd.DataFrame] = None   # OHLCV for all tickers
        self._vix: Optional[pd.DataFrame] = None       # ^VIX daily
        self._irx: Optional[pd.DataFrame] = None       # 13-week T-bill rate

    # ─────────────────────────────────────────────────────────────
    # 1.  Download
    # ─────────────────────────────────────────────────────────────

    def download(self, start: str = "2023-01-01",
                 end: Optional[str] = None,
                 force: bool = False) -> "HistoricalDataFetcher":
        """
        Download historical data from Yahoo Finance.

        Args:
            start: Start date (YYYY-MM-DD)
            end:   End date, defaults to today
            force: Re-download even if cache exists
        """
        end = end or date.today().isoformat()
        cache_path = os.path.join(self.data_dir, "historical_prices.csv")

        if os.path.exists(cache_path) and not force:
            logger.info(f"Loading cached data from {cache_path}")
            self._prices = pd.read_csv(cache_path, index_col=0, parse_dates=True, header=[0, 1])
            vix_cache = os.path.join(self.data_dir, "historical_vix.csv")
            if os.path.exists(vix_cache):
                self._vix = pd.read_csv(vix_cache, index_col=0, parse_dates=True)
            irx_cache = os.path.join(self.data_dir, "historical_irx.csv")
            if os.path.exists(irx_cache):
                self._irx = pd.read_csv(irx_cache, index_col=0, parse_dates=True)
            print(f"  ✅ Loaded cached data: {len(self._prices)} rows ({self._prices.index[0].date()} → {self._prices.index[-1].date()})")
            return self

        print(f"  📥 Downloading historical data {start} → {end} ...")

        # --- Prices: SPY, QQQ, IWM, SPX (^GSPC) ---
        tickers = ["SPY", "QQQ", "IWM", "^GSPC"]
        print(f"    Fetching {', '.join(tickers)} ...")
        prices = yf.download(tickers, start=start, end=end, auto_adjust=True, progress=False)
        # yf.download returns MultiIndex columns (Price, Ticker)
        # Flatten: keep Close, High, Low, Open, Volume for each
        self._prices = prices
        self._prices.to_csv(cache_path)
        print(f"    ✅ Prices: {len(self._prices)} trading days")

        # --- VIX ---
        print("    Fetching ^VIX ...")
        vix = yf.download("^VIX", start=start, end=end, auto_adjust=True, progress=False)
        self._vix = vix
        self._vix.to_csv(os.path.join(self.data_dir, "historical_vix.csv"))
        print(f"    ✅ VIX: {len(self._vix)} days")

        # --- Risk-free rate (13-week T-bill) ---
        print("    Fetching ^IRX (risk-free rate proxy) ...")
        try:
            irx = yf.download("^IRX", start=start, end=end, auto_adjust=True, progress=False)
            self._irx = irx
            self._irx.to_csv(os.path.join(self.data_dir, "historical_irx.csv"))
            print(f"    ✅ IRX: {len(self._irx)} days")
        except Exception:
            self._irx = None
            print("    ⚠️  IRX not available, using 4.5% default")

        print(f"\n  ✅ Download complete. Cached in {self.data_dir}/")
        return self

    # ─────────────────────────────────────────────────────────────
    # 2.  Build MarketSnapshots
    # ─────────────────────────────────────────────────────────────

    def build_snapshots(self) -> List[MarketSnapshot]:
        """
        Convert downloaded data into a list of MarketSnapshot objects,
        one per trading day, ready to feed into the engine.
        """
        if self._prices is None:
            raise RuntimeError("No data loaded. Call .download() first.")

        snapshots: List[MarketSnapshot] = []
        prices = self._prices
        vix_df = self._vix
        irx_df = self._irx

        # Get close prices for each ticker
        # Handle both MultiIndex (multiple tickers) and single-ticker formats
        try:
            spy_close = prices["Close"]["SPY"]
            qqq_close = prices["Close"]["QQQ"]
            iwm_close = prices["Close"]["IWM"]
            spx_close = prices["Close"]["^GSPC"]
            spy_high = prices["High"]["SPY"]
            spy_low = prices["Low"]["SPY"]
            spy_volume = prices["Volume"]["SPY"]
            spx_high = prices["High"]["^GSPC"]
            spx_low = prices["Low"]["^GSPC"]
            spx_open = prices["Open"]["^GSPC"]
        except (KeyError, TypeError):
            raise RuntimeError(
                "Price data format unexpected. Try re-downloading with force=True."
            )

        vix_close = vix_df["Close"] if vix_df is not None else None

        # Realized vol (20-day rolling)
        spx_returns = spx_close.pct_change()
        rv_20d = spx_returns.rolling(20).std() * np.sqrt(252)

        dates = spx_close.dropna().index

        for i, dt in enumerate(dates):
            if i < 1:
                continue  # Need at least 1 prior day

            try:
                spx_px = float(spx_close.loc[dt])
                spy_px = float(spy_close.loc[dt])
                qqq_px = float(qqq_close.loc[dt])
                iwm_px = float(iwm_close.loc[dt])
            except (KeyError, TypeError):
                continue

            # VIX
            vix_val = 18.0
            vix_1d_chg = 0.0
            if vix_close is not None:
                try:
                    vix_val = float(vix_close.loc[dt].iloc[0]) if hasattr(vix_close.loc[dt], 'iloc') else float(vix_close.loc[dt])
                except (KeyError, TypeError, IndexError):
                    pass
                if i >= 1:
                    prev_dt = dates[i - 1]
                    try:
                        prev_vix = float(vix_close.loc[prev_dt].iloc[0]) if hasattr(vix_close.loc[prev_dt], 'iloc') else float(vix_close.loc[prev_dt])
                        vix_1d_chg = vix_val - prev_vix
                    except (KeyError, TypeError, IndexError):
                        pass

            # Previous day
            prev_dt = dates[i - 1]
            prev_spx = float(spx_close.loc[prev_dt])
            prev_high = float(spx_high.loc[prev_dt])
            prev_low = float(spx_low.loc[prev_dt])

            # Actual intraday OHLC for today
            try:
                day_open_val = float(spx_open.loc[dt])
                day_high_val = float(spx_high.loc[dt])
                day_low_val = float(spx_low.loc[dt])
            except (KeyError, TypeError):
                day_open_val = spx_px
                day_high_val = spx_px
                day_low_val = spx_px

            # Overnight change = open - prev close
            try:
                day_open = float(spx_open.loc[dt])
                overnight_chg = day_open - prev_spx
            except (KeyError, TypeError):
                overnight_chg = spx_px - prev_spx

            # Realized vol
            rv = 0.15
            try:
                rv_val = rv_20d.loc[dt]
                if hasattr(rv_val, 'iloc'):
                    rv = float(rv_val.iloc[0])
                else:
                    rv = float(rv_val)
                if np.isnan(rv):
                    rv = 0.15
            except (KeyError, TypeError):
                pass

            # Risk-free rate
            r = 0.045
            if irx_df is not None:
                try:
                    irx_val = irx_df["Close"].loc[dt]
                    if hasattr(irx_val, 'iloc'):
                        r = float(irx_val.iloc[0]) / 100
                    else:
                        r = float(irx_val) / 100
                    if np.isnan(r):
                        r = 0.045
                except (KeyError, TypeError):
                    pass

            # IV approximation: VIX / 100
            iv = vix_val / 100

            # VIX term structure estimate — simple heuristic:
            # contango when VIX < 20, backwardation when VIX > 25
            if vix_val < 20:
                vix_ts = "contango"
                vf_front = vix_val * 1.03
                vf_second = vix_val * 1.08
            elif vix_val < 25:
                vix_ts = "contango"
                vf_front = vix_val * 1.01
                vf_second = vix_val * 1.04
            else:
                vix_ts = "backwardation"
                vf_front = vix_val * 0.97
                vf_second = vix_val * 0.93

            # IV Rank approximation (VIX percentile over trailing 252 days)
            iv_rank = 50.0
            if i >= 252 and vix_close is not None:
                try:
                    lookback = vix_close.iloc[max(0, i - 252):i]
                    vals = lookback.values.flatten()
                    vals = vals[~np.isnan(vals)]
                    if len(vals) > 10:
                        iv_rank = float(np.sum(vals < vix_val) / len(vals) * 100)
                except Exception:
                    pass

            # Put/call ratio — rough approximation based on VIX level
            pcr = 0.70 + (vix_val - 15) * 0.02  # Higher VIX = higher put/call
            pcr = max(0.5, min(1.5, pcr))

            # Build snapshot
            snap = MarketSnapshot(
                timestamp=datetime.combine(dt.date() if hasattr(dt, 'date') else dt, datetime.min.time()),
                spx_price=spx_px,
                spy_price=spy_px,
                qqq_price=qqq_px,
                iwm_price=iwm_px,
                vix_level=vix_val,
                vix_1d_change=round(vix_1d_chg, 2),
                vix_term_structure=vix_ts,
                vix_futures_front=round(vf_front, 2),
                vix_futures_second=round(vf_second, 2),
                spx_futures_price=spx_px,  # EOD approx
                spx_futures_overnight_change=round(overnight_chg, 2),
                globex_high=round(max(spx_px, prev_high) + 5, 2),
                globex_low=round(min(spx_px, prev_low) - 5, 2),
                iv_rank=round(iv_rank, 1),
                iv_percentile=round(iv_rank, 1),
                realized_vol_20d=round(rv, 4),
                implied_vol_30d=round(iv, 4),
                advance_decline_ratio=1.2 if spx_px > prev_spx else 0.8,
                put_call_ratio=round(pcr, 2),
                atm_straddle_price=0,  # Will be calculated
                expected_move_1d=0,    # Will be calculated
                expected_move_1w=0,
                expected_move_1m=0,
                prev_close=prev_spx,
                prev_high=prev_high,
                prev_low=prev_low,
                day_open=day_open_val,
                day_high=day_high_val,
                day_low=day_low_val,
            )

            # Calculate expected moves
            snap.expected_move_1d = round(expected_move(spx_px, iv, 1 / 252), 2)
            snap.expected_move_1w = round(expected_move(spx_px, iv, 5 / 252), 2)
            snap.expected_move_1m = round(expected_move(spx_px, iv, 21 / 252), 2)
            snap.atm_straddle_price = round(snap.expected_move_1d / 0.85, 2)

            snapshots.append(snap)

        print(f"  ✅ Built {len(snapshots)} MarketSnapshots ({snapshots[0].timestamp.date()} → {snapshots[-1].timestamp.date()})")
        return snapshots

    # ─────────────────────────────────────────────────────────────
    # 3.  Convenience: save / load snapshots
    # ─────────────────────────────────────────────────────────────

    def save_snapshots(self, snapshots: List[MarketSnapshot], path: Optional[str] = None):
        """Save snapshots to JSON for fast reload."""
        path = path or os.path.join(self.data_dir, "snapshots.json")
        data = []
        for s in snapshots:
            data.append({
                "date": s.timestamp.date().isoformat(),
                "spx": s.spx_price, "spy": s.spy_price,
                "qqq": s.qqq_price, "iwm": s.iwm_price,
                "vix": s.vix_level, "vix_1d": s.vix_1d_change,
                "vix_ts": s.vix_term_structure,
                "vf1": s.vix_futures_front, "vf2": s.vix_futures_second,
                "futures": s.spx_futures_price,
                "overnight": s.spx_futures_overnight_change,
                "globex_h": s.globex_high, "globex_l": s.globex_low,
                "iv_rank": s.iv_rank, "iv_pct": s.iv_percentile,
                "rv20": s.realized_vol_20d, "iv30": s.implied_vol_30d,
                "ad_ratio": s.advance_decline_ratio,
                "pcr": s.put_call_ratio,
                "em1d": s.expected_move_1d, "em1w": s.expected_move_1w,
                "em1m": s.expected_move_1m, "straddle": s.atm_straddle_price,
                "prev_c": s.prev_close, "prev_h": s.prev_high, "prev_l": s.prev_low,
                "day_o": s.day_open, "day_h": s.day_high, "day_l": s.day_low,
            })
        with open(path, "w") as f:
            json.dump(data, f)
        print(f"  💾 Saved {len(data)} snapshots to {path}")

    def load_snapshots(self, path: Optional[str] = None) -> List[MarketSnapshot]:
        """Load snapshots from JSON."""
        path = path or os.path.join(self.data_dir, "snapshots.json")
        with open(path) as f:
            data = json.load(f)

        snapshots = []
        for d in data:
            snap = MarketSnapshot(
                timestamp=datetime.fromisoformat(d["date"]),
                spx_price=d["spx"], spy_price=d["spy"],
                qqq_price=d["qqq"], iwm_price=d["iwm"],
                vix_level=d["vix"], vix_1d_change=d["vix_1d"],
                vix_term_structure=d["vix_ts"],
                vix_futures_front=d["vf1"], vix_futures_second=d["vf2"],
                spx_futures_price=d["futures"],
                spx_futures_overnight_change=d["overnight"],
                globex_high=d["globex_h"], globex_low=d["globex_l"],
                iv_rank=d["iv_rank"], iv_percentile=d["iv_pct"],
                realized_vol_20d=d["rv20"], implied_vol_30d=d["iv30"],
                advance_decline_ratio=d["ad_ratio"],
                put_call_ratio=d["pcr"],
                expected_move_1d=d["em1d"], expected_move_1w=d["em1w"],
                expected_move_1m=d["em1m"], atm_straddle_price=d["straddle"],
                prev_close=d["prev_c"], prev_high=d["prev_h"], prev_low=d["prev_l"],
                day_open=d.get("day_o", 0), day_high=d.get("day_h", 0),
                day_low=d.get("day_l", 0),
            )
            snapshots.append(snap)

        print(f"  ✅ Loaded {len(snapshots)} snapshots from cache")
        return snapshots

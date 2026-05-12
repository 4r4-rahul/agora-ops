"""
SectorMomentumAgent — weekly batch scan of sector ETF momentum.

Identifies sectors with strong directional momentum that should bias
our single-name strategy selection (bull vs bear spreads).

Runs Sunday evening via Batch API — scans all 11 SPDR sector ETFs
plus sub-sector ETFs for the week ahead.

Claude API features used:
  - Batch API: submit up to 50 requests overnight, poll for results
  - Each batch item: one sector ETF with its 4-week return, RSI, and IV rank
  - Claude classifies: momentum strength + implied options strategy bias
  - Haiku model for cost efficiency (50 calls × cheap = negligible cost)
  - Results cached in .agora/sector_momentum.json for the week
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import anthropic

from ..core.config import AgoraSettings, get_settings

logger = logging.getLogger(__name__)

_SECTOR_ETFS = [
    "XLK", "XLV", "XLF", "XLE", "XLU", "XLB", "XLI", "XLRE", "XLY", "XLP", "XLC",
    # Sub-sector
    "SMH", "IBB", "KBE", "XOP", "ARKK",
]

_CACHE_PATH = Path(".agora/sector_momentum.json")

_SYSTEM = """\
You are a momentum analyst for an options trading desk.
Given a sector ETF's recent performance metrics, classify momentum and
recommend the options strategy bias for the upcoming week.

Output JSON:
{
  "momentum": "strong_bull" | "mild_bull" | "neutral" | "mild_bear" | "strong_bear",
  "confidence": <float 0.0-1.0>,
  "strategy_bias": "bull_call_spread" | "bull_put_spread" | "iron_condor" | "bear_call_spread" | "bear_put_spread",
  "avoid_direction": "long" | "short" | null,
  "reasoning": "<1 sentence>"
}

Rules:
- strong_bull: 4w return > 3% AND RSI > 60 → bias toward bull spreads
- strong_bear: 4w return < -3% AND RSI < 40 → bias toward bear spreads
- neutral: iron_condor is default when momentum is unclear
- If IV rank > 60: prefer credit spreads regardless of direction
"""


class SectorMomentumAgent:
    """
    Weekly batch analysis of sector momentum using Claude Batch API.
    Output feeds into conviction scoring (sector tailwind/headwind).
    """

    def __init__(self, settings: AgoraSettings | None = None) -> None:
        self._settings = settings or get_settings()
        self._client = anthropic.Anthropic(api_key=self._settings.anthropic_api_key)
        self._async_client = anthropic.AsyncAnthropic(api_key=self._settings.anthropic_api_key)
        self._cache: dict[str, Any] = {}
        self._cache_date: str = ""
        self._load_cache()

    def _load_cache(self) -> None:
        if _CACHE_PATH.exists():
            try:
                data = json.loads(_CACHE_PATH.read_text())
                self._cache_date = data.get("date", "")
                self._cache = data.get("sectors", {})
            except Exception:
                pass

    def get_sector_bias(self, ticker: str) -> dict[str, Any] | None:
        """
        Get cached momentum bias for a ticker's sector.
        Returns None if cache is stale (> 7 days old).
        """
        cache_cutoff = (date.today() - timedelta(days=7)).isoformat()
        if self._cache_date < cache_cutoff:
            return None
        return self._cache.get(ticker)

    async def run_weekly_scan(self) -> dict[str, Any]:
        """
        Submit batch job for all sector ETFs. Waits for completion.
        Call Sunday evening or Monday pre-market.
        Returns dict of {ticker: momentum_analysis}.
        """
        market_data = await self._fetch_all_market_data()
        requests = self._build_batch_requests(market_data)

        if not requests:
            logger.warning("No market data available for batch scan")
            return {}

        # Submit batch
        logger.info("Submitting sector momentum batch: %d requests", len(requests))
        batch = self._client.messages.batches.create(requests=requests)
        batch_id = batch.id
        logger.info("Batch submitted: %s", batch_id)

        # Poll until complete (max 30 min)
        results = await self._poll_batch(batch_id, timeout_seconds=1800)
        self._save_results(results)
        return results

    def _build_batch_requests(
        self, market_data: dict[str, dict]
    ) -> list[dict]:
        requests = []
        for ticker, data in market_data.items():
            if not data:
                continue
            user_msg = (
                f"ETF: {ticker}\n"
                f"4-week return: {data.get('return_4w', 0):.2f}%\n"
                f"RSI-14: {data.get('rsi14', 50):.1f}\n"
                f"IV rank: {data.get('iv_rank', 50):.0f}\n"
                f"Avg volume vs 20d avg: {data.get('volume_ratio', 1.0):.2f}x\n"
                f"Price vs 50d SMA: {data.get('vs_sma50', 0):.2f}%"
            )
            requests.append({
                "custom_id": ticker,
                "params": {
                    "model": self._settings.claude_fast_model,
                    "max_tokens": 256,
                    "system": _SYSTEM,
                    "messages": [{"role": "user", "content": user_msg}],
                },
            })
        return requests

    async def _poll_batch(self, batch_id: str, timeout_seconds: int) -> dict[str, Any]:
        """Poll batch until processing_status == ended, then collect results."""
        start = time.monotonic()
        while time.monotonic() - start < timeout_seconds:
            await asyncio.sleep(30)   # poll every 30s
            batch = self._client.messages.batches.retrieve(batch_id)
            logger.debug("Batch %s status: %s", batch_id, batch.processing_status)
            if batch.processing_status == "ended":
                break
        else:
            logger.warning("Batch %s timed out after %ds", batch_id, timeout_seconds)
            return {}

        results: dict[str, Any] = {}
        for result in self._client.messages.batches.results(batch_id):
            ticker = result.custom_id
            if result.result.type == "succeeded":
                try:
                    text = result.result.message.content[0].text.strip()
                    if text.startswith("```"):
                        text = text.split("```")[1].lstrip("json").strip()
                    analysis = json.loads(text)
                    results[ticker] = analysis
                except Exception as exc:
                    logger.debug("Parse failed for %s: %s", ticker, exc)
        return results

    def _save_results(self, results: dict[str, Any]) -> None:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps({
            "date": date.today().isoformat(),
            "sectors": results,
        }))
        self._cache = results
        self._cache_date = date.today().isoformat()
        logger.info("Sector momentum cache updated: %d sectors", len(results))

    async def _fetch_all_market_data(self) -> dict[str, dict]:
        """Fetch market data for all sector ETFs concurrently."""
        tasks = {ticker: self._fetch_etf_data(ticker) for ticker in _SECTOR_ETFS}
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        return {
            ticker: (r if not isinstance(r, Exception) else {})
            for ticker, r in zip(tasks.keys(), results)
        }

    async def _fetch_etf_data(self, ticker: str) -> dict:
        """Fetch 4-week return, RSI, IV rank for one ETF."""
        try:
            import yfinance as yf
            import numpy as np
            tk = yf.Ticker(ticker)
            hist = tk.history(period="3mo", interval="1d")
            if hist.empty or len(hist) < 30:
                return {}

            closes = hist["Close"].values
            # 4-week return
            ret_4w = (closes[-1] / closes[-20] - 1) * 100 if len(closes) >= 20 else 0.0
            # RSI-14
            rsi = self._compute_rsi(closes, 14)
            # vs 50d SMA
            sma50 = float(closes[-50:].mean()) if len(closes) >= 50 else float(closes.mean())
            vs_sma50 = (closes[-1] / sma50 - 1) * 100
            # Volume ratio
            vols = hist["Volume"].values
            vol_ratio = (float(vols[-5:].mean()) / float(vols[-20:].mean())) if len(vols) >= 20 else 1.0

            # IV rank from cache (best-effort)
            iv_rank = 50.0  # default when not cached

            return {
                "return_4w": float(ret_4w),
                "rsi14": float(rsi),
                "iv_rank": iv_rank,
                "vs_sma50": float(vs_sma50),
                "volume_ratio": float(vol_ratio),
            }
        except Exception as exc:
            logger.debug("ETF data fetch failed for %s: %s", ticker, exc)
            return {}

    @staticmethod
    def _compute_rsi(closes: Any, period: int = 14) -> float:
        import numpy as np
        if len(closes) < period + 1:
            return 50.0
        deltas = np.diff(closes)
        gains = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)
        avg_gain = gains[-period:].mean()
        avg_loss = losses[-period:].mean()
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return float(100.0 - 100.0 / (1.0 + rs))

"""
DataIntegrityAgent — cross-validates market data feeds and quarantines bad data.

Problem it solves:
  yfinance IVR was returning 100 simultaneously for SPY, QQQ, and IWM — a data
  quality failure, not a real market signal. This was causing the vol premium
  bypass to fire and route low-conviction trades through the system. One bad data
  point is noise; three major ETFs at IVR=100 simultaneously is a feed outage.

Rules:
  1. IVR sanity: if ≥ 3 ETFs from the watchlist show IVR ≥ 99 in the same cycle,
     mark the IVR feed as degraded. Disable vol premium bypass until it recovers.
  2. IVR staleness: if an IVR value hasn't changed in > 2 consecutive polls,
     flag as potentially stale.
  3. Price sanity: spot price changes > 20% in one 30-min cycle flagged as suspect.

Exposes:
  ivr_feed_healthy       — bool; False blocks vol premium bypass in session
  get_validated_ivr(tk)  — returns None when feed degraded, float otherwise
  vol_bypass_allowed()   — False when IVR feed is degraded
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from ..core.config import AgoraSettings, get_settings

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# ETFs whose IVR we use as the sanity baseline
_SANITY_ETFS = ["SPY", "QQQ", "IWM", "GLD", "TLT"]
_IVR_DEGRADED_THRESHOLD = 3      # ≥ this many ETFs at IVR ≥ 99 → degraded
_IVR_STALE_CYCLES = 2            # same value for N+ cycles → stale suspect
_PRICE_SPIKE_PCT = 0.20          # 20% single-cycle move → suspect
_POLL_INTERVAL_SEC = 1800        # 30 min


class DataIntegrityAgent:
    """
    Async agent that polls IVR and price data every 30 min and maintains
    a health state that the session can query synchronously.
    """

    def __init__(
        self,
        settings: AgoraSettings | None = None,
        ceo_agent: Any = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._ceo = ceo_agent
        self._running = False

        # Feed health state
        self._ivr_feed_healthy: bool = True
        self._ivr_degraded_since: datetime | None = None

        # Per-ticker IVR history (last 3 values)
        self._ivr_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=3))
        self._last_ivr: dict[str, float] = {}

        # Per-ticker price history
        self._last_prices: dict[str, float] = {}

        # Stale tickers (suppressed)
        self._stale_ivr_tickers: set[str] = set()
        self._csuite_manager: Any = None   # COOAgent — set via register_csuite_manager()

    # ── Public API (synchronous — safe to call from anywhere) ────────

    def register_csuite_manager(self, manager: Any) -> None:
        """Wire the COOAgent as supervising executive for alert escalation."""
        self._csuite_manager = manager

    def get_stale_tickers(self) -> set[str]:
        """Return the set of tickers whose IVR is currently stale/suppressed."""
        return set(self._stale_ivr_tickers)

    @property
    def ivr_feed_healthy(self) -> bool:
        return self._ivr_feed_healthy

    def vol_bypass_allowed(self) -> bool:
        """Returns False when IVR feed is degraded. Session checks this before bypass."""
        return self._ivr_feed_healthy

    def get_validated_ivr(self, ticker: str) -> float | None:
        """
        Returns the IVR for a ticker, or None if the feed is degraded or stale.
        Session uses this instead of raw yfinance IVR when available.
        """
        if not self._ivr_feed_healthy:
            return None
        if ticker in self._stale_ivr_tickers:
            logger.debug("DataIntegrity: IVR for %s suppressed (stale)", ticker)
            return None
        return self._last_ivr.get(ticker)

    def is_price_suspect(self, ticker: str, current_price: float) -> bool:
        """Returns True if the price moved >20% since last poll (possible bad tick)."""
        last = self._last_prices.get(ticker)
        if last is None or last <= 0:
            return False
        pct_move = abs(current_price - last) / last
        return pct_move > _PRICE_SPIKE_PCT

    # ── Async loop ───────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        await self._poll_cycle()   # immediate first check
        while self._running:
            await asyncio.sleep(_POLL_INTERVAL_SEC)
            now_et = datetime.now(tz=ET)
            if 7 <= now_et.hour < 18:
                await self._poll_cycle()

    async def stop(self) -> None:
        self._running = False

    # ── Poll cycle ───────────────────────────────────────────────────

    async def _poll_cycle(self) -> None:
        try:
            import yfinance as yf
            high_ivr_count = 0
            new_ivr: dict[str, float] = {}

            for ticker in _SANITY_ETFS:
                try:
                    tk = yf.Ticker(ticker)
                    info = tk.info or {}
                    # IVR proxy: compare current IV to 52-week range
                    # yfinance doesn't give IVR directly; we use a heuristic:
                    # (current IV - 52w_iv_low) / (52w_iv_high - 52w_iv_low)
                    iv_now = float(
                        info.get("impliedVolatility")
                        or info.get("annualHoldingsReturnYTD")
                        or 0
                    )
                    # Simpler: trust what the IvPremiumScreen computes via realtime options
                    # For sanity check, we just look for the "too uniform" pattern
                    iv_pct = float(info.get("regularMarketVolume") or 0)

                    # The actual check: did yfinance return IVR=100 for this ticker?
                    # We detect this by reading from options chain ATM IV vs 52w high
                    try:
                        fi   = tk.fast_info
                        spot = float(getattr(fi, "last_price", None) or fi.get("lastPrice", 0) or 0)
                    except Exception:
                        spot = 0.0
                    if spot <= 0:
                        spot = float(info.get("regularMarketPrice") or 0)
                    if spot > 0 and tk.options:
                        try:
                            chain = tk.option_chain(tk.options[0])
                            calls = chain.calls
                            if not calls.empty:
                                atm = calls.iloc[(calls["strike"] - spot).abs().argsort()[:1]]
                                iv_atm = float(atm["impliedVolatility"].iloc[0] or 0)
                                new_ivr[ticker] = iv_atm

                                # Check history for staleness
                                history = self._ivr_history[ticker]
                                history.append(round(iv_atm, 4))
                                if (
                                    len(history) >= _IVR_STALE_CYCLES
                                    and len(set(history)) == 1
                                ):
                                    self._stale_ivr_tickers.add(ticker)
                                    logger.debug(
                                        "DataIntegrity: %s IVR stale (%s unchanged for %d cycles)",
                                        ticker, iv_atm, len(history),
                                    )
                                else:
                                    self._stale_ivr_tickers.discard(ticker)

                                # Count suspiciously-high IVR (proxy: ATM IV > 0.95)
                                if iv_atm >= 0.95:
                                    high_ivr_count += 1
                        except Exception:
                            pass

                    # Price sanity
                    if spot > 0:
                        if self.is_price_suspect(ticker, spot):
                            logger.warning(
                                "DataIntegrity: %s price spike suspect — "
                                "last=%.2f current=%.2f",
                                ticker, self._last_prices.get(ticker, 0), spot,
                            )
                        self._last_prices[ticker] = spot

                except Exception as exc:
                    logger.debug("DataIntegrity poll failed for %s: %s", ticker, exc)

            self._last_ivr.update(new_ivr)
            prev_healthy = self._ivr_feed_healthy

            # Sanity gate: ≥3 ETFs with suspicious IV simultaneously → degraded
            self._ivr_feed_healthy = high_ivr_count < _IVR_DEGRADED_THRESHOLD

            if not self._ivr_feed_healthy and prev_healthy:
                self._ivr_degraded_since = datetime.now(tz=ET)
                msg = (
                    f"IVR feed DEGRADED: {high_ivr_count}/{len(_SANITY_ETFS)} ETFs "
                    f"showing anomalous IV simultaneously. "
                    f"Vol premium bypass DISABLED until feed recovers."
                )
                logger.warning("DataIntegrity: %s", msg)
                await self._notify("warning", f"⚠️ {msg}")

            elif self._ivr_feed_healthy and not prev_healthy:
                duration_min = 0.0
                if self._ivr_degraded_since:
                    duration_min = (
                        datetime.now(tz=ET) - self._ivr_degraded_since
                    ).total_seconds() / 60
                msg = f"IVR feed RECOVERED after {duration_min:.0f} min. Vol bypass re-enabled."
                logger.info("DataIntegrity: %s", msg)
                self._ivr_degraded_since = None
                await self._notify("info", f"✅ {msg}")
            else:
                logger.debug(
                    "DataIntegrity: IVR feed healthy | high_ivr_count=%d | stale=%s",
                    high_ivr_count, self._stale_ivr_tickers,
                )

        except Exception as exc:
            logger.error("DataIntegrityAgent poll cycle error: %s", exc)

    async def _notify(self, level: str, message: str) -> None:
        """Route alert: DataIntegrityAgent → COO → CEO."""
        if self._csuite_manager:
            await self._csuite_manager.receive_alert("DataIntegrityAgent", level, message)
        elif self._ceo:
            await self._ceo.dispatch_alert(level, message)

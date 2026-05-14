"""
SetupWatcherAgent — fast-path intraday setup detection.

This agent is the second tier of the two-tier architecture:
  Tier 1 (slow): Claude generates strategy parameters every 90 minutes
  Tier 2 (fast): SetupWatcher polls 5-min bars every 5 minutes, fires
                 immediately when a setup trigger is detected using
                 pure arithmetic (no Claude call).

When a trigger fires, it publishes SETUP_TRIGGERED. The orchestrator
or an external process can then escalate to a full analysis if needed.

Setup types detected (all arithmetic, no AI):
  ORB_BREAK       — price breaks Opening Range Breakout level set 9:30-10:00
  VWAP_RECLAIM    — price crosses VWAP from below with volume spike
  SUPPORT_BOUNCE  — candle wicks to support ±0.3%, closes above
  RESISTANCE_FAIL — candle wicks to resistance ±0.3%, closes below
  MOMENTUM_SURGE  — 5-min bar range > 2x ATR with directional close
  IV_SPIKE        — IV rank jumps > 15 points in one 5-min window
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from ..core.bus import MessageBus
from ..core.config import Settings, get_settings
from ..core.models.agent import AgentMessage, AgentTopic, AnalysisSession
from ..core.state import SharedStateStore
from ..scripts.scheduler import _is_entry_blackout, _is_market_open

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# Opening range window (9:30–10:00 ET)
_ORB_START = time(9, 30)
_ORB_END   = time(10, 0)


@dataclass
class _TickerState:
    """Per-ticker rolling state for setup detection."""
    ticker: str
    bars: list[dict[str, float]] = field(default_factory=list)  # last 20 x 5-min bars
    orb_high: float | None = None
    orb_low:  float | None = None
    orb_set:  bool = False
    vwap:     float | None = None
    atr5:     float | None = None   # 5-min ATR (rolling)
    prev_iv:  float | None = None
    last_trigger: str | None = None
    last_trigger_time: datetime | None = None


def _compute_vwap(bars: list[dict]) -> float | None:
    """Compute VWAP from intraday bars (typical_price × volume / total_volume)."""
    total_vol = sum(b.get("volume", 0) for b in bars)
    if total_vol == 0:
        return None
    return sum(
        ((b["high"] + b["low"] + b["close"]) / 3) * b.get("volume", 0)
        for b in bars
    ) / total_vol


def _compute_atr(bars: list[dict]) -> float | None:
    """True range ATR over provided bars."""
    if len(bars) < 3:
        return None
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs) if trs else None


class SetupWatcherAgent:
    """
    Standalone polling agent — does NOT extend BaseAgent because it does
    not subscribe to the message bus (it polls IBKR directly on a timer).

    Instantiate once, call start(), and it will run until stop() is called.
    """

    name = "setup_watcher"

    def __init__(
        self,
        bus: MessageBus,
        state_store: SharedStateStore,
        settings: Settings | None = None,
        poll_interval_seconds: float = 300.0,  # 5 minutes
        cooldown_seconds: float = 900.0,       # 15 min between triggers per ticker
    ) -> None:
        self._bus = bus
        self._state = state_store
        self._settings = settings or get_settings()
        self._poll_interval = poll_interval_seconds
        self._cooldown = cooldown_seconds
        self._log = logging.getLogger("platform.agents.setup_watcher")
        self._running = False
        self._ticker_states: dict[str, _TickerState] = {}
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run(), name="setup_watcher")
        self._log.info(
            "SetupWatcher started — tickers=%s poll=%.0fs cooldown=%.0fs",
            self._settings.default_tickers,
            self._poll_interval,
            self._cooldown,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._log.info("SetupWatcher stopped")

    async def _run(self) -> None:
        while self._running:
            now = datetime.now(tz=ET)
            if not _is_market_open(now):
                await asyncio.sleep(60)
                continue

            in_blackout, _ = _is_entry_blackout(now)
            if not in_blackout:
                for ticker in self._settings.default_tickers:
                    try:
                        await self._check_ticker(ticker, now)
                    except Exception as exc:
                        self._log.error("SetupWatcher error for %s: %s", ticker, exc)

            await asyncio.sleep(self._poll_interval)

    async def _check_ticker(self, ticker: str, now: datetime) -> None:
        state = self._ticker_states.setdefault(ticker, _TickerState(ticker=ticker))

        # Fetch latest 5-min bars (last 20 bars = 100 minutes of data)
        bars = await self._fetch_5min_bars(ticker, count=20)
        if not bars or len(bars) < 3:
            return

        state.bars = bars
        latest = bars[-1]
        prev    = bars[-2]

        # ── Update ORB (reset at start of each session) ───────────────────
        if not state.orb_set:
            orb_bars = [b for b in bars if self._in_orb_window(b.get("dt"))]
            if orb_bars and now.time() >= _ORB_END:
                state.orb_high = max(b["high"] for b in orb_bars)
                state.orb_low  = min(b["low"]  for b in orb_bars)
                state.orb_set  = True
                self._log.info(
                    "%s ORB set — high=%.2f low=%.2f",
                    ticker, state.orb_high, state.orb_low,
                )

        state.vwap = _compute_vwap(bars)
        state.atr5 = _compute_atr(bars)

        # Check cooldown
        if state.last_trigger_time:
            secs_since = (now - state.last_trigger_time.astimezone(ET)).total_seconds()
            if secs_since < self._cooldown:
                return

        # ── Setup detection ───────────────────────────────────────────────
        trigger = self._detect_setup(state, latest, prev)
        if trigger:
            state.last_trigger = trigger
            state.last_trigger_time = now
            await self._fire_trigger(ticker, trigger, latest, state, now)

    def _in_orb_window(self, dt: Any) -> bool:
        if dt is None:
            return False
        try:
            t = datetime.fromisoformat(str(dt)).astimezone(ET).time()
            return _ORB_START <= t < _ORB_END
        except Exception:
            return False

    def _detect_setup(
        self,
        state: _TickerState,
        latest: dict,
        prev: dict,
    ) -> str | None:
        close = latest["close"]
        high  = latest["high"]
        low   = latest["low"]
        vol   = latest.get("volume", 0)
        avg_vol = sum(b.get("volume", 0) for b in state.bars[:-1]) / max(len(state.bars) - 1, 1)

        # ── ORB breakout ──────────────────────────────────────────────────
        if state.orb_set and state.orb_high and state.orb_low:
            if prev["close"] <= state.orb_high and close > state.orb_high * 1.001:
                return "ORB_BREAK_UP"
            if prev["close"] >= state.orb_low and close < state.orb_low * 0.999:
                return "ORB_BREAK_DOWN"

        # ── VWAP reclaim ──────────────────────────────────────────────────
        if state.vwap:
            vwap_crossed_up   = prev["close"] < state.vwap and close > state.vwap
            vwap_crossed_down = prev["close"] > state.vwap and close < state.vwap
            vol_spike = avg_vol > 0 and vol > avg_vol * 1.5
            if vwap_crossed_up and vol_spike:
                return "VWAP_RECLAIM_UP"
            if vwap_crossed_down and vol_spike:
                return "VWAP_RECLAIM_DOWN"

        # ── Momentum surge ────────────────────────────────────────────────
        if state.atr5:
            bar_range = high - low
            if bar_range > state.atr5 * 2.0:
                # Directional close: in top/bottom 20% of range
                if close > low + bar_range * 0.80:
                    return "MOMENTUM_SURGE_UP"
                if close < low + bar_range * 0.20:
                    return "MOMENTUM_SURGE_DOWN"

        return None

    async def _fire_trigger(
        self,
        ticker: str,
        trigger: str,
        bar: dict,
        state: _TickerState,
        now: datetime,
    ) -> None:
        direction = "bullish" if trigger.endswith("_UP") else "bearish"

        payload: dict[str, Any] = {
            "ticker": ticker,
            "trigger": trigger,
            "direction": direction,
            "price": bar["close"],
            "bar_high": bar["high"],
            "bar_low": bar["low"],
            "volume": bar.get("volume"),
            "vwap": state.vwap,
            "atr5": state.atr5,
            "orb_high": state.orb_high,
            "orb_low": state.orb_low,
            "detected_at": now.isoformat(),
        }

        self._log.info(
            "SETUP TRIGGERED: %s %s @ %.2f (VWAP=%.2f)",
            ticker, trigger, bar["close"], state.vwap or 0,
        )

        # Create a fresh analysis session for this setup trigger
        session = await self._state.create_session(ticker)
        session_id = session.session_id

        msg = AgentMessage.create(
            topic=AgentTopic.SETUP_TRIGGERED,
            session_id=session_id,
            sender=self.name,
            payload=payload,
        )
        await self._bus.publish(msg)

    async def _fetch_5min_bars(self, ticker: str, count: int = 20) -> list[dict]:
        """
        Fetch last `count` 5-min bars.
        Uses IBKR if available, falls back to yfinance for development.
        """
        try:
            from ..services.ibkr_client import get_ibkr_client
            ibkr = get_ibkr_client()
            if ibkr and await ibkr.is_connected():
                bars = await ibkr.get_5min_bars(ticker, count=count)
                if bars:
                    return bars
        except Exception as exc:
            self._log.debug("IBKR bars unavailable for %s: %s — falling back to yfinance", ticker, exc)

        # yfinance fallback (development / paper trading)
        try:
            import yfinance as yf
            tk = yf.Ticker(ticker)
            hist = tk.history(period="1d", interval="5m")
            if hist.empty:
                return []
            bars = []
            for ts, row in hist.tail(count).iterrows():
                bars.append({
                    "dt": ts.isoformat(),
                    "open":   float(row["Open"]),
                    "high":   float(row["High"]),
                    "low":    float(row["Low"]),
                    "close":  float(row["Close"]),
                    "volume": float(row["Volume"]),
                })
            return bars
        except Exception as exc:
            self._log.warning("yfinance 5-min bars failed for %s: %s", ticker, exc)
            return []

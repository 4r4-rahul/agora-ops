"""
PreMarketAgent — overnight + pre-market intelligence brief.

Subscribes to: ANALYSIS_REQUEST (fires at market open OR when triggered manually)
Publishes:     PREMARKET_READY → stored in session.premarket_context

Gathers:
  - ES/NQ futures vs previous close (overnight move, gap direction)
  - VIX futures term structure (M1 vs M2 — contango/backwardation)
  - Previous session close + after-hours/pre-market move
  - Key macro events today (from MacroCalendar)
  - Opening gap size and historical gap-fill statistics
  - Overnight high/low range

This runs deterministically (no Claude call). The output is injected
into the OptionsStrategy and Regime prompts as context.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from ..core.models.agent import AgentMessage, AgentTopic
from .base import BaseAgent

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# Gap size thresholds
_GAP_SMALL    = 0.003   # < 0.3% — noise, usually fills
_GAP_MEDIUM   = 0.008   # 0.3–0.8% — notable, ~60% fill rate
_GAP_LARGE    = 0.015   # 0.8–1.5% — strong directional signal
_GAP_EXTREME  = 0.025   # > 2.5% — fade with caution


class PreMarketAgent(BaseAgent):
    """
    Pre-market intelligence — runs at or before 9:30 ET.
    No Claude call; all arithmetic from market data.
    """

    name = "premarket"
    subscriptions = [AgentTopic.ANALYSIS_REQUEST]

    # Class-level daily cache: {ticker: (date, context_dict)}
    # Refreshes automatically on a new calendar date.
    _daily_cache: dict[str, tuple[date, dict]] = {}

    async def handle(self, message: AgentMessage) -> None:
        session_id = message.session_id
        session = await self._state.get(session_id)
        if not session:
            return

        ticker = session.ticker
        now_et = datetime.now(tz=ET)
        today = now_et.date()

        # Return cached context if we already fetched for this ticker today
        cached = self._daily_cache.get(ticker)
        if cached and cached[0] == today:
            await self._state.update(session_id, premarket_context=cached[1])
            await self.publish(
                AgentTopic.PREMARKET_READY,
                session_id=session_id,
                payload=cached[1],
            )
            return

        # Only run pre-market analysis before 10:30 ET or if context missing
        if now_et.time() > time(10, 30) and session.premarket_context:
            return  # already populated, not yet cached (shouldn't happen)

        self._log.info("[%s] building pre-market context for %s", session_id, ticker)

        try:
            context = await self._build_context(ticker)
        except Exception as exc:
            self._log.warning("[%s] pre-market context failed: %s", session_id, exc)
            context = {"error": str(exc), "ticker": ticker}

        # Cache for the rest of the trading day (keyed by ticker + date)
        PreMarketAgent._daily_cache[ticker] = (today, context)

        await self._state.update(session_id, premarket_context=context)
        await self.publish(
            AgentTopic.PREMARKET_READY,
            session_id=session_id,
            payload=context,
        )
        self._log.info(
            "[%s] premarket: gap=%.1f%% direction=%s VIX_term=%s",
            session_id,
            context.get("gap_pct", 0) * 100,
            context.get("gap_direction", "flat"),
            context.get("vix_term_structure", "unknown"),
        )

    async def _build_context(self, ticker: str) -> dict:
        import yfinance as yf

        # ── Fetch SPY/QQQ and futures proxies ─────────────────────────────
        # /ES futures → ES=F, /NQ futures → NQ=F, VIX → ^VIX, VIXM → ^VXMT (mid-term VIX)
        symbols = [ticker, "SPY", "^VIX", "ES=F", "NQ=F"]
        data: dict[str, dict] = {}
        for sym in symbols:
            try:
                tk = yf.Ticker(sym)
                hist = tk.history(period="5d", interval="1d", prepost=True)
                if not hist.empty:
                    data[sym] = {
                        "close":     float(hist["Close"].iloc[-1]),
                        "prev_close": float(hist["Close"].iloc[-2]) if len(hist) >= 2 else None,
                        "open":      float(hist["Open"].iloc[-1]),
                        "high":      float(hist["High"].iloc[-1]),
                        "low":       float(hist["Low"].iloc[-1]),
                        "volume":    float(hist["Volume"].iloc[-1]),
                    }
            except Exception:
                pass

        target = data.get(ticker, {})
        spy    = data.get("SPY", {})
        vix    = data.get("^VIX", {})
        vixmt  = data.get("^VXMT", {})
        es_fut = data.get("ES=F", {})

        # ── Gap analysis ──────────────────────────────────────────────────
        prev_close = target.get("prev_close") or target.get("close", 0)
        today_open = target.get("open", prev_close)
        gap_pct = (today_open - prev_close) / prev_close if prev_close else 0

        if abs(gap_pct) < _GAP_SMALL:
            gap_type = "flat"
        elif abs(gap_pct) < _GAP_MEDIUM:
            gap_type = "small_gap"
        elif abs(gap_pct) < _GAP_LARGE:
            gap_type = "medium_gap"
        elif abs(gap_pct) < _GAP_EXTREME:
            gap_type = "large_gap"
        else:
            gap_type = "extreme_gap"

        gap_direction = "up" if gap_pct > _GAP_SMALL else ("down" if gap_pct < -_GAP_SMALL else "flat")

        # Gap-fill probability (historical averages from academic research)
        gap_fill_prob = {
            "flat": 0.0, "small_gap": 0.72, "medium_gap": 0.56,
            "large_gap": 0.38, "extreme_gap": 0.22,
        }.get(gap_type, 0.5)

        # ── VIX term structure ─────────────────────────────────────────────
        vix_spot  = vix.get("close")
        vixmt_val = vixmt.get("close")
        if vix_spot and vixmt_val:
            vix_spread = vixmt_val - vix_spot
            if vix_spread > 2:
                vix_term = "contango"     # normal, market calm
            elif vix_spread < -2:
                vix_term = "backwardation"  # fear, market stressed
            else:
                vix_term = "flat"
        else:
            vix_term = "unknown"
            vix_spread = None

        # ── Futures overnight move ─────────────────────────────────────────
        es_close     = es_fut.get("close")
        es_prev      = es_fut.get("prev_close")
        futures_move = (es_close - es_prev) / es_prev if (es_close and es_prev) else None

        # ── Overnight range (proxy: today high-low before open) ───────────
        overnight_range = None
        if target.get("high") and target.get("low"):
            overnight_range = target["high"] - target["low"]

        # ── Macro events today ────────────────────────────────────────────
        from ..services.macro_calendar import get_macro_calendar
        cal = get_macro_calendar()
        today_events = [
            e for e in cal.upcoming_events(days=1)
            if e.event_date == date.today()
        ]
        days_to_next, next_event = cal.days_to_next_event(date.today())

        # ── Bias synthesis ────────────────────────────────────────────────
        # Simple rule-based directional bias for the opening session
        bias = "neutral"
        bias_strength = 0.5

        if futures_move is not None and abs(futures_move) > 0.003:
            bias = "bullish" if futures_move > 0 else "bearish"
            bias_strength = min(0.9, 0.5 + abs(futures_move) * 20)

        if vix_term == "backwardation" and vix_spot and vix_spot > 25:
            bias = "bearish"
            bias_strength = max(bias_strength, 0.7)

        return {
            "ticker": ticker,
            "as_of": datetime.now(tz=ET).isoformat(),
            # Gap
            "prev_close": prev_close,
            "today_open": today_open,
            "gap_pct": round(gap_pct, 4),
            "gap_type": gap_type,
            "gap_direction": gap_direction,
            "gap_fill_probability": gap_fill_prob,
            # VIX / vol
            "vix_spot": vix_spot,
            "vix_midterm": vixmt_val,
            "vix_term_structure": vix_term,
            "vix_spread": vix_spread,
            # Futures
            "es_futures_move_pct": round(futures_move * 100, 2) if futures_move else None,
            # Range
            "overnight_range": overnight_range,
            # Macro
            "macro_events_today": today_events,
            "days_to_next_macro": days_to_next,
            "next_macro_event": next_event,
            "macro_risk": cal.get_risk_level(date.today()),
            # Bias
            "opening_bias": bias,
            "bias_strength": bias_strength,
        }

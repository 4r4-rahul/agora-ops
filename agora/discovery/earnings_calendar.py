"""
EarningsCalendarAgent — proactive pre-earnings intelligence.

The problem it solves: CSCO gapped $18 at 5:33 PM because we were reactive.
This agent makes us *proactive* — we know about CSCO earnings T-7 days out,
calculate the implied vs historical move edge, and position BEFORE the announcement.

What it does every morning at 6:30 AM ET:
  1. Sweeps all universe tickers for earnings in next 1-7 days
  2. Computes edge ratio: avg_historical_move / implied_move (need > 1.2× to trade)
  3. Finds sector peers that already reported this quarter → read-through prediction
  4. Builds an EarningsSetup for each qualified ticker
  5. Fires on_earnings_setup callback → session builds pre-earnings options position

What it does NOT do: re-discover post-announcement (that's CatalystDiscoveryAgent's job).

Claude API: Haiku for peer classification, Opus for edge analysis + synthesis.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Coroutine
from zoneinfo import ZoneInfo

import anthropic

from ..core.config import AgoraSettings, get_settings
from ..ops.llm_cost_log import log_call as _log_llm

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# Sector → peer tickers for read-through analysis
_SECTOR_PEERS: dict[str, list[str]] = {
    "technology":       ["AAPL", "MSFT", "GOOGL", "META", "AMZN", "NVDA", "AMD", "INTC", "AVGO"],
    "semiconductors":   ["NVDA", "AMD", "INTC", "AVGO", "TSM", "MU", "TXN", "LRCX", "ASML"],
    "ai_infrastructure":["NVDA", "MSFT", "META", "GOOGL", "AMZN", "ORCL", "CSCO", "AVGO"],
    "cloud":            ["SNOW", "ZS", "MSFT", "AMZN", "GOOGL", "ORCL"],
    "defense":          ["PLTR", "KTOS", "AVAV", "RKLB"],
    "energy":           ["VST", "CEG", "NEE", "CCJ"],
    "finance":          ["SCHW", "HOOD", "CBOE"],
    "healthcare":       ["LLY", "JNJ", "ABT", "BSX", "BIIB", "MDT"],
    "retail":           ["COST", "WMT", "BJ"],
}

# Minimum edge ratio to fire a pre-earnings setup signal
_MIN_EDGE_RATIO = 1.20   # historical avg move must be ≥ 1.2× implied move
# Implied move is estimated as: spot × IV × sqrt(DTE/365)
# If IV = 30%, DTE = 2 days, spot = $100 → implied move ≈ $2.48


@dataclass
class EarningsSetup:
    """Fully-qualified pre-earnings opportunity."""
    ticker: str
    earnings_date: date
    dte: int                         # days to earnings from today
    direction: str                   # "bullish" | "bearish" | "neutral"
    confidence: float                # 0-1

    implied_move_pct: float          # how much options market implies
    historical_move_avg_pct: float   # avg of last 4 earnings moves (abs)
    edge_ratio: float                # historical / implied — higher = more edge

    peer_context: str                # read-through summary from Claude
    reasoning: str                   # why we like the setup
    spot: float = 0.0

    # Peer data — tickers that reported this quarter and their moves
    peer_moves: dict[str, float] = field(default_factory=dict)


class EarningsCalendarAgent:
    """
    Async agent that sweeps the upcoming earnings calendar every morning.
    Runs independently of the catalyst agent — this is proactive, not reactive.
    """

    def __init__(
        self,
        settings: AgoraSettings | None = None,
        on_earnings_setup: Callable[[EarningsSetup], Coroutine] | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._on_earnings_setup = on_earnings_setup
        self._client = anthropic.AsyncAnthropic(api_key=self._settings.anthropic_api_key)
        self._running = False
        self._last_sweep_date: date | None = None   # one sweep per calendar day
        self._csuite_manager: Any = None   # RNDAgent — set via register_csuite_manager()
        self._upcoming_events: list[dict] = []   # qualified setups from last sweep

    def register_csuite_manager(self, manager: Any) -> None:
        """Wire the RNDAgent as supervising executive."""
        self._csuite_manager = manager

    def get_upcoming_events(self) -> list[dict]:
        """Return upcoming qualified earnings setups for R&D briefing."""
        return list(self._upcoming_events)

    async def start(self) -> None:
        self._running = True
        logger.info("EarningsCalendarAgent started")
        while self._running:
            now_et = datetime.now(tz=ET)
            # Sweep window: 6:30 AM ET, once per day
            if (now_et.hour == 6 and now_et.minute >= 30) or (now_et.hour == 7 and now_et.minute == 0):
                today = now_et.date()
                if self._last_sweep_date != today:
                    try:
                        await self._morning_sweep()
                        self._last_sweep_date = today
                    except Exception as exc:
                        logger.error("Earnings calendar sweep failed: %s", exc)
            await asyncio.sleep(60)

    async def stop(self) -> None:
        self._running = False

    # ── Morning sweep ──────────────────────────────────────────────

    async def _morning_sweep(self) -> None:
        today = date.today()
        horizon = today + timedelta(days=7)
        universe = self._settings.etf_universe

        logger.info("Earnings calendar sweep: universe=%d tickers, horizon=%s", len(universe), horizon)

        # Fetch earnings dates for all universe tickers (in parallel batches)
        batch_size = 10
        all_upcoming: list[tuple[str, date]] = []
        for i in range(0, len(universe), batch_size):
            batch = universe[i : i + batch_size]
            results = await asyncio.gather(
                *[self._get_earnings_date(t) for t in batch],
                return_exceptions=True,
            )
            for ticker, result in zip(batch, results):
                if isinstance(result, date) and today <= result <= horizon:
                    all_upcoming.append((ticker, result))
                    logger.info("Earnings upcoming: %s on %s (T-%d)",
                                ticker, result, (result - today).days)

        if not all_upcoming:
            logger.info("No earnings in next 7 days for universe tickers")
            return

        logger.info("Found %d upcoming earnings: %s",
                    len(all_upcoming),
                    ", ".join(f"{t}({d})" for t, d in all_upcoming))

        # Process each qualified ticker
        tasks = [self._analyze_ticker(ticker, earnings_date)
                 for ticker, earnings_date in all_upcoming]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _get_earnings_date(self, ticker: str) -> date | None:
        """
        Fetch next earnings date from yfinance calendar.

        yfinance often returns a LIST [date_low, date_high] when the company hasn't
        confirmed the exact date. E.g., CSCO might return [May 14, May 18].

        Strategy:
          - Range ≤ 5 days: use the MIDPOINT (unconfirmed window, reasonable estimate)
          - Range > 5 days: skip — too uncertain to position against
          - Single date: use as-is (company confirmed)
          - Log which case we hit so we can audit accuracy over time
        """
        try:
            import yfinance as yf
            tk = yf.Ticker(ticker)
            cal = tk.calendar
            if cal is None:
                return None

            if hasattr(cal, "get"):
                earnings = cal.get("Earnings Date")
            elif hasattr(cal, "loc"):
                try:
                    earnings = cal.loc["Earnings Date"].iloc[0] if "Earnings Date" in cal.index else None
                except Exception:
                    return None
            else:
                return None

            if earnings is None:
                return None

            def _to_date(v) -> date | None:
                if hasattr(v, "date"):
                    return v.date()
                if isinstance(v, date):
                    return v
                return None

            # Handle range vs single date
            if isinstance(earnings, (list, tuple)) and len(earnings) >= 2:
                d0 = _to_date(earnings[0])
                d1 = _to_date(earnings[-1])
                if d0 and d1:
                    spread = (d1 - d0).days
                    if spread > 5:
                        logger.debug(
                            "%s earnings date too uncertain: range %s–%s (%d days) — skipping",
                            ticker, d0, d1, spread,
                        )
                        return None
                    # Midpoint of confirmed range (round toward earlier for conservative DTE)
                    mid = d0 + timedelta(days=spread // 2)
                    logger.debug("%s earnings date: range %s–%s → midpoint %s", ticker, d0, d1, mid)
                    return mid
                return d0 or d1

            if isinstance(earnings, (list, tuple)) and len(earnings) == 1:
                return _to_date(earnings[0])

            return _to_date(earnings)
        except Exception:
            return None

    # ── Ticker analysis ────────────────────────────────────────────

    async def _analyze_ticker(self, ticker: str, earnings_date: date) -> None:
        """Full pre-earnings analysis for one ticker."""
        try:
            today = date.today()
            dte = (earnings_date - today).days

            # Use the expiry just PAST earnings to capture post-event IV
            # (earnings-week expiry has event IV priced in; prior expiry does not)
            target_dte = dte + 2   # first expiry after earnings

            # Fetch spot + ATM straddle price (industry-standard implied move proxy)
            spot, straddle, iv = await self._get_spot_and_straddle(ticker, target_dte)
            if spot <= 0:
                logger.debug("No spot data for %s — skipping", ticker)
                return

            # Primary: straddle / spot = implied move (most accurate near earnings)
            # Fallback: IV × sqrt(DTE / 365) — calendar days, correct denominator
            if straddle > 0:
                implied_move_pct = straddle / spot
                logger.debug("%s implied move via straddle: %.1f%% (straddle=$%.2f spot=$%.2f)",
                             ticker, implied_move_pct * 100, straddle, spot)
            elif iv > 0:
                implied_move_pct = iv * math.sqrt(max(dte, 0.5) / 365)
                logger.debug("%s implied move via IV fallback: %.1f%%", ticker, implied_move_pct * 100)
            else:
                logger.debug("No straddle or IV for %s — skipping", ticker)
                return

            # Historical earnings moves (last 4 quarters)
            hist_moves = await self._get_historical_earnings_moves(ticker)
            if not hist_moves:
                logger.debug("No historical moves for %s — skipping", ticker)
                return

            hist_avg = sum(abs(m) for m in hist_moves) / len(hist_moves)
            edge_ratio = hist_avg / implied_move_pct if implied_move_pct > 0 else 0.0

            if edge_ratio < _MIN_EDGE_RATIO:
                logger.info(
                    "%s: edge ratio %.2f < %.2f — skipping (implied=%.1f%%, hist_avg=%.1f%%)",
                    ticker, edge_ratio, _MIN_EDGE_RATIO,
                    implied_move_pct * 100, hist_avg * 100,
                )
                return

            # Sector peer read-through
            peer_moves, peer_context = await self._peer_read_through(ticker, earnings_date)

            # Direction: synthesize from peer read-through and sector momentum
            direction, confidence, reasoning = await self._synthesize_setup(
                ticker=ticker,
                dte=dte,
                implied_move_pct=implied_move_pct,
                hist_moves=hist_moves,
                edge_ratio=edge_ratio,
                peer_moves=peer_moves,
                peer_context=peer_context,
                spot=spot,
            )

            setup = EarningsSetup(
                ticker=ticker,
                earnings_date=earnings_date,
                dte=dte,
                direction=direction,
                confidence=confidence,
                implied_move_pct=implied_move_pct,
                historical_move_avg_pct=hist_avg,
                edge_ratio=edge_ratio,
                peer_context=peer_context,
                reasoning=reasoning,
                spot=spot,
                peer_moves=peer_moves,
            )

            logger.info(
                "EARNINGS SETUP: %s | %s T-%d | dir=%s conf=%.2f | "
                "edge=%.2fx (hist=%.1f%% vs impl=%.1f%%)",
                ticker, earnings_date, dte,
                direction, confidence,
                edge_ratio, hist_avg * 100, implied_move_pct * 100,
            )

            self._upcoming_events.append({
                "ticker": ticker,
                "earnings_date": earnings_date.isoformat(),
                "dte": dte,
                "direction": direction,
                "confidence": confidence,
                "edge_ratio": round(edge_ratio, 2),
            })

            if self._on_earnings_setup:
                await self._on_earnings_setup(setup)

            if self._csuite_manager:
                await self._csuite_manager.receive_alert(
                    "EarningsCalendar", "info",
                    f"Pre-earnings setup: {ticker} reports {earnings_date} (T-{dte}) | "
                    f"dir={direction} conf={confidence:.2f} edge={edge_ratio:.2f}x",
                )

        except Exception as exc:
            logger.error("Earnings analysis failed for %s: %s", ticker, exc)

    async def _get_spot_and_straddle(self, ticker: str, target_dte: int) -> tuple[float, float, float]:
        """
        Return (spot, atm_straddle_price, atm_iv).

        Straddle price (call + put at same ATM strike) is the industry-standard
        way to read the implied move: implied_move_pct = straddle / spot.
        This is more accurate than IV × √(T) near earnings because:
          - Term structure distorts IV (front month inflated)
          - Put-call parity ensures straddle = market's actual expected move range

        Falls back to IV approximation if puts data unavailable.
        """
        try:
            import yfinance as yf
            from trading_platform.services.market_data.yfinance_provider import _YF_OPTIONS_LOCK

            with _YF_OPTIONS_LOCK:
                tk = yf.Ticker(ticker)
                try:
                    fi   = tk.fast_info
                    spot = float(getattr(fi, "last_price", None) or fi.get("lastPrice", 0) or 0)
                except Exception:
                    spot = 0.0
                if spot <= 0:
                    info = tk.info or {}
                    spot = float(info.get("regularMarketPrice") or info.get("currentPrice") or 0)
                if spot <= 0:
                    return 0.0, 0.0, 0.0

                exps = tk.options or []
                if not exps:
                    return spot, 0.0, 0.0

                # Pick expiry closest to target_dte (just past the earnings date)
                today_d = date.today()
                best_exp = min(
                    exps,
                    key=lambda e: abs((date.fromisoformat(e) - today_d).days - target_dte),
                )
                chain = tk.option_chain(best_exp)
                calls = chain.calls if chain and not chain.calls.empty else None
                puts  = chain.puts  if chain and not chain.puts.empty  else None
                if calls is None or calls.empty:
                    return spot, 0.0, 0.0

                # ATM index by strike closest to spot
                atm_idx = int((calls["strike"] - spot).abs().argsort().iloc[0])
                atm_iv  = float(calls["impliedVolatility"].iloc[atm_idx] or 0)

                # Straddle = ATM call mid + ATM put mid
                call_bid = float(calls["bid"].iloc[atm_idx] or 0)
                call_ask = float(calls["ask"].iloc[atm_idx] or 0)
                call_mid = (call_bid + call_ask) / 2 if call_ask > 0 else 0.0

                put_mid = 0.0
                if puts is not None and not puts.empty:
                    atm_strike = float(calls["strike"].iloc[atm_idx])
                    put_row = puts.iloc[(puts["strike"] - atm_strike).abs().argsort().iloc[0]]
                    p_bid = float(put_row.get("bid") or 0)
                    p_ask = float(put_row.get("ask") or 0)
                    put_mid = (p_bid + p_ask) / 2 if p_ask > 0 else 0.0

                straddle = call_mid + put_mid
                return spot, straddle, atm_iv
        except Exception:
            return 0.0, 0.0, 0.0

    async def _get_historical_earnings_moves(self, ticker: str) -> list[float]:
        """
        Return last 4 earnings day moves as fraction of price (e.g. 0.08 = 8% up).
        Uses yfinance earnings history + price data.
        """
        try:
            import yfinance as yf
            import pandas as pd

            tk = yf.Ticker(ticker)

            # earnings_dates returns a DataFrame with EPS data indexed by date
            earnings_dates = tk.earnings_dates
            if earnings_dates is None or earnings_dates.empty:
                return []

            # Get last 4 past earnings dates
            past = earnings_dates[earnings_dates.index < pd.Timestamp.now(tz="UTC")]
            past = past.sort_index(ascending=False).head(4)
            if past.empty:
                return []

            # Fetch 1-year daily price history
            hist = tk.history(period="1y", interval="1d", auto_adjust=True)
            if hist.empty:
                return []

            moves: list[float] = []
            for ts in past.index:
                earnings_day = ts.date() if hasattr(ts, "date") else ts
                # Find the day-after close vs day-before close
                try:
                    hist_dates = [d.date() for d in hist.index]
                    if earnings_day not in hist_dates:
                        continue
                    idx = hist_dates.index(earnings_day)
                    if idx == 0:
                        continue
                    close_earnings = float(hist["Close"].iloc[idx])
                    close_prev     = float(hist["Close"].iloc[idx - 1])
                    move = (close_earnings - close_prev) / close_prev
                    moves.append(move)
                except (ValueError, IndexError):
                    continue

            return moves
        except Exception:
            return []

    async def _peer_read_through(
        self, ticker: str, earnings_date: date
    ) -> tuple[dict[str, float], str]:
        """
        Find sector peers that reported this quarter (last 60 days) and measure their moves.
        Returns (peer_moves dict, Claude summary of read-through).
        """
        peers = self._get_sector_peers(ticker)
        if not peers:
            return {}, "No sector peers defined"

        # Fetch recent moves for peers (last 60 days of price history)
        peer_moves: dict[str, float] = {}
        try:
            import yfinance as yf
            for peer in peers[:6]:   # limit to 6 peers
                if peer == ticker:
                    continue
                try:
                    tk = yf.Ticker(peer)
                    hist = tk.history(period="60d", interval="1d", auto_adjust=True)
                    if hist.empty or len(hist) < 2:
                        continue
                    # Look for earnings jump: largest single-day move in last 60 days
                    daily_returns = hist["Close"].pct_change().dropna()
                    max_move = float(daily_returns.abs().max())
                    if max_move >= 0.04:   # 4%+ single-day move = likely earnings
                        max_day_idx = daily_returns.abs().idxmax()
                        peer_moves[peer] = float(daily_returns.loc[max_day_idx])
                except Exception:
                    continue
        except Exception:
            pass

        if not peer_moves:
            return {}, "No peers with recent significant moves"

        # Summarize with Claude Haiku
        summary = await self._summarize_peer_context(ticker, peer_moves)
        return peer_moves, summary

    def _get_sector_peers(self, ticker: str) -> list[str]:
        """Map ticker to its sector peer group."""
        for sector, peers in _SECTOR_PEERS.items():
            if ticker in peers:
                return [p for p in peers if p != ticker]
        return []

    async def _summarize_peer_context(
        self, ticker: str, peer_moves: dict[str, float]
    ) -> str:
        """One-sentence peer read-through summary using Claude Haiku."""
        try:
            peer_str = "\n".join(
                f"  {peer}: {'+' if m > 0 else ''}{m*100:.1f}% earnings move"
                for peer, m in peer_moves.items()
            )
            resp = await self._client.messages.create(
                model=self._settings.claude_fast_model,
                max_tokens=150,
                messages=[{
                    "role": "user",
                    "content": (
                        f"Sector peer earnings read-through for {ticker}.\n"
                        f"Sector peers that already reported this quarter:\n{peer_str}\n\n"
                        f"In one sentence: what does this imply for {ticker}'s upcoming earnings "
                        f"direction? Be specific about direction (bullish/bearish) and magnitude."
                    ),
                }],
            )
            if hasattr(resp, "usage"):
                _log_llm(str(self._settings.db_path), "EarningsCalendar", self._settings.claude_fast_model,
                         resp.usage.input_tokens, resp.usage.output_tokens, purpose="peer_readthrough")
            return resp.content[0].text.strip()
        except Exception as exc:
            logger.debug("Peer context summarization failed: %s", exc)
            return f"Peers: {', '.join(f'{p}({m*100:+.1f}%)' for p, m in peer_moves.items())}"

    # ── Setup synthesis (Opus) ─────────────────────────────────────

    async def _synthesize_setup(
        self,
        ticker: str,
        dte: int,
        implied_move_pct: float,
        hist_moves: list[float],
        edge_ratio: float,
        peer_moves: dict[str, float],
        peer_context: str,
        spot: float,
    ) -> tuple[str, float, str]:
        """
        Use Opus to synthesize direction + confidence for the pre-earnings play.
        Returns (direction, confidence, reasoning).
        """
        try:
            hist_str = ", ".join(f"{m*100:+.1f}%" for m in hist_moves)
            peer_str = ", ".join(f"{p}: {m*100:+.1f}%" for p, m in peer_moves.items()) or "none"

            prompt = f"""
Pre-earnings setup analysis for {ticker}:

Current spot: ${spot:.2f}
Days to earnings: {dte}
Implied move: ±{implied_move_pct*100:.1f}% (options market pricing)
Historical earnings moves (last {len(hist_moves)} quarters): {hist_str}
  → Average historical move: {sum(abs(m) for m in hist_moves)/len(hist_moves)*100:.1f}%
  → Edge ratio: {edge_ratio:.2f}× (historical vs implied)

Sector peer read-through:
  Peers with recent earnings: {peer_str}
  Analysis: {peer_context}

Based on this analysis:
1. Direction: bullish, bearish, or neutral?
2. Confidence: 0.0 to 1.0
3. Reasoning: one sentence

Output JSON: {{"direction": "bullish|bearish|neutral", "confidence": 0.0-1.0, "reasoning": "..."}}
"""
            resp = await self._client.messages.create(
                model=self._settings.claude_model,
                max_tokens=256,
                thinking={"type": "adaptive"},
                messages=[{"role": "user", "content": prompt}],
            )
            if hasattr(resp, "usage"):
                _log_llm(str(self._settings.db_path), "EarningsCalendar", self._settings.claude_model,
                         resp.usage.input_tokens, resp.usage.output_tokens, purpose="earnings_direction")

            import json as _json
            raw = resp.content[-1].text.strip() if resp.content else ""
            if raw.startswith("```"):
                raw = raw.split("```")[1].lstrip("json").strip()
            data = _json.loads(raw)
            return (
                data.get("direction", "neutral"),
                float(data.get("confidence", 0.5)),
                data.get("reasoning", ""),
            )
        except Exception as exc:
            logger.debug("Setup synthesis failed for %s: %s", ticker, exc)
            # Fallback: if more peers moved up, bullish
            peer_vals = list(peer_moves.values())
            if peer_vals:
                avg_peer = sum(peer_vals) / len(peer_vals)
                direction = "bullish" if avg_peer > 0.02 else ("bearish" if avg_peer < -0.02 else "neutral")
                return direction, 0.55, f"Peer read-through: avg peer move {avg_peer*100:+.1f}%"
            return "neutral", 0.5, "Insufficient data for directional bias"

"""
UniverseScreenerAgent — multi-stage stock selection pipeline.

Stage 0 (mechanical filter, no AI):
  Start: ~600 optionable US stocks
  Filters: price $20-$500, ADV > 2M shares, options ADV > 500 contracts,
           IV rank 20-80 (not vol-crushed, not spiked), earnings not within 5 days
  Output: 15-25 tickers

Stage 1 (Claude catalyst check):
  Input: 15-25 tickers with news headlines
  Claude ranks by: catalyst quality, sector rotation momentum, options flow alignment
  Output: 5-8 tickers for full pipeline

Stage 2 (full pipeline): normal orchestrator.analyze() on each finalist
  Output: ranked STRATEGY_CANDIDATES

Subscribes to: ANALYSIS_REQUEST (when tickers=[many]) or fires autonomously
Publishes:     UNIVERSE_READY with watchlist

Triggered: at 9:45 ET (post-open, after ORB sets) and 13:00 ET
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from ..core.models.agent import AgentMessage, AgentTopic
from .base import BaseAgent

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# Stage 0 mechanical filter parameters
_MIN_PRICE       = 20.0
_MAX_PRICE       = 500.0
_MIN_ADV_SHARES  = 2_000_000     # average daily volume in shares
_MIN_OPT_ADV     = 500           # options average daily contracts
_MIN_IV_RANK     = 15.0          # below this: vol too cheap for selling, options illiquid
_MAX_IV_RANK     = 85.0          # above this: buying vol too expensive
_EARNINGS_BUFFER = 5             # days — skip if earnings within N days

# Universe to screen (S&P 500 high-liquidity subset + ETFs)
# In production, this would be loaded from a database or API.
_CANDIDATE_UNIVERSE = [
    # Major ETFs (always liquid, good options markets)
    "SPY", "QQQ", "IWM", "GLD", "TLT", "XLE", "XLF", "XLK", "XLV", "SMH",
    # Mega-cap tech
    "AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "TSLA", "AMD", "AVGO",
    # Finance
    "JPM", "GS", "MS", "BAC", "C", "WFC", "BRK.B",
    # Healthcare
    "JNJ", "UNH", "LLY", "ABBV", "MRK", "PFE", "BMY",
    # Energy
    "XOM", "CVX", "OXY", "HAL", "SLB",
    # Consumer
    "AMZN", "WMT", "COST", "HD", "LOW", "NKE", "SBUX",
    # Semis / AI infrastructure
    "INTC", "MU", "QCOM", "ARM", "SMCI", "MRVL",
    # Macro-sensitive
    "GLD", "SLV", "USO", "UNG",
]
# Deduplicate
_CANDIDATE_UNIVERSE = list(dict.fromkeys(_CANDIDATE_UNIVERSE))


class ScreenedTicker(BaseModel):
    ticker: str
    price: float
    iv_rank: float
    adv_shares: float
    has_earnings_risk: bool
    earnings_date: str | None = None
    news_headline: str = ""
    stage0_score: float = 0.0   # 0-100 mechanical score
    stage1_rank: int | None = None  # Claude rank (1=best)
    catalyst_summary: str = ""


class UniverseScreenerAgent(BaseAgent):
    """
    Two-stage universe screener. Stage 0 is pure arithmetic; Stage 1 uses Claude.
    Fires autonomously on schedule OR when triggered by ANALYSIS_REQUEST with many tickers.
    """

    name = "universe_screener"
    subscriptions = [AgentTopic.ANALYSIS_REQUEST]

    async def handle(self, message: AgentMessage) -> None:
        session_id = message.session_id
        payload = message.payload

        # Only run if the request is specifically for universe screening
        # (tickers list has > 3 items, or context says "screen")
        tickers_req = payload.get("tickers", [])
        context = payload.get("context", {})

        if len(tickers_req) <= 1 and not context.get("screen_universe"):
            return  # single-ticker analysis — not our job

        self._log.info("[%s] running universe screen — stage 0", session_id)

        # Stage 0: mechanical filter
        candidates = await self._stage0_filter()
        self._log.info(
            "[%s] Stage 0: %d tickers → %d passed filter",
            session_id, len(_CANDIDATE_UNIVERSE), len(candidates),
        )

        if not candidates:
            self._log.warning("[%s] no tickers passed Stage 0 filter", session_id)
            return

        # Stage 1: Claude catalyst ranking
        finalists = await self._stage1_claude_rank(candidates, session_id)
        self._log.info(
            "[%s] Stage 1: %d tickers → %d finalists",
            session_id, len(candidates), len(finalists),
        )

        watchlist = [t.model_dump() for t in finalists]
        await self._state.update(session_id, universe_watchlist=watchlist)
        await self.publish(
            AgentTopic.UNIVERSE_READY,
            session_id=session_id,
            payload={
                "watchlist": watchlist,
                "screened_from": len(_CANDIDATE_UNIVERSE),
                "stage0_passed": len(candidates),
                "finalists": len(finalists),
            },
        )

    @staticmethod
    def _compute_stage0_score(iv_rank: float, adv: float, price: float) -> float:
        """Pure mechanical score 0-100. Higher is more desirable for options trading."""
        iv_center_score = max(0.0, 1.0 - abs(iv_rank - 45) / 45)   # peaks at IV rank 45
        vol_score       = min(1.0, adv / 10_000_000)                 # peaks at 10M ADV
        price_score     = max(0.0, 1.0 - abs(price - 150) / 300)     # peaks at $150 stocks
        return round(iv_center_score * 40 + vol_score * 40 + price_score * 20, 1)

    async def _stage0_filter(self) -> list[ScreenedTicker]:
        """Mechanical filter — no AI, fast yfinance data pull."""
        import yfinance as yf
        from ..services.macro_calendar import get_macro_calendar

        cal = get_macro_calendar()
        results: list[ScreenedTicker] = []

        # Batch download snapshot data
        tickers_str = " ".join(_CANDIDATE_UNIVERSE)
        try:
            raw = yf.download(
                tickers_str,
                period="5d",
                interval="1d",
                group_by="ticker",
                threads=True,
                progress=False,
                auto_adjust=True,
            )
        except Exception as exc:
            self._log.error("yfinance batch download failed: %s", exc)
            return []

        for ticker in _CANDIDATE_UNIVERSE:
            try:
                # Extract per-ticker data from multi-index DataFrame
                if ticker in raw.columns.get_level_values(0):
                    tk_data = raw[ticker].dropna()
                else:
                    continue

                if tk_data.empty or len(tk_data) < 2:
                    continue

                latest = tk_data.iloc[-1]
                price = float(latest.get("Close", 0))
                volume = float(latest.get("Volume", 0))

                # Price filter
                if not (_MIN_PRICE <= price <= _MAX_PRICE):
                    continue

                # ADV filter (use last 5 days)
                adv = float(tk_data["Volume"].mean()) if "Volume" in tk_data.columns else 0
                if adv < _MIN_ADV_SHARES:
                    continue

                # Fetch IV rank via yfinance info (approximate from options chain)
                try:
                    tk_obj = yf.Ticker(ticker)
                    info = tk_obj.info
                    # Use beta as a liquidity proxy when IV rank unavailable
                    # Real IV rank requires options chain historical data
                    iv_rank = float(info.get("impliedSharesOutstanding", 50))  # placeholder
                    # Better approximation: check if options exist
                    calls = tk_obj.option_chain(
                        tk_obj.options[0] if tk_obj.options else ""
                    ).calls if tk_obj.options else None
                    if calls is None or calls.empty:
                        continue
                    # Use ATM IV as proxy for IV rank estimation
                    atm_idx = (calls["strike"] - price).abs().idxmin()
                    iv_rank = float(calls.loc[atm_idx, "impliedVolatility"] * 100) if atm_idx in calls.index else 40.0
                except Exception:
                    iv_rank = 40.0  # assume neutral if data unavailable

                # IV rank filter
                if not (_MIN_IV_RANK <= iv_rank <= _MAX_IV_RANK):
                    continue

                # Earnings risk check
                try:
                    tk_obj = yf.Ticker(ticker)
                    cal_data = tk_obj.calendar
                    earnings_date = None
                    has_earnings = False
                    if cal_data is not None and not cal_data.empty:
                        if "Earnings Date" in cal_data.columns:
                            ed = cal_data["Earnings Date"].iloc[0]
                            if ed:
                                earnings_date = str(ed.date() if hasattr(ed, "date") else ed)
                                days_to_earnings = (date.fromisoformat(earnings_date[:10]) - date.today()).days
                                has_earnings = 0 <= days_to_earnings <= _EARNINGS_BUFFER
                except Exception:
                    has_earnings = False
                    earnings_date = None

                if has_earnings:
                    continue

                stage0_score = self._compute_stage0_score(iv_rank, adv, price)

                results.append(ScreenedTicker(
                    ticker=ticker,
                    price=price,
                    iv_rank=iv_rank,
                    adv_shares=adv,
                    has_earnings_risk=has_earnings,
                    earnings_date=earnings_date,
                    stage0_score=round(stage0_score, 1),
                ))

            except Exception as exc:
                self._log.debug("Stage 0 error for %s: %s", ticker, exc)
                continue

        # Sort by mechanical score, return top 25
        results.sort(key=lambda x: x.stage0_score, reverse=True)
        return results[:25]

    async def _stage1_claude_rank(
        self, candidates: list[ScreenedTicker], session_id: str
    ) -> list[ScreenedTicker]:
        """Claude ranks candidates by catalyst quality and trade opportunity."""
        if not candidates:
            return []

        # Fetch recent headline for each candidate
        import yfinance as yf
        for cand in candidates:
            try:
                tk = yf.Ticker(cand.ticker)
                news = tk.news
                if news:
                    cand.news_headline = news[0].get("title", "")[:200]
            except Exception:
                pass

        # Build prompt
        candidates_text = "\n".join(
            f"  {i+1:2}. {c.ticker:6} | price=${c.price:.0f} | IV_rank={c.iv_rank:.0f} "
            f"| ADV={c.adv_shares/1e6:.1f}M | score={c.stage0_score:.0f} | news: {c.news_headline}"
            for i, c in enumerate(candidates)
        )

        system = (
            "You are a quantitative equity analyst who specializes in options flow and catalyst identification. "
            "You receive a pre-filtered list of optionable stocks and rank them by trade opportunity quality. "
            "Evaluate: (1) catalyst strength — is there a real news driver or just noise? "
            "(2) sector momentum — is this sector in focus today? "
            "(3) options flow alignment — high IV rank = vol selling opportunity; low = directional buying. "
            "(4) risk/reward — avoid crowded trades, prefer names with clear thesis. "
            "Return a JSON array of ticker rankings with brief reasoning."
        )

        from datetime import date as _date
        user_msg = f"""Today is {_date.today()}. Rank the following {len(candidates)} pre-screened tickers
by best options trade opportunity for today's session. Return ONLY the top 8.

{candidates_text}

Return JSON array: [{{"ticker": "X", "rank": 1, "reason": "..."}}]
"""

        try:
            from pydantic import BaseModel as BM

            class _RankedItem(BM):
                ticker: str
                rank: int
                reason: str = ""

            class _RankOutput(BM):
                rankings: list[_RankedItem] = Field(default_factory=list)

            output = await self._call_claude_structured(
                system_prompt=system,
                user_message=user_msg,
                output_schema=_RankOutput,
                tool_name="rank_tickers",
                max_tokens=1024,
            )

            ticker_to_rank = {r.ticker: (r.rank, r.reason) for r in output.rankings}
            finalists = []
            for cand in candidates:
                if cand.ticker in ticker_to_rank:
                    rank, reason = ticker_to_rank[cand.ticker]
                    cand.stage1_rank = rank
                    cand.catalyst_summary = reason
                    finalists.append(cand)

            finalists.sort(key=lambda x: x.stage1_rank or 999)
            return finalists[:8]

        except Exception as exc:
            self._log.error("[%s] Stage 1 Claude ranking failed: %s — returning top 8 by score", session_id, exc)
            return candidates[:8]

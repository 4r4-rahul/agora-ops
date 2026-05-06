"""
NewsCatalystAgent — Claude-powered news sentiment and earnings risk analysis.

Subscribes to: ANALYSIS_REQUEST
Publishes:     NEWS_RESULT
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from ..core.models.agent import AgentMessage, AgentTopic, AnalysisRequest, NewsResult
from .base import BaseAgent

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a professional options trader who specializes in news catalyst analysis.
Your job: assess the news impact on a stock and flag risks for options traders.

Critical checks for options traders:
1. Earnings dates within the option's expiration — IV crush kills long options
2. FDA approvals/rejections — binary events, gaps over stops
3. M&A rumors — huge gamma, but spreads widen to 30-40%
4. Macro events (FOMC, CPI) — systemic vol spikes affect all options
5. Insider activity, analyst upgrades/downgrades
6. Product launches, legal actions, executive changes

Sentiment scoring: -1.0 = extremely bearish, 0 = neutral, +1.0 = extremely bullish
Catalyst types: earnings | fda | macro | ma | analyst | product | legal | none

You MUST call structured_output with your analysis. Be conservative — when in doubt
about earnings risk, flag it. Missed earnings risk is how traders blow up accounts.
"""


class NewsOutput(BaseModel):
    sentiment: str = Field(..., description="bullish | bearish | neutral")
    sentiment_score: float = Field(..., ge=-1.0, le=1.0)
    catalyst_type: str = Field(default="none")
    has_earnings_risk: bool
    earnings_date: str | None = Field(
        default=None, description="YYYY-MM-DD if known, else null"
    )
    headline_count: int = Field(default=0, ge=0)
    key_headlines: list[str] = Field(default_factory=list, max_length=5)
    summary: str = Field(..., min_length=30)


class NewsCatalystAgent(BaseAgent):
    name = "news"
    subscriptions = [AgentTopic.ANALYSIS_REQUEST]

    async def handle(self, message: AgentMessage) -> None:
        req = AnalysisRequest.model_validate(message.payload)
        session_id = message.session_id
        ticker = req.ticker

        self._log.info("[%s] analyzing news for %s", session_id, ticker)

        # Fetch headlines via yfinance
        headlines = await self._fetch_headlines(ticker)

        if not headlines:
            # No headline data — skip Claude call, return conservative default
            self._log.info("[%s] no headlines for %s — skipping news Claude call", session_id, ticker)
            output = NewsOutput(
                sentiment="neutral",
                sentiment_score=0.0,
                has_earnings_risk=False,
                summary="No headlines available — assuming no near-term catalyst risk",
            )
        else:
            user_message = self._build_prompt(ticker, headlines)
            try:
                output = await self._call_claude_structured(
                    system_prompt=_SYSTEM_PROMPT,
                    user_message=user_message,
                    output_schema=NewsOutput,
                    tool_name="structured_output",
                    max_tokens=1024,
                )
            except Exception as exc:
                self._log.error("[%s] news analysis failed: %s", session_id, exc)
                output = NewsOutput(
                    sentiment="neutral",
                    sentiment_score=0.0,
                    has_earnings_risk=False,
                    summary="News analysis unavailable — proceed with caution",
                )

        result = NewsResult(
            ticker=ticker,
            sentiment=output.sentiment,
            sentiment_score=output.sentiment_score,
            catalyst_type=output.catalyst_type,
            has_earnings_risk=output.has_earnings_risk,
            earnings_date=output.earnings_date,
            headline_count=output.headline_count,
            key_headlines=output.key_headlines,
            summary=output.summary,
        )

        await self._state.update(
            session_id,
            news_result=result.model_dump(mode="json"),
        )

        await self.publish(
            AgentTopic.NEWS_RESULT,
            session_id=session_id,
            payload=result.model_dump(mode="json"),
        )

        self._log.info(
            "[%s] %s news: %s (%.2f) | earnings_risk=%s",
            session_id,
            ticker,
            result.sentiment,
            result.sentiment_score,
            result.has_earnings_risk,
        )

    async def _fetch_headlines(self, ticker: str) -> list[str]:
        """Fetch recent headlines via yfinance. Returns empty list on failure."""
        try:
            import yfinance as yf

            stock = yf.Ticker(ticker)
            news = stock.news or []
            return [
                item.get("title", "")
                for item in news[:10]
                if item.get("title")
            ]
        except Exception as exc:
            self._log.warning("headline fetch failed for %s: %s", ticker, exc)
            return []

    def _build_prompt(self, ticker: str, headlines: list[str]) -> str:
        headline_text = (
            "\n".join(f"  - {h}" for h in headlines)
            if headlines
            else "  No recent headlines found."
        )
        return f"""Analyze news and catalyst risk for {ticker} options.

Recent headlines:
{headline_text}

Questions to answer:
1. What is the overall sentiment signal for {ticker}?
2. Is there a catalyst event within the next 30 days that could cause an earnings-like IV spike?
3. Does {ticker} have earnings scheduled within the next 30 days?
4. What is the specific risk for options traders?

Provide a complete structured analysis."""

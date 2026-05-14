"""
PriceTargetAgent — analyst consensus + peer regression → bull/base/bear price scenarios.

The CSCO lesson: we had no price target. We picked strikes based on delta,
not where the stock was actually going. Cisco's consensus PT was $65, but after
a 17% EPS beat + AI infrastructure upgrade cycle, the market repriced to $120 within hours.

This agent provides:
  1. Analyst consensus: mean/high/low PT from yfinance
  2. Sector peer regression: if NVDA/MSFT re-rated after AI earnings, apply multiplier
  3. Bull/base/bear scenarios: 3 price targets + probabilities
  4. Strike anchoring: recommended strikes for the trade based on scenarios

Used by: StrategyRulesEngine for earnings-event trade construction.
Used by: CEOAgent for "price target" section in reports.

Note: No external API required — yfinance provides analyst data.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date
from typing import Any

import anthropic

from ..core.config import AgoraSettings, get_settings

logger = logging.getLogger(__name__)


@dataclass
class PriceScenario:
    label: str          # "bull" | "base" | "bear"
    target: float
    probability: float  # 0-1
    catalyst: str       # what drives this scenario
    return_pct: float   # vs current spot


@dataclass
class PriceTargetAnalysis:
    ticker: str
    spot: float
    analyst_mean_pt: float | None
    analyst_high_pt: float | None
    analyst_low_pt: float | None
    analyst_count: int
    scenarios: list[PriceScenario]
    recommended_call_strike: float | None   # for bull play
    recommended_put_strike: float | None    # for bear play
    reasoning: str


class PriceTargetAgent:
    """
    Synchronous-first: `get_price_target(ticker, spot)` can be called inline.
    Async Claude synthesis runs when sector intelligence is available.
    """

    def __init__(
        self,
        settings: AgoraSettings | None = None,
        sector_intel_agent: Any = None,   # SectorIntelligenceAgent
    ) -> None:
        self._settings     = settings or get_settings()
        self._sector_intel = sector_intel_agent
        self._client       = anthropic.AsyncAnthropic(api_key=self._settings.anthropic_api_key)
        # Cache: ticker → analysis (refreshed daily)
        self._cache: dict[str, tuple[date, PriceTargetAnalysis]] = {}

    async def get_price_target(self, ticker: str, spot: float) -> PriceTargetAnalysis | None:
        """
        Full price target analysis. Called pre-trade for earnings setups.
        Cached daily — first call fetches, subsequent calls return cached.
        """
        today = date.today()
        if ticker in self._cache:
            cached_date, cached_analysis = self._cache[ticker]
            if cached_date == today:
                return cached_analysis

        try:
            analysis = await self._analyze(ticker, spot)
            if analysis:
                self._cache[ticker] = (today, analysis)
            return analysis
        except Exception as exc:
            logger.error("PriceTargetAgent failed for %s: %s", ticker, exc)
            return None

    async def _analyze(self, ticker: str, spot: float) -> PriceTargetAnalysis | None:
        # 1. Fetch analyst consensus
        analyst_mean, analyst_high, analyst_low, analyst_count = await self._fetch_analyst_targets(ticker)

        # 2. Get sector intel read-through
        sector_intel = None
        if self._sector_intel:
            sector_intel = self._sector_intel.get_intelligence(ticker)

        # 3. Build scenarios with Claude
        scenarios, reasoning = await self._build_scenarios(
            ticker, spot, analyst_mean, analyst_high, analyst_low, sector_intel
        )

        if not scenarios:
            return None

        # 4. Derive strike recommendations
        bull_scenario = next((s for s in scenarios if s.label == "bull"), None)
        bear_scenario = next((s for s in scenarios if s.label == "bear"), None)

        # Strike = 1 standard deviation OTM from the scenario target
        # For calls: slightly OTM from base case (not the full bull target)
        base_scenario = next((s for s in scenarios if s.label == "base"), None)
        recommended_call = None
        recommended_put  = None
        if base_scenario and bull_scenario:
            # Call strike: between base and bull (capture upside without overpaying)
            recommended_call = round((base_scenario.target + spot) / 2 / 2.5) * 2.5
        if base_scenario and bear_scenario:
            recommended_put = round((bear_scenario.target + spot) / 2 / 2.5) * 2.5

        return PriceTargetAnalysis(
            ticker=ticker,
            spot=spot,
            analyst_mean_pt=analyst_mean,
            analyst_high_pt=analyst_high,
            analyst_low_pt=analyst_low,
            analyst_count=analyst_count,
            scenarios=scenarios,
            recommended_call_strike=recommended_call,
            recommended_put_strike=recommended_put,
            reasoning=reasoning,
        )

    async def _fetch_analyst_targets(
        self, ticker: str
    ) -> tuple[float | None, float | None, float | None, int]:
        """Fetch analyst price targets from yfinance."""
        try:
            import yfinance as yf
            info = yf.Ticker(ticker).info or {}
            mean = float(info.get("targetMeanPrice") or 0) or None
            high = float(info.get("targetHighPrice") or 0) or None
            low  = float(info.get("targetLowPrice") or 0) or None
            count = int(info.get("numberOfAnalystOpinions") or 0)
            return mean, high, low, count
        except Exception:
            return None, None, None, 0

    async def _build_scenarios(
        self,
        ticker: str,
        spot: float,
        analyst_mean: float | None,
        analyst_high: float | None,
        analyst_low:  float | None,
        sector_intel: Any,
    ) -> tuple[list[PriceScenario], str]:
        """Use Opus to build bull/base/bear scenarios anchored to data."""
        try:
            intel_str = ""
            if sector_intel:
                intel_str = (
                    f"\nSector read-through ({sector_intel.sector}):\n"
                    f"  Peers avg beat: {(sector_intel.avg_peer_beat_pct or 0)*100:+.1f}%\n"
                    f"  Peers avg move: {(sector_intel.avg_peer_stock_move or 0)*100:+.1f}%\n"
                    f"  Beat rate: {(sector_intel.beat_rate or 0)*100:.0f}% of peers beat estimates\n"
                    f"  Read-through: {sector_intel.regression_note}"
                )

            analyst_str = ""
            if analyst_mean:
                analyst_str = (
                    f"\nAnalyst consensus ({analyst_mean} analysts):\n"
                    f"  Mean PT: ${analyst_mean:.2f} ({(analyst_mean-spot)/spot*100:+.1f}%)\n"
                    f"  High PT: ${analyst_high:.2f}\n"
                    f"  Low PT:  ${analyst_low:.2f}"
                )

            prompt = f"""
Price target scenario analysis for {ticker}. Current spot: ${spot:.2f}
{analyst_str}
{intel_str}

Build 3 price scenarios for the next 30 days:
1. Bull case: what drives above-consensus performance?
2. Base case: what is the most likely 30-day price?
3. Bear case: what drives underperformance?

For each scenario provide: target_price, probability (must sum to 1.0), catalyst (1 phrase).

Output JSON:
{{
  "scenarios": [
    {{"label": "bull", "target": 0.0, "probability": 0.0, "catalyst": "..."}},
    {{"label": "base", "target": 0.0, "probability": 0.0, "catalyst": "..."}},
    {{"label": "bear", "target": 0.0, "probability": 0.0, "catalyst": "..."}}
  ],
  "reasoning": "one sentence on the key risk/opportunity"
}}
"""
            resp = await self._client.messages.create(
                model=self._settings.claude_model,
                max_tokens=512,
                thinking={"type": "adaptive"},
                messages=[{"role": "user", "content": prompt}],
            )

            import json as _json
            # Get last text block
            raw = ""
            for block in reversed(resp.content):
                if hasattr(block, "text"):
                    raw = block.text.strip()
                    break
            if raw.startswith("```"):
                raw = raw.split("```")[1].lstrip("json").strip()
            data = _json.loads(raw)

            scenarios = [
                PriceScenario(
                    label=s["label"],
                    target=float(s["target"]),
                    probability=float(s["probability"]),
                    catalyst=s.get("catalyst", ""),
                    return_pct=(float(s["target"]) - spot) / spot,
                )
                for s in data.get("scenarios", [])
            ]
            return scenarios, data.get("reasoning", "")
        except Exception as exc:
            logger.debug("PriceTarget scenario build failed for %s: %s", ticker, exc)
            # Fallback: simple percentage scenarios
            if analyst_mean:
                upside = (analyst_mean - spot) / spot
                scenarios = [
                    PriceScenario("bull", analyst_mean * 1.10, 0.25, "Analyst PT + re-rating", upside * 1.10),
                    PriceScenario("base", analyst_mean, 0.50, "Analyst consensus", upside),
                    PriceScenario("bear", analyst_mean * 0.85, 0.25, "Miss + guidance cut", upside * 0.85),
                ]
                return scenarios, f"Analyst mean PT ${analyst_mean:.2f}"
            return [], ""

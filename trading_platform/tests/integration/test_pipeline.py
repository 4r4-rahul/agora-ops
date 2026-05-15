"""
Integration test — full analysis pipeline, mocked Claude + yfinance.

Patches anthropic.AsyncAnthropic BEFORE agents are instantiated so no real
HTTP connections are created. Mocked responses are routed by matching a unique
substring of each agent's system prompt.

No API keys or network calls required.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from trading_platform.core.bus import MessageBus
from trading_platform.core.config import Settings
from trading_platform.core.models.market import Bar, MarketSnapshot
from trading_platform.core.models.trade import TradeDecision
from trading_platform.core.state import SharedStateStore
from trading_platform.agents.conviction import ConvictionAgent
from trading_platform.agents.execution import ExecutionAgent
from trading_platform.agents.journal import TradeJournalAgent
from trading_platform.agents.market_data import MarketDataAgent
from trading_platform.agents.news import NewsCatalystAgent
from trading_platform.agents.options_strategy import OptionsStrategyAgent
from trading_platform.agents.orchestrator import OrchestratorAgent
from trading_platform.agents.regime import RegimeAgent
from trading_platform.agents.reviewer import ReviewerAgent
from trading_platform.agents.risk_manager import RiskManagerAgent
from trading_platform.agents.technical import TechnicalAnalysisAgent


# ── Canned Claude payloads ────────────────────────────────────────────
# Keyed by a unique substring of each agent's _SYSTEM_PROMPT (lower-cased).

_CANNED: dict[str, dict[str, Any]] = {
    "quantitative market regime": {
        "regime": "bull_trend",
        "confidence": 0.82,
        "vix_trend": "flat",
        "trend_direction": "bullish",
        "breadth_score": 0.65,
        "reasoning": (
            "SPY is above all three key moving averages (20/50/200). "
            "VIX at 16.5 is well below danger threshold. "
            "RSI at 58 indicates momentum without being overbought. "
            "Breadth is positive with majority of S&P 500 above SMA50."
        ),
        "regime_suitable_strategies": ["bull_call_spread", "cash_secured_put"],
    },
    "news catalyst": {
        "sentiment": "neutral",
        "sentiment_score": 0.1,
        "catalyst_type": "none",
        "has_earnings_risk": False,
        "earnings_date": None,
        "headline_count": 2,
        "key_headlines": ["SPY hits new high", "Fed holds rates steady"],
        "summary": (
            "No significant negative catalysts. Fed policy is supportive "
            "and no earnings within the option expiration window."
        ),
    },
    "professional options trader": {
        "ticker": "SPY",
        "direction": "bullish",
        "thesis": (
            "SPY is in a confirmed bull trend above all key SMAs (20/50/200). "
            "VIX is low at 16.5, regime confirms bull trend. "
            "IV rank at 42 is fair for buying a spread — not overpaying. "
            "Bull call spread targets a 1-2% move higher over 14 days."
        ),
        "strategy": "bull_call_spread",
        "legs": [
            {"option_type": "call", "strike": 503.0, "expiration_dte": 14,
             "action": "buy", "quantity": 1},
            {"option_type": "call", "strike": 508.0, "expiration_dte": 14,
             "action": "sell", "quantity": 1},
        ],
        "strike_selection_logic": (
            "Buy ATM call at 503, sell 1% OTM call at 508 to cap cost. "
            "Max gain if SPY rallies 1.1% by expiration."
        ),
        "expiration_dte": 14,
        "entry_trigger": "SPY breaks and holds above 503.50 on 15m close with vol >1.2x avg",
        "entry_price": 2.20,
        "stop_loss": 1.10,
        "stop_loss_logic": "Exit at 50% of debit — hard rule, no exceptions",
        "profit_target": 4.00,
        "profit_target_logic": "Exit at 80% of max profit ($5 - $2.20 = $2.80 gain)",
        "max_loss_dollars": 220.0,
        "max_gain_dollars": 360.0,
        "reward_risk_ratio": 1.64,
        "contracts": 1,
        "position_size_dollars": 220.0,
        "iv_crush_risk": "low",
        "earnings_risk": "none",
        "regime_confirms": True,
        "regime_notes": "bull_trend confirmed — debit spreads favored",
    },
    "systematic risk manager": {
        "approved": True,
        "veto_reasons": [],
        "portfolio_delta": 0.28,
        "correlation_risk": "low",
        "tail_risk_notes": "Gap risk minimal — no earnings or binary events before expiration.",
        "liquidity_assessment": "SPY options are among the most liquid. No fill risk.",
        "summary": (
            "Trade passes all risk checks. Position size $220 well within limits. "
            "Delta 0.28 adds modest long exposure. No correlation or event risks."
        ),
    },
    "senior options trading desk reviewer": {
        "decision": "APPROVED",
        "confidence": 0.78,
        "approval_notes": (
            "Thesis is sound: SPY above all SMAs, bull regime confirmed, IV rank 42 fair. "
            "Entry trigger is specific. R/R 1.27:1 acceptable for high-probability setup "
            "with defined max loss of $220. Proceed to human approval gate."
        ),
        "rejection_reasons": [],
        "suggested_modifications": [],
        "watchlist_conditions": "",
    },
}


def _tool_response(payload: dict[str, Any]) -> MagicMock:
    """Build a fake messages.create response with a tool_use block."""
    block = MagicMock()
    block.type = "tool_use"
    block.name = "structured_output"
    block.input = payload  # plain dict — Pydantic can validate this

    resp = MagicMock()
    resp.content = [block]
    return resp


def _make_mock_client(canned: dict[str, dict[str, Any]] | None = None) -> MagicMock:
    """
    Build a mock AsyncAnthropic client.
    Routes responses by matching a keyword against the system prompt text.
    """
    lookup = canned if canned is not None else _CANNED

    async def _create(**kwargs) -> MagicMock:
        system = kwargs.get("system", [])
        text = " ".join(
            (s.get("text", "") if isinstance(s, dict) else str(s))
            for s in (system if isinstance(system, list) else [system])
        ).lower()
        for keyword, payload in lookup.items():
            if keyword in text:
                return _tool_response(payload)
        raise RuntimeError(f"No canned response matched system prompt snippet: {text[:100]!r}")

    client = MagicMock()
    client.messages.create = AsyncMock(side_effect=_create)
    return client


# ── Fixtures ──────────────────────────────────────────────────────────

@pytest.fixture
def settings() -> Settings:
    return Settings(
        anthropic_api_key="sk-ant-test-key",
        trading_mode="paper",
        require_human_approval=False,  # skip stdin prompt
        account_size=25_000.0,
        min_reward_risk_ratio=1.5,
    )


@pytest.fixture
def bus() -> MessageBus:
    return MessageBus()


@pytest.fixture
def state_store() -> SharedStateStore:
    return SharedStateStore()


@pytest.fixture
def spy_snapshot() -> MarketSnapshot:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    bars = [
        Bar(
            ts=now - timedelta(days=i),
            open=500.0 + i * 0.5,
            high=505.0 + i * 0.5,
            low=498.0 + i * 0.5,
            close=502.0 + i * 0.5,
            volume=50_000_000,
        )
        for i in range(30, 0, -1)
    ]
    return MarketSnapshot(
        ticker="SPY",
        timestamp=now,
        price=502.50,
        volume=45_000_000,
        day_open=499.0,
        day_high=504.0,
        day_low=497.0,
        prev_close=498.0,
        atr_14=3.50,
        rsi_14=58.0,
        sma_20=498.0,
        sma_50=490.0,
        sma_200=470.0,
        vix=16.5,
        iv_rank=42.0,
        iv_percentile=45.0,
        hist_vol_30=0.14,
        bars_daily=bars,
    )


@pytest.fixture
def mock_anthropic(settings):
    """
    Patch anthropic.AsyncAnthropic BEFORE any agent is instantiated.
    Agents call anthropic.AsyncAnthropic(...) in __init__, so this patch
    must be active when agents are created — achieved by using this fixture
    as a dependency of `all_agents`.
    """
    mock_client = _make_mock_client()
    with patch(
        "trading_platform.agents.base.anthropic.AsyncAnthropic",
        return_value=mock_client,
    ):
        yield mock_client


@pytest.fixture
def all_agents(bus, state_store, settings, mock_anthropic):
    """
    All agents, created INSIDE the anthropic patch context (via fixture dependency).
    """
    kwargs = {"bus": bus, "state_store": state_store, "settings": settings}
    return [
        MarketDataAgent(**kwargs),
        RegimeAgent(**kwargs),
        TechnicalAnalysisAgent(**kwargs),
        NewsCatalystAgent(**kwargs),
        ConvictionAgent(**kwargs),
        OptionsStrategyAgent(**kwargs),
        RiskManagerAgent(**kwargs),
        ReviewerAgent(**kwargs),
        ExecutionAgent(**kwargs),
        TradeJournalAgent(**kwargs),
    ]


# ── Shared yfinance mocks applied inside each test ────────────────────

def _yf_patches(spy_snapshot: MarketSnapshot):
    """Context manager that mocks yfinance calls for all tests."""
    return [
        patch(
            "trading_platform.agents.market_data.YFinanceProvider.get_snapshot",
            new_callable=AsyncMock,
            return_value=spy_snapshot,
        ),
        patch(
            "trading_platform.agents.news.NewsCatalystAgent._fetch_headlines",
            new_callable=AsyncMock,
            return_value=["SPY hits new high"],
        ),
    ]


# ── Tests ─────────────────────────────────────────────────────────────

class TestFullPipeline:

    @pytest.mark.asyncio
    async def test_pipeline_produces_recommendation(
        self, bus, state_store, settings, spy_snapshot, all_agents
    ):
        """Full pipeline end-to-end: fires ANALYSIS_REQUEST, waits for RECOMMENDATION_READY."""
        patches = _yf_patches(spy_snapshot)
        for p in patches:
            p.start()
        try:
            for agent in all_agents:
                await agent.start()

            orchestrator = OrchestratorAgent(bus=bus, state_store=state_store, settings=settings)
            result = await orchestrator.analyze("SPY", timeout=30.0)

            for agent in all_agents:
                await agent.stop()
        finally:
            for p in patches:
                p.stop()

        assert result.get("ticker") == "SPY",           f"wrong ticker: {result}"
        assert result.get("strategy") == "bull_call_spread"
        assert result.get("direction") == "bullish"
        assert result.get("max_loss_dollars") == 220.0
        assert result.get("reward_risk_ratio") == 1.64
        assert result.get("final_decision") == TradeDecision.PENDING_APPROVAL

    @pytest.mark.asyncio
    async def test_all_session_stages_populated(
        self, bus, state_store, settings, spy_snapshot, all_agents
    ):
        """Every pipeline stage must write its result to the shared state store."""
        patches = _yf_patches(spy_snapshot)
        for p in patches:
            p.start()
        try:
            for agent in all_agents:
                await agent.start()

            orchestrator = OrchestratorAgent(bus=bus, state_store=state_store, settings=settings)
            await orchestrator.analyze("SPY", timeout=30.0)

            for agent in all_agents:
                await agent.stop()
        finally:
            for p in patches:
                p.stop()

        sessions = await state_store.list_sessions(limit=10)
        assert sessions
        session = sessions[-1]

        assert session.market_snapshot is not None,       "market_snapshot missing"
        assert session.regime_result is not None,         "regime_result missing"
        assert session.technical_result is not None,      "technical_result missing"
        assert session.news_result is not None,           "news_result missing"
        assert session.strategy_candidates,               "strategy_candidates empty"
        assert session.risk_assessment is not None,       "risk_assessment missing"
        assert session.final_recommendation is not None,  "final_recommendation missing"
        assert session.status == "complete",              f"unexpected status: {session.status}"

    @pytest.mark.asyncio
    async def test_risk_manager_hard_veto_rejects_trade(
        self, bus, state_store, settings, spy_snapshot, mock_anthropic
    ):
        """R/R below the configured minimum must produce REJECTED. Reviewer not called."""
        bad_strategy = dict(_CANNED["professional options trader"])
        bad_strategy["reward_risk_ratio"] = 0.5
        bad_strategy["max_gain_dollars"] = 110.0

        canned_bad = dict(_CANNED, **{"professional options trader": bad_strategy})
        mock_client = _make_mock_client(canned=canned_bad)

        reviewer_calls = 0
        original = mock_client.messages.create

        async def _tracking(**kwargs):
            system = kwargs.get("system", [])
            text = " ".join(
                (s.get("text", "") if isinstance(s, dict) else str(s))
                for s in (system if isinstance(system, list) else [system])
            ).lower()
            if "senior options trading desk reviewer" in text:
                nonlocal reviewer_calls
                reviewer_calls += 1
            return await original(**kwargs)

        mock_client.messages.create = AsyncMock(side_effect=_tracking)

        # Patch again with the bad-strategy client
        with patch(
            "trading_platform.agents.base.anthropic.AsyncAnthropic",
            return_value=mock_client,
        ):
            kwargs = {"bus": bus, "state_store": state_store, "settings": settings}
            agents = [
                MarketDataAgent(**kwargs), RegimeAgent(**kwargs),
                TechnicalAnalysisAgent(**kwargs), NewsCatalystAgent(**kwargs),
                ConvictionAgent(**kwargs), OptionsStrategyAgent(**kwargs),
                RiskManagerAgent(**kwargs), ReviewerAgent(**kwargs),
                ExecutionAgent(**kwargs), TradeJournalAgent(**kwargs),
            ]

            patches = _yf_patches(spy_snapshot)
            for p in patches:
                p.start()
            try:
                for agent in agents:
                    await agent.start()

                orchestrator = OrchestratorAgent(
                    bus=bus, state_store=state_store, settings=settings
                )
                result = await orchestrator.analyze("SPY", timeout=30.0)

                for agent in agents:
                    await agent.stop()
            finally:
                for p in patches:
                    p.stop()

        assert result.get("final_decision") == TradeDecision.REJECTED, (
            f"Expected REJECTED, got {result.get('final_decision')}"
        )
        assert reviewer_calls == 0, (
            f"Reviewer was called {reviewer_calls}x despite hard-rule veto"
        )

    @pytest.mark.asyncio
    async def test_zero_bus_errors_on_clean_run(
        self, bus, state_store, settings, spy_snapshot, all_agents
    ):
        """Message bus must have zero handler errors after a clean pipeline run."""
        patches = _yf_patches(spy_snapshot)
        for p in patches:
            p.start()
        try:
            for agent in all_agents:
                await agent.start()

            orchestrator = OrchestratorAgent(bus=bus, state_store=state_store, settings=settings)
            await orchestrator.analyze("SPY", timeout=30.0)

            for agent in all_agents:
                await agent.stop()
        finally:
            for p in patches:
                p.stop()

        stats = bus.stats
        assert stats["errors"] == 0, f"Bus had {stats['errors']} handler errors"
        assert stats["published"] >= 7, f"Expected ≥7 messages, got {stats['published']}"

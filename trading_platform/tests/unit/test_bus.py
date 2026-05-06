"""Unit tests for the AsyncIO message bus."""

from __future__ import annotations

import asyncio
import pytest

from trading_platform.core.bus import MessageBus
from trading_platform.core.models.agent import AgentMessage, AgentTopic


@pytest.fixture
def bus() -> MessageBus:
    return MessageBus()


@pytest.fixture
def sample_message() -> AgentMessage:
    return AgentMessage.create(
        topic=AgentTopic.ANALYSIS_REQUEST,
        session_id="test-session-123",
        sender="orchestrator",
        payload={"ticker": "SPY"},
    )


class TestMessageBus:
    @pytest.mark.asyncio
    async def test_subscribe_and_receive(self, bus, sample_message):
        received = []

        async def handler(msg: AgentMessage):
            received.append(msg)

        bus.subscribe(AgentTopic.ANALYSIS_REQUEST, handler)
        count = await bus.publish(sample_message)

        assert count == 1
        assert len(received) == 1
        assert received[0].session_id == "test-session-123"

    @pytest.mark.asyncio
    async def test_no_subscribers_returns_zero(self, bus, sample_message):
        count = await bus.publish(sample_message)
        assert count == 0

    @pytest.mark.asyncio
    async def test_multiple_subscribers(self, bus, sample_message):
        counts = [0, 0]

        async def h1(msg): counts[0] += 1
        async def h2(msg): counts[1] += 1

        bus.subscribe(AgentTopic.ANALYSIS_REQUEST, h1)
        bus.subscribe(AgentTopic.ANALYSIS_REQUEST, h2)
        await bus.publish(sample_message)

        assert counts == [1, 1]

    @pytest.mark.asyncio
    async def test_unsubscribe(self, bus, sample_message):
        received = []

        async def handler(msg): received.append(msg)

        bus.subscribe(AgentTopic.ANALYSIS_REQUEST, handler)
        bus.unsubscribe(AgentTopic.ANALYSIS_REQUEST, handler)
        await bus.publish(sample_message)

        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_handler_exception_doesnt_block_others(self, bus, sample_message):
        received = []

        async def bad_handler(msg):
            raise RuntimeError("intentional error")

        async def good_handler(msg):
            received.append(msg)

        bus.subscribe(AgentTopic.ANALYSIS_REQUEST, bad_handler)
        bus.subscribe(AgentTopic.ANALYSIS_REQUEST, good_handler)
        count = await bus.publish(sample_message)

        # good_handler still ran
        assert len(received) == 1
        assert bus.stats["errors"] == 1

    @pytest.mark.asyncio
    async def test_topic_isolation(self, bus):
        market_received = []
        regime_received = []

        async def market_handler(msg): market_received.append(msg)
        async def regime_handler(msg): regime_received.append(msg)

        bus.subscribe(AgentTopic.MARKET_DATA_RESULT, market_handler)
        bus.subscribe(AgentTopic.REGIME_RESULT, regime_handler)

        market_msg = AgentMessage.create(
            topic=AgentTopic.MARKET_DATA_RESULT,
            session_id="s1",
            sender="market_data",
            payload={},
        )
        await bus.publish(market_msg)

        assert len(market_received) == 1
        assert len(regime_received) == 0

    @pytest.mark.asyncio
    async def test_stats_tracking(self, bus, sample_message):
        async def noop(msg): pass
        bus.subscribe(AgentTopic.ANALYSIS_REQUEST, noop)

        await bus.publish(sample_message)
        await bus.publish(sample_message)

        stats = bus.stats
        assert stats["published"] == 2
        assert stats["errors"] == 0

    @pytest.mark.asyncio
    async def test_publish_and_wait(self, bus):
        request = AgentMessage.create(
            topic=AgentTopic.ANALYSIS_REQUEST,
            session_id="wait-session",
            sender="test",
            payload={},
        )
        response = AgentMessage.create(
            topic=AgentTopic.MARKET_DATA_RESULT,
            session_id="wait-session",
            sender="market_data",
            payload={"price": 500.0},
        )

        async def fake_market_agent(msg: AgentMessage):
            await asyncio.sleep(0.01)
            await bus.publish(response)

        bus.subscribe(AgentTopic.ANALYSIS_REQUEST, fake_market_agent)

        result = await bus.publish_and_wait(
            request,
            response_topic=AgentTopic.MARKET_DATA_RESULT,
            session_id="wait-session",
            timeout=5.0,
        )
        assert result.payload["price"] == 500.0

    @pytest.mark.asyncio
    async def test_publish_and_wait_timeout(self, bus):
        msg = AgentMessage.create(
            topic=AgentTopic.ANALYSIS_REQUEST,
            session_id="timeout-session",
            sender="test",
            payload={},
        )
        with pytest.raises(asyncio.TimeoutError):
            await bus.publish_and_wait(
                msg,
                response_topic=AgentTopic.MARKET_DATA_RESULT,
                session_id="timeout-session",
                timeout=0.1,
            )

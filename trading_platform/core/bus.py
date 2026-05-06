"""
Typed AsyncIO pub/sub message bus.

Each topic has an asyncio.Queue per subscriber. Agents subscribe to topics
they care about and publish results back through the same bus.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Callable, Coroutine

from .models.agent import AgentMessage, AgentTopic

logger = logging.getLogger(__name__)

# Callback type: async function that receives a message
MessageHandler = Callable[[AgentMessage], Coroutine]


class MessageBus:
    """
    Lightweight in-process pub/sub bus.

    Usage:
        bus = MessageBus()

        # Subscribe
        async def handle_market(msg: AgentMessage):
            ...
        bus.subscribe(AgentTopic.MARKET_DATA_RESULT, handle_market)

        # Publish
        await bus.publish(AgentMessage.create(...))
    """

    def __init__(self) -> None:
        # topic → list of async handlers
        self._handlers: dict[str, list[MessageHandler]] = defaultdict(list)
        self._message_count: int = 0
        self._error_count: int = 0

    def subscribe(self, topic: AgentTopic | str, handler: MessageHandler) -> None:
        """Register an async handler for a topic."""
        self._handlers[str(topic)].append(handler)
        logger.debug("subscribed %s to %s", getattr(handler, "__qualname__", handler), topic)

    def unsubscribe(self, topic: AgentTopic | str, handler: MessageHandler) -> None:
        """Remove a handler from a topic (no-op if not subscribed)."""
        handlers = self._handlers.get(str(topic), [])
        try:
            handlers.remove(handler)
        except ValueError:
            pass

    async def publish(self, message: AgentMessage) -> int:
        """
        Deliver message to all subscribers of its topic.

        Returns the number of handlers invoked.
        """
        self._message_count += 1
        handlers = self._handlers.get(str(message.topic), [])

        if not handlers:
            logger.debug("no subscribers for topic %s", message.topic)
            return 0

        tasks = [asyncio.create_task(h(message)) for h in handlers]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        delivered = 0
        for result in results:
            if isinstance(result, Exception):
                self._error_count += 1
                logger.error(
                    "handler error on topic %s: %s",
                    message.topic,
                    result,
                    exc_info=result,
                )
            else:
                delivered += 1

        logger.debug(
            "published %s [session=%s] → %d/%d handlers",
            message.topic,
            message.session_id,
            delivered,
            len(handlers),
        )
        return delivered

    async def publish_and_wait(
        self,
        message: AgentMessage,
        response_topic: AgentTopic,
        session_id: str,
        timeout: float = 30.0,
    ) -> AgentMessage:
        """
        Publish a message and wait for the first response on response_topic
        matching the same session_id.
        """
        future: asyncio.Future[AgentMessage] = asyncio.get_event_loop().create_future()

        async def _capture(msg: AgentMessage) -> None:
            if msg.session_id == session_id and not future.done():
                future.set_result(msg)

        self.subscribe(response_topic, _capture)
        try:
            await self.publish(message)
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self.unsubscribe(response_topic, _capture)

    @property
    def stats(self) -> dict[str, int]:
        return {
            "published": self._message_count,
            "errors": self._error_count,
            "topics": len(self._handlers),
            "subscribers": sum(len(v) for v in self._handlers.values()),
        }

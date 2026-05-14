"""
AgentEventBus — in-process async pub/sub for lateral C-to-C communications.

Agents publish typed events; subscribers each get their own asyncio.Queue.
No external dependency — runs entirely within the AGORA event loop.

Defined event types:
  regime_changed        — CIO publishes; CTO subscribes (adjust strategy selection)
  kill_switch_tripped   — CRO publishes; CTO, COO subscribe (halt new orders)
  kill_switch_reset     — CRO publishes; CTO subscribes (resume trading)
  size_bias_changed     — CRO publishes; CTO subscribes (adjust sizing)
  daily_loss_warning    — CFO publishes at 60% / 85%; CRO, CTO subscribe
  profit_factor_low     — CFO publishes when PF < 1.0; CRO subscribes
  fill_rate_critical    — COO publishes; CTO subscribes (pause new entries)
  ibkr_disconnected     — COO publishes; CTO subscribes (halt order submission)
  ghost_fills_detected  — COO publishes; CEO subscribes
  macro_context_updated — CIO publishes; CTO subscribes (re-evaluate strategy)
  position_closed       — CTO publishes; CFO, RND subscribe (trade feedback)
  conviction_drift      — RND publishes; CTO subscribes (calibration alert)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

# All legal event types — enforced on publish to catch typos early
VALID_EVENTS = frozenset({
    "regime_changed",
    "kill_switch_tripped",
    "kill_switch_reset",
    "size_bias_changed",
    "daily_loss_warning",
    "profit_factor_low",
    "fill_rate_critical",
    "ibkr_disconnected",
    "ghost_fills_detected",
    "macro_context_updated",
    "position_closed",
    "conviction_drift",
})


@dataclass
class AgentEvent:
    event_type: str
    publisher:  str          # TITLE of the publishing agent
    payload:    dict[str, Any]
    ts:         str = field(default_factory=lambda: datetime.now(tz=ET).isoformat())


class AgentEventBus:
    """
    Simple in-process event bus. Each subscriber gets a dedicated asyncio.Queue
    so publish() is non-blocking and no subscriber can block another.
    """

    def __init__(self) -> None:
        # event_type → list of (subscriber_name, Queue)
        self._subs: dict[str, list[tuple[str, asyncio.Queue]]] = {}
        self._publish_count: int = 0

    def subscribe(self, subscriber_name: str, *event_types: str) -> asyncio.Queue:
        """
        Register for one or more event types. Returns a single Queue that
        receives ALL subscribed event types for this subscriber.

        Usage in __init__:
          self._event_q = bus.subscribe("CTO", "regime_changed", "kill_switch_tripped")
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        for et in event_types:
            self._subs.setdefault(et, []).append((subscriber_name, q))
        logger.debug("EventBus: %s subscribed to %s", subscriber_name, event_types)
        return q

    async def publish(self, event_type: str, publisher: str, payload: dict[str, Any]) -> None:
        """Publish an event to all subscribers. Non-blocking — drops if queue full."""
        if event_type not in VALID_EVENTS:
            logger.warning("EventBus: unknown event_type %r from %s — dropped", event_type, publisher)
            return
        event = AgentEvent(event_type=event_type, publisher=publisher, payload=payload)
        delivered = 0
        for name, q in self._subs.get(event_type, []):
            try:
                q.put_nowait(event)
                delivered += 1
            except asyncio.QueueFull:
                logger.warning("EventBus: queue full for %s — event %s dropped", name, event_type)
        self._publish_count += 1
        if delivered:
            logger.debug("EventBus: [%s] from %s → %d subscriber(s)", event_type, publisher, delivered)

    def get_stats(self) -> dict[str, Any]:
        return {
            "total_published":   self._publish_count,
            "subscriptions":     {et: [n for n, _ in subs] for et, subs in self._subs.items()},
        }

"""
BaseAgent — shared infrastructure for all agents in the platform.

Every intelligent agent inherits from here. Key features:
  • Typed pub/sub wiring through the MessageBus
  • _call_claude_structured() — calls Claude with tool_use to get typed Pydantic output
  • Prompt caching on the system prompt (cache_control: ephemeral)
  • Adaptive thinking enabled by default
  • Structured logging with session context
"""

from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from typing import Any, TypeVar

import anthropic
from pydantic import BaseModel

from ..core.bus import MessageBus
from ..core.config import Settings, get_settings
from ..core.models.agent import AgentMessage, AgentStatus, AgentTopic
from ..core.state import SharedStateStore

T = TypeVar("T", bound=BaseModel)

logger = logging.getLogger(__name__)


class BaseAgent(ABC):
    """
    Base class for all platform agents.

    Subclasses implement:
        - name: str — unique agent identifier
        - subscriptions: list[AgentTopic] — topics this agent listens to
        - handle(message) — process one incoming message
    """

    name: str = "base"
    subscriptions: list[AgentTopic] = []

    def __init__(
        self,
        bus: MessageBus,
        state_store: SharedStateStore,
        settings: Settings | None = None,
    ) -> None:
        self._bus = bus
        self._state = state_store
        self._settings = settings or get_settings()
        self._status = AgentStatus.IDLE
        self._client = anthropic.AsyncAnthropic(
            api_key=self._settings.anthropic_api_key
        )
        self._log = logging.getLogger(f"platform.agents.{self.name}")

    # ── Lifecycle ─────────────────────────────────────────────────

    async def start(self) -> None:
        """Register subscriptions and mark agent ready."""
        for topic in self.subscriptions:
            self._bus.subscribe(topic, self._dispatch)
        self._status = AgentStatus.IDLE
        self._log.info("%s started, subscribed to %s", self.name, self.subscriptions)

    async def stop(self) -> None:
        for topic in self.subscriptions:
            self._bus.unsubscribe(topic, self._dispatch)
        self._status = AgentStatus.STOPPED
        self._log.info("%s stopped", self.name)

    async def _dispatch(self, message: AgentMessage) -> None:
        """Internal handler — wraps handle() with logging and status tracking."""
        self._status = AgentStatus.RUNNING
        try:
            await self.handle(message)
        except Exception as exc:
            self._log.error(
                "unhandled error in %s [session=%s]: %s",
                self.name,
                message.session_id,
                exc,
                exc_info=True,
            )
            await self._publish_error(message.session_id, str(exc))
        finally:
            self._status = AgentStatus.IDLE

    @abstractmethod
    async def handle(self, message: AgentMessage) -> None:
        """Process one incoming message. Must be implemented by each agent."""

    # ── Publishing ────────────────────────────────────────────────

    async def publish(
        self,
        topic: AgentTopic,
        session_id: str,
        payload: dict[str, Any],
    ) -> None:
        msg = AgentMessage.create(
            topic=topic,
            session_id=session_id,
            sender=self.name,
            payload=payload,
        )
        await self._bus.publish(msg)

    async def _publish_error(self, session_id: str, error: str) -> None:
        await self.publish(
            AgentTopic.ERROR,
            session_id=session_id,
            payload={"agent": self.name, "error": error},
        )

    # ── Claude Integration ────────────────────────────────────────

    async def _call_claude_structured(
        self,
        system_prompt: str,
        user_message: str,
        output_schema: type[T],
        tool_name: str = "structured_output",
        extra_context: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
    ) -> T:
        """
        Call Claude and extract a structured Pydantic object via tool_use.

        The system prompt is marked for prompt caching (ephemeral).
        Adaptive thinking is enabled on Opus 4.7.
        """
        schema = output_schema.model_json_schema()

        # Resolve $ref references and clean the schema for Anthropic tool use.
        # Anthropic requires: no $defs, no $ref, no `default` keys.
        defs: dict[str, Any] = schema.get("$defs", {})

        def _clean_schema(obj: Any) -> Any:
            if isinstance(obj, dict):
                if "$ref" in obj:
                    ref = obj["$ref"]
                    if ref.startswith("#/$defs/"):
                        def_name = ref[len("#/$defs/"):]
                        return _clean_schema(defs.get(def_name, {}))
                return {
                    k: _clean_schema(v)
                    for k, v in obj.items()
                    if k not in ("default", "$defs", "title")
                }
            if isinstance(obj, list):
                return [_clean_schema(i) for i in obj]
            return obj

        clean_schema = _clean_schema(schema)

        tool_def = {
            "name": tool_name,
            "description": f"Return a structured {output_schema.__name__} object",
            "input_schema": clean_schema,
        }

        messages: list[dict[str, Any]] = []
        if extra_context:
            messages.extend(extra_context)
        messages.append({"role": "user", "content": user_message})

        last_exc: Exception | None = None
        for attempt in range(2):
            response = await self._client.messages.create(
                model=self._settings.claude_model,
                max_tokens=max_tokens,
                # Note: thinking cannot be enabled when tool_choice forces tool use (API constraint)
                system=[
                    {
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                tools=[tool_def],
                tool_choice={"type": "tool", "name": tool_name},
                messages=messages,
            )

            # Extract the tool_use block
            for block in response.content:
                if block.type == "tool_use" and block.name == tool_name:
                    data = block.input
                    # Claude sometimes wraps output in a single-key dict:
                    #   {"$PARAM_NAME": {...}}, {"parameter": {...}}, {"output": {...}}, etc.
                    # Unwrap if the inner value is a dict containing known schema fields.
                    if isinstance(data, dict) and len(data) == 1:
                        inner = next(iter(data.values()))
                        if isinstance(inner, str):
                            try:
                                inner = json.loads(inner)
                            except Exception:
                                inner = None
                        if isinstance(inner, dict):
                            schema_fields = set(output_schema.model_fields.keys())
                            if schema_fields & set(inner.keys()):
                                data = inner
                    try:
                        return output_schema.model_validate(data)
                    except Exception as exc:
                        last_exc = exc
                        if attempt == 0:
                            # The API requires a tool_result immediately after each tool_use.
                            # Send an error result + correction instruction so Claude retries.
                            messages.append({"role": "assistant", "content": response.content})
                            messages.append({
                                "role": "user",
                                "content": [
                                    {
                                        "type": "tool_result",
                                        "tool_use_id": block.id,
                                        "is_error": True,
                                        "content": (
                                            f"Schema validation failed: {exc}. "
                                            f"Call {tool_name} again with every required field "
                                            f"as a separate named JSON property."
                                        ),
                                    }
                                ],
                            })
                        break  # retry the outer loop

        raise last_exc or RuntimeError(
            f"{self.name}: Claude did not return a valid {tool_name} tool call"
        )

    async def _call_claude_text(
        self,
        system_prompt: str,
        user_message: str,
        max_tokens: int = 2048,
    ) -> str:
        """
        Simple Claude call that returns plain text.
        Used for explanations, summaries, and open-ended analysis.
        """
        response = await self._client.messages.create(
            model=self._settings.claude_model,
            max_tokens=max_tokens,
            thinking={"type": "adaptive"},
            system=[
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_message}],
        )
        texts = [b.text for b in response.content if hasattr(b, "text")]
        return "\n".join(texts)

    @property
    def status(self) -> AgentStatus:
        return self._status

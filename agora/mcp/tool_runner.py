"""
agora/mcp/tool_runner.py — Agentic loop helper for tool_use / tool_result cycles.

Wraps the standard Anthropic messages.create() call into a loop that:
  1. Calls the model
  2. If stop_reason == "tool_use", executes the requested tools
  3. Appends assistant + tool_result turns
  4. Repeats until stop_reason != "tool_use" or max_turns exceeded

Usage:
    response = await run_with_tools(
        client, model, system, messages,
        tools=SQLITE_TOOLS + SEARCH_TOOLS,
        handlers={**sqlite_tool_handlers(db_path), **search_tool_handlers(api_key)},
        max_turns=4,
        max_tokens=2048,
        thinking={"type": "adaptive"},
    )
    text = next(b.text for b in response.content if b.type == "text")
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

import anthropic

logger = logging.getLogger(__name__)


async def run_with_tools(
    client: anthropic.AsyncAnthropic,
    model: str,
    system: str | list,
    messages: list[dict],
    tools: list[dict],
    handlers: dict[str, Callable],
    max_turns: int = 5,
    **create_kwargs: Any,
) -> anthropic.types.Message:
    """
    Run an agentic tool-use loop.

    Returns the final Message (stop_reason="end_turn" or exhausted max_turns).
    Never raises — tool errors are returned as tool_result is_error=True so
    Claude can adapt rather than crashing the whole evaluation.
    """
    msgs = list(messages)

    for turn in range(max_turns):
        response = await client.messages.create(
            model=model,
            system=system,
            messages=msgs,
            tools=tools,
            **create_kwargs,
        )

        if response.stop_reason != "tool_use":
            return response

        # Collect tool calls from this response
        tool_results: list[dict] = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            handler = handlers.get(block.name)
            if handler is None:
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": f"Unknown tool: {block.name}",
                    "is_error": True,
                })
                continue
            try:
                result = await _call(handler, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, default=str),
                })
                logger.debug("Tool %s → %d chars", block.name, len(str(result)))
            except Exception as exc:
                logger.warning("Tool %s failed: %s", block.name, exc)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": f"Tool error: {exc}",
                    "is_error": True,
                })

        # Append assistant turn + tool results
        msgs.append({"role": "assistant", "content": response.content})
        msgs.append({"role": "user", "content": tool_results})

    logger.warning("run_with_tools exhausted %d turns without end_turn", max_turns)
    return response  # Return last response


async def _call(fn: Callable, kwargs: dict) -> Any:
    """Call sync or async handler uniformly."""
    import asyncio
    if asyncio.iscoroutinefunction(fn):
        return await fn(**kwargs)
    return fn(**kwargs)

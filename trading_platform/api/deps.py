"""
FastAPI dependency injection — global platform singletons.

Separated from main.py to avoid circular imports.
Routes import from here; main.py sets values here during lifespan startup.
"""

from __future__ import annotations

from typing import Any

from ..core.bus import MessageBus
from ..core.state import SharedStateStore
from ..agents.orchestrator import OrchestratorAgent

# ── Singletons (set during lifespan startup) ──────────────────────────
_bus: MessageBus | None = None
_state_store: SharedStateStore | None = None
_orchestrator: OrchestratorAgent | None = None
_all_agents: list[Any] = []


def set_platform(
    bus: MessageBus,
    state_store: SharedStateStore,
    orchestrator: OrchestratorAgent,
    all_agents: list[Any],
) -> None:
    global _bus, _state_store, _orchestrator, _all_agents
    _bus = bus
    _state_store = state_store
    _orchestrator = orchestrator
    _all_agents = list(all_agents)


def clear_platform() -> None:
    global _bus, _state_store, _orchestrator, _all_agents
    _bus = None
    _state_store = None
    _orchestrator = None
    _all_agents = []


def get_bus() -> MessageBus:
    assert _bus is not None, "Platform not initialized"
    return _bus


def get_state_store() -> SharedStateStore:
    assert _state_store is not None, "Platform not initialized"
    return _state_store


def get_orchestrator() -> OrchestratorAgent:
    assert _orchestrator is not None, "Platform not initialized"
    return _orchestrator


def get_all_agents() -> list[Any]:
    return _all_agents

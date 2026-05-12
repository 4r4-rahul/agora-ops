"""
Shared mutable state between the FastAPI routes and the running AgoraSession.

The session is started in the app lifespan and stored here. Routes import
`get_session()` as a FastAPI dependency.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..session import AgoraSession

_session: "AgoraSession | None" = None


def set_session(s: "AgoraSession") -> None:
    global _session
    _session = s


def get_session() -> "AgoraSession":
    if _session is None:
        raise RuntimeError("AgoraSession not initialised — lifespan not running")
    return _session

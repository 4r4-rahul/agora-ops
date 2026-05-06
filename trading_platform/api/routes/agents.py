"""
Agent status API routes.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from ..deps import get_bus

router = APIRouter()


@router.get("/", summary="Get agent platform status")
async def get_agent_status() -> dict[str, Any]:
    bus = get_bus()
    return {
        "status": "running",
        "bus_stats": bus.stats,
    }


@router.get("/bus/stats", summary="Message bus statistics")
async def get_bus_stats() -> dict[str, Any]:
    bus = get_bus()
    return bus.stats

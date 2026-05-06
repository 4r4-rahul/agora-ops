"""
Analysis API routes — trigger pipeline, poll results, approve trades.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field

from ...core.models.agent import AnalysisSession
from ...core.models.trade import TradeDecision
from ..deps import get_orchestrator, get_state_store

router = APIRouter()


class AnalyzeRequest(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=10)
    force_refresh: bool = False


class BatchAnalyzeRequest(BaseModel):
    tickers: list[str] = Field(..., min_length=1, max_length=20)
    concurrency: int = Field(default=3, ge=1, le=10)


class ApproveRequest(BaseModel):
    approved: bool
    user: str = Field(default="api_user")
    notes: str = ""


@router.post("/", summary="Analyze a single ticker")
async def analyze_ticker(
    req: AnalyzeRequest,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    """
    Kick off the full analysis pipeline for one ticker.

    Returns immediately with session_id. Poll GET /analysis/{session_id} for results.
    Or wait synchronously (blocks up to 120s).
    """
    orchestrator = get_orchestrator()
    state_store = get_state_store()

    # Pre-create the session so the caller can poll immediately with the
    # returned session_id. Pass it into analyze() to avoid creating a second.
    session = await state_store.create_session(req.ticker.upper())
    session_id = session.session_id

    background_tasks.add_task(
        orchestrator.analyze, req.ticker.upper(), 120.0, session_id
    )

    return {
        "session_id": session_id,
        "ticker": req.ticker.upper(),
        "status": "started",
        "poll_url": f"/api/v1/analysis/{session_id}",
    }


@router.post("/sync", summary="Analyze synchronously (blocks until complete)")
async def analyze_ticker_sync(req: AnalyzeRequest) -> dict[str, Any]:
    """
    Run analysis pipeline and wait for result. Max 120 seconds.
    """
    orchestrator = get_orchestrator()
    result = await orchestrator.analyze(req.ticker.upper(), timeout=120.0)
    return result


@router.post("/batch", summary="Analyze multiple tickers")
async def analyze_batch(req: BatchAnalyzeRequest) -> dict[str, Any]:
    """
    Run analysis pipeline for multiple tickers with bounded concurrency.
    """
    orchestrator = get_orchestrator()
    tickers = [t.upper() for t in req.tickers]
    results = await orchestrator.analyze_batch(tickers, concurrency=req.concurrency)
    return {"tickers": tickers, "results": results}


@router.get("/{session_id}", summary="Get analysis session status and result")
async def get_session(
    session_id: str = Path(..., description="Session ID from analyze endpoint"),
) -> dict[str, Any]:
    state_store = get_state_store()
    session = await state_store.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    return session.model_dump(mode="json")


@router.get("/", summary="List recent analysis sessions")
async def list_sessions(
    status: str | None = Query(None, description="Filter by status: pending|complete|failed"),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    state_store = get_state_store()
    sessions = await state_store.list_sessions(status=status, limit=limit)
    return {
        "sessions": [s.model_dump(mode="json") for s in sessions],
        "count": len(sessions),
    }


@router.post("/{session_id}/approve", summary="Human approval for a recommendation")
async def approve_recommendation(
    session_id: str,
    req: ApproveRequest,
) -> dict[str, Any]:
    """
    Submit human approval decision for a pending recommendation.
    Required when require_human_approval=true.
    """
    state_store = get_state_store()
    session = await state_store.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")

    if not session.final_recommendation:
        raise HTTPException(
            status_code=400,
            detail="No recommendation ready for this session",
        )

    from ...core.models.trade import TradeRecommendation

    rec = TradeRecommendation.model_validate(session.final_recommendation)

    if req.approved:
        rec.approve(user=req.user)
    else:
        rec.reject(reasons=[req.notes or "Rejected by human reviewer"])

    await state_store.update(
        session_id,
        final_recommendation=rec.model_dump(mode="json"),
    )

    return {
        "session_id": session_id,
        "ticker": rec.ticker,
        "decision": rec.final_decision,
        "approved_by": rec.approved_by,
    }

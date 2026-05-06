"""
Trade journal API routes.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from ..deps import get_state_store

router = APIRouter()


@router.get("/open", summary="List open paper trades")
async def get_open_trades() -> dict[str, Any]:
    from ...agents.journal import TradeJournalAgent

    # Journal is stateful — access via module-level instance
    # In production, use a proper DI container
    try:
        import sqlite3
        from pathlib import Path

        db_path = Path("./trade_journal.db")
        if not db_path.exists():
            return {"trades": [], "count": 0}

        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM trade_journal WHERE status = 'open' ORDER BY opened_at DESC"
            ).fetchall()
        trades = [dict(r) for r in rows]
        return {"trades": trades, "count": len(trades)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/performance", summary="Performance summary")
async def get_performance() -> dict[str, Any]:
    try:
        import sqlite3
        from pathlib import Path

        db_path = Path("./trade_journal.db")
        if not db_path.exists():
            return {"message": "No trades recorded yet"}

        with sqlite3.connect(db_path) as conn:
            row = conn.execute("""
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as winners,
                    SUM(realized_pnl) as total_pnl,
                    AVG(realized_pnl) as avg_pnl,
                    MAX(realized_pnl) as best,
                    MIN(realized_pnl) as worst
                FROM trade_journal
                WHERE status = 'closed'
            """).fetchone()

        if not row or row[0] == 0:
            return {"message": "No closed trades yet"}

        total = row[0]
        winners = row[1] or 0
        return {
            "total_trades": total,
            "winners": winners,
            "losers": total - winners,
            "win_rate": round(winners / total * 100, 1),
            "total_pnl": round(row[2] or 0, 2),
            "avg_pnl": round(row[3] or 0, 2),
            "best_trade": round(row[4] or 0, 2),
            "worst_trade": round(row[5] or 0, 2),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/history", summary="Trade history")
async def get_history(limit: int = 50) -> dict[str, Any]:
    try:
        import sqlite3
        from pathlib import Path

        db_path = Path("./trade_journal.db")
        if not db_path.exists():
            return {"trades": [], "count": 0}

        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"SELECT * FROM trade_journal ORDER BY opened_at DESC LIMIT {min(limit, 200)}"
            ).fetchall()
        trades = [dict(r) for r in rows]
        return {"trades": trades, "count": len(trades)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

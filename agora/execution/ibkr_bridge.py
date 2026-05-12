"""
AGORA → IBKR execution bridge.

Translates TradeRecommendation / OpenPosition into the leg format
expected by trading_platform.services.ibkr_client, then dispatches
to either the real IBKR gateway (live mode) or a dry-run log (paper mode).

Stop-loss is NOT submitted as an order — IBKR rejects STP orders on BAG
(combo) contracts. The PositionManager owns stop-loss enforcement by polling
prices every 60s and calling close_trade() when the threshold is breached.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

logger = logging.getLogger(__name__)


# ── Leg translation helpers ────────────────────────────────────────────────────

def _rec_to_legs(rec: Any) -> list[dict]:
    """SpreadLeg list from a TradeRecommendation → ibkr_client leg dicts."""
    today = date.today()
    return [
        {
            "strike":         leg.strike,
            "option_type":    leg.option_type,       # "call" | "put"
            "action":         leg.action.upper(),     # "BUY" | "SELL"
            "quantity":       1,                      # contracts multiplied at order level
            "expiration_dte": max(1, (leg.expiration - today).days),
        }
        for leg in rec.legs
    ]


def _pos_to_close_legs(pos: Any) -> list[dict]:
    """Reverse the legs of an open position to produce a closing order."""
    today = date.today()
    return [
        {
            "strike":         leg.strike,
            "option_type":    leg.option_type,
            "action":         "SELL" if leg.action.lower() == "buy" else "BUY",
            "quantity":       1,
            "expiration_dte": max(1, (leg.expiration - today).days),
        }
        for leg in pos.legs
    ]


# ── Public API ─────────────────────────────────────────────────────────────────

async def submit_trade(rec: Any, settings: Any, session_id: str) -> dict:
    """
    Route a TradeRecommendation to IBKR (live) or log it (paper).

    Profit target = 50 % of credit/debit received (matches backtest rule).
    Stop loss     = 2× credit/debit (informational — enforced by PositionManager).
    """
    profit_target = abs(rec.entry_debit_credit) * 0.50
    stop_loss     = abs(rec.entry_debit_credit) * 2.00

    if settings.trading_mode == "paper":
        logger.info(
            "PAPER OPEN  | %-6s | %-22s | contracts=%d | entry=%+.2f"
            " | target=%.2f | stop=%.2f | score=%.0f",
            rec.ticker, rec.strategy.value, rec.contracts,
            rec.entry_debit_credit, profit_target, stop_loss,
            rec.conviction_score,
        )
        return {"order_id": -1, "status": "paper", "fills": []}

    # ── Live mode ──────────────────────────────────────────────────────────────
    try:
        from trading_platform.services.ibkr_client import place_bracket_order
    except ImportError as exc:
        raise RuntimeError(
            "ib_insync is not installed or ibkr_client is unavailable"
        ) from exc

    logger.info(
        "LIVE OPEN   | %-6s | %-22s | contracts=%d | entry=%+.2f"
        " | target=%.2f | stop=%.2f (monitor-enforced)",
        rec.ticker, rec.strategy.value, rec.contracts,
        rec.entry_debit_credit, profit_target, stop_loss,
    )

    return await place_bracket_order(
        ticker=rec.ticker,
        legs=_rec_to_legs(rec),
        contracts=rec.contracts,
        entry_price=rec.entry_debit_credit,
        profit_target=profit_target,
        stop_loss=stop_loss,
        session_id=session_id,
        host=settings.ibkr_host,
        port=settings.ibkr_port,
        client_id=settings.ibkr_client_id,
    )


async def close_trade(pos: Any, settings: Any, session_id: str) -> dict:
    """
    Close an existing open position at market (MOC limit).

    Paper mode: logs the close with unrealized P&L.
    Live mode: submits a closing combo order via IBKR.
    """
    unrealized = getattr(pos, "unrealized_pnl", 0.0)

    if settings.trading_mode == "paper":
        logger.info(
            "PAPER CLOSE | %-6s | unrealized_pnl=%+.0f",
            pos.ticker, unrealized,
        )
        return {"order_id": -1, "status": "paper_close", "fills": []}

    try:
        from trading_platform.services.ibkr_client import close_position
    except ImportError as exc:
        raise RuntimeError("ib_insync is not installed") from exc

    logger.info("LIVE CLOSE  | %-6s | unrealized_pnl=%+.0f", pos.ticker, unrealized)

    return await close_position(
        ticker=pos.ticker,
        legs=_pos_to_close_legs(pos),
        contracts=pos.contracts,
        session_id=session_id,
        host=settings.ibkr_host,
        port=settings.ibkr_port,
        client_id=settings.ibkr_client_id + 1,  # different client_id to avoid order conflicts
    )

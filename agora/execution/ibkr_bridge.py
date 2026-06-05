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

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any

logger = logging.getLogger(__name__)

# ib_insync manages its own internal event loop and is not safe to call from
# inside uvicorn's asyncio loop. All IBKR calls are offloaded to a dedicated
# single-threaded executor where each call gets a brand-new event loop via
# _run_in_new_loop(), bypassing the "already running" asyncio.run() restriction.
_IBKR_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ibkr")


def _run_in_new_loop(coro):
    """Run a coroutine in a fresh event loop — safe to call from a thread pool."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        loop.close()
        asyncio.set_event_loop(None)


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

    Profit target:
      Credit spreads: 50% of credit received (buy back at half the premium).
      Debit spreads:  entry + 50% of max_gain (sell when spread rises by half its remaining room).
    Stop loss: 2× entry debit/credit (informational — enforced by PositionManager).
    """
    # IBKR combo (BAG) orders use per-share pricing; entry_debit_credit is total dollars.
    # Divide by (contracts × 100) to get the per-share limit price TWS expects.
    per_share_divisor  = max(1, rec.contracts) * 100
    entry_per_share    = rec.entry_debit_credit / per_share_divisor
    max_gain_per_share = getattr(rec, "max_gain_dollars", 0.0) / per_share_divisor

    if entry_per_share > 0:
        # Debit spread (BUY): close when spread value rises above entry.
        # profit_target = entry + 50% of remaining max gain.
        # Bug guard: abs(entry) * 0.50 is WRONG here — it prices the GTC SELL below
        # entry, which fires immediately when bid > half of debit paid.
        profit_target = entry_per_share + max_gain_per_share * 0.50
    else:
        # Credit spread (SELL): close when the spread can be bought back cheaply.
        # profit_target = 50% of credit received = the price to buy back.
        profit_target = max_gain_per_share * 0.50

    stop_loss = abs(entry_per_share) * 2.00

    mode_label = "PAPER" if settings.trading_mode == "paper" else "LIVE"
    logger.info(
        "%s OPEN  | %-6s | %-22s | contracts=%d | entry=%+.4f/sh"
        " | target=%.4f | stop=%.4f | score=%.0f | port=%d",
        mode_label, rec.ticker, rec.strategy.value, rec.contracts,
        entry_per_share, profit_target, stop_loss,
        rec.conviction_score, settings.ibkr_port,
    )

    try:
        from trading_platform.services.ibkr_client import (
            place_bracket_order,
            place_legs_individually,
        )
    except ImportError as exc:
        raise RuntimeError(
            "ib_insync is not installed or ibkr_client is unavailable"
        ) from exc

    kwargs = dict(
        ticker=rec.ticker,
        legs=_rec_to_legs(rec),
        contracts=rec.contracts,
        entry_price=entry_per_share,
        profit_target=profit_target,
        stop_loss=stop_loss,
        session_id=session_id,
        host=settings.ibkr_host,
        port=settings.ibkr_port,
        client_id=settings.ibkr_client_id,
        price_step_size=getattr(settings, "pricing_step_size", 0.05),
        use_adaptive_algo=getattr(settings, "use_adaptive_algo", True),
        adaptive_algo_priority=getattr(settings, "adaptive_algo_priority", "Normal"),
    )

    # Paper mode: submit each leg individually to bypass the IBKR riskless-combination
    # order limit (Error 201). Individual option orders are not classified as "riskless
    # combination orders" and work for all tickers including single-name equities.
    # Live mode: always use BAG combo (atomic fill guarantee, no leg gap risk).
    if settings.trading_mode == "paper":
        fn = place_legs_individually
        logger.info("PAPER mode: using leg-by-leg submission for %s (avoids Error 201)", rec.ticker)
    else:
        fn = place_bracket_order

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        _IBKR_EXECUTOR,
        lambda: _run_in_new_loop(fn(**kwargs)),
    )


async def close_trade(pos: Any, settings: Any, session_id: str) -> dict:
    """
    Close an existing open position at market (MOC limit).

    Paper mode: logs the close with unrealized P&L.
    Live mode: submits a closing combo order via IBKR.
    """
    unrealized = getattr(pos, "unrealized_pnl", 0.0)

    mode_label = "PAPER" if settings.trading_mode == "paper" else "LIVE"
    logger.info(
        "%s CLOSE | %-6s | unrealized_pnl=%+.0f | port=%d",
        mode_label, pos.ticker, unrealized, settings.ibkr_port,
    )

    try:
        from trading_platform.services.ibkr_client import close_position
    except ImportError as exc:
        raise RuntimeError("ib_insync is not installed") from exc

    kwargs = dict(
        ticker=pos.ticker,
        legs=_pos_to_close_legs(pos),
        contracts=pos.contracts,
        session_id=session_id,
        host=settings.ibkr_host,
        port=settings.ibkr_port,
        client_id=settings.ibkr_client_id + 1,
    )
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        _IBKR_EXECUTOR,
        lambda: _run_in_new_loop(close_position(**kwargs)),
    )

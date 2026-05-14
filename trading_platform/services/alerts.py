"""
Alert service — clean one-line Discord notifications for trade lifecycle events.

Three events per trade:
  1. APPROVED  — trade cleared approval gate, about to be sent to broker
  2. EXECUTED  — broker fill confirmed (or paper simulation complete)
  3. CLOSED    — position exited (profit target / stop loss / expiry)

Configure ALERT_WEBHOOK_URL in .env (Discord webhook URL).
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

logger = logging.getLogger(__name__)


# ── HTTP helper ──────────────────────────────────────────────────────────────

async def _post(url: str, payload: dict[str, Any]) -> bool:
    """POST JSON to webhook. Retries once on 429."""
    try:
        import asyncio
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code == 429:
                retry_after = float(resp.json().get("retry_after", 1.0))
                await asyncio.sleep(max(retry_after, 0.5))
                resp = await client.post(url, json=payload)
            if resp.status_code not in (200, 204):
                logger.warning("webhook %d: %s", resp.status_code, resp.text[:200])
                return False
            return True
    except Exception as exc:
        logger.error("webhook post failed: %s", exc)
        return False


async def _send(url: str | None, content: str) -> None:
    if url:
        await _post(url, {"content": content})


# ── Formatting helpers ────────────────────────────────────────────────────────

def _dte_label(expiration: date | None, dte_int: int | None = None) -> str:
    if expiration:
        days = (expiration - date.today()).days
        return f"{max(0, days)}DTE"
    if dte_int is not None:
        return f"{dte_int}DTE"
    return "?DTE"


def _legs_summary(legs: list) -> str:
    """
    Compact leg string.  Examples:
      Bull call spread → 625C / 630C
      Iron condor      → 615P / 620P / 630C / 635C
      Single leg       → 625C
    """
    if not legs:
        return "?"
    parts = []
    for leg in legs:
        # Support both SpreadLeg objects and plain dicts
        strike = getattr(leg, "strike", None) or leg.get("strike", "?")
        otype  = getattr(leg, "option_type", None) or leg.get("option_type", "?")
        parts.append(f"{int(strike) if float(strike) == int(float(strike)) else strike}"
                     f"{otype[0].upper()}")
    return " / ".join(parts)


def _strategy_label(strategy: str) -> str:
    return {
        "bull_call_spread":  "Bull Call Spread",
        "bear_put_spread":   "Bear Put Spread",
        "iron_condor":       "Iron Condor",
        "iron_butterfly":    "Iron Butterfly",
        "long_call":         "Long Call",
        "long_put":          "Long Put",
        "cash_secured_put":  "Cash Secured Put",
        "diagonal_spread":   "Diagonal Spread",
        "calendar_spread":   "Calendar Spread",
    }.get(str(strategy).lower(), str(strategy).replace("_", " ").title())


# ── Public alert functions ────────────────────────────────────────────────────

async def alert_trade_approved(
    webhook_url: str | None,
    *,
    ticker: str,
    strategy: str,
    legs: list,
    expiration: date | None = None,
    expiration_dte: int | None = None,
    entry_price: float,
    stop_loss: float,
    profit_target: float,
    max_loss_dollars: float,
    contracts: int,
    mode: str = "paper",
    **_kw: Any,
) -> None:
    """Fire when a trade is approved and about to be sent to broker."""
    leg_str  = _legs_summary(legs)
    dte_str  = _dte_label(expiration, expiration_dte)
    strat    = _strategy_label(strategy)
    mode_tag = "PAPER" if mode == "paper" else "LIVE"

    line1 = f"✅ **APPROVED [{mode_tag}]**  •  {ticker}  {leg_str}  {strat}  {dte_str}"
    line2 = f"   `@${entry_price:.2f}  ×{contracts} contracts`   Stop ${stop_loss:.2f}  |  Target ${profit_target:.2f}  |  Max loss ${max_loss_dollars:,.0f}"

    await _send(webhook_url, f"{line1}\n{line2}")


async def alert_trade_executed(
    webhook_url: str | None,
    *,
    ticker: str,
    strategy: str,
    legs: list,
    expiration: date | None = None,
    expiration_dte: int | None = None,
    entry_price: float,
    contracts: int,
    mode: str = "paper",
    **_kw: Any,
) -> None:
    """Fire when the broker fill (or paper simulation) is confirmed."""
    leg_str  = _legs_summary(legs)
    dte_str  = _dte_label(expiration, expiration_dte)
    strat    = _strategy_label(strategy)
    mode_tag = "PAPER" if mode == "paper" else "LIVE"

    line = f"⚡ **EXECUTED [{mode_tag}]**  •  {ticker}  {leg_str}  {strat}  {dte_str}   `@${entry_price:.2f}  ×{contracts} contracts`"
    await _send(webhook_url, line)


async def alert_position_closed(
    webhook_url: str | None,
    *,
    ticker: str,
    strategy: str,
    exit_reason: str,
    underlying_price: float,
    option_exit_price: float,
    estimated_pnl: float,
    journal_id: str,
    **_kw: Any,
) -> None:
    """Fire when a position is closed."""
    reason_map = {
        "profit_target":       ("🎯", "Profit Target"),
        "stop_loss":           ("🛑", "Stop Loss"),
        "expiry":              ("⏰", "Expired"),
        "dynamic_profit_early":("🎯", "Early Profit"),
        "adjustment":          ("🔧", "Adjusted"),
    }
    emoji, reason_label = reason_map.get(exit_reason, ("📤", exit_reason.replace("_", " ").title()))
    pnl_str = f"+${estimated_pnl:,.0f}" if estimated_pnl >= 0 else f"-${abs(estimated_pnl):,.0f}"
    strat   = _strategy_label(strategy)
    color   = "🟢" if estimated_pnl >= 0 else "🔴"

    line = (f"{emoji} **CLOSED**  •  {ticker}  {strat}  •  {reason_label}"
            f"   {color} **{pnl_str}**   underlying ${underlying_price:.2f}"
            f"   `{journal_id[:12]}`")
    await _send(webhook_url, line)


async def alert_pipeline_error(
    webhook_url: str | None,
    *,
    ticker: str,
    error: str,
    session_id: str,
    **_kw: Any,
) -> None:
    """Fire when the pipeline fails for a ticker."""
    line = f"🔥 **ERROR**  •  {ticker}  •  `{error[:200]}`   session `{session_id[:8]}`"
    await _send(webhook_url, line)


# Kept for backwards-compat — scheduler calls this; suppress for non-APPROVED to reduce noise
async def alert_recommendation_ready(
    webhook_url: str | None,
    *,
    final_decision: str,
    **_kw: Any,
) -> None:
    """Only notify for non-routine decisions (REJECTED) to reduce noise."""
    if final_decision == "REJECTED":
        ticker   = _kw.get("ticker", "?")
        strategy = _strategy_label(_kw.get("strategy", ""))
        reasons  = _kw.get("rejection_reasons", "")
        line = f"❌ **REJECTED**  •  {ticker}  {strategy}   `{str(reasons)[:120]}`"
        await _send(webhook_url, line)

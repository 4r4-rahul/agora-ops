"""
Alert service — sends trade event notifications via webhook (Discord/Slack).

Triggered by: trade approved, position closed (stop/target/expiry), pipeline errors.

Configure via ALERT_WEBHOOK_URL in .env:
  Discord:  https://discord.com/api/webhooks/{id}/{token}
  Slack:    https://hooks.slack.com/services/{...}

Both Discord and Slack accept the same {"content": "..."} payload for simple
text messages. Discord additionally supports rich embeds via {"embeds": [...]}.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


async def _post(url: str, payload: dict[str, Any]) -> bool:
    """POST JSON to webhook URL. Returns True on success."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code not in (200, 204):
                logger.warning("webhook returned %d: %s", resp.status_code, resp.text[:200])
                return False
            return True
    except Exception as exc:
        logger.error("webhook post failed: %s", exc)
        return False


def _embed(title: str, description: str, color: int, fields: list[dict]) -> dict:
    """Build a Discord-compatible embed (also readable as plain text in Slack)."""
    return {
        "embeds": [{
            "title": title,
            "description": description,
            "color": color,
            "fields": [
                {"name": f["name"], "value": str(f["value"]), "inline": f.get("inline", True)}
                for f in fields
            ],
        }]
    }


# ── Public alert functions ────────────────────────────────────────────────

async def alert_trade_approved(
    webhook_url: str | None,
    *,
    ticker: str,
    strategy: str,
    direction: str,
    entry_price: float,
    stop_loss: float,
    profit_target: float,
    max_loss_dollars: float,
    reward_risk_ratio: float,
    contracts: int,
    thesis: str,
    session_id: str,
) -> None:
    """Fire when a trade clears human approval and is queued for execution."""
    if not webhook_url:
        return

    color = 0x2ECC71  # green
    payload = _embed(
        title=f"✅ TRADE APPROVED — {ticker} {strategy.upper()}",
        description=thesis[:200],
        color=color,
        fields=[
            {"name": "Direction", "value": direction.upper()},
            {"name": "Entry", "value": f"${entry_price:.2f}"},
            {"name": "Stop", "value": f"${stop_loss:.2f}"},
            {"name": "Target", "value": f"${profit_target:.2f}"},
            {"name": "Max Loss", "value": f"${max_loss_dollars:,.0f}"},
            {"name": "R/R", "value": f"{reward_risk_ratio:.1f}:1"},
            {"name": "Contracts", "value": str(contracts)},
            {"name": "Session", "value": session_id[:12]},
        ],
    )
    await _post(webhook_url, payload)


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
) -> None:
    """Fire when MonitorAgent closes a paper position."""
    if not webhook_url:
        return

    emoji = "🎯" if exit_reason == "profit_target" else ("🛑" if exit_reason == "stop_loss" else "⏰")
    color = 0x2ECC71 if estimated_pnl >= 0 else 0xE74C3C  # green / red
    reason_label = {
        "profit_target": "Profit Target Hit",
        "stop_loss": "Stop Loss Hit",
        "expiry": "Expired",
    }.get(exit_reason, exit_reason.replace("_", " ").title())

    payload = _embed(
        title=f"{emoji} POSITION CLOSED — {ticker} {strategy.upper()}",
        description=f"**{reason_label}**",
        color=color,
        fields=[
            {"name": "Underlying", "value": f"${underlying_price:.2f}"},
            {"name": "Option Price", "value": f"${option_exit_price:.2f}"},
            {"name": "Est. PnL", "value": f"${estimated_pnl:+,.0f}"},
            {"name": "Journal ID", "value": journal_id[:12]},
        ],
    )
    await _post(webhook_url, payload)


async def alert_recommendation_ready(
    webhook_url: str | None,
    *,
    ticker: str,
    strategy: str,
    direction: str,
    reward_risk_ratio: float,
    max_loss_dollars: float,
    final_decision: str,
    session_id: str,
) -> None:
    """Fire on every completed pipeline run (APPROVED, WATCHLIST, REJECTED)."""
    if not webhook_url:
        return

    emoji = {"APPROVED": "✅", "PENDING_APPROVAL": "⏳", "WATCHLIST": "👀", "REJECTED": "❌"}.get(
        final_decision, "📊"
    )
    color = {
        "APPROVED": 0x2ECC71,
        "PENDING_APPROVAL": 0xF1C40F,
        "WATCHLIST": 0x3498DB,
        "REJECTED": 0xE74C3C,
    }.get(final_decision, 0x95A5A6)

    payload = _embed(
        title=f"{emoji} {final_decision} — {ticker} {strategy.upper()}",
        description=f"{direction.upper()} | R/R {reward_risk_ratio:.1f}:1 | Max loss ${max_loss_dollars:,.0f}",
        color=color,
        fields=[{"name": "Session", "value": session_id[:12]}],
    )
    await _post(webhook_url, payload)


async def alert_pipeline_error(
    webhook_url: str | None,
    *,
    ticker: str,
    error: str,
    session_id: str,
) -> None:
    """Fire when the pipeline fails for a ticker."""
    if not webhook_url:
        return

    payload = _embed(
        title=f"🔥 PIPELINE ERROR — {ticker}",
        description=f"```{error[:500]}```",
        color=0xE74C3C,
        fields=[{"name": "Session", "value": session_id[:12]}],
    )
    await _post(webhook_url, payload)

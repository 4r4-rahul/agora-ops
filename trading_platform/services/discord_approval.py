"""
Interactive trade approval via Discord DM using Discord's REST API directly.

Flow:
  1. POST /users/@me/channels  → open DM channel with the approval user
  2. POST /channels/{id}/messages → send formatted trade card
  3. GET  /channels/{id}/messages → poll every 10s for a y/n reply
  4. Resolve True/False; timeout → auto-reject

Setup (one-time):
  1. Create a bot at https://discord.com/developers/applications
  2. Enable "Message Content Intent" under Bot → Privileged Gateway Intents
  3. Copy the Bot Token → set DISCORD_BOT_TOKEN in .env
  4. Enable Developer Mode in Discord (Settings → Advanced)
  5. Right-click your username → Copy User ID → set DISCORD_APPROVAL_USER_ID in .env
  6. Invite the bot to any shared server so it can DM you:
     https://discord.com/api/oauth2/authorize?client_id=<APP_ID>&scope=bot&permissions=2048
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

_BASE = "https://discord.com/api/v10"
_POLL_INTERVAL = 10  # seconds between reply checks


async def _api(
    method: str, path: str, token: str, **kwargs: Any
) -> dict[str, Any] | list | None:
    """Authenticated Discord REST request. Returns parsed JSON or None on error."""
    try:
        import httpx
    except ImportError:
        logger.error("httpx not installed — cannot call Discord API")
        return None

    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await getattr(client, method)(
            f"{_BASE}{path}", headers=headers, **kwargs
        )
    if resp.status_code in (200, 201):
        return resp.json()
    if resp.status_code == 204:
        return {}
    logger.error("Discord %s %s → %d: %s", method.upper(), path, resp.status_code, resp.text[:200])
    return None


async def _open_dm(token: str, user_id: str) -> str | None:
    """Return the DM channel ID for the given user."""
    result = await _api("post", "/users/@me/channels", token, json={"recipient_id": user_id})
    return result.get("id") if isinstance(result, dict) else None


async def _send(token: str, channel_id: str, content: str) -> str | None:
    """Post a message. Returns the message ID."""
    result = await _api("post", f"/channels/{channel_id}/messages", token, json={"content": content})
    return result.get("id") if isinstance(result, dict) else None


async def _poll_reply(
    token: str, channel_id: str, after_id: str, timeout: int
) -> bool | None:
    """
    Poll for a human reply after `after_id`.
    Returns True (approve), False (reject), or None (timeout).
    """
    try:
        import httpx
    except ImportError:
        return None

    headers = {"Authorization": f"Bot {token}"}
    deadline = asyncio.get_event_loop().time() + timeout

    async with httpx.AsyncClient(timeout=15.0) as client:
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(_POLL_INTERVAL)
            try:
                resp = await client.get(
                    f"{_BASE}/channels/{channel_id}/messages",
                    headers=headers,
                    params={"after": after_id, "limit": 10},
                )
            except Exception as exc:
                logger.warning("discord poll error: %s", exc)
                continue

            if resp.status_code != 200:
                continue

            for msg in reversed(resp.json()):  # oldest first
                if msg.get("author", {}).get("bot"):
                    continue
                text = msg.get("content", "").strip().lower()
                if text in ("y", "yes", "approve", "ok"):
                    return True
                if text in ("n", "no", "reject", "deny"):
                    return False

    return None  # timed out


async def request_approval(
    *,
    token: str,
    user_id: str,
    ticker: str,
    strategy: str,
    direction: str,
    legs_summary: str,
    entry_price: float,
    stop_loss: float,
    profit_target: float,
    max_loss_dollars: float,
    reward_risk_ratio: float,
    contracts: int,
    timeout_seconds: int = 300,
) -> bool:
    """
    Send a trade card to the approval user via DM and wait for a y/n reply.
    Returns True if approved, False if rejected or timed out.
    """
    channel_id = await _open_dm(token, user_id)
    if not channel_id:
        logger.error("discord approval: could not open DM channel — falling back to rejection")
        return False

    body = (
        "**⚡ TRADE APPROVAL NEEDED**\n"
        "```\n"
        f"Ticker:   {ticker}  {strategy.upper()}  ({direction.upper()})\n"
        f"Legs:     {legs_summary}\n"
        f"Entry:    ${entry_price:.2f}  |  Stop: ${stop_loss:.2f}  |  Target: ${profit_target:.2f}\n"
        f"Max Loss: ${max_loss_dollars:,.0f}  |  R/R: {reward_risk_ratio:.1f}:1  |  Qty: {contracts}\n"
        "```\n"
        f"Reply **y** to approve or **n** to reject. Timeout: {timeout_seconds // 60} min."
    )

    msg_id = await _send(token, channel_id, body)
    if not msg_id:
        logger.error("discord approval: failed to send DM — falling back to rejection")
        return False

    logger.info("discord approval: DM sent, waiting up to %ds for reply", timeout_seconds)
    result = await _poll_reply(token, channel_id, msg_id, timeout_seconds)

    if result is None:
        logger.warning("discord approval: timed out — auto-rejecting %s %s", ticker, strategy)
        await _send(token, channel_id, "⏰ No reply — trade **rejected** (timeout).")
        return False

    await _send(token, channel_id, "✅ Approved!" if result else "❌ Rejected.")
    return result

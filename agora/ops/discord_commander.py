"""
agora/ops/discord_commander.py — Bidirectional Discord bridge.

Two roles in one poller loop (every POLL_INTERVAL seconds):

ROLE 1 — Trading command handler
  Polls the bot's DM channel (or a configured command channel) for messages
  from the approval user. Handles:
    !positions          — current open positions + P&L
    !health             — system health, kill switch, regime
    !kill               — arm kill switch
    !unkill             — disarm kill switch
    !close <TICKER>     — force-close a position
    !lessons            — list pending lessons
    !approve <id>       — approve a lesson
    !reject <id>        — reject a lesson
    !calibration        — latest calibration report summary
    !help               — list available commands

ROLE 2 — Claude Code inbox bridge
  Discord → .agora/discord_inbox.json   (you → Claude Code)
  .agora/discord_outbox.json → Discord  (Claude Code → you)

  To send a coding instruction: prefix message with ">> "
    Example: ">> add cost widget to dashboard"
  Claude Code (running /loop) reads the inbox, implements it, and writes
  the result to discord_outbox.json. This poller sends that back to you as a DM.

Setup (same as discord_approval.py):
  DISCORD_BOT_TOKEN       in .env
  DISCORD_APPROVAL_USER_ID in .env  (your Discord user ID)

Latency:
  Trading commands: ~15s (POLL_INTERVAL)
  Claude Code bridge: depends on /loop schedule (60s–300s)
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_BASE          = "https://discord.com/api/v10"
POLL_INTERVAL  = 15   # seconds between Discord polls
_INBOX_FILE    = Path(".agora/discord_inbox.json")
_OUTBOX_FILE   = Path(".agora/discord_outbox.json")
_MAX_MSG_LEN   = 1900  # Discord 2000 char limit with buffer


# ── Low-level Discord REST ────────────────────────────────────────────────────

async def _api(method: str, path: str, token: str, **kwargs: Any) -> Any:
    try:
        import httpx
    except ImportError:
        logger.error("httpx not installed — Discord commander disabled")
        return None
    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await getattr(client, method)(f"{_BASE}{path}", headers=headers, **kwargs)
    if resp.status_code in (200, 201):
        return resp.json()
    if resp.status_code == 204:
        return {}
    logger.debug("Discord %s %s → %d", method.upper(), path, resp.status_code)
    return None


async def _open_dm(token: str, user_id: str) -> str | None:
    r = await _api("post", "/users/@me/channels", token, json={"recipient_id": user_id})
    return r.get("id") if isinstance(r, dict) else None


async def _send(token: str, channel_id: str, text: str) -> str | None:
    chunks = [text[i:i + _MAX_MSG_LEN] for i in range(0, len(text), _MAX_MSG_LEN)]
    last_id = None
    for chunk in chunks:
        r = await _api("post", f"/channels/{channel_id}/messages", token, json={"content": chunk})
        if isinstance(r, dict):
            last_id = r.get("id")
        if len(chunks) > 1:
            await asyncio.sleep(0.3)
    return last_id


async def _fetch_after(token: str, channel_id: str, after_id: str) -> list[dict]:
    r = await _api(
        "get", f"/channels/{channel_id}/messages", token,
        params={"after": after_id, "limit": 20},
    )
    if not isinstance(r, list):
        return []
    return [m for m in r if not m.get("author", {}).get("bot")]


# ── Command handlers ──────────────────────────────────────────────────────────

def _fmt_positions(session: Any) -> str:
    try:
        positions = session._position_mgr.get_open_positions()
        if not positions:
            return "📭 No open positions."
        lines = ["**Open Positions**"]
        for p in positions:
            pe = session._position_mgr.get_profit_engine_state(p.position_id)
            pnl = p.unrealized_pnl or 0
            pnl_str = f"+${pnl:.0f}" if pnl >= 0 else f"-${abs(pnl):.0f}"
            dte = (p.expiry_date - datetime.now(tz=UTC).date()).days if p.expiry_date else "?"
            line = f"`{p.ticker}` {p.strategy_type or '?'} | PnL {pnl_str} | {dte} DTE"
            if pe:
                line += f" | {pe.get('last_rule','?')} @ {pe.get('profit_pct',0)*100:.0f}%"
            lines.append(line)
        return "\n".join(lines)
    except Exception as exc:
        return f"⚠️ positions error: {exc}"


def _fmt_health(session: Any) -> str:
    try:
        ks = getattr(session, "_kill_switch_active", False)
        regime = "unknown"
        if hasattr(session, "_macro_context") and session._macro_context:
            regime = getattr(session._macro_context, "macro_stance", "unknown")
        mode = session._settings.trading_mode if hasattr(session._settings, "trading_mode") else "?"
        n_pos = len(session._position_mgr.get_open_positions())
        lines = [
            "**System Health**",
            f"Mode: `{mode}` | Regime: `{regime}`",
            f"Kill switch: {'🔴 ARMED' if ks else '🟢 off'}",
            f"Open positions: {n_pos}",
        ]
        return "\n".join(lines)
    except Exception as exc:
        return f"⚠️ health error: {exc}"


def _arm_kill(session: Any) -> str:
    try:
        session._kill_switch_active = True
        logger.warning("Kill switch armed via Discord command")
        return "🔴 **Kill switch ARMED** — no new trades will be submitted."
    except Exception as exc:
        return f"⚠️ kill error: {exc}"


def _disarm_kill(session: Any) -> str:
    try:
        session._kill_switch_active = False
        logger.info("Kill switch disarmed via Discord command")
        return "🟢 **Kill switch disarmed** — trading resumed."
    except Exception as exc:
        return f"⚠️ unkill error: {exc}"


async def _close_ticker(session: Any, ticker: str) -> str:
    try:
        positions = session._position_mgr.get_open_positions()
        matches = [p for p in positions if p.ticker.upper() == ticker.upper()]
        if not matches:
            return f"❌ No open position for `{ticker}`."
        pos = matches[0]
        await session._position_mgr._close_position(pos, "Discord commander: manual close")
        return f"✅ Closed `{pos.ticker}` {pos.strategy_type or ''} position."
    except Exception as exc:
        return f"⚠️ close error: {exc}"


def _fmt_lessons(db_path: str) -> str:
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                """SELECT lesson_id, agent_name, lesson_text, confidence_in_lesson
                   FROM agent_lessons
                   WHERE human_approved IS NULL AND active = 1
                   ORDER BY created_at_utc DESC LIMIT 10"""
            ).fetchall()
        if not rows:
            return "No pending lessons."
        lines = ["**Pending Lessons** (reply `!approve <id>` or `!reject <id>`)"]
        for lid, agent, text, conf in rows:
            short = text[:120] + "…" if len(text) > 120 else text
            conf_str = f"{conf:.0%}" if conf is not None else "?"
            lines.append(f"`[{lid}]` **{agent}** (conf={conf_str})\n  {short}")
        return "\n".join(lines)
    except Exception as exc:
        return f"⚠️ lessons error: {exc}"


def _approve_lesson(db_path: str, lesson_id: int) -> str:
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE agent_lessons SET human_approved=1, approved_at_utc=? WHERE lesson_id=?",
                (datetime.now(tz=UTC).isoformat(), lesson_id),
            )
        return f"✅ Lesson `{lesson_id}` approved — active next agent run."
    except Exception as exc:
        return f"⚠️ approve error: {exc}"


def _reject_lesson(db_path: str, lesson_id: int) -> str:
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE agent_lessons SET human_approved=0, rejected_at_utc=? WHERE lesson_id=?",
                (datetime.now(tz=UTC).isoformat(), lesson_id),
            )
        return f"❌ Lesson `{lesson_id}` rejected."
    except Exception as exc:
        return f"⚠️ reject error: {exc}"


def _fmt_calibration(calibration_path: str) -> str:
    try:
        p = Path(calibration_path)
        if not p.exists():
            return "📭 No calibration report yet. Runs weekly."
        data = json.loads(p.read_text())
        lines = [
            f"**Calibration Report** ({data.get('generated_at','?')})",
            f"Trades analyzed: {data.get('total_closed_trades', 0)} over {data.get('analysis_period_days',90)}d",
        ]
        notes = data.get("proposal_notes", [])
        for note in notes[:4]:
            lines.append(f"• {note[:150]}")
        return "\n".join(lines)
    except Exception as exc:
        return f"⚠️ calibration error: {exc}"


_HELP = """**AGORA Discord Commands**
`!positions`       — open positions + P&L
`!health`          — system health, regime, kill switch
`!kill`            — arm kill switch (no new trades)
`!unkill`          — disarm kill switch
`!close TICKER`    — force-close a position
`!lessons`         — pending lessons needing approval
`!approve <id>`    — approve a lesson
`!reject <id>`     — reject a lesson
`!calibration`     — latest conviction calibration report
`!help`            — this message

**Claude Code bridge** — prefix with `>> `:
`>> add cost widget to dashboard`
`>> what's the P&L attribution for last week?`
Claude Code picks up the instruction and replies here."""


# ── Route inbox message to Claude Code ────────────────────────────────────────

def _write_inbox(instruction: str) -> None:
    """Write a coding instruction to the Claude Code inbox file."""
    _INBOX_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "instruction": instruction,
        "from": "discord",
        "timestamp": datetime.now(tz=UTC).isoformat(),
        "read": False,
    }
    _INBOX_FILE.write_text(json.dumps(payload, indent=2))
    logger.info("Discord → inbox: %s", instruction[:80])


def _read_outbox() -> str | None:
    """Read a pending reply from Claude Code. Returns text and clears the file."""
    if not _OUTBOX_FILE.exists():
        return None
    try:
        data = json.loads(_OUTBOX_FILE.read_text())
        if data.get("sent"):
            return None
        data["sent"] = True
        _OUTBOX_FILE.write_text(json.dumps(data, indent=2))
        return data.get("reply")
    except Exception:
        return None


# ── Main commander class ──────────────────────────────────────────────────────

class DiscordCommander:
    """
    Bidirectional Discord bridge. Runs as a background asyncio task.

    Pass the AgoraSession instance after construction via set_session().
    The commander starts polling immediately; commands that need the session
    return an error until set_session() is called.
    """

    def __init__(self, token: str, user_id: str, db_path: str,
                 calibration_path: str = ".agora/calibration_report.json") -> None:
        self._token      = token
        self._user_id    = user_id
        self._db_path    = db_path
        self._cal_path   = calibration_path
        self._session: Any = None
        self._channel_id: str | None = None
        self._last_msg_id: str | None = None
        self._running    = False

    def set_session(self, session: Any) -> None:
        self._session = session

    async def start(self) -> None:
        self._running = True
        logger.info("DiscordCommander starting — polling every %ds", POLL_INTERVAL)
        # Open DM channel on first run
        self._channel_id = await _open_dm(self._token, self._user_id)
        if not self._channel_id:
            logger.error("DiscordCommander: could not open DM channel — commander inactive")
            return
        # Anchor: treat all existing messages as already seen
        msgs = await _api("get", f"/channels/{self._channel_id}/messages", self._token,
                          params={"limit": 1})
        if isinstance(msgs, list) and msgs:
            self._last_msg_id = msgs[0]["id"]

        await _send(self._token, self._channel_id,
                    "🤖 **AGORA online** — type `!help` for available commands.")

        while self._running:
            try:
                await self._poll_once()
            except Exception as exc:
                logger.warning("DiscordCommander poll error: %s", exc)
            await asyncio.sleep(POLL_INTERVAL)

    async def stop(self) -> None:
        self._running = False
        if self._channel_id:
            try:
                await _send(self._token, self._channel_id, "🔌 AGORA going offline.")
            except Exception:
                pass

    async def _poll_once(self) -> None:
        if not self._channel_id:
            return

        # ── Check outbox (Claude Code → Discord) ──────────────────────
        reply = _read_outbox()
        if reply:
            await _send(self._token, self._channel_id, f"🤖 **Claude Code**:\n{reply}")

        # ── Fetch new messages ─────────────────────────────────────────
        if self._last_msg_id:
            messages = await _fetch_after(self._token, self._channel_id, self._last_msg_id)
        else:
            messages = []

        for msg in reversed(messages):  # oldest first
            self._last_msg_id = msg["id"]
            text = msg.get("content", "").strip()
            if not text:
                continue

            response = await self._handle(text)
            if response:
                await _send(self._token, self._channel_id, response)

    async def _handle(self, text: str) -> str | None:
        lower = text.lower().strip()

        # Claude Code bridge — prefix ">>"
        if text.startswith(">>"):
            instruction = text[2:].strip()
            if instruction:
                _write_inbox(instruction)
                return f"📥 Sent to Claude Code: `{instruction[:100]}`\nI'll reply here when done."
            return None

        if not lower.startswith("!"):
            return None  # ignore non-commands

        parts = lower.split()
        cmd   = parts[0]

        if cmd == "!help":
            return _HELP

        if cmd == "!positions":
            if not self._session:
                return "⚠️ Session not ready yet."
            return _fmt_positions(self._session)

        if cmd == "!health":
            if not self._session:
                return "⚠️ Session not ready yet."
            return _fmt_health(self._session)

        if cmd == "!kill":
            if not self._session:
                return "⚠️ Session not ready yet."
            return _arm_kill(self._session)

        if cmd == "!unkill":
            if not self._session:
                return "⚠️ Session not ready yet."
            return _disarm_kill(self._session)

        if cmd == "!close":
            if not self._session:
                return "⚠️ Session not ready yet."
            if len(parts) < 2:
                return "Usage: `!close TICKER`"
            return await _close_ticker(self._session, parts[1].upper())

        if cmd == "!lessons":
            return _fmt_lessons(self._db_path)

        if cmd == "!approve":
            if len(parts) < 2 or not parts[1].isdigit():
                return "Usage: `!approve <lesson_id>`"
            return _approve_lesson(self._db_path, int(parts[1]))

        if cmd == "!reject":
            if len(parts) < 2 or not parts[1].isdigit():
                return "Usage: `!reject <lesson_id>`"
            return _reject_lesson(self._db_path, int(parts[1]))

        if cmd == "!calibration":
            return _fmt_calibration(self._cal_path)

        return f"❓ Unknown command `{cmd}`. Type `!help`."


# ── Standalone helper used by /loop ──────────────────────────────────────────

def write_outbox(reply: str) -> None:
    """
    Called by Claude Code after completing an inbox instruction.
    Writes the reply so DiscordCommander picks it up on next poll.
    """
    _OUTBOX_FILE.parent.mkdir(parents=True, exist_ok=True)
    _OUTBOX_FILE.write_text(json.dumps({
        "reply": reply,
        "timestamp": datetime.now(tz=UTC).isoformat(),
        "sent": False,
    }, indent=2))


def read_inbox() -> dict | None:
    """
    Called by Claude Code /loop to check for pending instructions.
    Returns the instruction dict if unread, None otherwise.
    """
    if not _INBOX_FILE.exists():
        return None
    try:
        data = json.loads(_INBOX_FILE.read_text())
        if data.get("read"):
            return None
        data["read"] = True
        _INBOX_FILE.write_text(json.dumps(data, indent=2))
        return data
    except Exception:
        return None

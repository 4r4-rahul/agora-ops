"""
StrategyHealthAgent — rolling P&L Sharpe monitor per (pillar, regime).

Pauses a pillar/regime cell when rolling 30-day Sharpe < -0.5 over ≥ MIN_TRADES.
Unpauses automatically when Sharpe recovers above RECOVERY_THRESHOLD.

Pause state lives in the `pillar_pauses` table (SQLite, agora.db).
RiskCouncil reads this table synchronously during approve_trade().

Design rules:
  - Pure Python, no LLM. Zero API cost.
  - MIN_TRADES = 20 before any pause/unpause action (prevents noise).
  - Runs every PATROL_INTERVAL_SEC (1800 = 30 min).
  - Pillar "all" pause row blocks ALL regimes for that pillar.
  - Regime-specific row blocks that (pillar, regime) combo only.
  - Never pauses during the first 90 days — not enough sample.
"""

from __future__ import annotations

import asyncio
import logging
import math
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ..core.config import AgoraSettings, get_settings

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

MIN_TRADES          = 20      # minimum closed trades in cell before acting
SHARPE_PAUSE_THRESH = -0.5    # pause when Sharpe drops below this
SHARPE_RECOVERY     = 0.0     # unpause when Sharpe recovers above this
LOOKBACK_DAYS       = 30      # rolling window
PATROL_INTERVAL_SEC = 1800    # 30 min patrol


def ensure_table(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pillar_pauses (
                pillar        TEXT NOT NULL,
                regime        TEXT NOT NULL DEFAULT 'all',
                paused_at     TEXT NOT NULL,
                reason        TEXT NOT NULL DEFAULT '',
                sharpe_at_pause REAL,
                trade_count   INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (pillar, regime)
            )
        """)
        conn.commit()


def _compute_sharpe(pnls: list[float]) -> float | None:
    """Rolling Sharpe = mean(pnl) / std(pnl). Returns None if < 2 samples."""
    n = len(pnls)
    if n < 2:
        return None
    mean = sum(pnls) / n
    variance = sum((x - mean) ** 2 for x in pnls) / (n - 1)
    std = math.sqrt(variance) if variance > 0 else 0.0
    if std == 0:
        return 1.0 if mean > 0 else (-1.0 if mean < 0 else 0.0)
    return mean / std


def compute_health(db_path: str) -> dict[str, dict]:
    """
    Compute rolling 30-day Sharpe per (pillar, regime) from trade_records.
    Returns: {"{pillar}:{regime}": {"sharpe": float|None, "count": int, "mean_pnl": float}}
    """
    cutoff = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                """SELECT pillar, COALESCE(regime_at_entry, 'neutral') as regime,
                          realized_pnl
                   FROM trade_records
                   WHERE close_date IS NOT NULL
                     AND close_date >= ?
                     AND realized_pnl IS NOT NULL
                   ORDER BY pillar, regime""",
                (cutoff,),
            ).fetchall()
    except Exception as exc:
        logger.warning("StrategyHealth DB read failed: %s", exc)
        return {}

    # Group by (pillar, regime)
    cells: dict[str, list[float]] = {}
    for pillar, regime, pnl in rows:
        key = f"{pillar}:{regime}"
        cells.setdefault(key, []).append(float(pnl))

    result: dict[str, dict] = {}
    for key, pnls in cells.items():
        pillar, regime = key.split(":", 1)
        sharpe = _compute_sharpe(pnls)
        result[key] = {
            "pillar":   pillar,
            "regime":   regime,
            "count":    len(pnls),
            "mean_pnl": round(sum(pnls) / len(pnls), 2),
            "sharpe":   round(sharpe, 3) if sharpe is not None else None,
        }
    return result


def get_paused_cells(db_path: str) -> dict[str, dict]:
    """
    Return currently paused (pillar, regime) cells.
    Key: "{pillar}:{regime}"
    """
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT pillar, regime, paused_at, reason, sharpe_at_pause, trade_count FROM pillar_pauses"
            ).fetchall()
        return {
            f"{r[0]}:{r[1]}": {
                "pillar": r[0], "regime": r[1], "paused_at": r[2],
                "reason": r[3], "sharpe_at_pause": r[4], "trade_count": r[5],
            }
            for r in rows
        }
    except Exception:
        return {}


def is_pillar_paused(db_path: str, pillar: str, regime: str) -> tuple[bool, str]:
    """
    Check if a (pillar, regime) combo is currently paused.
    Returns (paused, reason). Checks both specific and 'all' regime rows.
    """
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                """SELECT reason FROM pillar_pauses
                   WHERE pillar = ? AND (regime = ? OR regime = 'all')""",
                (pillar, regime),
            ).fetchall()
        if rows:
            return True, rows[0][0]
    except Exception:
        pass
    return False, ""


class StrategyHealthAgent:
    """
    Background patrol: every 30 min, compute rolling Sharpe per (pillar, regime)
    and auto-pause/unpause cells based on thresholds.

    No C-suite inheritance — this is a lightweight ops agent.
    """

    def __init__(self, settings: AgoraSettings | None = None, ceo_agent: Any = None) -> None:
        self._settings = settings or get_settings()
        self._ceo = ceo_agent
        self._running = False
        ensure_table(str(self._settings.db_path))

    async def start(self) -> None:
        self._running = True
        logger.info("StrategyHealthAgent started (patrol every %ds)", PATROL_INTERVAL_SEC)
        # Initial patrol on startup
        await asyncio.sleep(60)   # brief startup delay
        while self._running:
            try:
                await self._patrol()
            except Exception as exc:
                logger.error("StrategyHealthAgent patrol error: %s", exc)
            await asyncio.sleep(PATROL_INTERVAL_SEC)

    async def stop(self) -> None:
        self._running = False

    async def _patrol(self) -> None:
        db_path = str(self._settings.db_path)
        health = compute_health(db_path)

        if not health:
            logger.debug("StrategyHealth: no closed trades in rolling window")
            return

        paused = get_paused_cells(db_path)
        newly_paused: list[str] = []
        newly_unpaused: list[str] = []

        with sqlite3.connect(db_path) as conn:
            for key, stats in health.items():
                pillar  = stats["pillar"]
                regime  = stats["regime"]
                count   = stats["count"]
                sharpe  = stats["sharpe"]
                mean_pnl = stats["mean_pnl"]

                if sharpe is None or count < MIN_TRADES:
                    logger.debug(
                        "StrategyHealth %s:%s — count=%d (need %d), sharpe=%s — monitoring",
                        pillar, regime, count, MIN_TRADES, sharpe,
                    )
                    continue

                currently_paused = key in paused

                if not currently_paused and sharpe < SHARPE_PAUSE_THRESH:
                    reason = (
                        f"Sharpe={sharpe:.3f} < {SHARPE_PAUSE_THRESH} over "
                        f"{count} trades (30d). Mean P&L=${mean_pnl:.0f}/trade."
                    )
                    conn.execute(
                        """INSERT OR REPLACE INTO pillar_pauses
                           (pillar, regime, paused_at, reason, sharpe_at_pause, trade_count)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (pillar, regime, datetime.now(tz=ET).isoformat(),
                         reason, sharpe, count),
                    )
                    newly_paused.append(f"{pillar}/{regime} (Sharpe={sharpe:.3f})")
                    logger.warning(
                        "StrategyHealth PAUSED: %s:%s | %s", pillar, regime, reason
                    )

                elif currently_paused and sharpe >= SHARPE_RECOVERY:
                    conn.execute(
                        "DELETE FROM pillar_pauses WHERE pillar = ? AND regime = ?",
                        (pillar, regime),
                    )
                    newly_unpaused.append(f"{pillar}/{regime} (Sharpe recovered to {sharpe:.3f})")
                    logger.info(
                        "StrategyHealth UNPAUSED: %s:%s | Sharpe recovered to %.3f",
                        pillar, regime, sharpe,
                    )

                else:
                    logger.info(
                        "StrategyHealth %s:%s — count=%d sharpe=%.3f mean_pnl=$%.0f paused=%s",
                        pillar, regime, count, sharpe, mean_pnl, currently_paused,
                    )

            conn.commit()

        # Escalate to CEO if anything changed
        if newly_paused and self._ceo:
            msg = "StrategyHealth AUTO-PAUSED:\n" + "\n".join(f"  🚨 {p}" for p in newly_paused)
            try:
                await self._ceo.dispatch_alert("critical", f"[StrategyHealth] {msg}")
            except Exception:
                pass

        if newly_unpaused and self._ceo:
            msg = "StrategyHealth UNPAUSED (Sharpe recovered):\n" + "\n".join(f"  ✓ {p}" for p in newly_unpaused)
            try:
                await self._ceo.dispatch_alert("info", f"[StrategyHealth] {msg}")
            except Exception:
                pass

    def get_status(self) -> dict:
        """Synchronous status snapshot for dashboard and RND brief."""
        db_path = str(self._settings.db_path)
        health  = compute_health(db_path)
        paused  = get_paused_cells(db_path)
        return {
            "paused_count": len(paused),
            "paused_cells": list(paused.values()),
            "health":       list(health.values()),
            "min_trades_required": MIN_TRADES,
            "sharpe_pause_threshold": SHARPE_PAUSE_THRESH,
        }

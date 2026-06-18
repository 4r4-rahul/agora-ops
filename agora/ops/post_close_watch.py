"""
agora/ops/post_close_watch.py — post-close counterfactual (roadmap S0.2, the keystone).

After a position closes we keep watching the UNDERLYING for a window, then ask the only question
that improves exit timing: *did the move continue in our favor (we exited too EARLY and left money)
or against us (the exit was CORRECT and protected the book)?* Aggregated **by exit reason** (stop /
profit-target / LLM-thesis / time/DTE), this tells us empirically which exit type cuts winners short
— the data the day-0 churn investigation needed.

Mirrors the proven shadow_book shape: record → evaluate_due → report. Underlying-path proxy (we have
no option marks after close), same philosophy as shadow_book. Read/observe only — never trades,
never raises. Price fetch is injectable so it is fully unit-testable offline.
"""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

_HORIZON_DAYS = 7          # how long we keep watching after the close
_MOVE_THRESHOLD = 0.015    # 1.5% underlying move = "material" (above option/spread noise)

_CREATE = """
CREATE TABLE IF NOT EXISTS post_close_watch (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id     TEXT,
    ticker          TEXT NOT NULL,
    strategy        TEXT,
    direction       TEXT,
    exit_reason     TEXT,
    closed_at_utc   TEXT NOT NULL,
    close_date      TEXT NOT NULL,
    spot_at_close   REAL,
    realized_pnl    REAL,
    horizon_date    TEXT NOT NULL,
    evaluated       INTEGER DEFAULT 0,
    favorable_move_pct REAL,    -- best move in our favor over the window (signed +)
    adverse_move_pct   REAL,    -- worst move against us
    regret_label    TEXT,       -- EARLY_EXIT | CORRECT_EXIT | NEUTRAL | UNMEASURABLE
    regret_pct      REAL        -- favorable move we left on the table (0 unless EARLY_EXIT)
);
CREATE INDEX IF NOT EXISTS idx_pcw_eval ON post_close_watch(evaluated, horizon_date);
CREATE INDEX IF NOT EXISTS idx_pcw_reason ON post_close_watch(exit_reason);
"""


def _ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(_CREATE)


def _bullish(direction: str, strategy: str) -> bool | None:
    """Which way helps the CLOSED position? True=up helps, False=down helps, None=undecidable."""
    d, s = (direction or "").lower(), (strategy or "").lower()
    if d == "bullish":
        return True
    if d == "bearish":
        return False
    # fall back to structure. Check the DIRECTIONAL word first — spread names carry both legs
    # ("bear_call_spread" contains "call" but is bearish), so bull/bear must win over call/put.
    if "bull" in s:
        return True
    if "bear" in s:
        return False
    if "call" in s:        # long_call
        return True
    if "put" in s:         # long_put
        return False
    return None


def _spot_now(ticker: str) -> float | None:
    """Best-effort current underlying price (for spot_at_close). None on any failure.
    Sync yfinance — the provider's get_snapshot is async and cannot be awaited from this hook."""
    try:
        import yfinance as yf
        h = yf.Ticker(ticker).history(period="1d")
        return float(h["Close"].iloc[-1]) if h is not None and not h.empty else None
    except Exception:
        return None


def record_close(db_path: str, position: Any, exit_reason: str, spot: float | None = None) -> None:
    """Register a just-closed position to watch its post-exit path. Best-effort; never raises.
    Runs AFTER the broker close is confirmed, so the spot lookup never delays an order."""
    try:
        if not spot or spot <= 0:
            spot = _spot_now(getattr(position, "ticker", "")) or 0.0
        horizon = (date.today() + timedelta(days=_HORIZON_DAYS)).isoformat()
        strat = str(getattr(getattr(position, "strategy", ""), "value", getattr(position, "strategy", "")))
        with sqlite3.connect(db_path, timeout=10) as conn:
            _ensure(conn)
            conn.execute(
                """INSERT INTO post_close_watch
                   (position_id, ticker, strategy, direction, exit_reason, closed_at_utc, close_date,
                    spot_at_close, realized_pnl, horizon_date)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (str(getattr(position, "position_id", "")), getattr(position, "ticker", ""),
                 strat, str(getattr(position, "direction", "")), str(exit_reason or "unknown"),
                 datetime.now(tz=UTC).isoformat(), date.today().isoformat(),
                 float(spot or 0) or None,
                 float(getattr(position, "unrealized_pnl", 0) or 0), horizon),
            )
    except Exception as exc:
        logger.debug("post_close_watch.record_close: %s", exc)


def _default_price_fn(ticker: str, start: str, end: str) -> list[float]:
    """Daily closes for ticker over the watch window. Sync yfinance; [] on any failure."""
    try:
        import yfinance as yf
        hist = yf.Ticker(ticker).history(start=start, end=end)
        return [float(x) for x in (hist["Close"].tolist() if hist is not None and not hist.empty else [])]
    except Exception:
        return []


def evaluate_due(db_path: str, price_fn: Callable[[str, str, str], list[float]] | None = None,
                 today: date | None = None) -> int:
    """Score every watch row whose horizon has passed. Returns count evaluated. Never raises."""
    price_fn = price_fn or _default_price_fn
    today = today or date.today()
    n = 0
    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            _ensure(conn)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM post_close_watch WHERE evaluated=0 AND horizon_date <= ?",
                (today.isoformat(),),
            ).fetchall()
            for r in rows:
                label, fav, adv, regret = _score(r, price_fn)
                conn.execute(
                    """UPDATE post_close_watch SET evaluated=1, favorable_move_pct=?,
                       adverse_move_pct=?, regret_label=?, regret_pct=? WHERE id=?""",
                    (fav, adv, label, regret, r["id"]),
                )
                n += 1
        if n:
            logger.info("post_close_watch: evaluated %d closed-trade counterfactual(s)", n)
    except Exception as exc:
        logger.debug("post_close_watch.evaluate_due: %s", exc)
    return n


def _score(r: sqlite3.Row, price_fn) -> tuple[str, float | None, float | None, float]:
    spot0 = r["spot_at_close"]
    bull = _bullish(r["direction"], r["strategy"])
    if not spot0 or spot0 <= 0 or bull is None:
        return "UNMEASURABLE", None, None, 0.0
    path = price_fn(r["ticker"], r["close_date"], r["horizon_date"])
    if not path:
        return "UNMEASURABLE", None, None, 0.0
    hi, lo = max(path), min(path)
    if bull:                       # up helps: favorable = highest, adverse = lowest
        fav = (hi - spot0) / spot0
        adv = (lo - spot0) / spot0          # negative
    else:                          # down helps: favorable = how far it fell, adverse = how far it rose
        fav = (spot0 - lo) / spot0
        adv = (spot0 - hi) / spot0          # negative
    fav, adv = round(fav, 4), round(adv, 4)
    if fav >= _MOVE_THRESHOLD:
        return "EARLY_EXIT", fav, adv, fav          # the move continued our way → we left money
    if adv <= -_MOVE_THRESHOLD:
        return "CORRECT_EXIT", fav, adv, 0.0        # it went against us → exit protected the book
    return "NEUTRAL", fav, adv, 0.0


def exit_regret_report(db_path: str) -> dict[str, Any]:
    """Aggregate evaluated counterfactuals BY EXIT REASON → which exit type cuts winners short."""
    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            _ensure(conn)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT exit_reason, regret_label, favorable_move_pct, regret_pct "
                "FROM post_close_watch WHERE evaluated=1 AND regret_label<>'UNMEASURABLE'"
            ).fetchall()
        by: dict[str, dict[str, Any]] = {}
        for r in rows:
            b = by.setdefault(r["exit_reason"] or "unknown",
                              {"n": 0, "early": 0, "correct": 0, "neutral": 0, "_regret": []})
            b["n"] += 1
            b["_regret"].append(r["regret_pct"] or 0.0)
            b["early"] += r["regret_label"] == "EARLY_EXIT"
            b["correct"] += r["regret_label"] == "CORRECT_EXIT"
            b["neutral"] += r["regret_label"] == "NEUTRAL"
        out = {}
        for reason, b in by.items():
            nz = b["n"] or 1
            out[reason] = {
                "n": b["n"],
                "early_exit_rate": round(b["early"] / nz, 3),
                "correct_exit_rate": round(b["correct"] / nz, 3),
                "mean_regret_pct": round(sum(b["_regret"]) / nz, 4),
            }
        return {"by_exit_reason": out, "total_evaluated": sum(b["n"] for b in by.values())}
    except Exception as exc:
        return {"error": str(exc)}

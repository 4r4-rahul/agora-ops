"""
agora/ops/shadow_book.py — counterfactual outcomes for BLOCKED trades (loop step 3).

A LIVE advocate BLOCK stops the trade, so it never fills → no position → its precision is
unmeasurable from fills (the structural hole found in the investigation: BLOCK precision stayed
permanently NULL, so the advocate could never be validated or graduate).

This records each blocked trade's structure at block time, then later evaluates what it WOULD have
done from the underlying's actual move vs the structure's profit zone — a coarse but honest
binary win/loss proxy (the magnitude is approximate; the WIN/LOSS is what BLOCK precision needs).
The attributor then scores a BLOCK as "right" iff the shadow trade would have LOST.

Proxy model (held to a fixed horizon, evaluated on the underlying close):
  • credit vertical (bull_put / bear_call): wins if the underlying stays on the favorable side of
    the SHORT strike (bullish → spot ≥ short put; bearish → spot ≤ short call).
  • debit vertical / long option: wins if the underlying ends past the breakeven in the thesis
    direction.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, date, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

_HORIZON_DAYS = 7   # thesis-resolution window for the counterfactual read

_CREATE = """
CREATE TABLE IF NOT EXISTS shadow_book (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id       TEXT,
    ticker            TEXT NOT NULL,
    strategy          TEXT,
    direction         TEXT,
    blocked_at_utc    TEXT NOT NULL,
    spot_at_block     REAL,
    short_strike      REAL,
    breakeven         REAL,
    entry_debit_credit REAL,
    max_gain          REAL,
    max_loss          REAL,
    horizon_date      TEXT NOT NULL,
    evaluated         INTEGER DEFAULT 0,
    spot_at_horizon   REAL,
    hypothetical_win  INTEGER,
    hypothetical_pnl  REAL
);
CREATE INDEX IF NOT EXISTS idx_shadow_decision ON shadow_book(decision_id);
CREATE INDEX IF NOT EXISTS idx_shadow_eval ON shadow_book(evaluated, horizon_date);
"""


def _ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(_CREATE)


def record_block(db_path: str, decision_id: str, ticker: str, rec: Any, spot: float) -> None:
    """Record a BLOCKED trade for later counterfactual scoring. Best-effort; never raises."""
    try:
        legs = list(getattr(rec, "legs", []) or [])
        short_strike = next(
            (float(l.strike) for l in legs if str(getattr(l, "action", "")).lower() == "sell"),
            None,
        )
        if short_strike is None and legs:   # long option: its single strike
            short_strike = float(legs[0].strike)
        horizon = (date.today() + timedelta(days=_HORIZON_DAYS)).isoformat()
        with sqlite3.connect(db_path, timeout=10) as conn:
            _ensure(conn)
            conn.execute(
                """INSERT INTO shadow_book
                   (decision_id, ticker, strategy, direction, blocked_at_utc, spot_at_block,
                    short_strike, breakeven, entry_debit_credit, max_gain, max_loss, horizon_date)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (decision_id, ticker,
                 str(getattr(getattr(rec, "strategy", ""), "value", getattr(rec, "strategy", ""))),
                 str(getattr(rec, "direction", "")),
                 datetime.now(tz=UTC).isoformat(), float(spot or 0),
                 short_strike, getattr(rec, "breakeven_price", None),
                 float(getattr(rec, "entry_debit_credit", 0) or 0),
                 float(getattr(rec, "max_gain_dollars", 0) or 0),
                 float(getattr(rec, "max_loss_dollars", 0) or 0), horizon),
            )
    except Exception as exc:
        logger.debug("shadow_book.record_block: %s", exc)


def _hypothetical_win(strategy: str, direction: str, short_strike: float | None,
                      breakeven: float | None, entry_cd: float, spot_h: float) -> bool | None:
    """True=win, False=loss, None=can't decide. Coarse underlying-vs-profit-zone proxy."""
    s = (strategy or "").lower()
    d = (direction or "").lower()
    # Unplumbed price: entry_debit_credit==0 means we don't know credit-vs-debit, so we can't
    # pick the right proxy branch. Return None (unmeasurable) rather than mis-branching a credit
    # spread onto the debit logic.
    if not entry_cd:
        return None
    is_credit = entry_cd < 0
    if is_credit and short_strike:
        if "bull" in s or d == "bullish":      # bull put: win if spot stays ≥ short put
            return spot_h >= short_strike
        if "bear" in s or d == "bearish":      # bear call: win if spot stays ≤ short call
            return spot_h <= short_strike
    # Debit vertical / long option → directional vs BREAKEVEN only. Do NOT fall back to
    # short_strike: for a bear_put_spread the short strike is the max-PROFIT (lower) leg, so using
    # it as the win line scores nearly every bearish debit as a LOSS — a bias that spuriously
    # inflates BLOCK precision and could promote a fail-closed advocate on bad data. If breakeven
    # is missing, leave the row UNMEASURABLE rather than guess.
    if not breakeven or breakeven <= 0:
        return None
    if d == "bullish" or "call" in s or "bull" in s:
        return spot_h > breakeven
    if d == "bearish" or "put" in s or "bear" in s:
        return spot_h < breakeven
    return None


def evaluate_due(db_path: str) -> int:
    """Evaluate shadow rows whose horizon has passed, using the underlying close at the horizon."""
    try:
        from agora.scan.market_snapshot import get_market_snapshot
        ms = get_market_snapshot()
    except Exception:
        ms = None
    evaluated = 0
    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            _ensure(conn)
            today = date.today().isoformat()
            rows = conn.execute(
                """SELECT id, ticker, strategy, direction, short_strike, breakeven,
                          entry_debit_credit, max_gain, max_loss, horizon_date
                   FROM shadow_book WHERE evaluated=0 AND horizon_date <= ?""",
                (today,),
            ).fetchall()
            for (sid, ticker, strat, direction, short_strike, breakeven,
                 entry_cd, max_gain, max_loss, horizon_date) in rows:
                spot_h = _close_on_or_before(ms, ticker, horizon_date)
                if spot_h is None or spot_h <= 0:
                    continue   # leave unevaluated; retry next cycle
                win = _hypothetical_win(strat, direction, short_strike, breakeven,
                                        entry_cd or 0, spot_h)
                if win is None:
                    conn.execute("UPDATE shadow_book SET evaluated=1, spot_at_horizon=? WHERE id=?",
                                 (spot_h, sid))
                    continue
                pnl = (max_gain or 0) if win else -(max_loss or 0)
                conn.execute(
                    """UPDATE shadow_book
                       SET evaluated=1, spot_at_horizon=?, hypothetical_win=?, hypothetical_pnl=?
                       WHERE id=?""",
                    (spot_h, 1 if win else 0, round(pnl, 2), sid),
                )
                evaluated += 1
            if evaluated:
                logger.info("ShadowBook: evaluated %d blocked-trade counterfactuals", evaluated)
    except Exception as exc:
        logger.debug("shadow_book.evaluate_due: %s", exc)
    return evaluated


def _close_on_or_before(ms: Any, ticker: str, horizon_date: str) -> float | None:
    """Underlying close on/just-before horizon_date from cached daily history."""
    if ms is None:
        return None
    try:
        hist = ms.history(ticker, period="3mo", interval="1d")
        if hist is None or getattr(hist, "empty", True):
            return None
        import pandas as pd  # noqa
        h = hist.copy()
        h.index = [str(i)[:10] for i in h.index]
        on_or_before = [d for d in h.index if d <= horizon_date]
        if not on_or_before:
            return None
        return float(h.loc[on_or_before[-1], "Close"])
    except Exception:
        return None

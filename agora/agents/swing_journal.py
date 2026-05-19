"""
SwingJournal — SQLite-backed trade journal for swing decisions.

Records every go/no-go decision, fill, and close event.
Triggers Claude self-audit on position close to extract lessons.
Feeds last-N entries back to SwingJudgeAgent as learning context.

Schema (swing_journal table):
  ticker, decision_ts, session_id
  raw_score, factor_breakdown (JSON)
  go, option_type, direction, entry_condition
  strike, target_expiry_dte, price_at_decision
  price_target, stop_price, max_hold_days, confidence
  key_thesis, what_kills_trade, journal_text, method
  entry_order_id, fill_price, fill_ts
  close_price, close_ts, pnl_dollars, pnl_pct
  outcome (open|win|loss|expired|manual_close)
  post_trade_audit, prediction_accuracy, lesson_learned
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anthropic

from ..core.config import AgoraSettings, get_settings
from ..ops.llm_cost_log import log_call as _log_llm
from .swing_judge import SwingDecision

logger = logging.getLogger(__name__)

_AUDIT_SYSTEM = """\
You are a post-trade auditor for a swing options trading system.

You receive the complete record of a swing trade: the pre-trade thesis, the entry and
exit data, and the P&L outcome. Your job is to write a concise self-audit that helps
the system learn and improve future decisions.

Be honest and critical. If the trade lost, explain exactly what went wrong vs. what was
assumed. If it won, explain whether the win came from skill or luck.

Output ONLY valid JSON (no prose, no markdown):
{
  "prediction_accuracy": <float 0.0-1.0, how accurate was the original thesis>,
  "what_went_right": "<1-2 sentences>",
  "what_went_wrong": "<1-2 sentences or 'N/A'>",
  "lesson_learned": "<1 sentence — actionable rule for future decisions>",
  "post_trade_audit": "<3-4 sentence narrative of the full trade lifecycle>"
}
"""

_CACHED_AUDIT_SYSTEM = [
    {"type": "text", "text": _AUDIT_SYSTEM, "cache_control": {"type": "ephemeral"}}
]


class SwingJournal:
    """
    Persistent swing trade journal.
    Thread-safe via WAL mode; safe to call from async context.
    """

    def __init__(self, settings: AgoraSettings | None = None) -> None:
        self._settings = settings or get_settings()
        self._client = anthropic.AsyncAnthropic(api_key=self._settings.anthropic_api_key)
        self._db = self._init_db()

    def _init_db(self) -> sqlite3.Connection:
        db_path = self._settings.db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS swing_journal (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker            TEXT NOT NULL,
                decision_ts       TEXT NOT NULL,
                session_id        TEXT NOT NULL DEFAULT '',
                raw_score         REAL NOT NULL DEFAULT 0,
                factor_breakdown  TEXT NOT NULL DEFAULT '{}',
                go                INTEGER NOT NULL DEFAULT 0,
                option_type       TEXT NOT NULL DEFAULT '',
                direction         TEXT NOT NULL DEFAULT '',
                entry_condition   TEXT NOT NULL DEFAULT '',
                strike            REAL,
                target_expiry_dte INTEGER,
                price_at_decision REAL,
                price_target      REAL,
                stop_price        REAL,
                max_hold_days     INTEGER,
                confidence        REAL NOT NULL DEFAULT 0,
                key_thesis        TEXT NOT NULL DEFAULT '',
                what_kills_trade  TEXT NOT NULL DEFAULT '',
                journal_text      TEXT NOT NULL DEFAULT '',
                method            TEXT NOT NULL DEFAULT 'claude',
                entry_order_id    TEXT,
                fill_price        REAL,
                fill_ts           TEXT,
                close_price       REAL,
                close_ts          TEXT,
                pnl_dollars       REAL,
                pnl_pct           REAL,
                outcome           TEXT NOT NULL DEFAULT 'no_trade',
                post_trade_audit  TEXT,
                prediction_accuracy REAL,
                lesson_learned    TEXT
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_swing_ticker_ts ON swing_journal(ticker, decision_ts)"
        )
        conn.commit()
        return conn

    # ── Write path ─────────────────────────────────────────────────

    def record_decision(
        self,
        ticker: str,
        decision: SwingDecision,
        price_at_decision: float,
        session_id: str = "",
    ) -> int:
        """Insert a go/no-go decision. Returns the new row id."""
        outcome = "open" if decision.go else "no_trade"
        cur = self._db.execute("""
            INSERT INTO swing_journal (
                ticker, decision_ts, session_id,
                raw_score, factor_breakdown,
                go, option_type, direction, entry_condition,
                strike, target_expiry_dte, price_at_decision,
                price_target, stop_price, max_hold_days, confidence,
                key_thesis, what_kills_trade, journal_text, method, outcome
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            ticker,
            decision.timestamp.isoformat(),
            session_id,
            decision.raw_score,
            json.dumps(decision.factor_breakdown),
            int(decision.go),
            decision.option_type,
            decision.direction,
            decision.entry_condition,
            decision.strike,
            decision.target_expiry_dte,
            price_at_decision,
            decision.price_target,
            decision.stop_price,
            decision.max_hold_days,
            decision.confidence,
            decision.key_thesis,
            decision.what_kills_trade,
            decision.journal_text,
            decision.method,
            outcome,
        ))
        self._db.commit()
        row_id = cur.lastrowid
        logger.info(
            "SwingJournal: recorded %s %s decision row_id=%d go=%s score=%.0f",
            ticker, decision.direction, row_id, decision.go, decision.raw_score,
        )
        return row_id

    def record_fill(
        self,
        journal_id: int,
        order_id: str,
        fill_price: float,
        fill_ts: datetime | None = None,
    ) -> None:
        """Update a 'go' decision once IBKR confirms the fill."""
        ts = (fill_ts or datetime.now(tz=timezone.utc)).isoformat()
        self._db.execute("""
            UPDATE swing_journal
            SET entry_order_id=?, fill_price=?, fill_ts=?, outcome='open'
            WHERE id=?
        """, (order_id, fill_price, ts, journal_id))
        self._db.commit()
        logger.info("SwingJournal: fill recorded journal_id=%d price=%.2f", journal_id, fill_price)

    def record_close(
        self,
        journal_id: int,
        close_price: float,
        pnl_dollars: float,
        outcome: str,               # "win" | "loss" | "expired" | "manual_close"
        close_ts: datetime | None = None,
    ) -> None:
        """Update a position with its closing data."""
        ts = (close_ts or datetime.now(tz=timezone.utc)).isoformat()
        row = self._db.execute(
            "SELECT fill_price FROM swing_journal WHERE id=?", (journal_id,)
        ).fetchone()
        fill_price = row[0] if row and row[0] else 0.0
        pnl_pct = (pnl_dollars / (fill_price * 100)) * 100 if fill_price else None

        self._db.execute("""
            UPDATE swing_journal
            SET close_price=?, close_ts=?, pnl_dollars=?, pnl_pct=?, outcome=?
            WHERE id=?
        """, (close_price, ts, pnl_dollars, pnl_pct, outcome, journal_id))
        self._db.commit()
        logger.info(
            "SwingJournal: close recorded journal_id=%d outcome=%s pnl=$%.0f",
            journal_id, outcome, pnl_dollars,
        )

    # ── Self-audit ─────────────────────────────────────────────────

    async def trigger_self_audit(self, journal_id: int) -> dict[str, Any]:
        """
        Call Claude to write a post-trade audit.
        Called automatically after record_close() when fill_price is set.
        Returns the audit dict; also writes it to the DB.
        """
        row = self._db.execute("""
            SELECT ticker, decision_ts, go, option_type, direction,
                   price_at_decision, price_target, stop_price, fill_price,
                   close_price, pnl_dollars, pnl_pct, outcome,
                   key_thesis, what_kills_trade, journal_text, raw_score,
                   factor_breakdown, confidence, entry_condition, max_hold_days,
                   close_ts, fill_ts
            FROM swing_journal WHERE id=?
        """, (journal_id,)).fetchone()
        if not row:
            return {}

        (ticker, dec_ts, go, opt_type, direction, price_at_dec, price_target,
         stop_price, fill_price, close_price, pnl_dollars, pnl_pct, outcome,
         key_thesis, what_kills, journal_text, raw_score, factor_json,
         confidence, entry_cond, max_hold, close_ts, fill_ts) = row

        if not fill_price:
            logger.debug("SwingJournal: skip audit journal_id=%d — no fill", journal_id)
            return {}

        prompt = _build_audit_prompt(
            ticker=ticker,
            decision_ts=dec_ts,
            go=bool(go),
            option_type=opt_type,
            direction=direction,
            price_at_decision=price_at_dec,
            price_target=price_target,
            stop_price=stop_price,
            fill_price=fill_price,
            close_price=close_price,
            pnl_dollars=pnl_dollars,
            pnl_pct=pnl_pct,
            outcome=outcome,
            key_thesis=key_thesis,
            what_kills=what_kills,
            journal_text=journal_text,
            raw_score=raw_score,
            confidence=confidence,
            entry_condition=entry_cond,
            max_hold_days=max_hold,
            fill_ts=fill_ts,
            close_ts=close_ts,
        )

        try:
            t0 = time.monotonic()
            response = await self._client.messages.create(
                model=self._settings.claude_model,
                max_tokens=400,
                thinking={"type": "adaptive"},
                system=_CACHED_AUDIT_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
                timeout=anthropic.Timeout(connect=30.0, read=120.0, write=30.0, pool=30.0),
            )
            elapsed = time.monotonic() - t0
            logger.info("SwingJournal audit OK in %.1fs | %s id=%d", elapsed, ticker, journal_id)
            if hasattr(response, "usage"):
                _log_llm(str(self._settings.db_path), "SwingJournal", self._settings.claude_model,
                         response.usage.input_tokens, response.usage.output_tokens, purpose="post_trade_audit")

            text_blocks = [b for b in response.content if b.type == "text"]
            if not text_blocks:
                return {}

            raw = text_blocks[-1].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1].lstrip("json").strip()

            data = json.loads(raw)
            self._db.execute("""
                UPDATE swing_journal
                SET post_trade_audit=?, prediction_accuracy=?, lesson_learned=?
                WHERE id=?
            """, (
                data.get("post_trade_audit", ""),
                _float(data.get("prediction_accuracy")),
                data.get("lesson_learned", ""),
                journal_id,
            ))
            self._db.commit()
            logger.info(
                "SwingJournal: audit written journal_id=%d accuracy=%.2f lesson=%s",
                journal_id,
                data.get("prediction_accuracy", 0),
                data.get("lesson_learned", "")[:60],
            )
            return data

        except Exception as exc:
            logger.warning("SwingJournal audit failed journal_id=%d: %s", journal_id, exc)
            return {}

    # ── Read path ──────────────────────────────────────────────────

    def get_past_entries(self, ticker: str, limit: int = 5) -> list[dict]:
        """
        Return last `limit` journal entries for a ticker (most recent first).
        Used by SwingJudgeAgent as learning context.
        """
        rows = self._db.execute("""
            SELECT decision_ts, go, direction, outcome, pnl_pct,
                   key_thesis, post_trade_audit, confidence, raw_score
            FROM swing_journal
            WHERE ticker=?
            ORDER BY decision_ts DESC
            LIMIT ?
        """, (ticker, limit)).fetchall()

        return [
            {
                "decision_ts": r[0],
                "go": bool(r[1]),
                "direction": r[2],
                "outcome": r[3],
                "pnl_pct": r[4],
                "key_thesis": r[5],
                "post_trade_audit": r[6],
                "confidence": r[7],
                "raw_score": r[8],
            }
            for r in rows
        ]

    def get_all_open(self) -> list[dict]:
        """Return all open (filled but not closed) swing positions."""
        rows = self._db.execute("""
            SELECT id, ticker, decision_ts, option_type, direction,
                   strike, target_expiry_dte, price_at_decision, fill_price,
                   fill_ts, price_target, stop_price, max_hold_days,
                   confidence, key_thesis, what_kills_trade, entry_order_id
            FROM swing_journal
            WHERE outcome='open' AND fill_price IS NOT NULL
            ORDER BY fill_ts DESC
        """).fetchall()

        return [
            {
                "id": r[0],
                "ticker": r[1],
                "decision_ts": r[2],
                "option_type": r[3],
                "direction": r[4],
                "strike": r[5],
                "target_expiry_dte": r[6],
                "price_at_decision": r[7],
                "fill_price": r[8],
                "fill_ts": r[9],
                "price_target": r[10],
                "stop_price": r[11],
                "max_hold_days": r[12],
                "confidence": r[13],
                "key_thesis": r[14],
                "what_kills_trade": r[15],
                "entry_order_id": r[16],
            }
            for r in rows
        ]

    def get_no_go_summary(self, limit: int = 20) -> list[dict]:
        """Return recent no-go decisions for dashboard display."""
        rows = self._db.execute("""
            SELECT ticker, decision_ts, direction, raw_score, confidence, key_thesis, journal_text
            FROM swing_journal
            WHERE go=0
            ORDER BY decision_ts DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [
            {
                "ticker": r[0],
                "decision_ts": r[1],
                "direction": r[2],
                "raw_score": r[3],
                "confidence": r[4],
                "key_thesis": r[5],
                "journal_text": r[6],
            }
            for r in rows
        ]

    def get_performance_summary(self) -> dict[str, Any]:
        """Aggregate win/loss stats across all closed swing trades."""
        row = self._db.execute("""
            SELECT
                COUNT(*) FILTER (WHERE go=1)                          AS total_taken,
                COUNT(*) FILTER (WHERE outcome='win')                  AS wins,
                COUNT(*) FILTER (WHERE outcome='loss')                 AS losses,
                AVG(pnl_pct) FILTER (WHERE outcome IN ('win','loss'))  AS avg_pnl_pct,
                SUM(pnl_dollars) FILTER (WHERE outcome IN ('win','loss')) AS total_pnl,
                AVG(prediction_accuracy) FILTER (WHERE prediction_accuracy IS NOT NULL) AS avg_accuracy
            FROM swing_journal
        """).fetchone()
        if not row:
            return {}
        total, wins, losses, avg_pnl, total_pnl, avg_acc = row
        win_rate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else None
        return {
            "total_taken": total or 0,
            "wins": wins or 0,
            "losses": losses or 0,
            "win_rate_pct": round(win_rate, 1) if win_rate is not None else None,
            "avg_pnl_pct": round(avg_pnl, 1) if avg_pnl is not None else None,
            "total_pnl_dollars": round(total_pnl, 2) if total_pnl is not None else None,
            "avg_prediction_accuracy": round(avg_acc, 2) if avg_acc is not None else None,
        }


# ── Helpers ────────────────────────────────────────────────────────

def _float(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _build_audit_prompt(
    ticker: str,
    decision_ts: str,
    go: bool,
    option_type: str,
    direction: str,
    price_at_decision: float | None,
    price_target: float | None,
    stop_price: float | None,
    fill_price: float | None,
    close_price: float | None,
    pnl_dollars: float | None,
    pnl_pct: float | None,
    outcome: str,
    key_thesis: str,
    what_kills: str,
    journal_text: str,
    raw_score: float,
    confidence: float,
    entry_condition: str,
    max_hold_days: int | None,
    fill_ts: str | None,
    close_ts: str | None,
) -> str:
    pnl_str = f"${pnl_dollars:+.0f} ({pnl_pct:+.1f}%)" if pnl_dollars is not None and pnl_pct is not None else "n/a"
    lines = [
        f"TRADE AUDIT: {ticker} | Outcome: {outcome.upper()} | P&L: {pnl_str}",
        "",
        "=== PRE-TRADE THESIS ===",
        f"  Decision date:    {decision_ts[:10]}",
        f"  Direction:        {direction} — {option_type}",
        f"  Swing score:      {raw_score:.0f}/100",
        f"  Claude confidence: {confidence:.0%}",
        f"  Entry condition:  {entry_condition}",
        f"  Price at decision: ${price_at_decision:.2f}" if price_at_decision else "  Price at decision: n/a",
        f"  Price target:     ${price_target:.2f}" if price_target else "  Price target: n/a",
        f"  Stop price:       ${stop_price:.2f}" if stop_price else "  Stop price: n/a",
        f"  Max hold days:    {max_hold_days}",
        "",
        f"  Key thesis: {key_thesis}",
        f"  Trade killer: {what_kills}",
        f"  Journal note: {journal_text}",
        "",
        "=== EXECUTION ===",
        f"  Fill price:   ${fill_price:.2f}" if fill_price else "  Fill price: n/a",
        f"  Fill time:    {fill_ts}" if fill_ts else "  Fill time: n/a",
        f"  Close price:  ${close_price:.2f}" if close_price else "  Close price: n/a",
        f"  Close time:   {close_ts}" if close_ts else "  Close time: n/a",
        f"  Final P&L:    {pnl_str}",
        "",
        "Audit this trade objectively. Was the thesis valid? Did the exit respect the plan?",
        "What should the system learn to make better decisions on future similar setups?",
    ]
    return "\n".join(lines)

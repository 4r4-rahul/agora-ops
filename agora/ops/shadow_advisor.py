"""
agora/ops/shadow_advisor.py — Rung 2 of the model→trading promotion ladder: SHADOW.

For each recent trade it records what each model WOULD have advised (had it been live) and pairs that
with the trade's actual P&L — building the dataset that proves (or disproves) a model's edge BEFORE it
is ever allowed to gate a live trade (Rung 3, board-approved). This is OFFLINE: it reads trades +
model_scores and writes only shadow_model_decisions. It does NOT sit in the order/entry/exit path and
calls nothing in it → it CANNOT affect execution. n-gated + never raises.

Models shadowed today:
  • M2 regime — would it down-weight directional DEBITS taken when premium is rich? (IV-crush thesis)
  • M1 fill — would it flag a structure as too hard to fill?
  • M3 liquidity — would it flag the ticker as un-fillable?
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS shadow_model_decisions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_date  TEXT NOT NULL,
    position_id    TEXT,
    ticker         TEXT,
    structure_class TEXT,
    is_credit      INTEGER,
    model          TEXT,
    would_advise   TEXT,
    model_score    REAL,
    trade_pnl      REAL,
    status         TEXT,
    UNIQUE(decision_date, position_id, model)
);
CREATE INDEX IF NOT EXISTS idx_shadow_date ON shadow_model_decisions(decision_date);
"""

_MIN_VALIDATE_N = 20   # below this, the would-be-impact verdict is 'insufficient_n'


def _latest_score(conn: sqlite3.Connection, model: str, entity_id: str) -> tuple[float | None, dict]:
    r = conn.execute(
        """SELECT score, meta_json FROM model_scores WHERE model_name=? AND entity_id=?
           AND score_date=(SELECT MAX(score_date) FROM model_scores WHERE model_name=?) LIMIT 1""",
        (model, entity_id, model)).fetchone()
    if not r:
        return None, {}
    try:
        return r[0], (json.loads(r[1]) if r[1] else {})
    except Exception:
        return r[0], {}


def run_shadow_advisor(db_path: str) -> dict[str, Any]:
    """Record shadow decisions for recent trades + measure would-be impact. Idempotent/day. Never raises."""
    today = datetime.now(UTC).date().isoformat()
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_DDL)
        conn.row_factory = sqlite3.Row
        if not conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='trade_features'").fetchone()[0]:
            conn.close()
            return {"recorded": 0, "note": "feature store not built"}

        credit_favor, _cfmeta = _latest_score(conn, "regime_model", "credit_favorability")
        cf = credit_favor if credit_favor is not None else 0.5

        # recent trades: closed in last 14d (realized) + all open (unrealized)
        trades = conn.execute(
            """SELECT t.position_id, t.ticker, t.structure_class, t.is_credit, t.status,
                      t.realized_pnl, t.win,
                      (SELECT unrealized_pnl FROM lifecycle_snapshots l WHERE l.position_id=t.position_id
                       ORDER BY snapshot_date DESC LIMIT 1) upnl
               FROM trade_features t WHERE t.status='open' OR t.win IS NOT NULL""").fetchall()

        recorded = 0
        m2_flagged_pnls: list[float] = []
        m2_other_pnls: list[float] = []
        for t in trades:
            pnl = t["realized_pnl"] if t["status"] == "closed" and t["realized_pnl"] is not None else (t["upnl"] or 0.0)
            is_credit = bool(t["is_credit"])
            # M2 advice mirrors the regime model's OWN bias thresholds (>=0.6 favor credit / avoid
            # debit, <=0.35 debit-ok, else neutral) — so the shadow advice never overstates M2.
            if cf >= 0.6 and not is_credit:
                m2_adv = "downweight_debit_rich_iv"
                m2_flagged_pnls.append(pnl)
            elif cf >= 0.6 and is_credit:
                m2_adv = "favor_credit"
                m2_other_pnls.append(pnl)
            else:
                m2_adv = "neutral"   # mid/low vol — M2 does not oppose debits here
                m2_other_pnls.append(pnl)
            _rows = [("regime_model", m2_adv, cf)]
            # M1 fill flag for the structure
            fscore, _ = _latest_score(conn, "fill_model", t["structure_class"] or "")
            if fscore is not None:
                _rows.append(("fill_model", "flag_low_fill" if fscore < 0.05 else "fillable", fscore))
            # M3 liquidity flag for the ticker
            lscore, _ = _latest_score(conn, "liquidity_model", t["ticker"] or "")
            if lscore is not None:
                _rows.append(("liquidity_model", "flag_illiquid" if lscore == 0.0 else "liquid", lscore))
            for model, adv, sc in _rows:
                conn.execute(
                    """INSERT OR REPLACE INTO shadow_model_decisions
                       (decision_date, position_id, ticker, structure_class, is_credit, model,
                        would_advise, model_score, trade_pnl, status)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (today, t["position_id"], t["ticker"], t["structure_class"], int(is_credit),
                     model, adv, sc, round(pnl, 2), t["status"]))
                recorded += 1
        conn.commit()

        # would-be impact verdict for M2 (the actionable one)
        n_flag, n_other = len(m2_flagged_pnls), len(m2_other_pnls)
        avg_flag = round(sum(m2_flagged_pnls) / n_flag, 2) if n_flag else None
        avg_other = round(sum(m2_other_pnls) / n_other, 2) if n_other else None
        if n_flag == 0:
            verdict = "m2_neutral_regime"   # current vol regime doesn't trigger M2's avoid-debit advice
        elif (n_flag + n_other) < _MIN_VALIDATE_N:
            verdict = "insufficient_n"
        elif avg_flag is not None and avg_other is not None:
            verdict = "supports_m2" if avg_flag < avg_other else "contradicts_m2"
        else:
            verdict = "insufficient_n"
        conn.close()
        return {"recorded": recorded, "m2_would_downweight": n_flag, "m2_other": n_other,
                "avg_pnl_downweighted": avg_flag, "avg_pnl_other": avg_other,
                "m2_verdict": verdict, "credit_favorability": cf}
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("run_shadow_advisor failed: %s", exc)
        return {"recorded": 0, "error": str(exc)}

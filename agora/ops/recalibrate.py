"""
agora/ops/recalibrate.py — advocate confidence recalibration + risk-coverage threshold (loop step 5).

The investigation found the advocate's confidence is poorly calibrated (Brier 0.269 ≈ coin-flip)
and the block rate (~71%) is set implicitly, not from a risk–coverage curve (external best
practice). This module, once enough attributed outcomes exist, (1) measures reliability on the
real (confidence → was_right) data, and (2) sweeps the block-confidence threshold to RECOMMEND the
operating point that meets a target BLOCK precision while maximizing coverage.

Design choices (deliberate, safe):
  • Gated on MIN_SAMPLE — below it the job reports "insufficient data" rather than fitting noise
    (recalibrating on ~30 samples would bake in error; the user and the literature both flag this).
  • RECOMMENDS + STORES + LOGS only. It does NOT auto-mutate the live block threshold — changing a
    fail-closed trading gate from a freshly-fit curve is a human decision. The recommendation lands
    in calibration_params for review; apply by setting the advocate's threshold knob.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

MIN_SAMPLE = 50          # below this, do not fit — report insufficient
TARGET_BLOCK_PRECISION = 0.65   # recommend the threshold meeting this BLOCK precision

_CREATE = """
CREATE TABLE IF NOT EXISTS calibration_params (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    agent                 TEXT NOT NULL,
    computed_at_utc       TEXT NOT NULL,
    sample_size           INTEGER,
    brier                 REAL,
    block_precision       REAL,
    recommended_threshold REAL,
    coverage_at_threshold REAL,
    note                  TEXT
);
"""


def recalibrate_advocate(db_path: str) -> dict[str, Any]:
    """Measure advocate calibration + recommend a block-confidence threshold from a risk–coverage
    sweep. Returns a dict; stores a calibration_params row and logs the recommendation."""
    try:
        conn = sqlite3.connect(db_path, timeout=10)
    except Exception as exc:
        return {"error": str(exc)}
    try:
        conn.executescript(_CREATE)
        rows = conn.execute(
            """SELECT verdict_confidence, advocate_was_right, verdict
               FROM advocate_journal
               WHERE trade_taken=1 AND advocate_was_right IS NOT NULL
                 AND verdict_confidence IS NOT NULL""",
        ).fetchall()
        n = len(rows)
        if n < MIN_SAMPLE:
            note = f"insufficient data (n={n}<{MIN_SAMPLE}) — not fitting; loop keeps accruing"
            logger.info("Recalibrate[advocate]: %s", note)
            _store(conn, n, None, None, None, None, note)
            return {"status": "insufficient", "n": n, "needed": MIN_SAMPLE}

        # Brier on confidence-as-P(correct): the verbalized confidence is the model's stated
        # probability that its own verdict is right.
        brier = round(sum((c / 100.0 - r) ** 2 for c, r, _ in rows) / n, 4)

        # Risk–coverage sweep over BLOCK rows only (the actionable gate). precision = P(was_right
        # | confidence ≥ t); coverage = fraction of blocks kept at ≥ t.
        blocks = [(c, r) for c, r, v in rows if v == "BLOCK"]
        rec_thr = cov = prec = None
        if blocks:
            nb = len(blocks)
            best = None
            for t in range(50, 100, 5):
                kept = [(c, r) for c, r in blocks if c >= t]
                if not kept:
                    continue
                p = sum(r for _, r in kept) / len(kept)
                coverage = len(kept) / nb
                if p >= TARGET_BLOCK_PRECISION and (best is None or coverage > best[2]):
                    best = (t, p, coverage)
            if best:
                rec_thr, prec, cov = float(best[0]), round(best[1], 3), round(best[2], 3)

        note = (f"fit on n={n}; Brier={brier}; "
                + (f"recommend BLOCK only when confidence≥{rec_thr:.0f} "
                   f"(precision={prec}, coverage={cov})" if rec_thr
                   else f"no threshold reaches {TARGET_BLOCK_PRECISION} precision yet"))
        logger.info("Recalibrate[advocate]: %s", note)
        _store(conn, n, brier, prec, rec_thr, cov, note)
        return {"status": "fit", "n": n, "brier": brier,
                "recommended_threshold": rec_thr, "block_precision": prec, "coverage": cov}
    finally:
        conn.close()


def _store(conn: sqlite3.Connection, n: int, brier, prec, thr, cov, note: str) -> None:
    conn.execute(
        """INSERT INTO calibration_params
           (agent, computed_at_utc, sample_size, brier, block_precision,
            recommended_threshold, coverage_at_threshold, note)
           VALUES ('advocate', ?, ?, ?, ?, ?, ?, ?)""",
        (datetime.now(tz=timezone.utc).isoformat(), n, brier, prec, thr, cov, note),
    )
    conn.commit()   # bare connection (no `with`) — must commit or the row is rolled back on close

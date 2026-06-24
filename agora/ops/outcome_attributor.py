"""
agora/ops/outcome_attributor.py — Closes the full multi-agent feedback loop.

After a position closes, attributes the outcome to every agent that touched
the decision chain (analyst, strategy selector, advocate, exit intelligence).
Writes Brier-score calibration metrics. Triggers lesson synthesis when
enough new samples accumulate.

Design rules:
  - Pure Python for core attribution. One LLM call only for lesson synthesis.
  - Safe to call repeatedly — only writes NULL columns; never overwrites.
  - Attributes ONLY from a genuine agent-driven close (_REAL_CLOSE: status='closed' with a
    close_source in lifecycle/thesis_exit/trailing_stop). `realized_pnl` is NOT NULL DEFAULT 0,
    so it can never gate a real fill — an open/reset/sync position reads pnl=0 and was previously
    mis-attributed as a loss. Each pass first self-heals via _purge_fabricated_attribution.
  - Runs every 6 hours via ScheduledAttributor.
  - Also callable on-demand: GET /agora/health/analyst (analyst only)
    and GET /agora/health/promotion-readiness (all agents).

Attribution per agent:
  analyst_journal:
    thesis_played_out         = 1 if realized_pnl > 0
    magnitude_realized_pct    = |pnl| / max_loss * 100
    confidence_was_calibrated = 1 if high-conf win or low-conf loss, else 0

  strategy_journal:
    structure_used     = 1 (always True when trade fills — selector endorsed it)
    realized_pnl       = from trade_records
    vs_rules_engine_pnl = NULL (counterfactual requires separate rules replay)

  advocate_journal:
    trade_taken        = 1 (always True when trade fills)
    realized_pnl       = from trade_records
    advocate_was_right = 1 if PASS+win or BLOCK-would-have-been-loss (BLOCK rows
                          cannot be attributed from fills; those remain NULL until
                          a shadow-mode BLOCK is later compared to actual outcome)

  exit_journal:
    action_taken       = 'close_confirmed' when position closes
    exit_alpha_pct     = NULL (counterfactual requires shadow-book replay — deferred)

  calibration_log:
    Written after each attribution pass. One row per agent per pass.
    brier_score = mean((predicted_win_rate - actual_win_rate)^2) per bucket.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

MATCH_WINDOW_HOURS = 24    # max hours before entry date to look for agent entry
PATROL_INTERVAL_SEC = 3600 * 6   # run every 6 hours
LESSON_TRIGGER_NEW_ATTRIBUTIONS = 10   # synthesize lessons after every N new attributions
LESSON_TIME_INTERVAL_SEC = 3600 * 24 * 7   # also synthesize lessons weekly regardless of closes
CALIBRATION_INTERVAL_SEC = 3600 * 24 * 7   # run ConvictionCalibrator weekly

# A position's outcome is trustworthy for learning ONLY if it closed via a real,
# agent-driven exit fill. `realized_pnl` is `NOT NULL DEFAULT 0`, so it can NEVER gate this —
# an open or reset position reads pnl=0 and was previously mis-attributed as a loss. The real
# gate is the close provenance. Excluded on purpose:
#   • status='open'                 — not closed at all
#   • status='reset'                — a reset, never a fill (pnl=0)
#   • close_source='tws_startup_sync' / orphan reconcile — broker-sync artifacts the agents
#     did not decide; their P&L must not feed agent learning.
_REAL_CLOSE_SOURCES = ("lifecycle", "thesis_exit", "trailing_stop", "stop_loss")
# A genuine agent-driven close that carries REAL fill-based P&L (post the close-booking fix): the
# profit engine (lifecycle), LLM (thesis_exit), trailing_stop, stop_loss, and CTO/CEO direct closes
# (close_source LIKE 'session:%'). EXCLUDES broker-sync artifacts (tws_startup_sync, reconcile_ghost)
# whose realized_pnl is not a real decision outcome.
_REAL_CLOSE = (
    "p.status='closed' AND p.close_date IS NOT NULL AND p.close_date<>'' "
    "AND (p.close_source IN ('lifecycle','thesis_exit','trailing_stop','stop_loss') "
    "     OR p.close_source LIKE 'session:%')"
)
# Sub-select of chain_ids whose position genuinely closed — used both to gate attribution
# writes and to detect (and purge) attribution written against anything else.
_REAL_CLOSED_CHAINS = (
    "SELECT dc.chain_id FROM decision_chains dc JOIN positions p "
    f"ON p.position_id = dc.position_id WHERE {_REAL_CLOSE}"
)

# Shadow mode promotion thresholds (must match spec §8)
_PROMOTION_THRESHOLDS = {
    "analyst":  {"metric": "direction_hit_rate", "min_rows": 40, "threshold": 0.55},
    "exit":     {"metric": "quality_accuracy",   "min_rows": 20, "threshold": 0.60},
    "advocate": {"metric": "precision",          "min_rows": 20, "threshold": 0.60},
    "strategy": {"metric": "win_rate",           "min_rows": 20, "threshold": 0.55},
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _table_exists(db_path: str, table: str) -> bool:
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
        return row is not None
    except Exception:
        return False


def _calibrated(confidence_pct: int | None, won: bool) -> int | None:
    if confidence_pct is None:
        return None
    if confidence_pct >= 60:
        return 1 if won else 0
    if confidence_pct < 50:
        return 1 if not won else 0
    return None   # 50-59% ambiguous band


def _brier_score(confidence_buckets: list[tuple[float, float]]) -> float | None:
    """
    confidence_buckets: list of (predicted_pct/100, actual_outcome_0_or_1)
    Returns mean Brier score (lower = better calibrated; 0 = perfect).
    """
    if not confidence_buckets:
        return None
    total = sum((pred - actual) ** 2 for pred, actual in confidence_buckets)
    return round(total / len(confidence_buckets), 4)


def _fetch_closed_trades(conn: sqlite3.Connection) -> list[tuple]:
    """Returns (trade_id, ticker, entry_date, realized_pnl, max_loss_dollars, max_gain_dollars).
    Gated on a genuine agent-driven close (INNER JOIN + _REAL_CLOSE) so exit attribution never
    keys off broker-sync artifacts or reset positions."""
    return conn.execute(
        f"""SELECT t.trade_id, t.ticker, t.entry_date, p.realized_pnl,
                  p.max_loss_dollars, p.max_gain_dollars
           FROM trade_records t
           JOIN positions p ON p.position_id = t.trade_id
           WHERE p.close_date IS NOT NULL
             AND p.realized_pnl IS NOT NULL
             AND {_REAL_CLOSE}
           ORDER BY t.entry_date""",
    ).fetchall()


def _ensure_exit_journal_quality_cols(conn: sqlite3.Connection) -> None:
    """Idempotent: add quality columns to exit_journal for existing databases."""
    existing = {r[1] for r in conn.execute("PRAGMA table_info(exit_journal)")}
    for col, defn in [
        ("pnl_pct_of_max", "REAL"),
        ("outcome_pnl",    "REAL"),
        ("action_quality", "TEXT"),
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE exit_journal ADD COLUMN {col} {defn}")


# ── Analyst attribution ───────────────────────────────────────────────────────

def _attribute_analyst(conn: sqlite3.Connection) -> int:
    """Re-keyed on the EXACT decision_id (was a fuzzy ticker+time match that mis-attributed when
    a ticker was reviewed repeatedly). Join analyst_journal.decision_id = decision_chains.chain_id,
    then chains.position_id -> positions.realized_pnl. positions is the source of truth (all closed
    rows carry realized_pnl); the chain's own realized_pnl backfill leaks, so we read positions."""
    rows = conn.execute(
        f"""SELECT a.journal_id, a.confidence_pct, p.realized_pnl, p.max_loss_dollars, a.ticker
           FROM analyst_journal a
           JOIN decision_chains dc ON dc.chain_id = a.decision_id
           JOIN positions       p  ON p.position_id = dc.position_id
           WHERE a.thesis_played_out IS NULL
             AND a.decision = 'thesis'
             AND a.decision_id IS NOT NULL AND a.decision_id <> ''
             AND {_REAL_CLOSE}""",
    ).fetchall()
    attributed = 0
    for journal_id, confidence_pct, realized_pnl, max_loss, ticker in rows:
        won = (realized_pnl or 0) > 0
        mag = (round(abs(realized_pnl) / max_loss * 100, 1)
               if (max_loss and max_loss > 0) else None)
        conn.execute(
            """UPDATE analyst_journal
               SET thesis_played_out = ?, magnitude_realized_pct = ?,
                   confidence_was_calibrated = ?
               WHERE journal_id = ?""",
            (1 if won else 0, mag, _calibrated(confidence_pct, won), journal_id),
        )
        attributed += 1
        logger.info("OutcomeAttributor[analyst] %s jid=%d played_out=%d mag=%s (decision_id)",
                    ticker, journal_id, 1 if won else 0, mag)
    return attributed


# ── Strategy selector attribution ─────────────────────────────────────────────

def _attribute_strategy(conn: sqlite3.Connection) -> int:
    """Mark structure_used=1 and realized_pnl, keyed on the exact decision_id (see _attribute_analyst)."""
    rows = conn.execute(
        f"""SELECT s.journal_id, p.realized_pnl, s.ticker
           FROM strategy_journal s
           JOIN decision_chains dc ON dc.chain_id = s.decision_id
           JOIN positions       p  ON p.position_id = dc.position_id
           WHERE s.structure_used IS NULL
             AND s.decision_id IS NOT NULL AND s.decision_id <> ''
             AND {_REAL_CLOSE}""",
    ).fetchall()
    attributed = 0
    for journal_id, realized_pnl, ticker in rows:
        conn.execute(
            """UPDATE strategy_journal
               SET structure_used = 1, realized_pnl = ?
               WHERE journal_id = ?""",
            (round(realized_pnl, 2), journal_id),
        )
        attributed += 1
        logger.info("OutcomeAttributor[strategy] %s jid=%d pnl=%.2f (decision_id)",
                    ticker, journal_id, realized_pnl)
    return attributed


# ── Advocate attribution ──────────────────────────────────────────────────────

def _attribute_advocate(conn: sqlite3.Connection) -> int:
    """
    Keyed on the EXACT decision_id (was a fuzzy ticker+time match). Join advocate_journal.decision_id
    = decision_chains.chain_id -> positions.realized_pnl. A PASS/CAUTION that filled is scored
    right-if-win; a BLOCK that nonetheless executed (shadow-mode history) is scored right-if-loss.

    NOTE: a LIVE BLOCK stops the trade -> no position -> never joins here, so live BLOCK precision
    stays unmeasurable from fills alone. That is what the shadow book (loop-rebuild step 2) is for.
    """
    rows = conn.execute(
        f"""SELECT a.journal_id, a.verdict, p.realized_pnl, a.ticker
           FROM advocate_journal a
           JOIN decision_chains dc ON dc.chain_id = a.decision_id
           JOIN positions       p  ON p.position_id = dc.position_id
           WHERE a.trade_taken IS NULL
             AND a.decision_id IS NOT NULL AND a.decision_id <> ''
             AND a.verdict IN ('PASS', 'CAUTION', 'BLOCK')
             AND {_REAL_CLOSE}""",
    ).fetchall()
    attributed = 0
    for journal_id, verdict, realized_pnl, ticker in rows:
        won = (realized_pnl or 0) > 0
        was_right = (1 if not won else 0) if verdict == "BLOCK" else (1 if won else 0)
        conn.execute(
            """UPDATE advocate_journal
               SET trade_taken = 1, realized_pnl = ?, advocate_was_right = ?
               WHERE journal_id = ?""",
            (round(realized_pnl, 2), was_right, journal_id),
        )
        attributed += 1
        logger.info("OutcomeAttributor[advocate] %s jid=%d verdict=%s right=%d (decision_id)",
                    ticker, journal_id, verdict, was_right)

    # L3: score LIVE BLOCKs from the shadow book — a blocked trade never fills, so its precision
    # is only knowable from the counterfactual. advocate_was_right=1 iff the shadow trade LOST.
    try:
        shadow = conn.execute(
            """SELECT a.journal_id, sb.hypothetical_win, sb.hypothetical_pnl, a.ticker
               FROM advocate_journal a
               JOIN shadow_book sb ON sb.decision_id = a.decision_id AND sb.decision_id <> ''
               WHERE a.trade_taken IS NULL AND a.verdict='BLOCK'
                 AND a.decision_id IS NOT NULL AND a.decision_id <> ''
                 AND sb.evaluated=1 AND sb.hypothetical_win IS NOT NULL""",
        ).fetchall()
        for journal_id, hyp_win, hyp_pnl, ticker in shadow:
            was_right = 0 if hyp_win else 1   # right to BLOCK iff the shadow trade would have lost
            conn.execute(
                """UPDATE advocate_journal
                   SET trade_taken=1, realized_pnl=?, advocate_was_right=?
                   WHERE journal_id=?""",
                (round(hyp_pnl or 0, 2), was_right, journal_id),
            )
            attributed += 1
            logger.info("OutcomeAttributor[advocate-shadow] %s jid=%d BLOCK right=%d (counterfactual)",
                        ticker, journal_id, was_right)
    except Exception as exc:
        logger.debug("advocate shadow attribution: %s", exc)
    return attributed


# ── Long-options vetter attribution ───────────────────────────────────────────

def _attribute_vetter(conn: sqlite3.Connection) -> int:
    """Revive the vetter's dead learning loop. Long options bypass decision_chains (no exact key),
    so match long_vetter_log → long-option positions on ticker + strike (near-unique) + entry near
    the vet timestamp, and backfill outcome (win/loss) + realized_pnl. SKIP verdicts never fill,
    so they stay unattributed (like live BLOCKs)."""
    import json
    try:
        pos = []
        for ticker, legs_json, entry_date, pnl in conn.execute(
            f"""SELECT ticker, legs_json, entry_date, realized_pnl FROM positions p
               WHERE strategy IN ('long_call','long_put') AND {_REAL_CLOSE}"""):
            try:
                strike = float(json.loads(legs_json)[0]["strike"])
            except Exception:
                continue
            pos.append((ticker, strike, str(entry_date or "")[:10], pnl))

        attributed = 0
        for vid, vticker, vstrike, vts in conn.execute(
            "SELECT id, ticker, strike, ts_utc FROM long_vetter_log WHERE outcome IS NULL OR outcome=''"):
            match_pnl = None
            for (ticker, strike, entry_date, pnl) in pos:
                if ticker != vticker or vstrike is None or abs(strike - float(vstrike)) > 0.5:
                    continue
                # entry within ±3 days of the vet
                try:
                    if abs((datetime.fromisoformat(entry_date) - datetime.fromisoformat(vts[:10])).days) > 3:
                        continue
                except Exception:
                    pass
                match_pnl = pnl
                break
            if match_pnl is None:
                continue
            conn.execute(
                "UPDATE long_vetter_log SET outcome=?, realized_pnl=? WHERE id=?",
                ("win" if match_pnl > 0 else "loss", round(match_pnl, 2), vid),
            )
            attributed += 1
        if attributed:
            logger.info("OutcomeAttributor[vetter] backfilled %d long-options outcomes", attributed)
        return attributed
    except Exception as exc:
        logger.debug("vetter attribution: %s", exc)
        return 0


# ── Exit agent attribution ────────────────────────────────────────────────────

def _attribute_exit(conn: sqlite3.Connection, trades: list[tuple]) -> int:
    """
    For each closed position, write action_quality and outcome_pnl to every
    unattributed exit_journal row.

    Quality logic:
      CLOSE_NOW + final_pnl < pnl_at_rec*max_gain → EARLY_EXIT_CORRECT (called the turn)
      CLOSE_NOW + final_pnl >= pnl_at_rec*max_gain → EARLY_EXIT_WRONG (left money on table)
      HOLD       + final_pnl > 0                  → HOLD_CORRECT
      HOLD       + final_pnl <= 0                 → HOLD_WRONG
    """
    attributed = 0
    for trade_id, _ticker, _entry_date_str, realized_pnl, _max_loss, max_gain in trades:
        rows = conn.execute(
            """SELECT journal_id, recommendation, pnl_pct_of_max
               FROM exit_journal
               WHERE position_id = ? AND action_taken IS NULL""",
            (trade_id,),
        ).fetchall()
        for jid, rec, pnl_at_rec_pct in rows:
            if rec == "CLOSE_NOW":
                pnl_at_rec_dollars = (pnl_at_rec_pct or 0.0) * (max_gain or 0.0)
                if (realized_pnl or 0) < pnl_at_rec_dollars:
                    quality = "EARLY_EXIT_CORRECT"
                else:
                    quality = "EARLY_EXIT_WRONG"
            elif rec == "HOLD":
                quality = "HOLD_CORRECT" if (realized_pnl or 0) > 0 else "HOLD_WRONG"
            else:
                # TIGHTEN_STOP / TAKE_PARTIAL / ROLL — treat as HOLD for calibration
                quality = "HOLD_CORRECT" if (realized_pnl or 0) > 0 else "HOLD_WRONG"

            conn.execute(
                """UPDATE exit_journal
                   SET action_taken = 'close_confirmed',
                       outcome_pnl  = ?,
                       action_quality = ?
                   WHERE journal_id = ?""",
                (round(realized_pnl, 2) if realized_pnl is not None else None,
                 quality, jid),
            )
            attributed += 1

    if attributed:
        logger.info("OutcomeAttributor[exit] %d rows attributed with action_quality", attributed)
    return attributed


# ── Calibration log ───────────────────────────────────────────────────────────

def _write_calibration(conn: sqlite3.Connection, agent_name: str,
                        sample_window_days: int = 90) -> None:
    """
    Compute Brier score for one agent and append a calibration_log row.
    Only runs if there are ≥5 attributed samples since last calibration write.
    """
    cutoff = (datetime.now(UTC) - timedelta(days=sample_window_days)).isoformat()

    if agent_name == "analyst":
        rows = conn.execute(
            """SELECT confidence_pct, thesis_played_out FROM analyst_journal
               WHERE thesis_played_out IS NOT NULL AND decided_at_utc >= ?""",
            (cutoff,)
        ).fetchall()
        buckets = [(r[0] / 100.0, float(r[1])) for r in rows if r[0] is not None]
    elif agent_name == "advocate":
        rows = conn.execute(
            """SELECT verdict_confidence, advocate_was_right FROM advocate_journal
               WHERE advocate_was_right IS NOT NULL AND decided_at_utc >= ?""",
            (cutoff,)
        ).fetchall()
        buckets = [(r[0] / 100.0, float(r[1])) for r in rows if r[0] is not None]
    elif agent_name == "strategy":
        rows = conn.execute(
            """SELECT realized_pnl FROM strategy_journal
               WHERE structure_used = 1 AND realized_pnl IS NOT NULL AND decided_at_utc >= ?""",
            (cutoff,)
        ).fetchall()
        if not rows:
            return
        wins = sum(1 for r in rows if r[0] > 0)
        actual_win_rate = round(wins / len(rows), 4)
        conn.execute(
            """INSERT INTO calibration_log (agent_name, measured_at_utc, sample_window_days,
               sample_size, actual_win_rate, predicted_win_rate, calibration_gap)
               VALUES (?,?,?,?,?,NULL,NULL)""",
            (agent_name, datetime.now(UTC).isoformat(), sample_window_days, len(rows),
             actual_win_rate),
        )
        return
    elif agent_name == "exit":
        rows = conn.execute(
            """SELECT confidence_pct, action_quality FROM exit_journal
               WHERE action_quality IS NOT NULL AND decided_at_utc >= ?""",
            (cutoff,)
        ).fetchall()
        buckets = [
            (r[0] / 100.0,
             1.0 if r[1] in ("EARLY_EXIT_CORRECT", "HOLD_CORRECT") else 0.0)
            for r in rows if r[0] is not None
        ]
    else:
        return

    if len(buckets) < 5:
        return

    brier = _brier_score(buckets)
    wins = sum(1 for _, actual in buckets if actual == 1.0)
    avg_pred = round(sum(p for p, _ in buckets) / len(buckets), 4)
    actual_wr = round(wins / len(buckets), 4)
    gap = round(avg_pred - actual_wr, 4)
    drift_alert = 1 if abs(gap) > 0.15 else 0

    conn.execute(
        """INSERT INTO calibration_log (agent_name, measured_at_utc, sample_window_days,
           sample_size, brier_score, predicted_win_rate, actual_win_rate,
           calibration_gap, drift_alert)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (agent_name, datetime.now(UTC).isoformat(), sample_window_days, len(buckets),
         brier, avg_pred, actual_wr, gap, drift_alert),
    )
    if drift_alert:
        logger.warning("Calibration drift alert [%s]: predicted=%.2f actual=%.2f gap=%.2f",
                       agent_name, avg_pred, actual_wr, gap)


# ── Self-healing purge of fabricated attribution ──────────────────────────────

def _purge_fabricated_attribution(conn: sqlite3.Connection) -> dict[str, int]:
    """Idempotent self-heal. NULL any agent attribution that was written against something that
    is NOT a genuine agent-driven close. Such rows entered the journals two ways:
      1. an earlier fuzzy ticker+time matcher that paired journal rows to the wrong (or no) trade;
      2. the `realized_pnl IS NOT NULL` no-op gate, which let open/reset/sync positions through.
    Once clean this updates 0 rows each pass (the corrected attributors only write real closes), so
    it doubles as a continuous regression guard. Advocate BLOCK rows scored from the shadow book
    (a designed counterfactual, no real position) are explicitly spared."""
    purged: dict[str, int] = {}

    purged["analyst"] = conn.execute(
        f"""UPDATE analyst_journal
            SET thesis_played_out=NULL, magnitude_realized_pct=NULL, confidence_was_calibrated=NULL
            WHERE thesis_played_out IS NOT NULL
              AND decision_id NOT IN ({_REAL_CLOSED_CHAINS})""",
    ).rowcount

    purged["strategy"] = conn.execute(
        f"""UPDATE strategy_journal
            SET structure_used=NULL, realized_pnl=NULL
            WHERE structure_used IS NOT NULL
              AND decision_id NOT IN ({_REAL_CLOSED_CHAINS})""",
    ).rowcount

    # Advocate: spare rows legitimately scored from the shadow book (BLOCK counterfactuals never
    # have a real position by construction).
    purged["advocate"] = conn.execute(
        f"""UPDATE advocate_journal
            SET trade_taken=NULL, realized_pnl=NULL, advocate_was_right=NULL
            WHERE trade_taken IS NOT NULL
              AND decision_id NOT IN ({_REAL_CLOSED_CHAINS})
              AND decision_id NOT IN (
                  SELECT decision_id FROM shadow_book
                  WHERE evaluated=1 AND hypothetical_win IS NOT NULL)""",
    ).rowcount if _table_exists_conn(conn, "shadow_book") else conn.execute(
        f"""UPDATE advocate_journal
            SET trade_taken=NULL, realized_pnl=NULL, advocate_was_right=NULL
            WHERE trade_taken IS NOT NULL
              AND decision_id NOT IN ({_REAL_CLOSED_CHAINS})""",
    ).rowcount

    total = sum(purged.values())
    if total:
        logger.warning("OutcomeAttributor PURGED fabricated attribution: %s", purged)
    return purged


def _table_exists_conn(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


# ── Core attribution entry point ──────────────────────────────────────────────

def attribute_closed_trades(db_path: str) -> dict:
    """
    Full attribution pass: analyst + strategy + advocate + exit + calibration_log.
    Returns a summary dict with per-agent attributed counts.
    Safe to call repeatedly; only fills NULL columns.
    """
    if not _table_exists(db_path, "analyst_journal"):
        logger.debug("OutcomeAttributor: analyst_journal not yet created — skipping")
        return {"attributed": 0, "skipped_no_match": 0, "already_done": 0}

    total: dict[str, int] = {
        "analyst": 0, "strategy": 0, "advocate": 0, "vetter": 0, "exit": 0
    }

    # L3: evaluate any blocked-trade counterfactuals whose horizon has passed (own connection,
    # committed) BEFORE the main attribution opens its transaction, so newly-resolved BLOCKs are
    # scored this same cycle without two writers contending on the DB.
    try:
        from agora.ops.shadow_book import evaluate_due
        evaluate_due(db_path)
    except Exception as exc:
        logger.debug("shadow_book.evaluate_due: %s", exc)

    # S0.2: score post-close counterfactuals whose horizon has passed (did our exit cut a winner?).
    try:
        from agora.ops.post_close_watch import evaluate_due as _pcw_eval
        _pcw_eval(db_path)
    except Exception as exc:
        logger.debug("post_close_watch.evaluate_due: %s", exc)

    try:
        with sqlite3.connect(db_path) as conn:
            if _table_exists(db_path, "exit_journal"):
                _ensure_exit_journal_quality_cols(conn)

            # Self-heal FIRST: strip any attribution that does not trace to a genuine close, so a
            # contaminated row from a prior pass (or an earlier code version) cannot survive and
            # poison calibration/recalibration/value-monitor downstream this same cycle.
            _purge_fabricated_attribution(conn)

            # Analyst / strategy / advocate are attributed by EXACT decision_id join (no fuzzy
            # ticker+time match, no dependence on the leaky chain.realized_pnl) — they self-query.
            # All three JOIN decision_chains, so a DB without that table (a brand-new DB before the
            # first chain is logged) would otherwise raise and abort the ENTIRE pass — including the
            # downstream calibration / value-monitor / perf-snapshot. Gate on its existence so the
            # pass degrades gracefully. (In production position_manager creates it, so this is a
            # no-op there; it only matters for fresh/partial DBs.)
            _has_chains = _table_exists(db_path, "decision_chains")
            if _has_chains:
                total["analyst"] = _attribute_analyst(conn)
                if _table_exists(db_path, "strategy_journal"):
                    total["strategy"] = _attribute_strategy(conn)
                if _table_exists(db_path, "advocate_journal"):
                    total["advocate"] = _attribute_advocate(conn)
            if _table_exists(db_path, "long_vetter_log"):
                total["vetter"] = _attribute_vetter(conn)
            # Exit attribution still keys off closed-trade fills.
            trades = _fetch_closed_trades(conn)
            if trades and _table_exists(db_path, "exit_journal"):
                total["exit"] = _attribute_exit(conn, trades)

            # Calibration runs regardless of new trades — uses all attributed rows in journal
            if _table_exists(db_path, "calibration_log"):
                for agent in ("analyst", "strategy", "advocate", "exit"):
                    try:
                        _write_calibration(conn, agent)
                    except Exception as exc:
                        logger.debug("calibration write [%s]: %s", agent, exc)

    except Exception as exc:
        logger.error("OutcomeAttributor failed: %s", exc)
        return {"error": str(exc)}

    # Per-agent value-vs-cost reevaluation (the "every penny" loop) — logs a scorecard and
    # flags retire candidates each cycle, using the freshly-attributed outcomes above.
    try:
        from agora.ops.agent_value_monitor import log_agent_value
        log_agent_value(db_path)
    except Exception as exc:
        logger.debug("agent_value_monitor: %s", exc)

    # Advocate recalibration + risk-coverage threshold recommendation (gated on sample size;
    # recommends only, never auto-mutates the live gate).
    try:
        from agora.ops.recalibrate import recalibrate_advocate
        recalibrate_advocate(db_path)
    except Exception as exc:
        logger.debug("recalibrate: %s", exc)

    # Performance snapshot — persist today's precise edge metrics (expectancy/win-rate/profit-factor)
    # + the book↔DB reconciliation as first-class learning data, so the loop can act on the trend.
    try:
        from agora.ops.performance_metrics import snapshot_daily
        snapshot_daily(db_path)
    except Exception as exc:
        logger.debug("perf snapshot: %s", exc)

    grand_total = sum(total.values())
    if grand_total > 0:
        logger.info("OutcomeAttributor pass: %s", total)
    return {
        "attributed": total["analyst"],   # backward-compat key used by dashboard
        "attributed_by_agent": total,
        "skipped_no_match": 0,
        "already_done": 0,
    }


# ── Analyst performance stats (for dashboard) ─────────────────────────────────

def get_analyst_stats(db_path: str) -> dict[str, Any]:
    """
    Compute summary stats for the analyst dashboard endpoint.
    Returns: total_theses, direction_hit_rate, calibration_rate, recent rows.
    """
    if not _table_exists(db_path, "analyst_journal"):
        return {"status": "analyst_journal table not yet created"}

    try:
        with sqlite3.connect(db_path) as conn:
            total_row = conn.execute(
                "SELECT COUNT(*), SUM(CASE WHEN decision='thesis' THEN 1 ELSE 0 END) FROM analyst_journal"
            ).fetchone()
            total_calls  = total_row[0] or 0
            total_thesis = total_row[1] or 0

            attributed = conn.execute(
                """SELECT COUNT(*), AVG(confidence_pct), AVG(magnitude_realized_pct),
                          SUM(thesis_played_out),
                          SUM(CASE WHEN confidence_was_calibrated=1 THEN 1 ELSE 0 END),
                          SUM(CASE WHEN confidence_was_calibrated IS NOT NULL THEN 1 ELSE 0 END)
                   FROM analyst_journal WHERE thesis_played_out IS NOT NULL"""
            ).fetchone()
            attr_count = attributed[0] or 0
            avg_conf   = round(attributed[1], 1) if attributed[1] else None
            avg_mag    = round(attributed[2], 1) if attributed[2] else None
            wins       = attributed[3] or 0
            calib_yes  = attributed[4] or 0
            calib_n    = attributed[5] or 0

            shadow_ct = conn.execute(
                "SELECT COUNT(*) FROM analyst_journal WHERE shadow_mode=1 AND decision='thesis'"
            ).fetchone()[0]

            cost_row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd),0), COALESCE(SUM(input_tokens),0), "
                "COALESCE(SUM(output_tokens),0) FROM analyst_journal"
            ).fetchone()

            recent = conn.execute(
                """SELECT ticker, decided_at_utc, decision, direction, magnitude_pct,
                          confidence_pct, strategy_family, thesis_played_out,
                          magnitude_realized_pct, shadow_mode
                   FROM analyst_journal ORDER BY decided_at_utc DESC LIMIT 10"""
            ).fetchall()

        return {
            "total_calls":       total_calls,
            "total_theses":      total_thesis,
            "no_thesis_pct":     round((total_calls - total_thesis) / total_calls * 100, 1) if total_calls else None,
            "shadow_theses":     shadow_ct,
            "live_theses":       total_thesis - shadow_ct,
            "attributed_count":  attr_count,
            "direction_hit_rate": round(wins / attr_count, 3) if attr_count else None,
            "avg_confidence_pct": avg_conf,
            "avg_magnitude_pct":  avg_mag,
            "calibration_rate":   round(calib_yes / calib_n, 3) if calib_n else None,
            "total_cost_usd":     round(cost_row[0], 4),
            "total_input_tokens":  cost_row[1],
            "total_output_tokens": cost_row[2],
            "recent": [
                {
                    "ticker": r[0], "decided_at": r[1], "decision": r[2],
                    "direction": r[3], "magnitude_pct": r[4], "confidence_pct": r[5],
                    "strategy_family": r[6], "played_out": r[7],
                    "magnitude_realized_pct": r[8], "shadow": bool(r[9]),
                }
                for r in recent
            ],
        }
    except Exception as exc:
        logger.error("get_analyst_stats failed: %s", exc)
        return {"error": str(exc)}


# ── Promotion readiness stats (for /health/promotion-readiness) ───────────────

def get_promotion_readiness(db_path: str) -> dict[str, Any]:
    """
    Per-agent shadow→live promotion readiness evaluation.
    Each agent has explicit thresholds from the spec; this returns
    current metrics + a READY/NOT_READY/INSUFFICIENT_DATA verdict.
    """
    result: dict[str, Any] = {}

    try:
        with sqlite3.connect(db_path) as conn:
            # ── Analyst: ≥40 attributed, direction_hit_rate ≥55% ─────────────
            if _table_exists(db_path, "analyst_journal"):
                a = conn.execute(
                    """SELECT COUNT(*), SUM(thesis_played_out),
                              AVG(confidence_pct), AVG(confidence_was_calibrated)
                       FROM analyst_journal WHERE thesis_played_out IS NOT NULL"""
                ).fetchone()
                n, wins, avg_conf, avg_cal = a[0] or 0, a[1] or 0, a[2], a[3]
                hit_rate = round(wins / n, 3) if n else None
                result["analyst"] = {
                    "attributed_count": n,
                    "direction_hit_rate": hit_rate,
                    "avg_confidence_pct": round(avg_conf, 1) if avg_conf else None,
                    "calibration_rate": round(avg_cal, 3) if avg_cal is not None else None,
                    "threshold_attributed": 40,
                    "threshold_hit_rate": 0.55,
                    "ready": (
                        n >= 40 and hit_rate is not None and hit_rate >= 0.55
                    ),
                    "status": (
                        "READY" if (n >= 40 and hit_rate and hit_rate >= 0.55)
                        else "INSUFFICIENT_DATA" if n < 40
                        else "NOT_READY"
                    ),
                    "missing": (
                        f"Need {max(0, 40 - n)} more attributions; "
                        + (f"hit_rate {hit_rate:.1%} < 55%" if hit_rate and hit_rate < 0.55 else "hit_rate threshold met")
                    ),
                }

            # ── Advocate: precision ≥60%, recall ≥50% over ≥30 trades ────────
            if _table_exists(db_path, "advocate_journal"):
                av = conn.execute(
                    """SELECT COUNT(*), verdict, advocate_was_right
                       FROM advocate_journal
                       WHERE trade_taken IS NOT NULL
                       GROUP BY verdict, advocate_was_right"""
                ).fetchall()
                # Precision = BLOCK was_right / total BLOCK attributed
                # Recall    = BLOCK was_right / total actual losers
                block_right = sum(r[0] for r in av if r[1] == "BLOCK" and r[2] == 1)
                block_wrong = sum(r[0] for r in av if r[1] == "BLOCK" and r[2] == 0)
                sum(r[0] for r in av if r[1] == "PASS"  and r[2] == 1)
                pass_wrong  = sum(r[0] for r in av if r[1] == "PASS"  and r[2] == 0)
                total_attr  = sum(r[0] for r in av)
                total_block = block_right + block_wrong
                total_losers = block_right + pass_wrong
                precision = round(block_right / total_block, 3) if total_block else None
                recall    = round(block_right / total_losers, 3) if total_losers else None
                result["advocate"] = {
                    "attributed_count": total_attr,
                    "block_right": block_right,
                    "block_wrong": block_wrong,
                    "precision": precision,
                    "recall": recall,
                    "threshold_precision": 0.60,
                    "threshold_recall": 0.50,
                    "ready": (
                        total_attr >= 30
                        and precision is not None and precision >= 0.60
                        and recall    is not None and recall    >= 0.50
                    ),
                    "status": (
                        "READY" if (total_attr >= 30 and precision and precision >= 0.60
                                    and recall and recall >= 0.50)
                        else "INSUFFICIENT_DATA" if total_attr < 30
                        else "NOT_READY"
                    ),
                }

            # ── Strategy selector: ≥40 attributed, no vs_rules_engine_pnl yet ─
            if _table_exists(db_path, "strategy_journal"):
                sv = conn.execute(
                    """SELECT COUNT(*), SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END),
                              AVG(realized_pnl)
                       FROM strategy_journal WHERE structure_used = 1"""
                ).fetchone()
                sn = sv[0] or 0
                s_wins = sv[1] or 0
                s_avg_pnl = round(sv[2], 2) if sv[2] else None
                s_hit = round(s_wins / sn, 3) if sn else None
                result["strategy_selector"] = {
                    "attributed_count": sn,
                    "win_rate": s_hit,
                    "avg_pnl": s_avg_pnl,
                    "threshold_attributed": 40,
                    "threshold_win_rate": 0.55,
                    "note": "rules engine counterfactual computed after first 10 closed trades" if sn < 10 else None,
                    "ready": sn >= 40 and s_hit is not None and s_hit >= 0.55,
                    "status": (
                        "READY" if (sn >= 40 and s_hit and s_hit >= 0.55)
                        else "INSUFFICIENT_DATA" if sn < 40
                        else "NOT_READY"
                    ),
                }

            # ── Exit agent: ≥20 attributed, decision_accuracy ≥60% ───────────
            if _table_exists(db_path, "exit_journal"):
                # Self-heal: a pre-quality-columns exit_journal (older or freshly-created DB that
                # has never run an attribution pass) lacks action_quality — reading it would raise
                # and fail the whole readiness report. Idempotent ALTER, same as attribute pass.
                _ensure_exit_journal_quality_cols(conn)
                ev = conn.execute(
                    """SELECT COUNT(*),
                              SUM(CASE WHEN action_quality IN
                                  ('EARLY_EXIT_CORRECT','HOLD_CORRECT') THEN 1 ELSE 0 END),
                              AVG(confidence_pct)
                       FROM exit_journal WHERE action_quality IS NOT NULL"""
                ).fetchone()
                en = ev[0] or 0
                correct = ev[1] or 0
                avg_conf = round(ev[2], 1) if ev[2] else None
                decision_accuracy = round(correct / en, 3) if en else None
                breakdown = conn.execute(
                    """SELECT action_quality, COUNT(*) FROM exit_journal
                       WHERE action_quality IS NOT NULL GROUP BY action_quality"""
                ).fetchall()
                result["exit_intelligence"] = {
                    "attributed_count": en,
                    "decision_accuracy": decision_accuracy,
                    "avg_confidence_pct": avg_conf,
                    "quality_breakdown": {r[0]: r[1] for r in breakdown},
                    "threshold_attributed": 20,
                    "threshold_accuracy": 0.60,
                    "ready": en >= 20 and decision_accuracy is not None and decision_accuracy >= 0.60,
                    "status": (
                        "READY" if (en >= 20 and decision_accuracy and decision_accuracy >= 0.60)
                        else "INSUFFICIENT_DATA" if en < 20
                        else "NOT_READY"
                    ),
                }

            # ── Latest calibration scores ─────────────────────────────────────
            if _table_exists(db_path, "calibration_log"):
                cal_rows = conn.execute(
                    """SELECT agent_name, measured_at_utc, sample_size,
                              brier_score, actual_win_rate, calibration_gap, drift_alert
                       FROM calibration_log
                       WHERE (agent_name, measured_at_utc) IN (
                           SELECT agent_name, MAX(measured_at_utc)
                           FROM calibration_log GROUP BY agent_name
                       )"""
                ).fetchall()
                result["calibration"] = [
                    {
                        "agent": r[0], "measured_at": r[1], "sample_size": r[2],
                        "brier_score": r[3], "actual_win_rate": r[4],
                        "calibration_gap": r[5], "drift_alert": bool(r[6]),
                    }
                    for r in cal_rows
                ]

    except Exception as exc:
        logger.error("get_promotion_readiness failed: %s", exc)
        return {"error": str(exc)}

    return result


# ── Scheduled background runner ───────────────────────────────────────────────

class ScheduledAttributor:
    """
    Background task that owns four weekly/6-hourly jobs:

    1. Attribution patrol (every 6h) — attributes closed trades to agent journals,
       computes Brier score calibration.
    2. Lesson synthesis — triggered by N new attributions OR weekly timer.
    3. ConvictionCalibrator — runs weekly, writes calibration_report.json,
       alerts via Discord webhook if configured.
    4. PerformanceAnalystAgent — runs weekly, cross-agent meta-analysis,
       proposes lessons invisible to per-agent synthesis, sends Discord DM digest.

    Also checks shadow-mode promotion thresholds after each attribution pass
    and sends Discord alerts when an agent is ready to promote.
    """

    def __init__(self, db_path: str,
                 calibration_output: str = ".agora/calibration_report.json",
                 alert_webhook_url: str | None = None) -> None:
        self._db_path    = db_path
        self._cal_output = calibration_output
        self._webhook    = alert_webhook_url
        self._running    = False
        self._total_attributed_since_last_lesson = 0
        self._last_lesson_time        = 0.0   # epoch seconds
        self._last_calibration_time   = 0.0
        self._last_perf_analysis_time = 0.0

    def set_webhook(self, url: str | None) -> None:
        self._webhook = url

    def _weekly_marker_path(self) -> str:
        import os
        return os.path.join(os.path.dirname(self._db_path) or ".", "weekly_markers.json")

    def _weekly_due(self, key: str, interval_days: int = 7) -> bool:
        """True if `key` last ran >= interval_days ago, via a PERSISTED wall-clock marker that
        survives restarts. Replaces the prior monotonic-clock check (asyncio loop time ≈
        time.monotonic() — seconds since boot — compared against an init of 0.0), which was
        true on the FIRST loop iteration after EVERY startup and so re-fired the 'weekly' report
        once per restart (the Discord spam). 2026-06-22 fix."""
        import json
        try:
            with open(self._weekly_marker_path()) as f:
                last = json.load(f).get(key)
            if not last:
                return True
            return (datetime.now(UTC) - datetime.fromisoformat(last)).days >= interval_days
        except Exception:
            return True

    def _weekly_mark(self, key: str) -> None:
        import json
        try:
            p = self._weekly_marker_path()
            try:
                with open(p) as f:
                    data = json.load(f)
            except Exception:
                data = {}
            data[key] = datetime.now(UTC).isoformat()
            with open(p, "w") as f:
                json.dump(data, f)
        except Exception:
            pass

    async def start(self) -> None:
        self._running = True
        logger.info("ScheduledAttributor started (every %dh)", PATROL_INTERVAL_SEC // 3600)
        await asyncio.sleep(30)
        # Anchor the lesson timer to NOW so it doesn't fire on startup (loop time ≈ seconds-since-boot,
        # which is >> any interval when compared against the 0.0 init — same monotonic bug the weekly
        # markers fix). The weekly calibrator/perf-analysis use persisted wall-clock markers instead.
        self._last_lesson_time = asyncio.get_event_loop().time()
        while self._running:
            try:
                # Daily lifecycle "film" — snapshot every open (+ just-closed) position once/day.
                # Idempotent (UNIQUE position_id+date), read-only on positions, additive-only → cannot
                # affect execution. Foundation for the lifecycle-attribution / win-prob / mgmt models.
                try:
                    from agora.ops.lifecycle_capture import capture_lifecycle_snapshots
                    _lc = capture_lifecycle_snapshots(self._db_path)
                    logger.debug("lifecycle capture: %s", _lc)
                except Exception as _lcexc:
                    logger.debug("lifecycle capture skipped: %s", _lcexc)

                result = attribute_closed_trades(self._db_path)
                new_analyst = result.get("attributed_by_agent", {}).get("analyst", 0)

                # Phase 0b feature store — rebuild the unified ML table AFTER capture+attribution so
                # path features + labels are fresh. Read-only on sources, writes only trade_features.
                try:
                    from agora.core.config import get_settings
                    from agora.ops.feature_store import build_feature_store
                    _cut = getattr(get_settings(), "expectancy_legacy_cutoff_date", "2026-06-12")
                    _fs = build_feature_store(self._db_path, legacy_cutoff=_cut)
                    logger.debug("feature store: %s", _fs)
                except Exception as _fsexc:
                    logger.debug("feature store skipped: %s", _fsexc)

                # Phase 0d market capture — daily VIX/term-structure snapshot for the regime model.
                # Network-fetch (yfinance), idempotent/day, writes only market_snapshots, never raises.
                try:
                    from agora.ops.market_capture import capture_market_snapshot
                    capture_market_snapshot(self._db_path)
                except Exception as _mcexc:
                    logger.debug("market capture skipped: %s", _mcexc)

                # Phase 0c model-runner — run any model whose cadence is due (reads the fresh feature
                # store). Read-only on trading data, writes only model_runs/model_scores, never raises.
                try:
                    from agora.ops.model_runner import run_due_models
                    _mr = run_due_models(self._db_path)
                    if _mr.get("ran"):
                        logger.debug("model-runner: %s", _mr)
                        # Model Analyst — turn the fresh scores into improvement recommendations.
                        try:
                            from agora.ops.model_analyst import analyze_models
                            _ma = analyze_models(self._db_path)
                            logger.debug("model-analyst: %s", _ma)
                        except Exception as _maexc:
                            logger.debug("model-analyst skipped: %s", _maexc)
                        # Rung 2 — shadow advisor: record what each model WOULD advise on real trades
                        # (offline, read-only, no hot-path) to validate before any live wiring.
                        try:
                            from agora.ops.shadow_advisor import run_shadow_advisor
                            _sa = run_shadow_advisor(self._db_path)
                            logger.debug("shadow-advisor: %s", _sa)
                        except Exception as _saexc:
                            logger.debug("shadow-advisor skipped: %s", _saexc)
                        # Phase A — per-ticker characterization: profile each traded ticker from ~3y
                        # price history (yfinance). DAILY-gated (network-heavy); the adapter below
                        # reads the stored vol signature each cycle.
                        if self._weekly_due("ticker_profiles", interval_days=1):
                            try:
                                import sqlite3 as _sq
                                _c = _sq.connect(self._db_path, timeout=8)
                                _tk = [r[0] for r in _c.execute(
                                    "SELECT DISTINCT ticker FROM trade_features WHERE ticker IS NOT NULL")]
                                _c.close()
                                from agora.ops.ticker_profile import build_profiles
                                _bp = build_profiles(self._db_path, _tk)
                                logger.info("ticker-profiles: %s", _bp.get("summary", _bp))
                                self._weekly_mark("ticker_profiles")
                            except Exception as _bpexc:
                                logger.debug("ticker-profiles skipped: %s", _bpexc)
                        # Per-ticker adaptive job (Phase 2, SHADOW): unify vol signature (profile) +
                        # shrunk realized edge → per-ticker risk cap (min, down-only) — logged, NOT applied.
                        try:
                            from agora.ops.ticker_adapter import run_ticker_adapter
                            _ta = run_ticker_adapter(self._db_path)
                            logger.info("ticker-adapter: %s", _ta.get("summary", _ta))
                        except Exception as _taexc:
                            logger.debug("ticker-adapter skipped: %s", _taexc)
                except Exception as _mrexc:
                    logger.debug("model-runner skipped: %s", _mrexc)
                if result.get("attributed", 0) > 0 or new_analyst > 0:
                    logger.info("Attribution patrol: %s", result)
                # Trigger lessons when N new attributions OR weekly timer
                self._total_attributed_since_last_lesson += new_analyst
                now = asyncio.get_event_loop().time()
                time_triggered = (now - self._last_lesson_time) >= LESSON_TIME_INTERVAL_SEC
                count_triggered = self._total_attributed_since_last_lesson >= LESSON_TRIGGER_NEW_ATTRIBUTIONS
                if count_triggered or time_triggered:
                    await self._try_generate_lessons()
                    self._total_attributed_since_last_lesson = 0
                    self._last_lesson_time = now
                # Promotion threshold check
                await self._check_promotion_alerts()
                # Weekly conviction calibrator — persisted wall-clock cadence (survives restarts)
                if self._weekly_due("calibration"):
                    await self._run_calibrator()
                    self._weekly_mark("calibration")
                # Weekly cross-agent performance analysis — persisted wall-clock cadence
                if self._weekly_due("perf_analysis"):
                    await self._run_performance_analysis()
                    self._weekly_mark("perf_analysis")
            except Exception as exc:
                logger.error("ScheduledAttributor error: %s", exc)
            await asyncio.sleep(PATROL_INTERVAL_SEC)

    async def stop(self) -> None:
        self._running = False

    # ── Lesson synthesis ──────────────────────────────────────────────────────

    async def _try_generate_lessons(self) -> None:
        try:
            from agora.agents.lessons_generator import LessonsGenerator
            gen = LessonsGenerator(db_path=self._db_path)
            await gen.generate_all()
            # Unblock the loop: auto-approve the freshly-generated lessons that clear the strict
            # evidence bar, so high-confidence learnings flow into agent context instead of
            # stranding behind manual approval (bounded §17 relaxation — see lessons_store).
            try:
                from agora.core.config import get_settings
                from agora.ops.lessons_store import auto_approve_lessons
                auto_approve_lessons(self._db_path, get_settings())
            except Exception as _aex:
                logger.debug("auto_approve_lessons skipped: %s", _aex)
        except Exception as exc:
            logger.warning("LessonsGenerator skipped: %s", exc)

    async def _run_performance_analysis(self) -> None:
        """Run PerformanceAnalystAgent weekly — cross-agent meta-analysis + Discord digest."""
        try:
            from agora.agents.performance_analyst import PerformanceAnalystAgent
            agent  = PerformanceAnalystAgent(db_path=self._db_path)
            result = await agent.analyze()
            digest = result.get("digest", "")
            n      = result.get("lessons_written", 0)
            if digest:
                await self._send_webhook(digest)
            elif n > 0:
                await self._send_webhook(
                    f"📚 PerformanceAnalyst: {n} cross-agent lesson(s) pending review. "
                    f"Reply `!lessons` to see them."
                )
            # Surface any calibration flags as additional alerts
            for flag in result.get("calibration_flags", []):
                if flag.get("severity") == "high":
                    await self._send_webhook(
                        f"⚠️ **Calibration flag [{flag.get('agent','?')}]:** {flag.get('issue','?')}"
                    )
        except Exception as exc:
            logger.warning("PerformanceAnalystAgent skipped: %s", exc)

    # ── Promotion alerts ──────────────────────────────────────────────────────

    async def _check_promotion_alerts(self) -> None:
        """Check each shadow agent against its promotion threshold and alert via Discord."""
        try:
            readiness = get_promotion_readiness(self._db_path)
        except Exception:
            return

        alerts: list[str] = []
        for agent, spec in _PROMOTION_THRESHOLDS.items():
            info = readiness.get(f"{agent}_intelligence", readiness.get(agent, {}))
            status = info.get("status", "")
            if status == "READY_TO_PROMOTE":
                metric_val = info.get(spec["metric"], info.get("decision_accuracy"))
                alerts.append(
                    f"🎯 **{agent.upper()} agent ready to promote out of shadow mode!**\n"
                    f"  {spec['metric']}={metric_val:.0%} ≥ {spec['threshold']:.0%} "
                    f"over {info.get('row_count', info.get('attributed_count', '?'))} samples.\n"
                    f"  Set `{agent}_shadow_mode=false` in .env to go live."
                )

        if alerts:
            msg = "\n\n".join(alerts)
            logger.info("Promotion alerts: %d agent(s) ready", len(alerts))
            await self._send_webhook(msg)

    # ── ConvictionCalibrator ──────────────────────────────────────────────────

    async def _run_calibrator(self) -> None:
        try:
            from pathlib import Path

            from agora.ops.conviction_calibrator import calibrate
            Path(self._cal_output).parent.mkdir(parents=True, exist_ok=True)
            result = calibrate(self._db_path, self._cal_output)
            n = result.get("total_closed_trades", 0)
            notes = result.get("proposal_notes", [])
            summary = (
                f"📊 **Weekly Calibration Report**\n"
                f"  {n} trades analyzed ({result.get('analysis_period_days',90)}d)\n"
            )
            for note in notes[:3]:
                summary += f"  • {note[:160]}\n"
            if n < result.get("min_trades_for_proposal", 50):
                summary += "  ⚠️ Insufficient data for weight proposals — collecting more trades.\n"
            summary += "  Full report: `.agora/calibration_report.json`"
            logger.info("ConvictionCalibrator complete: %d trades", n)
            await self._send_webhook(summary)
        except Exception as exc:
            logger.warning("ConvictionCalibrator error: %s", exc)

    # ── Discord webhook ───────────────────────────────────────────────────────

    async def _send_webhook(self, msg: str) -> None:
        if not self._webhook:
            logger.info("Attribution alert (no webhook):\n%s", msg)
            return
        try:
            import httpx
            chunks = [msg[i:i+1900] for i in range(0, len(msg), 1900)]
            async with httpx.AsyncClient(timeout=10) as client:
                for chunk in chunks:
                    await client.post(self._webhook, json={"content": chunk})
        except Exception as exc:
            logger.warning("Attribution webhook failed: %s", repr(exc))

"""
agora/ops/feature_store.py — Phase 0b: the unified, model-ready feature table (2026-06-22).

Materializes ONE row per position into `trade_features`: the complete entry feature vector + the
lifecycle PATH features (from lifecycle_snapshots) + the outcome label. This is the single table the
ML models (M1/M4/M5/M8) train and score against — assembled once so each model reads clean columns
instead of re-joining six journals.

Guarantees (same as Phase 0a):
  • READ-ONLY on every source (positions, decision_chains, lifecycle_snapshots) — SELECT only.
  • Writes ONLY to `trade_features`. Touches no execution/order/gate code → cannot affect trading.
  • Idempotent: INSERT OR REPLACE keyed on position_id (full rebuild each run; table is small).
  • Label uses the _REAL_CLOSE provenance + the legacy cutoff, so no model trains on fabricated rows.
  • Never raises.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, date, datetime
from typing import Any

# SINGLE SOURCE OF TRUTH for the label provenance — the canonical predicate from edge_dashboard, so
# the ML label can never drift from the rest of the system (expert review 2026-06-22: three
# hand-maintained copies is exactly how the fabricated-label bug re-emerges). The LABEL is set only on
# trustworthy, agent-driven, post-cutoff closes.
from agora.ops.edge_dashboard import _REAL_CLOSE

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS trade_features (
    position_id         TEXT PRIMARY KEY,
    ticker              TEXT,
    -- entry feature vector
    strategy            TEXT,
    structure_class     TEXT,    -- credit_spread | debit_spread | long_option | other
    pillar              TEXT,
    direction           TEXT,
    is_credit           INTEGER,
    conviction_at_entry REAL,
    regime_at_entry     TEXT,
    dte_at_entry        INTEGER,
    rr_ratio            REAL,
    max_loss_dollars    REAL,
    max_gain_dollars    REAL,
    entry_price         REAL,
    -- discovery / gates
    triggered_by        TEXT,
    gates_passed_n      INTEGER,
    -- lifecycle PATH features (from lifecycle_snapshots)
    n_frames            INTEGER,
    days_held           INTEGER,
    max_adverse_pct     REAL,    -- worst unrealized / max_loss seen
    max_favorable_pct   REAL,    -- best captured / max_gain seen
    final_net_delta     REAL,
    final_net_theta     REAL,
    -- label
    status              TEXT,
    is_real_close       INTEGER,
    realized_pnl        REAL,
    return_on_risk      REAL,
    win                 INTEGER, -- 1 win / 0 loss / NULL if not a real close
    config_version_at_entry INTEGER, -- settings regime this trade was opened under (ML provenance)
    hv_at_entry         REAL,    -- per-ticker realized vol (drives the adaptive stop + adaptive sizing)
    built_at            TEXT
);
CREATE INDEX IF NOT EXISTS idx_tf_status ON trade_features(status);
CREATE INDEX IF NOT EXISTS idx_tf_win    ON trade_features(win);
"""


def _structure_class(strategy: str | None, is_credit: bool) -> str:
    s = (strategy or "").lower()
    if "spread" in s or "condor" in s:
        return "credit_spread" if is_credit else "debit_spread"
    if s.startswith("long_"):
        return "long_option"
    return "other"


def _dte(entry: str | None, expiry: str | None) -> int | None:
    try:
        return (date.fromisoformat(str(expiry)[:10]) - date.fromisoformat(str(entry)[:10])).days
    except Exception:
        return None


def build_feature_store(db_path: str, legacy_cutoff: str = "2026-06-12") -> dict[str, Any]:
    """Rebuild `trade_features` from positions + decision_chains + lifecycle_snapshots. Never raises."""
    built = 0
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_DDL)
        # Migration: CREATE TABLE IF NOT EXISTS never ADDS a column to a pre-existing table, so a
        # table created before a DDL column was introduced is missing it — and EVERY row INSERT then
        # fails silently (caught per-row), freezing the feature store. Add any missing DDL columns.
        _have = {r[1] for r in conn.execute("PRAGMA table_info(trade_features)")}
        for _col, _type in (("config_version_at_entry", "INTEGER"), ("hv_at_entry", "REAL")):
            if _col not in _have:
                conn.execute(f"ALTER TABLE trade_features ADD COLUMN {_col} {_type}")
        conn.row_factory = sqlite3.Row
        has_life = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='lifecycle_snapshots'").fetchone()[0]
        # Per-ticker realized vol (the signal that drives BOTH the adaptive stop and the adaptive entry
        # sizing) → give the ML the same per-ticker vol the engine adapts on. Read once; join per row.
        hv_by_ticker = {r[0]: r[1] for r in conn.execute(
            "SELECT ticker, hv_annual FROM ticker_profiles WHERE hv_annual IS NOT NULL")} \
            if conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='ticker_profiles'").fetchone()[0] \
            else {}

        positions = conn.execute("SELECT * FROM positions").fetchall()
        for p in positions:
            try:
                pid = p["position_id"]
                ml = abs(p["max_loss_dollars"] or 0.0)
                mg = abs(p["max_gain_dollars"] or 0.0)
                is_credit = 1 if (p["entry_price"] or 0) < 0 else 0
                # discovery / gates from the linked decision chain (latest)
                dc = conn.execute(
                    "SELECT triggered_by, gates_passed FROM decision_chains "
                    "WHERE position_id=? ORDER BY started_at DESC LIMIT 1", (pid,)).fetchone()
                gates_n = None
                if dc and dc["gates_passed"]:
                    try:
                        import json as _j
                        gates_n = len(_j.loads(dc["gates_passed"]))
                    except Exception:
                        gates_n = None
                # lifecycle path features
                nf = dh = mae = mfe = fnd = fnt = None
                if has_life:
                    lr = conn.execute(
                        """SELECT COUNT(*) nf, MAX(days_held) dh, MIN(unrealized_pct_risk) mae,
                                  MAX(captured_pct_gain) mfe FROM lifecycle_snapshots WHERE position_id=?""",
                        (pid,)).fetchone()
                    nf, dh, mae, mfe = lr["nf"], lr["dh"], lr["mae"], lr["mfe"]
                    fr = conn.execute(
                        """SELECT net_delta, net_theta FROM lifecycle_snapshots WHERE position_id=?
                           ORDER BY snapshot_date DESC LIMIT 1""", (pid,)).fetchone()
                    if fr:
                        fnd, fnt = fr["net_delta"], fr["net_theta"]
                # Prefer the RUNNING MFE/MAE (captured at every mark → true excursion) over the
                # sparse daily-snapshot reconstruction, which missed intraday peaks/troughs on a
                # ~2-day swing book. peak/trough are dollars → convert to pct of max_gain/max_loss.
                _keys = p.keys()
                peak = p["peak_unrealized_pnl"] if "peak_unrealized_pnl" in _keys else None
                trough = p["trough_unrealized_pnl"] if "trough_unrealized_pnl" in _keys else None
                if peak is not None and mg > 0:
                    mfe = round(peak / mg, 4)
                if trough is not None and ml > 0:
                    mae = round(trough / ml, 4)
                # label — only on a trustworthy real close, post-cutoff
                is_real = conn.execute(
                    f"SELECT COUNT(*) FROM positions WHERE position_id=? AND ({_REAL_CLOSE}) "
                    f"AND close_date>=?", (pid, legacy_cutoff)).fetchone()[0]
                rpnl = p["realized_pnl"]
                win = (1 if (rpnl or 0) > 0 else 0) if is_real else None
                ror = round(rpnl / ml, 4) if (is_real and rpnl is not None and ml > 0) else None

                conn.execute(
                    """INSERT OR REPLACE INTO trade_features (
                        position_id, ticker, strategy, structure_class, pillar, direction, is_credit,
                        conviction_at_entry, regime_at_entry, dte_at_entry, rr_ratio, max_loss_dollars,
                        max_gain_dollars, entry_price, triggered_by, gates_passed_n, n_frames, days_held,
                        max_adverse_pct, max_favorable_pct, final_net_delta, final_net_theta,
                        status, is_real_close, realized_pnl, return_on_risk, win,
                        config_version_at_entry, hv_at_entry, built_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (pid, p["ticker"], p["strategy"], _structure_class(p["strategy"], bool(is_credit)),
                     p["pillar"], p["direction"], is_credit, p["conviction_at_entry"], p["regime_at_entry"],
                     _dte(p["entry_date"], p["expiry_date"]),
                     round(mg / ml, 3) if ml > 0 else None, ml, mg, p["entry_price"],
                     dc["triggered_by"] if dc else None, gates_n, nf, dh, mae, mfe, fnd, fnt,
                     p["status"], 1 if is_real else 0, rpnl, ror, win,
                     (p["config_version_at_entry"] if "config_version_at_entry" in p.keys() else None),
                     hv_by_ticker.get(p["ticker"]),
                     datetime.now(UTC).date().isoformat()),
                )
                built += 1   # count REAL inserts so a silent row-failure (e.g. schema drift) is visible
            except Exception as _exc:
                logger.debug("feature row failed for %s: %s", p["position_id"], _exc)
        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM trade_features").fetchone()[0]
        labeled = conn.execute("SELECT COUNT(*) FROM trade_features WHERE win IS NOT NULL").fetchone()[0]
        if built < total:   # some rows failed to (re)build — surface it instead of masking
            logger.warning("feature_store: only %d/%d rows rebuilt — schema drift or bad data", built, total)
        conn.close()
        return {"rows": total, "inserted": built, "labeled": labeled}
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("build_feature_store failed: %s", exc)
        return {"rows": built, "error": str(exc)}

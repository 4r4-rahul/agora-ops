"""
agora/ops/config_provenance.py — settings-regime provenance so the ML/analytics layer knows WHICH
settings each trade ran under.

Every recalibration we ship (R1/R2/R3, the #4 risk cap, future per-ticker settings, exit tuning…)
changes how trades are selected/sized/exited. Without provenance, the ML mixes trades from different
settings regimes and mis-attributes outcomes. This module records a monotonic `config_version` each
time a behaviour-affecting setting changes (with an auto-generated diff of what changed), and the
engine stamps `config_version_at_entry` on every position. The feature store carries it through, so a
model can say "win-rate under v7 (post-risk-cap) vs v4 (pre-cap)" instead of blending them.

Design: fingerprint a curated set of trade-affecting tunables. On engine start, if the fingerprint
differs from the latest recorded version, insert a new version row (diff vs previous as description).
Never raises — provenance must never break trading.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

# Behaviour-affecting tunables — the knobs whose change alters trade SELECTION, SIZING, or EXITS.
# Add a name here whenever you introduce a tunable that changes how trades are made, so ML keeps a
# faithful map of "what changed when". (Pure infra/logging settings are intentionally excluded.)
_TRACKED: tuple[str, ...] = (
    # entry gates
    "credit_spread_short_delta",
    "long_options_min_conviction",
    "neutral_regime_require_trend_confirm",
    "long_options_ivr_cap",
    "long_options_rsi_overbought",
    "long_options_rsi_oversold",
    "max_debit_to_width_ratio",
    "min_conviction_score",
    # sizing
    "high_conviction_size_boost_enabled",
    "max_risk_per_trade_dollars",
    "risk_per_trade_dollars",
    "max_contracts_per_trade",
    "max_position_size_pct",
    "edge_sizing_enabled",
    "adaptive_entry_sizing_enabled",   # per-ticker vol-normalized entry sizing
    "adaptive_size_ceil",              # 1.0 down-only vs >1.0 two-sided risk parity (regime change)
    # exits — adaptivity regime (so ML segments outcomes by the stop logic in force)
    "adaptive_stop_enabled",           # per-ticker vol-normalized stop level
    "surveillance_act_all_stops",      # structure stops live vs shadow
    # risk / portfolio
    "max_open_positions",
    "daily_loss_limit_pct",
)

_DDL = """
CREATE TABLE IF NOT EXISTS config_versions (
    version       INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint   TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    description   TEXT NOT NULL,
    settings_json TEXT NOT NULL
);
"""


def _snapshot(settings: Any) -> dict:
    """Curated trade-affecting settings → a plain JSON-able dict (Path/enum coerced to str)."""
    snap: dict[str, Any] = {}
    for k in _TRACKED:
        v = getattr(settings, k, None)
        snap[k] = v if isinstance(v, (int, float, bool, str, type(None))) else str(v)
    return snap


def _fingerprint(snap: dict) -> str:
    return hashlib.sha256(json.dumps(snap, sort_keys=True).encode()).hexdigest()[:16]


def _diff(old: dict, new: dict) -> str:
    changes = [f"{k}: {old.get(k)}→{new.get(k)}"
               for k in sorted(set(old) | set(new)) if old.get(k) != new.get(k)]
    return "; ".join(changes) if changes else "initial snapshot"


def record_config_version(db_path: str, settings: Any) -> int:
    """Ensure the current settings regime is recorded; return its version. No-op when unchanged.
    Never raises — returns 0 on any error so trading is never blocked by provenance."""
    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        conn.executescript(_DDL)
        snap = _snapshot(settings)
        fp = _fingerprint(snap)
        latest = conn.execute(
            "SELECT version, fingerprint, settings_json FROM config_versions "
            "ORDER BY version DESC LIMIT 1").fetchone()
        if latest and latest[1] == fp:
            conn.close()
            return int(latest[0])                       # unchanged → same regime
        desc = _diff(json.loads(latest[2]), snap) if latest else "initial snapshot"
        cur = conn.execute(
            "INSERT INTO config_versions (fingerprint, created_at, description, settings_json) "
            "VALUES (?,?,?,?)",
            (fp, datetime.now(UTC).isoformat(), desc, json.dumps(snap)))
        conn.commit()
        v = int(cur.lastrowid)
        conn.close()
        return v
    except Exception:
        return 0


def current_config_version(db_path: str) -> int:
    """Latest recorded version (0 if none). Never raises."""
    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        conn.executescript(_DDL)
        row = conn.execute("SELECT version FROM config_versions ORDER BY version DESC LIMIT 1").fetchone()
        conn.close()
        return int(row[0]) if row else 0
    except Exception:
        return 0

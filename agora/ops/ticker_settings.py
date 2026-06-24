"""
agora/ops/ticker_settings.py — per-ticker adaptive settings (Phase 1 foundation).

Each ticker has its own "story" — its own response to market conditions — so the engine should be
able to override global tunables per ticker (volatility-scaled stops, regime-conditional structure,
sizing by the ticker's own realized edge). This module is the STORE + RESOLVER.

Safety model (panel-approved):
  • SPARSE — a ticker uses the global default until it EARNS an override from its own clean history.
  • SHADOW-FIRST — overrides are written with active=0 (logged, validated, never applied) until
    explicitly promoted. The resolver applies ONLY active=1 rows → Phase 1+2 are fully inert.
  • ZERO-REGRESSION — resolve(ticker, key, default) returns the global default for any (ticker, key)
    without a LIVE override, so an un-overridden engine behaves byte-for-byte as today.
  • CACHED — active overrides load once (per day / per resolver build); resolve() is an O(1) dict hit.

Promotion to active=1 (Phase 3) is gated separately (n-samples + shadow-validated + down-only).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

_DDL = """
CREATE TABLE IF NOT EXISTS ticker_settings (
    ticker         TEXT NOT NULL,
    setting_key    TEXT NOT NULL,
    value_json     TEXT NOT NULL,
    source         TEXT NOT NULL,           -- which adapter/version produced it
    n_samples      INTEGER NOT NULL,        -- clean closes the override was computed from
    active         INTEGER NOT NULL DEFAULT 0,  -- 0 = shadow (logged, NOT applied), 1 = live
    rationale      TEXT,                    -- human/ML-readable why
    config_version INTEGER,                 -- settings regime when written (provenance)
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (ticker, setting_key)
);
"""


class TickerSettingsResolver:
    """Resolve a tunable for a ticker: the LIVE per-ticker override if one exists, else the global
    default. Loads active overrides once into a cache (rebuild/reload daily). Never raises."""

    def __init__(self, db_path: Any) -> None:
        self._db_path = str(db_path)
        self._cache: dict[tuple[str, str], Any] = {}
        self._loaded = False

    def load(self) -> TickerSettingsResolver:
        """(Re)load LIVE overrides into the cache. Call once per day / on settings change."""
        cache: dict[tuple[str, str], Any] = {}
        try:
            conn = sqlite3.connect(self._db_path, timeout=10)
            conn.executescript(_DDL)
            for tk, key, vj in conn.execute(
                "SELECT ticker, setting_key, value_json FROM ticker_settings WHERE active=1"):
                try:
                    cache[(tk, key)] = json.loads(vj)
                except Exception:
                    continue
            conn.close()
        except Exception:
            pass
        self._cache = cache
        self._loaded = True
        return self

    def resolve(self, ticker: str, key: str, global_default: Any) -> Any:
        """The live per-ticker value for (ticker, key), or the global default. O(1)."""
        if not self._loaded:
            self.load()
        return self._cache.get((str(ticker), key), global_default)

    @property
    def active_count(self) -> int:
        if not self._loaded:
            self.load()
        return len(self._cache)


def set_override(db_path: Any, ticker: str, key: str, value: Any, *, source: str,
                 n_samples: int, active: bool = False, rationale: str = "",
                 config_version: int | None = None) -> bool:
    """Upsert a per-ticker override. active=False (default) writes a SHADOW row (logged, not applied).
    Returns True on success. Never raises."""
    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        conn.executescript(_DDL)
        conn.execute(
            "INSERT INTO ticker_settings "
            "(ticker, setting_key, value_json, source, n_samples, active, rationale, config_version, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(ticker, setting_key) DO UPDATE SET "
            "value_json=excluded.value_json, source=excluded.source, n_samples=excluded.n_samples, "
            "active=excluded.active, rationale=excluded.rationale, config_version=excluded.config_version, "
            "updated_at=excluded.updated_at",
            (str(ticker), key, json.dumps(value), source, int(n_samples), 1 if active else 0,
             rationale, config_version, datetime.now(UTC).isoformat()))
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False


def get_overrides(db_path: Any, *, active_only: bool = False) -> list[dict]:
    """All per-ticker overrides (for the dashboard / analytics). Never raises."""
    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        conn.executescript(_DDL)
        conn.row_factory = sqlite3.Row
        where = "WHERE active=1" if active_only else ""
        rows = conn.execute(
            f"SELECT ticker, setting_key, value_json, source, n_samples, active, rationale, "
            f"config_version, updated_at FROM ticker_settings {where} ORDER BY ticker, setting_key"
        ).fetchall()
        conn.close()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["value"] = json.loads(d.pop("value_json"))
            except Exception:
                d["value"] = d.pop("value_json", None)
            out.append(d)
        return out
    except Exception:
        return []

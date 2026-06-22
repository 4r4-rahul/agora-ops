"""
agora/ops/market_capture.py — Phase 0d: a daily snapshot of the MARKET regime state, so the regime
model (M2) has real features. Records VIX + the VIX term structure (VIX9D/VIX/VIX3M — backwardation
signals stress) + SPY/QQQ daily change into `market_snapshots`, once/day.

PURELY ADDITIVE: writes only market_snapshots, touches no trading/order/gate code → cannot affect
execution. Idempotent per day. Network-failure-safe (a fetch failure → skip, never crash). The fetcher
is injectable so tests don't hit the network. Never raises.
"""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS market_snapshots (
    snapshot_date   TEXT PRIMARY KEY,
    captured_at_utc TEXT,
    vix             REAL,
    vix9d           REAL,
    vix3m           REAL,
    term_ratio      REAL,    -- vix9d / vix3m : >1 backwardation (stress), <1 contango (calm)
    vol_regime      TEXT,    -- low | elevated | high | extreme
    term_state      TEXT,    -- contango | flat | backwardation
    spy_chg_pct     REAL,
    qqq_chg_pct     REAL
);
"""

_SYMBOLS = {"VIX": "^VIX", "VIX9D": "^VIX9D", "VIX3M": "^VIX3M", "SPY": "SPY", "QQQ": "QQQ"}

_IV_CACHE_DIR = ".agora/iv_cache"


def iv_rank_for_ticker(ticker: str, cache_dir: str = _IV_CACHE_DIR) -> float | None:
    """Per-ticker IV-rank (0-100) from the daily ATM-IV cache the live scans populate
    (.agora/iv_cache/{ticker}.json). Mirrors yfinance_provider._iv_rank_from_cache. None if no/too-few
    data. Pure file read — no network. This is the per-ticker signal M2 needs (market VIX is too coarse)."""
    import json
    import os
    try:
        path = os.path.join(cache_dir, f"{ticker}.json")
        if not os.path.exists(path):
            return None
        with open(path) as f:
            ivs = json.load(f).get("atm_ivs") or []
        if len(ivs) < 3:
            return None
        mn, mx = min(ivs), max(ivs)
        if mx <= mn:
            return 50.0
        return round(max(0.0, min(100.0, (ivs[-1] - mn) / (mx - mn) * 100)), 1)
    except Exception:
        return None


def _yf_fetch() -> dict[str, dict[str, float | None]]:
    """Default fetcher — yfinance fast_info. Returns {label: {price, change_pct}}. Best-effort."""
    import yfinance as yf
    out: dict[str, dict[str, float | None]] = {}
    for label, sym in _SYMBOLS.items():
        try:
            fi = yf.Ticker(sym).fast_info
            price = float(fi.get("lastPrice"))
            prev = float(fi.get("previousClose") or price)
            chg = round((price - prev) / prev * 100, 2) if prev else 0.0
            out[label] = {"price": round(price, 2), "change_pct": chg}
        except Exception:
            out[label] = {"price": None, "change_pct": None}
    return out


def _vol_regime(vix: float | None) -> str:
    if vix is None:
        return "unknown"
    if vix >= 30:
        return "extreme"
    if vix >= 22:
        return "high"
    if vix >= 16:
        return "elevated"
    return "low"


def _term_state(ratio: float | None) -> str:
    if ratio is None:
        return "unknown"
    if ratio >= 1.03:
        return "backwardation"   # near-term fear > long-term → stress
    if ratio <= 0.97:
        return "contango"        # calm
    return "flat"


def capture_market_snapshot(db_path: str, as_of: str | None = None,
                            fetcher: Callable[[], dict] | None = None) -> dict[str, Any]:
    """Capture today's market regime snapshot (idempotent/day). Never raises."""
    today = as_of or date.today().isoformat()
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_DDL)
        if conn.execute("SELECT 1 FROM market_snapshots WHERE snapshot_date=?", (today,)).fetchone():
            conn.close()
            return {"captured": 0, "skipped": 1, "snapshot_date": today}
        data = (fetcher or _yf_fetch)()
        vix = data.get("VIX", {}).get("price")
        vix9d = data.get("VIX9D", {}).get("price")
        vix3m = data.get("VIX3M", {}).get("price")
        term = round(vix9d / vix3m, 3) if (vix9d and vix3m) else None
        if vix is None:
            conn.close()
            return {"captured": 0, "skipped": 1, "snapshot_date": today, "note": "no VIX data"}
        conn.execute(
            """INSERT OR REPLACE INTO market_snapshots
               (snapshot_date, captured_at_utc, vix, vix9d, vix3m, term_ratio, vol_regime, term_state,
                spy_chg_pct, qqq_chg_pct) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (today, datetime.now(UTC).isoformat(timespec="seconds"), vix, vix9d, vix3m, term,
             _vol_regime(vix), _term_state(term),
             data.get("SPY", {}).get("change_pct"), data.get("QQQ", {}).get("change_pct")))
        conn.commit(); conn.close()
        return {"captured": 1, "snapshot_date": today, "vix": vix, "vol_regime": _vol_regime(vix),
                "term_state": _term_state(term)}
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("capture_market_snapshot failed: %s", exc)
        return {"captured": 0, "error": str(exc)}

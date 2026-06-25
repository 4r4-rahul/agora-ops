"""
agora/ops/ticker_profile.py — per-ticker market characterization (Phase A).

Each ticker has its own "magnetic response" to the market. With only ~4 of our own closes per ticker,
we cannot learn settings from trade outcomes yet — but we CAN learn each ticker's behaviour from
years of price history. This module computes a per-ticker signature (realized vol, ATR%, trend
persistence, beta-to-SPY, 1-month momentum) and derives a heuristic, DOWN-ONLY, vol-scaled risk-cap
factor that the per-ticker adapter combines with the edge signal.

Design:
  • characterize() is PURE — arrays in, signature out — so every edge case is unit-testable.
  • The fetcher is injectable; the default pulls daily closes via yfinance.
  • vol_scaled_cap_factor() is DOWN-ONLY: a high-realized-vol ticker (wider swings per dollar of risk)
    gets a TIGHTER cap; a calm ticker stays at 1.0 (never loosened). Clamped to [_DOWN_MIN, 1.0].
  • Profiles are stored in `ticker_profiles`; the cap factor flows into ticker_settings (shadow).

Never raises in the IO paths (build_profiles); the pure math validates its inputs and returns None on
insufficient/garbage data rather than guessing.
"""
from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

_MIN_DAYS = 60         # need ~3 months of clean closes before a profile is trustworthy
_TARGET_HV = 0.30      # annualized realized-vol anchor; cap_factor = target/hv (down-only)
_DOWN_MIN = 0.5        # never tighten the per-ticker cap below 50% of global on vol alone

_DDL = """
CREATE TABLE IF NOT EXISTS ticker_profiles (
    ticker            TEXT PRIMARY KEY,
    n_days            INTEGER NOT NULL,
    hv_annual         REAL,      -- annualized close-to-close realized vol
    atr_pct           REAL,      -- mean true-range as a fraction of price (None if no high/low)
    trend_persistence REAL,      -- fraction of days close > its 20d SMA  [0,1]
    beta_spy          REAL,      -- beta vs SPY (None if SPY history absent)
    momentum_1m       REAL,      -- 21-day return
    cap_factor        REAL,      -- derived down-only vol-scaled risk-cap factor [_DOWN_MIN, 1.0]
    updated_at        TEXT NOT NULL
);
"""


def _clean(seq: Any) -> list[float]:
    """Coerce to a list of finite positive floats (drops None/NaN/inf/<=0 — garbage IV/price)."""
    out: list[float] = []
    for x in seq or []:
        try:
            v = float(x)
        except (TypeError, ValueError):
            continue
        if math.isfinite(v) and v > 0:
            out.append(v)
    return out


def _log_returns(closes: list[float]) -> list[float]:
    return [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]


def _std(xs: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    m = sum(xs) / n
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def characterize(closes: Any, *, highs: Any = None, lows: Any = None,
                 spy_closes: Any = None) -> dict | None:
    """Compute a ticker's market signature from daily closes. Returns None if there is not enough
    clean data (< _MIN_DAYS). Pure — no IO, never raises on well-formed numeric input."""
    c = _clean(closes)
    if len(c) < _MIN_DAYS:
        return None
    rets = _log_returns(c)
    hv = _std(rets) * math.sqrt(252)

    # ATR% — only if aligned high/low present; else None (close-to-close vol already captured by hv).
    atr_pct: float | None = None
    h, low = _clean(highs), _clean(lows)
    if len(h) == len(c) and len(low) == len(c) and len(c) >= 2:
        trs = []
        for i in range(1, len(c)):
            tr = max(h[i] - low[i], abs(h[i] - c[i - 1]), abs(low[i] - c[i - 1]))
            trs.append(tr / c[i] if c[i] > 0 else 0.0)
        atr_pct = sum(trs) / len(trs) if trs else None

    # Trend persistence — fraction of days the close is above its trailing 20d SMA.
    win = 20
    if len(c) > win:
        above = sum(1 for i in range(win, len(c)) if c[i] > sum(c[i - win:i]) / win)
        trend = above / (len(c) - win)
    else:
        trend = 0.5

    # Beta vs SPY — only when an aligned SPY series is supplied.
    beta: float | None = None
    s = _clean(spy_closes)
    if len(s) == len(c) and len(c) >= _MIN_DAYS:
        sr = _log_returns(s)
        var_s = _std(sr) ** 2
        if var_s > 0 and len(sr) == len(rets):
            ms, mr = sum(sr) / len(sr), sum(rets) / len(rets)
            cov = sum((sr[i] - ms) * (rets[i] - mr) for i in range(len(sr))) / (len(sr) - 1)
            beta = cov / var_s

    momentum_1m = (c[-1] / c[-22] - 1.0) if len(c) >= 22 else 0.0

    return {
        "n_days": len(c),
        "hv_annual": round(hv, 4),
        "atr_pct": round(atr_pct, 4) if atr_pct is not None else None,
        "trend_persistence": round(trend, 3),
        "beta_spy": round(beta, 3) if beta is not None else None,
        "momentum_1m": round(momentum_1m, 4),
        "cap_factor": vol_scaled_cap_factor(hv),
    }


def vol_scaled_cap_factor(hv_annual: float, target: float = _TARGET_HV,
                          down_min: float = _DOWN_MIN) -> float:
    """Down-only vol-scaled risk-cap factor in [down_min, 1.0]. High realized vol → tighter cap
    (a fixed dollar risk on a wild name is likelier to be hit); calm names stay at 1.0 (never up)."""
    if hv_annual <= 0 or not math.isfinite(hv_annual):
        return 1.0
    return round(max(down_min, min(1.0, target / hv_annual)), 3)


def _default_fetcher(tickers: list[str], lookback_days: int) -> dict[str, dict]:
    """Batch daily-close history via yfinance. Returns {ticker: {closes, highs, lows}}. Best-effort."""
    try:
        import yfinance as yf
        period = f"{max(1, lookback_days // 252 + 1)}y"
        data = yf.download(tickers, period=period, interval="1d",
                           group_by="ticker", progress=False, threads=False)
        out: dict[str, dict] = {}
        for t in tickers:
            try:
                df = data[t] if len(tickers) > 1 else data
                out[t] = {"closes": list(df["Close"].dropna()),
                          "highs": list(df["High"].dropna()),
                          "lows": list(df["Low"].dropna())}
            except Exception:
                continue
        return out
    except Exception:
        return {}


def build_profiles(db_path: Any, tickers: list[str], *,
                   fetcher: Callable[[list[str], int], dict[str, dict]] | None = None,
                   lookback_days: int = 756) -> dict:
    """Fetch ~3y history for `tickers`, characterize each, persist to ticker_profiles, and write the
    vol-scaled cap factor for the adapter to consume. Never raises."""
    fetch = fetcher or _default_fetcher
    uniq = sorted({t.upper() for t in tickers if t})
    if not uniq:
        return {"status": "skipped", "reason": "no tickers"}
    try:
        history = fetch(uniq + (["SPY"] if "SPY" not in uniq else []), lookback_days)
    except Exception:
        history = {}
    spy = (history.get("SPY") or {}).get("closes")

    written = 0
    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        conn.executescript(_DDL)
        for t in uniq:
            h = history.get(t)
            if not h:
                continue
            sig = characterize(h.get("closes"), highs=h.get("highs"), lows=h.get("lows"),
                               spy_closes=spy if t != "SPY" else None)
            if not sig:
                continue
            conn.execute(
                "INSERT INTO ticker_profiles (ticker, n_days, hv_annual, atr_pct, trend_persistence, "
                "beta_spy, momentum_1m, cap_factor, updated_at) VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(ticker) DO UPDATE SET n_days=excluded.n_days, hv_annual=excluded.hv_annual, "
                "atr_pct=excluded.atr_pct, trend_persistence=excluded.trend_persistence, "
                "beta_spy=excluded.beta_spy, momentum_1m=excluded.momentum_1m, "
                "cap_factor=excluded.cap_factor, updated_at=excluded.updated_at",
                (t, sig["n_days"], sig["hv_annual"], sig["atr_pct"], sig["trend_persistence"],
                 sig["beta_spy"], sig["momentum_1m"], sig["cap_factor"], datetime.now(UTC).isoformat()))
            written += 1
        conn.commit()
        conn.close()
    except Exception:
        return {"status": "error", "profiled": written}
    return {"status": "ok", "profiled": written, "requested": len(uniq),
            "summary": f"profiled {written}/{len(uniq)} tickers from ~3y price history"}


def get_profile(db_path: Any, ticker: str) -> dict | None:
    """Read one ticker's stored profile (for the adapter / dashboard). Never raises."""
    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        conn.executescript(_DDL)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM ticker_profiles WHERE ticker=?", (ticker.upper(),)).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception:
        return None


def resolve_size_factor(db_path: Any, ticker: str, *, enabled: bool = True) -> float:
    """Per-ticker RISK-PARITY entry-size multiplier (pure vol-math baseHV/HV) resolved from the ticker's
    stored realized-vol profile. This is the SINGLE shared factor both entry paths use — the spread
    rules-engine AND the long-options agent — so a name sizes identically no matter which strategy fires.
    Returns 1.0 when disabled or for an unprofiled ticker. PURE downstream math; the physical bound
    (max_contracts_per_trade + the 1-contract floor) is applied by each caller. Never raises."""
    if not enabled:
        return 1.0
    try:
        from agora.ops.adaptive_stop import size_factor
        prof = get_profile(db_path, (ticker or "").upper())
        return size_factor((prof or {}).get("hv_annual"))
    except Exception:
        return 1.0


def all_profiles(db_path: Any) -> list[dict]:
    """All stored profiles (dashboard). Never raises."""
    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        conn.executescript(_DDL)
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM ticker_profiles ORDER BY hv_annual DESC")]
        conn.close()
        return rows
    except Exception:
        return []

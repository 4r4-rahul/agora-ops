"""
agora/ops/news_events.py — per-ticker news-response capture (Phase N1).

The system already INGESTS rich per-ticker news (sector_intelligence, stock_analyst, uw_market_intel,
macro synth) but discards it. This module gives it MEMORY: every event is stored timestamped at the
moment we saw it (guaranteed point-in-time — no look-ahead, the trap that kills historical-news alpha),
then once the horizon elapses we capture the ticker's REALIZED forward behaviour. Aggregated per
(ticker, category) → a "news-response profile": does THIS ticker move WITH this kind of news, and how
much? Forward-captured, so it needs no expensive point-in-time vendor and cannot leak the future.

Pure math (compute_forward_return, _hit) is isolated for exhaustive edge-case testing. IO paths never
raise. Everything is observational/shadow — N1 captures and profiles; it does not yet steer trades.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

_HORIZON_DAYS = 5      # capture the ticker's realized move over the 5 trading days after an event
_MIN_N = 5             # a (ticker, category) cell needs >= this many captured events to profile

CATEGORIES = ("stock", "sector", "macro", "geopolitics", "earnings")

_DDL = """
CREATE TABLE IF NOT EXISTS news_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker        TEXT NOT NULL,
    category      TEXT NOT NULL,        -- stock | sector | macro | geopolitics | earnings
    sentiment     REAL NOT NULL,        -- signed [-1, 1]: + bullish, - bearish, 0 neutral
    source        TEXT NOT NULL,        -- which agent produced it
    headline      TEXT,
    event_ts_utc  TEXT NOT NULL,        -- WHEN WE SAW IT (point-in-time guarantee)
    spot_at_event REAL,                 -- price at ingestion (denominator for the forward move)
    fwd_return    REAL,                 -- realized return over _HORIZON_DAYS (NULL until captured)
    captured      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_news_ticker ON news_events(ticker);
CREATE INDEX IF NOT EXISTS idx_news_uncaptured ON news_events(captured);
"""


def _norm_sentiment(sentiment: Any) -> float:
    """Coerce a sentiment/direction to a signed float in [-1, 1]. Accepts numbers or direction words."""
    if isinstance(sentiment, (int, float)):
        return max(-1.0, min(1.0, float(sentiment)))
    s = str(sentiment or "").strip().lower()
    return {"bullish": 1.0, "positive": 1.0, "bearish": -1.0, "negative": -1.0,
            "neutral": 0.0, "": 0.0}.get(s, 0.0)


def record_news_event(db_path: Any, ticker: str, category: str, sentiment: Any, *,
                      source: str, headline: str = "", spot: float | None = None,
                      event_ts: str | None = None) -> bool:
    """Persist a news event AT INGESTION TIME (point-in-time). category is coerced to a known bucket;
    sentiment to a signed float. Returns True on success. Never raises."""
    cat = category if category in CATEGORIES else "stock"
    sent = _norm_sentiment(sentiment)
    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        conn.executescript(_DDL)
        cur = conn.execute(
            "INSERT INTO news_events (ticker, category, sentiment, source, headline, event_ts_utc, "
            "spot_at_event, captured) VALUES (?,?,?,?,?,?,?,0)",
            (str(ticker).upper(), cat, sent, source, headline[:300],
             event_ts or datetime.now(UTC).isoformat(),
             float(spot) if spot and spot > 0 else None))
        eid = cur.lastrowid
        conn.commit()
        conn.close()
        # Unify into the predicted-vs-actual ledger: a DIRECTIONAL news event predicts the move
        # direction → P(up) = 0.5 + sentiment/2. Scored later from the captured forward return.
        if sent != 0:
            try:
                from agora.ops.prediction_ledger import record_prediction
                record_prediction(db_path, source="news", target_key=f"news:{eid}",
                                  target_type="news", predicted=0.5 + sent / 2.0)
            except Exception:
                pass
        return True
    except Exception:
        return False


def compute_forward_return(spot_at_event: float, future_closes: Any) -> float | None:
    """PURE: realized return from the event spot to the close _HORIZON_DAYS later (or the last
    available close if fewer). Returns None on insufficient/garbage data — never guesses, never raises."""
    if not spot_at_event or spot_at_event <= 0:
        return None
    closes: list[float] = []
    for x in future_closes or []:
        try:
            v = float(x)
        except (TypeError, ValueError):
            continue
        if v > 0 and v == v:   # finite, positive (v==v rejects NaN)
            closes.append(v)
    if not closes:
        return None
    end = closes[min(_HORIZON_DAYS, len(closes)) - 1]
    return round(end / spot_at_event - 1.0, 5)


def _hit(sentiment: float, fwd_return: float) -> bool:
    """Did the ticker move IN the news direction? (neutral sentiment is never a hit/miss.)"""
    return (sentiment > 0 and fwd_return > 0) or (sentiment < 0 and fwd_return < 0)


def news_edge_hint(hit_rate: float | None, responsiveness: float, n: int) -> str:
    """PURE: classify a (ticker, category) news-response cell into an ACTIONABLE label (shadow — the
    bridge from capture to use). Conservative: 'insufficient' below sample, never over-claims edge.
      predictable_reactor  — moves WITH its news, materially → the news direction is a usable signal
      contrarian_or_noisy  — moves AGAINST/unpredictably while reacting hard → fade or avoid on news
      non_reactive         — barely moves on this news type → news is not informative here
      weak                 — reacts, but no clean directional edge
    """
    if n < _MIN_N or hit_rate is None:
        return "insufficient"
    if responsiveness < 0.01:
        return "non_reactive"
    if hit_rate >= 0.60 and responsiveness >= 0.02:
        return "predictable_reactor"
    if hit_rate <= 0.40 and responsiveness >= 0.03:
        return "contrarian_or_noisy"
    return "weak"


def default_capture_fetcher(ticker: str, since_iso: str) -> list[float]:
    """Daily closes from the event date onward (yfinance) for the capture job. Best-effort → []."""
    try:
        import yfinance as yf
        df = yf.download(ticker, start=since_iso[:10], interval="1d", progress=False, threads=False)
        return [float(x) for x in df["Close"].dropna()]
    except Exception:
        return []


def capture_forward_returns(db_path: Any, *,
                            fetcher: Callable[[str, str], list[float]] | None = None,
                            horizon_days: int = _HORIZON_DAYS) -> dict:
    """Backfill fwd_return for events whose horizon has elapsed. fetcher(ticker, since_iso) → closes
    AFTER the event. Never raises."""
    if fetcher is None:
        return {"status": "skipped", "reason": "no fetcher"}
    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        conn.executescript(_DDL)
        cutoff = datetime.now(UTC).timestamp() - horizon_days * 86400 * 1.4  # ~calendar pad for weekends
        rows = conn.execute(
            "SELECT id, ticker, event_ts_utc, spot_at_event FROM news_events "
            "WHERE captured=0").fetchall()
        captured = 0
        for eid, ticker, ts, spot in rows:
            try:
                if datetime.fromisoformat(ts).timestamp() > cutoff:
                    continue                       # horizon not elapsed yet — leave for later
                closes = fetcher(ticker, ts)
                if spot and spot > 0:
                    fr = compute_forward_return(spot, closes)          # spot captured at ingestion
                elif closes:
                    fr = compute_forward_return(closes[0], closes[1:])  # resolve spot from event-date close
                else:
                    fr = None
            except Exception:
                fr = None
            if fr is not None:
                conn.execute("UPDATE news_events SET fwd_return=?, captured=1 WHERE id=?", (fr, eid))
                captured += 1
        conn.commit()
        conn.close()
        return {"status": "ok", "captured": captured, "pending": len(rows) - captured}
    except Exception:
        return {"status": "error"}


def news_response_profile(db_path: Any, *, min_n: int = _MIN_N) -> list[dict]:
    """Per (ticker, category): n, hit-rate (moved with the news), mean signed response (+ = moves with
    sentiment), and responsiveness (mean |move|). Only cells with >= min_n captured events. Never raises."""
    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        conn.executescript(_DDL)
        rows = conn.execute(
            "SELECT ticker, category, sentiment, fwd_return FROM news_events "
            "WHERE captured=1 AND fwd_return IS NOT NULL").fetchall()
        conn.close()
    except Exception:
        return []
    cells: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for ticker, cat, sent, fr in rows:
        cells.setdefault((ticker, cat), []).append((sent, fr))
    out = []
    for (ticker, cat), pairs in cells.items():
        directional = [(s, f) for s, f in pairs if s != 0]
        n = len(pairs)
        if n < min_n:
            continue
        hits = sum(1 for s, f in directional if _hit(s, f))
        signed = [f * (1 if s > 0 else -1) for s, f in directional] or [0.0]
        hit_rate = round(hits / len(directional), 3) if directional else None
        responsiveness = round(sum(abs(f) for _, f in pairs) / n, 4)
        out.append({
            "ticker": ticker, "category": cat, "n": n,
            "hit_rate": hit_rate,
            "mean_signed_response": round(sum(signed) / len(signed), 4),
            "responsiveness": responsiveness,
            "hint": news_edge_hint(hit_rate, responsiveness, n),
        })
    out.sort(key=lambda d: -d["responsiveness"])
    return out


def recent_events(db_path: Any, limit: int = 40) -> list[dict]:
    """Most recent stored events (for the dashboard). Never raises."""
    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        conn.executescript(_DDL)
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT ticker, category, sentiment, source, headline, event_ts_utc, fwd_return, captured "
            "FROM news_events ORDER BY id DESC LIMIT ?", (limit,))]
        conn.close()
        return rows
    except Exception:
        return []

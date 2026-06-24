"""
agora/tests/test_news_events.py — per-ticker news-response capture (Phase N1).

Heavy edge coverage on the pure math (sentiment coercion, forward-return horizon/garbage, hit logic)
and the IO paths (record, horizon-gated capture, aggregation min-n + neutral handling, error-safety).
The design guarantee under test: point-in-time storage + forward-only capture (no look-ahead).
"""
from __future__ import annotations

import sqlite3
import tempfile
from datetime import UTC, datetime

from agora.ops.news_events import (
    _hit,
    _norm_sentiment,
    capture_forward_returns,
    compute_forward_return,
    news_response_profile,
    recent_events,
    record_news_event,
)


def _db():
    return tempfile.NamedTemporaryFile(suffix=".db", delete=False).name


# ── _norm_sentiment ──────────────────────────────────────────────────────────────────
def test_norm_sentiment_numbers_clamped():
    assert _norm_sentiment(0.5) == 0.5
    assert _norm_sentiment(9.0) == 1.0 and _norm_sentiment(-9.0) == -1.0


def test_norm_sentiment_words_and_garbage():
    assert _norm_sentiment("bullish") == 1.0 and _norm_sentiment("BEARISH") == -1.0
    assert _norm_sentiment("neutral") == 0.0
    assert _norm_sentiment(None) == 0.0 and _norm_sentiment("???") == 0.0


# ── compute_forward_return (pure) ─────────────────────────────────────────────────────
def test_forward_return_uses_horizon_close():
    # 5-day horizon → uses the 5th close (index 4)
    assert compute_forward_return(100.0, [101, 102, 103, 104, 110, 999]) == 0.10


def test_forward_return_fewer_than_horizon_uses_last():
    assert compute_forward_return(100.0, [102, 104, 106]) == 0.06   # only 3 → last


def test_forward_return_bad_inputs_return_none():
    assert compute_forward_return(0.0, [100]) is None        # zero spot
    assert compute_forward_return(-5.0, [100]) is None       # negative spot
    assert compute_forward_return(100.0, []) is None         # no closes
    assert compute_forward_return(100.0, None) is None
    assert compute_forward_return(100.0, [None, "x", -1, float("nan")]) is None  # all garbage


def test_forward_return_filters_garbage_but_keeps_clean():
    # garbage dropped; first 5 CLEAN closes used
    assert compute_forward_return(100.0, [None, 101, "x", 102, 103, 104, 105]) == 0.05


# ── _hit ──────────────────────────────────────────────────────────────────────────────
def test_hit_logic():
    assert _hit(1.0, 0.03) is True and _hit(-1.0, -0.02) is True
    assert _hit(1.0, -0.03) is False and _hit(-1.0, 0.02) is False
    assert _hit(0.0, 0.05) is False        # neutral sentiment is never a "hit"


# ── record + capture (horizon-gated, forward-only) ────────────────────────────────────
def test_record_coerces_category_and_sentiment():
    db = _db()
    assert record_news_event(db, "nvda", "weird_cat", "bullish", source="t", spot=100.0)
    ev = recent_events(db)[0]
    assert ev["ticker"] == "NVDA" and ev["category"] == "stock" and ev["sentiment"] == 1.0
    assert ev["captured"] == 0


def test_capture_skips_unelapsed_and_captures_elapsed():
    db = _db()
    # elapsed event (long past) → captured; fresh event (now) → left pending
    record_news_event(db, "AAA", "stock", 1.0, source="t", spot=100.0, event_ts="2020-01-01T00:00:00+00:00")
    record_news_event(db, "AAA", "stock", 1.0, source="t", spot=100.0,
                      event_ts=datetime.now(UTC).isoformat())
    r = capture_forward_returns(db, fetcher=lambda tk, since: [101, 102, 103, 104, 105])
    assert r["status"] == "ok" and r["captured"] == 1 and r["pending"] == 1


def test_capture_no_fetcher_is_safe():
    assert capture_forward_returns(_db())["status"] == "skipped"


# ── news_response_profile (aggregation) ───────────────────────────────────────────────
def _seed_captured(db, ticker, cat, pairs):
    """pairs: (sentiment, fwd_return) inserted directly as captured events."""
    c = sqlite3.connect(db)
    c.executescript("CREATE TABLE IF NOT EXISTS news_events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "ticker TEXT, category TEXT, sentiment REAL, source TEXT, headline TEXT, "
                    "event_ts_utc TEXT, spot_at_event REAL, fwd_return REAL, captured INTEGER)")
    c.executemany("INSERT INTO news_events (ticker,category,sentiment,source,event_ts_utc,fwd_return,captured) "
                  "VALUES (?,?,?,?,?,?,1)",
                  [(ticker, cat, s, "t", "2020-01-01", f) for s, f in pairs])
    c.commit(); c.close()


def test_profile_below_min_n_excluded():
    db = _db()
    _seed_captured(db, "AAA", "stock", [(1.0, 0.02)] * 3)   # n=3 < 5
    assert news_response_profile(db) == []


def test_profile_hit_rate_and_signed_response():
    db = _db()
    # NVDA reacts WITH stock news: bullish→up, bearish→down (4/5 hits)
    _seed_captured(db, "NVDA", "stock",
                   [(1.0, 0.05), (1.0, 0.03), (-1.0, -0.04), (-1.0, -0.02), (1.0, -0.01)])
    prof = news_response_profile(db)
    assert len(prof) == 1
    p = prof[0]
    assert p["ticker"] == "NVDA" and p["n"] == 5
    assert p["hit_rate"] == 0.8                  # 4 of 5 directional events moved with the news
    assert p["mean_signed_response"] > 0         # net moves WITH sentiment
    assert p["responsiveness"] > 0


def test_profile_neutral_events_excluded_from_hit_rate():
    db = _db()
    _seed_captured(db, "KO", "macro", [(0.0, 0.05), (0.0, -0.05)] * 3)   # all neutral
    prof = news_response_profile(db)
    assert len(prof) == 1 and prof[0]["hit_rate"] is None    # no directional events → no hit rate


# ── news_edge_hint (the capture→act classifier, pure) ─────────────────────────────────
from agora.ops.news_events import news_edge_hint


def test_news_edge_hint_classifies():
    assert news_edge_hint(None, 0.05, 10) == "insufficient"        # no directional data
    assert news_edge_hint(0.8, 0.05, 3) == "insufficient"          # below sample
    assert news_edge_hint(0.7, 0.005, 10) == "non_reactive"        # barely moves
    assert news_edge_hint(0.75, 0.04, 10) == "predictable_reactor"  # moves WITH news, hard
    assert news_edge_hint(0.30, 0.05, 10) == "contrarian_or_noisy"  # moves against/unpredictably
    assert news_edge_hint(0.50, 0.02, 10) == "weak"               # reacts, no clean edge


def test_profile_includes_actionable_hint():
    db = _db()
    _seed_captured(db, "NVDA", "stock",
                   [(1.0, 0.05), (1.0, 0.04), (-1.0, -0.04), (-1.0, -0.03), (1.0, 0.03)])  # 5/5 hits, hard
    prof = news_response_profile(db)
    assert prof[0]["hint"] == "predictable_reactor"


def test_error_safe():
    assert record_news_event("/nonexistent/x.db", "X", "stock", 1.0, source="t") is False
    assert news_response_profile("/nonexistent/x.db") == []
    assert recent_events("/nonexistent/x.db") == []
    assert capture_forward_returns("/nonexistent/x.db", fetcher=lambda t, s: [])["status"] in ("ok", "error")

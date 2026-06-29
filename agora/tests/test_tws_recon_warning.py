"""
_tws_recon_warning — surface GENUINELY-missing TWS entries without crying wolf on closed round-trips.
Fix (2026-06-29): a symbol the engine CLOSED TODAY is known to the book — its buy-to-close BOT leg must
not false-flag it as 'missing from shadow book' (the AMZN bear_put_spread false positive).
"""
import sqlite3
import types

from agora.api.routes import _symbols_closed_today, _tws_recon_warning


def _session(tmp_path, open_tickers, closed_today):
    db = str(tmp_path / "r.db")
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE positions (ticker TEXT, status TEXT, close_date TEXT)")
        for t in closed_today:
            c.execute("INSERT INTO positions VALUES (?, 'closed', date('now'))", (t,))
        c.commit()
    pm = types.SimpleNamespace(
        get_open_positions=lambda: [types.SimpleNamespace(ticker=t) for t in open_tickers])
    return types.SimpleNamespace(_position_mgr=pm, _settings=types.SimpleNamespace(db_path=db))


def _live(bot=(), sld=()):
    return {"tws_fills":
            [{"secType": "BAG", "action": "BOT", "symbol": s} for s in bot]
            + [{"secType": "BAG", "action": "SLD", "symbol": s} for s in sld]}


class TestTwsReconWarning:
    def test_closed_today_not_flagged_missing(self, tmp_path):
        # THE FIX: AMZN buy-to-close BOT leg, closed today → NO "missing" warning
        sess = _session(tmp_path, open_tickers=[], closed_today=["AMZN"])
        w = _tws_recon_warning(_live(bot=["AMZN"]), sess)
        assert not any("AMZN" in x and "missing" in x for x in w)

    def test_genuinely_missing_is_still_flagged(self, tmp_path):
        # XYZ BOT-only, not open, not closed-today → genuinely missing → MUST still warn
        sess = _session(tmp_path, open_tickers=[], closed_today=[])
        w = _tws_recon_warning(_live(bot=["XYZ"]), sess)
        assert any("XYZ" in x and "missing" in x for x in w)

    def test_closed_roundtrip_no_missing(self, tmp_path):
        sess = _session(tmp_path, open_tickers=[], closed_today=[])
        w = _tws_recon_warning(_live(bot=["FOO"], sld=["FOO"]), sess)
        assert not any("missing" in x for x in w)

    def test_closed_in_tws_but_open_in_shadow_warns_stale(self, tmp_path):
        sess = _session(tmp_path, open_tickers=["BAR"], closed_today=[])
        w = _tws_recon_warning(_live(bot=["BAR"], sld=["BAR"]), sess)
        assert any("BAR" in x and "stale" in x for x in w)


class TestSymbolsClosedToday:
    def test_returns_closed_today(self, tmp_path):
        sess = _session(tmp_path, [], ["AMZN", "NVDA"])
        assert _symbols_closed_today(sess._settings.db_path) == {"AMZN", "NVDA"}

    def test_bad_path_empty(self):
        assert _symbols_closed_today("/nonexistent/dir/x.db") == set()

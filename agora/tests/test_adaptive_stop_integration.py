"""
agora/tests/test_adaptive_stop_integration.py — the per-ticker adaptive stop wired through the
PositionManager surveillance path (glue layer over the pure engines tested in test_adaptive_stop /
test_position_surveillance).

Proves end-to-end: HV is resolved per ticker from ticker_profiles → an adaptive SurveillanceConfig is
built → the structure stop fires at the PER-TICKER level (a calm name cuts where the old fixed −55%
would still hold) → the verdict is shadow-logged with its adaptive inputs (hv/dte/debit_stop_pct).
Uses a lightweight SimpleNamespace `self` so we exercise the real methods without standing up the full
1,600-line manager.
"""
from __future__ import annotations

import sqlite3
import tempfile
import types
from datetime import date, timedelta

from agora.core.config import get_settings
from agora.lifecycle.position_manager import PositionManager


def _conn():
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE ticker_profiles (ticker TEXT, hv_annual REAL)")
    c.executemany("INSERT INTO ticker_profiles VALUES (?,?)", [("KO", 0.16), ("TSLA", 0.58)])
    c.execute("CREATE TABLE positions (position_id TEXT, peak_unrealized_pnl REAL, "
              "regime_at_entry TEXT DEFAULT 'neutral')")
    c.commit()
    return c


def _pos(ticker, **kw):
    base = dict(position_id=f"{ticker}-1", ticker=ticker, entry_price=10.0, contracts=1,
                unrealized_pnl=0.0, max_loss_dollars=1000.0, max_gain_dollars=1000.0,
                expiry_date=date.today() + timedelta(days=30))
    base.update(kw)
    return types.SimpleNamespace(**base)


def _self(conn, *, adaptive=True):
    s = get_settings().model_copy(update={"adaptive_stop_enabled": adaptive})
    fake = types.SimpleNamespace(_db=conn, _macro_ctx=None, _settings=s)
    # bind the real _ticker_hv method so the surveillance path resolves HV exactly as in production
    fake._ticker_hv = types.MethodType(PositionManager._ticker_hv, fake)
    return fake


# ── _ticker_hv resolver ────────────────────────────────────────────────────────────────
def test_ticker_hv_resolves_and_caches():
    c = _conn()
    fake = types.SimpleNamespace(_db=c)
    assert PositionManager._ticker_hv(fake, "KO") == 0.16
    assert PositionManager._ticker_hv(fake, "tsla") == 0.58       # case-insensitive
    assert PositionManager._ticker_hv(fake, "UNKNOWN") is None    # no profile → None
    assert fake._hv_cache["KO"] == 0.16                           # cached


# ── adaptive structure stop fires at the per-ticker level ───────────────────────────────
def test_calm_ticker_stops_tighter_than_fixed():
    c = _conn()
    fake = _self(c, adaptive=True)
    # KO (HV 0.16) debit down −30% of a $1000 premium → adaptive ~−27% stop fires EXIT
    pos = _pos("KO", unrealized_pnl=-300.0)
    fake._db.execute("INSERT INTO positions (position_id, peak_unrealized_pnl) VALUES ('KO-1', NULL)")
    v = PositionManager._log_surveillance(fake, pos)
    assert v is not None and v.action == "EXIT" and "debit stop" in v.reason
    # logged with adaptive inputs
    row = c.execute("SELECT hv, dte, debit_stop_pct FROM surveillance_log WHERE position_id='KO-1'").fetchone()
    assert row[0] == 0.16 and row[1] == 30 and row[2] < 0.55      # per-ticker, tighter than the old fixed


def test_volatile_ticker_holds_where_calm_would_stop():
    c = _conn()
    fake = _self(c, adaptive=True)
    # TSLA (HV 0.58) at the SAME −30% → wide adaptive stop (~−65%) still HOLDS (needs room)
    pos = _pos("TSLA", unrealized_pnl=-300.0)
    fake._db.execute("INSERT INTO positions (position_id, peak_unrealized_pnl) VALUES ('TSLA-1', NULL)")
    v = PositionManager._log_surveillance(fake, pos)
    assert v is not None and v.action == "HOLD"


def test_disabled_flag_uses_fixed_stop():
    c = _conn()
    fake = _self(c, adaptive=False)
    # adaptive OFF → KO uses the fixed −55%, so −30% is NOT a stop → HOLD
    pos = _pos("KO", unrealized_pnl=-300.0)
    fake._db.execute("INSERT INTO positions (position_id, peak_unrealized_pnl) VALUES ('KO-1', NULL)")
    v = PositionManager._log_surveillance(fake, pos)
    assert v is not None and v.action == "HOLD"


def test_blowout_still_fires_regardless_of_adaptive():
    c = _conn()
    fake = _self(c, adaptive=True)
    pos = _pos("TSLA", unrealized_pnl=-900.0)             # 90% of max loss → blowout backstop
    fake._db.execute("INSERT INTO positions (position_id, peak_unrealized_pnl) VALUES ('TSLA-1', NULL)")
    v = PositionManager._log_surveillance(fake, pos)
    assert v is not None and v.action == "EXIT" and v.is_backstop is True

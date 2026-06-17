"""Keystone close-booking tests — the exact DB-write behavior behind the +$2,693 -> -$3,248.50
restatement. Binds the REAL PositionManager._close_position to a stub with a temp sqlite DB
(never touches .agora/agora.db). Verifies: (1) a real broker fill is NOT overwritten by the
in-memory mark; (2) a FAILED close keeps the position OPEN; (3) the pure-sim path books the mark.
"""
import sqlite3
import types

import pytest

import agora.lifecycle.position_manager as pm
from agora.lifecycle.position_manager import PositionManager


def _stub(tmp_path, on_close, prewrite=None):
    db = sqlite3.connect(str(tmp_path / "t.db"))
    db.execute(
        "CREATE TABLE positions (position_id TEXT, status TEXT, close_date TEXT, "
        "close_price REAL, close_source TEXT, realized_pnl REAL, last_reviewed TEXT)"
    )
    db.execute(
        "INSERT INTO positions (position_id, status, realized_pnl, close_price) VALUES (?,?,?,?)",
        ("p1", "open", prewrite[0] if prewrite else None, prewrite[1] if prewrite else None),
    )
    db.commit()
    recorded = {"trade_records": []}
    stub = types.SimpleNamespace(
        _on_close=on_close,
        _db=db,
        _settings=types.SimpleNamespace(db_path=str(tmp_path / "t.db")),
    )
    stub._write_trade_record = lambda position, reason, realized_pnl=None, close_price=None: (
        recorded["trade_records"].append(
            {"reason": reason, "realized_pnl": realized_pnl, "close_price": close_price})
    )
    return stub, db, recorded


@pytest.fixture(autouse=True)
def _no_decision_chain(monkeypatch):
    monkeypatch.setattr(pm, "_decision_chain_close", lambda *a, **k: None)


@pytest.mark.asyncio
async def test_real_fill_pnl_is_not_overwritten_by_mark(tmp_path):
    """KEYSTONE: _execute_close already booked the real fill (-235 / 6.20). _close_position
    must NOT overwrite it with the +385 unrealized mark — only re-stamp close_source."""
    async def on_close(pos, reason):
        return True
    stub, db, rec = _stub(tmp_path, on_close, prewrite=(-235.0, 6.20))
    pos = types.SimpleNamespace(position_id="p1", ticker="MKSI",
                                unrealized_pnl=385.0, current_price=0.0, contracts=1)

    await PositionManager._close_position(stub, pos, "thesis_exit", source="thesis_exit")

    row = db.execute(
        "SELECT realized_pnl, close_price, close_source FROM positions WHERE position_id='p1'"
    ).fetchone()
    assert row[0] == -235.0          # real fill P&L preserved, NOT the +385 mark
    assert row[1] == 6.20            # real close price preserved
    assert row[2] == "thesis_exit"   # source re-stamped
    assert rec["trade_records"][0]["realized_pnl"] == -235.0   # downstream got the real number


@pytest.mark.asyncio
async def test_failed_close_keeps_position_open(tmp_path):
    """C3 guard: a failed broker close (callback returns False) must leave the position OPEN
    and write NO trade record — never strand it 'closed' but live at the broker."""
    async def on_close(pos, reason):
        return False
    stub, db, rec = _stub(tmp_path, on_close)
    pos = types.SimpleNamespace(position_id="p1", ticker="X",
                                unrealized_pnl=-50.0, current_price=1.0, contracts=1)

    await PositionManager._close_position(stub, pos, "stop_loss", source="stop_loss")

    status = db.execute("SELECT status FROM positions WHERE position_id='p1'").fetchone()[0]
    assert status == "open"            # NOT flipped to closed
    assert rec["trade_records"] == []  # no trade record on a failed close


@pytest.mark.asyncio
async def test_raised_close_keeps_position_open(tmp_path):
    """A close callback that RAISES is treated as a failed close — position stays OPEN."""
    async def on_close(pos, reason):
        raise RuntimeError("broker rejected")
    stub, db, rec = _stub(tmp_path, on_close)
    pos = types.SimpleNamespace(position_id="p1", ticker="X",
                                unrealized_pnl=-50.0, current_price=1.0, contracts=1)

    await PositionManager._close_position(stub, pos, "stop_loss", source="stop_loss")

    assert db.execute("SELECT status FROM positions WHERE position_id='p1'").fetchone()[0] == "open"
    assert rec["trade_records"] == []


@pytest.mark.asyncio
async def test_pure_sim_path_books_the_mark(tmp_path):
    """With no real-close callback wired (pure paper sim), the unrealized mark IS the realized
    result by design — books status=closed, realized=mark, close_price=current."""
    stub, db, rec = _stub(tmp_path, None)
    pos = types.SimpleNamespace(position_id="p1", ticker="X",
                                unrealized_pnl=120.0, current_price=0.55, contracts=1,
                                direction="bullish")

    await PositionManager._close_position(stub, pos, "lifecycle", source="lifecycle")

    row = db.execute(
        "SELECT status, realized_pnl, close_price FROM positions WHERE position_id='p1'"
    ).fetchone()
    assert row[0] == "closed"
    assert row[1] == 120.0
    assert row[2] == 0.55
    assert len(rec["trade_records"]) == 1

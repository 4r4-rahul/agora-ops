"""
agora/tests/test_position_reconciler.py — leg-level DB↔IBKR reconciliation (position TRUTH).

This is the only check that can catch a position existing at the broker but not in our book (or
vice-versa). A bug here means we either trade against phantom risk or leave orphan broker positions
unmanaged. We mock both sides — a temp `positions` DB and a fake `ib` object — and pin the signed
aggregation + matched/orphan/ghost/qty_mismatch classification exactly.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import types

from agora.ops.position_reconciler import (
    LIVE_STATUSES,
    _norm_expiry,
    db_legs,
    diff,
    format_report,
    ibkr_legs,
)


# ── helpers ───────────────────────────────────────────────────────────────────
def _db(rows: list[tuple]) -> str:
    """rows: (ticker, status, legs_json)."""
    p = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    with sqlite3.connect(p) as c:
        c.execute("CREATE TABLE positions (ticker TEXT, status TEXT, legs_json TEXT)")
        c.executemany("INSERT INTO positions VALUES (?,?,?)", rows)
    return p


def _leg(action="sell", option_type="put", strike=100.0, expiration="2026-08-21", contracts=1):
    return {"action": action, "option_type": option_type, "strike": strike,
            "expiration": expiration, "contracts": contracts}


def _ib(positions: list):
    return types.SimpleNamespace(positions=lambda: positions)


def _ib_pos(symbol, right, strike, expiry, qty, sec_type="OPT"):
    contract = types.SimpleNamespace(
        symbol=symbol, right=right, strike=strike,
        lastTradeDateOrContractMonth=expiry, secType=sec_type,
    )
    return types.SimpleNamespace(contract=contract, position=qty)


# ── _norm_expiry ──────────────────────────────────────────────────────────────
class TestNormExpiry:
    def test_strips_dashes(self):
        assert _norm_expiry("2026-08-21") == "20260821"

    def test_already_compact(self):
        assert _norm_expiry("20260821") == "20260821"

    def test_empty_and_none(self):
        assert _norm_expiry("") == ""
        assert _norm_expiry(None) == ""


# ── db_legs ───────────────────────────────────────────────────────────────────
class TestDbLegs:
    def test_sell_is_negative_buy_is_positive(self):
        db = _db([
            ("AAPL", "open", json.dumps([_leg(action="sell", strike=100, contracts=2)])),
            ("MSFT", "open", json.dumps([_leg(action="buy", strike=200, contracts=3)])),
        ])
        legs = db_legs(db)
        assert legs[("AAPL", "P", 100.0, "20260821")] == -2
        assert legs[("MSFT", "P", 200.0, "20260821")] == +3

    def test_aggregates_same_leg_across_positions(self):
        db = _db([
            ("AAPL", "open", json.dumps([_leg(action="sell", strike=100, contracts=2)])),
            ("AAPL", "tested", json.dumps([_leg(action="sell", strike=100, contracts=1)])),
        ])
        assert db_legs(db)[("AAPL", "P", 100.0, "20260821")] == -3

    def test_net_zero_legs_dropped(self):
        db = _db([
            ("AAPL", "open", json.dumps([
                _leg(action="sell", strike=100, contracts=2),
                _leg(action="buy", strike=100, contracts=2),
            ])),
        ])
        assert ("AAPL", "P", 100.0, "20260821") not in db_legs(db)

    def test_only_live_statuses_counted(self):
        live = json.dumps([_leg(strike=100)])
        dead = json.dumps([_leg(strike=999)])
        db = _db([("AAPL", "open", live), ("AAPL", "closed", dead)])
        legs = db_legs(db)
        assert ("AAPL", "P", 100.0, "20260821") in legs
        assert ("AAPL", "P", 999.0, "20260821") not in legs

    def test_all_live_statuses_recognized(self):
        rows = [(f"T{i}", st, json.dumps([_leg(strike=100 + i)]))
                for i, st in enumerate(LIVE_STATUSES)]
        legs = db_legs(_db(rows))
        assert len(legs) == len(LIVE_STATUSES)

    def test_call_vs_put_right(self):
        db = _db([("AAPL", "open", json.dumps([
            _leg(option_type="call", strike=110), _leg(option_type="put", strike=90),
        ]))])
        legs = db_legs(db)
        assert ("AAPL", "C", 110.0, "20260821") in legs
        assert ("AAPL", "P", 90.0, "20260821") in legs

    def test_bad_legs_json_skipped(self):
        db = _db([("AAPL", "open", "{not valid json"), ("MSFT", "open", json.dumps([_leg()]))])
        legs = db_legs(db)
        assert len(legs) == 1   # only MSFT survives

    def test_expiry_normalised_from_db_format(self):
        db = _db([("AAPL", "open", json.dumps([_leg(expiration="2026-12-18")]))])
        assert ("AAPL", "P", 100.0, "20261218") in db_legs(db)


# ── ibkr_legs ─────────────────────────────────────────────────────────────────
class TestIbkrLegs:
    def test_signed_quantities(self):
        ib = _ib([
            _ib_pos("AAPL", "P", 100.0, "20260821", -2),
            _ib_pos("MSFT", "C", 200.0, "20260821", +3),
        ])
        legs = ibkr_legs(ib)
        assert legs[("AAPL", "P", 100.0, "20260821")] == -2
        assert legs[("MSFT", "C", 200.0, "20260821")] == +3

    def test_non_option_positions_skipped(self):
        ib = _ib([
            _ib_pos("SPY", "", 0.0, "", 100, sec_type="STK"),   # a stock — must be ignored
            _ib_pos("AAPL", "P", 100.0, "20260821", -1),
        ])
        legs = ibkr_legs(ib)
        assert len(legs) == 1 and ("AAPL", "P", 100.0, "20260821") in legs

    def test_net_zero_dropped(self):
        ib = _ib([
            _ib_pos("AAPL", "P", 100.0, "20260821", +2),
            _ib_pos("AAPL", "P", 100.0, "20260821", -2),
        ])
        assert ibkr_legs(ib) == {}

    def test_compact_expiry_passthrough(self):
        ib = _ib([_ib_pos("AAPL", "P", 100.0, "20260821", -1)])
        assert ("AAPL", "P", 100.0, "20260821") in ibkr_legs(ib)


# ── diff (the core classifier) ────────────────────────────────────────────────
K1 = ("AAPL", "P", 100.0, "20260821")
K2 = ("MSFT", "C", 200.0, "20260821")
K3 = ("TSLA", "P", 300.0, "20260821")


class TestDiff:
    def test_matched_when_equal(self):
        rep = diff({K1: -2}, {K1: -2})
        assert rep.clean
        assert len(rep.matched) == 1 and not rep.orphans and not rep.ghosts

    def test_orphan_when_only_at_ibkr(self):
        rep = diff({}, {K1: -1})
        assert not rep.clean
        assert rep.orphans[0]["symbol"] == "AAPL"
        assert rep.orphans[0]["db_qty"] == 0 and rep.orphans[0]["ibkr_qty"] == -1

    def test_ghost_when_only_in_db(self):
        rep = diff({K1: -1}, {})
        assert rep.ghosts[0]["symbol"] == "AAPL"
        assert rep.ghosts[0]["db_qty"] == -1 and rep.ghosts[0]["ibkr_qty"] == 0

    def test_qty_mismatch_when_both_differ(self):
        rep = diff({K1: -2}, {K1: -1})   # partial fill
        assert rep.qty_mismatch[0]["db_qty"] == -2
        assert rep.qty_mismatch[0]["ibkr_qty"] == -1
        assert not rep.clean

    def test_mixed_book_classified_correctly(self):
        rep = diff({K1: -2, K2: +1}, {K1: -2, K3: -1})
        # K1 matches, K2 ghost (db only), K3 orphan (ibkr only)
        assert len(rep.matched) == 1
        assert {g["symbol"] for g in rep.ghosts} == {"MSFT"}
        assert {o["symbol"] for o in rep.orphans} == {"TSLA"}
        assert not rep.clean

    def test_sign_flip_is_mismatch_not_match(self):
        # long vs short same strike — a real divergence, must NOT be 'matched'
        rep = diff({K1: +1}, {K1: -1})
        assert len(rep.qty_mismatch) == 1 and not rep.matched

    def test_to_dict_counts(self):
        # K1 match, K2 ghost (db only), K3 qty_mismatch (-2 vs -1), K4 orphan (ibkr only)
        K4 = ("NVDA", "C", 400.0, "20260821")
        rep = diff({K1: -2, K2: +1, K3: -2}, {K1: -2, K3: -1, K4: -1}, account="DU123")
        d = rep.to_dict()
        assert d["account"] == "DU123"
        assert d["clean"] is False
        assert d["counts"] == {"matched": 1, "orphans": 1, "ghosts": 1, "qty_mismatch": 1}

    def test_empty_both_is_clean(self):
        rep = diff({}, {})
        assert rep.clean and rep.to_dict()["counts"]["matched"] == 0


# ── format_report ─────────────────────────────────────────────────────────────
class TestFormatReport:
    def test_clean_marker(self):
        out = format_report(diff({K1: -1}, {K1: -1}, account="DU9"))
        assert "CLEAN" in out and "DU9" in out

    def test_divergence_lists_each_class(self):
        out = format_report(diff({K2: +1}, {K3: -1}))
        assert "DIVERGENCE" in out
        assert "ORPHANS" in out and "TSLA" in out
        assert "GHOSTS" in out and "MSFT" in out


# ── end-to-end: DB + IBKR through to a report ─────────────────────────────────
def test_end_to_end_db_vs_ibkr_partial_fill():
    # DB thinks we sold 2 contracts; broker only filled 1 → qty_mismatch surfaced.
    db = _db([("AAPL", "open", json.dumps([_leg(action="sell", strike=100, contracts=2)]))])
    ib = _ib([_ib_pos("AAPL", "P", 100.0, "20260821", -1)])
    rep = diff(db_legs(db), ibkr_legs(ib), account="DU1")
    assert not rep.clean
    assert rep.qty_mismatch[0]["db_qty"] == -2
    assert rep.qty_mismatch[0]["ibkr_qty"] == -1


# ── adopted-position SIZE-SANITY ALERT (2026-06-25) ────────────────────────────
# The contract "leak" (DIA 59, NOK 12 = 44% of AUM) was NOT engine sizing — it was the reconciler
# adopting large LEGACY broker positions silently. These assert the oversized-adoption alert fires
# distinctly while still tracking the position (untracked is worse than flagged).
def _do_adopt(qty: int, sym: str = "DIA", cap: int = 10):
    import types as _t

    from agora.core.models import (
        OpenPosition,
        PositionStatus,
        SpreadLeg,
        StrategyPillar,
        StrategyType,
    )
    from agora.ops.position_reconciler import _adopt_group
    adopted = []
    pm = _t.SimpleNamespace(add_position=lambda pos: adopted.append(pos),
                            _settings=_t.SimpleNamespace(max_contracts_per_trade=cap))
    legs = [{"right": "C", "strike": 440.0, "ibkr_qty": qty}]
    detailed = {(sym, "C", 440.0, "20260724"): (qty, 250.0)}
    ok = _adopt_group(pm, sym, "20260724", legs, detailed,
                      OpenPosition, SpreadLeg, StrategyType, StrategyPillar, PositionStatus)
    return ok, adopted


class TestAdoptOversizedAlert:
    def test_oversized_adoption_is_flagged(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING):
            ok, _ = _do_adopt(59)
        assert ok is True
        assert "ADOPTED OVERSIZED" in caplog.text and "59" in caplog.text

    def test_normal_adoption_not_flagged(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING):
            ok, _ = _do_adopt(3)
        assert ok is True
        assert "ADOPTED OVERSIZED" not in caplog.text   # routine size → no anomaly alert

    def test_still_adopts_at_real_size_when_oversized(self, caplog):
        # observability ONLY — the oversized position must still be tracked, at its real broker size
        _ok, adopted = _do_adopt(40)
        assert len(adopted) == 1 and adopted[0].contracts == 40

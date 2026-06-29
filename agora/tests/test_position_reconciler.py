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
    ReconcileReport,
    _norm_expiry,
    db_legs,
    diff,
    format_report,
    ibkr_legs,
    plan_overfill_flatten,
)


# ── helpers ───────────────────────────────────────────────────────────────────
def _db(rows: list[tuple]) -> str:
    """rows: (ticker, status, legs_json). The position-level `contracts` is the authoritative
    absolute size (what the broker order was placed with); db_legs() reads it now, so we derive it
    from the legs (max leg count = the absolute count for these single-ratio test positions)."""
    p = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    with sqlite3.connect(p) as c:
        c.execute("CREATE TABLE positions (ticker TEXT, status TEXT, legs_json TEXT, "
                  "contracts INTEGER DEFAULT 1, regime_at_entry TEXT DEFAULT 'neutral')")
        enriched = []
        for ticker, status, legs_json in rows:
            try:
                counts = [int(l.get("contracts", 1) or 1) for l in json.loads(legs_json or "[]")]
            except Exception:
                counts = []
            enriched.append((ticker, status, legs_json, max(counts) if counts else 1))
        c.executemany(
            "INSERT INTO positions (ticker, status, legs_json, contracts) VALUES (?,?,?,?)",
            enriched,
        )
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


# ── over-fill flatten planner (2026-06-26 DIA 631-vs-59 runaway) ────────────────
def _mm(symbol, db_qty, ibkr_qty, right="P", strike=505.0, expiry="20260717"):
    return {"symbol": symbol, "right": right, "strike": strike, "expiry": expiry,
            "db_qty": db_qty, "ibkr_qty": ibkr_qty}


class TestOverfillPlanner:
    def test_flags_the_dia_runaway_signature(self):
        rep = ReconcileReport(qty_mismatch=[_mm("DIA", -59, 631)])
        plan = plan_overfill_flatten(rep, min_excess=25)
        assert len(plan) == 1
        p = plan[0]
        # broker is +631, book wants -59 → SELL 690 to bring broker to book
        assert p["action"] == "SELL" and p["flatten_qty"] == 690 and p["target_qty"] == -59

    def test_buy_side_overfill(self):
        rep = ReconcileReport(qty_mismatch=[_mm("DIA", 59, -631)])
        p = plan_overfill_flatten(rep, min_excess=25)[0]
        assert p["action"] == "BUY" and p["flatten_qty"] == 690

    def test_ignores_routine_partial_fill(self):
        # db -1 vs ibkr -3 is a normal partial, NOT a runaway — must not be flagged.
        rep = ReconcileReport(qty_mismatch=[_mm("IWM", -1, -3, right="C", strike=307.0)])
        assert plan_overfill_flatten(rep, min_excess=25) == []

    def test_threshold_respected(self):
        # excess of exactly min_excess with ≥2x magnitude is flagged; just under is not.
        assert plan_overfill_flatten(ReconcileReport(qty_mismatch=[_mm("X", 10, 35)]), 25)  # excess 25
        assert plan_overfill_flatten(ReconcileReport(qty_mismatch=[_mm("X", 10, 34)]), 25) == []


class TestFlattenExecutor:
    """The flatten executor must (a) place the right single-leg order, (b) NOT stack when one is
    already working, (c) refuse an insane size. It runs on the heal()-connected sync ib."""

    class _FakeOrder:
        def __init__(self, ref, status):
            self.order = types.SimpleNamespace(orderRef=ref, orderId=1)
            self.orderStatus = types.SimpleNamespace(status=status)

    class _FakeIB:
        def __init__(self, working=()):
            self._working = list(working)
            self.placed = []
        def reqAllOpenOrders(self): pass
        def sleep(self, _s): pass
        def openTrades(self): return self._working
        def qualifyContracts(self, opt): return [opt]
        def placeOrder(self, contract, order): self.placed.append((contract, order)); return order

    def _plan(self):
        return [{"symbol": "DIA", "right": "P", "strike": 505.0, "expiry": "20260717",
                 "db_qty": -59, "ibkr_qty": 631, "action": "SELL",
                 "flatten_qty": 690, "target_qty": -59}]

    def test_places_the_flatten_order(self):
        from agora.ops.position_reconciler import _flatten_overfill
        ib = self._FakeIB()
        out = _flatten_overfill(ib, self._plan(), max_flatten=5000)
        assert out["placed"] == 1 and len(ib.placed) == 1
        _, order = ib.placed[0]
        assert order.action == "SELL" and order.totalQuantity == 690
        assert order.orderRef.startswith("FLATTEN_DIA")

    def test_does_not_stack_when_already_working(self):
        from agora.ops.position_reconciler import _flatten_overfill
        ref = "FLATTEN_DIAP505_20260717"
        ib = self._FakeIB(working=[self._FakeOrder(ref, "Submitted")])
        out = _flatten_overfill(ib, self._plan(), max_flatten=5000)
        assert out["placed"] == 0 and out["skipped"] == 1 and ib.placed == []

    def test_refuses_insane_size(self):
        from agora.ops.position_reconciler import _flatten_overfill
        ib = self._FakeIB()
        out = _flatten_overfill(ib, self._plan(), max_flatten=100)  # 690 > 100
        assert out["placed"] == 0 and out["refused"] == 1 and ib.placed == []


class TestOverfillFromOrphans:
    """The 10:29 restart ghost-closed the adopted DIA row while its 659-contract broker legs
    persisted — so the over-fill surfaced as an ORPHAN, not a qty_mismatch. The planner must catch
    orphan over-fills too (book=0 → flatten the whole broker leg), or the runaway escapes cleanup."""

    def test_massive_orphan_is_flagged_for_flatten(self):
        rep = ReconcileReport(orphans=[
            {"symbol": "DIA", "right": "P", "strike": 505.0, "expiry": "20260717",
             "db_qty": 0, "ibkr_qty": 659},
        ])
        plan = plan_overfill_flatten(rep, min_excess=25)
        assert len(plan) == 1
        p = plan[0]
        assert p["action"] == "SELL" and p["flatten_qty"] == 659 and p["target_qty"] == 0

    def test_normal_small_orphan_not_flagged(self):
        # a routine 5-contract adopted orphan must NOT be flattened — it gets adopted normally.
        rep = ReconcileReport(orphans=[
            {"symbol": "SPY", "right": "C", "strike": 600.0, "expiry": "20260731",
             "db_qty": 0, "ibkr_qty": 5},
        ])
        assert plan_overfill_flatten(rep, min_excess=25) == []


class TestLegRatioVsAbsolute:
    """Regression for the 2026-06-26 phantom mismatch: legs_json stores the per-leg count
    inconsistently (AMD long_put = absolute 8; IWM/SCHW 3-lot vertical = ratio 1). db_legs must
    use position.contracts as the authoritative size so the reconciler agrees with the broker."""

    def _db_with_contracts(self, ticker, contracts, legs):
        import sqlite3
        import tempfile
        p = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        with sqlite3.connect(p) as c:
            c.execute("CREATE TABLE positions (ticker TEXT, status TEXT, legs_json TEXT, "
                      "contracts INTEGER, regime_at_entry TEXT DEFAULT 'neutral')")
            c.execute("INSERT INTO positions (ticker, status, legs_json, contracts) VALUES (?,?,?,?)",
                      (ticker, "open", json.dumps(legs), contracts))
        return p

    def test_ratio_convention_uses_position_contracts(self):
        # IWM 3-lot vertical, legs stored as ratio 1 → must aggregate to ±3 (broker truth).
        legs = [_leg(action="sell", option_type="call", strike=307, contracts=1),
                _leg(action="buy", option_type="call", strike=309, contracts=1)]
        db = self._db_with_contracts("IWM", 3, legs)
        out = db_legs(db)
        assert out[("IWM", "C", 307.0, "20260821")] == -3
        assert out[("IWM", "C", 309.0, "20260821")] == +3

    def test_absolute_convention_not_double_counted(self):
        # AMD long_put, legs stored as absolute 8, position.contracts 8 → ±8, NOT 8*8.
        legs = [_leg(action="buy", option_type="put", strike=200, contracts=8)]
        db = self._db_with_contracts("AMD", 8, legs)
        assert db_legs(db)[("AMD", "P", 200.0, "20260821")] == +8

    def test_genuine_ratio_spread_preserved(self):
        # 1x2 ratio, 4 spreads: legs stored as ratio (1,2) → ±4 and ±8.
        legs = [_leg(action="buy", option_type="call", strike=100, contracts=1),
                _leg(action="sell", option_type="call", strike=110, contracts=2)]
        db = self._db_with_contracts("XYZ", 4, legs)
        out = db_legs(db)
        assert out[("XYZ", "C", 100.0, "20260821")] == +4
        assert out[("XYZ", "C", 110.0, "20260821")] == -8


# ── ghost-close stability guard (the 2026-06-26 false-close root) ───────────────
class TestSafeToGhostClose:
    K = ("DIA", "P", 505.0, "20260717")

    def test_unstable_snapshots_never_close(self):
        from agora.ops.position_reconciler import safe_to_ghost_close
        # leg absent in snap_a but present in snap_b → stream still arriving → must NOT close
        assert safe_to_ghost_close({}, {self.K: -59}, [self.K]) is False

    def test_stable_and_absent_in_both_closes(self):
        from agora.ops.position_reconciler import safe_to_ghost_close
        assert safe_to_ghost_close({}, {}, [self.K]) is True

    def test_stable_but_present_does_not_close(self):
        from agora.ops.position_reconciler import safe_to_ghost_close
        assert safe_to_ghost_close({self.K: -59}, {self.K: -59}, [self.K]) is False


class _FakeContract:
    def __init__(self, symbol, right, strike, expiry):
        self.symbol, self.right, self.strike = symbol, right, strike
        self.lastTradeDateOrContractMonth, self.secType = expiry, "OPT"


class _FakeBrokerPos:
    def __init__(self, symbol, right, strike, expiry, qty):
        self.contract = _FakeContract(symbol, right, strike, expiry)
        self.position, self.avgCost = qty, 1.0


class _StreamingIB:
    """Returns successive position() snapshots to simulate a large book still streaming."""
    def __init__(self, snapshots):
        self._snaps, self._i = snapshots, 0
    def connect(self, *a, **k): pass
    def reqPositions(self): pass
    def sleep(self, _s): pass
    def positions(self):
        snap = self._snaps[min(self._i, len(self._snaps) - 1)]
        self._i += 1
        return snap
    def disconnect(self): pass


def _dia_position():
    leg = types.SimpleNamespace(option_type="put", strike=505.0,
                                expiration=types.SimpleNamespace(isoformat=lambda: "2026-07-17"))
    return types.SimpleNamespace(position_id="adopt-dia", ticker="DIA", legs=[leg])


def _mgr_with(pos):
    closed = []
    mgr = types.SimpleNamespace(
        get_open_positions=lambda: [pos],
        mark_position_closed=lambda **kw: closed.append(kw),
    )
    return mgr, closed


class TestHealGhostCloseStability:
    def test_does_not_ghost_close_while_streaming(self, monkeypatch):
        from agora.ops import position_reconciler as pr
        dia = _FakeBrokerPos("DIA", "P", 505.0, "20260717", -59)
        # snap1: DIA not yet streamed (absent); snap2 & detailed: DIA present
        ib = _StreamingIB([[], [dia], [dia]])
        monkeypatch.setattr("ib_insync.IB", lambda: ib)
        mgr, closed = _mgr_with(_dia_position())
        db = _db([("DIA", "open", json.dumps([_leg(option_type="put", strike=505,
                                                    expiration="2026-07-17", contracts=59)]))])
        out = pr.heal(db, mgr, overfill_flatten_enabled=False)
        assert out.get("ghost_close_deferred") is True
        assert out["ghosts_closed"] == 0
        assert closed == [], "must NOT ghost-close a position whose legs are mid-stream"

    def test_ghost_closes_when_genuinely_absent_and_stable(self, monkeypatch):
        from agora.ops import position_reconciler as pr
        # all three snapshots agree the broker is flat → the DB position is a true ghost
        ib = _StreamingIB([[], [], []])
        monkeypatch.setattr("ib_insync.IB", lambda: ib)
        mgr, closed = _mgr_with(_dia_position())
        db = _db([("DIA", "open", json.dumps([_leg(option_type="put", strike=505,
                                                    expiration="2026-07-17", contracts=59)]))])
        out = pr.heal(db, mgr, overfill_flatten_enabled=False)
        assert out.get("ghost_close_deferred") is not True
        assert out["ghosts_closed"] == 1
        assert len(closed) == 1 and closed[0]["position_id"] == "adopt-dia"


# ── settled under-fill: correct the BOOK down to the broker (TSLA 2-vs-1) ────────
class TestUnderfillBookCorrection:
    """broker holds FEWER than the book (a settled under-fill). The book is corrected DOWN to the
    broker — but ONLY when stable AND aged past fill latency, never mid-fill."""

    def _aged_pos(self, contracts, broker_present=True):
        leg = types.SimpleNamespace(option_type="call", strike=435.0,
                                    expiration=types.SimpleNamespace(isoformat=lambda: "2026-07-24"))
        leg2 = types.SimpleNamespace(option_type="call", strike=445.0,
                                     expiration=types.SimpleNamespace(isoformat=lambda: "2026-07-24"))
        return types.SimpleNamespace(position_id="tsla-1", ticker="TSLA", contracts=contracts,
                                     legs=[leg, leg2], entry_ts_utc="2026-06-25T12:00:00+00:00")

    def _run(self, monkeypatch, broker_qty, age_ts, min_age=20.0):
        from agora.ops import position_reconciler as pr
        # broker holds `broker_qty` of each TSLA leg, stable across snapshots
        legs = [_FakeBrokerPos("TSLA", "C", 435.0, "20260724", -broker_qty),
                _FakeBrokerPos("TSLA", "C", 445.0, "20260724", broker_qty)] if broker_qty else []
        ib = _StreamingIB([legs, legs, legs])
        monkeypatch.setattr("ib_insync.IB", lambda: ib)
        resized = {}
        pos = self._aged_pos(2)
        pos.entry_ts_utc = age_ts
        mgr = types.SimpleNamespace(
            get_open_positions=lambda: [pos],
            mark_position_closed=lambda **kw: None,
            reconcile_contracts=lambda position_id, broker_contracts, reason: resized.update(
                {"id": position_id, "to": broker_contracts}) or True,
        )
        db = _db([("TSLA", "open", json.dumps([
            {"option_type": "call", "strike": 435.0, "expiration": "2026-07-24",
             "action": "sell", "contracts": 1},
            {"option_type": "call", "strike": 445.0, "expiration": "2026-07-24",
             "action": "buy", "contracts": 1}]))])
        out = pr.heal(db, mgr, overfill_flatten_enabled=False, underfill_min_age_min=min_age)
        return out, resized

    def test_aged_underfill_resizes_book_down(self, monkeypatch):
        out, resized = self._run(monkeypatch, broker_qty=1, age_ts="2026-06-25T12:00:00+00:00")
        assert resized.get("to") == 1, "aged 2-vs-1 under-fill must resize book to broker (1)"
        assert out.get("book_resized") == 1

    def test_fresh_underfill_is_left_alone(self, monkeypatch):
        # a position entered 'now' (huge min_age) must NOT be corrected — it may still be filling
        out, resized = self._run(monkeypatch, broker_qty=1,
                                 age_ts="2026-06-25T12:00:00+00:00", min_age=10_000_000)
        assert resized == {}, "a still-settling fill must never be corrected mid-flight"
        assert out.get("book_resized") in (None, 0)


class TestReconcileContractsPrimitive:
    def test_resizes_down_and_scales_dollars(self, tmp_path):
        import sqlite3 as _sq

        from agora.lifecycle.position_manager import PositionManager
        db = _sq.connect(str(tmp_path / "t.db"))
        db.execute("CREATE TABLE positions (position_id TEXT, contracts INTEGER, "
                   "max_loss_dollars REAL, max_gain_dollars REAL, ticker TEXT, last_reviewed TEXT)")
        db.execute("INSERT INTO positions VALUES ('p1', 2, 400.0, 600.0, 'TSLA', '')")
        db.commit()
        stub = types.SimpleNamespace(_db=db)
        ok = PositionManager.reconcile_contracts(stub, "p1", 1, "underfill_book_to_broker")
        row = db.execute("SELECT contracts, max_loss_dollars, max_gain_dollars FROM positions").fetchone()
        assert ok and row == (1, 200.0, 300.0), "book resized 2→1, dollars halved, no P&L"

    def test_never_increases(self, tmp_path):
        import sqlite3 as _sq

        from agora.lifecycle.position_manager import PositionManager
        db = _sq.connect(str(tmp_path / "t.db"))
        db.execute("CREATE TABLE positions (position_id TEXT, contracts INTEGER, "
                   "max_loss_dollars REAL, max_gain_dollars REAL, ticker TEXT, last_reviewed TEXT)")
        db.execute("INSERT INTO positions VALUES ('p1', 1, 200.0, 300.0, 'TSLA', '')")
        db.commit()
        stub = types.SimpleNamespace(_db=db)
        assert PositionManager.reconcile_contracts(stub, "p1", 3, "x") is False  # never increase


# ── ORPHAN-ADOPTION IN-FLIGHT RACE GUARD (2026-06-29) ────────────────────────────
# During leg-by-leg spread entry the long leg fills SECONDS before the short, and the spread's DB row
# is written only AFTER both fill. In that window the lone long leg looks like an orphan; adopting it
# double-booked the contract (JPM 340C / NVDA 205C: db_qty=2 vs broker=1). heal() now defers adoption
# for any ticker that still has a WORKING AGORA entry order (the in-flight short leg).
class _IBWithOrders:
    """Stable broker snapshot + a reqAllOpenOrders() feed (entry orders live on other clientIds)."""
    def __init__(self, broker_positions, open_orders):
        self._pos, self._orders = broker_positions, open_orders
    def connect(self, *a, **k): pass
    def reqPositions(self): pass
    def sleep(self, _s): pass
    def positions(self): return self._pos
    def reqAllOpenOrders(self): return self._orders
    def disconnect(self): pass


def _working_order(symbol, status="Submitted", ref="AGORA-1119-L1"):
    return types.SimpleNamespace(
        order=types.SimpleNamespace(orderRef=ref),
        orderStatus=types.SimpleNamespace(status=status),
        contract=types.SimpleNamespace(symbol=symbol),
    )


def _adopt_mgr():
    adopted = []
    mgr = types.SimpleNamespace(
        get_open_positions=lambda: [],
        add_position=lambda pos: adopted.append(pos),
        _settings=types.SimpleNamespace(max_contracts_per_trade=10),
    )
    return mgr, adopted


class TestOrphanInflightRaceGuard:
    def _broker_long_leg(self):
        # only the long leg of a JPM bull_call_spread has filled at the broker so far
        return [_FakeBrokerPos("JPM", "C", 340.0, "20260724", 1)]

    def test_inflight_spread_long_leg_not_adopted(self, monkeypatch):
        """The race: long leg filled, short leg STILL WORKING, spread row not written yet → defer."""
        from agora.ops import position_reconciler as pr
        ib = _IBWithOrders(self._broker_long_leg(), [_working_order("JPM")])
        monkeypatch.setattr("ib_insync.IB", lambda: ib)
        mgr, adopted = _adopt_mgr()
        out = pr.heal(_db([]), mgr, overfill_flatten_enabled=False)
        assert out["orphans_adopted"] == 0
        assert out.get("orphan_adopt_deferred") == 1
        assert adopted == [], "must NOT double-book a leg whose spread is mid-completion"

    def test_genuine_orphan_adopted_when_no_working_order(self, monkeypatch):
        """No working AGORA order → a real untracked broker leg must still be adopted."""
        from agora.ops import position_reconciler as pr
        ib = _IBWithOrders(self._broker_long_leg(), [])
        monkeypatch.setattr("ib_insync.IB", lambda: ib)
        mgr, adopted = _adopt_mgr()
        out = pr.heal(_db([]), mgr, overfill_flatten_enabled=False)
        assert out["orphans_adopted"] == 1
        assert len(adopted) == 1

    def test_guard_is_ticker_scoped_not_global(self, monkeypatch):
        """A working order on NVDA must not block adopting a genuine JPM orphan."""
        from agora.ops import position_reconciler as pr
        ib = _IBWithOrders(self._broker_long_leg(), [_working_order("NVDA")])
        monkeypatch.setattr("ib_insync.IB", lambda: ib)
        mgr, adopted = _adopt_mgr()
        out = pr.heal(_db([]), mgr, overfill_flatten_enabled=False)
        assert out["orphans_adopted"] == 1

    def test_filled_order_does_not_defer(self, monkeypatch):
        """Only WORKING states defer; a Filled order is not in-flight."""
        from agora.ops import position_reconciler as pr
        ib = _IBWithOrders(self._broker_long_leg(), [_working_order("JPM", status="Filled")])
        monkeypatch.setattr("ib_insync.IB", lambda: ib)
        mgr, adopted = _adopt_mgr()
        out = pr.heal(_db([]), mgr, overfill_flatten_enabled=False)
        assert out["orphans_adopted"] == 1

    def test_non_agora_order_does_not_defer(self, monkeypatch):
        """A manual/non-AGORA working order is not our in-flight spread → adopt normally."""
        from agora.ops import position_reconciler as pr
        ib = _IBWithOrders(self._broker_long_leg(), [_working_order("JPM", ref="MANUAL-7")])
        monkeypatch.setattr("ib_insync.IB", lambda: ib)
        mgr, adopted = _adopt_mgr()
        out = pr.heal(_db([]), mgr, overfill_flatten_enabled=False)
        assert out["orphans_adopted"] == 1

    def test_guard_disabled_adopts_unconditionally(self, monkeypatch):
        """orphan_inflight_guard=False restores the old unconditional adoption."""
        from agora.ops import position_reconciler as pr
        ib = _IBWithOrders(self._broker_long_leg(), [_working_order("JPM")])
        monkeypatch.setattr("ib_insync.IB", lambda: ib)
        mgr, adopted = _adopt_mgr()
        out = pr.heal(_db([]), mgr, overfill_flatten_enabled=False, orphan_inflight_guard=False)
        assert out["orphans_adopted"] == 1

    def test_reread_drops_orphan_when_book_now_covers_it(self, monkeypatch):
        """Belt: a spread row that lands BETWEEN the snapshot diff and the adopt is caught by the
        fresh db_legs re-read (orphan in the snapshot, covered on re-read → skipped, not adopted)."""
        from agora.ops import position_reconciler as pr
        ib = _IBWithOrders(self._broker_long_leg(), [])
        monkeypatch.setattr("ib_insync.IB", lambda: ib)
        mgr, adopted = _adopt_mgr()
        calls = {"n": 0}
        def fake_db_legs(_p):
            calls["n"] += 1
            return {} if calls["n"] == 1 else {("JPM", "C", 340.0, "20260724"): 1}
        monkeypatch.setattr(pr, "db_legs", fake_db_legs)
        out = pr.heal(_db([]), mgr, overfill_flatten_enabled=False)
        assert out["orphans_adopted"] == 0
        assert adopted == []

"""
agora/tests/test_scan_engine.py — UniverseScanEngine scheduling logic: priority ordering,
in-flight dedup, IMMEDIATE bypass-when-full, queue-full backpressure, and time-of-day cadence.
A regression here silently starves high-priority (catalyst) tickers or double-evaluates names —
both corrupt the scan economics. Pure/async logic, mocked with a temp DB + dummy evaluator.
"""
from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime as _dt

import pytest

import agora.scan.engine as eng
from agora.scan.engine import ScanPriority, ScanRequest, UniverseScanEngine, _MAX_QUEUE_SIZE


def _engine(universe=None) -> UniverseScanEngine:
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name

    async def _noop_eval(ticker, priority, reason):
        return None

    return UniverseScanEngine(
        universe_fn=lambda: (universe or ["AAPL", "MSFT"]),
        evaluator=_noop_eval,
        db_path=db,
        n_workers=2,
        shadow_mode=True,
    )


# ── ScanPriority + ScanRequest ordering ───────────────────────────────────────
class TestPriorityOrdering:
    def test_priority_enum_order(self):
        assert ScanPriority.IMMEDIATE < ScanPriority.URGENT < ScanPriority.NORMAL < ScanPriority.BACKGROUND

    def test_scan_request_sorts_by_priority_only(self):
        reqs = [
            ScanRequest(ScanPriority.BACKGROUND, 3.0, "C", "bg"),
            ScanRequest(ScanPriority.IMMEDIATE, 1.0, "A", "now"),
            ScanRequest(ScanPriority.NORMAL, 2.0, "B", "norm"),
        ]
        ordered = sorted(reqs)
        assert [r.ticker for r in ordered] == ["A", "B", "C"]

    def test_equal_priority_does_not_raise(self):
        # enqueue_time/ticker are compare=False — two equal-priority reqs must be comparable
        a = ScanRequest(ScanPriority.NORMAL, 1.0, "A", "x")
        b = ScanRequest(ScanPriority.NORMAL, 2.0, "B", "y")
        assert not (a < b) and not (b < a)   # equal under the dataclass ordering


# ── enqueue ───────────────────────────────────────────────────────────────────
class TestEnqueue:
    @pytest.mark.asyncio
    async def test_basic_enqueue_increments_queue(self):
        e = _engine()
        assert await e.enqueue(ScanPriority.NORMAL, "AAPL", "test") is True
        assert e.queue_size() == 1

    @pytest.mark.asyncio
    async def test_in_flight_ticker_is_deduped(self):
        e = _engine()
        e._in_flight.add("AAPL")
        assert await e.enqueue(ScanPriority.NORMAL, "AAPL", "dup") is False
        assert e.queue_size() == 0

    @pytest.mark.asyncio
    async def test_priority_dequeue_order(self):
        e = _engine()
        await e.enqueue(ScanPriority.NORMAL, "N", "n")
        await e.enqueue(ScanPriority.IMMEDIATE, "I", "i")
        await e.enqueue(ScanPriority.URGENT, "U", "u")
        popped = [e._queue.get_nowait().ticker for _ in range(3)]
        assert popped == ["I", "U", "N"]   # IMMEDIATE → URGENT → NORMAL

    @pytest.mark.asyncio
    async def test_queue_full_rejects_non_immediate(self):
        e = _engine()
        e._queue = asyncio.PriorityQueue(maxsize=2)
        assert await e.enqueue(ScanPriority.NORMAL, "A", "x") is True
        assert await e.enqueue(ScanPriority.NORMAL, "B", "x") is True
        assert await e.enqueue(ScanPriority.NORMAL, "C", "x") is False   # full → dropped
        assert e.queue_size() == 2

    @pytest.mark.asyncio
    async def test_immediate_bypasses_full_queue(self):
        e = _engine()
        e._queue = asyncio.PriorityQueue(maxsize=2)
        await e.enqueue(ScanPriority.BACKGROUND, "A", "x")
        await e.enqueue(ScanPriority.BACKGROUND, "B", "x")
        # IMMEDIATE must force in even when full (it drains one blocked item to make room)
        assert await e.enqueue(ScanPriority.IMMEDIATE, "URGENT1", "catalyst") is True
        # the IMMEDIATE item is present and sorts to the front
        assert e._queue.get_nowait().ticker == "URGENT1"

    @pytest.mark.asyncio
    async def test_metadata_carried_through(self):
        e = _engine()
        await e.enqueue(ScanPriority.URGENT, "AAPL", "move", metadata={"pct": 2.1})
        assert e._queue.get_nowait().metadata == {"pct": 2.1}


# ── status accessors ──────────────────────────────────────────────────────────
class TestStatus:
    def test_get_status_shape(self):
        e = _engine()
        s = e.get_status()
        assert set(s) == {"shadow_mode", "n_workers", "queue_size", "in_flight", "tickers_seen"}
        assert s["shadow_mode"] is True and s["n_workers"] == 2

    def test_shadow_mode_setter(self):
        e = _engine()
        e.shadow_mode = False
        assert e.shadow_mode is False

    def test_in_flight_count(self):
        e = _engine()
        e._in_flight.update({"A", "B"})
        assert e.in_flight_count() == 2

    def test_max_queue_size_constant(self):
        # the real engine queue is bounded — guards against an unbounded memory leak
        e = _engine()
        assert e._queue.maxsize == _MAX_QUEUE_SIZE


# ── time-of-day cadence ───────────────────────────────────────────────────────
class TestCadence:
    def _patch_clock(self, monkeypatch, hour, minute):
        class _FakeDT:
            @staticmethod
            def now(tz=None):
                return _dt(2026, 1, 5, hour, minute, tzinfo=tz)
        monkeypatch.setattr(eng, "datetime", _FakeDT)

    def test_prime_time(self, monkeypatch):
        self._patch_clock(monkeypatch, 10, 30)        # 10:00–11:30 prime
        assert _engine()._get_cadence() == (60, 180)

    def test_opening_watch(self, monkeypatch):
        self._patch_clock(monkeypatch, 9, 45)         # 09:30–10:00
        assert _engine()._get_cadence() == (30, 60)

    def test_premarket(self, monkeypatch):
        self._patch_clock(monkeypatch, 6, 0)          # 04:00–09:30
        assert _engine()._get_cadence() == (300, 1800)

    def test_after_hours(self, monkeypatch):
        self._patch_clock(monkeypatch, 20, 0)         # 16:00–24:00
        assert _engine()._get_cadence() == (600, 3600)

    def test_overnight_fallback(self, monkeypatch):
        self._patch_clock(monkeypatch, 2, 0)          # 00:00–04:00 → no band → fallback
        assert _engine()._get_cadence() == (600, 3600)

    def test_cadence_returns_int_tuple(self, monkeypatch):
        self._patch_clock(monkeypatch, 13, 0)
        sweep, stale = _engine()._get_cadence()
        assert isinstance(sweep, int) and isinstance(stale, int) and sweep > 0 and stale > 0

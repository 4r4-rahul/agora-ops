"""EntryTimingGate — the hard entry-window gate (prevents the CSCO after-hours-8K failure). The clock
is mocked so every branch (weekend / pre-market / open-window / permitted / after-close + boundaries)
is deterministic."""
import datetime as _dt
from zoneinfo import ZoneInfo

import agora.risk.entry_timing as et
from agora.risk.entry_timing import EntryTimingGate

ET = ZoneInfo("America/New_York")
# 2026-06-29 = Monday, 2026-06-27 = Saturday (verified weekdays)


def _freeze(monkeypatch, y, mo, d, h, mi):
    fixed = _dt.datetime(y, mo, d, h, mi, tzinfo=ET)
    class _F:
        @staticmethod
        def now(tz=None):
            return fixed
    monkeypatch.setattr(et, "datetime", _F)


class TestEntryTimingGate:
    def test_weekend_blocked(self, monkeypatch):
        _freeze(monkeypatch, 2026, 6, 27, 11, 0)               # Saturday midday
        ok, reason = EntryTimingGate().is_entry_permitted()
        assert ok is False and "Weekend" in reason

    def test_premarket_blocked(self, monkeypatch):
        _freeze(monkeypatch, 2026, 6, 29, 8, 0)                # Monday 08:00
        ok, reason = EntryTimingGate().is_entry_permitted()
        assert ok is False and "Pre-market" in reason

    def test_open_window_blocked(self, monkeypatch):
        _freeze(monkeypatch, 2026, 6, 29, 9, 45)               # Monday 09:45 (first 30 min)
        ok, reason = EntryTimingGate().is_entry_permitted()
        assert ok is False and "open window" in reason.lower()

    def test_permitted_midday(self, monkeypatch):
        _freeze(monkeypatch, 2026, 6, 29, 11, 0)               # Monday 11:00
        ok, reason = EntryTimingGate().is_entry_permitted()
        assert ok is True and "Market hours" in reason

    def test_boundary_exactly_open_is_permitted(self, monkeypatch):
        _freeze(monkeypatch, 2026, 6, 29, 10, 0)               # exactly 10:00
        assert EntryTimingGate().is_entry_permitted()[0] is True

    def test_boundary_exactly_close_is_blocked(self, monkeypatch):
        _freeze(monkeypatch, 2026, 6, 29, 15, 30)              # exactly 3:30
        ok, reason = EntryTimingGate().is_entry_permitted()
        assert ok is False and "3:30" in reason

    def test_after_close_blocked(self, monkeypatch):
        _freeze(monkeypatch, 2026, 6, 29, 15, 45)              # Monday 15:45
        assert EntryTimingGate().is_entry_permitted()[0] is False

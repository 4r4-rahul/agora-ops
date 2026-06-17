"""
tests/test_eval_cooldown.py — the per-ticker evaluation cooldown that stops a BACKGROUND sweep
from re-running the full spread stack (analyst + strategy + advocate LLM calls) on the same ticker
every cycle (~11×/session observed for NVDA, all redundant). Catalyst/urgent/normal scans bypass.
Tests the extracted decision helper directly (no AgoraSession construction needed).
"""
from __future__ import annotations

import types
from datetime import datetime, timedelta, timezone

from agora.session import AgoraSession
from agora.scan import ScanPriority


def _fake(cooldown_secs: int):
    """A minimal stand-in carrying just the attributes _eval_cooldown_skip touches."""
    obj = types.SimpleNamespace()
    obj._settings = types.SimpleNamespace(evaluate_ticker_cooldown_secs=cooldown_secs)
    obj._eval_cooldowns = {}
    return obj


def _skip(obj, ticker, prio):
    # Call the real method against the lightweight object.
    return AgoraSession._eval_cooldown_skip(obj, ticker, prio)


def test_first_background_eval_proceeds_and_arms_cooldown():
    obj = _fake(900)
    assert _skip(obj, "NVDA", ScanPriority.BACKGROUND) is False
    assert "NVDA" in obj._eval_cooldowns  # cooldown armed


def test_second_background_eval_within_window_is_skipped():
    obj = _fake(900)
    assert _skip(obj, "NVDA", ScanPriority.BACKGROUND) is False
    # immediate re-scan → within the window → skipped
    assert _skip(obj, "NVDA", ScanPriority.BACKGROUND) is True


def test_expired_cooldown_proceeds_again():
    obj = _fake(900)
    _skip(obj, "NVDA", ScanPriority.BACKGROUND)
    # force the cooldown into the past
    obj._eval_cooldowns["NVDA"] = datetime.now(tz=timezone.utc) - timedelta(seconds=1)
    assert _skip(obj, "NVDA", ScanPriority.BACKGROUND) is False


def test_urgent_scan_bypasses_cooldown():
    obj = _fake(900)
    _skip(obj, "NVDA", ScanPriority.BACKGROUND)        # arm it
    # An URGENT (catalyst/price-move/sweep) scan must NEVER be throttled.
    assert _skip(obj, "NVDA", ScanPriority.URGENT) is False
    assert _skip(obj, "NVDA", ScanPriority.IMMEDIATE) is False
    assert _skip(obj, "NVDA", ScanPriority.NORMAL) is False


def test_cooldown_zero_disables_gate():
    obj = _fake(0)
    assert _skip(obj, "NVDA", ScanPriority.BACKGROUND) is False
    assert _skip(obj, "NVDA", ScanPriority.BACKGROUND) is False  # never skips
    assert obj._eval_cooldowns == {}  # nothing armed


def test_distinct_tickers_independent():
    obj = _fake(900)
    assert _skip(obj, "NVDA", ScanPriority.BACKGROUND) is False
    assert _skip(obj, "AAPL", ScanPriority.BACKGROUND) is False  # different ticker proceeds
    assert _skip(obj, "NVDA", ScanPriority.BACKGROUND) is True   # NVDA still cooling

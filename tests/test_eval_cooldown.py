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


def _fake(cooldown_secs: int, urgent_secs: int = 180):
    """A minimal stand-in carrying just the attributes _eval_cooldown_skip touches."""
    obj = types.SimpleNamespace()
    obj._settings = types.SimpleNamespace(
        evaluate_ticker_cooldown_secs=cooldown_secs,
        urgent_eval_cooldown_secs=urgent_secs,
    )
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
    # force the last-eval timestamp past the BACKGROUND gap
    obj._eval_cooldowns["NVDA"] = datetime.now(tz=timezone.utc) - timedelta(seconds=901)
    assert _skip(obj, "NVDA", ScanPriority.BACKGROUND) is False


def test_immediate_scan_always_bypasses():
    obj = _fake(900)
    _skip(obj, "NVDA", ScanPriority.BACKGROUND)        # arm it
    # IMMEDIATE (discrete catalyst / position-under-stress) must NEVER be throttled.
    assert _skip(obj, "NVDA", ScanPriority.IMMEDIATE) is False
    assert _skip(obj, "NVDA", ScanPriority.IMMEDIATE) is False  # even back-to-back


def test_urgent_and_normal_get_lighter_cooldown():
    # LLM-spend audit: URGENT/NORMAL are now throttled by the LIGHTER cooldown, not fully bypassed.
    obj = _fake(900, urgent_secs=180)
    assert _skip(obj, "NVDA", ScanPriority.URGENT) is False   # first one proceeds + records
    assert _skip(obj, "NVDA", ScanPriority.URGENT) is True    # immediate re-fire → throttled
    assert _skip(obj, "NVDA", ScanPriority.NORMAL) is True    # NORMAL shares the lighter gap
    # past the urgent gap → proceeds again
    obj._eval_cooldowns["NVDA"] = datetime.now(tz=timezone.utc) - timedelta(seconds=181)
    assert _skip(obj, "NVDA", ScanPriority.URGENT) is False


def test_urgent_zero_reverts_to_full_bypass():
    obj = _fake(900, urgent_secs=0)
    _skip(obj, "NVDA", ScanPriority.BACKGROUND)               # arm it
    assert _skip(obj, "NVDA", ScanPriority.URGENT) is False   # 0 → URGENT never throttled
    assert _skip(obj, "NVDA", ScanPriority.URGENT) is False


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

"""
agora/tests/test_close_path_audit.py — S0.1 close-path audit, encoded as a REGRESSION GUARD.

The day-0/1 churn that destroyed expectancy was fixed by min-hold + require-kill guards (~06-11).
These tests fail loudly if anyone removes or weakens those guards, so the fix can't silently
regress. They assert (a) the config knobs exist with churn-safe defaults, and (b) the exit path in
position_manager still references them.
"""
from __future__ import annotations

import inspect

from agora.core.config import get_settings
from agora.lifecycle import position_manager


class TestChurnGuardConfig:
    def test_min_hold_defaults_are_churn_safe(self):
        s = get_settings()
        # longs: ≥2 days; spreads: ≥3 days — the guards that stopped the day-0/1 churn
        assert getattr(s, "long_exit_llm_min_hold_days", 0) >= 2
        assert getattr(s, "spread_exit_llm_min_hold_days", 0) >= 3

    def test_spread_require_kill_on(self):
        # credit spreads must require a HARD kill trigger to close on a soft thesis read
        assert getattr(get_settings(), "spread_exit_require_kill", False) is True

    def test_winner_lock_on(self):
        assert getattr(get_settings(), "long_exit_llm_winner_lock", False) is True


class TestExitPathStillGuarded:
    def test_exit_check_references_min_hold(self):
        src = inspect.getsource(position_manager)
        # the LLM exit path must still gate on the min-hold guard
        assert "long_exit_llm_min_hold_days" in src
        assert "spread_exit_llm_min_hold_days" in src
        # …and still short-circuit before acting when held < min
        assert "entry_date).days < _min_hold" in src

    def test_post_close_watch_is_wired(self):
        # S0.2 must remain wired into the close path
        src = inspect.getsource(position_manager)
        assert "_watch_after_close" in src and "record_close" in src

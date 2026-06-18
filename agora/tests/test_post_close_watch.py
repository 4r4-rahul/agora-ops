"""
agora/tests/test_post_close_watch.py — S0.2 post-close counterfactual. After a close, we watch the
underlying for a window and label the exit EARLY_EXIT (move continued our way → left money),
CORRECT_EXIT (moved against us → exit protected the book), or NEUTRAL, then aggregate by exit
reason. Pure logic with an injected price function — no network. A bug here mis-teaches the exit
agent which exit type cuts winners short.
"""
from __future__ import annotations

import tempfile
import types
from datetime import date, timedelta

from agora.ops.post_close_watch import (
    _MOVE_THRESHOLD,
    _bullish,
    evaluate_due,
    exit_regret_report,
    record_close,
)


def _pos(ticker="QQQ", strategy="long_put", direction="bearish", pnl=-100.0, pid="p1"):
    return types.SimpleNamespace(
        position_id=pid, ticker=ticker, direction=direction, unrealized_pnl=pnl,
        strategy=types.SimpleNamespace(value=strategy))


def _db():
    return tempfile.NamedTemporaryFile(suffix=".db", delete=False).name


def _evaluate(db, path):
    return evaluate_due(db, price_fn=lambda t, s, e: path, today=date.today() + timedelta(days=8))


# ── _bullish (which way helps the closed position) ────────────────────────────
class TestBullish:
    def test_direction_wins(self):
        assert _bullish("bullish", "long_put") is True
        assert _bullish("bearish", "long_call") is False

    def test_falls_back_to_structure(self):
        assert _bullish("", "long_call") is True
        assert _bullish("", "bull_put_spread") is True
        assert _bullish("", "long_put") is False
        assert _bullish("", "bear_call_spread") is False

    def test_undecidable(self):
        assert _bullish("neutral", "iron_condor") is None


# ── scoring (the heart) ───────────────────────────────────────────────────────
class TestScoring:
    def test_long_put_underlying_falls_is_early_exit(self):
        # bearish position, underlying drops 4% after close → favorable → we left money
        db = _db(); record_close(db, _pos(strategy="long_put", direction="bearish"), "stop_loss", spot=500.0)
        _evaluate(db, [495.0, 488.0, 480.0])
        rep = exit_regret_report(db)["by_exit_reason"]["stop_loss"]
        assert rep["early_exit_rate"] == 1.0 and rep["mean_regret_pct"] > 0.03

    def test_long_call_underlying_rises_is_early_exit(self):
        db = _db(); record_close(db, _pos(strategy="long_call", direction="bullish"), "profit_target", spot=100.0)
        _evaluate(db, [101.0, 104.0, 106.0])   # +6% up = favorable for a call
        rep = exit_regret_report(db)["by_exit_reason"]["profit_target"]
        assert rep["early_exit_rate"] == 1.0

    def test_exit_was_correct_when_move_goes_against(self):
        # closed a bullish call; underlying then FELL 4% → exiting protected us
        db = _db(); record_close(db, _pos(strategy="long_call", direction="bullish"), "21_dte", spot=100.0)
        _evaluate(db, [99.0, 97.0, 96.0])
        rep = exit_regret_report(db)["by_exit_reason"]["21_dte"]
        assert rep["correct_exit_rate"] == 1.0 and rep["mean_regret_pct"] == 0.0

    def test_neutral_when_chop(self):
        db = _db(); record_close(db, _pos(strategy="long_call", direction="bullish"), "lifecycle", spot=100.0)
        _evaluate(db, [100.3, 99.8, 100.1])   # <1.5% either way
        rep = exit_regret_report(db)["by_exit_reason"]["lifecycle"]
        assert rep["n"] == 1 and rep["early_exit_rate"] == 0.0 and rep["correct_exit_rate"] == 0.0

    def test_credit_spread_bullish_holds_above_short(self):
        # bull put spread (bullish credit): underlying rises → would've kept the credit → early exit
        db = _db(); record_close(db, _pos(strategy="bull_put_spread", direction="bullish"), "stop_loss", spot=200.0)
        _evaluate(db, [203.0, 206.0])
        assert exit_regret_report(db)["by_exit_reason"]["stop_loss"]["early_exit_rate"] == 1.0

    def test_unmeasurable_without_spot_or_path(self, monkeypatch):
        # spot unknown (the underlying couldn't be priced at close) → UNMEASURABLE, excluded
        import agora.ops.post_close_watch as pcw
        monkeypatch.setattr(pcw, "_spot_now", lambda t: None)   # also keeps the test offline
        db = _db(); record_close(db, _pos(direction="bearish"), "stop_loss", spot=0.0)
        evaluate_due(db, price_fn=lambda t, s, e: [1, 2], today=date.today() + timedelta(days=8))
        assert exit_regret_report(db).get("total_evaluated", 0) == 0

    def test_threshold_boundary(self):
        db = _db(); record_close(db, _pos(strategy="long_call", direction="bullish"), "t", spot=100.0)
        _evaluate(db, [100.0, 100.0 * (1 + _MOVE_THRESHOLD)])   # exactly +1.5% → EARLY
        assert exit_regret_report(db)["by_exit_reason"]["t"]["early_exit_rate"] == 1.0


# ── lifecycle: record → only-due evaluated → idempotent ───────────────────────
class TestLifecycle:
    def test_only_due_rows_evaluated(self):
        db = _db()
        record_close(db, _pos(pid="p1"), "stop_loss", spot=100.0)
        # not due yet (horizon is +7d, today is now)
        n = evaluate_due(db, price_fn=lambda t, s, e: [100, 99], today=date.today())
        assert n == 0

    def test_evaluate_is_idempotent(self):
        db = _db(); record_close(db, _pos(strategy="long_call", direction="bullish"), "t", spot=100.0)
        n1 = _evaluate(db, [104.0])
        n2 = _evaluate(db, [104.0])   # already evaluated
        assert n1 == 1 and n2 == 0

    def test_record_close_never_raises(self):
        # bad position object → swallowed, no row, no exception
        record_close(_db(), object(), "x", spot=100.0)

    def test_report_by_multiple_reasons(self):
        db = _db()
        record_close(db, _pos(pid="a", strategy="long_call", direction="bullish"), "stop_loss", spot=100.0)
        record_close(db, _pos(pid="b", strategy="long_call", direction="bullish"), "profit_target", spot=100.0)
        _evaluate(db, [106.0])
        rep = exit_regret_report(db)["by_exit_reason"]
        assert set(rep) == {"stop_loss", "profit_target"}

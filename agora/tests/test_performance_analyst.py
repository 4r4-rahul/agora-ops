"""
agora/tests/test_performance_analyst.py — pure helpers of the weekly PerformanceAnalyst (the agent
that mines closed trades into reviewable lessons): win-rate computation, robust JSON extraction from
LLM output, lesson de-duplication (word-overlap vs the live lesson store), and the Discord digest
formatter. A wrong win-rate or a missed dedup pollutes the human lesson-review queue.
"""
from __future__ import annotations

import sqlite3
import tempfile
import types

import pytest

from agora.agents.performance_analyst import _parse_json, _win_rate
import agora.agents.performance_analyst as pa


# ── _win_rate ─────────────────────────────────────────────────────────────────
class TestWinRate:
    def test_all_wins(self):
        assert _win_rate([{"pnl": 10}, {"pnl": 5}], "pnl") == 1.0

    def test_mixed(self):
        # 2 wins of 4 scored → 0.5
        assert _win_rate([{"pnl": 10}, {"pnl": -3}, {"pnl": 1}, {"pnl": -2}], "pnl") == 0.5

    def test_none_pnls_excluded(self):
        # only the 2 scored rows count; 1 win → 0.5
        assert _win_rate([{"pnl": 10}, {"pnl": -3}, {"pnl": None}], "pnl") == 0.5

    def test_all_none_returns_none(self):
        assert _win_rate([{"pnl": None}, {"pnl": None}], "pnl") is None

    def test_empty_returns_none(self):
        assert _win_rate([], "pnl") is None

    def test_zero_is_not_a_win(self):
        assert _win_rate([{"pnl": 0}, {"pnl": 0}], "pnl") == 0.0

    def test_rounded_to_3dp(self):
        rows = [{"pnl": 1}] * 1 + [{"pnl": -1}] * 2   # 1/3
        assert _win_rate(rows, "pnl") == 0.333


# ── _parse_json (robust LLM-output extraction) ────────────────────────────────
class TestParseJson:
    def test_clean_json(self):
        assert _parse_json('{"a": 1}') == {"a": 1}

    def test_prose_prefix_stripped(self):
        assert _parse_json('Here is the result: {"a": 1, "b": 2}')["b"] == 2

    def test_trailing_garbage_after_object(self):
        assert _parse_json('{"a": 1} -- done')["a"] == 1

    def test_unparseable_returns_skeleton(self):
        r = _parse_json("no json here at all")
        assert r["summary"] == "parse error" and r["lessons"] == []

    def test_empty_string_returns_skeleton(self):
        assert _parse_json("")["summary"] == "parse error"


# ── _is_duplicate (word-overlap vs the lesson store) ──────────────────────────
class TestIsDuplicate:
    def _stub(self, existing_lessons):
        db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        with sqlite3.connect(db) as c:
            c.execute("CREATE TABLE agent_lessons (agent_name TEXT, lesson_text TEXT, active INTEGER)")
            for agent, text in existing_lessons:
                c.execute("INSERT INTO agent_lessons VALUES (?,?,1)", (agent, text))
        return types.SimpleNamespace(_db_path=db)

    def test_high_overlap_is_duplicate(self):
        stub = self._stub([("analyst", "avoid selling premium when IV rank is below thirty")])
        # >50% of the new words appear in the existing lesson
        assert pa.PerformanceAnalystAgent._is_duplicate(
            stub, "analyst", "avoid selling premium when IV rank below thirty percent") is True

    def test_low_overlap_not_duplicate(self):
        stub = self._stub([("analyst", "avoid selling premium when IV rank is below thirty")])
        assert pa.PerformanceAnalystAgent._is_duplicate(
            stub, "analyst", "close positions before earnings announcements") is False

    def test_other_agent_not_compared(self):
        stub = self._stub([("exit", "avoid selling premium when IV rank is below thirty")])
        # same text but a DIFFERENT agent → not a duplicate for 'analyst'
        assert pa.PerformanceAnalystAgent._is_duplicate(
            stub, "analyst", "avoid selling premium when IV rank is below thirty") is False

    def test_empty_store_not_duplicate(self):
        assert pa.PerformanceAnalystAgent._is_duplicate(self._stub([]), "analyst", "any lesson") is False


# ── _build_digest (Discord report formatting) ─────────────────────────────────
class TestBuildDigest:
    def test_includes_summary_and_count(self):
        out = pa.PerformanceAnalystAgent._build_digest(
            object(), {"summary": "Solid week."}, written=3)
        assert "Solid week." in out
        assert "3 lesson(s) pending" in out
        assert "!lessons" in out

    def test_feature_value_lifts_rendered(self):
        raw = {"summary": "x", "feature_value": {
            "chart_vision_delta_pct": 4.2, "flow_signals_delta_pct": -1.5}}
        out = pa.PerformanceAnalystAgent._build_digest(object(), raw, written=0)
        assert "Chart vision lift: +4.2%" in out
        assert "Flow signals lift: -1.5%" in out

    def test_calibration_flags_capped_at_three(self):
        raw = {"summary": "x", "calibration_flags": [
            {"severity": "high", "agent": f"a{i}", "issue": "overconfident"} for i in range(5)]}
        out = pa.PerformanceAnalystAgent._build_digest(object(), raw, written=1)
        assert out.count("overconfident") == 3   # only first 3 shown
        assert "[HIGH]" in out

    def test_missing_fields_safe(self):
        out = pa.PerformanceAnalystAgent._build_digest(object(), {}, written=0)
        assert "No summary." in out and "0 lesson(s) pending" in out

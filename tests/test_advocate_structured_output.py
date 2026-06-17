"""
tests/test_advocate_structured_output.py — the advocate's DEFAULT (MCP-off) path forces
structured output via the `advocate_verdict` tool, so the verdict is a schema-validated tool
input (no free-text JSON to parse, no fail-closed-on-prose). These tests prove:

  1. review() calls messages.create with tools=[advocate_verdict] + tool_choice forcing it.
  2. The tool_use input drives the deterministic verdict (HIGH+prob>=20 → BLOCK; else PASS).
  3. Field names in the tool schema match what _compute_verdict / the journal expect.
"""
from __future__ import annotations

import sys
import types
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import agora.agents.advocate_agent as adv
from agora.agents.advocate_agent import AdvocateAgent, _ADVOCATE_VERDICT_TOOL, _compute_verdict


# ── fakes ────────────────────────────────────────────────────────────────────
class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Usage:
    input_tokens = 1200
    output_tokens = 200


class _Resp:
    def __init__(self, content):
        self.content = content
        self.usage = _Usage()
        self.stop_reason = "tool_use"


def _settings(tmp_path: Path):
    return types.SimpleNamespace(
        anthropic_api_key="sk-test",
        db_path=str(tmp_path / "x.db"),
        tavily_api_key=None,
        agent_mcp_tools_enabled=False,   # ← the default path under test
        advocate_cache_ttl_secs=0,       # disable cache so each call hits the client
        claude_fast_model="claude-haiku-4-5",
    )


def _leg():
    return types.SimpleNamespace(
        action="sell", option_type="put", strike=100.0,
        expiration=date.today() + timedelta(days=30),
        mid_price=1.0, delta=-0.2,
    )


def _recommendation():
    return types.SimpleNamespace(
        strategy=types.SimpleNamespace(value="bull_put_spread"),
        direction="bullish", contracts=1, entry_debit_credit=-1.0,
        max_gain_dollars=100.0, max_loss_dollars=400.0, reward_risk_ratio=0.25,
        legs=[_leg()], event_mitigation="none", entry_ivr=0.0,
    )


def _tool_response(failure_modes, verdict_conf=70):
    return _Resp([
        _Block(type="tool_use", name="advocate_verdict", input={
            "verdict": "PASS",  # overwritten deterministically by _compute_verdict
            "verdict_confidence_pct": verdict_conf,
            "verdict_reasoning_one_line": "test",
            "failure_modes": failure_modes,
            "most_likely_loss_scenario": "x",
            "recommendation_if_pass": None,
        }),
    ])


@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    # Stub the DB-touching helpers so the test is pure-in-memory.
    monkeypatch.setattr(adv, "_load_lessons", lambda *a, **k: [])
    monkeypatch.setattr(adv, "_load_cal_note", lambda *a, **k: "")
    monkeypatch.setattr(adv, "_log_msg", lambda *a, **k: None)
    monkeypatch.setattr(AdvocateAgent, "_write_journal", lambda *a, **k: None)


# ── the schema contract ──────────────────────────────────────────────────────
def test_tool_schema_field_names_match_downstream():
    props = _ADVOCATE_VERDICT_TOOL["input_schema"]["properties"]
    assert _ADVOCATE_VERDICT_TOOL["name"] == "advocate_verdict"
    assert props["verdict"]["enum"] == ["PASS", "CAUTION", "BLOCK"]
    fm = props["failure_modes"]["items"]["properties"]
    # These are the exact keys _compute_verdict + the journal + IV filter read.
    for key in ("mode_name", "severity", "probability_pct",
                "trigger_conditions", "already_addressed_by_kill_condition"):
        assert key in fm, f"failure_mode schema missing {key}"
    assert _ADVOCATE_VERDICT_TOOL["input_schema"]["required"] == ["verdict", "failure_modes"]


# ── the forced-tool call ─────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_review_forces_the_verdict_tool(tmp_path):
    agent = AdvocateAgent(_settings(tmp_path), shadow_mode=True)
    create = AsyncMock(return_value=_tool_response(
        [{"mode_name": "low risk", "severity": "LOW", "probability_pct": 10}]
    ))
    agent._client.messages.create = create

    verdict = await agent.review("SPY", _recommendation(), None, [], None)

    assert verdict is not None
    create.assert_awaited_once()
    kwargs = create.await_args.kwargs
    assert kwargs["tools"] == [_ADVOCATE_VERDICT_TOOL]
    assert kwargs["tool_choice"] == {"type": "tool", "name": "advocate_verdict"}
    # No HIGH-severity mode → deterministic PASS.
    assert verdict.verdict == "PASS"


@pytest.mark.asyncio
async def test_high_severity_failure_mode_blocks(tmp_path):
    agent = AdvocateAgent(_settings(tmp_path), shadow_mode=True)
    agent._client.messages.create = AsyncMock(return_value=_tool_response(
        [{"mode_name": "gap risk", "severity": "HIGH", "probability_pct": 45,
          "already_addressed_by_kill_condition": False, "trigger_conditions": []}]
    ))

    verdict = await agent.review("SPY", _recommendation(), None, [], None)
    assert verdict.verdict == "BLOCK"
    assert verdict.failure_modes[0]["mode_name"] == "gap risk"


def test_compute_verdict_matches_schema_keys():
    # Same keys the tool emits drive the deterministic computer.
    assert _compute_verdict(
        [{"severity": "HIGH", "probability_pct": 30,
          "already_addressed_by_kill_condition": False, "trigger_conditions": []}], []
    ) == "BLOCK"
    assert _compute_verdict(
        [{"severity": "HIGH", "probability_pct": 5,
          "already_addressed_by_kill_condition": False, "trigger_conditions": []}], []
    ) == "CAUTION"
    assert _compute_verdict(
        [{"severity": "LOW", "probability_pct": 90}], []
    ) == "PASS"

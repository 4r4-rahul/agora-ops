"""Unit tests for the PURE (non-LLM) decision helpers in AdvocateAgent and
StrategySelectorAgent.

These helpers gate whether trades execute (JSON extraction, deterministic verdict
computation, fact/structure gating, chain summarization). They are pure functions —
no LLM, no network. Bound methods (_apply_fact_gate / _apply_long_structure_filter)
are exercised by constructing the agent via __new__ (skipping the anthropic client)
and setting only the attributes the pure path touches.
"""
from types import SimpleNamespace
from datetime import date, timedelta

import pandas as pd
import pytest

from agora.agents.advocate_agent import (
    AdvocateAgent,
    AdvocateVerdict,
    _parse_json_robust,
    _extract_balanced_object,
    _compute_verdict,
    _parse_verdict,
)
from agora.agents.strategy_selector import (
    StrategySelection,
    _map_strategy_type,
    _parse_selection,
    _summarize_chain,
)
import agora.ops.fact_grounding as fact_grounding


# ── Fixtures / builders ───────────────────────────────────────────────────────

def _fm(name, severity, prob, *, triggers=None, addressed=False, mechanism=""):
    return {
        "mode_name": name,
        "severity": severity,
        "probability_pct": prob,
        "mechanism": mechanism,
        "trigger_conditions": triggers or [],
        "already_addressed_by_kill_condition": addressed,
    }


def _make_advocate():
    """AdvocateAgent without the anthropic client (skip __init__)."""
    agent = AdvocateAgent.__new__(AdvocateAgent)
    agent._settings = SimpleNamespace(db_path=":memory:")
    agent._shadow_mode = True
    # default: no imminent macro event (keeps the structure filter active)
    agent._macro_event_within = lambda days: False
    return agent


def _make_chain():
    """Synthetic options chain {expiry: {calls: df, puts: df}} ~30 DTE out."""
    expiry = (date.today() + timedelta(days=30)).isoformat()
    strikes = [90, 95, 100, 105, 110, 115, 120]
    calls = pd.DataFrame({
        "strike": strikes,
        "bid": [11.0, 7.0, 4.0, 2.0, 1.0, 0.5, 0.2],
        "ask": [11.4, 7.3, 4.2, 2.2, 1.1, 0.6, 0.3],
        "openInterest": [500, 800, 1200, 900, 600, 300, 150],
        "impliedVolatility": [0.30, 0.29, 0.28, 0.29, 0.31, 0.33, 0.35],
    })
    puts = pd.DataFrame({
        "strike": strikes,
        "bid": [0.2, 0.5, 1.0, 2.1, 4.1, 7.1, 11.1],
        "ask": [0.3, 0.6, 1.1, 2.3, 4.3, 7.4, 11.5],
        "openInterest": [120, 250, 700, 1100, 950, 600, 400],
        "impliedVolatility": [0.34, 0.32, 0.30, 0.29, 0.28, 0.29, 0.30],
    })
    return {expiry: {"calls": calls, "puts": puts}}, expiry


# ── 1. JSON extraction robustness (advocate) ──────────────────────────────────

class TestParseJsonRobust:
    def test_bare_object_parsed(self):
        text = '{"verdict": "PASS", "failure_modes": []}'
        obj = _parse_json_robust(text)
        assert obj["verdict"] == "PASS"
        assert obj["failure_modes"] == []

    def test_prose_preamble_then_fenced_json(self):
        text = (
            "Here is my adversarial review of this trade:\n\n"
            "```json\n"
            '{"verdict": "BLOCK", "failure_modes": '
            '[{"mode_name": "IV crush", "severity": "HIGH", "probability_pct": 40}]}\n'
            "```\n"
            "That concludes my analysis."
        )
        obj = _parse_json_robust(text)
        assert obj["verdict"] == "BLOCK"
        assert obj["failure_modes"][0]["mode_name"] == "IV crush"

    def test_literal_empty_braces_in_prose_do_not_trap(self):
        # The model writes a literal `{}` before the real verdict object — the
        # scanner must skip it (it lacks failure_modes) and find the real one.
        text = (
            "The macro thesis object is empty `{}` so I weight structure more.\n"
            '{"verdict": "CAUTION", "failure_modes": '
            '[{"mode_name": "wide spread", "severity": "LOW", "probability_pct": 10}]}'
        )
        obj = _parse_json_robust(text)
        assert obj["verdict"] == "CAUTION"
        assert obj["failure_modes"][0]["mode_name"] == "wide spread"

    def test_object_without_failure_modes_is_rejected(self):
        # _valid requires 'failure_modes' — a dict lacking it is not a verdict.
        with pytest.raises(ValueError):
            _parse_json_robust('{"verdict": "PASS"}')

    def test_array_wrapping_valid_object_extracts_inner_dict(self):
        # A JSON array is not itself a verdict, but the balanced-object scanner
        # finds the first embedded {...} satisfying _valid (has failure_modes) —
        # documented behavior: it recovers the inner verdict dict from the array.
        obj = _parse_json_robust('[{"verdict": "PASS", "failure_modes": []}]')
        assert obj["verdict"] == "PASS"
        assert obj["failure_modes"] == []

    def test_array_without_valid_inner_object_raises(self):
        # Inner dicts lack failure_modes → nothing recoverable → raise, no PASS.
        with pytest.raises(ValueError):
            _parse_json_robust('[{"a": 1}, {"b": 2}]')

    def test_garbage_raises_valueerror_no_crash(self):
        with pytest.raises(ValueError):
            _parse_json_robust("this is not json at all, no braces here")

    def test_empty_string_raises(self):
        with pytest.raises(ValueError):
            _parse_json_robust("")


class TestExtractBalancedObject:
    def test_extracts_first_valid_object(self):
        text = 'noise {"a": 1} more {"failure_modes": [], "verdict": "PASS"} tail'
        # validator requires failure_modes → skips {"a":1}, returns the second.
        obj = _extract_balanced_object(text, lambda o: "failure_modes" in o)
        assert obj["verdict"] == "PASS"

    def test_brace_inside_string_not_counted(self):
        text = '{"note": "a } brace in a string", "failure_modes": []}'
        obj = _extract_balanced_object(text)
        assert obj["note"] == "a } brace in a string"
        assert obj["failure_modes"] == []

    def test_returns_none_when_no_balanced_object(self):
        assert _extract_balanced_object("no braces here at all") is None


# ── 2. Deterministic verdict computation (advocate) ───────────────────────────

class TestComputeVerdict:
    def test_high_severity_high_prob_blocks(self):
        modes = [_fm("crush", "HIGH", 40, triggers=["IV spikes 30%"])]
        assert _compute_verdict(modes, kill_conditions=[]) == "BLOCK"

    def test_high_severity_low_prob_cautions(self):
        modes = [_fm("crush", "HIGH", 10, triggers=["IV spikes 30%"])]
        assert _compute_verdict(modes, kill_conditions=[]) == "CAUTION"

    def test_all_medium_passes(self):
        modes = [_fm("drift", "MEDIUM", 80), _fm("theta", "MEDIUM", 60)]
        assert _compute_verdict(modes, kill_conditions=[]) == "PASS"

    def test_empty_failure_modes_passes(self):
        assert _compute_verdict([], kill_conditions=[]) == "PASS"

    def test_explicit_addressed_flag_downgrades_block(self):
        modes = [_fm("crush", "HIGH", 50, triggers=["x"], addressed=True)]
        assert _compute_verdict(modes, kill_conditions=[]) == "PASS"

    def test_kill_condition_text_match_addresses_mode(self):
        # trigger word ("breakdown") appears in a kill condition → auto-addressed.
        modes = [_fm("breakout fail", "HIGH", 50,
                     triggers=["breakdown below support confirmed"])]
        kc = ["exit if breakdown below 95 support"]
        assert _compute_verdict(modes, kill_conditions=kc) == "PASS"


# ── parse_verdict / parse_selection: defaults vs well-formed ──────────────────

class TestParseVerdict:
    def test_wellformed_maps_all_fields(self):
        raw = {
            "verdict": "CAUTION",
            "verdict_confidence_pct": 75,
            "verdict_reasoning_one_line": "thin liquidity",
            "failure_modes": [_fm("x", "HIGH", 15)],
            "most_likely_loss_scenario": "gap down",
            "recommendation_if_pass": "size down",
        }
        v = _parse_verdict(raw)
        assert isinstance(v, AdvocateVerdict)
        assert v.verdict == "CAUTION"
        assert v.verdict_confidence == 75
        assert v.recommendation == "size down"
        assert v.is_pass is False and v.is_block is False

    def test_missing_fields_fall_back_to_safe_defaults(self):
        v = _parse_verdict({})
        assert v.verdict == "PASS"          # safe default
        assert v.verdict_confidence == 60
        assert v.failure_modes == []
        assert v.recommendation is None
        assert v.is_pass is True

    def test_block_flag(self):
        v = _parse_verdict({"verdict": "BLOCK", "failure_modes": []})
        assert v.is_block is True


# ── 3. Bound gating methods (advocate) ────────────────────────────────────────

class TestLongStructureFilter:
    def test_low_ivr_long_drops_iv_crush_high_mode(self):
        agent = _make_advocate()
        raw = {
            "verdict": "BLOCK",
            "failure_modes": [
                _fm("IV crush on debit", "HIGH", 45,
                    mechanism="iv crush erases gains", triggers=["vega collapse"]),
            ],
        }
        rec = SimpleNamespace(strategy="long_call", entry_ivr=20.0)
        v = agent._apply_long_structure_filter(raw, rec, kill_conditions=[], ticker="TST")
        # The single HIGH mode was a misapplied IV-crush concern on a low-IVR long
        # → dropped → no HIGH modes remain → PASS.
        assert v.verdict == "PASS"
        assert raw["verdict"] == "PASS"
        assert "structure_gate" in raw
        assert raw["structure_gate"]["removed_modes"]

    def test_high_ivr_long_keeps_iv_crush_mode(self):
        agent = _make_advocate()
        raw = {
            "verdict": "BLOCK",
            "failure_modes": [
                _fm("IV crush on debit", "HIGH", 45,
                    mechanism="iv crush erases gains", triggers=["vega collapse"]),
            ],
        }
        # IVR > 50 → genuine crush risk → mode kept → still BLOCK.
        rec = SimpleNamespace(strategy="long_call", entry_ivr=70.0)
        v = agent._apply_long_structure_filter(raw, rec, kill_conditions=[], ticker="TST")
        assert v.verdict == "BLOCK"
        assert "structure_gate" not in raw

    def test_imminent_macro_event_keeps_crush_mode(self):
        agent = _make_advocate()
        agent._macro_event_within = lambda days: True   # event <=2d away
        raw = {
            "verdict": "BLOCK",
            "failure_modes": [
                _fm("IV crush on debit", "HIGH", 45,
                    mechanism="iv crush", triggers=["vega collapse"]),
            ],
        }
        rec = SimpleNamespace(strategy="long_call", entry_ivr=20.0)
        v = agent._apply_long_structure_filter(raw, rec, kill_conditions=[], ticker="TST")
        assert v.verdict == "BLOCK"

    def test_non_long_strategy_untouched(self):
        agent = _make_advocate()
        raw = {
            "verdict": "BLOCK",
            "failure_modes": [
                _fm("IV crush", "HIGH", 45, mechanism="iv crush", triggers=["vega"]),
            ],
        }
        rec = SimpleNamespace(strategy="bull_put_spread", entry_ivr=20.0)
        v = agent._apply_long_structure_filter(raw, rec, kill_conditions=[], ticker="TST")
        # Not a long single → filter is a no-op, verdict reflects raw["verdict"].
        assert v.verdict == "BLOCK"
        assert "structure_gate" not in raw


class TestFactGate:
    def test_no_divergences_leaves_verdict_unchanged(self, monkeypatch):
        agent = _make_advocate()
        monkeypatch.setattr(fact_grounding, "scan", lambda *a, **k: [])
        raw = {
            "verdict": "BLOCK",
            "failure_modes": [_fm("FOMC tomorrow", "HIGH", 50, triggers=["FOMC tomorrow"])],
            "most_likely_loss_scenario": "FOMC surprise tomorrow",
        }
        verdict = _parse_verdict(raw)
        out = agent._apply_fact_gate(raw, verdict, kill_conditions=[], ticker="TST")
        assert out.verdict == "BLOCK"
        assert "fact_gate" not in raw

    def test_fabricated_event_mode_downgrades_block(self, monkeypatch):
        agent = _make_advocate()
        # Simulate the scanner finding a fabricated imminent FOMC claim, and the
        # neutralizer stripping the single HIGH mode that rested on it.
        monkeypatch.setattr(
            fact_grounding, "scan",
            lambda *a, **k: [{"claimed_event": "fomc", "issue": "no FOMC within 14d"}],
        )
        monkeypatch.setattr(
            fact_grounding, "neutralize_fabricated_modes",
            lambda modes, divs: ([], ["FOMC tomorrow"]),
        )
        raw = {
            "verdict": "BLOCK",
            "failure_modes": [_fm("FOMC tomorrow", "HIGH", 50, triggers=["FOMC tomorrow"])],
            "most_likely_loss_scenario": "FOMC surprise tomorrow",
        }
        verdict = _parse_verdict(raw)
        out = agent._apply_fact_gate(raw, verdict, kill_conditions=[], ticker="TST")
        # All HIGH modes removed → recomputed verdict relaxes to PASS.
        assert out.verdict == "PASS"
        assert raw["fact_gate"]["to"] == "PASS"
        assert "FOMC tomorrow" in raw["fact_gate"]["removed_modes"]

    def test_genuine_risk_holds_when_fabricated_mode_removed_but_high_remains(self, monkeypatch):
        agent = _make_advocate()
        genuine = _fm("wide spread eats edge", "HIGH", 40, triggers=["bid-ask 12%"])
        monkeypatch.setattr(
            fact_grounding, "scan",
            lambda *a, **k: [{"claimed_event": "fomc", "issue": "no FOMC"}],
        )
        # Neutralizer strips only the phantom mode; a genuine HIGH mode survives.
        monkeypatch.setattr(
            fact_grounding, "neutralize_fabricated_modes",
            lambda modes, divs: ([genuine], ["FOMC tomorrow"]),
        )
        raw = {
            "verdict": "BLOCK",
            "failure_modes": [
                _fm("FOMC tomorrow", "HIGH", 50, triggers=["FOMC tomorrow"]),
                genuine,
            ],
        }
        verdict = _parse_verdict(raw)
        out = agent._apply_fact_gate(raw, verdict, kill_conditions=[], ticker="TST")
        # Recomputed verdict still BLOCK (genuine HIGH @40%); fact_gate records the
        # held verdict.
        assert out.verdict == "BLOCK"
        assert raw["fact_gate"]["verdict_held"] is True


# ── 2b. Selector parsing & strategy mapping ───────────────────────────────────

class TestMapStrategyType:
    def test_known_strategy_maps_to_enum(self):
        from agora.core.models import StrategyType
        assert _map_strategy_type("long_call") == StrategyType.LONG_CALL
        assert _map_strategy_type("BULL_PUT_SPREAD") == StrategyType.BULL_PUT_SPREAD

    def test_unknown_strategy_returns_none(self):
        assert _map_strategy_type("nonsense_strategy") is None

    def test_none_or_empty_returns_none(self):
        assert _map_strategy_type(None) is None
        assert _map_strategy_type("") is None


class TestParseSelection:
    def test_endorse_wellformed(self):
        raw = {
            "decision": "endorse",
            "strategy_type": "bull_put_spread",
            "expiry_preference": "2026-07-17",
            "contracts": 2,
            "endorses_rules": True,
            "rationale_one_line": "matches thesis",
            "liquidity_score": 8,
            "thesis_alignment_score": 9,
        }
        sel = _parse_selection(raw)
        assert isinstance(sel, StrategySelection)
        assert sel.decision == "endorse"
        assert sel.strategy_type == "bull_put_spread"
        assert sel.contracts == 2
        assert sel.rationale == "matches thesis"
        assert sel.liquidity_score == 8

    def test_no_structure_uses_reason_for_rationale(self):
        # no_structure shape has 'reason' not 'rationale_one_line'.
        raw = {"decision": "no_structure", "reason": "avoid regime"}
        sel = _parse_selection(raw)
        assert sel.decision == "no_structure"
        assert sel.rationale == "avoid regime"
        assert sel.strategy_type is None

    def test_missing_fields_fall_back_to_defaults(self):
        sel = _parse_selection({})
        assert sel.decision == "endorse"      # safe default
        assert sel.contracts == 1
        assert sel.endorses_rules is True
        assert sel.liquidity_score == 5
        assert sel.thesis_alignment_score == 5
        assert sel.concerns == []


# ── 4. Chain summarization (selector) ─────────────────────────────────────────

class TestSummarizeChain:
    def test_compact_shape_and_dte(self):
        chain, expiry = _make_chain()
        summary = _summarize_chain(chain, spot=100.0)
        assert expiry in summary
        block = summary[expiry]
        assert set(block.keys()) == {"dte", "calls", "puts"}
        assert block["dte"] == 30
        assert isinstance(block["calls"], list)
        # Each contract row carries the documented compact columns.
        row = block["calls"][0]
        for col in ("strike", "bid", "ask", "openInterest", "impliedVolatility"):
            assert col in row

    def test_near_spot_strikes_selected(self):
        chain, expiry = _make_chain()
        summary = _summarize_chain(chain, spot=100.0)
        call_strikes = {r["strike"] for r in summary[expiry]["calls"]}
        # calls window is 0.97..1.10 × spot = 97..110 → 100, 105, 110 in range.
        assert 100.0 in call_strikes
        assert 105.0 in call_strikes
        # 120 is outside the near-spot window and not a focus strike → excluded.
        assert 120.0 not in call_strikes

    def test_focus_strikes_always_included(self):
        chain, expiry = _make_chain()
        # 120 is FAR out-of-window for calls, but a focus strike → must appear.
        summary = _summarize_chain(chain, spot=100.0, focus_strikes=[120.0])
        call_strikes = {r["strike"] for r in summary[expiry]["calls"]}
        assert 120.0 in call_strikes

    def test_empty_chain_returns_empty_summary(self):
        assert _summarize_chain({}, spot=100.0) == {}

    def test_malformed_expiry_skipped_gracefully(self):
        # Bad expiry key → date.fromisoformat raises inside the loop → skipped,
        # no crash, that expiry simply absent.
        chain, good_expiry = _make_chain()
        chain["not-a-date"] = {"calls": pd.DataFrame(), "puts": pd.DataFrame()}
        summary = _summarize_chain(chain, spot=100.0)
        assert "not-a-date" not in summary
        assert good_expiry in summary

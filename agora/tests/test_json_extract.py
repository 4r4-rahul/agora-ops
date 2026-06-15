"""Tests for the shared robust LLM-JSON extractor (agora/core/json_extract.py).

This is the single source of truth that replaced the brittle startswith('```') parser across
13 agents — the bug that took down StockAnalyst for 2 days and was actively breaking
ExitIntelligenceAgent in production with 'Expecting value: line 1 column 1 (char 0)'.
"""
import json

import pytest

from agora.core.json_extract import extract_json


def test_prose_preamble_before_fence():
    # The exact production failure: model narrates before the ```json fence.
    txt = 'All data gathered. Synthesizing now.\n\n```json\n{"decision": "hold"}\n```'
    assert extract_json(txt) == {"decision": "hold"}


def test_fence_only():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_fence_without_json_label():
    assert extract_json('```\n{"a": 1}\n```') == {"a": 1}


def test_bare_object():
    assert extract_json('{"bare": true}') == {"bare": True}


def test_prose_around_bare_object():
    assert extract_json('Here is my answer: {"x": 2} hope that helps') == {"x": 2}


def test_bare_array():
    assert extract_json('[{"i": 1}, {"i": 2}]') == [{"i": 1}, {"i": 2}]


def test_array_in_fence():
    assert extract_json('```json\n[{"i": 1}]\n```') == [{"i": 1}]


def test_nested_object_preserved():
    txt = '```json\n{"a": {"b": [1, 2], "c": "}"}}\n```'
    assert extract_json(txt) == {"a": {"b": [1, 2], "c": "}"}}


def test_empty_raises_valueerror():
    with pytest.raises(ValueError):
        extract_json("")
    with pytest.raises(ValueError):
        extract_json("   \n  ")


def test_no_json_raises_jsondecodeerror():
    with pytest.raises(json.JSONDecodeError):
        extract_json("there is no json here at all")

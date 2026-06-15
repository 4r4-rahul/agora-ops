"""Robust JSON extraction from LLM responses — the single source of truth.

Replaces the brittle `if raw_text.startswith("```"): ...` pattern that was duplicated across
~13 agents. That pattern only stripped a ```json fence when the text STARTED with it, so the
moment a model wrote a prose preamble ("All data gathered. Synthesizing now.\n\n```json\n{...}")
json.loads received the prose and raised "Expecting value: line 1 column 1 (char 0)". That took
down StockAnalyst for 2 days and was actively breaking ExitIntelligenceAgent in production.
"""
from __future__ import annotations

import json
import re

_FENCE = re.compile(r"```(?:json)?\s*(\{.*\}|\[.*\])\s*```", re.DOTALL)


def extract_json(text: str):
    """Parse a JSON object/array from an LLM response, tolerant of a ```json fence and/or a
    prose preamble/suffix around it. Returns the parsed dict/list. Raises ValueError on empty
    input and json.JSONDecodeError if no JSON can be recovered."""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty LLM response — no JSON content")

    # 1) A ```json ... ``` fenced object/array anywhere (handles a prose preamble before it).
    m = _FENCE.search(raw)
    if m:
        return json.loads(m.group(1).strip())

    # 2) Already a bare object/array.
    if raw[0] in "{[":
        return json.loads(raw)

    # 3) Prose wrapping a bare object/array — slice to the outermost bracket span.
    starts = [i for i in (raw.find("{"), raw.find("[")) if i != -1]
    ends   = [i for i in (raw.rfind("}"), raw.rfind("]")) if i != -1]
    if starts and ends and max(ends) > min(starts):
        return json.loads(raw[min(starts): max(ends) + 1])

    # Nothing JSON-like — let json raise the precise error.
    return json.loads(raw)

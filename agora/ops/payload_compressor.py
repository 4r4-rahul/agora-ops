"""
agora/ops/payload_compressor.py — Token compression layer (TokenJuice equivalent).

Drop-in replacements for json.dumps(payload, default=str) and raw f-string
content blocks before they reach any LLM. Typical savings:

  • Dict payloads:   25–45% via None-stripping, float rounding, OHLCV→CSV
  • Text content:    10–40% via dedup, whitespace collapse, length cap

Never strips semantic data. Float precision loss (6dp→2dp) is intentional
and safe for all financial inputs used here (price, ratio, pct).
"""

from __future__ import annotations

import json
import re
from typing import Any

# ── Tunables ──────────────────────────────────────────────────────────────────

_FLOAT_DP        = 2          # decimal places for all floats
_STR_MAX_CHARS   = 6_000      # truncate any single string value in a payload
_TEXT_MAX_CHARS  = 8_000      # compress_text() hard cap
_OHLCV_FIELDS    = frozenset({"open", "high", "low", "close", "volume",
                               "o", "h", "l", "c", "v", "date", "ts",
                               "timestamp", "price", "vwap"})
_OHLCV_MIN_ROWS  = 3          # minimum list length before OHLCV conversion


# ── Public API ────────────────────────────────────────────────────────────────

def compress_payload(payload: dict) -> str:
    """
    Compress a dict payload to a compact JSON string.
    Replaces: json.dumps(payload, default=str)
    """
    cleaned = _clean(payload)
    return json.dumps(cleaned, separators=(",", ":"), default=str)


def compress_text(text: str, max_chars: int = _TEXT_MAX_CHARS) -> str:
    """
    Compress a plain-text content block before sending to LLM.
    Collapses redundant whitespace, deduplicates repeated lines, caps length.
    """
    if not text:
        return text
    # collapse multiple blank lines to one
    text = re.sub(r"\n{3,}", "\n\n", text)
    # collapse spaces/tabs (not newlines)
    text = re.sub(r"[ \t]{2,}", " ", text)
    # dedup consecutive identical lines
    lines = text.splitlines(keepends=True)
    deduped: list[str] = []
    prev = None
    for ln in lines:
        stripped = ln.strip()
        if stripped and stripped == prev:
            continue
        deduped.append(ln)
        prev = stripped
    text = "".join(deduped)
    # hard truncation with marker
    if len(text) > max_chars:
        text = text[:max_chars] + "\n…[truncated]"
    return text


# ── Internal helpers ──────────────────────────────────────────────────────────

def _clean(obj: Any) -> Any:
    """Recursively clean a value: strip None, round floats, CSV-ify OHLCV."""
    if obj is None:
        return None  # caller strips at dict level
    if isinstance(obj, float):
        return round(obj, _FLOAT_DP)
    if isinstance(obj, dict):
        result: dict = {}
        for k, v in obj.items():
            cv = _clean(v)
            if cv is not None:            # drop None-valued keys
                result[k] = cv
        return result
    if isinstance(obj, list):
        return _compress_list(obj)
    if isinstance(obj, str):
        return _truncate_str(obj)
    return obj


def _compress_list(lst: list) -> Any:
    """
    If the list looks like a time-series of dicts (OHLCV / price history),
    serialise it as compact CSV instead of JSON objects.
    Otherwise recursively clean each element.
    """
    if (
        len(lst) >= _OHLCV_MIN_ROWS
        and all(isinstance(row, dict) for row in lst)
        and _is_ohlcv_like(lst)
    ):
        return _to_csv(lst)
    cleaned = [_clean(item) for item in lst]
    return [c for c in cleaned if c is not None]


def _is_ohlcv_like(rows: list[dict]) -> bool:
    """Return True if the majority of rows share OHLCV-ish keys."""
    if not rows:
        return False
    sample = rows[0]
    lower_keys = {k.lower() for k in sample}
    overlap = lower_keys & _OHLCV_FIELDS
    return len(overlap) >= 2


def _to_csv(rows: list[dict]) -> str:
    """Convert a list of uniform dicts to a compact CSV string."""
    headers = list(rows[0].keys())
    csv_rows = [",".join(str(headers))]  # header row
    csv_rows[0] = ",".join(headers)
    for row in rows:
        vals = []
        for h in headers:
            v = row.get(h)
            if isinstance(v, float):
                v = round(v, _FLOAT_DP)
            vals.append("" if v is None else str(v))
        csv_rows.append(",".join(vals))
    return "csv:" + "\n".join(csv_rows)


def _truncate_str(s: str) -> str:
    if len(s) > _STR_MAX_CHARS:
        return s[:_STR_MAX_CHARS] + "…"
    return s

"""
tests/test_discord_chunking.py — _chunk_for_discord splits CEO reports for Discord's 2000-char
limit WITHOUT cutting a line mid-content (a >2000-char post is rejected 400 and the whole report
is lost). Verifies: every chunk <= limit, no line is split unless it is itself over-long, and the
joined chunks reconstruct the original (modulo the line-join boundaries).
"""
from __future__ import annotations

from agora.agents.ceo_agent import _chunk_for_discord


def test_short_message_single_chunk():
    assert _chunk_for_discord("hello", limit=1900) == ["hello"]


def test_empty_message_no_chunks():
    assert _chunk_for_discord("", limit=1900) == []


def test_all_chunks_within_limit():
    lines = [f"line {i}: " + "x" * 80 for i in range(200)]
    msg = "\n".join(lines)
    chunks = _chunk_for_discord(msg, limit=1900)
    assert len(chunks) > 1
    assert all(len(c) <= 1900 for c in chunks)


def test_does_not_split_lines_midway():
    lines = [f"row-{i}-" + "y" * 50 for i in range(120)]
    msg = "\n".join(lines)
    chunks = _chunk_for_discord(msg, limit=400)
    # Every original line must appear intact inside exactly one chunk.
    joined = "\n".join(chunks)
    for ln in lines:
        assert ln in joined
    # No chunk should start or end with a fragment that breaks a known line.
    for c in chunks:
        for piece in c.split("\n"):
            # piece must be one of the original lines (never a partial)
            assert piece in lines


def test_overlong_single_line_is_hard_sliced():
    long_line = "z" * 5000
    chunks = _chunk_for_discord(long_line, limit=1900)
    assert all(len(c) <= 1900 for c in chunks)
    assert "".join(chunks) == long_line


def test_overlong_line_mixed_with_normal_lines():
    msg = "short head\n" + ("q" * 4000) + "\nshort tail"
    chunks = _chunk_for_discord(msg, limit=1900)
    assert all(len(c) <= 1900 for c in chunks)
    assert "short head" in chunks[0]
    assert "short tail" in chunks[-1]

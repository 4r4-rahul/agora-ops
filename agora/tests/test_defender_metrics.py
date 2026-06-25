"""
test_defender_metrics.py — override-precision measurement for the Thesis Defender.

Proves the JOIN defender_journal -> decision_chains -> positions/_REAL_CLOSE correctly measures
whether the defender's BLOCK->CAUTION overrides led to winning trades, on REAL fills only.
This is the "measured override precision" the retirement note required before re-enabling.
"""
from __future__ import annotations

import sqlite3

import pytest

from agora.ops.defender_metrics import defender_override_precision


def _make_db(path, defender_rows, chain_rows, position_rows):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE defender_journal (decision_id TEXT, thesis_strength TEXT, "
              "confidence REAL, go_recommendation INTEGER)")
    c.execute("CREATE TABLE decision_chains (chain_id TEXT, position_id TEXT)")
    c.execute("CREATE TABLE positions (position_id TEXT, realized_pnl REAL, status TEXT, "
              "close_date TEXT, close_source TEXT, regime_at_entry TEXT DEFAULT 'neutral')")
    c.executemany("INSERT INTO defender_journal VALUES (?,?,?,?)", defender_rows)
    c.executemany("INSERT INTO decision_chains VALUES (?,?)", chain_rows)
    c.executemany("INSERT INTO positions (position_id, realized_pnl, status, close_date, close_source) "
                  "VALUES (?,?,?,?,?)", position_rows)
    c.commit()
    c.close()


def test_measures_override_precision_on_real_closes(tmp_path):
    db = str(tmp_path / "d.db")
    _make_db(
        db,
        defender_rows=[
            ("c1", "strong", 0.80, 1),   # strong override -> winner
            ("c2", "strong", 0.70, 1),   # strong override -> loser
            ("c3", "weak",   0.90, 0),   # not an override (weak/go=0) -> excluded
            ("c4", "error",  None, 0),   # billing error -> excluded
        ],
        chain_rows=[("c1", "p1"), ("c2", "p2"), ("c3", "p3")],
        position_rows=[
            ("p1",  300.0, "closed", "2026-06-10", "lifecycle"),   # real win
            ("p2", -150.0, "closed", "2026-06-11", "stop_loss"),   # real loss
            ("p3",  999.0, "closed", "2026-06-12", "fabricated_sync"),  # NOT a real close -> excluded
        ],
    )
    r = defender_override_precision(db)
    assert r["overrides"] == 2            # only c1,c2 are strong+go=1
    assert r["closed_real"] == 2          # both reached a trustworthy close
    assert r["wins"] == 1 and r["losses"] == 1
    assert r["win_rate"] == 0.5
    assert r["net_pnl"] == pytest.approx(150.0)   # 300 - 150
    assert "precision" in r["verdict"].lower() or "positive" in r["verdict"].lower()


def test_excludes_fabricated_closes(tmp_path):
    """A fabricated/sync close must NOT count toward override precision (real fills only)."""
    db = str(tmp_path / "d.db")
    _make_db(
        db,
        defender_rows=[("c1", "strong", 0.80, 1)],
        chain_rows=[("c1", "p1")],
        position_rows=[("p1", 500.0, "closed", "2026-06-10", "tws_startup_sync")],
    )
    r = defender_override_precision(db)
    assert r["overrides"] == 1
    assert r["closed_real"] == 0          # fabricated/sync excluded by _REAL_CLOSE
    assert r["win_rate"] is None
    assert "pending" in r["verdict"].lower()


def test_no_overrides_is_safe(tmp_path):
    db = str(tmp_path / "d.db")
    _make_db(db, defender_rows=[("c1", "weak", 0.30, 0)], chain_rows=[], position_rows=[])
    r = defender_override_precision(db)
    assert r["overrides"] == 0
    assert "nothing to judge" in r["verdict"].lower()


# ── Cost control: dedup cache short-circuits identical blocks ──────────────────
def test_defender_dedup_cache(tmp_path, monkeypatch):
    """Re-scans block the same setup 12-26x/day; the agent must defend an identical
    (ticker,strategy,strikes) only ONCE per TTL and reuse the verdict with no LLM call."""
    import asyncio
    import time
    from types import SimpleNamespace

    from agora.agents import thesis_defender as TD

    s = SimpleNamespace(db_path=str(tmp_path / "d.db"), anthropic_api_key="sk-test",
                        defender_cache_ttl_secs=14400, defender_max_tokens=900, tavily_api_key=None)
    agent = TD.ThesisDefenderAgent(s, shadow_mode=False)

    rec = SimpleNamespace(strategy="bull_put_spread",
                          legs=[SimpleNamespace(option_type="put", strike=95.0),
                                SimpleNamespace(option_type="put", strike=90.0)])
    fp = agent._fingerprint("NVDA", rec)
    assert fp == "NVDA:bull_put_spread:p95.0/p90.0"
    # different strikes => different key (a genuinely new setup re-runs)
    rec2 = SimpleNamespace(strategy="bull_put_spread",
                           legs=[SimpleNamespace(option_type="put", strike=80.0),
                                 SimpleNamespace(option_type="put", strike=75.0)])
    assert agent._fingerprint("NVDA", rec2) != fp

    monkeypatch.setattr(TD, "run_with_tools",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("LLM called on cache hit")))
    sentinel = SimpleNamespace(thesis_strength="strong", confidence=0.8, go_recommendation=1, success_modes=[])
    agent._cache[fp] = (time.monotonic(), sentinel)
    # Use a dedicated loop left set as current, so we don't close the global loop that a later
    # test reaches via the deprecated asyncio.get_event_loop().
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    out = loop.run_until_complete(agent.defend("NVDA", rec, None, [], None, decision_id="d1"))
    assert out is sentinel   # reused, zero LLM calls

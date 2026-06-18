"""
agora/tests/test_tier1_gates.py — Tier-1 bleed-stoppers:
  S1.3 expectancy cell-gate (bench proven-negative post-fix cells; fail-open on thin data/error)
  S1.1 long-options DTE window (config-driven floor — no more 15-DTE theta knives)
  config knobs present with churn-safe defaults
All read-only / pure. The cell-gate is the one that decides whether new capital flows to a losing
cell, so its boundaries are pinned exactly.
"""
from __future__ import annotations

import sqlite3
import tempfile
from datetime import date, timedelta

from agora.agents.long_options_agent import _DTE_MAX, _DTE_MIN, LongOptionsAgent
from agora.core.config import get_settings
from agora.ops.cell_gate import blocked_cells, cell_stats, is_cell_blocked

_CUT = "2026-06-12"


def _db(rows):
    """rows: (strategy, pillar, close_date, realized_pnl)."""
    p = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    c = sqlite3.connect(p)
    c.execute("""CREATE TABLE positions (strategy TEXT, pillar TEXT, status TEXT,
                 close_date TEXT, close_source TEXT, realized_pnl REAL)""")
    c.executemany("INSERT INTO positions VALUES (?,?,'closed',?,'thesis_exit',?)", rows)
    c.commit(); c.close()
    return p


# ── S1.3 cell-gate ────────────────────────────────────────────────────────────
class TestCellGate:
    def test_proven_negative_cell_blocked(self):
        # 10 long_put/directional closes averaging −$150 (post-fix) → blocked
        db = _db([("long_put", "directional", "2026-06-15", -150.0)] * 10)
        blocked, why = is_cell_blocked(db, "long_put", "directional",
                                       cutoff=_CUT, min_samples=8, min_expectancy=-15.0)
        assert blocked is True and "proven negative" in why

    def test_positive_cell_not_blocked(self):
        db = _db([("bull_put_spread", "vol_premium", "2026-06-15", 40.0)] * 10)
        blocked, _ = is_cell_blocked(db, "bull_put_spread", "vol_premium",
                                     cutoff=_CUT, min_samples=8, min_expectancy=-15.0)
        assert blocked is False

    def test_thin_data_fails_open(self):
        # only 3 closes — below min_samples → never block, even if negative
        db = _db([("long_put", "directional", "2026-06-15", -200.0)] * 3)
        blocked, _ = is_cell_blocked(db, "long_put", "directional",
                                     cutoff=_CUT, min_samples=8, min_expectancy=-15.0)
        assert blocked is False

    def test_legacy_excluded_from_gate(self):
        # 10 pre-cutoff churn losers + 8 post-cutoff winners → cell is NOT blocked (judged post-fix)
        rows = [("long_put", "directional", "2026-06-01", -200.0)] * 10 \
             + [("long_put", "directional", "2026-06-15", 30.0)] * 8
        db = _db(rows)
        blocked, _ = is_cell_blocked(db, "long_put", "directional",
                                     cutoff=_CUT, min_samples=8, min_expectancy=-15.0)
        assert blocked is False   # post-fix expectancy is positive

    def test_mildly_negative_above_threshold_not_blocked(self):
        # −$10/trade is bad but above the −$15 bench line → kept (we only bench clear losers)
        db = _db([("iron_condor", "vol_premium", "2026-06-15", -10.0)] * 10)
        blocked, _ = is_cell_blocked(db, "iron_condor", "vol_premium",
                                     cutoff=_CUT, min_samples=8, min_expectancy=-15.0)
        assert blocked is False

    def test_error_fails_open(self):
        blocked, _ = is_cell_blocked("/nonexistent.db", "long_put", "directional",
                                     cutoff=_CUT, min_samples=8, min_expectancy=-15.0)
        assert blocked is False

    def test_cell_stats_and_report(self):
        db = _db([("long_put", "directional", "2026-06-15", -150.0)] * 10
                 + [("bull_put_spread", "vol_premium", "2026-06-15", 40.0)] * 10)
        stats = cell_stats(db, _CUT)
        assert stats[("long_put", "directional")]["expectancy"] == -150.0
        assert stats[("bull_put_spread", "vol_premium")]["expectancy"] == 40.0
        rep = blocked_cells(db, cutoff=_CUT, min_samples=8, min_expectancy=-15.0)
        assert "long_put/directional" in rep["blocked"]
        assert "bull_put_spread/vol_premium" not in rep["blocked"]


# ── S1.1 long-options DTE window ──────────────────────────────────────────────
class TestDteWindow:
    def _chain(self, dtes):
        today = date.today()
        return {(today + timedelta(days=d)).isoformat(): {"calls": [], "puts": []} for d in dtes}

    def test_window_filters_below_floor(self):
        # window [21,35]: a 14-DTE knife is excluded, 25 is selected
        chain = self._chain([14, 25, 40])
        exp, _ = LongOptionsAgent._select_expiry(chain, target_dte=21, dte_min=21, dte_max=35)
        assert exp == (date.today() + timedelta(days=25))

    def test_no_expiry_in_window_returns_none(self):
        chain = self._chain([10, 14, 50])   # nothing in [21,35]
        exp, _ = LongOptionsAgent._select_expiry(chain, 21, dte_min=21, dte_max=35)
        assert exp is None

    def test_default_window_is_module_constants(self):
        # backward-compat: omitting the window uses the old _DTE_MIN/_DTE_MAX
        chain = self._chain([_DTE_MIN, _DTE_MAX, _DTE_MAX + 10])
        exp, _ = LongOptionsAgent._select_expiry(chain, _DTE_MIN)
        assert exp == (date.today() + timedelta(days=_DTE_MIN))


# ── config defaults ───────────────────────────────────────────────────────────
class TestTier1Config:
    def test_defaults_present_and_safe(self):
        s = get_settings()
        assert s.long_options_min_dte >= 21          # off the theta knife
        assert s.long_options_max_dte >= s.long_options_min_dte
        assert s.credit_spread_min_ivr == 50.0       # only sell rich vol
        assert s.cell_gate_enabled is True
        assert s.cell_gate_min_samples >= 1
        assert s.cell_gate_min_expectancy < 0

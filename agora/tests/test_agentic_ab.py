"""
agentic_ab — does the AGENTIC layer earn its seat? (AGENTIC proof-gate)
Locks: predictiveness needs a credible sample + is outlier-robust (top half must beat bottom);
the forward A/B reports insufficient_data until both arms have enough closes, and the gate stays RED
until agentic provably beats the rules-only baseline.
"""
import sqlite3

from agora.ops.agentic_ab import arm_comparison, signal_predictiveness


def _pred_db(tmp_path, rows):
    db = str(tmp_path / "p.db")
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE prediction_ledger (source TEXT, predicted REAL, actual REAL, "
                  "scored INT, is_binary INT)")
        c.executemany("INSERT INTO prediction_ledger (source,predicted,actual,scored,is_binary) "
                      "VALUES (?,?,?,1,1)", rows)
        c.commit()
    return db


class TestSignalPredictiveness:
    def test_insufficient_sample(self, tmp_path):
        db = _pred_db(tmp_path, [("conviction", 0.6, 1.0)] * 5)
        assert signal_predictiveness(db)["conviction"]["verdict"] == "insufficient_data"

    def test_predictive_when_top_half_beats_bottom(self, tmp_path):
        # higher predicted → higher actual (real signal)
        rows = [("news", 0.1, 0.0)] * 15 + [("news", 0.9, 1.0)] * 15
        v = signal_predictiveness(_pred_db(tmp_path, rows))["news"]
        assert v["verdict"] == "predictive"

    def test_not_predictive_when_no_discrimination(self, tmp_path):
        # predicted varies but actual is independent (noise) → top half doesn't beat bottom
        rows = [("conviction", p / 100, (i % 2)) for i, p in enumerate(range(1, 41))]
        v = signal_predictiveness(_pred_db(tmp_path, rows))["conviction"]
        assert v["verdict"] == "not_predictive"


class TestArmComparison:
    def test_insufficient_without_column(self, tmp_path):
        db = str(tmp_path / "n.db")
        with sqlite3.connect(db) as c:
            c.execute("CREATE TABLE positions (realized_pnl REAL, status TEXT)")
            c.commit()
        assert arm_comparison(db)["status"] == "insufficient_data"


class TestAgenticGateStaysRed:
    def test_gate_red_without_arm_data(self, tmp_path):
        db = str(tmp_path / "g.db")
        with sqlite3.connect(db) as c:
            c.execute("CREATE TABLE positions (realized_pnl REAL, status TEXT)")
            c.commit()
        from agora.ops.proof_gates import RED, _agentic_gate
        assert _agentic_gate(db)["status"] == RED


# ── A/B RAIL (2026-06-30): deterministic assignment + regime-stratified readout ───────────────────
from agora.ops.agentic_ab import assign_arm  # noqa: E402


def _arm_db(tmp_path, rows):
    """rows: (decision_arm, regime, realized_pnl) — all real lifecycle closes."""
    db = str(tmp_path / "arm.db")
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE positions (status TEXT, close_date TEXT, close_source TEXT, "
                  "regime_at_entry TEXT, decision_arm TEXT, realized_pnl REAL)")
        c.executemany("INSERT INTO positions VALUES ('closed','2026-06-30','lifecycle',?,?,?)",
                      [(r[1], r[0], r[2]) for r in rows])
        c.commit()
    return db


class TestAssignArm:
    def test_off_is_always_agentic(self):
        assert all(assign_arm(f"T{i}", "2026-06-30", ab_enabled=False) == "agentic" for i in range(50))

    def test_on_is_deterministic_per_ticker_date(self):
        a = assign_arm("NVDA", "2026-06-30", ab_enabled=True)
        assert a == assign_arm("NVDA", "2026-06-30", ab_enabled=True)
        assert a in ("agentic", "rules_only")

    def test_on_splits_roughly_balanced(self):
        import collections
        c = collections.Counter(assign_arm(f"T{i}", "2026-06-30", ab_enabled=True) for i in range(400))
        assert 0.35 < c["rules_only"] / 400 < 0.65   # not degenerate


class TestArmComparisonStratified:
    def test_ready_with_both_arms_and_by_regime(self, tmp_path):
        rows = ([("agentic", "neutral", -50.0)] * 25 + [("agentic", "risk_off", 20.0)] * 20
                + [("rules_only", "neutral", -10.0)] * 25 + [("rules_only", "risk_off", 15.0)] * 20)
        out = arm_comparison(_arm_db(tmp_path, rows))
        assert out["status"] == "ready"
        assert out["agentic"]["n"] == 45 and out["rules_only"]["n"] == 45
        # Simpson's-paradox guard: the per-regime split must be present + carry Wilson CIs
        assert "by_regime" in out and "neutral" in out["by_regime"] and "risk_off" in out["by_regime"]
        assert out["agentic"]["wilson95"] is not None and out["agentic"]["median"] is not None

    def test_below_min_is_insufficient_but_still_stratifies(self, tmp_path):
        rows = [("agentic", "neutral", -50.0)] * 10 + [("rules_only", "neutral", -10.0)] * 10
        out = arm_comparison(_arm_db(tmp_path, rows))
        assert out["status"] == "insufficient_data"
        assert "by_regime" in out

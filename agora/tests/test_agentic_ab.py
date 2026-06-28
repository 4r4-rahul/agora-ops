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

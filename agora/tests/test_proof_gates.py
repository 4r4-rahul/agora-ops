"""
proof_gates — the mechanical scorecard of the four provable pillars (founder's "prove before sell" rule).

Locks the bright-line logic: nothing is sellable until EVERY gate is green; honest-by-design (today the
EDGE gate is RED, so overall is not sellable). Aggregation tested hermetically; the live board tested
for shape + honesty against the real DB contract.
"""
from agora.ops.proof_gates import AMBER, GREEN, RED, _aggregate, proof_gates


class TestAggregateRule:
    def test_all_green_is_green(self):
        assert _aggregate([GREEN, GREEN, GREEN, GREEN]) == GREEN

    def test_any_red_is_red(self):
        assert _aggregate([GREEN, AMBER, RED, GREEN]) == RED

    def test_no_red_but_amber_is_amber(self):
        assert _aggregate([GREEN, AMBER, GREEN, GREEN]) == AMBER

    def test_empty_is_not_green(self):
        assert _aggregate([]) != GREEN


class TestLiveBoard:
    def test_shape_and_honesty(self, tmp_path):
        # On an empty DB every gate degrades safely (RED/error) — must NOT crash, must NOT be sellable.
        board = proof_gates(str(tmp_path / "empty.db"))
        assert set(board) >= {"overall", "sellable", "gates", "note"}
        assert len(board["gates"]) == 4
        assert board["sellable"] is False                     # never sellable unless overall green
        assert board["overall"] in (RED, AMBER)

    def test_real_db_edge_is_red_not_sellable(self):
        # Against the real book the EDGE gate must be RED (expectancy negative) → overall not sellable.
        import os
        if not os.path.exists(".agora/agora.db"):
            return
        board = proof_gates(".agora/agora.db")
        edge = next(g for g in board["gates"] if g.get("pillar") == "EDGE")
        assert edge["status"] == RED
        assert board["sellable"] is False

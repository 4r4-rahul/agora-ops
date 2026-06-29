"""
Live DB↔TWS sync badge — _classify_sync turns the latest position-heal result into a status the UI
renders. in_sync (clean mirror) / corrected (divergence found AND healed → in sync now) / alert
(unresolved over-fill) / error (heal couldn't run) / unknown (no heal yet).
"""
from agora.api.routes import _classify_sync


class TestClassifySync:
    def test_none_is_unknown(self):
        assert _classify_sync(None) == "unknown"

    def test_clean_mirror_is_in_sync(self):
        assert _classify_sync({"matched": 24, "orphans_found": 0, "ghosts_found": 0,
                               "qty_mismatch": 0, "overfill": 0, "errors": []}) == "in_sync"

    def test_errors_is_error(self):
        assert _classify_sync({"errors": ["connect failed"]}) == "error"

    def test_overfill_is_alert(self):
        assert _classify_sync({"overfill": 2, "errors": []}) == "alert"

    def test_ghost_or_orphan_healed_is_corrected(self):
        # heal actually FIXED these this cycle → in sync now
        assert _classify_sync({"orphans_adopted": 1, "errors": []}) == "corrected"
        assert _classify_sync({"ghosts_closed": 1, "errors": []}) == "corrected"

    def test_unresolved_qty_mismatch_is_diverged_not_corrected(self):
        # HONESTY: a qty-mismatch the heal did NOT resolve (e.g. partial spread fill double-booked)
        # must read as a real divergence, never "corrected".
        assert _classify_sync({"qty_mismatch": 2, "errors": []}) == "diverged"
        assert _classify_sync({"qty_mismatch": 1, "orphans_adopted": 1, "errors": []}) == "diverged"

    def test_error_and_overfill_priority(self):
        assert _classify_sync({"errors": ["x"], "overfill": 1, "qty_mismatch": 1}) == "error"
        assert _classify_sync({"overfill": 1, "qty_mismatch": 1}) == "alert"

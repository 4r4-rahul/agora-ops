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

    def test_divergence_found_and_healed_is_corrected(self):
        assert _classify_sync({"orphans_found": 1, "errors": []}) == "corrected"
        assert _classify_sync({"ghosts_found": 1, "errors": []}) == "corrected"
        assert _classify_sync({"qty_mismatch": 1, "errors": []}) == "corrected"

    def test_error_takes_priority_over_overfill(self):
        assert _classify_sync({"errors": ["x"], "overfill": 1}) == "error"

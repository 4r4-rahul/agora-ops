"""
Startup stale-entry-order cleanup (2026-06-29): a restart leaves the prior session's working ENTRY
orders resting at the broker (duplicate/competing orders + naked-leg aborts). At startup the new session
cancels them. This locks the classifier: only WORKING AGORA-* ENTRY orders are stale; CLOSE_* exits and
already-filled/cancelled orders are left alone.
"""
from agora.session import _is_stale_entry_order


class TestIsStaleEntryOrder:
    def test_working_agora_entry_is_stale(self):
        assert _is_stale_entry_order("AGORA-20260629-0932-L0", "Submitted") is True
        assert _is_stale_entry_order("AGORA-20260629-0932-L0", "PreSubmitted") is True
        assert _is_stale_entry_order("AGORA-20260629-0932-L1", "PendingSubmit") is True

    def test_close_order_is_NOT_stale(self):
        # exits are left for the lifecycle manager, never cancelled at startup
        assert _is_stale_entry_order("CLOSE_88fa2ccf-8a59-4ce2-a-L0", "Submitted") is False

    def test_filled_or_cancelled_entry_is_not_stale(self):
        assert _is_stale_entry_order("AGORA-20260629-0932-L0", "Filled") is False
        assert _is_stale_entry_order("AGORA-20260629-0932-L0", "Cancelled") is False

    def test_non_agora_ref_is_not_touched(self):
        assert _is_stale_entry_order("CANARY-FILLTEST", "Submitted") is False
        assert _is_stale_entry_order("", "Submitted") is False
        assert _is_stale_entry_order("FLATTEN_PAPER_CLEANSLATE", "PreSubmitted") is False

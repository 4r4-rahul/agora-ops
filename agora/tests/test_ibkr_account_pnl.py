"""
_clean_ibkr_pnl — IBKR sends a huge sentinel (~1.8e308) or NaN for an account-PnL field until it
populates. We must show '—' (None), never a fake number, in the UI's TWS-daily mirror.
"""
from agora.ops.ibkr_knowledge_agent import _clean_ibkr_pnl


class TestCleanIbkrPnl:
    def test_real_values_rounded(self):
        assert _clean_ibkr_pnl(-2067.126) == -2067.13
        assert _clean_ibkr_pnl(373.7) == 373.7
        assert _clean_ibkr_pnl(0) == 0.0

    def test_nan_is_none(self):
        assert _clean_ibkr_pnl(float("nan")) is None

    def test_sentinel_is_none(self):
        assert _clean_ibkr_pnl(1.7976931348623157e+308) is None
        assert _clean_ibkr_pnl(-1e308) is None

    def test_non_numeric_is_none(self):
        assert _clean_ibkr_pnl(None) is None
        assert _clean_ibkr_pnl("x") is None

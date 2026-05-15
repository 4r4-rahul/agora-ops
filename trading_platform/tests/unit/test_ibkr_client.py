"""
Unit tests for the IBKR client service.

ib_insync is mocked at the IB class level — no TWS or IB Gateway needed.
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from trading_platform.services.ibkr_client import _next_expiry, place_bracket_order


# ── _next_expiry helper ───────────────────────────────────────────────────────

class TestNextExpiry:
    def test_returns_friday(self):
        expiry_str = _next_expiry(14)
        expiry = date.fromisoformat(
            f"{expiry_str[:4]}-{expiry_str[4:6]}-{expiry_str[6:]}"
        )
        assert expiry.weekday() == 4, f"Expected Friday, got weekday {expiry.weekday()}"

    def test_at_least_dte_days_out(self):
        dte = 21
        expiry_str = _next_expiry(dte)
        expiry = date.fromisoformat(
            f"{expiry_str[:4]}-{expiry_str[4:6]}-{expiry_str[6:]}"
        )
        assert expiry >= date.today() + timedelta(days=dte)

    def test_zero_dte_returns_this_or_next_friday(self):
        expiry_str = _next_expiry(0)
        expiry = date.fromisoformat(
            f"{expiry_str[:4]}-{expiry_str[4:6]}-{expiry_str[6:]}"
        )
        assert expiry >= date.today()
        assert expiry.weekday() == 4


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_ib_mock(order_status: str = "Submitted", fills: list | None = None):
    """Build a realistic ib_insync.IB mock — no real network calls."""
    ib = MagicMock()
    ib.isConnected.return_value = True
    ib.connectAsync = AsyncMock()

    qualified_contract = MagicMock()
    qualified_contract.conId = 999_001
    ib.qualifyContractsAsync = AsyncMock(return_value=[qualified_contract])

    parent_trade = MagicMock()
    parent_trade.order.orderId = 42
    parent_trade.orderStatus.status = order_status
    parent_trade.fills = fills or []

    child_trade = MagicMock()
    child_trade.order.orderId = 43
    child_trade.orderStatus.status = order_status
    child_trade.fills = []

    # First placeOrder call = parent (entry), second = child (profit target)
    ib.placeOrder.side_effect = [parent_trade, child_trade]
    ib.sleep = MagicMock()
    ib.disconnect = MagicMock()

    return ib, parent_trade


_LEGS = [
    {"option_type": "call", "strike": 503.0, "expiration_dte": 14,
     "action": "buy", "quantity": 1},
    {"option_type": "call", "strike": 508.0, "expiration_dte": 14,
     "action": "sell", "quantity": 1},
]


# ── place_bracket_order ───────────────────────────────────────────────────────

class TestPlaceBracketOrder:

    @pytest.mark.asyncio
    async def test_submitted_order_returns_status(self):
        ib_mock, _ = _make_ib_mock(order_status="Submitted")
        with patch("trading_platform.services.ibkr_client.IB", return_value=ib_mock):
            result = await place_bracket_order(
                ticker="SPY", legs=_LEGS, contracts=1,
                entry_price=2.20, profit_target=5.00, stop_loss=1.10,
                session_id="test-session", timeout=2.0,
            )
        assert result["status"] == "Submitted"
        assert result["order_id"] == 42

    @pytest.mark.asyncio
    async def test_filled_order_returns_fills(self):
        fill = MagicMock()
        fill.execution.execId = "exec-001"
        fill.execution.shares = 1
        fill.execution.price = 2.15
        fill.execution.time = None

        ib_mock, _ = _make_ib_mock(order_status="Filled", fills=[fill])
        with patch("trading_platform.services.ibkr_client.IB", return_value=ib_mock):
            result = await place_bracket_order(
                ticker="SPY", legs=_LEGS, contracts=1,
                entry_price=2.20, profit_target=5.00, stop_loss=1.10,
                session_id="test-session", timeout=2.0,
            )
        assert result["status"] == "Filled"
        assert len(result["fills"]) == 1

    @pytest.mark.asyncio
    async def test_cancelled_order_raises(self):
        ib_mock, _ = _make_ib_mock(order_status="Cancelled")
        with patch("trading_platform.services.ibkr_client.IB", return_value=ib_mock):
            with pytest.raises(RuntimeError, match="rejected"):
                await place_bracket_order(
                    ticker="SPY", legs=_LEGS, contracts=1,
                    entry_price=2.20, profit_target=5.00, stop_loss=1.10,
                    session_id="test-session", timeout=2.0,
                )
        ib_mock.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_connection_failure_raises(self):
        ib_mock, _ = _make_ib_mock()
        ib_mock.connectAsync = AsyncMock(side_effect=ConnectionRefusedError("no TWS"))
        ib_mock.isConnected.return_value = False
        with patch("trading_platform.services.ibkr_client.IB", return_value=ib_mock):
            with pytest.raises(ConnectionRefusedError):
                await place_bracket_order(
                    ticker="SPY", legs=_LEGS, contracts=1,
                    entry_price=2.20, profit_target=5.00, stop_loss=1.10,
                    session_id="test-session",
                )

    @pytest.mark.asyncio
    async def test_qualify_failure_raises(self):
        ib_mock, _ = _make_ib_mock()
        ib_mock.qualifyContractsAsync = AsyncMock(return_value=[])
        with patch("trading_platform.services.ibkr_client.IB", return_value=ib_mock):
            with pytest.raises(RuntimeError, match="Could not qualify"):
                await place_bracket_order(
                    ticker="SPY", legs=_LEGS, contracts=1,
                    entry_price=2.20, profit_target=5.00, stop_loss=1.10,
                    session_id="test-session",
                )
        ib_mock.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_session_id_written_to_order_ref(self):
        ib_mock, _ = _make_ib_mock(order_status="Submitted")
        captured: list = []

        def _capture(contract, order):
            captured.append(order)
            t = MagicMock()
            t.order.orderId = len(captured)
            t.orderStatus.status = "Submitted"
            t.fills = []
            return t

        ib_mock.placeOrder.side_effect = _capture
        session_id = "abc-1234-very-long-session-id-that-exceeds-forty-chars-surely"

        with patch("trading_platform.services.ibkr_client.IB", return_value=ib_mock):
            await place_bracket_order(
                ticker="SPY", legs=_LEGS, contracts=1,
                entry_price=2.20, profit_target=5.00, stop_loss=1.10,
                session_id=session_id, timeout=2.0,
            )

        ref = captured[0].orderRef
        assert len(ref) <= 40
        assert session_id[:40] == ref

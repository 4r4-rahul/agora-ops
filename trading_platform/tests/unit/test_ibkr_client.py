"""
Unit tests for the IBKR client service.

ib_insync is mocked at the IB class level — no TWS or IB Gateway needed.
The Option, Contract, ComboLeg, LimitOrder classes run as-is (ib_insync is
installed); only the network-touching IB instance is replaced.
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from trading_platform.services.ibkr_client import _next_expiry, place_combo_order


# ── _next_expiry helper ───────────────────────────────────────────────────

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


# ── Helpers ───────────────────────────────────────────────────────────────

def _make_ib_mock(order_status: str = "Submitted", fills: list | None = None):
    """Build a realistic ib_insync.IB mock — no real network calls."""
    ib = MagicMock()
    ib.isConnected.return_value = True
    ib.connectAsync = AsyncMock()

    qualified_contract = MagicMock()
    qualified_contract.conId = 999_001
    ib.qualifyContractsAsync = AsyncMock(return_value=[qualified_contract])

    trade = MagicMock()
    trade.order.orderId = 42
    trade.orderStatus.status = order_status
    trade.fills = fills or []
    ib.placeOrder.return_value = trade
    ib.sleep = MagicMock()
    ib.disconnect = MagicMock()

    return ib, trade


_LEGS = [
    {"option_type": "call", "strike": 503.0, "expiration_dte": 14,
     "action": "buy", "quantity": 1},
    {"option_type": "call", "strike": 508.0, "expiration_dte": 14,
     "action": "sell", "quantity": 1},
]


# ── place_combo_order ─────────────────────────────────────────────────────

class TestPlaceComboOrder:

    @pytest.mark.asyncio
    async def test_submitted_order_returns_status(self):
        ib_mock, _ = _make_ib_mock(order_status="Submitted")
        with patch("trading_platform.services.ibkr_client.IB", return_value=ib_mock):
            result = await place_combo_order(
                ticker="SPY", legs=_LEGS, contracts=1,
                limit_price=2.20, session_id="test-session", timeout=2.0,
            )
        assert result["status"] == "Submitted"
        assert result["order_id"] == 42

    @pytest.mark.asyncio
    async def test_filled_order_computes_avg_price(self):
        fill = MagicMock()
        fill.execution.execId = "exec-001"
        fill.execution.shares = 1
        fill.execution.price = 2.15
        fill.execution.time = None

        ib_mock, _ = _make_ib_mock(order_status="Filled", fills=[fill])
        with patch("trading_platform.services.ibkr_client.IB", return_value=ib_mock):
            result = await place_combo_order(
                ticker="SPY", legs=_LEGS, contracts=1,
                limit_price=2.20, session_id="test-session", timeout=2.0,
            )
        assert result["status"] == "Filled"
        assert result["avg_price"] == pytest.approx(2.15)
        assert len(result["fills"]) == 1

    @pytest.mark.asyncio
    async def test_cancelled_order_raises(self):
        ib_mock, _ = _make_ib_mock(order_status="Cancelled")
        with patch("trading_platform.services.ibkr_client.IB", return_value=ib_mock):
            with pytest.raises(RuntimeError, match="rejected"):
                await place_combo_order(
                    ticker="SPY", legs=_LEGS, contracts=1,
                    limit_price=2.20, session_id="test-session", timeout=2.0,
                )
        ib_mock.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_connection_failure_raises(self):
        ib_mock, _ = _make_ib_mock()
        ib_mock.connectAsync = AsyncMock(side_effect=ConnectionRefusedError("no IB Gateway"))
        ib_mock.isConnected.return_value = False
        with patch("trading_platform.services.ibkr_client.IB", return_value=ib_mock):
            with pytest.raises(ConnectionRefusedError):
                await place_combo_order(
                    ticker="SPY", legs=_LEGS, contracts=1,
                    limit_price=2.20, session_id="test-session",
                )

    @pytest.mark.asyncio
    async def test_qualify_failure_raises(self):
        ib_mock, _ = _make_ib_mock()
        ib_mock.qualifyContractsAsync = AsyncMock(return_value=[])
        with patch("trading_platform.services.ibkr_client.IB", return_value=ib_mock):
            with pytest.raises(RuntimeError, match="Could not qualify"):
                await place_combo_order(
                    ticker="SPY", legs=_LEGS, contracts=1,
                    limit_price=2.20, session_id="test-session",
                )
        ib_mock.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_credit_spread_uses_sell_action(self):
        """Negative limit_price (credit received) → order action must be SELL."""
        ib_mock, _ = _make_ib_mock(order_status="Submitted")
        captured: list = []

        def _capture(contract, order):
            captured.append(order)
            t = MagicMock()
            t.order.orderId = 99
            t.orderStatus.status = "Submitted"
            t.fills = []
            return t

        ib_mock.placeOrder.side_effect = _capture

        with patch("trading_platform.services.ibkr_client.IB", return_value=ib_mock):
            await place_combo_order(
                ticker="SPY", legs=_LEGS, contracts=1,
                limit_price=-0.80, session_id="test-session", timeout=2.0,
            )

        assert captured[0].action == "SELL"

    @pytest.mark.asyncio
    async def test_session_id_written_to_order_ref(self):
        """session_id (truncated to 40 chars) must appear in order.orderRef."""
        ib_mock, _ = _make_ib_mock(order_status="Submitted")
        captured: list = []

        def _capture(contract, order):
            captured.append(order)
            t = MagicMock()
            t.order.orderId = 1
            t.orderStatus.status = "Submitted"
            t.fills = []
            return t

        ib_mock.placeOrder.side_effect = _capture
        session_id = "abc-1234-very-long-session-id-that-exceeds-forty-chars-surely"

        with patch("trading_platform.services.ibkr_client.IB", return_value=ib_mock):
            await place_combo_order(
                ticker="SPY", legs=_LEGS, contracts=1,
                limit_price=2.20, session_id=session_id, timeout=2.0,
            )

        ref = captured[0].orderRef
        assert len(ref) <= 40
        assert session_id[:40] == ref

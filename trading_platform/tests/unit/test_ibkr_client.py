"""
Unit tests for the IBKR client service.

ib_insync is mocked at the IB class level — no TWS or IB Gateway needed.
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from trading_platform.services import ibkr_client as _ibkr_mod
from trading_platform.services.ibkr_client import _next_expiry, place_bracket_order


@pytest.fixture(autouse=True)
def _instant_sleep(monkeypatch):
    """Collapse every ``await asyncio.sleep(...)`` inside ibkr_client to a no-op.

    ``place_bracket_order`` now runs a price-walk loop that rests the limit for
    ``_PRICE_STEP_SEC`` seconds per step over up to ``_MAX_WALK_STEPS`` steps via
    real ``asyncio.sleep(1)`` calls (plus a ~6s market-data wait). Without this the
    suite spins for minutes. We replace the module's ``asyncio.sleep`` with an
    awaitable no-op so the walk reaches its terminal state immediately, leaving the
    order-status the mock feeds as the only thing that drives the outcome.
    """
    async def _noop(*_a, **_kw):
        return None

    monkeypatch.setattr(_ibkr_mod.asyncio, "sleep", _noop)


@pytest.fixture(autouse=True)
def _clear_conid_cache():
    """The legs below reuse the same strikes across tests; the module caches resolved
    conIds globally, so a prior test's successful qualify would let a later test skip
    the qualify round-trip. Clear it around every test for deterministic qualify paths."""
    _ibkr_mod._CONID_CACHE.clear()
    yield
    _ibkr_mod._CONID_CACHE.clear()


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

def _make_ib_mock(order_status: str = "Submitted", fills: list | None = None,
                  filled_qty: int = 0):
    """Build a realistic ib_insync.IB mock for the current ``place_bracket_order``.

    The modern entry path: connect → qualify legs → fetch combo pricing via
    ``reqMktData`` → place a BAG limit → walk the limit toward the natural until the
    order reaches a terminal status, repricing with repeated ``placeOrder`` calls.

    Mock contract:
      * ``reqMktData`` returns READY per-leg quotes (real bid/ask floats) so the combo
        pricing fetch's ``_all_ready()`` is true on the first poll and its 6s market-data
        wait exits immediately — without valid quotes that loop busy-spins the full
        wall-clock deadline (the ``asyncio.sleep`` no-op doesn't advance ``loop.time()``).
        The two legs (BUY 503C mid 3.10, SELL 508C mid 0.90) net to +2.20, matching the
        yfinance ``entry_price`` so the pricing-sanity and liquidity gates both pass and
        the order proceeds into the walk.
      * ``orderStatus.filled`` is a REAL int and ``trade.log`` a REAL list, because
        the cancel / terminal branches call ``int(orderStatus.filled)`` and
        ``list(trade.log)`` — a bare MagicMock would raise there.
      * ``placeOrder`` returns the SAME parent trade for every call (entry + every
        reprice), so the walk's repeated ``placeOrder`` never exhausts a side_effect.
    """
    ib = MagicMock()
    ib.isConnected.return_value = True
    ib.connectAsync = AsyncMock()

    qualified_contract = MagicMock()
    qualified_contract.conId = 999_001
    ib.qualifyContractsAsync = AsyncMock(return_value=[qualified_contract])

    # Ready, liquid per-leg quotes. reqMktData is called once per leg (in fetch order:
    # BUY 503C then SELL 508C); cycle the two quotes so each leg gets the right side.
    def _quote(bid: float, ask: float) -> MagicMock:
        q = MagicMock()
        q.bid, q.ask = bid, ask
        q.modelGreeks = None
        q.contract = qualified_contract
        return q

    leg_quotes = [_quote(3.05, 3.15), _quote(0.85, 0.95)]  # mids 3.10 / 0.90 → net +2.20
    ib.reqMktData.side_effect = lambda *a, **k: leg_quotes[
        (ib.reqMktData.call_count - 1) % len(leg_quotes)
    ]
    ib.cancelMktData = MagicMock()

    parent_trade = MagicMock()
    parent_trade.order.orderId = 42
    parent_trade.orderStatus.status = order_status
    parent_trade.orderStatus.filled = filled_qty
    parent_trade.fills = fills or []
    parent_trade.log = []   # iterated by the rejection/terminal branch

    # Every placeOrder (entry + each reprice in the walk) returns the parent trade.
    ib.placeOrder.return_value = parent_trade
    ib.cancelOrder = MagicMock()
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
        """A BAG entry that FILLS during the walk returns a fully-populated status dict.

        Rewritten for the current price-walk path: the only non-cancel terminal state
        the walk can settle on is ``Filled`` (an order that merely stays 'Submitted'
        and never fills is cancelled at the end of the walk — covered by
        ``test_unfilled_order_returns_cancelled``). We therefore model a filled order
        and assert the CURRENT return shape: status + order_id + the net fill prices
        and the new ``net_entry_signed`` field (signed +debit / -credit real fill)."""
        fill = MagicMock()
        fill.execution.execId = "exec-walk-001"
        fill.execution.shares = 1
        fill.execution.price = 2.18
        fill.execution.time = None
        fill.contract.conId = 999_001

        ib_mock, _ = _make_ib_mock(order_status="Filled", fills=[fill], filled_qty=1)
        with patch("trading_platform.services.ibkr_client.IB", return_value=ib_mock):
            result = await place_bracket_order(
                ticker="SPY", legs=_LEGS, contracts=1,
                entry_price=2.20, profit_target=5.00, stop_loss=1.10,
                session_id="test-session", timeout=2.0,
            )
        assert result["status"] == "Filled"
        assert result["order_id"] == 42
        # Current return shape carries the signed real fill alongside the net price.
        assert "net_entry_signed" in result
        assert result["net_fill_price"] == result["entry_price"]
        # Both legs share conId 999001 in the mock, so the signed net collapses to the
        # single fill price; the magnitude is what flows to entry_price.
        assert result["entry_price"] == pytest.approx(2.18, abs=1e-4)

    @pytest.mark.asyncio
    async def test_unfilled_order_returns_cancelled(self):
        """An order that stays working through the whole walk is cancelled, not held.

        The walk reprices toward the natural and, if the status never reaches a
        terminal state, cancels the remainder at the end. With no fills this surfaces
        as a Cancelled dict carrying the parent order_id and an 'Unfilled after
        walking' reason — NOT a 'Submitted' status (the old expectation)."""
        ib_mock, _ = _make_ib_mock(order_status="Submitted")
        with patch("trading_platform.services.ibkr_client.IB", return_value=ib_mock):
            result = await place_bracket_order(
                ticker="SPY", legs=_LEGS, contracts=1,
                entry_price=2.20, profit_target=5.00, stop_loss=1.10,
                session_id="test-session", timeout=2.0,
            )
        assert result["status"] == "Cancelled"
        assert result["order_id"] == 42
        assert result["fills"] == []
        assert "Unfilled" in result["reason"]
        ib_mock.cancelOrder.assert_called_once()

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
    async def test_cancelled_order_returns_cancelled_status(self):
        """A broker-rejected/cancelled BAG surfaces as a Cancelled status dict.

        Behavior change vs. the old test: ``place_bracket_order`` no longer RAISES on
        a rejected order — the walk detects the terminal Cancelled/ApiCancelled/Inactive
        status, logs the TWS reason, and RETURNS a structured Cancelled dict so the
        caller can record the non-fill instead of crashing the session. The intent
        ('a cancelled order is surfaced as a non-fill') is preserved; we assert the
        documented current outcome and that the connection is still cleaned up."""
        ib_mock, _ = _make_ib_mock(order_status="Cancelled")
        with patch("trading_platform.services.ibkr_client.IB", return_value=ib_mock):
            result = await place_bracket_order(
                ticker="SPY", legs=_LEGS, contracts=1,
                entry_price=2.20, profit_target=5.00, stop_loss=1.10,
                session_id="test-session", timeout=2.0,
            )
        assert result["status"] == "Cancelled"
        assert result["order_id"] == 42
        assert result["fills"] == []
        assert "reason" in result
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

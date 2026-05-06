"""
IBKR execution client — wraps ib_insync for asyncio compatibility.

Handles option combo order submission for paper and live accounts via
IB Gateway or TWS. Connect once per trade; disconnect when done.

Ports:
  TWS paper:        7497
  TWS live:         7496
  IB Gateway paper: 4002
  IB Gateway live:  4001

Prerequisites:
  - IB Gateway or TWS running locally with API connections enabled
  - Socket port matches ibkr_port in settings
  - "Allow connections from localhost only" recommended for security
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta
from typing import Any

logger = logging.getLogger(__name__)

try:
    from ib_insync import IB, Option, Contract, ComboLeg, LimitOrder
    _IB_AVAILABLE = True
except ImportError:
    IB = Option = Contract = ComboLeg = LimitOrder = None  # type: ignore[assignment,misc]
    _IB_AVAILABLE = False


def _next_expiry(dte: int) -> str:
    """
    Convert DTE to the nearest valid option expiration date string (YYYYMMDD).

    SPY and QQQ have weekly expirations every Friday. We find the Friday
    on or after (today + dte). Monthly options expire the 3rd Friday.
    For simplicity, we target the first Friday ≥ today + dte.
    """
    target = date.today() + timedelta(days=dte)
    # Advance to Friday (weekday 4) if not already Friday
    days_until_friday = (4 - target.weekday()) % 7
    expiry = target + timedelta(days=days_until_friday)
    return expiry.strftime("%Y%m%d")


async def place_combo_order(
    *,
    ticker: str,
    legs: list[dict[str, Any]],
    contracts: int,
    limit_price: float,
    session_id: str,
    host: str = "127.0.0.1",
    port: int = 7497,
    client_id: int = 1,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """
    Submit a multi-leg option combo order to IBKR.

    Parameters
    ----------
    ticker:      Underlying symbol (e.g. "SPY")
    legs:        List of leg dicts: {option_type, strike, expiration_dte, action, quantity}
    contracts:   Number of combos (applied to all legs proportionally)
    limit_price: Net debit (positive) or credit (negative) for the combo
    session_id:  Tracing ID written to IBKR order reference field
    host/port/client_id: IB Gateway connection params
    timeout:     Seconds to wait for order acknowledgement

    Returns
    -------
    dict with keys: order_id, status, fills, avg_price
    """
    if not _IB_AVAILABLE:
        raise RuntimeError("ib_insync not installed. Run: pip install ib_insync")

    ib = IB()

    try:
        logger.info(
            "[%s] Connecting to IB Gateway at %s:%d (client_id=%d)",
            session_id, host, port, client_id,
        )
        await ib.connectAsync(host, port, clientId=client_id, timeout=10)

        # ── Build and qualify each option leg ──────────────────────────────
        qualified_legs: list[tuple[dict, Any]] = []  # (leg_spec, qualified_contract)
        for leg in legs:
            opt = Option(
                symbol=ticker,
                lastTradeDateOrContractMonth=_next_expiry(leg["expiration_dte"]),
                strike=float(leg["strike"]),
                right="C" if leg["option_type"].lower() == "call" else "P",
                exchange="SMART",
                currency="USD",
                multiplier="100",
            )
            qualified = await ib.qualifyContractsAsync(opt)
            if not qualified:
                raise RuntimeError(
                    f"Could not qualify contract: {ticker} "
                    f"{leg['option_type']} {leg['strike']} "
                    f"exp {_next_expiry(leg['expiration_dte'])}"
                )
            qualified_legs.append((leg, qualified[0]))
            logger.debug(
                "[%s] Qualified %s %s %.1f → conId=%d",
                session_id, ticker, leg["option_type"], leg["strike"],
                qualified[0].conId,
            )

        # ── Build BAG combo contract ────────────────────────────────────────
        bag = Contract()
        bag.symbol = ticker
        bag.secType = "BAG"
        bag.currency = "USD"
        bag.exchange = "SMART"
        bag.comboLegs = [
            ComboLeg(
                conId=contract.conId,
                ratio=leg_spec["quantity"],
                action=leg_spec["action"].upper(),
                exchange="SMART",
            )
            for leg_spec, contract in qualified_legs
        ]

        # ── Place limit order ───────────────────────────────────────────────
        # Positive limit_price = debit paid; IBKR uses BUY for debit spreads.
        order_action = "BUY" if limit_price > 0 else "SELL"
        order = LimitOrder(
            action=order_action,
            totalQuantity=contracts,
            lmtPrice=abs(round(limit_price, 2)),
        )
        order.orderRef = session_id[:40]  # IBKR max 40 chars
        order.tif = "DAY"                 # expire at end of session
        order.transmit = True

        trade = ib.placeOrder(bag, order)
        logger.info(
            "[%s] Order submitted — orderId=%d action=%s qty=%d lmt=%.2f",
            session_id, trade.order.orderId, order_action, contracts, order.lmtPrice,
        )

        # ── Wait for acknowledgement / partial fill ─────────────────────────
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(1)
            ib.sleep(0)  # drive ib_insync event loop
            status = trade.orderStatus.status
            if status in ("Filled", "Submitted", "PreSubmitted"):
                break
            if status in ("Cancelled", "ApiCancelled", "Inactive"):
                raise RuntimeError(f"Order {trade.order.orderId} rejected: status={status}")

        fills = [
            {
                "exec_id": f.execution.execId,
                "shares": f.execution.shares,
                "price": f.execution.price,
                "time": f.execution.time.isoformat() if f.execution.time else None,
            }
            for f in trade.fills
        ]
        avg_price = (
            sum(f["shares"] * f["price"] for f in fills) / sum(f["shares"] for f in fills)
            if fills else None
        )

        return {
            "order_id": trade.order.orderId,
            "status": trade.orderStatus.status,
            "fills": fills,
            "avg_price": avg_price,
        }

    finally:
        if ib.isConnected():
            ib.disconnect()
        logger.debug("[%s] IBKR connection closed", session_id)

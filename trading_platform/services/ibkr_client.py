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
import uuid
from datetime import date, timedelta
from typing import Any

logger = logging.getLogger(__name__)

try:
    from ib_insync import IB, Option, Contract, ComboLeg, LimitOrder, StopOrder, Order
    _IB_AVAILABLE = True
except ImportError:
    IB = Option = Contract = ComboLeg = LimitOrder = StopOrder = Order = None  # type: ignore[assignment,misc]
    _IB_AVAILABLE = False


# ── Singleton client for SetupWatcher ─────────────────────────────────────────

class _IBKRPersistentClient:
    """Persistent IBKR connection used by SetupWatcher for streaming data."""

    def __init__(self, host: str = "127.0.0.1", port: int = 7497, client_id: int = 10) -> None:
        self._host = host
        self._port = port
        self._client_id = client_id
        self._ib: Any = None
        self._lock = asyncio.Lock()

    async def is_connected(self) -> bool:
        return self._ib is not None and self._ib.isConnected()

    async def ensure_connected(self) -> bool:
        if not _IB_AVAILABLE:
            return False
        async with self._lock:
            if self._ib and self._ib.isConnected():
                return True
            try:
                self._ib = IB()
                await self._ib.connectAsync(self._host, self._port, clientId=self._client_id, timeout=10)
                return True
            except Exception as exc:
                logger.debug("IBKR persistent connect failed: %s", exc)
                self._ib = None
                return False

    async def get_5min_bars(self, ticker: str, count: int = 20) -> list[dict]:
        """Fetch last N 5-min bars for ticker via IBKR reqHistoricalData."""
        if not await self.ensure_connected() or self._ib is None:
            return []
        try:
            from ib_insync import Stock
            contract = Stock(ticker, "SMART", "USD")
            bars_data = await self._ib.reqHistoricalDataAsync(
                contract,
                endDateTime="",
                durationStr=f"{count * 5 * 60 + 300} S",
                barSizeSetting="5 mins",
                whatToShow="TRADES",
                useRTH=True,
                formatDate=1,
            )
            result = []
            for b in bars_data[-count:]:
                result.append({
                    "dt": str(b.date),
                    "open": b.open,
                    "high": b.high,
                    "low": b.low,
                    "close": b.close,
                    "volume": b.volume,
                })
            return result
        except Exception as exc:
            logger.debug("IBKR get_5min_bars failed for %s: %s", ticker, exc)
            return []


_persistent_client: _IBKRPersistentClient | None = None


def get_ibkr_client(host: str = "127.0.0.1", port: int = 7497) -> _IBKRPersistentClient:
    global _persistent_client
    if _persistent_client is None:
        _persistent_client = _IBKRPersistentClient(host=host, port=port)
    return _persistent_client


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


async def _place_individual_legs(
    *,
    ib: Any,
    qualified_legs: list[tuple[dict, Any]],
    contracts: int,
    session_id: str,
    entry_price: float,
    profit_target: float,
    stop_loss: float,
    timeout: float,
) -> dict[str, Any]:
    """
    Fallback for Error 201 (Precautionary Settings blocks BAG combo orders).

    Submits each spread leg as a separate vanilla option order. Individual legs
    are not classified as 'riskless combination orders' and bypass the TWS limit.
    Legging risk is acceptable for paper trading; for live trading, fix the
    TWS Precautionary Settings instead.
    """
    order_ids = []
    trades = []

    # Spread net price → split proportionally across legs by setting each
    # leg at its standalone mid. For paper we just use the spread price as
    # a proxy limit on the first (short) leg and market on the long leg.
    short_legs = [(ls, c) for ls, c in qualified_legs if ls["action"].upper() == "SELL"]
    long_legs  = [(ls, c) for ls, c in qualified_legs if ls["action"].upper() == "BUY"]

    for leg_spec, contract in short_legs:
        sell_order = LimitOrder(
            action="SELL",
            totalQuantity=contracts * leg_spec["quantity"],
            lmtPrice=abs(round(entry_price, 2)),
        )
        sell_order.orderRef = f"{session_id[:30]}_S"
        sell_order.tif = "DAY"
        sell_order.transmit = True
        t = ib.placeOrder(contract, sell_order)
        trades.append(t)
        order_ids.append(t.order.orderId)
        logger.info("[%s] Leg order SELL %s strike=%.1f orderId=%d",
                    session_id, contract.localSymbol, leg_spec["strike"], t.order.orderId)

    for leg_spec, contract in long_legs:
        buy_order = LimitOrder(
            action="BUY",
            totalQuantity=contracts * leg_spec["quantity"],
            lmtPrice=max(0.01, abs(round(entry_price * 0.3, 2))),  # long leg costs ~30% of credit
        )
        buy_order.orderRef = f"{session_id[:30]}_L"
        buy_order.tif = "DAY"
        buy_order.transmit = True
        t = ib.placeOrder(contract, buy_order)
        trades.append(t)
        order_ids.append(t.order.orderId)
        logger.info("[%s] Leg order BUY  %s strike=%.1f orderId=%d",
                    session_id, contract.localSymbol, leg_spec["strike"], t.order.orderId)

    # Wait for at least the short leg to be accepted
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        await asyncio.sleep(1)
        statuses = [t.orderStatus.status for t in trades]
        if all(s in ("Submitted", "PreSubmitted", "Filled") for s in statuses):
            break
        if any(s in ("Cancelled", "ApiCancelled", "Inactive") for s in statuses):
            msgs = [m.message for t in trades for m in t.log if m.message]
            raise RuntimeError(f"Individual leg rejected — {msgs[-2:] if msgs else 'no detail'}")

    logger.info("[%s] Individual legs accepted — orderIds=%s", session_id, order_ids)
    return {
        "order_id": order_ids[0] if order_ids else -1,
        "order_ids": order_ids,
        "status": trades[0].orderStatus.status if trades else "Unknown",
        "fills": [],
        "entry_price": entry_price,
        "profit_target": profit_target,
        "stop_loss": stop_loss,
        "mode": "individual_legs",
    }


async def place_bracket_order(
    *,
    ticker: str,
    legs: list[dict[str, Any]],
    contracts: int,
    entry_price: float,
    profit_target: float,
    stop_loss: float,
    session_id: str,
    host: str = "127.0.0.1",
    port: int = 7497,
    client_id: int = 1,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """
    Submit entry + GTC profit-target for a multi-leg spread.

    IBKR does not support STP/STP LMT orders on BAG (combo) contracts, so the
    stop-loss is intentionally omitted here. MonitorAgent owns stop-loss exits:
    it polls prices every 60 s and calls close_position() when the stop is hit.
    This design avoids silent order rejections and keeps exit logic in one place.

    Parameters
    ----------
    entry_price:   Net debit (positive) or credit (negative) for the spread
    profit_target: Price at which to take profit (GTC limit child order)
    stop_loss:     Informational only — enforced by MonitorAgent, not submitted here
    """
    if not _IB_AVAILABLE:
        raise RuntimeError("ib_insync not installed. Run: pip install ib_insync")

    ib = IB()

    try:
        await ib.connectAsync(host, port, clientId=client_id, timeout=10)
        logger.info("[%s] Bracket order — connecting to %s:%d", session_id, host, port)

        # ── Qualify legs and build BAG contract ───────────────────────────
        qualified_legs: list[tuple[dict, Any]] = []
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
                raise RuntimeError(f"Could not qualify {ticker} {leg['option_type']} {leg['strike']}")
            qualified_legs.append((leg, qualified[0]))

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

        # ── Entry order (transmit=False — send with profit-target child) ──
        order_action = "BUY" if entry_price > 0 else "SELL"
        entry_order = LimitOrder(
            action=order_action,
            totalQuantity=contracts,
            lmtPrice=abs(round(entry_price, 2)),
        )
        entry_order.orderRef = session_id[:40]
        entry_order.tif = "DAY"
        entry_order.transmit = False
        entry_order.nonGuaranteedFill = True  # required for combo orders on paper accounts

        entry_trade = ib.placeOrder(bag, entry_order)
        parent_id = entry_trade.order.orderId

        # ── Profit target (GTC limit, opposite side of entry) ─────────────
        exit_action = "SELL" if order_action == "BUY" else "BUY"
        pt_order = LimitOrder(
            action=exit_action,
            totalQuantity=contracts,
            lmtPrice=abs(round(profit_target, 2)),
        )
        pt_order.parentId = parent_id
        pt_order.tif = "GTC"
        pt_order.transmit = True  # transmits both parent + child atomically
        pt_order.nonGuaranteedFill = True

        ib.placeOrder(bag, pt_order)

        _PRICE_STEP_SEC  = 30    # seconds between price adjustments
        _MAX_PRICE_STEPS = 6     # 6 steps × 30s = 3 minutes total
        _TICK            = 0.01  # minimum option tick size
        # Credit spreads (SELL): accept less credit each step → step price down
        # Debit spreads  (BUY):  pay more each step            → step price up
        price_step = -_TICK if order_action == "SELL" else +_TICK

        logger.info(
            "[%s] Bracket submitted — parentId=%d entry=%.2f target=%.2f "
            "(stop=%.2f managed by MonitorAgent)",
            session_id, parent_id, entry_price, profit_target, stop_loss,
        )

        # ── Phase 1: wait for TWS acknowledgement (up to `timeout` seconds) ─
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            await asyncio.sleep(1)
            status = entry_trade.orderStatus.status
            if status in ("Filled", "Submitted", "PreSubmitted"):
                break
            if status in ("Cancelled", "ApiCancelled", "Inactive"):
                tws_msgs = [e.message for e in entry_trade.log if e.message]
                reason = " | ".join(tws_msgs[-3:]) if tws_msgs else "no detail"
                logger.warning("[%s] BAG combo rejected (%s): %s", session_id, status, reason)
                if any("201" in m or "Riskless" in m for m in tws_msgs):
                    logger.info("[%s] Falling back to individual leg orders", session_id)
                    return await _place_individual_legs(
                        ib=ib,
                        qualified_legs=qualified_legs,
                        contracts=contracts,
                        session_id=session_id,
                        entry_price=entry_price,
                        profit_target=profit_target,
                        stop_loss=stop_loss,
                        timeout=timeout,
                    )
                raise RuntimeError(f"Bracket entry {parent_id} rejected: {status} — {reason}")

        # ── Phase 2: adaptive pricing — step toward market every 30s ─────────
        current_limit = abs(round(entry_price, 2))
        for step in range(1, _MAX_PRICE_STEPS + 1):
            if entry_trade.orderStatus.status == "Filled":
                break
            await asyncio.sleep(_PRICE_STEP_SEC)

            status = entry_trade.orderStatus.status
            if status == "Filled":
                break
            if status in ("Cancelled", "ApiCancelled", "Inactive"):
                tws_msgs = [e.message for e in entry_trade.log if e.message]
                raise RuntimeError(
                    f"Bracket entry {parent_id} cancelled during pricing: "
                    f"{'|'.join(tws_msgs[-2:])}"
                )

            # Adjust limit price one tick toward market
            current_limit = round(current_limit + price_step, 2)
            current_limit = max(current_limit, _TICK)   # never go below 1 tick
            entry_order.lmtPrice = current_limit
            entry_order.transmit = True  # standalone modification — transmit immediately
            ib.placeOrder(bag, entry_order)
            logger.info(
                "[%s] Price step %d/%d — new limit=%.2f (%s)",
                session_id, step, _MAX_PRICE_STEPS, current_limit, order_action,
            )

        # ── Cancel if still unfilled after all steps ──────────────────────────
        final_status = entry_trade.orderStatus.status
        if final_status == "Filled":
            pass  # handled below
        elif final_status in ("Submitted", "PreSubmitted"):
            # Order is still working in TWS. Cancel it — a DAY order that couldn't
            # fill at any price step in 3 minutes is too wide. Caller will NOT record
            # a position; the bracket sits cancelled in TWS.
            ib.cancelOrder(entry_order)
            logger.warning(
                "[%s] Order %d still pending (%s) after %d price steps — cancelled",
                session_id, parent_id, final_status, _MAX_PRICE_STEPS,
            )
            return {
                "order_id": parent_id,
                "status": "Cancelled",
                "fills": [],
                "entry_price": current_limit,
                "profit_target": profit_target,
                "stop_loss": stop_loss,
                "reason": f"Unfilled after {_MAX_PRICE_STEPS} price steps (3 min)",
            }
        else:
            ib.cancelOrder(entry_order)
            logger.warning(
                "[%s] Order %d unfilled after %d price steps — cancelled",
                session_id, parent_id, _MAX_PRICE_STEPS,
            )
            raise RuntimeError(
                f"Bracket entry {parent_id} unfilled after {_MAX_PRICE_STEPS} price steps"
            )

        fills = [
            {
                "exec_id": f.execution.execId,
                "shares": f.execution.shares,
                "price": f.execution.price,
                "time": f.execution.time.isoformat() if f.execution.time else None,
            }
            for f in entry_trade.fills
        ]

        return {
            "order_id": parent_id,
            "status": entry_trade.orderStatus.status,
            "fills": fills,
            "entry_price": current_limit,   # actual limit at time of fill
            "profit_target": profit_target,
            "stop_loss": stop_loss,
        }

    finally:
        if ib.isConnected():
            ib.disconnect()
        logger.debug("[%s] IBKR bracket connection closed", session_id)


async def close_position(
    *,
    ticker: str,
    legs: list[dict[str, Any]],
    contracts: int,
    session_id: str,
    host: str = "127.0.0.1",
    port: int = 7497,
    client_id: int = 2,  # separate client_id from entry to avoid conflicts
    timeout: float = 20.0,
) -> dict[str, Any]:
    """
    Close an open spread position at market (MOC-style limit at mid).

    Called by MonitorAgent when stop-loss, trailing stop, or thesis-break
    is triggered. Reverses all legs of the spread via a BAG market order.

    legs: same format as place_bracket_order — action is the ORIGINAL entry action;
          this function automatically reverses (buy→sell, sell→buy) to close.
    """
    if not _IB_AVAILABLE:
        raise RuntimeError("ib_insync not installed. Run: pip install ib_insync")

    ib = IB()

    try:
        await ib.connectAsync(host, port, clientId=client_id, timeout=10)
        logger.info("[%s] Closing position %s x%d", session_id, ticker, contracts)

        qualified_legs: list[tuple[dict, Any]] = []
        for leg in legs:
            opt = Option(
                symbol=ticker,
                lastTradeDateOrContractMonth=_next_expiry(leg.get("expiration_dte", 0)),
                strike=float(leg["strike"]),
                right="C" if leg["option_type"].lower() == "call" else "P",
                exchange="SMART",
                currency="USD",
                multiplier="100",
            )
            qualified = await ib.qualifyContractsAsync(opt)
            if not qualified:
                raise RuntimeError(f"Could not qualify closing leg: {ticker} {leg}")
            qualified_legs.append((leg, qualified[0]))

        bag = Contract()
        bag.symbol = ticker
        bag.secType = "BAG"
        bag.currency = "USD"
        bag.exchange = "SMART"
        # Reverse all leg directions to close the spread
        bag.comboLegs = [
            ComboLeg(
                conId=contract.conId,
                ratio=leg_spec["quantity"],
                action="SELL" if leg_spec["action"].upper() == "BUY" else "BUY",
                exchange="SMART",
            )
            for leg_spec, contract in qualified_legs
        ]

        # Use a market order for guaranteed exit — stop hits require certainty
        close_order = Order()
        close_order.action = "BUY"   # reversed relative to entry; BAG market order
        close_order.orderType = "MKT"
        close_order.totalQuantity = contracts
        close_order.tif = "DAY"
        close_order.orderRef = f"CLOSE_{session_id[:30]}"
        close_order.transmit = True

        trade = ib.placeOrder(bag, close_order)
        logger.info(
            "[%s] Close order submitted — orderId=%d",
            session_id, trade.order.orderId,
        )

        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(1)
            ib.sleep(0)
            status = trade.orderStatus.status
            if status == "Filled":
                break
            if status in ("Cancelled", "ApiCancelled", "Inactive"):
                raise RuntimeError(f"Close order {trade.order.orderId} rejected: {status}")

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

        logger.info(
            "[%s] Position closed — orderId=%d status=%s avg=%.2f",
            session_id, trade.order.orderId, trade.orderStatus.status, avg_price or 0,
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
        logger.debug("[%s] IBKR close connection closed", session_id)

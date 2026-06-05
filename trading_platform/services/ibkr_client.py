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
    from ib_insync import IB, Option, Contract, ComboLeg, LimitOrder, StopOrder, Order, TagValue
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


def _apply_adaptive_algo(order: Any, priority: str = "Normal") -> None:
    """Attach IBKR's Adaptive (Price Management) algo to an order in place.

    Root cause of the 100%-timeout execution failure (diagnosed 2026-06-04 via TWS):
    AGORA's limit prices landed >3% from the reference price, tripping IBKR's regulatory
    price collar — orders were rejected / stuck at PendingSubmit and never filled. The
    Adaptive algo lets IBKR manage the price SERVER-SIDE (using its own market data, so it
    works even without a client market-data subscription) to fill at a fair price within the
    collar. This is the programmatic equivalent of TWS's "Use Price Management Algo" prompt.

    Defensive: never let an algo-attach error block a trade — the order still submits plain.
    """
    try:
        if priority not in ("Urgent", "Normal", "Patient"):
            priority = "Normal"
        order.algoStrategy = "Adaptive"
        order.algoParams = [TagValue("adaptivePriority", priority)]
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Could not attach Adaptive algo (%s) — submitting order plain", exc)


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
    price_step_size: float = 0.05,
    use_adaptive_algo: bool = False,
    adaptive_algo_priority: str = "Normal",
    max_slippage_pct_of_width: float = 0.10,
) -> dict[str, Any]:
    """
    Submit a multi-leg spread entry as an atomic BAG combo order, priced at the
    net mid and repriced toward the marketable (natural) price until it fills or
    a slippage budget is exhausted.

    IBKR does not support STP/STP LMT orders on BAG (combo) contracts, so the
    stop-loss is intentionally omitted here. PositionManager owns stop-loss exits:
    it polls prices every 60 s and calls close_position() when the stop is hit.
    This design avoids silent order rejections and keeps exit logic in one place.

    Execution model (VERIFIED 2026-06-05)
    -------------------------------------
    There is no algo shortcut for filling option combos: IBKR's Adaptive algo is
    silently IGNORED on BAG orders (it only applies to single legs), and native
    combo books exist only on ISE/ONE/DTB — SMART-routed US combos are legged for
    best execution, so the only lever is the net limit price. We therefore start
    at the net mid (the price we have from yfinance) and walk it toward the natural
    in `price_step_size` increments, capped at `max_slippage_pct_of_width` × the
    spread's strike width. Adaptive is left off for combos by default.

    Parameters
    ----------
    entry_price:    Net debit (positive) or credit (negative) per share for the spread
    profit_target:  Informational — PositionManager owns the profit exit (no TWS child)
    stop_loss:      Informational only — enforced by PositionManager, not submitted here
    price_step_size: Dollars to step the net limit toward the natural each interval.
    max_slippage_pct_of_width: Walk budget as a fraction of strike width. A $5-wide
                    vertical at 0.10 gives $0.50 of room before the order is cancelled.
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

        # ── Slippage budget from the spread's strike width ──────────────────
        _TICK = 0.01  # minimum options tick size
        strikes = [float(ls["strike"]) for ls, _ in qualified_legs]
        width = (max(strikes) - min(strikes)) if len(strikes) >= 2 else 0.0
        # Calendars / same-strike combos have no strike width — fall back to a
        # fraction of the net mid so the walk still has somewhere to go.
        slippage_budget = round(
            (width * max_slippage_pct_of_width) if width > 0
            else max(price_step_size, abs(entry_price) * max_slippage_pct_of_width),
            2,
        )

        # ── Entry: net limit at the mid, no GTC child, no Adaptive on a BAG ──
        # GTC children accumulate across sessions → Error 201; PositionManager owns
        # all exits. Adaptive is silently ignored on combos (verified 06-05), so we
        # attach it ONLY to a genuine single leg.
        order_action = "BUY" if entry_price > 0 else "SELL"
        mid_limit = abs(round(entry_price, 2))
        entry_order = LimitOrder(
            action=order_action,
            totalQuantity=contracts,
            lmtPrice=mid_limit,
        )
        entry_order.orderRef = session_id[:40]
        entry_order.tif = "DAY"
        entry_order.transmit = True
        entry_order.nonGuaranteedFill = True  # required for SMART-routed combos
        if use_adaptive_algo and len(qualified_legs) == 1:
            _apply_adaptive_algo(entry_order, adaptive_algo_priority)

        entry_trade = ib.placeOrder(bag, entry_order)
        parent_id = entry_trade.order.orderId

        # Walk direction & natural bound. Debit (BUY) pays more toward the ask;
        # credit (SELL) accepts less credit toward the bid. Clamp so a debit never
        # exceeds the spread's max value and a credit never gives up more than half.
        step_dir = +1 if order_action == "BUY" else -1
        if order_action == "BUY":
            natural = mid_limit + slippage_budget
            if width > 0:
                natural = min(natural, width * 0.99)  # can't pay more than it can be worth
        else:
            natural = max(mid_limit - slippage_budget, mid_limit * 0.5, _TICK)
        natural = round(natural, 2)
        walk_room = abs(natural - mid_limit)
        # Bound the walk to a fixed number of reprices so a wide spread can't hog the
        # single-threaded IBKR executor for 10+ min. Size each step to cover the whole
        # budget within _MAX_WALK_STEPS (so the per-step move grows with width).
        _MAX_WALK_STEPS = 12
        _PRICE_STEP_SEC = 20  # seconds the limit rests before the next reprice (~4 min max)
        max_steps = max(1, min(_MAX_WALK_STEPS, int(round(walk_room / max(price_step_size, _TICK)))))
        eff_step = (max(price_step_size, round(walk_room / max_steps, 2))
                    if walk_room > 0 else price_step_size)

        logger.info(
            "[%s] BAG entry submitted — orderId=%d action=%s mid=%.2f walk->%.2f "
            "(width=%.2f budget=%.2f steps=%d) | stop=%.2f managed by PositionManager",
            session_id, parent_id, order_action, mid_limit, natural,
            width, slippage_budget, max_steps, stop_loss,
        )

        # ── Repricing walk: mid → natural until filled or budget exhausted ──
        current_limit = mid_limit
        # One extra interval so the order rests at `natural` before we give up.
        for step in range(max_steps + 1):
            # Let the limit rest for one interval, breaking early on a terminal status.
            for _ in range(_PRICE_STEP_SEC):
                await asyncio.sleep(1)
                if entry_trade.orderStatus.status in (
                    "Filled", "Cancelled", "ApiCancelled", "Inactive"
                ):
                    break

            status = entry_trade.orderStatus.status
            if status == "Filled":
                break
            if status in ("Cancelled", "ApiCancelled", "Inactive"):
                await asyncio.sleep(0.25)  # errorCode 201 lands in a later callback
                log_entries = list(entry_trade.log)
                tws_msgs = [e.message for e in log_entries if e.message]
                error_codes = [e.errorCode for e in log_entries if getattr(e, "errorCode", 0)]
                reason = " | ".join(tws_msgs[-3:]) if tws_msgs else "no detail"
                is_201 = (
                    201 in error_codes
                    or any("201" in m or "iskless" in m for m in tws_msgs)
                )
                logger.warning("[%s] BAG combo rejected (%s): %s", session_id, status, reason)
                if is_201:
                    # Riskless-combo limit — do NOT leg in (would leave a naked short).
                    return {
                        "order_id": parent_id, "status": "Cancelled", "error_code": "201",
                        "fills": [], "entry_price": entry_price,
                        "profit_target": profit_target, "stop_loss": stop_loss,
                        "reason": "Error 201 — riskless combination orders disabled on this account",
                    }
                return {
                    "order_id": parent_id, "status": "Cancelled", "fills": [],
                    "entry_price": current_limit, "profit_target": profit_target,
                    "stop_loss": stop_loss, "reason": f"Rejected during walk: {reason}",
                }

            # Reprice one step toward the natural (unless already there).
            if abs(current_limit - natural) < _TICK:
                continue  # resting at the natural — keep waiting, don't overshoot
            current_limit = round(current_limit + step_dir * eff_step, 2)
            current_limit = (min(current_limit, natural) if step_dir > 0
                             else max(current_limit, natural, _TICK))
            entry_order.lmtPrice = current_limit
            entry_order.transmit = True
            ib.placeOrder(bag, entry_order)
            logger.info(
                "[%s] Reprice step %d/%d — limit=%.2f (%s, natural=%.2f)",
                session_id, step + 1, max_steps, current_limit, order_action, natural,
            )

        # ── Cancel if still unfilled after the full walk ───────────────────
        if entry_trade.orderStatus.status != "Filled":
            try:
                ib.cancelOrder(entry_order)
            except Exception:
                pass
            logger.warning(
                "[%s] Order %d unfilled after walking mid=%.2f->natural=%.2f — cancelled",
                session_id, parent_id, mid_limit, natural,
            )
            return {
                "order_id": parent_id, "status": "Cancelled", "fills": [],
                "entry_price": current_limit, "profit_target": profit_target,
                "stop_loss": stop_loss,
                "reason": f"Unfilled after walking mid {mid_limit:.2f} -> natural {natural:.2f}",
            }

        fills = [
            {
                "exec_id": f.execution.execId,
                "shares": f.execution.shares,
                "price": f.execution.price,
                "time": f.execution.time.isoformat() if f.execution.time else None,
            }
            for f in entry_trade.fills
        ]

        # Net combo fill price (per share), so slippage-vs-mid is comparable.
        # A BAG reports one execution PER LEG — using a single leg's price (the old
        # bug) logged nonsense slippage like mid=0.53 vs fill=5.79. Sum the signed
        # leg fills (BUY=+, SELL=-) to recover the net; abs() to match the mid's
        # magnitude regardless of debit/credit sign.
        sign_by_conid = {
            c.conId: (+1 if ls["action"].upper() == "BUY" else -1)
            for ls, c in qualified_legs
        }
        net_fill = 0.0
        for f in entry_trade.fills:
            cid = getattr(getattr(f, "contract", None), "conId", None)
            net_fill += sign_by_conid.get(cid, +1) * float(f.execution.price)
        net_fill_price = round(abs(net_fill), 4) if entry_trade.fills else current_limit

        return {
            "order_id": parent_id,
            "status": entry_trade.orderStatus.status,
            "fills": fills,
            "entry_price": net_fill_price,       # net combo fill per share
            "net_fill_price": net_fill_price,    # explicit; used for slippage tracking
            "limit_at_fill": current_limit,      # the net limit when it filled
            "profit_target": profit_target,
            "stop_loss": stop_loss,
        }

    finally:
        if ib.isConnected():
            ib.disconnect()
        logger.debug("[%s] IBKR bracket connection closed", session_id)


async def place_legs_individually(
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
    client_id: int = 10,
    price_step_size: float = 0.05,
    use_adaptive_algo: bool = True,
    adaptive_algo_priority: str = "Normal",
) -> dict[str, Any]:
    """
    Submit each leg of a spread as a standalone option order.

    Used for paper trading to bypass IBKR Error 201 (riskless combination
    order limit). Individual option orders are not classified as "riskless
    combination orders" and don't count toward the TWS paper account limit.

    Pricing: fetches current bid/ask snapshot for each leg; submits at mid.
    Adaptive pricing steps each leg's limit toward market every 30s for up to
    3 minutes. If any leg is still unfilled, all pending legs are cancelled.

    Risk: brief leg gap between fills. Acceptable in paper mode since fills
    are simulated and no real capital is at risk.
    """
    if not _IB_AVAILABLE:
        raise RuntimeError("ib_insync not installed")

    _PRICE_STEP_SEC  = 30
    _MAX_PRICE_STEPS = 6
    _TICK = 0.01

    ib = IB()
    try:
        await ib.connectAsync(host, port, clientId=client_id, timeout=10)
        logger.info("[%s] Leg-by-leg order — connecting to %s:%d", session_id, host, port)

        # ── Qualify each leg contract ─────────────────────────────────────────
        qualified: list[tuple[dict, Any]] = []
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
            q = await ib.qualifyContractsAsync(opt)
            if not q:
                raise RuntimeError(f"Could not qualify {ticker} {leg['option_type']} {leg['strike']}")
            qualified.append((leg, q[0]))

        # ── Fetch market data snapshot for initial pricing ────────────────────
        leg_mids: list[float] = []
        for leg_spec, contract in qualified:
            ticker_obj = ib.reqMktData(contract, "", True, False)
            await asyncio.sleep(2)
            bid = ticker_obj.bid if ticker_obj.bid and ticker_obj.bid > 0 else 0.0
            ask = ticker_obj.ask if ticker_obj.ask and ticker_obj.ask > 0 else 0.0
            mid = round((bid + ask) / 2, 2) if bid > 0 and ask > 0 else max(0.01, abs(entry_price))
            leg_mids.append(mid)
            ib.cancelMktData(contract)

        # ── Submit individual leg orders ──────────────────────────────────────
        trades = []
        orders = []
        limits = list(leg_mids)
        for i, (leg_spec, contract) in enumerate(qualified):
            action = leg_spec["action"].upper()
            lmt = limits[i]
            order = LimitOrder(
                action=action,
                totalQuantity=contracts * leg_spec.get("quantity", 1),
                lmtPrice=max(_TICK, lmt),
            )
            order.orderRef = f"{session_id[:35]}-L{i}"
            order.tif = "DAY"
            order.transmit = True
            # Adaptive algo: let IBKR manage the price within the regulatory collar so the
            # order fills at a fair price instead of resting unfilled / stuck at PendingSubmit.
            if use_adaptive_algo:
                _apply_adaptive_algo(order, adaptive_algo_priority)
            trade = ib.placeOrder(contract, order)
            trades.append(trade)
            orders.append(order)
            logger.info(
                "[%s] Leg %d/%d submitted — %s %s %.0f %s @ %.2f",
                session_id, i + 1, len(legs),
                action, ticker, leg_spec["strike"], leg_spec["option_type"].upper(), lmt,
            )

        # ── Pricing loop ──────────────────────────────────────────────────────
        # When the Adaptive algo is on, IBKR owns the price (server-side, within the
        # collar) — we only POLL for fills and must NOT also step the limit manually
        # (that would fight the algo). When off, fall back to the manual price walk.
        for step in range(_MAX_PRICE_STEPS):
            await asyncio.sleep(_PRICE_STEP_SEC)
            all_filled = all(t.orderStatus.status == "Filled" for t in trades)
            if all_filled:
                break

            if use_adaptive_algo:
                continue  # Adaptive algo manages price; just keep polling for fills

            for i, (trade, order) in enumerate(zip(trades, orders)):
                if trade.orderStatus.status == "Filled":
                    continue
                leg_spec = legs[i]
                action   = leg_spec["action"].upper()
                # Credit leg (SELL): step price down toward bid; debit leg (BUY): step up toward ask
                step_dir = -1 if action == "SELL" else +1
                limits[i] = max(_TICK, round(limits[i] + step_dir * price_step_size, 2))
                order.lmtPrice = limits[i]
                order.transmit  = True
                ib.placeOrder(qualified[i][1], order)
                logger.info(
                    "[%s] Leg %d price step %d/%d → %.2f",
                    session_id, i + 1, step + 1, _MAX_PRICE_STEPS, limits[i],
                )

        # ── Evaluate final fill state ─────────────────────────────────────────
        filled = [t for t in trades if t.orderStatus.status == "Filled"]
        pending = [
            (i, t, orders[i], qualified[i][1])
            for i, t in enumerate(trades)
            if t.orderStatus.status not in ("Filled", "Cancelled", "ApiCancelled")
        ]

        if pending:
            for i, trade, order, contract in pending:
                try:
                    ib.cancelOrder(order)
                except Exception:
                    pass
            if not filled:
                return {
                    "order_id": -1, "status": "Cancelled", "fills": [],
                    "entry_price": entry_price, "profit_target": profit_target,
                    "stop_loss": stop_loss,
                    "reason": f"Unfilled after {_MAX_PRICE_STEPS} price steps (3 min)",
                }
            # Partial fill — some legs filled, others timed out.
            # In paper mode log the mismatch; position manager will track via actual fills.
            logger.warning(
                "[%s] Partial leg fill: %d/%d filled — recording position from filled legs",
                session_id, len(filled), len(trades),
            )

        all_fills = []
        avg_entry = entry_price
        for trade in filled:
            for f in trade.fills:
                all_fills.append({
                    "exec_id": f.execution.execId,
                    "shares":  f.execution.shares,
                    "price":   f.execution.price,
                    "time":    f.execution.time.isoformat() if f.execution.time else None,
                })
        if all_fills:
            prices = [f["price"] for f in all_fills]
            # Net entry: sum sells (negative) and buys (positive) by action
            net = 0.0
            for i, trade in enumerate(filled):
                action = legs[i]["action"].upper() if i < len(legs) else "BUY"
                sign   = -1 if action == "SELL" else +1
                if trade.fills:
                    net += sign * trade.fills[0].execution.price
            avg_entry = round(net, 4)

        return {
            "order_id": trades[0].order.orderId if trades else -1,
            "status":   "Filled" if filled else "Cancelled",
            "fills":    all_fills,
            "entry_price":   avg_entry,
            "profit_target": profit_target,
            "stop_loss":     stop_loss,
        }

    finally:
        if ib.isConnected():
            ib.disconnect()
        logger.debug("[%s] Leg-by-leg connection closed", session_id)


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
            await asyncio.sleep(1)   # yields to the loop; ib_insync processes fills here.
            # NB: do NOT call ib.sleep() — it is ib_insync's *synchronous* wait and runs
            # loop.run_until_complete() internally. Inside this already-running loop (we are
            # invoked via _run_in_new_loop) that raises "This event loop is already running"
            # and aborts the close — silently stranding a position that should have exited.
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

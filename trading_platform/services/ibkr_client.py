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
import math
import os
import uuid
from datetime import date, timedelta
from typing import Any

logger = logging.getLogger(__name__)

# ── Order-id high-water mark (Error 103 "Duplicate order id" guard) ──────────────
# The execution path opens a FRESH IB() connection per call (see _connect_ibkr). After rapid
# reconnects (e.g. a restart churn), IBKR's nextValidId for a clientId can lag the highest id it
# actually consumed, so a new connection re-issues a used id and IBKR rejects with Error 103 —
# which cancelled every entry walk and froze fills at 0%. We persist the highest order id we've
# ever placed and floor each connection's ib_insync sequence above it, guaranteeing strictly
# increasing, collision-free ids across per-call connections AND across restarts.
_ORDERID_HW_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", ".agora", ".orderid_highwater")
_orderid_hw: int | None = None


def _load_orderid_hw() -> int:
    global _orderid_hw
    if _orderid_hw is None:
        try:
            with open(_ORDERID_HW_PATH) as f:
                _orderid_hw = int((f.read().strip() or "0"))
        except Exception:
            _orderid_hw = 0
    return _orderid_hw


def _bump_orderid_hw(used: int) -> None:
    """Record the highest order id actually placed (persisted, single-executor-thread safe)."""
    global _orderid_hw
    if used and used > _load_orderid_hw():
        _orderid_hw = used
        try:
            os.makedirs(os.path.dirname(_ORDERID_HW_PATH), exist_ok=True)
            with open(_ORDERID_HW_PATH, "w") as f:
                f.write(str(used))
        except Exception as exc:
            logger.debug("orderid high-water persist failed: %s", exc)


def _floor_order_sequence(ib: Any) -> None:
    """Floor ib_insync's req/order-id sequence above our persisted high-water so a stale
    nextValidId from IBKR can't re-issue a consumed order id (Error 103)."""
    try:
        floor = _load_orderid_hw() + 1
        cur = getattr(ib.client, "_reqIdSeq", 0) or 0
        if floor > cur:
            ib.client._reqIdSeq = floor
    except Exception as exc:
        logger.debug("order-id floor skipped: %s", exc)

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


def _valid_quote(x: Any) -> bool:
    """True if x is a usable positive price (not None / NaN / <= 0)."""
    return x is not None and isinstance(x, (int, float)) and not math.isnan(x) and x > 0


# M2: bounded marketable-limit close price. A pure MKT close on a thin/empty options book
# can fill arbitrarily far from the touch (the order chases whatever liquidity exists). A
# marketable LIMIT crosses the spread by a buffer so it fills like a market order in a normal
# book, but caps catastrophic slippage. buffer = max(2 ticks, pct of the marketable side).
def _marketable_close_limit(action: str, bid: float, ask: float,
                            buffer_pct: float = 0.05) -> float | None:
    """Price a closing order to fill now but bounded. Returns None if the quote is unusable
    (caller should fall back to MKT — a guaranteed exit beats no exit on a stop)."""
    _TICK = 0.01
    if action.upper() == "BUY":          # buying to close a short leg — cross UP through the ask
        if not _valid_quote(ask):
            return None
        return round(ask + max(2 * _TICK, ask * buffer_pct), 2)
    # SELL to close a long leg — cross DOWN through the bid, floored at one tick
    if not _valid_quote(bid):
        return None
    return round(max(bid - max(2 * _TICK, bid * buffer_pct), _TICK), 2)


# H1: cache resolved option conIds (global, immutable per contract) so the SAME legs aren't
# re-qualified on every call — the chosen legs get qualified across reprice → submit → close,
# and a qualify round-trip is ~1.5s each. conIds never change, so the cache is always valid.
_CONID_CACHE: dict[tuple, int] = {}


# M3: critical IBKR connectivity/session errors that the trading engine must be ALERTED on
# (not just logged). 10197 = a competing live session has taken over our market data — quotes
# silently stop, so fills/reprices degrade with no obvious cause. 1100 = the API connection to
# TWS/IBKR was lost. 2103/2105/10182 = a market-data farm is disconnected. We capture these on
# each connection's errorEvent into a bounded buffer the bridge drains and the session alerts.
_CRITICAL_IBKR_ERRORS: dict[int, str] = {
    10197: "competing live session — market data is being starved (another login took over)",
    1100: "API connection to TWS/IBKR lost",
    2103: "market-data farm connection is broken",
    2105: "historical-data farm connection is broken",
    10182: "market-data farm disconnected — quotes unavailable",
}
_CRITICAL_ERR_BUFFER: list[dict[str, Any]] = []
_CRITICAL_ERR_MAX = 50


def _attach_error_monitor(ib: Any, session_id: str = "") -> None:
    """Register an errorEvent handler that records critical IBKR errors (M3). Safe to call
    once per fresh IB() — the handler lives for that connection's lifetime."""
    def _on_error(reqId: int, errorCode: int, errorString: str, contract: Any = None) -> None:
        if errorCode in _CRITICAL_IBKR_ERRORS:
            entry = {
                "code": errorCode,
                "meaning": _CRITICAL_IBKR_ERRORS[errorCode],
                "raw": errorString,
                "session_id": session_id,
            }
            logger.warning("[%s] CRITICAL IBKR error %d — %s | %s",
                           session_id, errorCode, entry["meaning"], errorString)
            _CRITICAL_ERR_BUFFER.append(entry)
            del _CRITICAL_ERR_BUFFER[:-_CRITICAL_ERR_MAX]   # keep only the most recent N
    try:
        ib.errorEvent += _on_error
    except Exception:   # pragma: no cover - defensive
        pass


def drain_critical_errors() -> list[dict[str, Any]]:
    """Pop and return all buffered critical IBKR errors (M3). The session polls this and
    dispatches an operator alert. Draining clears the buffer so each error alerts once."""
    out = list(_CRITICAL_ERR_BUFFER)
    _CRITICAL_ERR_BUFFER.clear()
    return out


async def _connect_ibkr(
    ib: Any,
    *,
    host: str,
    port: int,
    client_id: int,
    market_data_type: int | None = None,
    session_id: str = "",
    timeout: float = 10,
) -> None:
    """M4: single source of truth for an execution connection — connect, attach the M3
    critical-error monitor, and set the market-data type. Every entry/close/quote path
    funnels its per-call setup through here so the connection lifecycle lives in one place.

    A persistent (reused) socket is deliberately NOT used: each call runs in a fresh event
    loop on the single executor thread, and an IB() is bound to the loop it connected on, so
    reusing one across loops is unsafe. The H1 conId cache already removes the only expensive
    repeated work (qualify), leaving just a ~1s connect that a persistent socket would save.
    """
    await ib.connectAsync(host, port, clientId=client_id, timeout=timeout)
    _attach_error_monitor(ib, session_id)
    _floor_order_sequence(ib)   # Error-103 guard: never re-issue a consumed order id
    if market_data_type is not None:
        try:
            ib.reqMarketDataType(market_data_type)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("[%s] reqMarketDataType(%s) failed: %s", session_id, market_data_type, exc)


async def _qualify(ib: Any, opt: Any) -> Any | None:
    """Resolve an Option's conId, skipping the qualify round-trip when already cached.
    Returns the conId-set Option, or None if it can't be resolved."""
    key = (opt.symbol, opt.lastTradeDateOrContractMonth, float(opt.strike), opt.right)
    cid = _CONID_CACHE.get(key)
    if cid:
        opt.conId = cid
        return opt
    q = await ib.qualifyContractsAsync(opt)
    if not q or not getattr(q[0], "conId", 0):
        return None
    _CONID_CACHE[key] = q[0].conId
    return q[0]


async def _fetch_ibkr_combo_pricing(
    ib: Any,
    qualified_legs: list[tuple[dict, Any]],
    *,
    timeout: float = 6.0,
) -> tuple[float | None, float | None, dict[str, float]]:
    """
    Read per-leg bid/ask from IBKR and derive the NET spread mid + natural price.

    `ib.reqMarketDataType(...)` must already be set on the connection (1=live / 3=delayed).
    Per-leg quotes are unambiguous (no combo sign confusion) and also carry greeks.

    Net is expressed as cost-to-us: debit POSITIVE, credit NEGATIVE.
      net_mid     = Σ sign · leg_mid                    (sign +1 BUY, -1 SELL)
      net_natural = Σ (BUY leg at ask, SELL leg at bid) — the marketable side we
                    must cross to fill (pay the ask on longs, hit the bid on shorts).

    Returns (net_mid, net_natural, greeks). net_* are None if any leg quote is missing.
    greeks is a best-effort {delta, iv} aggregate (empty if unavailable).
    """
    tickers = [(leg_spec, ib.reqMktData(contract, "", False, False))
               for leg_spec, contract in qualified_legs]

    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout

    def _all_ready() -> bool:
        return all(_valid_quote(tk.bid) and _valid_quote(tk.ask) for _, tk in tickers)

    while loop.time() < deadline and not _all_ready():
        await asyncio.sleep(0.3)

    ok = _all_ready()
    net_mid = net_natural = 0.0
    net_delta = 0.0
    iv_vals: list[float] = []
    if ok:
        for leg_spec, tk in tickers:
            sign = +1 if leg_spec["action"].upper() == "BUY" else -1
            leg_mid = (tk.bid + tk.ask) / 2.0
            net_mid += sign * leg_mid
            net_natural += (tk.ask if sign > 0 else -tk.bid)
            g = getattr(tk, "modelGreeks", None)
            if g is not None:
                if _valid_quote(abs(getattr(g, "delta", 0) or 0)):
                    net_delta += sign * (g.delta or 0)
                if getattr(g, "impliedVol", None):
                    iv_vals.append(g.impliedVol)

    for _, tk in tickers:
        try:
            ib.cancelMktData(tk.contract)
        except Exception:
            pass

    greeks: dict[str, float] = {}
    if ok and net_delta:
        greeks["net_delta"] = round(net_delta, 4)
    if iv_vals:
        greeks["avg_iv"] = round(sum(iv_vals) / len(iv_vals), 4)

    if not ok:
        return None, None, greeks
    return round(net_mid, 2), round(net_natural, 2), greeks


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


def _leg_expiry(leg: dict) -> str:
    """IBKR expiry (YYYYMMDD) for a leg. PREFER the exact date the strategy selected from a
    real chain (expiration_date) over re-deriving the 'nearest Friday' from DTE: the latter
    silently shifts the date (e.g. a Thursday weekly → the next Friday) and can land on a
    contract that doesn't exist, failing qualification — the cause of long-option submits
    erroring 'Could not qualify ... 48.0'. Fall back to the DTE→Friday heuristic only when no
    exact date is carried (legacy callers / older position rows)."""
    exact = leg.get("expiration_date")
    if exact:
        return str(exact).replace("-", "")
    return _next_expiry(leg.get("expiration_dte", 0))



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
    market_data_type: int = 3,
    max_combo_spread_pct: float = 0.50,
    pricing_sanity_max_ratio: float = 2.0,
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
        # M4: connect + M3 error monitor + market-data type, all via the shared helper.
        await _connect_ibkr(ib, host=host, port=port, client_id=client_id,
                            market_data_type=market_data_type, session_id=session_id)
        logger.info("[%s] Bracket order — connecting to %s:%d", session_id, host, port)

        # ── Qualify legs and build BAG contract ───────────────────────────
        qualified_legs: list[tuple[dict, Any]] = []
        for leg in legs:
            opt = Option(
                symbol=ticker,
                lastTradeDateOrContractMonth=_leg_expiry(leg),
                strike=float(leg["strike"]),
                right="C" if leg["option_type"].lower() == "call" else "P",
                exchange="SMART",
                currency="USD",
                multiplier="100",
            )
            q = await _qualify(ib, opt)
            if q is None:
                raise RuntimeError(f"Could not qualify {ticker} {leg['option_type']} {leg['strike']}")
            qualified_legs.append((leg, q))

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

        # ── Pricing source: IBKR market data (delayed/live) → yfinance fallback ──
        # IBKR gives the REAL net mid + natural (the marketable side we cross to fill);
        # yfinance only gives a mid, so without IBKR we fall back to a width-heuristic
        # natural. Sign of the IBKR net must agree with the recommendation (debit>0 /
        # credit<0) — otherwise the quote is suspect and we keep yfinance.
        order_action = "BUY" if entry_price > 0 else "SELL"
        ibkr_mid = ibkr_natural = None
        ibkr_greeks: dict[str, float] = {}
        try:
            net_mid, net_natural, ibkr_greeks = await _fetch_ibkr_combo_pricing(ib, qualified_legs)
            if (net_mid is not None and abs(net_mid) >= _TICK
                    and (net_mid > 0) == (entry_price > 0)):
                ibkr_mid = abs(net_mid)
                ibkr_natural = abs(net_natural) if net_natural is not None else None
                logger.info(
                    "[%s] IBKR pricing (mdType=%d): net_mid=%.2f natural=%.2f greeks=%s "
                    "| yfinance mid=%.2f",
                    session_id, market_data_type, ibkr_mid,
                    ibkr_natural if ibkr_natural is not None else float("nan"),
                    ibkr_greeks or "{}", abs(entry_price),
                )
            elif net_mid is not None:
                logger.warning(
                    "[%s] IBKR net_mid=%.2f sign disagrees with yfinance entry=%.2f — "
                    "using yfinance mid", session_id, net_mid, entry_price,
                )
        except Exception as exc:
            logger.warning("[%s] IBKR combo pricing failed (%s) — using yfinance mid",
                           session_id, exc)

        # ── Pricing sanity gate: IBKR mid vs the yfinance mid the trade was built on ─
        # yfinance option mids are sometimes badly stale (COST: yf 1.85 vs real 8.55).
        # If they disagree by > the ratio, the recommendation's economics aren't real — abort.
        if ibkr_mid is not None and abs(entry_price) > 0:
            ratio = ibkr_mid / abs(entry_price)
            if not (1.0 / pricing_sanity_max_ratio <= ratio <= pricing_sanity_max_ratio):
                logger.warning(
                    "[%s] Skipping %s — pricing_sanity_fail: IBKR mid %.2f vs yfinance mid %.2f "
                    "(%.1fx) — recommendation built on bad data",
                    session_id, ticker, ibkr_mid, abs(entry_price), ratio,
                )
                return {
                    "order_id": -1, "status": "Cancelled", "fills": [],
                    "entry_price": entry_price, "profit_target": profit_target,
                    "stop_loss": stop_loss,
                    "reason": f"pricing_sanity_fail: IBKR {ibkr_mid:.2f} vs yfinance "
                              f"{abs(entry_price):.2f} ({ratio:.1f}x)",
                }

        # ── Liquidity gate (best-effort; only when IBKR quotes are available) ─
        if ibkr_mid is not None and ibkr_natural is not None and ibkr_mid > 0:
            rel_spread = 2.0 * abs(ibkr_mid - ibkr_natural) / ibkr_mid
            if rel_spread > max_combo_spread_pct:
                logger.warning(
                    "[%s] Skipping %s — combo too illiquid: net bid-ask %.0f%% of mid "
                    "(mid=%.2f natural=%.2f) > %.0f%% gate",
                    session_id, ticker, rel_spread * 100, ibkr_mid, ibkr_natural,
                    max_combo_spread_pct * 100,
                )
                return {
                    "order_id": -1, "status": "Cancelled", "fills": [],
                    "entry_price": entry_price, "profit_target": profit_target,
                    "stop_loss": stop_loss,
                    "reason": f"Illiquid: combo bid-ask {rel_spread:.0%} of mid > {max_combo_spread_pct:.0%}",
                }

        # ── Entry: net limit at the mid, no GTC child, no Adaptive on a BAG ──
        # GTC children accumulate across sessions → Error 201; PositionManager owns
        # all exits. Adaptive is silently ignored on combos (verified 06-05), so we
        # attach it ONLY to a genuine single leg.
        mid_limit = round(ibkr_mid if ibkr_mid is not None else abs(entry_price), 2)
        entry_order = LimitOrder(
            action=order_action,
            totalQuantity=contracts,
            lmtPrice=mid_limit,
        )
        entry_order.orderRef = session_id[:40]
        entry_order.tif = "DAY"
        entry_order.transmit = True
        # NOTE: keep the BAG GUARANTEED (atomic — both legs fill together or neither),
        # which avoids a naked-short leg gap on credit spreads. The old
        # `nonGuaranteedFill = True` was a no-op (not a real ib_insync field) AND
        # non-guaranteed routing does NOT clear the credit-spread rejection anyway:
        # IBKR flags a credit spread as a "riskless/guaranteed-loss combination" by
        # POSITION type, not fill type (verified 2026-06-08). The Error-201 reject on
        # credit spreads is a TWS *precautionary* block ("Transmit anyway" in the GUI) —
        # it requires enabling "Bypass Order Precautions for API Orders" in TWS
        # (Global Configuration -> API -> Settings), not a code/routing change.
        if use_adaptive_algo and len(qualified_legs) == 1:
            _apply_adaptive_algo(entry_order, adaptive_algo_priority)

        entry_trade = ib.placeOrder(bag, entry_order)
        parent_id = entry_trade.order.orderId
        _bump_orderid_hw(parent_id)   # persist high-water so reconnects don't re-issue this id

        # Net combo fill price (per share) — a BAG reports one execution PER LEG (and may
        # report several per leg on a multi-contract fill). Average each leg by share volume,
        # then sum the signed leg averages (BUY=+, SELL=-) to recover the net; abs() matches
        # the mid's magnitude regardless of debit/credit sign. Defined here so BOTH the full-
        # fill and the M5 partial-fill paths share one correct computation.
        sign_by_conid = {
            c.conId: (+1 if ls["action"].upper() == "BUY" else -1)
            for ls, c in qualified_legs
        }

        def _net_fill_per_share(trade: Any) -> float:
            by_conid: dict[Any, list[float]] = {}
            for f in trade.fills:
                cid = getattr(getattr(f, "contract", None), "conId", None)
                sh = float(getattr(f.execution, "shares", 0) or 0)
                if cid is None or sh <= 0:
                    continue
                acc = by_conid.setdefault(cid, [0.0, 0.0])
                acc[0] += float(f.execution.price) * sh
                acc[1] += sh
            net = 0.0
            for cid, (pxsum, shsum) in by_conid.items():
                if shsum > 0:
                    net += sign_by_conid.get(cid, +1) * (pxsum / shsum)
            return round(abs(net), 4)

        # Walk TARGET = the slippage-budget cap, NOT merely the natural. The combo "natural"
        # (sum of leg marketable sides) often isn't a real combo-book price, so a limit that
        # only REACHES it doesn't cross and the order sits unfilled (observed 2026-06-09: walked
        # to natural=12.60 and cancelled). Walking THROUGH the natural up to the budget crosses
        # the real market — it still fills at the FIRST crossing (so liquid spreads pay little),
        # and only pays the full budget when it must. ibkr_natural is logged above for reference.
        step_dir = +1 if order_action == "BUY" else -1
        if order_action == "BUY":
            target = mid_limit + slippage_budget
            if width > 0:
                target = min(target, width * 0.99)                   # ≤ the spread's max value
        else:
            target = max(mid_limit - slippage_budget, mid_limit * 0.5, _TICK)
        natural = round(target, 2)   # the walk loop + logs key off `natural`
        walk_room = abs(natural - mid_limit)
        # Bound the walk to a fixed number of reprices so a wide spread can't hog the
        # single-threaded IBKR executor; faster cadence so the market doesn't drift off the
        # target mid-walk (20s/4min was too slow — orders reached the natural after it moved).
        _MAX_WALK_STEPS = 12
        _PRICE_STEP_SEC = 12  # ~2.4 min max walk
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

        def _fills_payload() -> list[dict[str, Any]]:
            return [
                {
                    "exec_id": f.execution.execId,
                    "shares": f.execution.shares,
                    "price": f.execution.price,
                    "time": f.execution.time.isoformat() if f.execution.time else None,
                }
                for f in entry_trade.fills
            ]

        # ── Cancel if still unfilled after the full walk ───────────────────
        if entry_trade.orderStatus.status != "Filled":
            try:
                ib.cancelOrder(entry_order)
            except Exception:
                pass

            # M5: a multi-contract BAG is LEG-atomic but not QUANTITY-atomic — it can fill
            # part of the requested combos (e.g. 2 of 5) and rest. Cancelling the remainder
            # above leaves a REAL, smaller position open. Report it as PartiallyFilled with the
            # filled quantity so the session records the right size instead of discarding it as
            # a clean cancel (which would strand the open contracts, unmanaged).
            filled_qty = int(getattr(entry_trade.orderStatus, "filled", 0) or 0)
            if filled_qty > 0 and entry_trade.fills:
                net_fill_price = _net_fill_per_share(entry_trade) or current_limit
                logger.warning(
                    "[%s] Order %d PARTIALLY filled %d/%d combos after walk — "
                    "remainder cancelled, recording the %d filled",
                    session_id, parent_id, filled_qty, contracts, filled_qty,
                )
                return {
                    "order_id": parent_id, "status": "PartiallyFilled",
                    "fills": _fills_payload(), "filled_contracts": filled_qty,
                    "entry_price": net_fill_price, "net_fill_price": net_fill_price,
                    "limit_at_fill": current_limit, "ibkr_greeks": ibkr_greeks,
                    "profit_target": profit_target, "stop_loss": stop_loss,
                }

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

        net_fill_price = _net_fill_per_share(entry_trade) if entry_trade.fills else current_limit

        return {
            "order_id": parent_id,
            "status": entry_trade.orderStatus.status,
            "fills": _fills_payload(),
            "entry_price": net_fill_price,       # net combo fill per share
            "net_fill_price": net_fill_price,    # explicit; used for slippage tracking
            "limit_at_fill": current_limit,      # the net limit when it filled
            "ibkr_greeks": ibkr_greeks,          # net delta / avg IV from IBKR (best-effort)
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
    use_adaptive_algo: bool = False,
    adaptive_algo_priority: str = "Normal",
    adaptive_single_leg: bool = False,
    market_data_type: int = 3,
    max_combo_spread_pct: float = 0.50,
    pricing_sanity_max_ratio: float = 2.0,
) -> dict[str, Any]:
    """
    Submit each leg of a spread as a standalone option order.

    Used for CREDIT spreads on the paper account: IBKR classifies a credit spread
    as a "riskless/guaranteed-loss combination" and a BAG submission is hard-rejected
    with Error 201 ("maximum limit of active riskless combination orders"), which is a
    paper-account restriction that "Bypass Order Precautions for API Orders" does NOT
    clear (verified 2026-06-08). Individual leg orders are not riskless combinations,
    so they go through.

    Pricing: per-leg bid/ask from IBKR market data (delayed/live), priced at each leg's
    mid and walked toward the marketable side. If IBKR has no per-leg quote we DO NOT
    submit (the old code stamped the net spread price onto every leg — garbage that
    drove the 0.7% fill rate); we return a clean failure instead.

    Risk: brief leg gap between fills (one leg fills, the other rests). Acceptable in
    paper mode (simulated). For live, prefer the atomic BAG path.
    """
    if not _IB_AVAILABLE:
        raise RuntimeError("ib_insync not installed")

    _PRICE_STEP_SEC  = 12   # faster cadence so the per-leg marketable price doesn't drift off
    _MAX_PRICE_STEPS = 8
    _TICK = 0.01

    ib = IB()
    try:
        # M4: connect + M3 error monitor + market-data type, all via the shared helper.
        await _connect_ibkr(ib, host=host, port=port, client_id=client_id,
                            market_data_type=market_data_type, session_id=session_id)
        logger.info("[%s] Leg-by-leg order — connecting to %s:%d", session_id, host, port)

        # ── Qualify each leg contract ─────────────────────────────────────────
        qualified: list[tuple[dict, Any]] = []
        for leg in legs:
            opt = Option(
                symbol=ticker,
                lastTradeDateOrContractMonth=_leg_expiry(leg),
                strike=float(leg["strike"]),
                right="C" if leg["option_type"].lower() == "call" else "P",
                exchange="SMART",
                currency="USD",
                multiplier="100",
            )
            q = await _qualify(ib, opt)
            if q is None:
                raise RuntimeError(f"Could not qualify {ticker} {leg['option_type']} {leg['strike']}")
            qualified.append((leg, q))

        # ── Per-leg pricing from IBKR market data (delayed/live) ──────────────
        # Stream each leg's bid/ask and wait for it to populate. Price at the leg mid;
        # the walk below moves toward each leg's marketable side (BUY->ask, SELL->bid).
        # NO yfinance/net fallback — a missing quote means we skip rather than misprice.
        leg_tickers = [(ls, ib.reqMktData(c, "", False, False)) for ls, c in qualified]
        loop = asyncio.get_event_loop()
        deadline = loop.time() + 6.0

        def _legs_ready() -> bool:
            return all(_valid_quote(tk.bid) and _valid_quote(tk.ask) for _, tk in leg_tickers)

        while loop.time() < deadline and not _legs_ready():
            await asyncio.sleep(0.3)

        leg_mid: list[float] = []
        leg_natural: list[float] = []   # marketable side per leg
        missing: list[str] = []
        for ls, tk in leg_tickers:
            if _valid_quote(tk.bid) and _valid_quote(tk.ask):
                mid = round((tk.bid + tk.ask) / 2.0, 2)
                nat = round(tk.ask if ls["action"].upper() == "BUY" else tk.bid, 2)
                leg_mid.append(max(_TICK, mid))
                leg_natural.append(max(_TICK, nat))
            else:
                leg_mid.append(0.0)
                leg_natural.append(0.0)
                missing.append(f"{ls['strike']}{ls['option_type'][0].upper()}")
            try:
                ib.cancelMktData(tk.contract)
            except Exception:
                pass

        if missing:
            logger.warning("[%s] Leg-by-leg: no IBKR quote for %s (mdType=%d) — skipping",
                           session_id, ",".join(missing), market_data_type)
            return {
                "order_id": -1, "status": "Cancelled", "fills": [],
                "entry_price": entry_price, "profit_target": profit_target,
                "stop_loss": stop_loss,
                "reason": f"No per-leg market data for {','.join(missing)}",
            }

        logger.info("[%s] Leg pricing (mdType=%d): mids=%s naturals=%s",
                    session_id, market_data_type, leg_mid, leg_natural)

        # ── Liquidity gate: net combo bid-ask from the per-leg IBKR quotes ────
        net_mid = sum((+1 if ls["action"].upper() == "BUY" else -1) * leg_mid[i]
                      for i, (ls, _) in enumerate(qualified))
        net_nat = sum((+1 if ls["action"].upper() == "BUY" else -1) * leg_natural[i]
                      for i, (ls, _) in enumerate(qualified))

        # Pricing sanity gate: IBKR net mid vs the yfinance mid the trade was built on.
        if abs(net_mid) > 0 and abs(entry_price) > 0:
            ratio = abs(net_mid) / abs(entry_price)
            if not (1.0 / pricing_sanity_max_ratio <= ratio <= pricing_sanity_max_ratio):
                logger.warning(
                    "[%s] Skipping %s — pricing_sanity_fail: IBKR net mid %.2f vs yfinance %.2f "
                    "(%.1fx) — recommendation built on bad data",
                    session_id, ticker, abs(net_mid), abs(entry_price), ratio,
                )
                return {
                    "order_id": -1, "status": "Cancelled", "fills": [],
                    "entry_price": entry_price, "profit_target": profit_target,
                    "stop_loss": stop_loss,
                    "reason": f"pricing_sanity_fail: IBKR {abs(net_mid):.2f} vs yfinance "
                              f"{abs(entry_price):.2f} ({ratio:.1f}x)",
                }

        # Liquidity gate: net combo bid-ask vs mid.
        if abs(net_mid) > 0:
            rel_spread = 2.0 * abs(net_mid - net_nat) / abs(net_mid)
            if rel_spread > max_combo_spread_pct:
                logger.warning(
                    "[%s] Skipping %s — legs too illiquid: net bid-ask %.0f%% of mid > %.0f%% gate",
                    session_id, ticker, rel_spread * 100, max_combo_spread_pct * 100,
                )
                return {
                    "order_id": -1, "status": "Cancelled", "fills": [],
                    "entry_price": entry_price, "profit_target": profit_target,
                    "stop_loss": stop_loss,
                    "reason": f"Illiquid: net bid-ask {rel_spread:.0%} of mid > {max_combo_spread_pct:.0%}",
                }

        # ── H2: per-leg walk crosses slightly THROUGH the natural ──────────────
        # The leg "natural" (bid/ask at fetch time) can go stale over the ~1.6-min walk. Walk a
        # small buffer past it (BUY a touch above ask / SELL a touch below bid) so a moved market
        # still crosses — the leg-path analog of the BAG budget-cap walk. Bounded (~3% of leg mid).
        leg_cross: list[float] = []
        for i, m in enumerate(leg_mid):
            buf = max(2 * _TICK, round(0.03 * m, 2))
            if legs[i]["action"].upper() == "BUY":
                leg_cross.append(round(leg_natural[i] + buf, 2))
            else:
                leg_cross.append(max(_TICK, round(leg_natural[i] - buf, 2)))

        # ── C2: submit the protective LONG leg(s) FIRST, fill them, THEN the short(s) ──
        # NEVER hold a naked short: a credit spread's short leg is only placed once the long
        # (defined-risk) leg is filled. If the long can't fill, we abort before selling anything.
        trades: list = [None] * len(qualified)
        orders: list = [None] * len(qualified)
        limits = list(leg_mid)
        long_idx  = [i for i, (ls, _) in enumerate(qualified) if ls["action"].upper() == "BUY"]
        short_idx = [i for i, (ls, _) in enumerate(qualified) if ls["action"].upper() == "SELL"]

        def _place_leg(i: int) -> None:
            ls, contract = qualified[i]
            o = LimitOrder(action=ls["action"].upper(),
                           totalQuantity=contracts * ls.get("quantity", 1),
                           lmtPrice=max(_TICK, limits[i]))
            o.orderRef = f"{session_id[:35]}-L{i}"
            o.tif = "DAY"
            o.transmit = True
            # Single-leg orders (long_call/long_put) are NATIVE option orders, where the Adaptive
            # algo IS valid (it is silently ignored on BAG combos). It fills server-side within the
            # limit even without a client market-data sub — lifting fill rate above the walk-LMT
            # alone. Scoped to single legs: a multi-leg credit spread keeps the proven walk only,
            # so leg-in timing/atomicity is unchanged.
            if adaptive_single_leg and len(qualified) == 1:
                _apply_adaptive_algo(o, adaptive_algo_priority)
            trades[i] = ib.placeOrder(contract, o)
            _bump_orderid_hw(trades[i].order.orderId)
            orders[i] = o
            logger.info("[%s] Leg %d/%d submitted — %s %s %.0f %s @ %.2f (natural %.2f)",
                        session_id, i + 1, len(legs), ls["action"].upper(), ticker,
                        ls["strike"], ls["option_type"].upper(), limits[i], leg_natural[i])

        async def _walk(indices: list, max_steps: int) -> bool:
            for step in range(max_steps):
                # H3: fine-grained wait — ib_insync fires fill events on the loop, so poll every
                # 1s and react to a fill within ~1s instead of sleeping the whole step interval.
                for _ in range(_PRICE_STEP_SEC):
                    await asyncio.sleep(1)
                    if all(trades[i].orderStatus.status == "Filled" for i in indices):
                        return True
                for i in indices:
                    if trades[i].orderStatus.status == "Filled":
                        continue
                    step_dir = +1 if legs[i]["action"].upper() == "BUY" else -1
                    if abs(limits[i] - leg_cross[i]) < _TICK:
                        continue
                    nxt = round(limits[i] + step_dir * price_step_size, 2)
                    limits[i] = (min(nxt, leg_cross[i]) if step_dir > 0
                                 else max(nxt, leg_cross[i], _TICK))
                    orders[i].lmtPrice = limits[i]
                    orders[i].transmit = True
                    ib.placeOrder(qualified[i][1], orders[i])
                    logger.info("[%s] Leg %d step %d/%d → %.2f (natural %.2f)",
                                session_id, i + 1, step + 1, max_steps, limits[i], leg_natural[i])
            return all(trades[i].orderStatus.status == "Filled" for i in indices)

        if long_idx:
            for i in long_idx:
                _place_leg(i)
            if not await _walk(long_idx, _MAX_PRICE_STEPS):
                for i in long_idx:
                    try:
                        ib.cancelOrder(orders[i])
                    except Exception:
                        pass
                logger.warning("[%s] %s: protective long leg unfilled — aborting (no naked short)",
                               session_id, ticker)
                return {
                    "order_id": -1, "status": "Cancelled", "fills": [],
                    "entry_price": entry_price, "profit_target": profit_target,
                    "stop_loss": stop_loss,
                    "reason": "protective long leg unfilled — aborted to avoid naked short",
                }

        for i in short_idx:
            _place_leg(i)
        await _walk(short_idx, _MAX_PRICE_STEPS)

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

        # Net entry from the FILLED legs, signed by each leg's own action (robust to
        # partial fills — don't index legs[] positionally against the filtered list).
        all_fills = []
        net = 0.0
        for trade in filled:
            sign = +1 if trade.order.action.upper() == "BUY" else -1
            for f in trade.fills:
                all_fills.append({
                    "exec_id": f.execution.execId,
                    "shares":  f.execution.shares,
                    "price":   f.execution.price,
                    "time":    f.execution.time.isoformat() if f.execution.time else None,
                })
                net += sign * float(f.execution.price)
        net_fill_price = round(abs(net), 4) if all_fills else abs(entry_price)

        return {
            "order_id": trades[0].order.orderId if trades else -1,
            "status":   "Filled" if filled else "Cancelled",
            "fills":    all_fills,
            "entry_price":    net_fill_price,
            "net_fill_price": net_fill_price,   # net per share; used for slippage tracking
            "profit_target":  profit_target,
            "stop_loss":      stop_loss,
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
    market_data_type: int = 1,
) -> dict[str, Any]:
    """
    Close an open spread position with a bounded marketable-limit combo order (M2).

    Called by MonitorAgent when stop-loss, trailing stop, or thesis-break
    is triggered. Reverses all legs of the spread via a BAG market order.

    legs: same format as place_bracket_order — action is the ORIGINAL entry action;
          this function automatically reverses (buy→sell, sell→buy) to close.
    """
    if not _IB_AVAILABLE:
        raise RuntimeError("ib_insync not installed. Run: pip install ib_insync")

    ib = IB()

    try:
        # M4: connect + M3 error monitor + market-data type via the shared helper.
        await _connect_ibkr(ib, host=host, port=port, client_id=client_id,
                            market_data_type=market_data_type)
        logger.info("[%s] Closing position %s x%d", session_id, ticker, contracts)

        qualified_legs: list[tuple[dict, Any]] = []
        for leg in legs:
            opt = Option(
                symbol=ticker,
                lastTradeDateOrContractMonth=_leg_expiry(leg),
                strike=float(leg["strike"]),
                right="C" if leg["option_type"].lower() == "call" else "P",
                exchange="SMART",
                currency="USD",
                multiplier="100",
            )
            q = await _qualify(ib, opt)
            if q is None:
                raise RuntimeError(f"Could not qualify closing leg: {ticker} {leg}")
            qualified_legs.append((leg, q))

        # M2: derive the net cost to CLOSE at the marketable touch so we can place a bounded
        # LIMIT instead of a pure MKT. Closing reverses every leg: an original BUY is sold at
        # the bid (we receive), an original SELL is bought at the ask (we pay). The reversed
        # BAG order action is fixed BUY below, so a HIGHER net limit is always more marketable
        # — we set lmtPrice = close_net + buffer. None → fall back to MKT (guarantee the exit).
        _close_mkt = [(ls, ib.reqMktData(c, "", False, False)) for ls, c in qualified_legs]
        _loop = asyncio.get_event_loop()
        _qdl = _loop.time() + 4.0
        while _loop.time() < _qdl and not all(
            _valid_quote(tk.bid) and _valid_quote(tk.ask) for _, tk in _close_mkt
        ):
            await asyncio.sleep(0.3)
        close_net: float | None = 0.0
        for ls, tk in _close_mkt:
            if not (_valid_quote(tk.bid) and _valid_quote(tk.ask)):
                close_net = None
                break
            ratio = ls.get("quantity", 1)
            if ls["action"].upper() == "BUY":      # owned long → sell at bid (receive)
                close_net -= float(tk.bid) * ratio
            else:                                   # short → buy at ask (pay)
                close_net += float(tk.ask) * ratio
        for _, tk in _close_mkt:
            try:
                ib.cancelMktData(tk.contract)
            except Exception:
                pass

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

        # M2: bounded marketable LIMIT (cross the touch by a buffer) with MKT fallback.
        close_order = Order()
        close_order.action = "BUY"   # reversed relative to entry
        close_order.totalQuantity = contracts
        close_order.tif = "DAY"
        close_order.orderRef = f"CLOSE_{session_id[:30]}"
        close_order.transmit = True
        if close_net is not None:
            buffer = max(0.02, abs(close_net) * 0.05)
            close_order.orderType = "LMT"
            close_order.lmtPrice = round(close_net + buffer, 2)
        else:
            close_order.orderType = "MKT"   # no usable quote — guarantee the exit

        trade = ib.placeOrder(bag, close_order)
        _bump_orderid_hw(trade.order.orderId)
        logger.info(
            "[%s] Close order submitted — orderId=%d %s",
            session_id, trade.order.orderId,
            f"LMT {close_order.lmtPrice:.2f}" if close_order.orderType == "LMT" else "MKT",
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


async def close_position_legs(
    *,
    ticker: str,
    legs: list[dict[str, Any]],
    contracts: int,
    session_id: str,
    host: str = "127.0.0.1",
    port: int = 7497,
    client_id: int = 2,
    timeout: float = 25.0,
    market_data_type: int = 1,
) -> dict[str, Any]:
    """
    Close a spread LEG-BY-LEG with bounded marketable-limit orders (M2).

    Used for CREDIT-spread positions: the closing BAG re-creates a combo that IBKR
    re-flags as a riskless/guaranteed-loss combination and rejects with Error 201 on
    the paper account, stranding the position (an unbounded risk on a stop-loss exit).
    Individual orders aren't combos, guarantee the exit, and fill both legs immediately.

    M2: each leg is closed with a BOUNDED marketable LIMIT (cross the touch by a small
    buffer) instead of a pure MKT, so a thin/empty book can't fill the exit arbitrarily
    far from the quote. If a leg has no usable quote we fall back to MKT for that leg —
    a guaranteed exit beats no exit on a stop.

    `legs` carry the ORIGINAL entry actions; each is reversed here to flatten.
    """
    if not _IB_AVAILABLE:
        raise RuntimeError("ib_insync not installed")

    ib = IB()
    try:
        # M4: connect + M3 error monitor + market-data type via the shared helper.
        await _connect_ibkr(ib, host=host, port=port, client_id=client_id,
                            market_data_type=market_data_type)
        logger.info("[%s] Closing position LEG-BY-LEG %s x%d", session_id, ticker, contracts)

        qualified: list[tuple[dict, Any]] = []
        for leg in legs:
            opt = Option(
                symbol=ticker,
                lastTradeDateOrContractMonth=_leg_expiry(leg),
                strike=float(leg["strike"]),
                right="C" if leg["option_type"].lower() == "call" else "P",
                exchange="SMART", currency="USD", multiplier="100",
            )
            q = await _qualify(ib, opt)
            if q is None:
                raise RuntimeError(f"Could not qualify closing leg: {ticker} {leg}")
            qualified.append((leg, q))

        # M2: snapshot each leg's bid/ask so we can price a bounded marketable LIMIT.
        mkt = [(ls, ib.reqMktData(c, "", False, False)) for ls, c in qualified]
        loop = asyncio.get_event_loop()
        q_deadline = loop.time() + 4.0
        while loop.time() < q_deadline and not all(
            _valid_quote(tk.bid) and _valid_quote(tk.ask) for _, tk in mkt
        ):
            await asyncio.sleep(0.3)
        leg_quote = {id(c): tk for (ls, c), (_, tk) in zip(qualified, mkt)}
        for _, tk in mkt:
            try:
                ib.cancelMktData(tk.contract)
            except Exception:
                pass

        trades = []
        for i, (leg, contract) in enumerate(qualified):
            close_action = "SELL" if leg["action"].upper() == "BUY" else "BUY"  # reverse to flatten
            tk = leg_quote.get(id(contract))
            limit_px = (
                _marketable_close_limit(close_action, getattr(tk, "bid", None),
                                        getattr(tk, "ask", None))
                if tk is not None else None
            )
            o = Order()
            o.action = close_action
            o.totalQuantity = contracts * leg.get("quantity", 1)
            o.tif = "DAY"
            o.orderRef = f"CLOSE_{session_id[:26]}-L{i}"
            o.transmit = True
            if limit_px is not None:
                o.orderType = "LMT"
                o.lmtPrice = limit_px
            else:
                o.orderType = "MKT"   # no usable quote — guarantee the exit
            trades.append(ib.placeOrder(contract, o))
            logger.info(
                "[%s] Close leg %d/%d — %s %s %.0f %s %s",
                session_id, i + 1, len(qualified), close_action, ticker,
                leg["strike"], leg["option_type"].upper(),
                f"LMT {limit_px:.2f}" if limit_px is not None else "MKT",
            )

        # Wait for all legs to fill (market orders fill fast).
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(1)
            if all(t.orderStatus.status == "Filled" for t in trades):
                break
            for t in trades:
                if t.orderStatus.status in ("Cancelled", "ApiCancelled", "Inactive"):
                    tws_msgs = [e.message for e in t.log if e.message]
                    raise RuntimeError(
                        f"Close leg {t.order.orderId} rejected: {t.orderStatus.status} — "
                        f"{'|'.join(tws_msgs[-2:])}"
                    )

        fills = []
        net = 0.0
        for t in trades:
            sign = +1 if t.order.action.upper() == "BUY" else -1
            for f in t.fills:
                fills.append({
                    "exec_id": f.execution.execId, "shares": f.execution.shares,
                    "price": f.execution.price,
                    "time": f.execution.time.isoformat() if f.execution.time else None,
                })
                net += sign * float(f.execution.price)
        avg_price = round(abs(net), 4) if fills else None   # net cost to flatten, per share
        n_filled = sum(1 for t in trades if t.orderStatus.status == "Filled")
        logger.info(
            "[%s] Position closed leg-by-leg — %d/%d legs filled net=%.2f",
            session_id, n_filled, len(trades), avg_price or 0,
        )
        # 3-way status so the caller can tell a TRUE flatten from a no-op. The old code returned
        # "PartiallyClosed" for n_filled==0, which the session treated as success → it marked the
        # position closed while ALL legs were still open at the broker → orphan + fictitious P&L
        # (root cause of the 33 stranded legs found 2026-06-10).
        #   Filled          = every leg flattened (position is flat)
        #   Failed          = ZERO legs filled (position fully still open — safe to retry whole close)
        #   PartiallyClosed = some legs flat, some stranded (a naked leg remains — needs recon, NOT a
        #                     blind re-close which would over-trade the already-flat leg)
        if trades and n_filled == len(trades):
            _close_status = "Filled"
        elif n_filled == 0:
            _close_status = "Failed"
        else:
            _close_status = "PartiallyClosed"
        return {
            "order_id": trades[0].order.orderId if trades else -1,
            "status": _close_status,
            "fills": fills,
            "avg_price": avg_price,
            "n_filled": n_filled,
            "n_legs": len(trades),
        }

    finally:
        if ib.isConnected():
            ib.disconnect()
        logger.debug("[%s] Leg-by-leg close connection closed", session_id)


async def fetch_leg_quotes(
    *,
    ticker: str,
    legs: list[dict[str, Any]],
    market_data_type: int = 3,
    host: str = "127.0.0.1",
    port: int = 7497,
    client_id: int = 6,
    timeout: float = 6.0,
) -> list[dict[str, Any]] | None:
    """
    Precision-price the CHOSEN structure: fetch per-leg bid/ask/mid from IBKR for the
    specific legs the rules engine picked (NOT a full chain). Used by the pre-submission
    reprice/re-gate pass so the trade decision runs on real prices, not stale yfinance.

    Returns a list ALIGNED with `legs`:
      [{"strike","right","action","bid","ask","mid"}, ...]
    or None if ANY leg has no valid quote (don't reprice on partial data — keep yfinance).
    """
    if not _IB_AVAILABLE:
        return None

    ib = IB()
    try:
        # M4: connect + M3 error monitor + market-data type via the shared helper.
        await _connect_ibkr(ib, host=host, port=port, client_id=client_id,
                            market_data_type=market_data_type)

        qualified: list[tuple[dict, Any]] = []
        for leg in legs:
            opt = Option(
                symbol=ticker,
                lastTradeDateOrContractMonth=_leg_expiry(leg),
                strike=float(leg["strike"]),
                right="C" if leg["option_type"].lower() == "call" else "P",
                exchange="SMART", currency="USD", multiplier="100",
            )
            q = await _qualify(ib, opt)
            if q is None:
                return None
            qualified.append((leg, q))

        tickers = [(ls, ib.reqMktData(c, "", False, False)) for ls, c in qualified]
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout

        def _ready() -> bool:
            return all(_valid_quote(tk.bid) and _valid_quote(tk.ask) for _, tk in tickers)

        while loop.time() < deadline and not _ready():
            await asyncio.sleep(0.3)

        ok = _ready()
        out: list[dict[str, Any]] = []
        for ls, tk in tickers:
            if ok:
                out.append({
                    "strike": float(ls["strike"]),
                    "right": "C" if ls["option_type"].lower() == "call" else "P",
                    "action": ls["action"].upper(),
                    "bid": float(tk.bid), "ask": float(tk.ask),
                    "mid": round((tk.bid + tk.ask) / 2.0, 4),
                })
            try:
                ib.cancelMktData(tk.contract)
            except Exception:
                pass

        return out if ok else None
    finally:
        if ib.isConnected():
            ib.disconnect()


async def fetch_chain_quotes(
    *,
    ticker: str,
    expiry: str,                      # YYYYMMDD
    call_strikes: list[float],
    put_strikes: list[float],
    market_data_type: int = 3,
    host: str = "127.0.0.1",
    port: int = 7497,
    client_id: int = 8,
    timeout: float = 7.0,
) -> dict[tuple[float, str], dict[str, float]]:
    """
    Fetch real IBKR bid/ask/IV for a RANGE of option strikes (one expiry) — Phase B uses
    this to override the yfinance chain's prices before strike selection. IV (modelGreeks)
    trails the bid/ask, so we wait a touch longer once bid/ask coverage is good.

    Returns {(strike, "C"|"P"): {"bid","ask","iv"}} for the strikes that priced; strikes
    that never populate are simply absent (caller keeps yfinance for those).
    """
    if not _IB_AVAILABLE:
        return {}

    ib = IB()
    out: dict[tuple[float, str], dict[str, float]] = {}
    try:
        # M4: connect + M3 error monitor + market-data type via the shared helper.
        await _connect_ibkr(ib, host=host, port=port, client_id=client_id,
                            market_data_type=market_data_type)

        contracts: list[tuple[float, str, Any]] = []
        for right, strikes in (("C", call_strikes), ("P", put_strikes)):
            for k in strikes:
                opt = Option(symbol=ticker, lastTradeDateOrContractMonth=expiry,
                             strike=float(k), right=right, exchange="SMART",
                             currency="USD", multiplier="100")
                contracts.append((float(k), right, opt))
        if not contracts:
            return {}
        try:
            await ib.qualifyContractsAsync(*[c[2] for c in contracts])
        except Exception:
            pass

        tickers = [(k, right, ib.reqMktData(opt, "", False, False))
                   for k, right, opt in contracts if getattr(opt, "conId", 0)]
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        target = max(1, int(len(tickers) * 0.8))

        def _coverage() -> int:
            return sum(1 for _, _, tk in tickers if _valid_quote(tk.bid) and _valid_quote(tk.ask))

        while loop.time() < deadline:
            await asyncio.sleep(0.4)
            if _coverage() >= target:
                await asyncio.sleep(1.0)   # let IV/greeks catch up to the bid/ask
                break

        for k, right, tk in tickers:
            if _valid_quote(tk.bid) and _valid_quote(tk.ask):
                g = getattr(tk, "modelGreeks", None)
                iv = getattr(g, "impliedVol", None) if g else None
                out[(k, right)] = {
                    "bid": float(tk.bid), "ask": float(tk.ask),
                    "iv": float(iv) if iv and iv > 0 else 0.0,
                }
            try:
                ib.cancelMktData(tk.contract)
            except Exception:
                pass
        return out
    finally:
        if ib.isConnected():
            ib.disconnect()

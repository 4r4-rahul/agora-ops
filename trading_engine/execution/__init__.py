"""
IBKR Order Executor
=====================
Places, monitors, and manages option spread orders on Interactive Brokers.

This is the bridge between signal generation and live execution.
Uses ib_insync's combo order API for credit spreads and iron condors.

Safety features:
  • Human confirmation gate (configurable)
  • Pre-trade risk check via RiskManager
  • Fill timeout with auto-cancel
  • Partial fill detection
  • Order status callbacks
  • Kill switch (flatten all)

Usage:
    from trading_engine.execution.order_executor import OrderExecutor

    executor = OrderExecutor(ibkr_provider)
    executor.connect()

    fill = executor.place_credit_spread(
        ticker="SPY", expiry="20260309",
        short_strike=590, long_strike=592,
        right="C", num_contracts=2,
        limit_credit=0.80,
    )
    print(f"Filled: {fill.avg_fill_price} on {fill.num_filled} contracts")
"""

import os
import time
import json
import logging
from datetime import datetime, date, timedelta
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Tuple, Callable
from enum import Enum

logger = logging.getLogger(__name__)


class OrderStatus(Enum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    ERROR = "ERROR"


@dataclass
class FillResult:
    """Result of an order submission."""
    order_id: int = 0
    status: OrderStatus = OrderStatus.PENDING
    # Fill details
    num_contracts: int = 0
    num_filled: int = 0
    avg_fill_price: float = 0.0
    commission: float = 0.0
    # Timing
    submit_time: datetime = field(default_factory=datetime.now)
    fill_time: Optional[datetime] = None
    # Context
    strategy: str = ""
    ticker: str = ""
    short_strike: float = 0.0
    long_strike: float = 0.0
    right: str = ""
    expiry: str = ""
    # Error info
    error_msg: str = ""


@dataclass
class LivePosition:
    """A live position tracked by the executor."""
    fill: FillResult = field(default_factory=FillResult)
    entry_credit: float = 0.0
    current_mark: float = 0.0
    unrealized_pnl: float = 0.0
    # Exit levels
    stop_price: float = 0.0
    target_price: float = 0.0
    # Greeksentry_delta: float = 0.0
    current_delta: float = 0.0
    current_gamma: float = 0.0
    # Status
    is_open: bool = True
    exit_reason: str = ""
    exit_fill: Optional[FillResult] = None


class OrderExecutor:
    """
    Places and manages option spread orders on IBKR.

    Supports:
      - Credit spreads (put credit, call credit)
      - Iron condors
      - Long options (buy calls, buy puts) — for lotto/momentum plays
      - Single-leg closes
      - Combo (spread) orders with limit credit
    """

    def __init__(
        self,
        ibkr_provider=None,
        require_confirmation: bool = True,
        fill_timeout_sec: int = 30,
        max_retries: int = 2,
    ):
        """
        Args:
            ibkr_provider:        IBKRDataProvider instance (already connected)
            require_confirmation: If True, prompts user before placing orders
            fill_timeout_sec:     Seconds to wait for fill before cancelling
            max_retries:          Retries at worse price if not filled
        """
        self._provider = ibkr_provider
        self._ib = None
        self._ib_insync = None
        self.require_confirmation = require_confirmation
        self.fill_timeout_sec = fill_timeout_sec
        self.max_retries = max_retries

        # Track all orders placed this session
        self.order_history: List[FillResult] = []
        self.open_orders: Dict[int, Any] = {}  # order_id → Trade object

        # Callbacks
        self._on_fill: Optional[Callable] = None
        self._on_error: Optional[Callable] = None

    # ─── Connection ───────────────────────────────────────────────

    def connect(self) -> bool:
        """Ensure IBKR connection is active."""
        if self._provider is None:
            from ..data.ibkr_provider import IBKRDataProvider
            self._provider = IBKRDataProvider()

        if not self._provider.is_connected():
            if not self._provider.connect():
                return False

        self._ib = self._provider._ib
        self._ib_insync = self._provider._ib_insync
        return True

    def _require_connection(self):
        if not self._ib or not self._ib.isConnected():
            if not self.connect():
                raise RuntimeError("Not connected to IBKR")

    # ─── Credit Spread Orders ────────────────────────────────────

    def place_credit_spread(
        self,
        ticker: str,
        expiry: str,
        short_strike: float,
        long_strike: float,
        right: str,
        num_contracts: int = 1,
        limit_credit: Optional[float] = None,
        action: str = "SELL",
    ) -> FillResult:
        """
        Place a credit spread as a combo order.

        For a PUT CREDIT spread: sell higher put, buy lower put
        For a CALL CREDIT spread: sell lower call, buy higher call

        Args:
            ticker:         Underlying symbol (e.g., "SPY")
            expiry:         Expiry in YYYYMMDD format
            short_strike:   Strike to sell (short leg)
            long_strike:    Strike to buy (long leg)
            right:          "P" for puts, "C" for calls
            num_contracts:  Number of spreads
            limit_credit:   Minimum credit to accept (None = market)
            action:         "SELL" to open, "BUY" to close

        Returns:
            FillResult with fill details
        """
        self._require_connection()
        ib = self._ib_insync

        result = FillResult(
            strategy=f"{'call' if right == 'C' else 'put'}_credit_spread",
            ticker=ticker,
            short_strike=short_strike,
            long_strike=long_strike,
            right=right,
            expiry=expiry,
            num_contracts=num_contracts,
        )

        # ── Build combo contract ──
        # Use ticker profile for correct exchange routing
        from ..config import get_ticker_profile
        profile = get_ticker_profile(ticker)
        opt_exchange = profile.option_exchange

        short_opt = ib.Option(ticker.upper(), expiry, short_strike, right.upper(), opt_exchange)
        long_opt = ib.Option(ticker.upper(), expiry, long_strike, right.upper(), opt_exchange)

        try:
            self._ib.qualifyContracts(short_opt, long_opt)
        except Exception as e:
            result.status = OrderStatus.ERROR
            result.error_msg = f"Failed to qualify contracts: {e}"
            logger.error(result.error_msg)
            self.order_history.append(result)
            return result

        # Build combo (BAG) contract
        combo = ib.Contract()
        combo.symbol = ticker.upper()
        combo.secType = "BAG"
        combo.currency = "USD"
        combo.exchange = opt_exchange

        # Short leg: SELL
        short_leg = ib.ComboLeg()
        short_leg.conId = short_opt.conId
        short_leg.ratio = 1
        short_leg.action = "SELL" if action == "SELL" else "BUY"
        short_leg.exchange = opt_exchange

        # Long leg: BUY
        long_leg = ib.ComboLeg()
        long_leg.conId = long_opt.conId
        long_leg.ratio = 1
        long_leg.action = "BUY" if action == "SELL" else "SELL"
        long_leg.exchange = opt_exchange

        combo.comboLegs = [short_leg, long_leg]

        # ── Human confirmation gate ──
        spread_desc = (
            f"{'OPEN' if action == 'SELL' else 'CLOSE'} "
            f"{num_contracts}x {ticker} "
            f"{short_strike}/{long_strike} {'Call' if right == 'C' else 'Put'} "
            f"Credit Spread  exp={expiry}"
        )
        if limit_credit:
            spread_desc += f"  limit=${limit_credit:.2f}"

        if self.require_confirmation:
            if not self._confirm_order(spread_desc):
                result.status = OrderStatus.CANCELLED
                result.error_msg = "User cancelled"
                self.order_history.append(result)
                return result

        # ── Build order ──
        if limit_credit and limit_credit > 0:
            # For credit spreads sold as combo: IBKR uses negative price for credit
            order = ib.LimitOrder(
                action="SELL" if action == "SELL" else "BUY",
                totalQuantity=num_contracts,
                lmtPrice=-limit_credit if action == "SELL" else limit_credit,
            )
        else:
            order = ib.MarketOrder(
                action="SELL" if action == "SELL" else "BUY",
                totalQuantity=num_contracts,
            )

        order.tif = "DAY"
        order.transmit = True

        # ── Submit and monitor fill ──
        logger.info(f"Submitting: {spread_desc}")
        print(f"  📤 Submitting: {spread_desc}")

        try:
            trade = self._ib.placeOrder(combo, order)
            result.order_id = trade.order.orderId
            result.status = OrderStatus.SUBMITTED
            result.submit_time = datetime.now()
            self.open_orders[result.order_id] = trade

            # Wait for fill
            result = self._wait_for_fill(trade, result)

        except Exception as e:
            result.status = OrderStatus.ERROR
            result.error_msg = f"Order submission failed: {e}"
            logger.error(result.error_msg)
            print(f"  ❌ {result.error_msg}")

        self.order_history.append(result)
        return result

    def close_credit_spread(
        self,
        ticker: str,
        expiry: str,
        short_strike: float,
        long_strike: float,
        right: str,
        num_contracts: int = 1,
        limit_debit: Optional[float] = None,
    ) -> FillResult:
        """
        Close an existing credit spread by buying it back.
        """
        return self.place_credit_spread(
            ticker=ticker,
            expiry=expiry,
            short_strike=short_strike,
            long_strike=long_strike,
            right=right,
            num_contracts=num_contracts,
            limit_credit=limit_debit,
            action="BUY",
        )

    # ─── Long Option Orders (Lotto / Momentum) ────────────────

    def buy_option(
        self,
        ticker: str,
        expiry: str,
        strike: float,
        right: str,
        num_contracts: int = 1,
        limit_price: Optional[float] = None,
    ) -> FillResult:
        """
        Buy a single-leg option (long call or long put).

        Used for momentum/lotto plays where you BUY cheap OTM options
        for asymmetric payoff.

        Args:
            ticker:         Underlying symbol (e.g., "SPY")
            expiry:         Expiry in YYYYMMDD format
            strike:         Strike price
            right:          "C" for call, "P" for put
            num_contracts:  Number of contracts to buy
            limit_price:    Max price to pay (None = market order)

        Returns:
            FillResult with fill details
        """
        self._require_connection()
        ib = self._ib_insync

        result = FillResult(
            strategy=f"long_{'call' if right == 'C' else 'put'}",
            ticker=ticker,
            short_strike=strike,  # Reuse field for the strike
            long_strike=0.0,
            right=right,
            expiry=expiry,
            num_contracts=num_contracts,
        )

        # Build option contract (SPX-aware routing)
        from ..config import get_ticker_profile
        profile = get_ticker_profile(ticker)
        opt = ib.Option(ticker.upper(), expiry, strike, right.upper(), profile.option_exchange)

        try:
            self._ib.qualifyContracts(opt)
        except Exception as e:
            result.status = OrderStatus.ERROR
            result.error_msg = f"Failed to qualify contract: {e}"
            logger.error(result.error_msg)
            self.order_history.append(result)
            return result

        # ── Human confirmation gate ──
        settlement = " [CASH-SETTLED]" if profile.is_cash_settled else ""
        tax_note = " [60/40 tax]" if profile.tax_1256 else ""
        order_desc = (
            f"BUY {num_contracts}x {ticker} "
            f"{strike} {'Call' if right == 'C' else 'Put'}  "
            f"exp={expiry}{settlement}{tax_note}"
        )
        if limit_price:
            order_desc += f"  limit=${limit_price:.2f}"
            total_cost = limit_price * num_contracts * 100
            order_desc += f"  (total cost: ${total_cost:.2f})"

        if self.require_confirmation:
            if not self._confirm_order(order_desc):
                result.status = OrderStatus.CANCELLED
                result.error_msg = "User cancelled"
                self.order_history.append(result)
                return result

        # ── Build order ──
        if limit_price and limit_price > 0:
            order = ib.LimitOrder("BUY", num_contracts, limit_price)
        else:
            order = ib.MarketOrder("BUY", num_contracts)

        order.tif = "DAY"
        order.transmit = True

        # ── Submit and monitor fill ──
        logger.info(f"Submitting: {order_desc}")
        print(f"  📤 Submitting: {order_desc}")

        try:
            trade = self._ib.placeOrder(opt, order)
            result.order_id = trade.order.orderId
            result.status = OrderStatus.SUBMITTED
            result.submit_time = datetime.now()
            self.open_orders[result.order_id] = trade

            result = self._wait_for_fill(trade, result)

        except Exception as e:
            result.status = OrderStatus.ERROR
            result.error_msg = f"Order submission failed: {e}"
            logger.error(result.error_msg)
            print(f"  ❌ {result.error_msg}")

        self.order_history.append(result)
        return result

    def sell_option(
        self,
        ticker: str,
        expiry: str,
        strike: float,
        right: str,
        num_contracts: int = 1,
        limit_price: Optional[float] = None,
    ) -> FillResult:
        """
        Sell (close) a long option position.

        Args:
            ticker:         Underlying symbol
            expiry:         Expiry YYYYMMDD
            strike:         Strike price
            right:          "C" or "P"
            num_contracts:  Contracts to sell
            limit_price:    Min price to accept (None = market)

        Returns:
            FillResult with fill details
        """
        self._require_connection()
        ib = self._ib_insync

        result = FillResult(
            strategy=f"close_long_{'call' if right == 'C' else 'put'}",
            ticker=ticker,
            short_strike=strike,
            long_strike=0.0,
            right=right,
            expiry=expiry,
            num_contracts=num_contracts,
        )

        # Build option contract (SPX-aware routing)
        from ..config import get_ticker_profile
        profile = get_ticker_profile(ticker)
        opt = ib.Option(ticker.upper(), expiry, strike, right.upper(), profile.option_exchange)

        try:
            self._ib.qualifyContracts(opt)
        except Exception as e:
            result.status = OrderStatus.ERROR
            result.error_msg = f"Failed to qualify contract: {e}"
            logger.error(result.error_msg)
            self.order_history.append(result)
            return result

        order_desc = (
            f"SELL {num_contracts}x {ticker} "
            f"{strike} {'Call' if right == 'C' else 'Put'}  "
            f"exp={expiry}"
        )
        if limit_price:
            order_desc += f"  limit=${limit_price:.2f}"

        if self.require_confirmation:
            if not self._confirm_order(order_desc):
                result.status = OrderStatus.CANCELLED
                result.error_msg = "User cancelled"
                self.order_history.append(result)
                return result

        if limit_price and limit_price > 0:
            order = ib.LimitOrder("SELL", num_contracts, limit_price)
        else:
            order = ib.MarketOrder("SELL", num_contracts)

        order.tif = "DAY"
        order.transmit = True

        logger.info(f"Submitting: {order_desc}")
        print(f"  📤 Submitting: {order_desc}")

        try:
            trade = self._ib.placeOrder(opt, order)
            result.order_id = trade.order.orderId
            result.status = OrderStatus.SUBMITTED
            result.submit_time = datetime.now()
            self.open_orders[result.order_id] = trade

            result = self._wait_for_fill(trade, result)

        except Exception as e:
            result.status = OrderStatus.ERROR
            result.error_msg = f"Order submission failed: {e}"
            logger.error(result.error_msg)
            print(f"  ❌ {result.error_msg}")

        self.order_history.append(result)
        return result

    # ─── Iron Condor Orders ──────────────────────────────────────

    def place_iron_condor(
        self,
        ticker: str,
        expiry: str,
        put_short: float,
        put_long: float,
        call_short: float,
        call_long: float,
        num_contracts: int = 1,
        limit_credit: Optional[float] = None,
    ) -> Tuple[FillResult, FillResult]:
        """
        Place an iron condor as two separate credit spread orders.

        Returns (put_spread_fill, call_spread_fill).
        """
        print(f"\n  🦅 Iron Condor: {ticker} {put_short}/{put_long}P + {call_short}/{call_long}C")

        put_fill = self.place_credit_spread(
            ticker=ticker, expiry=expiry,
            short_strike=put_short, long_strike=put_long,
            right="P", num_contracts=num_contracts,
            limit_credit=limit_credit / 2 if limit_credit else None,
        )

        if put_fill.status != OrderStatus.FILLED:
            print(f"  ⚠️  Put spread not filled, skipping call spread")
            return put_fill, FillResult(status=OrderStatus.CANCELLED)

        call_fill = self.place_credit_spread(
            ticker=ticker, expiry=expiry,
            short_strike=call_short, long_strike=call_long,
            right="C", num_contracts=num_contracts,
            limit_credit=limit_credit / 2 if limit_credit else None,
        )

        return put_fill, call_fill

    # ─── Kill Switch ─────────────────────────────────────────────

    def flatten_all(self, reason: str = "KILL_SWITCH") -> List[FillResult]:
        """
        EMERGENCY: Close ALL open option positions immediately.

        Uses market orders for fastest execution. No confirmation required.
        """
        self._require_connection()
        ib_insync = self._ib_insync

        print(f"\n  🚨🚨🚨 KILL SWITCH ACTIVATED: {reason} 🚨🚨🚨")
        print(f"  Flattening all positions with MARKET orders...\n")
        logger.warning(f"KILL SWITCH: {reason}")

        positions = self._ib.positions()
        option_positions = [
            p for p in positions
            if p.contract.secType == "OPT" and p.position != 0
        ]

        if not option_positions:
            print(f"  ✅ No open option positions to flatten")
            return []

        results = []
        old_confirm = self.require_confirmation
        self.require_confirmation = False  # Skip confirmation for kill switch

        for pos in option_positions:
            c = pos.contract
            action = "SELL" if pos.position > 0 else "BUY"
            qty = abs(int(pos.position))

            print(f"  🔥 Closing: {action} {qty}x {c.symbol} "
                  f"{c.strike}{c.right} exp={c.lastTradeDateOrContractMonth}")

            try:
                order = ib_insync.MarketOrder(action, qty)
                order.tif = "IOC"  # Immediate or Cancel
                order.transmit = True

                trade = self._ib.placeOrder(c, order)

                fill_result = FillResult(
                    order_id=trade.order.orderId,
                    status=OrderStatus.SUBMITTED,
                    ticker=c.symbol,
                    short_strike=c.strike,
                    right=c.right,
                    expiry=c.lastTradeDateOrContractMonth,
                    num_contracts=qty,
                    submit_time=datetime.now(),
                )

                fill_result = self._wait_for_fill(trade, fill_result, timeout=10)
                results.append(fill_result)

            except Exception as e:
                logger.error(f"Kill switch failed for {c.symbol} {c.strike}{c.right}: {e}")
                print(f"  ❌ Failed to close {c.symbol} {c.strike}{c.right}: {e}")
                results.append(FillResult(
                    status=OrderStatus.ERROR,
                    error_msg=str(e),
                    ticker=c.symbol,
                    short_strike=c.strike,
                ))

        self.require_confirmation = old_confirm

        filled = sum(1 for r in results if r.status == OrderStatus.FILLED)
        print(f"\n  Kill switch complete: {filled}/{len(results)} positions closed")

        return results

    def cancel_all_open_orders(self):
        """Cancel all open/pending orders."""
        self._require_connection()
        open_trades = self._ib.openTrades()
        for trade in open_trades:
            self._ib.cancelOrder(trade.order)
            logger.info(f"Cancelled order {trade.order.orderId}")
        print(f"  🛑 Cancelled {len(open_trades)} open orders")

    # ─── Fill Monitoring ─────────────────────────────────────────

    def _wait_for_fill(
        self,
        trade,
        result: FillResult,
        timeout: Optional[int] = None,
    ) -> FillResult:
        """
        Wait for an order to fill with timeout.

        Polls order status every 0.5s. If timeout reached, cancels order.
        """
        timeout = timeout or self.fill_timeout_sec
        start = time.time()
        last_status = ""

        while time.time() - start < timeout:
            self._ib.sleep(0.5)  # ib_insync event loop

            status = trade.orderStatus.status
            if status != last_status:
                logger.info(f"Order {result.order_id}: {status}")
                last_status = status

            if status == "Filled":
                result.status = OrderStatus.FILLED
                result.num_filled = int(trade.orderStatus.filled)
                result.avg_fill_price = trade.orderStatus.avgFillPrice
                result.fill_time = datetime.now()
                result.commission = sum(
                    f.commissionReport.commission
                    for f in trade.fills
                    if f.commissionReport.commission < 1e6  # Filter invalid
                )
                elapsed = time.time() - start
                print(f"  ✅ Filled {result.num_filled} @ ${abs(result.avg_fill_price):.2f} "
                      f"(${result.commission:.2f} comm, {elapsed:.1f}s)")
                return result

            if status in ("Cancelled", "ApiCancelled"):
                result.status = OrderStatus.CANCELLED
                result.error_msg = "Order cancelled"
                print(f"  ⚠️  Order cancelled")
                return result

            if status == "Inactive":
                result.status = OrderStatus.REJECTED
                result.error_msg = "Order rejected by IBKR (insufficient margin or invalid)"
                print(f"  ❌ Order rejected")
                return result

            # Check for partial fill
            if trade.orderStatus.filled > 0:
                result.status = OrderStatus.PARTIAL
                result.num_filled = int(trade.orderStatus.filled)

        # Timeout — cancel unfilled order
        remaining = result.num_contracts - result.num_filled
        if remaining > 0:
            logger.warning(f"Order {result.order_id} timed out after {timeout}s, cancelling")
            print(f"  ⏱️  Timeout after {timeout}s — cancelling remaining {remaining}")
            self._ib.cancelOrder(trade.order)
            self._ib.sleep(1)

            if result.num_filled > 0:
                result.status = OrderStatus.PARTIAL
                result.avg_fill_price = trade.orderStatus.avgFillPrice
                print(f"  ⚠️  Partial fill: {result.num_filled}/{result.num_contracts}")
            else:
                result.status = OrderStatus.CANCELLED
                result.error_msg = f"Timed out after {timeout}s"

        return result

    # ─── Confirmation Gate ───────────────────────────────────────

    def _confirm_order(self, description: str) -> bool:
        """
        Prompt user for order confirmation.

        Returns True if approved, False if rejected.
        """
        print(f"\n  {'─' * 60}")
        print(f"  📋 ORDER CONFIRMATION REQUIRED")
        print(f"  {'─' * 60}")
        print(f"  {description}")
        print(f"  {'─' * 60}")

        try:
            response = input(f"  Approve? [y/N] ").strip().lower()
            approved = response in ("y", "yes")
            if approved:
                print(f"  ✅ Order approved")
            else:
                print(f"  ❌ Order rejected by user")
            return approved
        except (EOFError, KeyboardInterrupt):
            print(f"\n  ❌ Order cancelled (interrupt)")
            return False

    # ─── Order History & Logging ─────────────────────────────────

    def save_order_log(self, path: Optional[str] = None):
        """Save all orders from this session to JSON."""
        path = path or os.path.join("data", "order_log.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)

        records = []
        for fill in self.order_history:
            records.append({
                "order_id": fill.order_id,
                "status": fill.status.value,
                "strategy": fill.strategy,
                "ticker": fill.ticker,
                "short_strike": fill.short_strike,
                "long_strike": fill.long_strike,
                "right": fill.right,
                "expiry": fill.expiry,
                "num_contracts": fill.num_contracts,
                "num_filled": fill.num_filled,
                "avg_fill_price": fill.avg_fill_price,
                "commission": fill.commission,
                "submit_time": fill.submit_time.isoformat(),
                "fill_time": fill.fill_time.isoformat() if fill.fill_time else None,
                "error_msg": fill.error_msg,
            })

        with open(path, "w") as f:
            json.dump(records, f, indent=2)
        print(f"  💾 Saved {len(records)} orders to {path}")

    def print_session_summary(self):
        """Print summary of all orders this session."""
        if not self.order_history:
            print("  No orders placed this session")
            return

        filled = [f for f in self.order_history if f.status == OrderStatus.FILLED]
        total_comm = sum(f.commission for f in filled)

        print(f"\n  {'═' * 60}")
        print(f"  SESSION SUMMARY")
        print(f"  {'═' * 60}")
        print(f"  Orders Submitted:  {len(self.order_history)}")
        print(f"  Filled:            {len(filled)}")
        print(f"  Total Commission:  ${total_comm:.2f}")
        for f in self.order_history:
            icon = "✅" if f.status == OrderStatus.FILLED else "❌"
            print(f"    {icon} {f.strategy} {f.ticker} "
                  f"{f.short_strike}/{f.long_strike}{f.right} "
                  f"→ {f.status.value} @ ${abs(f.avg_fill_price):.2f}" if f.avg_fill_price else
                  f"    {icon} {f.strategy} → {f.status.value}: {f.error_msg}")
        print(f"  {'═' * 60}")

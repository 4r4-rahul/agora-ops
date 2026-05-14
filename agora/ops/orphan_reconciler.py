"""
OrphanOrderReconciler — reconciles IBKR open orders vs our position DB.

Runs at session startup and every 30 minutes during market hours.

The Error 201 problem:
  IBKR limits the number of concurrent "riskless/guaranteed-loss combination
  orders." A BAG (combo) bracket creates one such order per profit-target GTC
  child. If a session crashed or restarted while a GTC child was live, that
  child consumes the slot indefinitely — causing Error 201 on every subsequent
  bracket until the orphan is cancelled.

What we do:
  1. Fetch ALL open orders across ALL clientIds (reqAllOpenOrdersAsync)
  2. Fetch today's executions (reqExecutionsAsync) — catch filled GTC closes
  3. For filled GTC closes: mark the matching shadow-book position as closed
  4. For orphan GTC children (no matching position, parent gone): cancel them
  5. Report counts and escalate to COO if action taken
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from zoneinfo import ZoneInfo

from ..core.config import AgoraSettings, get_settings

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

_RECONCILE_INTERVAL_SEC = 1800  # 30 min
_IBKR_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ibkr-reconcile")


def _run_in_new_loop(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        loop.close()
        asyncio.set_event_loop(None)


class OrphanOrderReconciler:
    """
    Cancels IBKR GTC child orders that have no matching active position,
    and closes shadow-book positions whose GTC profit-target has already filled.
    """

    def __init__(
        self,
        settings: AgoraSettings | None = None,
        position_mgr: Any = None,
        ceo_agent: Any = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._position_mgr = position_mgr
        self._ceo = ceo_agent
        self._csuite_manager: Any = None
        self._running = False
        self._last_orphans_cancelled: int = 0
        self._last_positions_synced: int = 0

    def register_csuite_manager(self, manager: Any) -> None:
        self._csuite_manager = manager

    async def start(self) -> None:
        self._running = True
        await self._reconcile()
        while self._running:
            await asyncio.sleep(_RECONCILE_INTERVAL_SEC)
            from datetime import datetime
            now_et = datetime.now(tz=ET)
            if 8 <= now_et.hour < 20:
                await self._reconcile()

    async def stop(self) -> None:
        self._running = False

    async def reconcile_now(self) -> int:
        """On-demand reconcile. Returns number of orphans cancelled."""
        return await self._reconcile()

    @property
    def last_orphans_cancelled(self) -> int:
        return self._last_orphans_cancelled

    @property
    def last_positions_synced(self) -> int:
        return self._last_positions_synced

    # ── Core logic ──────────────────────────────────────────────────

    async def _reconcile(self) -> int:
        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                _IBKR_EXECUTOR,
                lambda: _run_in_new_loop(self._reconcile_sync()),
            )
            cancelled, synced = result if isinstance(result, tuple) else (result or 0, 0)
            self._last_orphans_cancelled = cancelled
            self._last_positions_synced  = synced
            return cancelled
        except Exception as exc:
            logger.debug("OrphanReconciler: IBKR not available — %s", exc)
            return 0

    async def _reconcile_sync(self) -> tuple[int, int]:
        try:
            from ib_insync import IB
        except ImportError:
            return (0, 0)

        ib = IB()
        try:
            await ib.connectAsync(
                self._settings.ibkr_host,
                self._settings.ibkr_port,
                clientId=9,
                timeout=10,
            )
        except Exception as exc:
            logger.debug("OrphanReconciler: IBKR connect failed — %s", exc)
            return (0, 0)

        try:
            # ── Step 1: Fetch ALL open orders across ALL clientIds ────────────
            # ib.trades() only returns orders from THIS session's clientId.
            # reqAllOpenOrdersAsync returns orders placed by any clientId (2, 3, etc.)
            all_open_trades = await ib.reqAllOpenOrdersAsync()

            # ── Step 2: Check today's fills — catch GTC closes we missed ─────
            synced = await self._sync_filled_closes(ib)

            if not all_open_trades:
                logger.debug("OrphanReconciler: no open orders in TWS")
                return (0, synced)

            # Active tickers in shadow book
            active_tickers: set[str] = set()
            if self._position_mgr:
                active_tickers = {p.ticker for p in self._position_mgr.get_open_positions()}

            # Build set of parent IDs that still have an active open entry order
            active_entry_ids: set[int] = {
                t.order.orderId
                for t in all_open_trades
                if t.order.parentId == 0
                and t.orderStatus.status in ("Submitted", "PreSubmitted", "PendingSubmit")
            }

            # ── Step 3: Cancel orphan GTC children ───────────────────────────
            cancelled = 0
            for trade in all_open_trades:
                order    = trade.order
                contract = trade.contract

                # Only interested in GTC profit-target children
                is_gtc_child = order.parentId > 0 and order.tif == "GTC"
                if not is_gtc_child:
                    continue

                parent_still_open = order.parentId in active_entry_ids
                has_db_position   = contract.symbol in active_tickers

                if not parent_still_open and not has_db_position:
                    logger.warning(
                        "OrphanReconciler: cancelling orphan GTC orderId=%d ticker=%s "
                        "(parent gone, no DB position)",
                        order.orderId, contract.symbol,
                    )
                    try:
                        ib.cancelOrder(order)
                        await asyncio.sleep(0.5)
                        cancelled += 1
                    except Exception as exc:
                        logger.warning(
                            "OrphanReconciler: cancel failed orderId=%d — %s",
                            order.orderId, exc,
                        )

            if cancelled > 0:
                msg = (
                    f"OrphanOrderReconciler cancelled {cancelled} orphaned GTC order(s). "
                    f"IBKR combo slot freed."
                )
                logger.info(msg)
                await self._notify("info", f"♻️ {msg}")
            else:
                logger.debug(
                    "OrphanReconciler: %d open orders checked, 0 orphans found",
                    len(all_open_trades),
                )

            return (cancelled, synced)

        finally:
            try:
                ib.disconnect()
            except Exception:
                pass

    async def _sync_filled_closes(self, ib: Any) -> int:
        """
        Fetch today's executions. For every BAG sell fill (GTC close that fired)
        whose ticker is still open in our shadow book, mark that position closed.
        Returns count of positions synced.
        """
        if self._position_mgr is None:
            return 0

        try:
            fills = await ib.reqExecutionsAsync()
        except Exception as exc:
            logger.debug("OrphanReconciler: reqExecutionsAsync failed — %s", exc)
            return 0

        bag_sells = [
            f for f in fills
            if getattr(f.contract, "secType", "") == "BAG"
            and getattr(f.execution, "side", "") == "SLD"
        ]

        if not bag_sells:
            return 0

        open_by_ticker = {
            p.ticker: p for p in self._position_mgr.get_open_positions()
        }

        synced = 0
        for fill in bag_sells:
            ticker = fill.contract.symbol
            pos = open_by_ticker.get(ticker)
            if pos is None:
                continue  # already closed or never in shadow book

            close_price  = float(fill.execution.price)
            realized_pnl = round(
                (close_price - pos.entry_price) * 100 * pos.contracts, 2
            )
            closed = self._position_mgr.mark_position_closed(
                position_id=pos.position_id,
                realized_pnl=realized_pnl,
                close_price=close_price,
                source="tws_orphan_reconcile",
            )
            if closed:
                logger.info(
                    "OrphanReconciler: synced close for %s | "
                    "fill=$%.4f entry=$%.4f pnl=$%.2f",
                    ticker, close_price, pos.entry_price, realized_pnl,
                )
                synced += 1

        if synced > 0:
            await self._notify(
                "info",
                f"♻️ OrphanReconciler synced {synced} position close(s) from TWS fills.",
            )

        return synced

    async def _notify(self, level: str, message: str) -> None:
        if self._csuite_manager:
            await self._csuite_manager.receive_alert("OrphanOrderReconciler", level, message)
        elif self._ceo:
            await self._ceo.dispatch_alert(level, message)

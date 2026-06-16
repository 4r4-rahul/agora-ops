"""
agora/ops/fill_canary.py — broker fill-engine canary.

Answers ONE question definitively: will the IBKR (paper) account fill a maximally-marketable
order at all? It places a 1-contract BUY on the most liquid option there is — SPY, ~ATM, a
near-dated weekly — priced THROUGH the ask (ask + buffer), waits briefly, records fill/no-fill,
then immediately flattens so nothing lingers. No strategy, no signals, no pricing heuristics —
just "marketable order in, did it fill?".

If even THIS won't fill, the ~2% strategy fill rate is the paper account's fill engine / market
data, not our execution code — and the fix is a TWS/market-data change, not more order logic.
If it DOES fill, our leg-by-leg + marketable-start path should too, and the gap is upstream.

Self-contained + never raises. Reuses the execution primitives so it tests the SAME plumbing.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_TICK = 0.01


def _log_canary(db_path: str | None, row: dict[str, Any]) -> None:
    if not db_path:
        return
    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS canary_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_utc TEXT, ticker TEXT, expiry TEXT, strike REAL, right TEXT,
                    quote_bid REAL, quote_ask REAL, marketable_limit REAL,
                    outcome TEXT, fill_price REAL, fill_secs REAL, slippage_vs_ask REAL,
                    flattened INTEGER, note TEXT
                )"""
            )
            conn.execute(
                """INSERT INTO canary_log (ts_utc,ticker,expiry,strike,right,quote_bid,quote_ask,
                       marketable_limit,outcome,fill_price,fill_secs,slippage_vs_ask,flattened,note)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (row.get("ts_utc"), row.get("ticker"), row.get("expiry"), row.get("strike"),
                 row.get("right"), row.get("quote_bid"), row.get("quote_ask"),
                 row.get("marketable_limit"), row.get("outcome"), row.get("fill_price"),
                 row.get("fill_secs"), row.get("slippage_vs_ask"),
                 1 if row.get("flattened") else 0, row.get("note")),
            )
    except Exception as exc:
        logger.debug("canary_log write failed: %s", exc)


async def run_fill_canary(
    *,
    host: str = "127.0.0.1",
    port: int = 7497,
    client_id: int = 99,               # distinct id — never collides with the engine's block
    market_data_type: int = 1,         # live OPRA
    ticker: str = "SPY",
    dte: int = 7,
    wait_secs: int = 25,
    buffer_pct: float = 0.12,          # price THROUGH the ask — maximally marketable
    db_path: str | None = None,
) -> dict[str, Any]:
    """Place + flatten a 1-lot marketable SPY ATM call. Returns a result dict; never raises."""
    from ib_insync import IB, Stock, Option, LimitOrder

    from trading_platform.services.ibkr_client import (
        _connect_ibkr, _qualify, _next_expiry, _valid_quote,
    )

    res: dict[str, Any] = {
        "ts_utc": datetime.now(timezone.utc).isoformat(), "ticker": ticker,
        "expiry": None, "strike": None, "right": "C", "quote_bid": None, "quote_ask": None,
        "marketable_limit": None, "outcome": "error", "fill_price": None, "fill_secs": None,
        "slippage_vs_ask": None, "flattened": False, "note": "",
    }
    ib = IB()
    try:
        await _connect_ibkr(ib, host=host, port=port, client_id=client_id,
                            market_data_type=market_data_type, session_id="CANARY")

        # ── Spot → nearest $1 ATM strike. The account has OPRA (options) but NOT real-time
        # stock data (Error 10089), so IBKR's underlying quote is NaN — fall back to yfinance,
        # which is what the engine uses for spot everywhere. We only need spot to pick the strike;
        # the OPTION NBBO we trade on IS subscribed (OPRA). ──
        import math
        spot = float("nan")
        try:
            [spy] = await ib.reqTickersAsync(Stock(ticker, "SMART", "USD"))
            for cand in (spy.marketPrice(), spy.last, spy.close):
                if _valid_quote(cand):
                    spot = float(cand); break
        except Exception:
            pass
        if not (spot == spot) or spot <= 0:   # NaN or non-positive → yfinance
            try:
                import yfinance as yf
                spot = float(yf.Ticker(ticker).fast_info["last_price"])
            except Exception as exc:
                res["note"] = f"no spot (IBKR stock unsubscribed + yfinance failed: {exc})"
                _log_canary(db_path, res); return res
        if not (spot == spot) or spot <= 0:
            res["note"] = "no usable SPY spot"
            _log_canary(db_path, res); return res
        strike = float(round(spot))
        expiry = _next_expiry(dte)
        res["expiry"], res["strike"] = expiry, strike

        # ── Qualify the ATM call, scanning ±2 strikes if the exact one is missing ──
        qual = None
        for ds in (0.0, 1.0, -1.0, 2.0, -2.0):
            opt = Option(ticker, expiry, strike + ds, "C", exchange="SMART")
            qual = await _qualify(ib, opt)
            if qual is not None:
                strike = strike + ds
                res["strike"] = strike
                break
        if qual is None:
            res["note"] = f"no qualifiable ATM contract near {strike} {expiry}"
            _log_canary(db_path, res); return res

        # ── Quote → maximally-marketable BUY limit (through the ask). Use STREAMING reqMktData +
        # a wait loop (the same method the engine's pricing path uses) rather than a snapshot —
        # an option snapshot often returns before the NBBO streams in, reading NaN. ──
        tk = ib.reqMktData(qual, "", False, False)
        bid = ask = float("nan")
        for _ in range(15):                      # up to ~4.5s for the book to populate
            await asyncio.sleep(0.3)
            if _valid_quote(tk.bid) and _valid_quote(tk.ask):
                bid, ask = float(tk.bid), float(tk.ask); break
        try:
            ib.cancelMktData(qual)
        except Exception:
            pass
        res["quote_bid"], res["quote_ask"] = (bid if _valid_quote(bid) else None,
                                              ask if _valid_quote(ask) else None)
        if not _valid_quote(ask):
            res["note"] = (f"no valid option NBBO after 4.5s (bid={bid} ask={ask}) — likely today's "
                           f"post-outage market-data degradation; retry on a clean session.")
            _log_canary(db_path, res); return res
        marketable = round(ask + max(2 * _TICK, ask * buffer_pct), 2)
        res["marketable_limit"] = marketable

        # ── Submit the 1-lot BUY ──
        order = LimitOrder("BUY", 1, marketable)
        order.tif = "DAY"; order.orderRef = "CANARY-FILLTEST"; order.transmit = True
        trade = ib.placeOrder(qual, order)
        t0 = time.monotonic()
        filled = False
        for _ in range(wait_secs):
            await asyncio.sleep(1)
            st = trade.orderStatus.status
            if st == "Filled":
                filled = True; break
            if st in ("Cancelled", "ApiCancelled", "Inactive"):
                break

        if not filled:
            try:
                ib.cancelOrder(order)
            except Exception:
                pass
            res["outcome"] = "no_fill"
            res["note"] = (f"marketable BUY @ {marketable} (ask {ask}) did NOT fill in {wait_secs}s "
                           f"→ status={trade.orderStatus.status}. Broker fill engine is the blocker.")
            logger.warning("[CANARY] %s", res["note"])
            _log_canary(db_path, res); return res

        # Filled — record, then FLATTEN so nothing lingers.
        fill_secs = round(time.monotonic() - t0, 1)
        fpx = trade.orderStatus.avgFillPrice or marketable
        res.update(outcome="filled", fill_price=round(float(fpx), 2), fill_secs=fill_secs,
                   slippage_vs_ask=round(float(fpx) - ask, 2))
        logger.info("[CANARY] FILLED 1 %s %s%.0fC @ %.2f in %.1fs (ask %.2f) — broker fills marketable orders ✓",
                    ticker, expiry, strike, fpx, fill_secs, ask)

        # Flatten with a MARKET order — reliability over price for the cleanup leg (a marketable
        # SELL LIMIT through the bid was observed not to fill, while MKT closes instantly on the
        # liquid ATM contract we just bought). The BUY above is the actual marketable-LIMIT test.
        from ib_insync import MarketOrder
        sell = MarketOrder("SELL", 1)
        sell.tif = "DAY"; sell.orderRef = "CANARY-FLATTEN"; sell.transmit = True
        sell_trade = ib.placeOrder(qual, sell)
        for _ in range(20):
            await asyncio.sleep(0.5)
            if sell_trade.orderStatus.status == "Filled":
                res["flattened"] = True; break
        if not res["flattened"]:
            try:
                ib.cancelOrder(sell)
            except Exception:
                pass
            res["note"] = ("⚠️ FILLED but flatten did not complete — a 1-lot SPY call may be open in "
                           "the paper account (orderRef CANARY-FLATTEN). Verify/close in TWS.")
            logger.warning("[CANARY] %s", res["note"])
        else:
            res["note"] = "round-trip complete (bought + flattened) — nothing left open."
        _log_canary(db_path, res); return res

    except Exception as exc:
        res["note"] = f"canary error: {exc}"
        logger.warning("[CANARY] error: %s", exc)
        _log_canary(db_path, res); return res
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass

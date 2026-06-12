#!/usr/bin/env python3
"""Probe LIVE (type 1 / OPRA) vs DELAYED (type 3) for OPTION quotes — the real OPRA test.

Uses known-valid listed contracts (the account's actual open option positions) so the
request can't fail on a bad strike/expiry. clientId=16 to avoid colliding.
"""
import asyncio


async def main():
    from ib_insync import IB, Option
    ib = IB()
    try:
        await ib.connectAsync("127.0.0.1", 7497, clientId=16, timeout=15)
    except Exception as e:
        print("connect failed:", e); return
    print(f"connected: {ib.isConnected()}")

    # Known-valid listed contracts (held in the account / seen in TWS).
    cons = [
        Option("TSLA", "20260702", 390, "P", "SMART", tradingClass="TSLA"),
        Option("MSFT", "20260710", 385, "P", "SMART", tradingClass="MSFT"),
    ]
    q = await ib.qualifyContractsAsync(*cons)
    cons = [c for c in q if c.conId]
    print("qualified:", [(c.symbol, c.lastTradeDateOrContractMonth, c.strike, c.right, c.conId) for c in cons])
    if not cons:
        print("nothing qualified"); ib.disconnect(); return

    for md_type, label in [(1, "LIVE (type 1 / OPRA)"), (3, "DELAYED (type 3)")]:
        print(f"\n=== {label} ===")
        ib.reqMarketDataType(md_type)
        tickers = [(c, ib.reqMktData(c, "", False, False)) for c in cons]
        await asyncio.sleep(6)
        for c, t in tickers:
            bid = t.bid if t.bid == t.bid else None
            ask = t.ask if t.ask == t.ask else None
            last = t.last if t.last == t.last else None
            tag = {1: "LIVE", 2: "frozen", 3: "DELAYED", 4: "delayed-frozen"}.get(t.marketDataType, "?")
            print(f"  {c.symbol} {c.strike:.0f}{c.right}: bid={bid} ask={ask} last={last}  -> served as {tag}")
        for c, _ in tickers:
            ib.cancelMktData(c)
        await asyncio.sleep(0.5)

    ib.disconnect()
    print("\ndone.")


asyncio.run(main())

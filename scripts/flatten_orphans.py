#!/usr/bin/env python3
"""Flatten IBKR orphan legs (open at broker, untracked in the DB) to reconcile the book.

MUST run during market hours (09:30–16:00 ET): options don't trade after close, and a paper
account has no option market-data subscription, so raw MKT orders sit PreSubmitted. Each leg is
closed with the IBKR Adaptive (Price Management) algo — server-side pricing that fills WITHOUT a
client market-data subscription (the proven fix for this account; Adaptive works on single legs,
no-ops only on BAG combos). Reads the orphan list live from /agora/reconcile.

Usage:  python3 scripts/flatten_orphans.py [--dry]
"""
import asyncio, json, sys, urllib.request
from ib_insync import IB, Option, Order, TagValue

DRY = "--dry" in sys.argv

def orphans():
    with urllib.request.urlopen("http://127.0.0.1:8001/agora/reconcile", timeout=60) as r:
        return json.loads(r.read()).get("orphans", [])

async def main():
    legs = orphans()
    print(f"{len(legs)} orphan legs to flatten")
    if DRY:
        for o in legs:
            print(f"  {'SELL' if o['ibkr_qty']>0 else 'BUY'} {abs(o['ibkr_qty'])}x "
                  f"{o['symbol']} {o['strike']}{o['right']} {o['expiry']}")
        return
    ib = IB(); await ib.connectAsync("127.0.0.1", 7497, clientId=72, timeout=15)
    try:
        trades = []
        for o in legs:
            opt = Option(o["symbol"], o["expiry"], float(o["strike"]), o["right"],
                         exchange="SMART", currency="USD", multiplier="100")
            q = await ib.qualifyContractsAsync(opt)
            if not q or not getattr(q[0], "conId", 0):
                print(f"  SKIP no-qualify {o['symbol']} {o['strike']}{o['right']}"); continue
            order = Order(action=("SELL" if o["ibkr_qty"]>0 else "BUY"),
                          totalQuantity=abs(int(o["ibkr_qty"])), orderType="MKT",
                          tif="DAY", transmit=True, orderRef="FLATTEN_ORPHAN")
            order.algoStrategy = "Adaptive"                      # server-side price mgmt
            order.algoParams = [TagValue("adaptivePriority", "Urgent")]
            trades.append((o, ib.placeOrder(q[0], order)))
        for _ in range(90):
            await asyncio.sleep(1)
            if all(t.orderStatus.status in ("Filled","Cancelled","ApiCancelled","Inactive")
                   for _, t in trades): break
        filled = sum(1 for _, t in trades if t.orderStatus.status == "Filled")
        print(f"RESULT: {filled}/{len(trades)} flattened")
        for o, t in trades:
            if t.orderStatus.status != "Filled":
                print(f"  unfilled: {o['symbol']} {o['strike']}{o['right']} -> {t.orderStatus.status}")
    finally:
        if ib.isConnected(): ib.disconnect()

asyncio.run(main())

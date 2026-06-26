#!/usr/bin/env python3
"""
flatten_paper_orphans.py — one-command CLEAN SLATE for the IBKR paper account.

Brings the broker to EXACTLY the engine's real book: flattens everything that is NOT a real, current
engine strategy position — orphans (untracked) AND adopted junk (tracked with reconstructed/bug-
tainted cost basis). Keeps only the engine's real open trades (DB status open/tested/rolled AND
regime_at_entry != 'adopted').

Why: weeks of execution-bug fixing + DB restatements left a junk pile in the *persistent* paper
account; the reconciler keeps re-adopting it and it pollutes the position views (the AMD −$11,159
naked-leg artifact, the DIA over-fills, etc.). This sweeps it so DB = broker = UI from a clean slate.
Going forward the close-idempotency guard + over-fill auto-flatten + continuous reconciliation keep
the books synced, so the junk pile should not re-accumulate.

MUST run during market hours (options don't trade after close; the paper account has no option
market-data subscription, so a raw MKT order sits PreSubmitted). Each leg is flattened with the IBKR
Adaptive (Price Management) algo — server-side pricing that fills WITHOUT a client market-data sub
(the proven fix for this account; Adaptive works on single legs).

Usage:
  python3 scripts/flatten_paper_orphans.py            # DRY-RUN (default): show what WOULD flatten
  python3 scripts/flatten_paper_orphans.py --execute  # actually flatten + mark adopted closed in DB
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agora.ops.position_reconciler import _norm_expiry, ibkr_legs  # noqa: E402

EXECUTE = "--execute" in sys.argv
DB = ".agora/agora.db"
LIVE_STATUSES = ("open", "tested", "rolled")


def real_engine_legs(db_path: str) -> dict[tuple, int]:
    """Signed qty per leg for REAL engine positions only — non-adopted, live. This is the set we
    KEEP; everything else at the broker is junk to flatten. Key = (ticker, right, strike,
    expiryYYYYMMDD); the ticker comes from the positions row (legs_json carries no symbol)."""
    legs: dict[tuple, int] = {}
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        ph = ",".join("?" * len(LIVE_STATUSES))
        rows = conn.execute(
            f"SELECT ticker, contracts, legs_json FROM positions WHERE status IN ({ph}) "
            f"AND COALESCE(regime_at_entry,'') <> 'adopted'", LIVE_STATUSES,
        ).fetchall()
    for r in rows:
        tk = r["ticker"]
        pos_ct = int(r["contracts"] or 0)
        try:
            parsed = json.loads(r["legs_json"] or "[]")
        except Exception:
            continue
        counts = [int(lg.get("contracts", 0) or 0) for lg in parsed]
        base = min((c for c in counts if c > 0), default=1)
        for lg in parsed:
            right = "C" if str(lg.get("option_type", "")).lower().startswith("c") else "P"
            strike = float(lg.get("strike", 0) or 0)
            expiry = _norm_expiry(str(lg.get("expiration", "")))
            ratio = (int(lg.get("contracts", 0) or 0) or base) / base
            qty = round(pos_ct * ratio)
            sign = 1 if str(lg.get("action", "")).lower() == "buy" else -1
            key = (tk, right, strike, expiry)
            legs[key] = legs.get(key, 0) + sign * qty
    return {k: v for k, v in legs.items() if v != 0}


def flatten_plan(broker: dict[tuple, int], keep: dict[tuple, int]) -> list[dict]:
    """For every broker leg, the order that brings it to the REAL engine quantity (keep). A leg the
    engine doesn't hold (keep=0) is flattened entirely; a leg held at fewer than the broker shows is
    trimmed to the real size."""
    plan = []
    for key, bqty in broker.items():
        kqty = keep.get(key, 0)
        delta = bqty - kqty   # >0 broker has too many → SELL delta; <0 → BUY
        if delta == 0:
            continue
        tk, right, strike, expiry = key
        plan.append({"symbol": tk, "right": right, "strike": strike, "expiry": expiry,
                     "broker_qty": bqty, "keep_qty": kqty,
                     "action": "SELL" if delta > 0 else "BUY", "qty": abs(delta)})
    return plan


async def main() -> int:
    from ib_insync import IB, Option, Order, TagValue
    keep = real_engine_legs(DB)
    print(f"{'DRY-RUN' if not EXECUTE else 'EXECUTE'} — clean-slate the paper account to the real book")
    print(f"  real engine legs to KEEP: {len(keep)}")
    ib = IB()
    try:
        await ib.connectAsync("127.0.0.1", 7497, clientId=74, timeout=15)
    except Exception as exc:
        print(f"  ERROR: cannot reach TWS on 7497 ({exc}). Run during market hours with TWS up.")
        return 1
    try:
        ib.reqPositions(); await asyncio.sleep(2.0)
        broker = ibkr_legs(ib)
        plan = flatten_plan(broker, keep)
        junk_qty = sum(p["qty"] for p in plan)
        print(f"  broker legs: {len(broker)} | JUNK legs to flatten: {len(plan)} ({junk_qty} contracts)")
        for p in plan:
            print(f"    {p['action']} {p['qty']}x {p['symbol']} {p['strike']}{p['right']} {p['expiry']} "
                  f"(broker {p['broker_qty']:+d} → keep {p['keep_qty']:+d})")
        if not plan:
            print("  nothing to flatten — broker already equals the real book ✓")
            return 0
        if not EXECUTE:
            print("\n  DRY-RUN only. Re-run with --execute to flatten + close adopted in the DB.")
            return 0

        trades = []
        for p in plan:
            opt = Option(p["symbol"], p["expiry"], float(p["strike"]), p["right"],
                         exchange="SMART", currency="USD", multiplier="100")
            q = await ib.qualifyContractsAsync(opt)
            if not q or not getattr(q[0], "conId", 0):
                print(f"    SKIP no-qualify {p['symbol']} {p['strike']}{p['right']}"); continue
            o = Order(action=p["action"], totalQuantity=p["qty"], orderType="MKT", tif="DAY",
                      transmit=True, orderRef="FLATTEN_PAPER_CLEANSLATE")
            o.algoStrategy = "Adaptive"
            o.algoParams = [TagValue("adaptivePriority", "Urgent")]
            trades.append((p, ib.placeOrder(q[0], o)))
        for _ in range(120):
            await asyncio.sleep(1)
            if all(t.orderStatus.status in ("Filled", "Cancelled", "ApiCancelled", "Inactive")
                   for _, t in trades):
                break
        filled = sum(1 for _, t in trades if t.orderStatus.status == "Filled")
        print(f"\n  RESULT: {filled}/{len(trades)} junk legs flattened")
        for p, t in trades:
            if t.orderStatus.status != "Filled":
                print(f"    unfilled: {p['symbol']} {p['strike']}{p['right']} -> {t.orderStatus.status}")
    finally:
        if ib.isConnected():
            ib.disconnect()

    # mark adopted DB rows closed + re-derive the ledger so DB mirrors the now-clean broker
    with sqlite3.connect(DB) as conn:
        n = conn.execute(
            "UPDATE positions SET status='closed', close_date=date('now'), realized_pnl=0, "
            "close_price=0, close_source='reconcile_cleanslate' "
            "WHERE COALESCE(regime_at_entry,'')='adopted' AND status IN ('open','tested','rolled')"
        ).rowcount
        conn.commit()
    print(f"  marked {n} adopted DB positions closed (book $0)")
    try:
        from agora.ops.book_manager import rebuild_daily_pnl
        rebuild_daily_pnl(DB)
        print("  ledger re-derived — DB reconciled to the clean broker ✓")
    except Exception as exc:
        print(f"  (ledger rebuild skipped: {exc})")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

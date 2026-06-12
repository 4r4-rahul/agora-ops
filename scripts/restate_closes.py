#!/usr/bin/env python3
"""Restate contaminated closed-position P&L from the REAL close fills logged by IBKR.

Background: the lifecycle/thesis_exit profit engine booked many closes at
close_price=0 (a fabricated max-win) instead of the real fill. This script
recovers the actual close net per share from the logged ``Fill(... orderRef=
'CLOSE_...')`` executions, recomputes realized P&L with the correct signed
formula, and (only with --apply) restates the rows — preserving the originals
in an audit table.

P&L convention (matches the live engine after commit 0700ed4 / 8806b33):
    entry_price       = signed per-share net  (+debit paid / -credit received)
    net_close_signed  = sum(sign(side)*price) over close fills (BOT=+1, SLD=-1)
    realized_pnl      = -(entry_price + net_close_signed) * 100 * contracts
    close_price       = abs(net_close_signed)   (stored as a positive magnitude)

Default is DRY RUN (report only). Pass --apply to write.
"""
from __future__ import annotations

import argparse
import glob
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime

DB = ".agora/agora.db"
LOG_GLOB = "agora/logs/agora-*.log"

# Parse the 'Filled' orderStatus line — IBKR's own avgFillPrice (share-weighted per
# order, no contract double-count) is the precise per-share fill. Sign by the order's
# action: BUY=+1 (debit), SELL=-1 (credit). For a BAG combo, avgFillPrice already IS
# the signed net; for single-leg-routed closes, each leg is its own order and we sum.
_SYMBOL = re.compile(r"symbol='([A-Z]+)'")
_ORDERID = re.compile(r"Order\(orderId=(\d+)")
# Anchor on Order( so we capture the ORDER's action, not a ComboLeg's action= that
# appears earlier on a BAG line (that earlier match flipped the sign on combo closes).
_ACTION = re.compile(r"Order\(orderId=\d+[^)]*?action='(BUY|SELL)'")
_REF    = re.compile(r"orderRef='(CLOSE_[^']+)'")
_AVGFILL = re.compile(r"avgFillPrice=(-?[\d.]+)")
_FILLED = re.compile(r"filled=([\d.]+)")
_DATE   = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def parse_close_fills() -> dict:
    """Return {base_ref|symbol: {symbol, net_signed, qty, dt}} from the logs.

    Uses each CLOSE order's final avgFillPrice (IBKR's share-weighted average) so
    multi-contract / multi-fill closes are NOT double-counted. net_signed =
    sum over the position's close orders of sign(action)*avgFillPrice =
    +debit paid / -credit received to close.
    """
    # orderId -> latest Filled snapshot (dedupe partial->complete; keep final avg).
    orders: dict[int, dict] = {}
    for path in sorted(glob.glob(LOG_GLOB)):
        with open(path, errors="ignore") as fh:
            for line in fh:
                if "orderRef='CLOSE_" not in line or "status='Filled'" not in line:
                    continue
                ref = _REF.search(line)
                oid = _ORDERID.search(line)
                act = _ACTION.search(line)
                avg = _AVGFILL.search(line)
                if not (ref and oid and act and avg):
                    continue
                sym = _SYMBOL.search(line)
                fl  = _FILLED.search(line)
                dm  = _DATE.search(line)
                dt = None
                if dm:
                    dt = datetime(int(dm.group(1)), int(dm.group(2)), int(dm.group(3)))
                orders[int(oid.group(1))] = {
                    "ref": ref.group(1),
                    "symbol": sym.group(1) if sym else None,
                    "action": act.group(1),
                    "avg": float(avg.group(1)),
                    "filled": float(fl.group(1)) if fl else 1.0,
                    "dt": dt,
                }

    # The close orderRef is CLOSE_{session} — SHARED by every position closed in that
    # session. Each order carries its own symbol, so group by (base_ref, symbol): one
    # group per closed position (its legs share the symbol; -L<n> suffix stripped).
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for o in orders.values():
        if not o["symbol"]:
            continue
        base = re.sub(r"-L\d+$", "", o["ref"])
        groups[(base, o["symbol"])].append(o)

    out: dict[str, dict] = {}
    for (base, sym), legs in groups.items():
        net = 0.0
        qty = 0.0
        dt = None
        for o in legs:
            sign = +1 if o["action"] == "BUY" else -1
            net += sign * o["avg"]
            qty = max(qty, o["filled"])
            dt = dt or o["dt"]
        out[f"{base}|{sym}"] = {
            "symbol": sym,
            "net_signed": round(net, 4),
            "qty": int(qty) if qty else 1,
            "dt": dt,
            "n_legs": len(legs),
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the restatement (default: dry run)")
    args = ap.parse_args()

    fills = parse_close_fills()
    print(f"Parsed {len(fills)} distinct CLOSE_* order fills from logs\n")

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT position_id, ticker, strategy, contracts, entry_price, close_price, "
        "realized_pnl, close_source, entry_date, close_date "
        "FROM positions WHERE status='closed' ORDER BY close_date"
    ).fetchall()

    # Index fills by (symbol, date) for matching; a symbol+date may have several.
    by_sym_date: dict[tuple, list[tuple[str, dict]]] = defaultdict(list)
    for base, f in fills.items():
        if f["symbol"] and f["dt"]:
            by_sym_date[(f["symbol"], f["dt"].date().isoformat())].append((base, f))

    matched, nofill, already = [], [], []
    used_refs: set[str] = set()
    for r in rows:
        priced = r["close_price"] not in (None, 0) and abs(r["close_price"]) > 1e-9
        cands = by_sym_date.get((r["ticker"], (r["close_date"] or "")[:10]), [])
        # Prefer an unused candidate with matching qty, else any unused, else any.
        pick = None
        for base, f in cands:
            if base in used_refs:
                continue
            if f["qty"] == r["contracts"]:
                pick = (base, f); break
        if pick is None:
            for base, f in cands:
                if base not in used_refs:
                    pick = (base, f); break

        if pick is None:
            (already if priced else nofill).append((r, None))
            continue
        base, f = pick
        used_refs.add(base)
        ncs = f["net_signed"]
        new_pnl = round(-(r["entry_price"] + ncs) * 100 * r["contracts"], 2)
        new_cp = round(abs(ncs), 4)
        rec = (r, {"ref": base, "ncs": ncs, "new_pnl": new_pnl,
                   "new_cp": new_cp, "qty": f["qty"], "dt": f["dt"]})
        if priced:
            already.append(rec)
        else:
            matched.append(rec)

    def fmt(r, info):
        base = f"{r['ticker']:5s} {r['strategy']:16s} x{r['contracts']} entry={r['entry_price']:+.2f} src={r['close_source']:14s} close={r['close_date']}"
        if info:
            return (base + f"\n        OLD cp={r['close_price']} pnl={r['realized_pnl']:+.2f}"
                    f"  ->  REAL net={info['ncs']:+.4f} cp={info['new_cp']} pnl={info['new_pnl']:+.2f}"
                    f"  [{info['ref']} qty={info['qty']}]")
        return base + f"\n        cp={r['close_price']} pnl={r['realized_pnl']:+.2f}  (NO close fill found in logs)"

    print(f"=== {len(matched)} ZERO-PRICE ROWS WITH A REAL CLOSE FILL (restatable) ===")
    for r, info in matched:
        print(" •", fmt(r, info))
    old_sum = sum(r["realized_pnl"] for r, _ in matched)
    new_sum = sum(info["new_pnl"] for _, info in matched)
    print(f"\n   restatable P&L: booked {old_sum:+.2f}  ->  real {new_sum:+.2f}  (delta {new_sum-old_sum:+.2f})")

    print(f"\n=== {len(nofill)} ZERO-PRICE ROWS WITH NO CLOSE FILL (fabricated close — no order ever sent) ===")
    for r, _ in nofill:
        print(" •", fmt(r, None))
    nofill_pnl = sum(r["realized_pnl"] for r, _ in nofill)
    print(f"\n   these currently contribute {nofill_pnl:+.2f} of fabricated P&L (cannot restate from a fill)")

    print(f"\n=== {len(already)} ALREADY-PRICED ROWS (sanity-check only) ===")
    drift = 0.0
    for r, info in already:
        if info and abs(info["new_pnl"] - r["realized_pnl"]) > 1.0:
            print(" • DRIFT", fmt(r, info))
            drift += 1
    print(f"   {int(drift)} priced rows disagree with their log fill by >$1")

    booked_total = sum(r["realized_pnl"] for r in rows)
    # Fully restated: zero-price rows use their fill P&L; priced rows use their fill
    # P&L when it drifts (the sign-inverted longs) else the original; no-fill rows
    # excluded (no real filled close to source).
    priced_restated = 0.0
    for r, info in already:
        if info and abs(info["new_pnl"] - r["realized_pnl"]) > 1.0:
            priced_restated += info["new_pnl"]
        else:
            priced_restated += r["realized_pnl"]
    real_total = new_sum + priced_restated
    print("\n=== HEADLINE ===")
    print(f"   booked total (all {len(rows)} closed):                          {booked_total:+.2f}")
    print(f"   restated from REAL fills (fabricated-no-fill EXCLUDED): {real_total:+.2f}")
    print(f"   ({len(nofill)} fabricated-no-fill rows carry {nofill_pnl:+.2f} of fiction, quarantined)")

    if not args.apply:
        print("\n(DRY RUN — no writes. Re-run with --apply to restate.)")
        con.close()
        return 0

    # ── APPLY: restate with a full audit trail (reversible) ─────────────────────
    ts = datetime.now().isoformat(timespec="seconds")
    con.execute(
        "CREATE TABLE IF NOT EXISTS positions_restatement_audit ("
        "position_id TEXT, restated_at TEXT, reason TEXT, "
        "old_close_price REAL, old_realized_pnl REAL, old_close_source TEXT, "
        "new_close_price REAL, new_realized_pnl REAL, new_close_source TEXT)"
    )

    def snapshot(r, reason, new_cp, new_pnl, new_src):
        con.execute(
            "INSERT INTO positions_restatement_audit VALUES (?,?,?,?,?,?,?,?,?)",
            (r["position_id"], ts, reason, r["close_price"], r["realized_pnl"],
             r["close_source"], new_cp, new_pnl, new_src),
        )

    n_fill, n_sign, n_void, n_fab = 0, 0, 0, 0

    # Tier A — zero-price rows restated from their real close fill.
    for r, info in matched:
        snapshot(r, "restated_from_fill", info["new_cp"], info["new_pnl"], r["close_source"])
        con.execute(
            "UPDATE positions SET close_price=?, realized_pnl=?, last_reviewed=? WHERE position_id=?",
            (info["new_cp"], info["new_pnl"], ts, r["position_id"]),
        )
        n_fill += 1

    # Tier B — already-priced rows whose P&L disagrees with the fill (sign-inverted longs).
    for r, info in already:
        if info and abs(info["new_pnl"] - r["realized_pnl"]) > 1.0:
            snapshot(r, "sign_corrected_from_fill", info["new_cp"], info["new_pnl"], r["close_source"])
            con.execute(
                "UPDATE positions SET close_price=?, realized_pnl=?, last_reviewed=? WHERE position_id=?",
                (info["new_cp"], info["new_pnl"], ts, r["position_id"]),
            )
            n_sign += 1

    # Tier D — no-fill rows: never had a filled close. A no-fill row whose (ticker,
    # contracts, close_date) twin DID get the real fill is a phantom duplicate (the MP
    # case) → 'duplicate_void'; the rest are fabricated marks → 'fabricated_unfilled'.
    # Both are quarantined: realized_pnl = NULL so they cannot pollute the scoreboard.
    sourced_keys = {(r["ticker"], r["contracts"], (r["close_date"] or "")[:10])
                    for r, info in matched} | {
                    (r["ticker"], r["contracts"], (r["close_date"] or "")[:10])
                    for r, info in already if info and abs(info["new_pnl"] - r["realized_pnl"]) > 1.0}
    for r, _ in nofill:
        key = (r["ticker"], r["contracts"], (r["close_date"] or "")[:10])
        if key in sourced_keys:
            src, reason = "duplicate_void", "phantom_duplicate_voided"
            n_void += 1
        else:
            src, reason = "fabricated_unfilled", "fabricated_no_filled_close"
            n_fab += 1
        # realized_pnl is NOT NULL — quarantine to 0 and exclude via close_source,
        # which the attributor/scoreboard filter on (these sources aren't "real closes").
        snapshot(r, reason, 0.0, 0.0, src)
        con.execute(
            "UPDATE positions SET realized_pnl=0, close_source=?, last_reviewed=? WHERE position_id=?",
            (src, ts, r["position_id"]),
        )

    con.commit()
    print("\n=== APPLIED ===")
    print(f"   {n_fill} restated from fill, {n_sign} sign-corrected, "
          f"{n_void} duplicates voided, {n_fab} fabricated quarantined")
    print(f"   audit trail: positions_restatement_audit ({n_fill+n_sign+n_void+n_fab} rows, restated_at={ts})")
    real_now = con.execute(
        "SELECT ROUND(SUM(realized_pnl),2) FROM positions WHERE status='closed' "
        "AND close_source NOT IN ('fabricated_unfilled','duplicate_void')"
    ).fetchone()[0]
    print(f"   DB realized P&L now (real fills only): {real_now:+.2f}")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

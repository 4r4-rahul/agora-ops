"""
Leg-level reconciliation between the AGORA shadow book (SQLite `positions`) and the
live IBKR account.

The prior check (system_health) only compared a DB count to position_mgr — both read
from the same DB, so it could never catch positions that exist at the broker but not in
the DB (or vice versa). This compares the two books at the OPTION-LEG level:

    key = (symbol, right 'C'/'P', strike, expiry 'YYYYMMDD')  ->  signed contract qty

and classifies every leg as:
    matched      — same signed qty in both books
    orphan       — present at IBKR, absent (or zero) in the DB  → not opened by AGORA
    ghost        — present in the DB, absent at IBKR            → order never filled / stale
    qty_mismatch — present in both but different signed qty     — partial fill / missed update

Run ad-hoc:   python -m agora.ops.position_reconciler
In the app:   GET /agora/reconcile  (read-only)
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

# Statuses the position manager treats as live (must match get_open_positions()).
LIVE_STATUSES = ("open", "tested", "rolled")

Leg = tuple[str, str, float, str]  # (symbol, right, strike, expiry YYYYMMDD)


def _norm_expiry(exp: str) -> str:
    """Normalise an expiry to YYYYMMDD (DB stores YYYY-MM-DD, IBKR YYYYMMDD)."""
    return (exp or "").replace("-", "").strip()


def db_legs(db_path: str) -> dict[Leg, int]:
    """Aggregate signed contract qty per option leg across all live DB positions."""
    legs: dict[Leg, int] = {}
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        placeholders = ",".join("?" * len(LIVE_STATUSES))
        rows = conn.execute(
            f"SELECT ticker, legs_json FROM positions WHERE status IN ({placeholders})",
            LIVE_STATUSES,
        ).fetchall()
    for r in rows:
        ticker = r["ticker"]
        try:
            parsed = json.loads(r["legs_json"] or "[]")
        except Exception:
            continue
        for lg in parsed:
            right = "C" if str(lg.get("option_type", "")).lower().startswith("c") else "P"
            strike = float(lg.get("strike", 0) or 0)
            expiry = _norm_expiry(str(lg.get("expiration", "")))
            qty = int(lg.get("contracts", 0) or 0)
            sign = 1 if str(lg.get("action", "")).lower() == "buy" else -1
            key: Leg = (ticker, right, strike, expiry)
            legs[key] = legs.get(key, 0) + sign * qty
    return {k: v for k, v in legs.items() if v != 0}


def ibkr_legs(ib: Any) -> dict[Leg, int]:
    """Signed contract qty per option leg from the live IBKR account."""
    legs: dict[Leg, int] = {}
    for p in ib.positions():
        c = p.contract
        if getattr(c, "secType", "") != "OPT":
            continue
        key: Leg = (
            c.symbol,
            getattr(c, "right", "") or "",
            float(getattr(c, "strike", 0) or 0),
            _norm_expiry(getattr(c, "lastTradeDateOrContractMonth", "")),
        )
        legs[key] = legs.get(key, 0) + int(p.position)
    return {k: v for k, v in legs.items() if v != 0}


@dataclass
class ReconcileReport:
    account: str = ""
    matched: list[dict] = field(default_factory=list)
    orphans: list[dict] = field(default_factory=list)       # at IBKR, not in DB
    ghosts: list[dict] = field(default_factory=list)        # in DB, not at IBKR
    qty_mismatch: list[dict] = field(default_factory=list)  # both, different qty

    @property
    def clean(self) -> bool:
        return not (self.orphans or self.ghosts or self.qty_mismatch)

    def to_dict(self) -> dict:
        return {
            "account": self.account,
            "clean": self.clean,
            "counts": {
                "matched": len(self.matched),
                "orphans": len(self.orphans),
                "ghosts": len(self.ghosts),
                "qty_mismatch": len(self.qty_mismatch),
            },
            "orphans": self.orphans,
            "ghosts": self.ghosts,
            "qty_mismatch": self.qty_mismatch,
        }


def _leg_dict(key: Leg, db_qty: int, ib_qty: int) -> dict:
    sym, right, strike, expiry = key
    return {
        "symbol": sym, "right": right, "strike": strike, "expiry": expiry,
        "db_qty": db_qty, "ibkr_qty": ib_qty,
    }


def diff(db: dict[Leg, int], ibkr: dict[Leg, int], account: str = "") -> ReconcileReport:
    rep = ReconcileReport(account=account)
    for key in sorted(set(db) | set(ibkr)):
        d, i = db.get(key, 0), ibkr.get(key, 0)
        entry = _leg_dict(key, d, i)
        if d == i:
            rep.matched.append(entry)
        elif d == 0:
            rep.orphans.append(entry)
        elif i == 0:
            rep.ghosts.append(entry)
        else:
            rep.qty_mismatch.append(entry)
    return rep


def reconcile(db_path: str, host: str = "127.0.0.1", port: int = 7497,
              client_id: int = 71) -> ReconcileReport:
    """Connect to IBKR, fetch live positions, and diff against the DB shadow book.
    Uses a short-lived dedicated client so it never disturbs the trading session."""
    from ib_insync import IB
    ib = IB()
    account = ""
    try:
        ib.connect(host, port, clientId=client_id, timeout=15)
        accts = ib.managedAccounts()
        account = accts[0] if accts else ""
        ib.reqPositions()
        ib.sleep(1.0)  # let paper account stream initial positions
        ibk = ibkr_legs(ib)
    finally:
        ib.disconnect()
    return diff(db_legs(db_path), ibk, account=account)


def format_report(rep: ReconcileReport) -> str:
    lines = [f"Reconciliation — account {rep.account or '?'} — "
             f"{'CLEAN ✓' if rep.clean else 'DIVERGENCE ✗'}"]
    lines.append(f"  matched={len(rep.matched)} orphans={len(rep.orphans)} "
                 f"ghosts={len(rep.ghosts)} qty_mismatch={len(rep.qty_mismatch)}")
    for title, items in (("ORPHANS (at IBKR, untracked in DB)", rep.orphans),
                         ("GHOSTS (in DB, absent at IBKR)", rep.ghosts),
                         ("QTY MISMATCH", rep.qty_mismatch)):
        if items:
            lines.append(f"  {title}:")
            for e in items:
                lines.append(f"    {e['symbol']:5} {e['right']}{e['strike']:<8} "
                             f"exp={e['expiry']}  db={e['db_qty']:+d} ibkr={e['ibkr_qty']:+d}")
    return "\n".join(lines)


if __name__ == "__main__":
    import os
    db = os.environ.get("AGORA_DB", ".agora/agora.db")
    host = os.environ.get("IBKR_HOST", "127.0.0.1")
    port = int(os.environ.get("IBKR_PORT", "7497"))
    print(format_report(reconcile(db, host, port)))

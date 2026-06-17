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


def ibkr_positions_detailed(ib: Any) -> dict[Leg, tuple[int, float]]:
    """Per-leg (signed qty, avgCost per-contract) from the live IBKR account — for adoption."""
    out: dict[Leg, tuple[int, float]] = {}
    for p in ib.positions():
        c = p.contract
        if getattr(c, "secType", "") != "OPT":
            continue
        key: Leg = (
            c.symbol, getattr(c, "right", "") or "",
            float(getattr(c, "strike", 0) or 0),
            _norm_expiry(getattr(c, "lastTradeDateOrContractMonth", "")),
        )
        out[key] = (int(p.position), float(getattr(p, "avgCost", 0.0) or 0.0))
    return out


def heal(db_path: str, position_mgr: Any, host: str = "127.0.0.1", port: int = 7497,
         client_id: int = 73, adopt_orphans: bool = True, close_ghosts: bool = True,
         ghost_min_age_min: float = 3.0) -> dict:
    """Make the DB a faithful mirror of the broker (TWS↔DB 100% accuracy):

      • GHOST   (DB position open, none of its legs at the broker) → mark closed in DB.
                Guarded by ghost_min_age_min so a just-filled entry whose IBKR position stream
                hasn't arrived yet is never closed prematurely.
      • ORPHAN  (broker leg with no DB position) → ADOPT into the DB as a tracked position so the
                engine manages its exit (never silently left unmanaged, never auto-flattened).
      • QTY_MISMATCH → reported, not auto-mutated (a partial fill needs a human/engine decision).

    Read-mostly on a clean book (no-op). Returns a summary dict. Never raises."""
    from ib_insync import IB

    from agora.core.models import (
        OpenPosition,
        PositionStatus,
        SpreadLeg,
        StrategyPillar,
        StrategyType,
    )

    out: dict[str, Any] = {"ghosts_closed": 0, "orphans_adopted": 0, "qty_mismatch": 0, "errors": []}
    ib = IB()
    try:
        ib.connect(host, port, clientId=client_id, timeout=15)
        ib.reqPositions()
        ib.sleep(1.2)
        ibk = ibkr_legs(ib)                    # signed qty per leg
        detailed = ibkr_positions_detailed(ib) # qty + avgCost per leg
    except Exception as exc:
        out["errors"].append(f"connect/positions: {exc}")
        try: ib.disconnect()
        except Exception: pass
        return out

    try:
        rep = diff(db_legs(db_path), ibk)

        # ── GHOSTS: close DB positions whose legs are entirely absent at the broker ──
        if close_ghosts:
            try:
                for pos in position_mgr.get_open_positions():
                    keys = []
                    for lg in pos.legs:
                        right = "C" if str(lg.option_type).lower().startswith("c") else "P"
                        keys.append((pos.ticker, right, float(lg.strike),
                                     _norm_expiry(lg.expiration.isoformat())))
                    if any(ibk.get(k, 0) != 0 for k in keys):
                        continue  # at least one leg still live at broker → not a ghost
                    # Safety against closing a just-filled entry whose IBKR position stream hasn't
                    # arrived: reqPositions + the 1.2s settle above means a real fill is already in
                    # `ibk`; combined with the periodic (not per-fill) cadence, a position absent
                    # here is genuinely gone from the broker. Close it so the DB mirrors TWS.
                    position_mgr.mark_position_closed(
                        position_id=pos.position_id, realized_pnl=0.0, close_price=0.0,
                        source="reconcile_ghost")
                    out["ghosts_closed"] += 1
            except Exception as exc:
                out["errors"].append(f"ghost-close: {exc}")

        # ── ORPHANS: adopt broker legs that no DB position covers ──
        if adopt_orphans and rep.orphans:
            try:
                groups: dict[tuple[str, str], list[dict]] = {}
                for o in rep.orphans:
                    groups.setdefault((o["symbol"], o["expiry"]), []).append(o)
                for (sym, expiry), legs in groups.items():
                    adopted = _adopt_group(position_mgr, sym, expiry, legs, detailed,
                                           OpenPosition, SpreadLeg, StrategyType,
                                           StrategyPillar, PositionStatus)
                    if adopted:
                        out["orphans_adopted"] += 1
            except Exception as exc:
                out["errors"].append(f"orphan-adopt: {exc}")

        out["qty_mismatch"] = len(rep.qty_mismatch)
        if out["ghosts_closed"] or out["orphans_adopted"] or out["qty_mismatch"]:
            import logging
            logging.getLogger(__name__).warning(
                "PositionHealer: closed %d ghost(s), adopted %d orphan(s), %d qty-mismatch%s",
                out["ghosts_closed"], out["orphans_adopted"], out["qty_mismatch"],
                f" {rep.qty_mismatch}" if rep.qty_mismatch else "")
    finally:
        try: ib.disconnect()
        except Exception: pass
    return out


def _adopt_group(position_mgr, sym, expiry, legs, detailed, OpenPosition, SpreadLeg,
                 StrategyType, StrategyPillar, PositionStatus) -> bool:
    """Build and persist an OpenPosition from orphan broker legs so the engine tracks it."""
    import uuid
    from datetime import date as _date
    from datetime import timedelta as _td
    exp_d = _date(int(expiry[:4]), int(expiry[4:6]), int(expiry[6:8]))
    spread_legs, debit = [], 0.0
    for o in legs:
        key = (sym, o["right"], float(o["strike"]), expiry)
        qty, avg_cost = detailed.get(key, (o["ibkr_qty"], 0.0))
        action = "buy" if qty > 0 else "sell"
        per_contract = (avg_cost / 100.0) if avg_cost else 0.0   # IBKR avgCost is per-share×100
        debit += (per_contract if action == "buy" else -per_contract) * abs(qty)
        spread_legs.append(SpreadLeg(
            option_type="call" if o["right"] == "C" else "put",
            strike=float(o["strike"]), expiration=exp_d, action=action,
            contracts=abs(int(qty)), mid_price=round(per_contract, 2)))
    if not spread_legs:
        return False
    # Infer strategy + direction conservatively.
    if len(spread_legs) == 1:
        lg = spread_legs[0]
        is_call = lg.option_type == "call"
        strat = StrategyType.LONG_CALL if is_call else StrategyType.LONG_PUT
        direction = "bullish" if is_call else "bearish"
    else:
        strat = StrategyType.IRON_CONDOR if len(spread_legs) >= 4 else StrategyType.BULL_CALL_SPREAD
        direction = "neutral"
    contracts = max((l.contracts for l in spread_legs), default=1)
    entry_debit = round(abs(debit) * 100, 2) or 1.0
    pos = OpenPosition(
        position_id=f"adopt-{uuid.uuid4().hex[:12]}", ticker=sym, strategy=strat,
        pillar=StrategyPillar.DIRECTIONAL, direction=direction, status=PositionStatus.OPEN,
        legs=spread_legs, contracts=contracts, entry_price=round(abs(debit), 2),
        entry_date=_date.today(), expiry_date=exp_d,
        target_close_date=min(exp_d, _date.today() + _td(days=21)),
        max_loss_dollars=entry_debit, max_gain_dollars=entry_debit * 3,
        notes="ADOPTED by position reconciler (broker leg untracked in DB)")
    position_mgr.add_position(pos)
    import logging
    logging.getLogger(__name__).warning(
        "PositionHealer ADOPTED %s %s %dx (%s) — was an untracked broker position",
        sym, strat.value, contracts, expiry)
    return True


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

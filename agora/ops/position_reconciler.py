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
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC
from typing import Any

logger = logging.getLogger(__name__)

# Statuses the position manager treats as live (must match get_open_positions()).
LIVE_STATUSES = ("open", "tested", "rolled")

Leg = tuple[str, str, float, str]  # (symbol, right, strike, expiry YYYYMMDD)


def _norm_expiry(exp: str) -> str:
    """Normalise an expiry to YYYYMMDD (DB stores YYYY-MM-DD, IBKR YYYYMMDD)."""
    return (exp or "").replace("-", "").strip()


def db_legs(db_path: str) -> dict[Leg, int]:
    """Aggregate signed contract qty per option leg across all live DB positions.

    The absolute per-leg quantity is the position-level `contracts` (the size the broker order was
    placed with) times the leg's RATIO. The per-leg `contracts` in legs_json is written
    inconsistently across entry paths — some store the absolute count (AMD long_put: 8), some store
    the ratio (IWM/SCHW 3-lot vertical: 1) — so reading it directly under-reported IWM/SCHW as ±1
    while the broker held ±3 (phantom qty-mismatch). We normalize the leg count against the smallest
    leg in the position to recover the true ratio, which is correct under BOTH conventions and still
    preserves a genuine ratio spread (e.g. 1×2)."""
    legs: dict[Leg, int] = {}
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        placeholders = ",".join("?" * len(LIVE_STATUSES))
        rows = conn.execute(
            f"SELECT ticker, contracts, legs_json FROM positions WHERE status IN ({placeholders})",
            LIVE_STATUSES,
        ).fetchall()
    for r in rows:
        ticker = r["ticker"]
        pos_contracts = int(r["contracts"] or 0)
        try:
            parsed = json.loads(r["legs_json"] or "[]")
        except Exception:
            continue
        leg_counts = [int(lg.get("contracts", 0) or 0) for lg in parsed]
        base = min((c for c in leg_counts if c > 0), default=1)  # smallest leg = 1 ratio unit
        for lg in parsed:
            right = "C" if str(lg.get("option_type", "")).lower().startswith("c") else "P"
            strike = float(lg.get("strike", 0) or 0)
            expiry = _norm_expiry(str(lg.get("expiration", "")))
            leg_ratio = (int(lg.get("contracts", 0) or 0) or base) / base
            qty = round(pos_contracts * leg_ratio)
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


def plan_overfill_flatten(rep: ReconcileReport, min_excess: int = 25) -> list[dict]:
    """Compute the flatten plan for broker legs MASSIVELY over the book — the 2026-06-26 signature
    (DIA book −59 vs broker +631). Scans BOTH qty_mismatch AND orphan legs: when a position is
    ghost-closed in the DB while its over-filled broker legs persist (exactly what happened on the
    10:29 restart — the 659-contract DIA didn't stream in time, the adopted row was ghost-closed,
    and the legs became orphans), the over-fill shows up as an ORPHAN, not a mismatch. We must catch
    both, or a runaway leg escapes flattening and gets re-adopted as a tracked giant position.

    A leg is over-filled when broker magnitude ≥ 2× the book (book=0 for an orphan) AND the excess
    ≥ min_excess contracts — so routine ±1/±2 partials and normal small orphan adoptions are left
    alone. Returns the single-leg order that brings each over-filled broker leg back to the book
    quantity (0 for an orphan). Pure computation — executes nothing."""
    plan: list[dict] = []
    # qty_mismatch carries db_qty/ibkr_qty; an orphan is the same shape with db_qty == 0.
    candidates = list(rep.qty_mismatch) + [
        {**o, "db_qty": 0} for o in rep.orphans
    ]
    for m in candidates:
        db_qty = int(m.get("db_qty", 0))
        ib_qty = int(m.get("ibkr_qty", 0))
        excess = abs(ib_qty) - abs(db_qty)
        if excess < min_excess or abs(ib_qty) < 2 * max(1, abs(db_qty)):
            continue
        delta = ib_qty - db_qty            # trade this to bring broker → book
        plan.append({
            "symbol": m.get("symbol"), "right": m.get("right"),
            "strike": m.get("strike"), "expiry": m.get("expiry"),
            "db_qty": db_qty, "ibkr_qty": ib_qty,
            "action": "SELL" if delta > 0 else "BUY",
            "flatten_qty": abs(delta), "target_qty": db_qty,
        })
    return plan


_FLATTEN_TERMINAL = frozenset(
    {"Filled", "Cancelled", "ApiCancelled", "Inactive", "PendingCancel"}
)


def _flatten_overfill(ib: Any, plan: list[dict], max_flatten: int) -> dict:
    """Execute an over-fill flatten plan on the heal()-connected (sync) ib: ONE single-leg order per
    leg to bring the broker quantity back to the book quantity.

    Each order carries a per-leg FLATTEN_<leg> orderRef and is skipped if one is already working —
    the SAME idempotency guard as the close path, so this cleanup can never stack into a runaway the
    way the bug it cleans up did. A paper fill that lags 2-4 min is therefore waited out across heal
    cycles, not re-fired. Orders larger than max_flatten are refused (defense vs a bad diff).

    Returns {"placed": n, "skipped": n, "refused": n}. Never raises."""
    from ib_insync import Option, Order
    out = {"placed": 0, "skipped": 0, "refused": 0}
    try:
        ib.reqAllOpenOrders()
        ib.sleep(0.5)
        working_refs = {(t.order.orderRef or "") for t in ib.openTrades()
                        if t.orderStatus.status not in _FLATTEN_TERMINAL}
    except Exception as exc:
        logger.warning("Over-fill flatten: open-orders snapshot failed (%s) — skipping this cycle", exc)
        return out

    for item in plan:
        ref = (f"FLATTEN_{item['symbol']}{item['right']}{int(float(item['strike']))}_"
               f"{item['expiry']}")[:30]
        if any(r.startswith(ref) for r in working_refs):
            out["skipped"] += 1
            logger.warning("Over-fill flatten SKIP %s — already working (not stacking)", ref)
            continue
        if int(item["flatten_qty"]) > max_flatten:
            out["refused"] += 1
            logger.error("Over-fill flatten REFUSED %s — qty %d exceeds sanity ceiling %d",
                         ref, item["flatten_qty"], max_flatten)
            continue
        try:
            opt = Option(item["symbol"], item["expiry"], float(item["strike"]), item["right"],
                         exchange="SMART", currency="USD", multiplier="100")
            if not ib.qualifyContracts(opt):
                logger.warning("Over-fill flatten: could not qualify %s — skipping", ref)
                continue
            o = Order()
            o.action = item["action"]
            o.totalQuantity = int(item["flatten_qty"])
            o.orderType = "MKT"   # cleanup of known junk — a guaranteed exit; paper slippage is moot
            o.tif = "DAY"
            o.orderRef = ref
            o.transmit = True
            ib.placeOrder(opt, o)
            out["placed"] += 1
            logger.error(
                "OVER-FILL FLATTEN PLACED — %s %s %s %d (broker %d → book %d) ref=%s",
                item["action"], item["symbol"], item["right"], item["flatten_qty"],
                item["ibkr_qty"], item["target_qty"], ref,
            )
        except Exception as exc:
            logger.error("Over-fill flatten place failed for %s: %s", ref, exc)
    return out


def _position_age_minutes(pos: Any) -> float | None:
    """Minutes since the position was entered, for the under-fill settle guard. Prefers the precise
    entry_ts_utc; falls back to entry_date (a prior calendar day → definitely settled). Returns None
    when age can't be established — the caller then treats it as 'not safe to correct yet'."""
    from datetime import date as _date
    from datetime import datetime
    ts = getattr(pos, "entry_ts_utc", None)
    if ts:
        try:
            dt = datetime.fromisoformat(str(ts))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return (datetime.now(UTC) - dt).total_seconds() / 60.0
        except Exception:
            pass
    ed = getattr(pos, "entry_date", None)
    if ed:
        try:
            if datetime.fromisoformat(str(ed)[:10]).date() < _date.today():
                return 24 * 60.0   # entered a prior day → long settled
        except Exception:
            pass
    return None


def safe_to_ghost_close(snap_a: dict, snap_b: dict, leg_keys: list) -> bool:
    """A DB position is a TRUE ghost (safe to close) only if BOTH broker snapshots agree (the
    position stream has settled — no leg still arriving) AND every one of its legs is absent in
    BOTH snapshots. This is the guard the 2026-06-26 false ghost-close lacked: a single half-
    streamed snapshot made the adopted DIA/NOW legs look gone, so the rows were closed and the
    over-fill turned into untracked orphans. Closing DB state on incomplete broker data is never
    safe — when in doubt, defer to the next cycle."""
    if snap_a != snap_b:
        return False   # stream not settled — defer
    return all(snap_a.get(k, 0) == 0 and snap_b.get(k, 0) == 0 for k in leg_keys)


def heal(db_path: str, position_mgr: Any, host: str = "127.0.0.1", port: int = 7497,
         client_id: int = 73, adopt_orphans: bool = True, close_ghosts: bool = True,
         ghost_min_age_min: float = 3.0,
         overfill_flatten_enabled: bool = True, overfill_min_excess: int = 25,
         overfill_max_flatten: int = 5000, underfill_min_age_min: float = 20.0,
         orphan_inflight_guard: bool = True) -> dict:
    """Make the DB a faithful mirror of the broker (TWS↔DB 100% accuracy):

      • GHOST   (DB position open, none of its legs at the broker) → mark closed in DB.
                Guarded by ghost_min_age_min so a just-filled entry whose IBKR position stream
                hasn't arrived yet is never closed prematurely.
      • ORPHAN  (broker leg with no DB position) → ADOPT into the DB as a tracked position so the
                engine manages its exit (never silently left unmanaged, never auto-flattened).
      • QTY_MISMATCH → reported. A routine partial fill is left for an engine decision, BUT a leg
                MASSIVELY over the book (the 2026-06-26 over-fill signature) is mechanically flattened
                back to the book qty when overfill_flatten_enabled (single-leg, idempotency-guarded).

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
        # A large position (e.g. the 659-contract DIA over-fill) streams as hundreds of position
        # messages; a single short settle truncated it on the 2026-06-26 restart and the adopted row
        # was falsely ghost-closed. Take TWO snapshots a beat apart: if they disagree the stream is
        # still arriving and ghost-closing (which DESTROYS DB state) must wait. The later snapshot is
        # the more complete one, so it is authoritative for the diff.
        ib.sleep(3.0)
        ibk_a = ibkr_legs(ib)                  # first snapshot
        ib.sleep(1.5)
        ibk = ibkr_legs(ib)                    # later snapshot → authoritative
        detailed = ibkr_positions_detailed(ib) # qty + avgCost per leg
        positions_stable = (ibk == ibk_a)      # stream settled?
    except Exception as exc:
        out["errors"].append(f"connect/positions: {exc}")
        try: ib.disconnect()
        except Exception: pass
        return out

    try:
        rep = diff(db_legs(db_path), ibk)

        # ── OVER-FILL DETECTION + FLATTEN (runs FIRST): legs MASSIVELY over the book (DIA 631-vs-59
        # on 2026-06-26), whether they show as a qty_mismatch or as an orphan (when the DB row was
        # ghost-closed but the over-filled broker legs persist). Flatten BEFORE adoption so a runaway
        # junk leg is brought back to the book — NOT adopted as a tracked giant position. Each order
        # is single-leg + idempotency-guarded, so the cleanup itself can never stack into a runaway.
        overfill_plan = plan_overfill_flatten(rep, overfill_min_excess)
        out["overfill_plan"] = overfill_plan
        overfill_keys: set[tuple] = set()
        if overfill_plan:
            overfill_keys = {
                (p["symbol"], p["right"], float(p["strike"]), p["expiry"]) for p in overfill_plan
            }
            logger.error(
                "OVER-FILL DETECTED — %d leg(s) far exceed the book. Flatten plan: %s",
                len(overfill_plan), overfill_plan,
            )
            if overfill_flatten_enabled:
                out["overfill_flatten"] = _flatten_overfill(
                    ib, overfill_plan, overfill_max_flatten)

        # ── GHOSTS: close DB positions whose legs are entirely absent at the broker ──
        # DEFERRED when the broker snapshot is unstable: ghost-closing destroys DB state, and a
        # half-streamed snapshot made adopted DIA/NOW look "gone" on 2026-06-26, falsely closing
        # them (which then turned the over-fill into orphans). Never close on incomplete data.
        if close_ghosts and not positions_stable:
            out["ghost_close_deferred"] = True
            logger.warning(
                "Ghost-close DEFERRED — broker positions still streaming (snapshot changed between "
                "reads); NOT closing DB positions on incomplete data. Will retry next cycle.")
        elif close_ghosts:
            try:
                for pos in position_mgr.get_open_positions():
                    keys = []
                    for lg in pos.legs:
                        right = "C" if str(lg.option_type).lower().startswith("c") else "P"
                        keys.append((pos.ticker, right, float(lg.strike),
                                     _norm_expiry(lg.expiration.isoformat())))
                    if not safe_to_ghost_close(ibk_a, ibk, keys):
                        continue  # legs still live in EITHER snapshot → not a confirmed ghost
                    position_mgr.mark_position_closed(
                        position_id=pos.position_id, realized_pnl=0.0, close_price=0.0,
                        source="reconcile_ghost")
                    out["ghosts_closed"] += 1
            except Exception as exc:
                out["errors"].append(f"ghost-close: {exc}")

        # ── SETTLED UNDER-FILL: broker holds FEWER than the book (the TSLA 2-vs-1 case). The broker
        # is the truth for what's held, so correct the BOOK down — but ONLY once the gap has SETTLED
        # (snapshots stable AND the position aged past fill latency), never mid-fill. A fresh small
        # gap is almost always a settling fill (06-26's TSLA self-resolved in <1 cycle, so correcting
        # it then would have fought a live fill). Makes closes/P&L/risk use the real size.
        if positions_stable:
            try:
                for pos in position_mgr.get_open_positions():
                    book_ct = int(getattr(pos, "contracts", 0) or 0)
                    if book_ct <= 0:
                        continue
                    age = _position_age_minutes(pos)
                    if age is None or age < underfill_min_age_min:
                        continue   # too fresh / unknown age → may still be filling; leave it
                    broker_qtys = []
                    for lg in pos.legs:
                        right = "C" if str(lg.option_type).lower().startswith("c") else "P"
                        k = (pos.ticker, right, float(lg.strike),
                             _norm_expiry(lg.expiration.isoformat()))
                        broker_qtys.append(abs(ibk.get(k, 0)))
                    if not broker_qtys:
                        continue
                    broker_ct = broker_qtys[0]
                    # uniform across legs (ratio-1 structure), broker holds SOME but fewer than book
                    if broker_ct > 0 and all(b == broker_ct for b in broker_qtys) and broker_ct < book_ct:
                        if position_mgr.reconcile_contracts(
                                pos.position_id, broker_ct, "underfill_book_to_broker"):
                            out["book_resized"] = out.get("book_resized", 0) + 1
            except Exception as exc:
                out["errors"].append(f"underfill-resize: {exc}")

        # ── ORPHANS: adopt broker legs that no DB position covers ──
        # EXCLUDE legs being flattened as over-fills — adopting a 659-contract junk leg would re-track
        # the very runaway we're unwinding.
        if adopt_orphans and rep.orphans:
            try:
                # RACE GUARD (2026-06-29): during leg-by-leg spread entry the long leg fills SECONDS
                # before the short, and the spread's DB row is written only AFTER both legs fill. In that
                # window the lone long leg looks like an orphan; adopting it double-books the contract
                # (the spread row lands moments later → db_qty=2 vs broker=1, e.g. JPM 340C / NVDA 205C).
                # The in-flight signal is NOT a DB position (none exists yet) — it's a STILL-WORKING AGORA
                # ENTRY ORDER on that ticker (the short leg). Defer adoption for such tickers (entry orders
                # are on OTHER clientIds, so reqAllOpenOrders — all clients — is required, not openTrades).
                inflight_tickers: set[str] = set()
                if orphan_inflight_guard:
                    try:
                        for t in (ib.reqAllOpenOrders() or []):
                            ref = str(getattr(t.order, "orderRef", "") or "")
                            st = str(getattr(t.orderStatus, "status", "") or "")
                            if ref.startswith("AGORA-") and st in (
                                    "PendingSubmit", "PreSubmitted", "Submitted", "ApiPending"):
                                inflight_tickers.add(getattr(t.contract, "symbol", ""))
                    except Exception as _oe:
                        logger.debug("orphan-adopt in-flight check skipped: %s", _oe)
                # Belt: re-read db_legs NOW — a spread row may have landed since the snapshot diff above.
                fresh_db = db_legs(db_path)
                groups: dict[tuple[str, str], list[dict]] = {}
                for o in rep.orphans:
                    key = (o["symbol"], o["right"], float(o["strike"]), o["expiry"])
                    if key in overfill_keys:
                        continue
                    if fresh_db.get(key, 0) != 0:
                        logger.info("Orphan adopt SKIP %s — book now covers it (race resolved)", key)
                        continue
                    if o["symbol"] in inflight_tickers:
                        out["orphan_adopt_deferred"] = out.get("orphan_adopt_deferred", 0) + 1
                        logger.info("Orphan adopt DEFER %s — AGORA entry order still working on %s "
                                    "(spread mid-completion) — avoids double-book", key, o["symbol"])
                        continue
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
        # Pre-heal diff snapshot for the live DB↔TWS sync badge: legs that matched the broker, and the
        # divergences found this cycle (heal corrects ghosts/orphans, so post-heal the book mirrors TWS).
        out["matched"] = len(rep.matched)
        out["orphans_found"] = len(rep.orphans)
        out["ghosts_found"] = len(rep.ghosts)

        if out["ghosts_closed"] or out["orphans_adopted"] or out["qty_mismatch"]:
            logger.warning(
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
    spread_legs, net_per_share = [], 0.0
    for o in legs:
        key = (sym, o["right"], float(o["strike"]), expiry)
        qty, avg_cost = detailed.get(key, (o["ibkr_qty"], 0.0))
        action = "buy" if qty > 0 else "sell"
        per_contract = (avg_cost / 100.0) if avg_cost else 0.0   # IBKR avgCost is per-share×100 → per share
        # SIGNED PER-SHARE net of the structure. CRITICAL: do NOT weight by qty here. entry_price is a
        # per-share figure (pnl.py convention) and realized_pnl multiplies by `contracts` itself, so a
        # qty-weighted entry_price is scaled by contracts TWICE — a contracts² blow-up. THIS was the
        # 2026-06-25 corruption: DIA 59x got entry_price=115.49 (=1.9575×59) and booked −$808k realized
        # on an $11.5k defined risk. Keep entry per-share; carry the size in `contracts` only.
        net_per_share += (per_contract if action == "buy" else -per_contract)
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
    # entry_price = per-share net; defined risk in dollars = per-share × 100 × contracts. This keeps the
    # invariant realized_pnl ∈ [−max_loss, +max_gain] structurally true, so pnl_within_bounds can never trip.
    entry_per_share = round(abs(net_per_share), 4) or 0.01
    total_risk = round(abs(net_per_share) * 100 * contracts, 2) or 1.0
    pos = OpenPosition(
        position_id=f"adopt-{uuid.uuid4().hex[:12]}", ticker=sym, strategy=strat,
        pillar=StrategyPillar.DIRECTIONAL, direction=direction, status=PositionStatus.OPEN,
        legs=spread_legs, contracts=contracts, entry_price=entry_per_share,
        entry_date=_date.today(), expiry_date=exp_d,
        target_close_date=min(exp_d, _date.today() + _td(days=21)),
        max_loss_dollars=total_risk, max_gain_dollars=total_risk * 3,
        # Explicit provenance marker: these were never scored by the entry gates, so audits/alerts/UI
        # must NOT read their conviction_at_entry=0 / blank regime as a "zero-conviction gate failure"
        # (the mid-morning check + the R&D calibration audit were doing exactly that). 2026-06-22 fix.
        regime_at_entry="adopted",
        notes="ADOPTED by position reconciler (broker leg untracked in DB)")
    position_mgr.add_position(pos)
    import logging
    _log = logging.getLogger(__name__)
    _log.warning(
        "PositionHealer ADOPTED %s %s %dx (%s) — was an untracked broker position",
        sym, strat.value, contracts, expiry)
    # SIZE-SANITY ALERT (2026-06-25): the engine NEVER opens more than max_contracts_per_trade, so an
    # adopted broker position larger than that is an anomaly — accumulated/legacy paper-account state or
    # a leg-quantity mismatch — and must NOT masquerade as a routine adoption. Surface it distinctly. We
    # still adopt it (an untracked real position is worse than a flagged one) — this is observability so
    # an oversized position (e.g. a 59-contract DIA = 44% of a $10k AUM) is visible, not silent.
    _cap = int(getattr(getattr(position_mgr, "_settings", None), "max_contracts_per_trade", 10) or 10)
    if contracts > _cap:
        _log.warning(
            "ADOPTED OVERSIZED: %s %s %dx EXCEEDS engine cap %d — broker position is larger than the "
            "engine would ever open (accumulated/legacy paper state or leg mismatch); review/clear",
            sym, strat.value, contracts, _cap)
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

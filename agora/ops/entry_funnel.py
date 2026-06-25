"""
agora/ops/entry_funnel.py — per-ticker ENTRY FUNNEL observability (read-only; zero trade impact).

Answers the owner's question: "why does a swinging name like AAOI never get traded?" The entry path has
silent decision points — the price monitor evaluates every universe ticker each cycle but only LOGS the
ones it promotes; the rules-engine gates accept/reject without a per-ticker durable record. This module
makes those decisions VISIBLE: one compact row per (ticker, day) tracking how far each ticker got down
the funnel —

    evaluated → promoted (tripped a move/volume trigger) → entry gate (passed / rejected, with reason)

so we can SEE, per ticker: did the monitor even surface it? if not, what was its biggest move vs the
trigger? if promoted, which gate killed it? No guessing — the data shows where each name falls out.

Design:
  • WRITE-ONLY observability — never touches execution/order/gate logic; it only records what already
    happened. Every IO path is wrapped and NEVER raises (a logging failure must not break the monitor).
  • One UPSERTed row per (date, ticker): bump counters, keep the MAX move/vol seen → compact (~universe
    rows/day), not an append-per-cycle firehose.
  • Pure aggregation in funnel_summary for the dashboard; sorted to surface the diagnostic case
    (high move, never promoted) first.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

_DDL = """
CREATE TABLE IF NOT EXISTS entry_funnel (
    day           TEXT NOT NULL,        -- YYYY-MM-DD (point-in-time bucket)
    ticker        TEXT NOT NULL,
    evaluated_n   INTEGER NOT NULL DEFAULT 0,   -- times the price monitor evaluated it this day
    max_move_pct  REAL,                 -- largest |30-min % move| the monitor saw
    max_vol_ratio REAL,                 -- largest volume spike ratio (latest/avg) seen
    promoted_n    INTEGER NOT NULL DEFAULT 0,   -- times it tripped a trigger and was promoted
    last_trigger  TEXT,                 -- the most recent promotion trigger string
    conviction_n  INTEGER NOT NULL DEFAULT 0,   -- times it reached the conviction/evaluate stage
    conviction_outcome TEXT,            -- conviction result (scored N / no-trade reason)
    gate_outcome  TEXT,                 -- most recent entry-gate result (passed / a rejection reason)
    gate_n        INTEGER NOT NULL DEFAULT 0,   -- times it reached the entry gates
    updated_at    TEXT,
    PRIMARY KEY (day, ticker)
);
"""

# Columns added after the table first shipped — _connect ALTERs them in (CREATE IF NOT EXISTS never adds
# a column to a pre-existing table, the silent-failure trap; see the feature-store fix).
_MIGRATIONS = (("conviction_n", "INTEGER NOT NULL DEFAULT 0"), ("conviction_outcome", "TEXT"))


def _today(day: str | None) -> str:
    return day or datetime.now(UTC).date().isoformat()


def _connect(db_path: Any) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.executescript(_DDL)
    have = {r[1] for r in conn.execute("PRAGMA table_info(entry_funnel)")}
    for col, decl in _MIGRATIONS:
        if col not in have:
            conn.execute(f"ALTER TABLE entry_funnel ADD COLUMN {col} {decl}")
    return conn


def record_monitor_evals(db_path: Any, evals: list[dict], *, day: str | None = None) -> int:
    """Record a batch of price-monitor evaluations for one cycle (one call per monitor cycle, not per
    ticker). Each eval: {ticker, move_pct, vol_ratio, promoted(bool), trigger(str|None)}. UPSERTs per
    (day, ticker): bumps evaluated_n, keeps the MAX move/vol seen, bumps promoted_n + records the trigger
    when promoted. Returns rows written. Never raises."""
    d = _today(day)
    now = datetime.now(UTC).isoformat()
    n = 0
    try:
        conn = _connect(db_path)
        for e in evals or []:
            tk = str(e.get("ticker") or "").upper()
            if not tk:
                continue
            mv = abs(float(e.get("move_pct") or 0.0))
            vr = float(e.get("vol_ratio") or 0.0)
            promoted = 1 if e.get("promoted") else 0
            trig = e.get("trigger")
            conn.execute(
                """INSERT INTO entry_funnel (day, ticker, evaluated_n, max_move_pct, max_vol_ratio,
                       promoted_n, last_trigger, updated_at)
                   VALUES (?,?,1,?,?,?,?,?)
                   ON CONFLICT(day, ticker) DO UPDATE SET
                       evaluated_n   = evaluated_n + 1,
                       max_move_pct  = MAX(COALESCE(max_move_pct, 0), excluded.max_move_pct),
                       max_vol_ratio = MAX(COALESCE(max_vol_ratio, 0), excluded.max_vol_ratio),
                       promoted_n    = promoted_n + excluded.promoted_n,
                       last_trigger  = COALESCE(excluded.last_trigger, last_trigger),
                       updated_at    = excluded.updated_at""",
                (d, tk, mv, vr, promoted, trig if promoted else None, now))
            n += 1
        conn.commit()
        conn.close()
    except Exception:
        return n
    return n


def record_gate_outcome(db_path: Any, ticker: str, outcome: str, *, day: str | None = None) -> bool:
    """Record the entry-gate result for a ticker (e.g. 'rejected: R/R 0.8<1.0', 'passed→order'). UPSERTs
    the last outcome + bumps gate_n. A ticker reaching the gates must already have been monitor-evaluated,
    but this is robust if not (inserts a minimal row). Never raises."""
    d = _today(day)
    now = datetime.now(UTC).isoformat()
    tk = str(ticker or "").upper()
    if not tk:
        return False
    try:
        conn = _connect(db_path)
        conn.execute(
            """INSERT INTO entry_funnel (day, ticker, gate_outcome, gate_n, updated_at)
               VALUES (?,?,?,1,?)
               ON CONFLICT(day, ticker) DO UPDATE SET
                   gate_outcome = excluded.gate_outcome,
                   gate_n       = gate_n + 1,
                   updated_at   = excluded.updated_at""",
            (d, tk, str(outcome)[:160], now))
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False


def record_conviction(db_path: Any, ticker: str, outcome: str, *, day: str | None = None) -> bool:
    """Record a promoted ticker reaching the CONVICTION/evaluate stage with its result — 'scored N' when
    it proceeds toward an order, or a 'no-trade: <reason>' when the conviction/IVR/vol gate drops it. This
    lights the stage BETWEEN promotion and the rules-engine gates (where the 31 promoted-but-untraded
    names fall out). UPSERTs the latest outcome + bumps conviction_n. Never raises."""
    d = _today(day)
    now = datetime.now(UTC).isoformat()
    tk = str(ticker or "").upper()
    if not tk:
        return False
    try:
        conn = _connect(db_path)
        conn.execute(
            """INSERT INTO entry_funnel (day, ticker, conviction_outcome, conviction_n, updated_at)
               VALUES (?,?,?,1,?)
               ON CONFLICT(day, ticker) DO UPDATE SET
                   conviction_outcome = excluded.conviction_outcome,
                   conviction_n       = conviction_n + 1,
                   updated_at         = excluded.updated_at""",
            (d, tk, str(outcome)[:160], now))
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False


def funnel_summary(db_path: Any, *, day: str | None = None, limit: int = 60) -> list[dict]:
    """Per-ticker funnel for the day, for the dashboard/analysis. Adds a derived `stage` (where the ticker
    fell out) and sorts to surface the DIAGNOSTIC case first: names that moved hard but were never promoted
    (the AAOI symptom), then promoted-but-gated, then traded. Never raises."""
    d = _today(day)
    try:
        conn = _connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM entry_funnel WHERE day=? ORDER BY max_move_pct DESC", (d,))]
        conn.close()
    except Exception:
        return []
    out = []
    for r in rows:
        promoted = (r.get("promoted_n") or 0) > 0
        convicted = (r.get("conviction_n") or 0) > 0
        gated = (r.get("gate_n") or 0) > 0
        if gated:
            stage = "reached_gates"      # reached rules-engine generate (gate_outcome has the result)
        elif convicted:
            stage = "reached_conviction"  # scored but the conviction/IVR/vol gate dropped it before generate
        elif promoted:
            stage = "promoted_only"      # surfaced but never reached the conviction stage
        else:
            stage = "never_promoted"     # evaluated but never tripped a trigger
        r["stage"] = stage
        out.append(r)
    # diagnostic ordering: earliest-fallout-but-highest-move first (where the funnel leaks most)
    _rank = {"never_promoted": 0, "promoted_only": 1, "reached_conviction": 2, "reached_gates": 3}
    out.sort(key=lambda x: (_rank[x["stage"]], -(x.get("max_move_pct") or 0)))
    return out[:limit]

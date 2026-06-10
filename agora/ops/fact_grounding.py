"""
agora/ops/fact_grounding.py — Fact-grounding monitor for agent reasoning.

The institutional blind spot the C-suite post-mortem surfaced: our oversight measured
OUTCOMES and AGGREGATES (win rate, fill rate, calibration), never the FACTUAL GROUNDING
of an agent's stated reasoning. An LLM that confidently asserts a *false but plausible*
fact — e.g. "FOMC tomorrow" when FOMC was 13 days out and the real event was NFP — and
produces the *right-looking* action (blocking into "an event") sails straight through
metric-based monitoring.

This module is the independent verifier: it cross-checks the factual claims inside agent
reasoning against deterministic ground truth (the macro calendar), and raises a logged,
persisted, dashboard-visible divergence alert when they disagree.

It is BOTH an alarm and a gate. `scan()` logs/persists the divergence (the alarm).
`neutralize_fabricated_modes()` lets a caller surgically remove the failure mode(s) whose
premise rests on a fabricated imminent event, so the caller can recompute its decision
without the influence of a proven-false fact. This was upgraded from alarm-only after the
06-05 post-mortem: an independent re-verification found the FOMC fabrication manufactured ~58
spurious BLOCKs in a single day, the monitor saw every one of them, yet — being alarm-only —
let them all stand. Gating only ever REMOVES phantom risk (it can downgrade a BLOCK driven by
a lie; it never invents new risk), so it cannot make the advocate more reckless.

Scope today: macro-event claims (FOMC / CPI / NFP) vs the calendar — the exact bug class
we hit. Designed to extend to price/earnings claims.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import date, datetime, timezone

logger = logging.getLogger(__name__)

# Event term → synonyms an LLM might use.
_EVENT_TERMS: dict[str, list[str]] = {
    "fomc": ["fomc", "fed rate", "rate decision", "fed meeting", "federal reserve meeting", "fed decision"],
    "cpi":  ["cpi", "inflation report", "inflation print", "inflation data"],
    "nfp":  ["nfp", "non-farm", "nonfarm", "payrolls", "jobs report", "jobs number"],
}
# Concrete day-level imminence claims (deliberately NOT vague words like "imminent",
# which false-positive inside "not imminent" / "in two weeks, not imminent").
_NEAR_TERMS = ["tomorrow", "today", "this session", "next session", "later today",
               "hours away", "intraday"]
_NEGATIONS = ("not", "no", "isn't", "aren't", "won't", "never", "without")
_NEAR_HORIZON_DAYS = 2   # an "imminent" claim is wrong if the real event is > this many days out


_NEG_RE = re.compile(r"\b(" + "|".join(_NEGATIONS) + r")\b")
# Distancing phrases that defuse an imminence word elsewhere in the clause.
_DISTANCING = ("weeks away", "weeks out", "days out", "days away", "not until",
               "well ahead", "two weeks", "next week", "later this month")


def _has_unnegated_near(low: str) -> bool:
    """True if an imminence term appears that is NOT negated/distanced in its local clause.
    Looks back ~22 chars for a negation and scans the local window for distancing phrases —
    so 'do not expect FOMC tomorrow' and 'FOMC weeks away' do NOT trigger, but
    'FOMC vol spike tomorrow' does."""
    for t in _NEAR_TERMS:
        for m in re.finditer(re.escape(t), low):
            pre = low[max(0, m.start() - 22):m.start()]
            window = low[max(0, m.start() - 40):m.start() + len(t) + 20]
            if _NEG_RE.search(pre):
                continue
            if any(p in window for p in _DISTANCING):
                continue
            return True
    return False


def _days_to_next_of_type(cal, etype: str) -> int | None:
    """Calendar days to the next scheduled event of a given type (None if none upcoming)."""
    try:
        today = date.today()
        cands = [e.event_date for e in cal.upcoming_events(days=180) if e.event_type == etype]
        return (min(cands) - today).days if cands else None
    except Exception:
        return None


def check_event_claims(text: str, source: str = "", ticker: str = "") -> list[dict]:
    """
    Scan agent reasoning for macro-event claims and validate against the macro calendar.
    Returns a list of divergence dicts (empty when claims are grounded or none are made).

    Flags HIGH only when an agent asserts an event is IMMINENT but the real next event of
    that type is > _NEAR_HORIZON_DAYS away — i.e. the exact "FOMC tomorrow" hallucination.
    """
    if not text:
        return []
    low = text.lower()
    claimed = [ev for ev, terms in _EVENT_TERMS.items() if any(t in low for t in terms)]
    if not claimed:
        return []
    if not _has_unnegated_near(low):
        return []   # only validate genuine imminence claims — generic/negated mentions are fine

    try:
        from trading_platform.services.macro_calendar import get_macro_calendar
        cal = get_macro_calendar()
        next_days, next_desc = cal.days_to_next_event()
    except Exception as exc:
        logger.debug("fact_grounding: calendar unavailable: %s", exc)
        return []

    divergences: list[dict] = []
    for ev in claimed:
        real_days = _days_to_next_of_type(cal, ev)
        # The agent says this event is imminent. Is it actually within the near horizon?
        if real_days is None or real_days > _NEAR_HORIZON_DAYS:
            divergences.append({
                "source":            source,
                "ticker":            ticker,
                "claimed_event":     ev.upper(),
                "claimed_timing":    "imminent (today/tomorrow)",
                "actual_event_days": real_days if real_days is not None else -1,
                "actual_next_event": f"{next_desc} in {next_days}d",
                "issue": (f"agent asserted {ev.upper()} is imminent, but the next {ev.upper()} is "
                          f"{('%dd out' % real_days) if real_days is not None else 'not scheduled'}; "
                          f"the actual next macro event is {next_desc} in {next_days}d"),
                "severity": "high",
            })
    return divergences


def neutralize_fabricated_modes(
    failure_modes: list, divergences: list[dict]
) -> tuple[list, list[str]]:
    """Given an advocate's failure_modes and the divergences `check_event_claims` found, drop the
    failure mode(s) whose premise rests on a fabricated IMMINENT macro event. A mode is neutralized
    only if it BOTH names a fabricated event AND asserts imminence about it — so a legitimate
    'post-FOMC drift over the coming weeks' mode (event named, not claimed imminent) is preserved.

    Returns (clean_modes, removed_mode_names). Surgical by design: it removes phantom risk and
    nothing else, so a recomputed verdict can only relax, never tighten."""
    if not divergences or not failure_modes:
        return failure_modes, []
    fab_terms: set[str] = set()
    for d in divergences:
        ev = (d.get("claimed_event") or "").lower()
        if ev in _EVENT_TERMS:
            fab_terms.update(_EVENT_TERMS[ev])
    if not fab_terms:
        return failure_modes, []

    clean: list = []
    removed: list[str] = []
    for fm in failure_modes:
        if not isinstance(fm, dict):
            clean.append(fm)
            continue
        triggers = fm.get("trigger_conditions", "")
        if isinstance(triggers, list):
            triggers = " ".join(str(t) for t in triggers)
        blob = " ".join(str(fm.get(k, "")) for k in ("mode_name", "scenario")) + " " + str(triggers)
        blob = blob.lower()
        if any(term in blob for term in fab_terms) and _has_unnegated_near(blob):
            removed.append(str(fm.get("mode_name", "?")))
        else:
            clean.append(fm)
    return clean, removed


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fact_divergence_log (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc           TEXT NOT NULL,
            source           TEXT NOT NULL,
            ticker           TEXT,
            claimed_event    TEXT,
            claimed_timing   TEXT,
            actual_next_event TEXT,
            issue            TEXT,
            severity         TEXT
        )
    """)


def scan(text: str, db_path: str, source: str, ticker: str = "") -> list[dict]:
    """
    Check agent reasoning, persist + log any divergences, and return them. Never raises —
    a monitor must never break the path it observes.
    """
    try:
        divs = check_event_claims(text, source=source, ticker=ticker)
    except Exception as exc:
        logger.debug("fact_grounding.scan check failed: %s", exc)
        return []
    if not divs:
        return []
    # Loud, routed log — this is the alarm the post-mortem said we lacked.
    for d in divs:
        logger.critical(
            "FACT DIVERGENCE [%s/%s]: %s | %s",
            d["source"], d["ticker"] or "-", d["severity"].upper(), d["issue"],
        )
    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            _ensure_table(conn)
            now = datetime.now(timezone.utc).isoformat()
            for d in divs:
                conn.execute(
                    """INSERT INTO fact_divergence_log
                       (ts_utc, source, ticker, claimed_event, claimed_timing,
                        actual_next_event, issue, severity)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (now, d["source"], d["ticker"], d["claimed_event"], d["claimed_timing"],
                     d["actual_next_event"], d["issue"], d["severity"]),
                )
    except Exception as exc:
        logger.debug("fact_grounding.scan persist failed: %s", exc)
    return divs

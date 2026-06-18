"""
agora/ops/entry_filters.py — small, pure entry-gate predicates (roadmap Tier 2).

Kept as standalone functions so the entry-gate decisions are unit-testable without standing up the
whole session. Pure: no I/O, no state. _entry_gate calls these and acts on the boolean.
"""
from __future__ import annotations


def bearish_debit_blocked_in_risk_on(
    direction: str, entry_debit_credit: float,
    macro_stance: str, confidence: float, *, min_confidence: float,
) -> bool:
    """S2.1 — True iff this is a bearish DEBIT structure (we PAY premium, so long_put /
    bear_put_spread) entered into a CONFIRMED risk-on tape. Such trades fight positive drift AND
    theta — the directional pillar's biggest historical bleed. Bearish premium-selling (a credit,
    entry_debit_credit < 0) and the long_call side are intentionally NOT caught here."""
    is_debit = float(entry_debit_credit or 0) > 0
    is_bearish = str(direction or "").lower() == "bearish"
    is_risk_on = str(macro_stance or "") == "risk_on"
    confident = float(confidence or 0) >= float(min_confidence)
    return is_bearish and is_debit and is_risk_on and confident

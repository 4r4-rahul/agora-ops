"""
agora/core/instruments.py — instrument routing registry.

Maps a trading symbol to the contract metadata the IBKR + yfinance paths need, so the system can
handle INDEX options (XSP / SPX / VIX — CBOE-routed, European-style, cash-settled) alongside the
default US-EQUITY options (SMART-routed, American, share-settled).

This is the single source of truth that the option/underlying construction sites and the sizing
logic key off — instead of hardcoding `Stock(...)` / `exchange="SMART"` / `multiplier="100"`
everywhere. Pure + dependency-free so it's trivially testable and safe to import anywhere.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class InstrumentSpec:
    symbol: str
    underlying_sec_type: str   # IBKR underlying secType: "STK" (equity/ETF) or "IND" (index)
    exchange: str              # routing for options AND the underlying: "SMART" or "CBOE"
    multiplier: str            # option contract multiplier (string, as IBKR requires)
    yf_symbol: str             # yfinance ticker (indices are caret-prefixed, e.g. "^XSP")
    is_index: bool             # index option (no shares; different lifecycle)
    cash_settled: bool         # cash settlement → no assignment / no shares to manage
    european: bool             # European exercise → no early assignment, settle at expiry only


# Index options the system may trade. XSP (Mini-SPX, 1/10th of SPX) is the account-appropriate one;
# SPX/VIX are listed for completeness/routing but are intentionally NOT in the default universe
# (SPX notional is too large for a small book — see the universe gate).
_INDEX_SPECS: dict[str, InstrumentSpec] = {
    "XSP": InstrumentSpec("XSP", "IND", "CBOE", "100", "^XSP", True, True, True),
    "SPX": InstrumentSpec("SPX", "IND", "CBOE", "100", "^SPX", True, True, True),
    "SPXW": InstrumentSpec("SPXW", "IND", "CBOE", "100", "^SPX", True, True, True),  # SPX weeklys
    "VIX": InstrumentSpec("VIX", "IND", "CBOE", "100", "^VIX", True, True, True),
}


def instrument_spec(symbol: str) -> InstrumentSpec:
    """Return the routing/contract spec for a symbol. Unknown symbols default to a SMART-routed
    US-equity option (the existing behavior), so this is safe to call anywhere unconditionally."""
    s = (symbol or "").upper().strip()
    spec = _INDEX_SPECS.get(s)
    if spec is not None:
        return spec
    return InstrumentSpec(s, "STK", "SMART", "100", s, False, False, False)


def is_index_option(symbol: str) -> bool:
    """True if this symbol's options are index (CBOE, European, cash-settled) rather than equity."""
    return instrument_spec(symbol).is_index

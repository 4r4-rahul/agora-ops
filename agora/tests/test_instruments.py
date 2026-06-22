"""
agora/tests/test_instruments.py — instrument routing registry (XSP foundation).

Pins the equity-default behavior (unknown/equity symbols → SMART/STK, unchanged) and the index
specs (XSP/SPX → CBOE/IND, European, cash-settled) so the contract-construction + sizing wiring can
key off a single source of truth instead of hardcoded SMART/Stock everywhere.
"""
from __future__ import annotations

from agora.core.instruments import instrument_spec, is_index_option


class TestEquityDefault:
    def test_unknown_symbol_is_smart_equity(self):
        s = instrument_spec("AAPL")
        assert s.underlying_sec_type == "STK"
        assert s.exchange == "SMART"
        assert s.multiplier == "100"
        assert s.yf_symbol == "AAPL"
        assert not s.is_index and not s.cash_settled and not s.european

    def test_etfs_are_equity(self):
        for t in ("SPY", "IWM", "QQQ"):
            assert not is_index_option(t)
            assert instrument_spec(t).exchange == "SMART"

    def test_case_and_whitespace_normalized(self):
        assert instrument_spec("  aapl ").symbol == "AAPL"
        assert instrument_spec("xsp").is_index


class TestIndexSpecs:
    def test_xsp_routes_to_cboe_index(self):
        s = instrument_spec("XSP")
        assert s.underlying_sec_type == "IND"
        assert s.exchange == "CBOE"        # NOT SMART
        assert s.yf_symbol == "^XSP"       # caret-prefixed for yfinance
        assert s.is_index and s.cash_settled and s.european

    def test_spx_and_vix_are_index(self):
        for t in ("SPX", "SPXW", "VIX"):
            s = instrument_spec(t)
            assert s.is_index and s.exchange == "CBOE" and s.underlying_sec_type == "IND"
        assert instrument_spec("SPX").yf_symbol == "^SPX"
        assert instrument_spec("SPXW").yf_symbol == "^SPX"

    def test_is_index_option_helper(self):
        assert is_index_option("XSP") is True
        assert is_index_option("SPY") is False

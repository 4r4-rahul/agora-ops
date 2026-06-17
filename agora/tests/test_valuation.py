"""
agora/tests/test_valuation.py — fundamental valuation + earnings-revision scoring (feeds the
conviction adjustment that sizes/gates entries). Pure scorers are pinned exactly; the yfinance
fetch path is mocked so we exercise the full cheap/fair/expensive + revision-momentum + capped
conviction composition deterministically.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

import agora.ops.valuation as val
from agora.ops.valuation import (
    ValuationGate,
    _get_medians,
    _neutral_result,
    _revision_classify,
    _safe_int,
    _score_metric,
    _tier_from_score,
    get_valuation,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    with val._cache_lock:
        val._val_cache.clear()
    yield
    with val._cache_lock:
        val._val_cache.clear()


# ── _score_metric ─────────────────────────────────────────────────────────────
class TestScoreMetric:
    @pytest.mark.parametrize("value,median,expected", [
        (7.5, 10.0, 2.5),    # ratio 0.75 → deep value
        (5.0, 10.0, 2.5),    # ratio 0.5
        (9.0, 10.0, 1.5),    # ratio 0.9 (<=1.0)
        (10.0, 10.0, 1.5),   # ratio 1.0 boundary
        (14.0, 10.0, 0.5),   # ratio 1.4 (<=1.5)
        (15.0, 10.0, 0.5),   # ratio 1.5 boundary
        (20.0, 10.0, 0.0),   # ratio 2.0 → expensive
    ])
    def test_ratio_bands(self, value, median, expected):
        assert _score_metric(value, median) == expected

    @pytest.mark.parametrize("value,median", [(None, 10.0), (0, 10.0), (-5, 10.0), (10, 0)])
    def test_missing_or_bad_returns_zero(self, value, median):
        assert _score_metric(value, median) == 0.0


# ── _get_medians ──────────────────────────────────────────────────────────────
class TestGetMedians:
    def test_none_sector_uses_default(self):
        assert _get_medians(None) == val._SECTOR_MEDIANS["_default"]

    def test_known_sector_substring_match(self):
        assert _get_medians("Technology") == val._SECTOR_MEDIANS["Technology"]
        # substring, case-insensitive
        assert _get_medians("information technology") == val._SECTOR_MEDIANS["Technology"]

    def test_unknown_sector_uses_default(self):
        assert _get_medians("Cryptocurrency") == val._SECTOR_MEDIANS["_default"]


# ── _tier_from_score ──────────────────────────────────────────────────────────
class TestTierFromScore:
    @pytest.mark.parametrize("score,tier,adj", [
        (9.0, "cheap", 5), (7.0, "cheap", 5),
        (6.0, "fair", 0), (5.0, "fair", 0),
        (4.0, "expensive", -5), (3.0, "expensive", -5),
        (2.0, "very_expensive", -10), (0.0, "very_expensive", -10),
    ])
    def test_tiers(self, score, tier, adj):
        assert _tier_from_score(score) == (tier, adj)


# ── _revision_classify ────────────────────────────────────────────────────────
class TestRevisionClassify:
    @pytest.mark.parametrize("net,signal,adj", [
        (11, "strong_upgrade", 5),
        (4, "upgrade", 2),
        (3, "neutral", 0), (0, "neutral", 0), (-3, "neutral", 0),
        (-4, "downgrade", -3), (-10, "downgrade", -3),
        (-11, "strong_downgrade", -7),
    ])
    def test_bands(self, net, signal, adj):
        assert _revision_classify(net) == (signal, adj)


# ── _safe_int ─────────────────────────────────────────────────────────────────
class TestSafeInt:
    def test_valid(self):
        assert _safe_int(5) == 5
        assert _safe_int("7") == 7
        assert _safe_int(3.9) == 3

    def test_bad_returns_zero(self):
        assert _safe_int(None) == 0
        assert _safe_int("abc") == 0


# ── _neutral_result ───────────────────────────────────────────────────────────
def test_neutral_result_shape():
    r = _neutral_result("AAPL", "test reason")
    assert r.valuation_score == 5.0
    assert r.valuation_tier == "fair"
    assert r.conviction_adj == 0
    assert r.revision_signal == "neutral"
    assert r.reason_str == "test reason"


# ── get_valuation (ETF exemption + cache + fetch) ─────────────────────────────
def _mock_ticker(info: dict, revisions: pd.DataFrame | None = None):
    obj = MagicMock()
    obj.info = info
    obj.eps_revisions = revisions if revisions is not None else pd.DataFrame()
    return obj


class TestGetValuation:
    def test_etf_is_exempt(self):
        r = get_valuation("SPY")
        assert r.conviction_adj == 0 and "ETF" in r.reason_str

    def test_cheap_stock_scores_high(self):
        # All metrics well below the Technology medians (pe30/ev25/pb8) → cheap → +5
        info = {"trailingPE": 12.0, "enterpriseToEbitda": 10.0,
                "priceToBook": 3.0, "sector": "Technology"}
        with patch.object(val.yf, "Ticker", return_value=_mock_ticker(info)):
            r = get_valuation("CHEAPCO")
        assert r.valuation_tier == "cheap"
        assert r.valuation_score >= 7.0
        assert r.conviction_adj == 5   # cheap +5, neutral revisions 0

    def test_expensive_stock_scores_low(self):
        info = {"trailingPE": 80.0, "enterpriseToEbitda": 60.0,
                "priceToBook": 20.0, "sector": "Technology"}
        with patch.object(val.yf, "Ticker", return_value=_mock_ticker(info)):
            r = get_valuation("RICHCO")
        assert r.valuation_tier == "very_expensive"
        assert r.conviction_adj == -10

    def test_forward_pe_used_when_trailing_missing(self):
        info = {"forwardPE": 12.0, "enterpriseToEbitda": 10.0,
                "priceToBook": 3.0, "sector": "Technology"}
        with patch.object(val.yf, "Ticker", return_value=_mock_ticker(info)):
            r = get_valuation("LOSSCO")
        assert r.pe_trailing is None
        assert r.pe_forward == 12.0
        assert r.valuation_tier == "cheap"   # forward PE counted in score

    def test_revision_momentum_adds_to_conviction(self):
        info = {"trailingPE": 20.0, "enterpriseToEbitda": 15.0,
                "priceToBook": 3.0, "sector": "Healthcare"}   # ~fair
        rev = pd.DataFrame(
            {"upLast30days": [15, 0], "downLast30days": [0, 0]},
            index=["0q", "+1q"],
        )
        with patch.object(val.yf, "Ticker", return_value=_mock_ticker(info, rev)):
            r = get_valuation("UPGRADECO")
        assert r.revision_score == 15
        assert r.revision_signal == "strong_upgrade"
        assert r.conviction_adj >= 5   # fair(0) + strong_upgrade(+5)

    def test_conviction_adj_capped(self):
        # cheap (+5) AND strong upgrade (+5) = +10, the cap — never exceeds it
        info = {"trailingPE": 8.0, "enterpriseToEbitda": 6.0,
                "priceToBook": 1.0, "sector": "Technology"}
        rev = pd.DataFrame({"upLast30days": [50], "downLast30days": [0]}, index=["0q"])
        with patch.object(val.yf, "Ticker", return_value=_mock_ticker(info, rev)):
            r = get_valuation("MOONCO")
        assert r.conviction_adj == 10   # capped at +10

    def test_yfinance_failure_returns_neutral(self):
        with patch.object(val.yf, "Ticker", side_effect=RuntimeError("network down")):
            r = get_valuation("BROKEN")
        assert r.conviction_adj == 0 and r.valuation_tier == "fair"

    def test_cache_avoids_second_fetch(self):
        info = {"trailingPE": 20.0, "enterpriseToEbitda": 15.0,
                "priceToBook": 3.0, "sector": "Healthcare"}
        m = MagicMock(return_value=_mock_ticker(info))
        with patch.object(val.yf, "Ticker", m):
            get_valuation("CACHECO")
            get_valuation("CACHECO")   # second call should hit cache
        assert m.call_count == 1


# ── ValuationGate ─────────────────────────────────────────────────────────────
def test_valuation_gate_delegates():
    info = {"trailingPE": 12.0, "enterpriseToEbitda": 10.0,
            "priceToBook": 3.0, "sector": "Technology"}
    with patch.object(val.yf, "Ticker", return_value=_mock_ticker(info)):
        r = ValuationGate().evaluate("GATECO")
    assert r.ticker == "GATECO" and r.valuation_tier == "cheap"

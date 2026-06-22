"""
agora/tests/test_ibkr_iv_rank.py — industry-standard #1: IV-rank ATM-IV input from IBKR/OPRA.

Pins the new _ibkr_atm_iv helper: OFF by default (instant None, zero overhead → cannot affect the
snapshot), returns the real IBKR IV when IV_RANK_USE_IBKR is on, and fails closed to None on any
error (caller keeps the yfinance ATM IV). The 252-day cache + percentile rank math is untouched.
"""
from __future__ import annotations

import json
from datetime import date

from trading_platform.services.market_data import yfinance_provider as yp
from trading_platform.services.market_data.yfinance_provider import YFinanceProvider


def test_off_by_default(monkeypatch):
    monkeypatch.delenv("IV_RANK_USE_IBKR", raising=False)
    assert yp._ibkr_atm_iv("SPY", "20260717", 500.0) is None


def test_off_when_flag_false(monkeypatch):
    monkeypatch.setenv("IV_RANK_USE_IBKR", "false")
    assert yp._ibkr_atm_iv("SPY", "20260717", 500.0) is None


def test_returns_call_iv_when_on(monkeypatch):
    monkeypatch.setenv("IV_RANK_USE_IBKR", "true")

    async def _fake(**kw):
        return {(500.0, "C"): {"bid": 1.0, "ask": 1.1, "iv": 0.273},
                (500.0, "P"): {"bid": 1.0, "ask": 1.1, "iv": 0.270}}
    monkeypatch.setattr("trading_platform.services.ibkr_client.fetch_chain_quotes", _fake)
    assert yp._ibkr_atm_iv("SPY", "20260717", 500.0) == 0.273   # prefers the call leg


def test_falls_back_to_put_iv(monkeypatch):
    monkeypatch.setenv("IV_RANK_USE_IBKR", "1")

    async def _fake(**kw):
        return {(500.0, "C"): {"bid": 0, "ask": 0, "iv": 0.0},   # call IV missing
                (500.0, "P"): {"bid": 1.0, "ask": 1.1, "iv": 0.265}}
    monkeypatch.setattr("trading_platform.services.ibkr_client.fetch_chain_quotes", _fake)
    assert yp._ibkr_atm_iv("SPY", "20260717", 500.0) == 0.265


def test_none_on_empty_quotes(monkeypatch):
    monkeypatch.setenv("IV_RANK_USE_IBKR", "on")

    async def _empty(**kw):
        return {}
    monkeypatch.setattr("trading_platform.services.ibkr_client.fetch_chain_quotes", _empty)
    assert yp._ibkr_atm_iv("SPY", "20260717", 500.0) is None


def test_fails_closed_on_error(monkeypatch):
    monkeypatch.setenv("IV_RANK_USE_IBKR", "true")

    async def _boom(**kw):
        raise RuntimeError("no IBKR connection")
    monkeypatch.setattr("trading_platform.services.ibkr_client.fetch_chain_quotes", _boom)
    assert yp._ibkr_atm_iv("SPY", "20260717", 500.0) is None


# ── IV-rank math + the once/ticker/day fetch gate ─────────────────────────────
def test_iv_rank_from_cache_math():
    cache = {"dates": ["a", "b", "c", "d", "e"], "atm_ivs": [0.10, 0.20, 0.30, 0.40, 0.25]}
    r = YFinanceProvider._iv_rank_from_cache(cache)
    assert r["rank"] == 50.0          # (0.25-0.10)/(0.40-0.10)*100
    assert r["percentile"] == 60.0    # 0.10,0.20,0.25 <= 0.25 → 3/5
    assert r["atm_iv"] == 25.0


def test_iv_rank_from_cache_thin_and_empty():
    assert YFinanceProvider._iv_rank_from_cache({"dates": ["a"], "atm_ivs": [0.2]})["rank"] is None
    assert YFinanceProvider._iv_rank_from_cache({"atm_ivs": []})["atm_iv"] is None


def test_once_per_day_skips_fetch_when_today_cached(monkeypatch, tmp_path):
    # Today already in cache → _get_real_iv_rank must NOT touch yfinance or IBKR (the load gate).
    monkeypatch.setattr(YFinanceProvider, "_IV_CACHE_DIR", tmp_path)
    today = date.today().isoformat()
    (tmp_path / "SPY.json").write_text(
        json.dumps({"dates": ["2026-01-01", "2026-01-02", today], "atm_ivs": [0.10, 0.20, 0.13]}))

    def _boom(*a, **k):
        raise AssertionError("must not fetch when today is already cached")
    monkeypatch.setattr(yp.yf, "Ticker", _boom)
    monkeypatch.setattr(yp, "_ibkr_atm_iv", _boom)

    r = YFinanceProvider()._get_real_iv_rank("SPY")
    assert r["atm_iv"] == 13.0                      # last cached reading
    assert r["rank"] == 30.0                        # (0.13-0.10)/(0.20-0.10)*100

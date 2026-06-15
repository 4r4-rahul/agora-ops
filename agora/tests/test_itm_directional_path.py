"""
test_itm_directional_path.py — proves the DARK deep-ITM directional path:

  1. INERT WHEN OFF: with long_options_itm_enabled=False, a ticker whose IVR exceeds the
     OTM cap still hard-skips with the exact original "options too expensive" reason —
     _try_itm_entry is never reached (zero behavior change vs pre-ITM build).
  2. PROCEEDS on a clean ITM setup when enabled (confirmed downtrend + ITM put).
  3. SKIPS on each verifier guard: low conviction, no-trend, RSI exhaustion, thin OI,
     wide bid-ask, premium over the tight ITM per-trade cap.

Run: PYTHONPATH=. .venv/bin/python -m pytest agora/tests/test_itm_directional_path.py -q
"""
from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from agora.core.config import AgoraSettings
from agora.agents.long_options_agent import LongOptionsAgent


def _settings(**over) -> AgoraSettings:
    s = AgoraSettings()
    s.account_size = 10_000.0
    for k, v in over.items():
        setattr(s, k, v)
    return s


def _put_chain(spot=100.0, *, oi=1000, bid=4.0, ask=4.2, itm_delta=-0.75):
    """One expiry ~30 DTE with ITM puts (strikes ABOVE spot) carrying explicit deltas."""
    exp = (date.today() + timedelta(days=30)).isoformat()
    # strikes 90..115; ITM puts are strike>spot. Give the 108 strike the ~0.75 delta.
    rows = []
    for k in (90, 95, 100, 105, 108, 112, 115):
        if k <= spot:
            d = -0.30   # OTM puts
        elif k == 108:
            d = itm_delta
        else:
            d = -0.85   # deeper ITM
        rows.append({
            "strike": float(k), "delta": d, "openInterest": oi,
            "bid": bid if k == 108 else 1.0,
            "ask": ask if k == 108 else 1.2,
            "impliedVolatility": 0.55,
        })
    puts = pd.DataFrame(rows)
    return {exp: {"puts": puts, "calls": puts.copy()}}


def _bearish_momentum(rsi=42.0, ret_10d=-0.08):
    return {
        "rsi": rsi, "ret_10d": ret_10d, "rsi_norm": 0.4,
        "above_sma20": False, "above_sma50": False, "hv5": 0.4, "hv20": 0.4,
    }


def _call_itm(agent, *, enabled_overrides=None, **kw):
    """Invoke _try_itm_entry with sensible bearish-ITM defaults; kw overrides."""
    base = dict(
        ticker="NVDA", spot=100.0, options_chain=_put_chain(),
        opt_type="put", direction="bearish", strategy="long_put",
        conviction=4, quality=3.0, signal_stack={"x": "y"},
        flow_dir="bearish", momentum=_bearish_momentum(),
        active_ivr=90.0, ivr_display=90.0, vix=22.0, regime="risk_off",
        per_ticker_ivr=90.0, session_id="sess",
    )
    base.update(kw)
    return agent._try_itm_entry(**base)


# ──────────────────────────────────────────────────────────────────────────────
def test_inert_when_off():
    """itm disabled + IVR over OTM cap → original hard-skip; ITM branch never fires."""
    s = _settings(long_options_itm_enabled=False)
    agent = LongOptionsAgent(s)
    macro = SimpleNamespace(iv_rank=90.0, vix=22.0, macro_stance="risk_off")
    dec = agent._evaluate_inner(
        ticker="NVDA", spot=100.0, options_chain=_put_chain(),
        macro_context=macro, flow_signals=None, momentum=_bearish_momentum(),
        gex_regime="neutral", session_id="sess", per_ticker_ivr=90.0,
        days_to_catalyst=None,
    )
    assert dec.outcome == "skipped"
    assert "options too expensive" in dec.block_reason
    assert "ITM" not in dec.block_reason          # the ITM builder was never reached
    assert dec.recommendation is None


def test_itm_proceeds_on_clean_setup():
    s = _settings(long_options_itm_enabled=True)
    agent = LongOptionsAgent(s)
    dec = _call_itm(agent)
    assert dec.outcome == "proceed", dec.block_reason
    assert dec.recommendation is not None
    rec = dec.recommendation
    assert rec.contracts == 1                      # tight 1-contract sizing
    assert rec.legs[0].strike > 100.0              # ITM put: strike above spot
    assert rec.direction == "bearish"
    assert "ITM-directional" in rec.reasoning


def test_itm_skips_low_conviction():
    agent = LongOptionsAgent(_settings(long_options_itm_enabled=True))
    dec = _call_itm(agent, conviction=2)
    assert dec.outcome == "skipped" and "conviction" in dec.block_reason


def test_itm_skips_no_trend():
    agent = LongOptionsAgent(_settings(long_options_itm_enabled=True))
    dec = _call_itm(agent, momentum=_bearish_momentum(ret_10d=-0.01))  # below trend_min
    assert dec.outcome == "skipped" and "downtrend" in dec.block_reason


def test_itm_skips_rsi_capitulation():
    """ITM put with RSI deep oversold → exhaustion guard blocks (bounce risk)."""
    agent = LongOptionsAgent(_settings(long_options_itm_enabled=True))
    dec = _call_itm(agent, momentum=_bearish_momentum(rsi=30.0))
    assert dec.outcome == "skipped" and "oversold" in dec.block_reason


def test_itm_skips_thin_oi():
    agent = LongOptionsAgent(_settings(long_options_itm_enabled=True))
    dec = _call_itm(agent, options_chain=_put_chain(oi=100))   # < 500 floor
    assert dec.outcome == "skipped" and "OI=" in dec.block_reason


def test_itm_skips_wide_bid_ask():
    agent = LongOptionsAgent(_settings(long_options_itm_enabled=True))
    dec = _call_itm(agent, options_chain=_put_chain(bid=3.6, ask=4.6))  # 1.0/4.1 ≈ 24% spread
    assert dec.outcome == "skipped" and "bid-ask" in dec.block_reason


def test_itm_skips_premium_over_cap():
    """$10.2/sh × 100 = $1,020 > 5% of $10k ($500) → over the tight ITM per-trade cap."""
    agent = LongOptionsAgent(_settings(long_options_itm_enabled=True))
    dec = _call_itm(agent, options_chain=_put_chain(bid=10.0, ask=10.4))  # mid 10.2 → $1,020
    assert dec.outcome == "skipped" and "per-trade cap" in dec.block_reason


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))

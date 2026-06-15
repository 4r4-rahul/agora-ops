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


# ── Paper-aware activation gate (live obeys flag; paper force-runs for data) ───
def _evaluate(agent, *, ivr, **kw):
    macro = SimpleNamespace(iv_rank=ivr, vix=22.0, macro_stance="risk_off")
    return agent._evaluate_inner(
        ticker="NVDA", spot=100.0, options_chain=_put_chain(),
        macro_context=macro, flow_signals=None, momentum=_bearish_momentum(),
        gex_regime="neutral", session_id="sess", per_ticker_ivr=ivr,
        days_to_catalyst=None, **kw,
    )


def test_live_disabled_skips_expensive():
    """LIVE + itm disabled + IVR in the ITM band → original OTM hard-skip; ITM never fires.

    IVR=75 sits in the ITM band (otm_cap 60 < 75 <= ceiling 85), so the ONLY thing keeping
    it on the OTM 'too expensive' path is that live obeys long_options_itm_enabled (False)."""
    agent = LongOptionsAgent(_settings(long_options_itm_enabled=False, trading_mode="live"))
    dec = _evaluate(agent, ivr=75.0)
    assert dec.outcome == "skipped"
    assert "options too expensive" in dec.block_reason   # OTM gate fired
    assert "ITM" not in dec.block_reason                 # ITM builder never reached


def test_paper_activates_itm_path():
    """PAPER + itm disabled-flag but paper_data_collection on → OTM 'too expensive' gate is
    BYPASSED and the ITM fork engages. Proven by the absence of the OTM skip reason (the
    decision either proceeds via ITM or skips for a scoring/ITM-specific reason instead)."""
    agent = LongOptionsAgent(_settings(long_options_itm_enabled=False, trading_mode="paper"))
    dec = _evaluate(agent, ivr=75.0)
    assert "options too expensive" not in (dec.block_reason or "")  # OTM gate was bypassed


def test_paper_can_be_opted_out():
    """Paper data-collection flag OFF → paper behaves like live (OTM gate fires)."""
    agent = LongOptionsAgent(_settings(
        long_options_itm_enabled=False, long_options_itm_paper_data_collection=False,
        trading_mode="paper"))
    dec = _evaluate(agent, ivr=75.0)
    assert "options too expensive" in dec.block_reason


def test_above_itm_ceiling_skips_even_in_paper():
    """IVR above the ITM ceiling (85) → even ITM is too rich; skip regardless of mode."""
    agent = LongOptionsAgent(_settings(long_options_itm_enabled=True, trading_mode="paper"))
    dec = _evaluate(agent, ivr=90.0)
    assert dec.outcome == "skipped" and "options too expensive" in dec.block_reason


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


# ── Lifetime tracking: ITM entries are distinguishable in long_journal ─────────
def test_itm_journaled_with_marker(tmp_path):
    """A proceeded ITM entry persists is_itm=1 + dte_reason='itm-directional', so the path's
    lifetime performance is attributable in the DB (vs being indistinguishable from OTM)."""
    import sqlite3
    from agora.ops.db_migrations import run_all
    db = str(tmp_path / "j.db")
    run_all(db)
    s = _settings(long_options_itm_enabled=True)
    s.db_path = db
    agent = LongOptionsAgent(s)
    dec = _call_itm(agent)
    assert dec.outcome == "proceed", dec.block_reason
    agent.journal(dec, db, position_id="ITM-1")
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    r = c.execute("SELECT is_itm, dte_reason, strategy FROM long_journal WHERE position_id='ITM-1'").fetchone()
    assert r["is_itm"] == 1
    assert r["dte_reason"] == "itm-directional"
    assert r["strategy"] == "long_put"


def test_otm_journaled_without_itm_marker(tmp_path):
    """Control: an OTM long entry journals is_itm=0 — the marker truly separates the two paths."""
    import sqlite3
    from agora.ops.db_migrations import run_all
    from agora.agents.long_options_agent import LongDecision
    from agora.core.models import TradeRecommendation, SpreadLeg, StrategyPillar
    from datetime import date, timedelta
    db = str(tmp_path / "j.db")
    run_all(db)
    agent = LongOptionsAgent(_settings())
    exp = date.today() + timedelta(days=30)
    rec = TradeRecommendation(
        session_id="s", ticker="AMD", strategy="long_call", pillar=StrategyPillar.DIRECTIONAL,
        direction="bullish",
        legs=[SpreadLeg(option_type="call", strike=110.0, expiration=exp, action="buy",
                        contracts=1, delta=0.35, mid_price=2.0)],
        contracts=1, entry_debit_credit=200.0, max_loss_dollars=200.0, max_gain_dollars=400.0,
        reward_risk_ratio=2.0,
    )
    dec = LongDecision(ticker="AMD", strategy="long_call", outcome="proceed", block_reason="",
                       recommendation=rec, dte=30, strike=110.0, delta_approx=0.35, premium=2.0,
                       dte_reason="4factor:standard", contracts=1)
    agent.journal(dec, db, position_id="OTM-1")
    c = sqlite3.connect(db)
    r = c.execute("SELECT is_itm, dte_reason FROM long_journal WHERE position_id='OTM-1'").fetchone()
    assert r[0] == 0 and r[1] == "4factor:standard"

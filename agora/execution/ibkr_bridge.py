"""
AGORA → IBKR execution bridge.

Translates TradeRecommendation / OpenPosition into the leg format
expected by trading_platform.services.ibkr_client, then dispatches
to either the real IBKR gateway (live mode) or a dry-run log (paper mode).

Stop-loss is NOT submitted as an order — IBKR rejects STP orders on BAG
(combo) contracts. The PositionManager owns stop-loss enforcement by polling
prices every 60s and calling close_trade() when the threshold is breached.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any

logger = logging.getLogger(__name__)

# ib_insync manages its own internal event loop and is not safe to call from
# inside uvicorn's asyncio loop. All IBKR calls are offloaded to a dedicated
# single-threaded executor where each call gets a brand-new event loop via
# _run_in_new_loop(), bypassing the "already running" asyncio.run() restriction.
# Entry/selection IBKR work (enrich, reprice, submit) serializes on ONE thread — ib_insync
# is not asyncio-safe, and one combo at a time is the safe pattern.
_IBKR_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ibkr")

# EXITS run on a SEPARATE executor (C1). A stop-loss / risk close is latency-critical and must
# NEVER be queued behind a slow entry walk (~2.4 min) or chain enrich. Distinct thread + distinct
# IBKR clientId (ibkr_client_id+1) → closes execute concurrently with entry work.
_IBKR_EXIT_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ibkr-exit")


def _run_in_new_loop(coro):
    """Run a coroutine in a fresh event loop — safe to call from a thread pool."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        loop.close()
        asyncio.set_event_loop(None)


# Credit strategies — a credit-spread BAG (entry OR close) is flagged by IBKR as a
# "riskless/guaranteed-loss combination" and rejected with Error 201 on the paper
# account. These route leg-by-leg in paper mode (entry and close). [[StrategyType]]
_CREDIT_STRATEGIES = {"bull_put_spread", "bear_call_spread", "iron_condor"}


# ── Leg translation helpers ────────────────────────────────────────────────────

def _rec_to_legs(rec: Any) -> list[dict]:
    """SpreadLeg list from a TradeRecommendation → ibkr_client leg dicts."""
    today = date.today()
    return [
        {
            "strike":         leg.strike,
            "option_type":    leg.option_type,       # "call" | "put"
            "action":         leg.action.upper(),     # "BUY" | "SELL"
            "quantity":       1,                      # contracts multiplied at order level
            "expiration_dte": max(1, (leg.expiration - today).days),
            # Exact chain-selected expiry — IBKR uses this verbatim instead of re-deriving a
            # 'nearest Friday' from DTE, which shifted weekly expiries to a non-existent
            # contract and failed qualification (long-option submits erroring out).
            "expiration_date": leg.expiration.strftime("%Y%m%d"),
        }
        for leg in rec.legs
    ]


def _pos_to_close_legs(pos: Any) -> list[dict]:
    """Position legs in ibkr_client leg-dict form, carrying the ORIGINAL ENTRY actions.
    The close functions (close_position / close_position_legs) reverse the action
    themselves to flatten. Do NOT reverse here — doing so double-reverses (close_position
    reverses again), which re-creates the entry combo and re-opens the position instead
    of closing it (the cause of 'exits not executing'). Verified 2026-06-08."""
    today = date.today()
    return [
        {
            "strike":         leg.strike,
            "option_type":    leg.option_type,
            "action":         leg.action.upper(),     # ORIGINAL entry action (BUY/SELL)
            "quantity":       1,
            "expiration_dte": max(1, (leg.expiration - today).days),
            "expiration_date": leg.expiration.strftime("%Y%m%d"),  # exact expiry (see _rec_to_legs)
        }
        for leg in pos.legs
    ]


# ── Public API ─────────────────────────────────────────────────────────────────

async def submit_trade(rec: Any, settings: Any, session_id: str) -> dict:
    """
    Route a TradeRecommendation to IBKR (live) or log it (paper).

    Profit target:
      Credit spreads: 50% of credit received (buy back at half the premium).
      Debit spreads:  entry + 50% of max_gain (sell when spread rises by half its remaining room).
    Stop loss: 2× entry debit/credit (informational — enforced by PositionManager).
    """
    # IBKR combo (BAG) orders use per-share pricing; entry_debit_credit is total dollars.
    # Divide by (contracts × 100) to get the per-share limit price TWS expects.
    per_share_divisor  = max(1, rec.contracts) * 100
    entry_per_share    = rec.entry_debit_credit / per_share_divisor
    max_gain_per_share = getattr(rec, "max_gain_dollars", 0.0) / per_share_divisor

    if entry_per_share > 0:
        # Debit spread (BUY): close when spread value rises above entry.
        # profit_target = entry + 50% of remaining max gain.
        # Bug guard: abs(entry) * 0.50 is WRONG here — it prices the GTC SELL below
        # entry, which fires immediately when bid > half of debit paid.
        profit_target = entry_per_share + max_gain_per_share * 0.50
    else:
        # Credit spread (SELL): close when the spread can be bought back cheaply.
        # profit_target = 50% of credit received = the price to buy back.
        profit_target = max_gain_per_share * 0.50

    stop_loss = abs(entry_per_share) * 2.00

    mode_label = "PAPER" if settings.trading_mode == "paper" else "LIVE"
    logger.info(
        "%s OPEN  | %-6s | %-22s | contracts=%d | entry=%+.4f/sh"
        " | target=%.4f | stop=%.4f | score=%.0f | port=%d",
        mode_label, rec.ticker, rec.strategy.value, rec.contracts,
        entry_per_share, profit_target, stop_loss,
        rec.conviction_score, settings.ibkr_port,
    )

    try:
        from trading_platform.services.ibkr_client import (
            place_bracket_order,
            place_legs_individually,
        )
    except ImportError as exc:
        raise RuntimeError(
            "ib_insync is not installed or ibkr_client is unavailable"
        ) from exc

    kwargs = dict(
        ticker=rec.ticker,
        legs=_rec_to_legs(rec),
        contracts=rec.contracts,
        entry_price=entry_per_share,
        profit_target=profit_target,
        stop_loss=stop_loss,
        session_id=session_id,
        host=settings.ibkr_host,
        port=settings.ibkr_port,
        client_id=settings.ibkr_client_id,
        price_step_size=getattr(settings, "pricing_step_size", 0.05),
        use_adaptive_algo=getattr(settings, "use_adaptive_algo", False),
        adaptive_algo_priority=getattr(settings, "adaptive_algo_priority", "Normal"),
    )

    # Both paths price off IBKR per-leg/combo market data (delayed by default) and
    # apply the execution-time liquidity gate.
    kwargs["market_data_type"] = getattr(settings, "ibkr_market_data_type", 3)
    kwargs["max_combo_spread_pct"] = getattr(settings, "max_combo_spread_pct", 0.50)
    kwargs["pricing_sanity_max_ratio"] = getattr(settings, "pricing_sanity_max_ratio", 2.0)
    # BOARD RULING: mode-dependent fill window. PAPER = patience (the simulator fills on a 2-4 min
    # lag, so wait ~4 min to land the slow fills + measure every validated setup). LIVE = speed (real
    # fills are instant; if not filled in ~45s the market moved → abort, don't chase slippage).
    kwargs["max_walk_steps"] = (getattr(settings, "entry_walk_steps_paper", 20)
                                if settings.trading_mode == "paper"
                                else getattr(settings, "entry_walk_steps_live", 4))

    # Spread-type-aware routing (verified 2026-06-08):
    #   • CREDIT spreads (entry credit < 0) on the PAPER account → leg-by-leg. IBKR
    #     hard-rejects credit-spread BAGs with Error 201 (riskless/guaranteed-loss combo
    #     limit) — a paper-account restriction that "Bypass Order Precautions for API
    #     Orders" does NOT clear. Individual legs aren't riskless combos, so they go through.
    #   • Everything else (debit spreads, and ALL live orders) → atomic BAG. Debit spreads
    #     fill fine as a BAG and atomicity avoids a naked-short leg gap; live accounts allow
    #     riskless combos.
    is_credit = entry_per_share < 0
    is_single_leg = len(getattr(rec, "legs", []) or []) == 1
    # entry_marketable_start is ONLY a place_legs_individually param — set it inside the leg-by-leg
    # branches, never on the place_bracket_order (BAG) path which doesn't accept it.
    _marketable = getattr(settings, "entry_marketable_start", True)
    if is_single_leg:
        # A single long leg (long_call/long_put) must NOT be wrapped in a BAG combo: IBKR cannot
        # MODIFY a combo order via re-place, so the reprice walk was rejected with Error 103
        # ("Duplicate order id") on every long-option entry — freezing fills at 0%. Adaptive is
        # also silently ignored on BAGs. Native single-leg orders are repriceable and fill.
        fn = place_legs_individually
        kwargs["adaptive_single_leg"] = getattr(settings, "use_adaptive_single_leg", True)
        kwargs["entry_marketable_start"] = _marketable
        logger.info("Single-leg %s %s → native leg order + Adaptive=%s",
                    rec.ticker, getattr(rec.strategy, "value", rec.strategy),
                    kwargs["adaptive_single_leg"])
    elif settings.trading_mode == "paper":
        # PAPER: route ALL spreads (credit AND debit) leg-by-leg. Credit BAGs hit the riskless-combo
        # Error 201; debit BAGs hit Error 103 on the walk-modify (can't reprice a combo) and froze at
        # 0% fill. Legging in (long protective leg first, then short) sidesteps both AND lets each leg
        # start at its marketable cross — the only paper path that actually fills. Leg-gap risk is
        # simulated/acceptable in paper. LIVE keeps the atomic BAG below (no naked-leg gap on real $).
        fn = place_legs_individually
        kwargs["entry_marketable_start"] = _marketable
        logger.info("PAPER %s spread %s → leg-by-leg marketable-start=%s",
                    "credit" if is_credit else "debit", rec.ticker, _marketable)
    else:
        fn = place_bracket_order
        kwargs["max_slippage_pct_of_width"] = getattr(settings, "max_slippage_pct_of_width", 0.10)

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        _IBKR_EXECUTOR,
        lambda: _run_in_new_loop(fn(**kwargs)),
    )


async def reprice_legs(rec: Any, settings: Any) -> list[dict] | None:
    """
    Fetch real IBKR per-leg quotes for the CHOSEN recommendation's legs (precision pricing
    before submission). Runs in the dedicated single-threaded IBKR executor (so it never
    races the entry/close calls). Returns a list aligned with rec.legs, or None if IBKR has
    no quote for any leg — in which case the caller keeps the yfinance economics and lets
    the execution-layer gates be the backstop.
    """
    try:
        from trading_platform.services.ibkr_client import fetch_leg_quotes
    except ImportError:
        return None

    kwargs = dict(
        ticker=rec.ticker,
        legs=_rec_to_legs(rec),
        market_data_type=getattr(settings, "ibkr_market_data_type", 3),
        host=settings.ibkr_host,
        port=settings.ibkr_port,
        client_id=getattr(settings, "ibkr_client_id", 2) + 5,  # dedicated id (entry=2, close=3)
    )
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        _IBKR_EXECUTOR,
        lambda: _run_in_new_loop(fetch_leg_quotes(**kwargs)),
    )


async def enrich_chain(ticker: str, chain_dict: dict, spot: float, settings: Any) -> dict:
    """
    Phase B: override the yfinance chain's bid/ask/IV with REAL IBKR quotes for the OTM
    strikes of ONE target-DTE expiry, so the rules engine selects strikes (credit-per-delta)
    on real prices. yfinance still supplies the strike grid + open interest (cheap reference).

    Bounded to a single expiry (nearest the credit-spread target DTE) and ±range_pct around
    spot to cap the per-candidate fetch (~20 strikes, ~5s). HARD FALLBACK: any error returns
    chain_dict unchanged — this must never break the scan.
    """
    if not chain_dict or spot <= 0:
        return chain_dict
    try:
        from datetime import date as _date

        from trading_platform.services.ibkr_client import fetch_chain_quotes

        range_pct = float(getattr(settings, "ibkr_chain_range_pct", 0.15))
        dte_lo = int(getattr(settings, "ibkr_chain_dte_lo", 18))
        dte_hi = int(getattr(settings, "ibkr_chain_dte_hi", 66))
        md_type = int(getattr(settings, "ibkr_market_data_type", 3))
        lo_k, hi_k = spot * (1.0 - range_pct), spot * (1.0 + range_pct)
        today = _date.today()
        target_dte = (dte_lo + dte_hi) // 2

        # Pick the ONE expiry in-band nearest the target DTE.
        cands = []
        for exp in chain_dict:
            try:
                dte = (_date.fromisoformat(exp) - today).days
            except Exception:
                continue
            if dte_lo <= dte <= dte_hi:
                cands.append((abs(dte - target_dte), exp))
        if not cands:
            return chain_dict
        cands.sort()
        best_exp = cands[0][1]
        data = chain_dict[best_exp]
        calls, puts = data.get("calls"), data.get("puts")

        call_ks = sorted(float(s) for s in (calls["strike"] if calls is not None else [])
                         if spot < float(s) <= hi_k)[:20]
        put_ks = sorted((float(s) for s in (puts["strike"] if puts is not None else [])
                         if lo_k <= float(s) < spot), reverse=True)[:20]
        if not call_ks and not put_ks:
            return chain_dict

        loop = asyncio.get_event_loop()
        quotes = await loop.run_in_executor(
            _IBKR_EXECUTOR,
            lambda: _run_in_new_loop(fetch_chain_quotes(
                ticker=ticker, expiry=best_exp.replace("-", ""),
                call_strikes=call_ks, put_strikes=put_ks, market_data_type=md_type,
                host=settings.ibkr_host, port=settings.ibkr_port,
                client_id=getattr(settings, "ibkr_client_id", 2) + 6,
            )),
        )
        if not quotes:
            return chain_dict

        overridden = 0
        for df, right in ((calls, "C"), (puts, "P")):
            if df is None or "strike" not in getattr(df, "columns", []):
                continue
            for (k, r), q in quotes.items():
                if r != right:
                    continue
                mask = df["strike"].astype(float) == k
                if mask.any():
                    df.loc[mask, "bid"] = q["bid"]
                    df.loc[mask, "ask"] = q["ask"]
                    if q["iv"] > 0 and "impliedVolatility" in df.columns:
                        df.loc[mask, "impliedVolatility"] = q["iv"]
                    # #3: overlay real IBKR greeks (delta drives strike selection; all four feed the
                    # portfolio risk limits). Only override when IBKR returned a value.
                    for _col in ("delta", "gamma", "theta", "vega"):
                        if _col in df.columns and q.get(_col):
                            df.loc[mask, _col] = q[_col]
                    overridden += 1
        if overridden:
            logger.info("IBKR chain enrich: %s %s — overrode %d strike-quotes (Phase B)",
                        ticker, best_exp, overridden)
        return chain_dict
    except Exception as exc:
        logger.warning("IBKR chain enrich failed for %s (%s) — keeping yfinance chain", ticker, exc)
        return chain_dict


async def close_trade(pos: Any, settings: Any, session_id: str) -> dict:
    """
    Close an existing open position at market (MOC limit).

    Paper mode: logs the close with unrealized P&L.
    Live mode: submits a closing combo order via IBKR.
    """
    unrealized = getattr(pos, "unrealized_pnl", 0.0)

    mode_label = "PAPER" if settings.trading_mode == "paper" else "LIVE"
    logger.info(
        "%s CLOSE | %-6s | unrealized_pnl=%+.0f | port=%d",
        mode_label, pos.ticker, unrealized, settings.ibkr_port,
    )

    try:
        from trading_platform.services.ibkr_client import close_position, close_position_legs
    except ImportError as exc:
        raise RuntimeError("ib_insync is not installed") from exc

    kwargs = dict(
        ticker=pos.ticker,
        legs=_pos_to_close_legs(pos),
        contracts=pos.contracts,
        session_id=session_id,
        host=settings.ibkr_host,
        port=settings.ibkr_port,
        client_id=settings.ibkr_client_id + 1,
        market_data_type=getattr(settings, "ibkr_market_data_type", 3),
    )

    # Mirror entry routing: a CREDIT-spread close on the paper account would re-trip the
    # riskless-combo Error 201 as a BAG and strand the position. Close those leg-by-leg
    # (market orders). Debit spreads + all live closes use the atomic BAG.
    strat = str(getattr(pos.strategy, "value", pos.strategy)).lower()
    is_single_leg = len(getattr(pos, "legs", []) or []) == 1
    is_credit = strat in _CREDIT_STRATEGIES
    # A SINGLE-LEG long option (long_call/long_put) must close via the native leg path, NOT a BAG:
    # a 1-leg BAG close re-creates the same combo that IBKR can't modify (Error 103) and strands
    # the position — exactly the failure the single-leg ENTRY routing fixed. So entries AND exits
    # now agree. Paper credit spreads also close leg-by-leg (riskless-combo 201). Debit spreads +
    # all live closes use the atomic BAG.
    close_fn = (close_position_legs
                if (is_single_leg or (settings.trading_mode == "paper" and is_credit))
                else close_position)
    if close_fn is close_position_legs:
        logger.info("Close %s (%s) → leg-by-leg native (%s)", pos.ticker, strat,
                    "single-leg long" if is_single_leg else "paper credit")

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        _IBKR_EXIT_EXECUTOR,   # C1: exits never wait behind entry work
        lambda: _run_in_new_loop(close_fn(**kwargs)),
    )

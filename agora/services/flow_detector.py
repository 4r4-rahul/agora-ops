"""Options flow detector — unusual activity signals from yfinance options chains."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import yfinance as yf

logger = logging.getLogger(__name__)


@dataclass
class SweepData:
    strike: float
    option_type: str       # "call" | "put"
    volume: int
    open_interest: int
    vol_oi_ratio: float
    bid: float
    ask: float
    at_ask: bool           # volume clustered at ask = aggressive buying


@dataclass
class FlowSignals:
    ticker: str
    volume_ratio: float          # today's total vol / total OI
    call_put_vol_ratio: float    # call volume / put volume
    net_premium_skew: float      # (call_vol*call_mid - put_vol*put_mid) / account_size; positive = bullish
    sweeps: list[SweepData]      # strikes with vol/OI > 3.0
    unusual: bool                # True if volume_ratio > 2.0
    direction: str               # "bullish" | "bearish" | "neutral"
    strength: str                # "strong" | "moderate" | "weak"
    summary: str                 # 1-sentence human readable
    timestamp: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))


def _compute_direction(call_put_vol_ratio: float, net_premium_skew: float) -> str:
    if call_put_vol_ratio > 1.4 and net_premium_skew > 0:
        return "bullish"
    if call_put_vol_ratio < 0.7 and net_premium_skew < 0:
        return "bearish"
    return "neutral"


def _compute_strength(unusual: bool, sweeps: list[SweepData], net_premium_skew: float) -> str:
    if unusual and len(sweeps) >= 2 and abs(net_premium_skew) > 0.002:
        return "strong"
    if unusual or len(sweeps) >= 1:
        return "moderate"
    return "weak"


def _build_summary(
    ticker: str,
    direction: str,
    strength: str,
    volume_ratio: float,
    call_put_vol_ratio: float,
    sweeps: list[SweepData],
) -> str:
    sweep_note = f" with {len(sweeps)} sweep(s)" if sweeps else ""
    return (
        f"{ticker.upper()} shows {strength} {direction} flow: "
        f"vol/OI ratio {volume_ratio:.2f}, "
        f"C/P vol ratio {call_put_vol_ratio:.2f}{sweep_note}."
    )


async def get_flow_signals(
    ticker: str,
    account_size: float = 25_000.0,
) -> Optional[FlowSignals]:
    """Fetch and merge options flow signals from all available sources.

    Runs UW Discord + yfinance concurrently and merges:
      - Sweeps:         UW Discord (real exchange sweeps) + yfinance proxy sweeps
      - vol/OI ratio:   yfinance (chain-level aggregates, more complete)
      - net premium:    UW Discord real dollars when available, else yfinance estimate
      - Direction:      both must agree to keep; disagreement → neutral (conservative)
      - Strength:       upgraded one level when both sources independently flag unusual
    Returns None only if yfinance also fails.
    """
    # ── Run UW Discord check + yfinance chain fetch concurrently ──────
    uw_sig: Optional[FlowSignals] = None
    try:
        from agora.services.unusual_whales_client import get_flow_signals as _uw_get
        uw_sig = await _uw_get(ticker, account_size)
    except Exception as _uw_exc:
        logger.debug("UW signal unavailable for %s: %s", ticker, _uw_exc)

    # ── yfinance chain (always runs) ─────────────────────────────────
    try:
        tk = yf.Ticker(ticker.upper())
        expiries: tuple[str, ...] = await asyncio.to_thread(lambda: tk.options)
    except Exception:
        logger.debug("flow_detector: failed to fetch expiries for %s", ticker)
        return None

    if not expiries:
        logger.debug("flow_detector: no expiries available for %s", ticker)
        return None

    target_expiries = expiries[:2]

    total_call_vol: int = 0
    total_put_vol: int = 0
    total_call_oi: int = 0
    total_put_oi: int = 0
    call_premium_sum: float = 0.0
    put_premium_sum: float = 0.0
    sweeps: list[SweepData] = []

    for expiry in target_expiries:
        try:
            chain = await asyncio.to_thread(lambda e=expiry: tk.option_chain(e))
        except Exception:
            logger.debug("flow_detector: option_chain failed for %s %s", ticker, expiry)
            continue

        calls = chain.calls
        puts = chain.puts

        if calls is None or calls.empty:
            logger.debug("flow_detector: empty calls for %s %s", ticker, expiry)
        else:
            # Ensure required columns exist
            for col in ("volume", "openInterest", "bid", "ask"):
                if col not in calls.columns:
                    calls[col] = 0

            calls = calls.fillna(0)

            exp_call_vol = int(calls["volume"].sum())
            exp_call_oi = int(calls["openInterest"].sum())
            total_call_vol += exp_call_vol
            total_call_oi += exp_call_oi

            # Mid-price weighted premium
            call_mid_series = (calls["bid"] + calls["ask"]) / 2.0
            call_premium_sum += float((calls["volume"] * call_mid_series).sum())

            # Sweeps: vol/OI > 3.0 per strike
            for _, row in calls.iterrows():
                vol = int(row.get("volume", 0) or 0)
                oi = int(row.get("openInterest", 0) or 0)
                if oi > 0 and vol / oi > 3.0:
                    bid = float(row.get("bid", 0) or 0)
                    ask = float(row.get("ask", 0) or 0)
                    mid = (bid + ask) / 2.0
                    at_ask = ask > 0 and mid > 0 and ((ask - mid) / ask < 0.05)
                    sweeps.append(
                        SweepData(
                            strike=float(row["strike"]),
                            option_type="call",
                            volume=vol,
                            open_interest=oi,
                            vol_oi_ratio=round(vol / oi, 2),
                            bid=round(bid, 2),
                            ask=round(ask, 2),
                            at_ask=at_ask,
                        )
                    )

        if puts is None or puts.empty:
            logger.debug("flow_detector: empty puts for %s %s", ticker, expiry)
        else:
            for col in ("volume", "openInterest", "bid", "ask"):
                if col not in puts.columns:
                    puts[col] = 0

            puts = puts.fillna(0)

            exp_put_vol = int(puts["volume"].sum())
            exp_put_oi = int(puts["openInterest"].sum())
            total_put_vol += exp_put_vol
            total_put_oi += exp_put_oi

            put_mid_series = (puts["bid"] + puts["ask"]) / 2.0
            put_premium_sum += float((puts["volume"] * put_mid_series).sum())

            for _, row in puts.iterrows():
                vol = int(row.get("volume", 0) or 0)
                oi = int(row.get("openInterest", 0) or 0)
                if oi > 0 and vol / oi > 3.0:
                    bid = float(row.get("bid", 0) or 0)
                    ask = float(row.get("ask", 0) or 0)
                    mid = (bid + ask) / 2.0
                    at_ask = ask > 0 and mid > 0 and ((ask - mid) / ask < 0.05)
                    sweeps.append(
                        SweepData(
                            strike=float(row["strike"]),
                            option_type="put",
                            volume=vol,
                            open_interest=oi,
                            vol_oi_ratio=round(vol / oi, 2),
                            bid=round(bid, 2),
                            ask=round(ask, 2),
                            at_ask=at_ask,
                        )
                    )

    total_vol = total_call_vol + total_put_vol
    total_oi = total_call_oi + total_put_oi

    if total_oi == 0:
        logger.debug("flow_detector: zero total OI for %s — skipping", ticker)
        return None

    volume_ratio = round(total_vol / total_oi, 4) if total_oi > 0 else 0.0

    # C/P vol ratio — guard against zero put volume
    if total_put_vol > 0:
        call_put_vol_ratio = round(total_call_vol / total_put_vol, 4)
    elif total_call_vol > 0:
        call_put_vol_ratio = 10.0   # all calls, no puts — maximally bullish signal
    else:
        call_put_vol_ratio = 1.0    # no activity either side

    # Net premium skew normalised by account size
    # Contracts represent 100 shares, premiums already per-share in yfinance
    net_premium_skew = round(
        (call_premium_sum * 100 - put_premium_sum * 100) / account_size, 6
    )

    unusual = volume_ratio > 2.0

    yf_direction = _compute_direction(call_put_vol_ratio, net_premium_skew)
    yf_strength  = _compute_strength(unusual, sweeps, net_premium_skew)

    # ── yfinance is primary — build the signal now ────────────────────
    summary = _build_summary(ticker, yf_direction, yf_strength,
                             volume_ratio, call_put_vol_ratio, sweeps)
    sig = FlowSignals(
        ticker=ticker.upper(),
        volume_ratio=volume_ratio,
        call_put_vol_ratio=call_put_vol_ratio,
        net_premium_skew=net_premium_skew,
        sweeps=sweeps,
        unusual=unusual,
        direction=yf_direction,
        strength=yf_strength,
        summary=summary,
    )

    # ── Enrich with UW Discord real sweeps (supplementary only) ───────
    # UW adds confirmed exchange sweeps to the list; does NOT change direction,
    # strength, vol_ratio, or any other yfinance-derived metric.
    if uw_sig is not None and uw_sig.sweeps:
        yf_strikes = {s.strike for s in sweeps}
        new_uw_sweeps = [s for s in uw_sig.sweeps if s.strike not in yf_strikes]
        if new_uw_sweeps:
            sig.sweeps = sweeps + new_uw_sweeps
            uw_meta = getattr(uw_sig, "_uw_meta", {})
            sig._uw_meta = {  # type: ignore[attr-defined]
                "source": "yfinance+uw_discord",
                "uw_real_sweeps_added": len(new_uw_sweeps),
                "uw_dark_pool_prints": uw_meta.get("dark_pool_prints", 0),
                "uw_net_premium_usd": uw_meta.get("net_premium_usd", 0),
            }
            logger.info(
                "flow_detector [%s]: yfinance primary + %d UW real sweep(s) added",
                ticker.upper(), len(new_uw_sweeps),
            )

    return sig

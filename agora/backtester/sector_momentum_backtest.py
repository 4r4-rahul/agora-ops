"""
Sector Momentum Backtest — validates IntradaySectorMomentumDetector signal quality.

Methodology:
  - Uses daily close-to-close returns as a proxy for intraday 30-minute moves.
    (A 3% daily close move guarantees the intraday threshold was hit; this gives a
     conservative lower-bound on signal frequency vs. the live 30-minute detector.)
  - On signal days (3+ sector peers ≥3% same direction), simulates a 7-DTE
    defined-risk directional spread on the strongest mover.
  - Exit rules mirror live session: 50% profit, 2× stop, or 7-DTE expiry.
  - IV uses per-ticker realized HV × 1.3 beta (semis trade ~30% richer than realized).

Run:
    cd /Users/rahul/Workspace/options_trading_agent_agentic
    python -m agora.backtester.sector_momentum_backtest
    python -m agora.backtester.sector_momentum_backtest --start 2023-01-01 --end 2025-05-15
    python -m agora.backtester.sector_momentum_backtest --sector semis --min-movers 3
"""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from agora.signals.sector_momentum_intraday import SECTOR_MAP

# ── Constants ──────────────────────────────────────────────────────────────────

_COMMISSION_PER_LEG = 0.65   # per contract per leg (IBKR rate)
_CONTRACTS = 1
_RISK_PER_TRADE = 500.0      # max $ risk per trade (1 contract)
_TARGET_DTE = 7              # short-dated spread matching the intraday nature of the signal
_PROFIT_TARGET = 0.50        # close at 50% of max profit
_STOP_MULTIPLIER = 2.0       # close at 2× entry credit/debit
_IV_SEMI_BETA = 1.30         # semis implied vol runs ~30% above realized (vs. SPX)
_MIN_MOVE_PCT = 3.0          # signal threshold (matches live detector)
_MIN_MOVERS = 3              # minimum peers required

# Tickers to exclude due to low liquidity (no listed options or near-zero ADTV)
_EXCLUDE_TICKERS = {"HIMX", "TSEM", "VECO", "AXTI", "AOSL", "AMSC", "FLNC", "FCEL", "BE"}

# Per-sector representative tickers for spread simulation (most liquid with listed options)
_SECTOR_REPRESENTATIVE = {
    "semis":            "MU",
    "mega_tech":        "NVDA",
    "energy_nuclear":   "CEG",
    "defense":          "PLTR",
    "commodities":      "GLD",
    "bonds":            "TLT",
    "biotech":          "LLY",
    "cloud_software":   "SNOW",
    "optical_photonics":"LITE",
    "finance":          "SCHW",
    "crypto_adjacent":  "MSTR",
    "industrial_power": "GEV",
    "mining_materials": "MP",
}


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class SignalEvent:
    signal_date:   date
    sector:        str
    direction:     str            # "bearish" | "bullish"
    movers:        list[str]      # tickers that triggered
    moves:         list[float]    # signed pct moves
    avg_move_pct:  float
    lead_ticker:   str            # strongest mover — used for spread simulation
    lead_price:    float          # closing price on signal date

    # 5-day forward tracking (underlying)
    fwd_5d_pct:    float = 0.0    # basket avg return over next 5 trading days
    fwd_10d_pct:   float = 0.0

    # Spread trade outcome
    spread_pnl:    float = 0.0    # net P&L in dollars (after commission)
    spread_status: str = "open"   # "profit_target" | "stop_loss" | "expiry" | "open"
    spread_dte_exit: int = 0


@dataclass
class SectorBacktestResult:
    sector:       str
    start_date:   date
    end_date:     date
    signals:      list[SignalEvent] = field(default_factory=list)

    @property
    def n_signals(self) -> int:
        return len(self.signals)

    @property
    def n_bearish(self) -> int:
        return sum(1 for s in self.signals if s.direction == "bearish")

    @property
    def n_bullish(self) -> int:
        return sum(1 for s in self.signals if s.direction == "bullish")

    @property
    def avg_bearish_fwd5(self) -> float:
        bearish = [s for s in self.signals if s.direction == "bearish"]
        return sum(s.fwd_5d_pct for s in bearish) / len(bearish) if bearish else 0.0

    @property
    def avg_bullish_fwd5(self) -> float:
        bullish = [s for s in self.signals if s.direction == "bullish"]
        return sum(s.fwd_5d_pct for s in bullish) / len(bullish) if bullish else 0.0

    @property
    def spread_win_rate(self) -> float:
        closed = [s for s in self.signals if s.spread_status != "open"]
        wins = [s for s in closed if s.spread_pnl > 0]
        return len(wins) / len(closed) if closed else 0.0

    @property
    def spread_total_pnl(self) -> float:
        return sum(s.spread_pnl for s in self.signals if s.spread_status != "open")

    @property
    def spread_avg_pnl(self) -> float:
        closed = [s for s in self.signals if s.spread_status != "open"]
        return sum(s.spread_pnl for s in closed) / len(closed) if closed else 0.0


# ── Black-Scholes helpers ──────────────────────────────────────────────────────

def _norm_cdf(x: float) -> float:
    """Approximation of Φ(x) — Abramowitz & Stegun 26.2.17."""
    a = [0.319381530, -0.356563782, 1.781477937, -1.821255978, 1.330274429]
    t = 1.0 / (1.0 + 0.2316419 * abs(x))
    poly = sum(a[i] * t ** (i + 1) for i in range(5))
    val = 1.0 - (1.0 / math.sqrt(2 * math.pi)) * math.exp(-0.5 * x * x) * poly
    return val if x >= 0 else 1.0 - val


def _bs(S: float, K: float, T: float, sigma: float, opt: str) -> float:
    """Black-Scholes price for call or put. r=0 (short-dated)."""
    if T <= 0 or sigma <= 0:
        if opt == "call":
            return max(0.0, S - K)
        return max(0.0, K - S)
    d1 = (math.log(S / K) + 0.5 * sigma ** 2 * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if opt == "call":
        return S * _norm_cdf(d1) - K * _norm_cdf(d2)
    return K * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def _bs_delta(S: float, K: float, T: float, sigma: float, opt: str) -> float:
    if T <= 0 or sigma <= 0:
        return (1.0 if S > K else 0.0) if opt == "call" else (-1.0 if S < K else 0.0)
    d1 = (math.log(S / K) + 0.5 * sigma ** 2 * T) / (sigma * math.sqrt(T))
    if opt == "call":
        return _norm_cdf(d1)
    return _norm_cdf(d1) - 1.0


def _strike_for_delta(S: float, T: float, sigma: float, target_delta: float, opt: str) -> float:
    """Bisect to find strike with given |delta|."""
    lo, hi = S * 0.50, S * 1.50
    for _ in range(50):
        mid = (lo + hi) / 2
        d = abs(_bs_delta(S, mid, T, sigma, opt))
        if abs(d - target_delta) < 1e-6:
            return mid
        if d > target_delta:
            if opt == "call":
                lo = mid
            else:
                hi = mid
        else:
            if opt == "call":
                hi = mid
            else:
                lo = mid
    return (lo + hi) / 2


def _simulate_spread(
    S: float,
    sigma: float,
    direction: str,
    dte: int = 7,
) -> dict:
    """
    Simulate a defined-risk directional spread:
      - Bearish → BEAR_CALL_SPREAD: sell 0.30-delta call, buy 0.40-delta call
      - Bullish → BULL_PUT_SPREAD:  sell 0.30-delta put,  buy 0.40-delta put

    Returns entry credit, max_loss, max_gain, short/long strikes.
    """
    T = dte / 252.0
    # Apply 5% slip on each leg price (wider bid-ask for single-name options)
    slip = 0.05

    if direction == "bearish":
        short_k = _strike_for_delta(S, T, sigma, 0.30, "call")
        long_k  = _strike_for_delta(S, T, sigma, 0.40, "call")
        short_p = _bs(S, short_k, T, sigma, "call")
        long_p  = _bs(S, long_k,  T, sigma, "call")
        # Credit spread: sell short_k, buy long_k (long_k > short_k for calls)
        if long_k < short_k:
            long_k, short_k = short_k, long_k
            long_p, short_p = short_p, long_p
        credit = (short_p * (1 - slip)) - (long_p * (1 + slip))
        width = long_k - short_k
        max_loss   = max(0.0, width - credit) * 100
        max_gain   = credit * 100
        return {
            "strategy": "bear_call_spread",
            "short_strike": short_k, "long_strike": long_k,
            "credit": credit, "max_loss": max_loss, "max_gain": max_gain,
        }
    else:
        short_k = _strike_for_delta(S, T, sigma, 0.30, "put")
        long_k  = _strike_for_delta(S, T, sigma, 0.40, "put")
        short_p = _bs(S, short_k, T, sigma, "put")
        long_p  = _bs(S, long_k,  T, sigma, "put")
        # Credit spread: sell short_k (higher), buy long_k (lower)
        if long_k > short_k:
            long_k, short_k = short_k, long_k
            long_p, short_p = short_p, long_p
        credit = (short_p * (1 - slip)) - (long_p * (1 + slip))
        width = short_k - long_k
        max_loss   = max(0.0, width - credit) * 100
        max_gain   = credit * 100
        return {
            "strategy": "bull_put_spread",
            "short_strike": short_k, "long_strike": long_k,
            "credit": credit, "max_loss": max_loss, "max_gain": max_gain,
        }


def _mark_spread(
    S: float, T: float, sigma: float,
    short_k: float, long_k: float,
    opt: str,
) -> float:
    """Current spread value (what it costs to close)."""
    short_val = _bs(S, short_k, T, sigma, opt)
    long_val  = _bs(S, long_k,  T, sigma, opt)
    return abs(short_val - long_val)


def _compute_hv(closes: list[float], window: int) -> float:
    """Annualized realized vol from last `window` daily returns."""
    if len(closes) < window + 1:
        return 0.30   # fallback for sparse data
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(-window, 0) if closes[i - 1] > 0]
    if len(rets) < 5:
        return 0.30
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / max(len(rets) - 1, 1)
    return math.sqrt(var) * math.sqrt(252)


# ── Data fetching ──────────────────────────────────────────────────────────────

async def _download_all(tickers: list[str], start: date, end: date) -> dict[str, list[dict]]:
    """Batch download OHLCV for all tickers via yfinance."""
    import yfinance as yf

    # Warm-up: 1 year before start for HV calculation
    warmup = start - timedelta(days=365)
    loop = asyncio.get_event_loop()

    def _dl():
        df = yf.download(
            tickers,
            start=warmup.isoformat(),
            end=(end + timedelta(days=1)).isoformat(),
            progress=False,
            auto_adjust=True,
            group_by="ticker",
        )
        return df

    df = await loop.run_in_executor(None, _dl)

    result: dict[str, list[dict]] = {}
    for ticker in tickers:
        try:
            if len(tickers) == 1:
                tdf = df
            else:
                tdf = df[ticker] if ticker in df.columns.get_level_values(0) else None
            if tdf is None or tdf.empty:
                result[ticker] = []
                continue
            bars = []
            for ts, row in tdf.iterrows():
                c = row.get("Close", None)
                if c is None:
                    continue
                close = float(c) if not hasattr(c, "__len__") else float(c.iloc[0])
                if close <= 0 or math.isnan(close):
                    continue
                bars.append({
                    "date":  ts.date() if hasattr(ts, "date") else ts,
                    "close": close,
                })
            result[ticker] = bars
        except Exception as e:
            print(f"  [warn] {ticker}: {e}")
            result[ticker] = []
    return result


def _get_close(bars: list[dict], d: date) -> Optional[float]:
    for b in bars:
        if b["date"] == d:
            return b["close"]
    return None


def _closes_before(bars: list[dict], d: date, n: int) -> list[float]:
    """Last n closes strictly before date d."""
    past = [b["close"] for b in bars if b["date"] < d]
    return past[-n:] if len(past) >= n else past


def _closes_after(bars: list[dict], d: date, n: int) -> list[float]:
    """First n closes strictly after date d."""
    after = [b["close"] for b in bars if b["date"] > d]
    return after[:n]


# ── Core simulation ────────────────────────────────────────────────────────────

def _run_sector(
    sector: str,
    tickers: list[str],
    all_data: dict[str, list[dict]],
    start: date,
    end: date,
    min_movers: int,
    target_dte: int,
) -> SectorBacktestResult:
    """Detect signals and simulate trades for one sector."""
    result = SectorBacktestResult(sector=sector, start_date=start, end_date=end)

    # Build union of trading days in range
    all_days: set[date] = set()
    for t in tickers:
        for b in all_data.get(t, []):
            if start <= b["date"] <= end:
                all_days.add(b["date"])
    trading_days = sorted(all_days)

    # Cooldown per sector: one signal per 14-day window (same spirit as live single-session limit)
    last_signal_date: date | None = None
    _COOLDOWN = 14

    for today in trading_days:
        if last_signal_date and (today - last_signal_date).days < _COOLDOWN:
            continue

        bearish: list[tuple[str, float]] = []
        bullish: list[tuple[str, float]] = []

        for t in tickers:
            bars = all_data.get(t, [])
            today_close = _get_close(bars, today)
            if today_close is None:
                continue
            prev_closes = _closes_before(bars, today, 1)
            if not prev_closes:
                continue
            prev_close = prev_closes[-1]
            if prev_close <= 0:
                continue
            pct = (today_close - prev_close) / prev_close * 100
            if pct <= -_MIN_MOVE_PCT:
                bearish.append((t, pct))
            elif pct >= _MIN_MOVE_PCT:
                bullish.append((t, pct))

        for group, direction in [(bearish, "bearish"), (bullish, "bullish")]:
            if len(group) < min_movers:
                continue

            movers = [t for t, _ in group]
            moves  = [p for _, p in group]
            avg    = sum(moves) / len(moves)

            # Lead ticker: strongest mover (largest abs pct), preferring liquid options
            liquid = [t for t, p in group if t not in _EXCLUDE_TICKERS]
            if liquid:
                lead = max(liquid, key=lambda t: abs(dict(group)[t]))
            else:
                lead = max(movers, key=lambda t: abs(dict(group)[t]))

            lead_bars  = all_data.get(lead, [])
            lead_price = _get_close(lead_bars, today)
            if lead_price is None or lead_price <= 0:
                continue

            # 5-day and 10-day forward return (basket average)
            fwd_5 = _basket_fwd_return(movers, all_data, today, 5)
            fwd_10 = _basket_fwd_return(movers, all_data, today, 10)

            # Spread simulation
            hv = _compute_hv(_closes_before(lead_bars, today, 22), 21)
            sigma = max(hv * _IV_SEMI_BETA, 0.05)
            spread = _simulate_spread(lead_price, sigma, direction, dte=target_dte)

            # Simulate daily lifecycle for spread
            commission = 2 * _CONTRACTS * _COMMISSION_PER_LEG * 2  # 2 legs × entry+exit
            pnl, status, dte_exit = _lifecycle_spread(
                spread=spread,
                lead_bars=lead_bars,
                entry_date=today,
                sigma=sigma,
                target_dte=target_dte,
                all_data=all_data,
                lead=lead,
                direction=direction,
            )
            pnl -= commission

            sig = SignalEvent(
                signal_date=today,
                sector=sector,
                direction=direction,
                movers=movers,
                moves=moves,
                avg_move_pct=avg,
                lead_ticker=lead,
                lead_price=lead_price,
                fwd_5d_pct=fwd_5,
                fwd_10d_pct=fwd_10,
                spread_pnl=pnl,
                spread_status=status,
                spread_dte_exit=dte_exit,
            )
            result.signals.append(sig)
            last_signal_date = today
            break   # one signal per day per sector (bearish takes priority)

    return result


def _basket_fwd_return(tickers: list[str], all_data: dict, entry: date, days: int) -> float:
    """Average close-to-close return of ticker basket over next `days` trading days."""
    returns = []
    for t in tickers:
        bars = all_data.get(t, [])
        entry_close = _get_close(bars, entry)
        if entry_close is None or entry_close <= 0:
            continue
        future = _closes_after(bars, entry, days)
        if not future:
            continue
        ret = (future[-1] - entry_close) / entry_close * 100
        returns.append(ret)
    return sum(returns) / len(returns) if returns else 0.0


def _lifecycle_spread(
    spread: dict,
    lead_bars: list[dict],
    entry_date: date,
    sigma: float,
    target_dte: int,
    all_data: dict,
    lead: str,
    direction: str,
) -> tuple[float, str, int]:
    """
    Simulate spread P&L over next target_dte trading days.
    Returns (raw_pnl_dollars_before_commission, status, dte_at_exit).
    """
    opt = "call" if direction == "bearish" else "put"
    entry_credit = spread["credit"]
    short_k = spread["short_strike"]
    long_k  = spread["long_strike"]
    max_gain = spread["max_gain"]
    max_loss = spread["max_loss"]

    profit_target = max_gain * _PROFIT_TARGET   # close at 50% of max gain
    stop_trigger  = entry_credit * _STOP_MULTIPLIER * 100   # 2× entry credit → stop

    future_closes = _closes_after(lead_bars, entry_date, target_dte + 2)

    for day_n, close in enumerate(future_closes, 1):
        dte_remaining = max(target_dte - day_n, 0)
        T = dte_remaining / 252.0

        current_val = _mark_spread(close, T, sigma, short_k, long_k, opt)
        pnl_per_share = entry_credit - current_val
        pnl_dollars = pnl_per_share * 100 * _CONTRACTS

        # 50% profit target
        if pnl_dollars >= profit_target:
            return pnl_dollars, "profit_target", day_n

        # 2× stop loss
        cost_to_close = current_val * 100
        if cost_to_close >= stop_trigger:
            return pnl_dollars, "stop_loss", day_n

        # DTE expiry
        if dte_remaining == 0:
            # Intrinsic at expiry
            if direction == "bearish":
                intrinsic = (
                    max(0.0, close - short_k) - max(0.0, close - long_k)
                ) * 100
                pnl = (entry_credit * 100 - intrinsic)
            else:
                intrinsic = (
                    max(0.0, short_k - close) - max(0.0, long_k - close)
                ) * 100
                pnl = (entry_credit * 100 - intrinsic)
            return pnl, "expiry", day_n

    # No data — return mark at last available price
    if future_closes:
        last = future_closes[-1]
        T = 0.0
        current_val = _mark_spread(last, T, sigma, short_k, long_k, opt)
        pnl = (entry_credit - current_val) * 100 * _CONTRACTS
        return pnl, "expiry", len(future_closes)
    return 0.0, "open", 0


# ── Reporting ──────────────────────────────────────────────────────────────────

def _print_report(results: list[SectorBacktestResult], start: date, end: date) -> None:
    n_years = max((end - start).days / 365.25, 0.1)

    print(f"\n{'='*90}")
    print(f"  SECTOR MOMENTUM BACKTEST  |  {start} → {end}  ({n_years:.1f} years)")
    print(f"  Signal: ≥{_MIN_MOVERS} sector peers moving ≥{_MIN_MOVE_PCT:.0f}% same direction (daily close proxy)")
    print(f"  Trade:  {_TARGET_DTE}-DTE credit spread on strongest liquid mover")
    print(f"  Exits:  50% profit target | 2× stop | {_TARGET_DTE}-DTE expiry")
    print(f"{'='*90}")

    total_signals = sum(r.n_signals for r in results)
    total_wins    = sum(
        sum(1 for s in r.signals if s.spread_pnl > 0 and s.spread_status != "open")
        for r in results
    )
    total_closed  = sum(
        sum(1 for s in r.signals if s.spread_status != "open")
        for r in results
    )
    total_pnl = sum(r.spread_total_pnl for r in results)

    print(f"\n  Total signals detected:  {total_signals:>5}  ({total_signals / n_years:.1f}/year)")
    print(f"  Closed trades:           {total_closed:>5}")
    print(f"  Win rate (all sectors):  {total_wins/max(total_closed,1):.1%}")
    print(f"  Total spread P&L:        ${total_pnl:>+,.0f}")
    print(f"  Avg P&L per signal:      ${total_pnl/max(total_closed,1):>+,.0f}")

    print(f"\n{'─'*90}")
    print(f"  {'Sector':<22} {'Sigs':>5} {'Bear':>5} {'Bull':>5} "
          f"{'AvgBearFwd5':>12} {'AvgBullFwd5':>12} {'WinRate':>8} {'TotalPnL':>10}")
    print(f"  {'─'*22} {'─'*5} {'─'*5} {'─'*5} "
          f"{'─'*12} {'─'*12} {'─'*8} {'─'*10}")

    for r in sorted(results, key=lambda x: -x.n_signals):
        if r.n_signals == 0:
            continue
        print(
            f"  {r.sector:<22} {r.n_signals:>5} {r.n_bearish:>5} {r.n_bullish:>5} "
            f"  {r.avg_bearish_fwd5:>+10.2f}%  {r.avg_bullish_fwd5:>+10.2f}% "
            f"  {r.spread_win_rate:>6.1%}  ${r.spread_total_pnl:>+9,.0f}"
        )

    print(f"\n{'─'*90}")
    print(f"  SIGNAL EVENT LOG (most recent 30):")
    print(f"  {'Date':<12} {'Sector':<18} {'Dir':>8} {'Movers':>7} {'AvgMv':>7} "
          f"{'Fwd5':>7} {'Status':<16} {'P&L':>8}")
    print(f"  {'─'*12} {'─'*18} {'─'*8} {'─'*7} {'─'*7} {'─'*7} {'─'*16} {'─'*8}")

    all_signals = sorted(
        [s for r in results for s in r.signals],
        key=lambda x: x.signal_date
    )
    for sig in all_signals[-30:]:
        pnl_str = f"${sig.spread_pnl:>+,.0f}" if sig.spread_status != "open" else "open"
        print(
            f"  {sig.signal_date!s:<12} {sig.sector:<18} {sig.direction:>8} "
            f"  {len(sig.movers):>5}  {sig.avg_move_pct:>+6.1f}%  {sig.fwd_5d_pct:>+6.1f}% "
            f"  {sig.spread_status:<16} {pnl_str:>8}"
        )

    # Direction alignment check
    print(f"\n{'─'*90}")
    print(f"  PREDICTIVE VALUE: does spread direction align with 5-day forward move?")
    bear_correct = sum(
        1 for r in results for s in r.signals
        if s.direction == "bearish" and s.fwd_5d_pct < 0
    )
    bull_correct = sum(
        1 for r in results for s in r.signals
        if s.direction == "bullish" and s.fwd_5d_pct > 0
    )
    bear_total = sum(1 for r in results for s in r.signals if s.direction == "bearish")
    bull_total = sum(1 for r in results for s in r.signals if s.direction == "bullish")

    print(f"  Bearish signals correct (basket fell next 5d): {bear_correct}/{bear_total} "
          f"= {bear_correct/max(bear_total,1):.1%}")
    print(f"  Bullish signals correct (basket rose next 5d): {bull_correct}/{bull_total} "
          f"= {bull_correct/max(bull_total,1):.1%}")
    print(f"{'='*90}\n")


# ── CLI entry ──────────────────────────────────────────────────────────────────

async def main() -> None:
    parser = argparse.ArgumentParser(description="Sector Momentum Pillar Backtest")
    parser.add_argument("--start",      default="2023-01-01",  help="Start date YYYY-MM-DD")
    parser.add_argument("--end",        default="2025-05-15",  help="End date YYYY-MM-DD")
    parser.add_argument("--sector",     default="all",         help="Sector name or 'all'")
    parser.add_argument("--min-movers", type=int, default=3,   help="Minimum tickers needed")
    parser.add_argument("--dte",        type=int, default=7,   help="Days to expiry for spread")
    args = parser.parse_args()

    global _MIN_MOVERS, _TARGET_DTE
    _MIN_MOVERS  = args.min_movers
    _TARGET_DTE  = args.dte

    start = date.fromisoformat(args.start)
    end   = date.fromisoformat(args.end)

    # Build ticker list for download
    if args.sector == "all":
        sectors_to_run = list(SECTOR_MAP.items())
    else:
        if args.sector not in SECTOR_MAP:
            print(f"Unknown sector '{args.sector}'. Available: {list(SECTOR_MAP.keys())}")
            sys.exit(1)
        sectors_to_run = [(args.sector, SECTOR_MAP[args.sector])]

    all_tickers: list[str] = sorted({
        t for _, tickers in sectors_to_run for t in tickers
        if t not in _EXCLUDE_TICKERS
    })
    print(f"\n  Downloading {len(all_tickers)} tickers: {args.start} → {args.end} ...")

    all_data = await _download_all(all_tickers, start, end)

    actual_counts = {t: sum(1 for b in all_data[t] if start <= b["date"] <= end) for t in all_tickers}
    print(f"  Download complete. Tickers with ≥100 bars: "
          f"{sum(1 for v in actual_counts.values() if v >= 100)}/{len(all_tickers)}")

    results: list[SectorBacktestResult] = []
    for sector, raw_tickers in sectors_to_run:
        tickers = [t for t in raw_tickers if t not in _EXCLUDE_TICKERS and actual_counts.get(t, 0) > 20]
        if len(tickers) < _MIN_MOVERS:
            print(f"  [skip] {sector}: only {len(tickers)} usable tickers (need {_MIN_MOVERS})")
            continue
        r = _run_sector(sector, tickers, all_data, start, end, _MIN_MOVERS, _TARGET_DTE)
        results.append(r)
        print(f"  {sector:<22}: {r.n_signals:>3} signals  ({r.n_bearish}B/{r.n_bullish}U)  "
              f"WR={r.spread_win_rate:.0%}  P&L=${r.spread_total_pnl:>+,.0f}")

    _print_report(results, start, end)


if __name__ == "__main__":
    asyncio.run(main())

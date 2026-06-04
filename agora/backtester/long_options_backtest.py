"""
agora/backtester/long_options_backtest.py — Edge Research Engine v1.

A faithful, lookahead-free backtest of the LONG-OPTIONS deterministic core, built
to answer one question with evidence: does the strategy have an edge BEFORE any LLM
touches it — and which signals actually predict?

What it replicates faithfully (from agora/agents/long_options_agent.py + the exit
sweep in agora/session.py):
  • Signal scoring (_score_direction) for the PRICE/VOLUME-derived signals.
  • Conviction gate, RSI-extreme block, IVR gate (proxied), conviction-delta strike.
  • 4-factor DTE selection (_select_dte), clamped [14, 30].
  • Quality-based sizing + per-trade dollar cap.
  • The exact exit ladder: 5-day (calendar) time stop, conviction-dynamic profit
    target, +30%/15% trailing stop, -50% flat stop, 3-day urgency taper.

What it CANNOT replicate (no historical data) — stated honestly:
  • flow sweeps, GEX regime, news flags, macro stance → EXCLUDED. So this tests the
    strategy MINUS its highest-weighted signal (flow). If the core has edge here,
    flow is upside; if it does not, the entire edge rests on un-backtestable signals.
  • Real option IV → proxied as HV20 × (1 + variance-risk-premium). Vega P&L is
    held out by marking at constant IV per trade (isolates delta/theta/gamma — the
    dominant P&L for a directional swing). Stated as a modeling assumption, not truth.

Pricing reuses agora/backtester/synthetic_pricing (bs_price / strike_for_delta).

Run:  python -m agora.backtester.long_options_backtest
"""

from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass, field, asdict
from datetime import date, timedelta
from typing import Any

from agora.backtester.synthetic_pricing import bs_price, strike_for_delta

# Realistic near-ATM OPTION half-spreads (fraction of mid). The shared
# synthetic_pricing default (4%) is ETF-illiquid pricing; these single-name
# mega-cap options are among the most liquid in the market. Source: typical
# penny/nickel-wide near-ATM monthly quotes. Conservative (slightly wide) end.
_HALF_SPREAD = {
    "SPY": 0.005, "QQQ": 0.005, "IWM": 0.008,
    "AAPL": 0.010, "MSFT": 0.012, "NVDA": 0.010, "AMZN": 0.012,
    "META": 0.012, "GOOGL": 0.012, "TSLA": 0.010, "AMD": 0.012,
    "PLTR": 0.015, "AVGO": 0.015,
}
_DEFAULT_HALF_SPREAD = 0.015


def entry_slippage(ticker: str) -> float:
    return _HALF_SPREAD.get(ticker, _DEFAULT_HALF_SPREAD)

# ── Config (mirrors the live long_options_* defaults / .env v2 values) ──────────
UNIVERSE = ["SPY", "QQQ", "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL",
            "TSLA", "AMD", "PLTR", "AVGO"]
START = "2023-01-01"
END   = "2026-05-31"

ACCOUNT_SIZE     = 10_000.0
TARGET_DELTA     = 0.42        # .env LONG_OPTIONS_TARGET_DELTA (v2)
IVR_CAP          = 45.0        # long_options_ivr_cap  (proxied by HV20 rank)
MIN_PREMIUM      = 50.0        # long_options_min_premium ($/contract)
MAX_PREMIUM_PCT  = 0.15        # long_options_max_premium_pct
MAX_HOLD_DAYS    = 5           # calendar days (hard time stop)
TRAIL_TRIGGER    = 0.30
TRAIL_FLOOR      = 0.15
STOP_LOSS_PCT    = 0.50
RSI_OB           = 72
RSI_OS           = 28
MIN_CONVICTION   = 2
MAX_CONTRACTS    = 3
R_FREE           = 0.05
VRP              = 0.15        # variance-risk-premium markup: IV ≈ HV20 × 1.15
COMMISSION       = 0.65        # per contract per leg
IV_CRUSH         = 0.0         # fractional IV decay over the hold (mean-reversion stress test):
                              # sigma_t = sigma_entry × (1 - IV_CRUSH × min(age/10, 1)).
                              # Set >0 to model IV reverting down after an oversold-fear entry.

# Beta map (from _select_dte._BETA_MAP) for DTE beta-compression
_BETA = {"TSLA": 2.1, "NVDA": 1.9, "AMD": 1.8, "MSTR": 3.5, "PLTR": 2.0,
         "META": 1.4, "AAPL": 1.2, "MSFT": 1.1, "SPY": 1.0, "QQQ": 1.1,
         "IWM": 1.2, "AMZN": 1.3, "GOOGL": 1.2, "AVGO": 1.5}


# ── Indicators (lookahead-free: all use closes strictly up to index i) ──────────
def _rsi(closes: list[float], i: int, period: int = 14) -> float:
    """Wilder RSI at index i using closes[:i+1]."""
    if i < period:
        return 50.0
    gains, losses = 0.0, 0.0
    for k in range(i - period + 1, i + 1):
        ch = closes[k] - closes[k - 1]
        gains += max(ch, 0.0)
        losses += max(-ch, 0.0)
    avg_g, avg_l = gains / period, losses / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100.0 - 100.0 / (1.0 + rs)


def _sma(closes: list[float], i: int, n: int) -> float:
    if i + 1 < n:
        return statistics.fmean(closes[: i + 1])
    return statistics.fmean(closes[i - n + 1: i + 1])


def _hv(closes: list[float], i: int, window: int) -> float:
    """Annualized realized vol from log returns ending at index i."""
    if i < window:
        return 0.0
    rets = [math.log(closes[k] / closes[k - 1])
            for k in range(i - window + 1, i + 1) if closes[k - 1] > 0]
    if len(rets) < 2:
        return 0.0
    return statistics.pstdev(rets) * math.sqrt(252)


def _adx(highs: list[float], lows: list[float], closes: list[float], i: int, period: int = 14) -> float:
    """Wilder ADX at index i (trend-strength: >25 trending, <20 ranging). Lookahead-free."""
    if i < period * 2:
        return 0.0
    trs, plus_dm, minus_dm = [], [], []
    for k in range(i - period * 2 + 1, i + 1):
        up = highs[k] - highs[k - 1]
        dn = lows[k - 1] - lows[k]
        plus_dm.append(up if (up > dn and up > 0) else 0.0)
        minus_dm.append(dn if (dn > up and dn > 0) else 0.0)
        trs.append(max(highs[k] - lows[k], abs(highs[k] - closes[k - 1]), abs(lows[k] - closes[k - 1])))
    # Wilder-smoothed DI over the last `period`
    atr = sum(trs[-period:]) / period
    if atr <= 0:
        return 0.0
    pdi = 100 * (sum(plus_dm[-period:]) / period) / atr
    mdi = 100 * (sum(minus_dm[-period:]) / period) / atr
    dx = 100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) > 0 else 0.0
    return dx  # single-period DX ~ ADX proxy (sufficient for regime bucketing)


def _hv_rank(hv_series: list[float], i: int, lookback: int = 252) -> float:
    """Percentile rank of current HV vs its trailing `lookback` — IVR proxy (0-100)."""
    lo = max(0, i - lookback)
    window = [h for h in hv_series[lo: i + 1] if h > 0]
    if len(window) < 20 or hv_series[i] <= 0:
        return 50.0
    below = sum(1 for h in window if h < hv_series[i])
    return 100.0 * below / len(window)


# ── Strategy replicas (exact, from the live code) ───────────────────────────────
def _conviction_delta(conviction: int, base: float) -> float:
    if conviction >= 5:
        return min(0.50, base + 0.10)
    if conviction >= 4:
        return min(0.45, base + 0.05)
    if conviction >= 3:
        return base
    return max(0.22, base - 0.07)


def _conviction_profit_target(conviction: int) -> float:
    if conviction >= 5:
        return 1.00
    if conviction == 4:
        return 0.75
    if conviction == 3:
        return 0.50
    return 0.40


def _select_dte(ticker: str, ivr: float, hold_days: int, hv5: float, hv20: float) -> int:
    if ivr < 20:
        base = 60
    elif ivr < 35:
        base = 45
    elif ivr < 50:
        base = 30
    else:
        base = 21
    beta = _BETA.get(ticker, 1.0)
    if beta >= 2.5:
        base = int(base * 0.70)
    elif beta >= 1.8:
        base = int(base * 0.80)
    elif beta >= 1.3:
        base = int(base * 0.90)
    hold_floor = hold_days * 3
    if base < hold_floor:
        base = hold_floor
    if hv5 > 0 and hv20 > 0 and hv5 > 1.5 * hv20:
        base = int(base * 0.75)
    return max(14, min(30, base))


def _score(rsi: float, above20: bool, above50: bool, ret10: float, vol_surge: bool):
    """Backtestable subset of _score_direction. Returns (direction, conviction, quality, stack)."""
    bull = bear = 0
    qb = qbear = 0.0
    stack = {}
    # momentum
    if rsi > 55 and above20 and above50:
        bull += 1; qb += 0.8; stack["momentum"] = "bull"
    elif rsi < 45 and not above20 and not above50:
        bear += 1; qbear += 0.8; stack["momentum"] = "bear"
    # relative strength
    if ret10 > 0.03:
        bull += 1; qb += 1.0; stack["rel_strength"] = "bull"
    elif ret10 < -0.03:
        bear += 1; qbear += 1.0; stack["rel_strength"] = "bear"
    # volume surge → dominant side only
    if vol_surge:
        if bull > bear:
            bull += 1; qb += 0.7; stack["vol_surge"] = "bull"
        elif bear > bull:
            bear += 1; qbear += 0.7; stack["vol_surge"] = "bear"
    if bull >= MIN_CONVICTION and bull > bear:
        if rsi > RSI_OB:
            return None, bull, qb, stack  # RSI block CALL
        return "bull", bull, qb, stack
    if bear >= MIN_CONVICTION and bear > bull:
        if rsi < RSI_OS:
            return None, bear, qbear, stack  # RSI block PUT
        return "bear", bear, qbear, stack
    return None, max(bull, bear), max(qb, qbear), stack


def _contracts(quality: float, premium_per_contract: float) -> int:
    if quality >= 3.5:
        c = MAX_CONTRACTS
    elif quality >= 2.0:
        c = 2
    else:
        c = 1
    cap = ACCOUNT_SIZE * MAX_PREMIUM_PCT
    if premium_per_contract > 0:
        aff = int(cap // premium_per_contract)
        if aff < 1:
            return 0
        c = min(c, aff)
    return c


@dataclass
class Trade:
    ticker: str
    direction: str
    entry_date: str
    exit_date: str
    conviction: int
    quality: float
    dte: int
    entry_spot: float
    strike: float
    entry_premium: float    # per share, after slippage (what we paid)
    contracts: int
    exit_reason: str
    pnl_pct: float          # per-contract %, net of round-trip slippage
    pnl_dollars: float      # net of commission, sized
    hold_days: int
    signals: dict = field(default_factory=dict)
    regime: str = "all"     # market regime at entry (set by regime-aware runner)


def simulate_ticker(ticker: str, bars: list[dict]) -> tuple[list[Trade], list[dict]]:
    """Returns (trades, daily_signal_rows) for one ticker — default momentum signal. No lookahead."""
    closes = [b["close"] for b in bars]
    n = len(bars)
    sig_rows: list[dict] = []
    for i in range(60, n):
        if i + 5 < n:
            S = closes[i]; rsi = _rsi(closes, i)
            above20 = S > _sma(closes, i, 20); above50 = S > _sma(closes, i, 50)
            ret10 = (closes[i] / closes[i - 10] - 1.0) if closes[i - 10] > 0 else 0.0
            avg = statistics.fmean([b["volume"] for b in bars][i - 20:i]) if i >= 20 else 0
            vs = avg > 0 and bars[i]["volume"] > 2 * avg
            fwd5 = closes[i + 5] / S - 1.0
            mom = 1 if (rsi > 55 and above20 and above50) else (-1 if (rsi < 45 and not above20 and not above50) else 0)
            rs = 1 if ret10 > 0.03 else (-1 if ret10 < -0.03 else 0)
            sig_rows.append({"mom": mom, "rs": rs, "vol": 1 if vs else 0, "conv_net": (mom + rs), "fwd5": fwd5})

    def _default_signal(f):
        return _score(f["rsi"], f["above20"], f["above50"], f["ret10"], f["vol_surge"])
    return run_strategy(ticker, bars, _default_signal), sig_rows


def _features(closes, vols, highs, lows, hv20_series, i):
    """All indicators at bar i (lookahead-free) — the feature vector strategies/regimes read."""
    S = closes[i]
    avg20 = statistics.fmean(vols[i - 20:i]) if i >= 20 else 0
    return {
        "S": S, "i": i,
        "rsi": _rsi(closes, i),
        "above20": S > _sma(closes, i, 20),
        "above50": S > _sma(closes, i, 50),
        "above200": S > _sma(closes, i, 200),
        "ret5":  (closes[i] / closes[i - 5] - 1.0)  if closes[i - 5] > 0 else 0.0,
        "ret10": (closes[i] / closes[i - 10] - 1.0) if closes[i - 10] > 0 else 0.0,
        "ret20": (closes[i] / closes[i - 20] - 1.0) if closes[i - 20] > 0 else 0.0,
        "vol_surge": avg20 > 0 and vols[i] > 2 * avg20,
        "adx": _adx(highs, lows, closes, i),
        "hv20": hv20_series[i],
        "ivr": _hv_rank(hv20_series, i),
        "high20": max(closes[i - 20:i]) if i >= 20 else S,   # prior 20-day high (breakout)
        "low20":  min(closes[i - 20:i]) if i >= 20 else S,
    }


def _simulate_position(ticker, closes, dates, i, n, direction, conviction, quality,
                       ivr, hv20, hv5, stack, regime="all"):
    """Run one option entry + the exact exit ladder from bar i. Returns (Trade|None, exit_idx)."""
    S = closes[i]
    dte = _select_dte(ticker, ivr, MAX_HOLD_DAYS, hv5, hv20)
    sigma = hv20 * (1 + VRP)
    opt = "call" if direction == "bull" else "put"
    tdelta = _conviction_delta(conviction, TARGET_DELTA)
    T = dte / 365.0     # calendar-day convention (see note: avoids 1.448x theta over-decay)
    K = strike_for_delta(S, T, sigma, tdelta, opt, R_FREE)
    if opt == "call" and K <= S * 1.001:
        K = round(S * 1.01, 2)
    if opt == "put" and K >= S * 0.999:
        K = round(S * 0.99, 2)
    mid = bs_price(S, K, T, R_FREE, sigma, opt)
    if mid <= 0:
        return None, i
    slip = entry_slippage(ticker)
    pay = mid * (1 + slip)
    if pay * 100 < MIN_PREMIUM:
        return None, i
    contracts = _contracts(quality, pay * 100)
    if contracts < 1:
        return None, i

    entry_dt = dates[i]
    target_close_dt = entry_dt + timedelta(days=MAX_HOLD_DAYS)
    profit_tgt = _conviction_profit_target(conviction)
    peak = 0.0; exit_reason = "expiry"; exit_idx = i; pnl_pct = 0.0; cur_pct = 0.0
    j = i + 1
    while j < n:
        Sd = closes[j]
        age = (dates[j] - entry_dt).days
        dte_rem = dte - age
        if dte_rem <= 0:
            val = max(0.0, Sd - K) if opt == "call" else max(0.0, K - Sd)
        else:
            sig_t = sigma * (1 - IV_CRUSH * min(age / 10.0, 1.0))
            val = bs_price(Sd, K, dte_rem / 365.0, R_FREE, sig_t, opt)
        exit_val = val * (1 - slip)
        cur_pct = exit_val / pay - 1.0
        peak = max(peak, cur_pct)
        urgent = (target_close_dt - dates[j]).days <= 3
        eff_tgt = profit_tgt * 0.5 if urgent else profit_tgt
        eff_floor = TRAIL_FLOOR * 0.5 if urgent else TRAIL_FLOOR
        if age >= MAX_HOLD_DAYS:
            exit_reason = "time_stop"; pnl_pct = cur_pct; exit_idx = j; break
        if cur_pct >= eff_tgt and peak < TRAIL_TRIGGER:
            exit_reason = "profit_target"; pnl_pct = cur_pct; exit_idx = j; break
        if peak >= TRAIL_TRIGGER and cur_pct <= (peak - eff_floor):
            exit_reason = "trailing_stop"; pnl_pct = cur_pct; exit_idx = j; break
        if cur_pct <= -STOP_LOSS_PCT:
            exit_reason = "stop_loss"; pnl_pct = cur_pct; exit_idx = j; break
        j += 1
    else:
        pnl_pct = cur_pct; exit_idx = n - 1

    pnl_dollars = pnl_pct * pay * 100 * contracts - COMMISSION * 2 * contracts
    return Trade(
        ticker=ticker, direction=direction, entry_date=str(entry_dt), exit_date=str(dates[exit_idx]),
        conviction=conviction, quality=round(quality, 2), dte=dte, entry_spot=round(S, 2), strike=K,
        entry_premium=round(pay, 3), contracts=contracts, exit_reason=exit_reason,
        pnl_pct=round(pnl_pct, 4), pnl_dollars=round(pnl_dollars, 2),
        hold_days=(dates[exit_idx] - entry_dt).days, signals=stack, regime=regime,
    ), exit_idx


def run_strategy(ticker, bars, signal_fn, regime_fn=None):
    """
    Generic strategy runner. signal_fn(features) -> (direction, conviction, quality, stack);
    regime_fn(features) -> regime label (tagged onto each trade). One position per ticker.
    """
    closes = [b["close"] for b in bars]; vols = [b["volume"] for b in bars]
    highs = [b["high"] for b in bars]; lows = [b["low"] for b in bars]; dates = [b["date"] for b in bars]
    n = len(bars)
    hv20_series = [_hv(closes, i, 20) for i in range(n)]
    trades: list[Trade] = []
    open_until = -1
    for i in range(200, n - 1):   # need 200 bars for SMA200/ADX context
        if i <= open_until:
            continue
        f = _features(closes, vols, highs, lows, hv20_series, i)
        direction, conviction, quality, stack = signal_fn(f)
        if direction is None or f["hv20"] <= 0:
            continue
        ivr = f["ivr"]
        if ivr > IVR_CAP:
            continue
        regime = regime_fn(f) if regime_fn else "all"
        tr, exit_idx = _simulate_position(ticker, closes, dates, i, n, direction, conviction,
                                          quality, ivr, f["hv20"], _hv(closes, i, 5), stack, regime)
        if tr is not None:
            trades.append(tr)
            open_until = exit_idx
    return trades


# ── Metrics ─────────────────────────────────────────────────────────────────────
def _pearson(xs: list[float], ys: list[float]) -> float:
    if len(xs) < 3:
        return 0.0
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return num / (dx * dy) if dx > 0 and dy > 0 else 0.0


def summarize(trades: list[Trade]) -> dict:
    if not trades:
        return {"n": 0}
    pcts = [t.pnl_pct for t in trades]
    wins = [p for p in pcts if p > 0]
    losses = [p for p in pcts if p <= 0]
    gross_win = sum(p for p in pcts if p > 0)
    gross_loss = -sum(p for p in pcts if p < 0)
    # equity curve in $ (sequential, by exit date)
    ordered = sorted(trades, key=lambda t: t.exit_date)
    equity = ACCOUNT_SIZE
    peak_eq = equity
    max_dd = 0.0
    curve = []
    for t in ordered:
        equity += t.pnl_dollars
        peak_eq = max(peak_eq, equity)
        max_dd = max(max_dd, (peak_eq - equity) / peak_eq)
        curve.append(equity)
    sharpe = (statistics.fmean(pcts) / statistics.pstdev(pcts) * math.sqrt(len(pcts))) \
        if len(pcts) > 1 and statistics.pstdev(pcts) > 0 else 0.0
    return {
        "n": len(trades),
        "win_rate": round(len(wins) / len(trades), 3),
        "avg_win_pct": round(statistics.fmean(wins), 3) if wins else 0.0,
        "avg_loss_pct": round(statistics.fmean(losses), 3) if losses else 0.0,
        "expectancy_pct": round(statistics.fmean(pcts), 4),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else float("inf"),
        "total_pnl_usd": round(sum(t.pnl_dollars for t in trades), 2),
        "final_equity": round(equity, 2),
        "return_pct": round((equity / ACCOUNT_SIZE - 1) * 100, 1),
        "max_drawdown_pct": round(max_dd * 100, 1),
        "sharpe_trade": round(sharpe, 2),
        "exit_mix": {r: sum(1 for t in trades if t.exit_reason == r)
                     for r in ("time_stop", "profit_target", "trailing_stop", "stop_loss", "expiry")},
    }


def load_universe(tickers=UNIVERSE, start=START, end=END, min_bars=250):
    """Batch-download daily OHLCV → {ticker: [bar dicts]}. Shared by main() and the regime engine."""
    import yfinance as yf
    df = yf.download(tickers, start=start, end=end, progress=False, auto_adjust=True, group_by="ticker")
    data: dict[str, list[dict]] = {}
    for t in tickers:
        try:
            tdf = df[t]
        except Exception:
            continue
        bars = []
        for ts, row in tdf.iterrows():
            c = row.get("Close")
            if c is None or (isinstance(c, float) and math.isnan(c)) or c <= 0:
                continue
            bars.append({"date": ts.date(), "open": float(row.get("Open", c)),
                         "high": float(row.get("High", c)), "low": float(row.get("Low", c)),
                         "close": float(c), "volume": int(row.get("Volume", 0) or 0)})
        if len(bars) > min_bars:
            data[t] = bars
    return data


def main():
    print(f"Fetching {len(UNIVERSE)} tickers {START}..{END} ...")
    data = load_universe()
    print(f"Loaded {len(data)} tickers with history.\n")

    all_trades: list[Trade] = []
    all_sig: list[dict] = []
    for t, bars in data.items():
        tr, sg = simulate_ticker(t, bars)
        all_trades.extend(tr)
        all_sig.extend(sg)

    overall = summarize(all_trades)
    print("══════════════ OVERALL (long-options deterministic core) ══════════════")
    for k, v in overall.items():
        print(f"  {k:18}: {v}")

    # Per-signal IC (across ALL ticker-days vs forward 5-day underlying return)
    print("\n══════════════ SIGNAL INFORMATION COEFFICIENT (predictive power) ══════════════")
    print("  IC = Pearson(signal at t, forward 5-day underlying return). |IC|>0.03 is meaningful.")
    fwd = [r["fwd5"] for r in all_sig]
    for name in ("mom", "rs", "vol", "conv_net"):
        ic = _pearson([r[name] for r in all_sig], fwd)
        print(f"  {name:10}: IC = {ic:+.4f}  (n={len(all_sig)})")

    # Walk-forward by year
    print("\n══════════════ WALK-FORWARD (robustness — is edge stable or curve-fit?) ══════════════")
    years = sorted({t.entry_date[:4] for t in all_trades})
    for y in years:
        s = summarize([t for t in all_trades if t.entry_date.startswith(y)])
        print(f"  {y}: n={s['n']:<4} win={s.get('win_rate',0):<6} "
              f"exp={s.get('expectancy_pct',0):<8} PF={s.get('profit_factor',0):<6} "
              f"ret={s.get('return_pct',0)}%")

    # Direction split
    print("\n══════════════ BY DIRECTION ══════════════")
    for d in ("bull", "bear"):
        s = summarize([t for t in all_trades if t.direction == d])
        print(f"  {d}: n={s.get('n',0):<4} win={s.get('win_rate',0):<6} exp={s.get('expectancy_pct',0)}")

    # Persist
    out = {"config": {"universe": list(data.keys()), "start": START, "end": END,
                      "target_delta": TARGET_DELTA, "vrp": VRP, "max_hold": MAX_HOLD_DAYS},
           "overall": overall,
           "ic": {name: round(_pearson([r[name] for r in all_sig], fwd), 4)
                  for name in ("mom", "rs", "vol", "conv_net")},
           "by_year": {y: summarize([t for t in all_trades if t.entry_date.startswith(y)]) for y in years},
           "n_trades": len(all_trades)}
    with open("backtest_results/long_options_core.json", "w") as f:
        json.dump(out, f, indent=2, default=str)
    with open("backtest_results/long_options_trades.json", "w") as f:
        json.dump([asdict(t) for t in all_trades], f, indent=2, default=str)
    print(f"\nWrote backtest_results/long_options_core.json ({len(all_trades)} trades)")
    return out


if __name__ == "__main__":
    main()

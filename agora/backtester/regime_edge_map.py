"""
agora/backtester/regime_edge_map.py — Strategy × Regime Edge Map (research organ).

Builds the matrix the multi-strategy system needs: for each long-options PLAYBOOK,
measure its out-of-sample expectancy CONDITIONED ON the market regime. Proves the
load-bearing premise — that an edge is conditional on regime (mean-reversion wins in
ranges, momentum/breakout wins in trends) — so the live system can deploy only the
(strategy × regime) cells that are proven, and sit out otherwise.

Reuses the validated Edge Research Engine (long_options_backtest): same lookahead-free
indicators, BS pricing, exact exit ladder, and IV-crush stress. Honest scope: price/
volume signals only (flow/news still require live forward-validation). Headline map is
run at a realistic 15% IV crush.

Run:  python -m agora.backtester.regime_edge_map
"""

from __future__ import annotations

import agora.backtester.long_options_backtest as bt


# ── Strategy library — each a falsifiable playbook (features dict → decision) ────
def s_mean_rev(f):
    """Oversold dip-buyer (PROVEN). Buy calls on RSI<40; weak overbought puts."""
    if f["rsi"] < 40:
        return "bull", 2, 1.6, {"strat": "mean_rev", "rsi": round(f["rsi"])}
    if f["rsi"] > 78:
        return "bear", 1, 0.6, {"strat": "mean_rev", "rsi": round(f["rsi"])}
    return None, 0, 0.0, {}


def s_momentum(f):
    """Trend-following: RSI strong + above MAs → call; weak + below → put."""
    if f["rsi"] > 55 and f["above20"] and f["above50"]:
        return "bull", 2, 1.6, {"strat": "momentum"}
    if f["rsi"] < 45 and not f["above20"] and not f["above50"]:
        return "bear", 2, 1.6, {"strat": "momentum"}
    return None, 0, 0.0, {}


def s_breakout(f):
    """Breakout: new 20-day high → call; new 20-day low → put."""
    if f["S"] >= f["high20"] and f["above50"]:
        return "bull", 2, 1.4, {"strat": "breakout"}
    if f["S"] <= f["low20"] and not f["above50"]:
        return "bear", 2, 1.4, {"strat": "breakout"}
    return None, 0, 0.0, {}


def s_trend_pullback(f):
    """Buy-the-dip in an uptrend: above 200MA but RSI<45 (shallow pullback) → call."""
    if f["above200"] and f["rsi"] < 45:
        return "bull", 2, 1.6, {"strat": "trend_pullback"}
    if (not f["above200"]) and f["rsi"] > 60:
        return "bear", 2, 1.6, {"strat": "trend_pullback"}
    return None, 0, 0.0, {}


STRATEGIES = {
    "mean_rev":       s_mean_rev,
    "momentum":       s_momentum,
    "breakout":       s_breakout,
    "trend_pullback": s_trend_pullback,
}


# ── Regime classifiers (features dict → label) ──────────────────────────────────
def regime_trend(f):
    """ADX-based: TREND (adx>=25) vs RANGE (adx<25)."""
    return "TREND" if f["adx"] >= 25 else "RANGE"


def regime_vol(f):
    """HV-rank (IVR proxy): LOVOL (<50) vs HIVOL (>=50)."""
    return "LOVOL" if f["ivr"] < 50 else "HIVOL"


def regime_dir(f):
    """200-MA: UPTREND vs DOWNTREND."""
    return "UP" if f["above200"] else "DOWN"


def _cell(trades, regime_label):
    sub = [t for t in trades if t.regime == regime_label]
    ins = [t for t in sub if t.entry_date < "2025-01-01"]
    oos = [t for t in sub if t.entry_date >= "2025-01-01"]
    si, so = bt.summarize(ins), bt.summarize(oos)
    return {"n": len(sub), "n_oos": so.get("n", 0),
            "exp_is": si.get("expectancy_pct", 0.0), "pf_is": si.get("profit_factor", 0.0),
            "exp_oos": so.get("expectancy_pct", 0.0), "pf_oos": so.get("profit_factor", 0.0)}


def build_map(data, regime_fn, regime_labels, label):
    print(f"\n══════════════════ EDGE MAP — by {label} (15% IV crush, OOS=2025-26) ══════════════════")
    header = f"  {'strategy':16}"
    for r in regime_labels + ["ALL"]:
        header += f"│ {r:^22} "
    print(header)
    print("  " + "─" * (16 + 24 * (len(regime_labels) + 1)))
    rows = {}
    for sname, sfn in STRATEGIES.items():
        all_trades = []
        for t, bars in data.items():
            all_trades.extend(bt.run_strategy(t, bars, sfn, regime_fn))
        rows[sname] = all_trades
        line = f"  {sname:16}"
        for r in regime_labels:
            c = _cell(all_trades, r)
            mark = "✅" if (c["exp_oos"] > 0.02 and c["pf_oos"] >= 1.15 and c["n_oos"] >= 15) else "  "
            line += f"│{mark}OOS {c['exp_oos']:+.3f} PF{c['pf_oos']:<4.2f} n{c['n_oos']:<3}"
        # ALL regimes
        ins = [t for t in all_trades if t.entry_date < "2025-01-01"]
        oos = [t for t in all_trades if t.entry_date >= "2025-01-01"]
        so = bt.summarize(oos)
        mark = "✅" if (so.get("expectancy_pct", 0) > 0.02 and so.get("profit_factor", 0) >= 1.15) else "  "
        line += f"│{mark}OOS {so.get('expectancy_pct',0):+.3f} PF{so.get('profit_factor',0):<4.2f} n{so.get('n',0):<3}"
        print(line)
    print("  (✅ = OOS expectancy>+2%, PF>=1.15, n_oos>=15 — a PROVEN cell)")
    return rows


def main():
    bt.IV_CRUSH = 0.15   # honest headline: realistic IV crush on every mark
    print(f"Fetching {len(bt.UNIVERSE)} tickers {bt.START}..{bt.END} ...")
    data = bt.load_universe()
    print(f"Loaded {len(data)} tickers.")

    build_map(data, regime_trend, ["TREND", "RANGE"], "TREND vs RANGE (ADX)")
    build_map(data, regime_vol,   ["LOVOL", "HIVOL"], "VOLATILITY (HV-rank)")
    build_map(data, regime_dir,   ["UP", "DOWN"],     "DIRECTION (200-MA)")

    print("\n══════════════════ READOUT ══════════════════")
    print("  Premise = 'edge is conditional on regime'. If mean_rev's RANGE cell and")
    print("  momentum/breakout's TREND cell are ✅ while their opposite cells are not,")
    print("  regime-conditioning is PROVEN and the live system should switch on regime.")


if __name__ == "__main__":
    main()

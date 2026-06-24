"""
agora/ops/ticker_adapter.py — the per-ticker adaptive job (Phase 2, SHADOW).

Each ticker has its own edge. This job reads each ticker's OWN clean realized history (trade_features,
now with fixed MFE/MAE capture), shrinks it toward the global prior so thin samples can't overfit, and
writes per-ticker setting overrides. In Phase 2 every override is written SHADOW (active=0) — logged
and dashboard-visible, NEVER applied — so we validate the signal before it can touch a live trade.

Method (panel-approved):
  • Bayesian shrinkage (partial pooling): shrunk = (n·ticker + k·global) / (n + k). A ticker with 6
    closes looks ~mostly global; with 40 closes ~mostly itself. k = _PRIOR_STRENGTH.
  • N-gated: no override below _MIN_N clean closes (else the ticker uses the global default).
  • Down-only: the only override emitted is a TIGHTER per-ticker risk cap for a ticker whose shrunk
    expectancy is negative — concentrate capital away from proven per-ticker losers. Never loosens.
  • Provenance: each override is stamped with the current config_version + an ML/human rationale
    (shrunk EV, n, IVR signature).

Never raises — returns a summary dict; on any error returns {"status": "error"|"skipped"}.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from agora.ops.config_provenance import current_config_version
from agora.ops.ticker_settings import set_override

_MIN_N = 6              # clean closes a ticker needs before it earns ANY override
_PRIOR_STRENGTH = 20.0  # k — global-prior weight in the shrinkage (higher = more conservative)
_DOWN_FACTOR = 0.5      # tighter per-ticker risk cap for proven (shrunk) losers
_SOURCE = "ticker_adapter_v1"


def _shrink(ticker_stat: float, global_stat: float, n: int, k: float = _PRIOR_STRENGTH) -> float:
    """Partial pooling: blend the ticker estimate with the global prior by sample size."""
    return (n * ticker_stat + k * global_stat) / (n + k) if (n + k) > 0 else global_stat


def run_ticker_adapter(db_path: Any) -> dict:
    """Compute per-ticker SHADOW overrides from each ticker's shrunk realized edge. Never raises."""
    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        # Global prior + per-ticker realized P&L over trustworthy labeled closes.
        rows = conn.execute(
            "SELECT ticker, realized_pnl FROM trade_features "
            "WHERE is_real_close=1 AND realized_pnl IS NOT NULL").fetchall()
        conn.close()
    except Exception:
        return {"status": "skipped", "reason": "no feature store"}

    if not rows:
        return {"status": "skipped", "reason": "no labeled closes"}

    global_ev = sum(r["realized_pnl"] for r in rows) / len(rows)
    by_ticker: dict[str, list[float]] = {}
    for r in rows:
        by_ticker.setdefault(r["ticker"], []).append(r["realized_pnl"])

    cfg_v = current_config_version(str(db_path))
    # Vol-scaled cap factors from the per-ticker profiles (price-history characterization, Phase A).
    # Down-only: a high-realized-vol ticker gets a tighter cap; 1.0 if no profile yet.
    try:
        from agora.ops.ticker_profile import all_profiles
        vol_factor = {p["ticker"]: (p.get("cap_factor") or 1.0) for p in all_profiles(db_path)}
    except Exception:
        vol_factor = {}

    cap_global = 400.0   # global max_risk_per_trade_dollars (#4); overrides only ever TIGHTEN it.
    universe = set(by_ticker) | set(vol_factor)
    written, examined = 0, 0
    for ticker in sorted(universe):
        examined += 1
        pnls = by_ticker.get(ticker, [])
        n = len(pnls)
        # Edge factor — n-gated, down-only: a proven (shrunk-EV<0) per-ticker loser → _DOWN_FACTOR.
        edge_factor, shrunk_ev = 1.0, None
        if n >= _MIN_N:
            ticker_ev = sum(pnls) / n
            shrunk_ev = _shrink(ticker_ev, global_ev, n)
            if shrunk_ev < 0:
                edge_factor = _DOWN_FACTOR
        # Vol factor — from the ticker's realized-vol profile (abundant price history, no n-gate).
        vf = vol_factor.get(ticker, 1.0)
        factor = min(edge_factor, vf)
        if factor >= 1.0:
            continue   # neither signal tightens → use the global default
        new_cap = round(cap_global * factor)
        parts = []
        if vf < 1.0:
            parts.append(f"vol×{vf}")
        if edge_factor < 1.0 and shrunk_ev is not None:
            parts.append(f"edge shrunk_EV=${shrunk_ev:.0f}(n={n})")
        rationale = f"cap {cap_global:.0f}→{new_cap} [{', '.join(parts)}; global EV ${global_ev:.0f}]"
        if set_override(db_path, ticker, "max_risk_per_trade_dollars", new_cap,
                        source=_SOURCE, n_samples=n, active=False,   # SHADOW — never applied
                        rationale=rationale, config_version=cfg_v):
            written += 1

    return {"status": "ok", "global_ev": round(global_ev, 2), "tickers_examined": examined,
            "shadow_overrides_written": written,
            "summary": f"{written} shadow per-ticker cap overrides (vol+edge, down-only) "
                       f"from {examined} tickers; global EV ${global_ev:.0f}"}

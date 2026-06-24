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
    try:
        from agora.ops.market_capture import iv_rank_for_ticker
    except Exception:
        iv_rank_for_ticker = lambda _t: None   # noqa: E731

    written, examined = 0, 0
    cap_global = 400.0   # the global max_risk_per_trade_dollars (#4); overrides only ever tighten it
    for ticker, pnls in by_ticker.items():
        n = len(pnls)
        if n < _MIN_N:
            continue
        examined += 1
        ticker_ev = sum(pnls) / n
        shrunk_ev = _shrink(ticker_ev, global_ev, n)
        # Down-only: only proven (shrunk) per-ticker losers get a tighter cap.
        if shrunk_ev >= 0:
            continue
        ivr = None
        try:
            ivr = iv_rank_for_ticker(ticker)
        except Exception:
            pass
        new_cap = round(cap_global * _DOWN_FACTOR)
        rationale = (f"shrunk_EV=${shrunk_ev:.0f} (raw ${ticker_ev:.0f}, n={n}, global ${global_ev:.0f}) "
                     f"→ tighten risk cap {cap_global:.0f}→{new_cap}"
                     + (f"; IVR={ivr:.0f}" if ivr is not None else ""))
        if set_override(db_path, ticker, "max_risk_per_trade_dollars", new_cap,
                        source=_SOURCE, n_samples=n, active=False,  # SHADOW
                        rationale=rationale, config_version=cfg_v):
            written += 1

    return {"status": "ok", "global_ev": round(global_ev, 2), "tickers_examined": examined,
            "shadow_overrides_written": written,
            "summary": f"{written} shadow per-ticker risk-cap overrides "
                       f"(from {examined} tickers with >={_MIN_N} closes; global EV ${global_ev:.0f})"}

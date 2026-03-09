"""
Scoring Service — bridges the backend API to the real trading engine.

Replaces the hash-based stub with actual:
  • VIX regime classification (GREEN / YELLOW / RED)
  • ATR-based position sizing filter
  • IBKR options chain with live Greeks
  • Strike selection using validated delta targets
  • Credit spread / iron condor plan generation

All logic here is thin orchestration — the heavy lifting lives in
trading_engine.config, trading_engine.filters, and run_live.py.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

# Ensure the project root is importable
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from trading_engine.config import AdaptiveConfig, EngineConfig
from trading_engine.filters import ProductionFilters, FilterDecision
from trading_engine.execution.state import StateManager

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────
# VIX helpers (same as run_live.py — lightweight, no IBKR needed)
# ─────────────────────────────────────────────────────────────────

def _classify_regime(vix: float) -> str:
    if vix < 18:
        return "GREEN"
    elif vix < 25:
        return "YELLOW"
    return "RED"


def _fetch_vix_yfinance() -> Optional[float]:
    """Fallback VIX fetch via yfinance (no IBKR needed)."""
    try:
        import yfinance as yf
        data = yf.download("^VIX", period="5d", progress=False)
        if not data.empty:
            return float(data["Close"].iloc[-1])
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────────────────────────
# Dataclasses returned to the API layer
# ─────────────────────────────────────────────────────────────────

@dataclass
class SymbolScore:
    """Real score for a tradeable symbol."""
    symbol: str
    score: float               # 0.0–1.0 composite score
    regime: str                # GREEN / YELLOW / RED
    vix: float
    atr_pct: float
    size_multiplier: float     # Final compound sizing
    direction: str             # "credit_sell" (premium selling)
    preferred_strategy: str    # call_credit / put_credit / iron_condor
    trade_enabled: bool
    reason: str                # Human-readable


@dataclass
class SpreadPlan:
    """A concrete spread plan with real strikes + credit."""
    symbol: str
    strategy: str              # call_credit / put_credit / iron_condor
    right: str                 # C / P / IC
    short_strike: float
    long_strike: float
    credit_per_contract: float
    max_loss_per_contract: float
    width: float
    num_contracts: int
    total_credit: float
    total_max_loss: float
    short_delta: float
    short_iv: float
    expiry: str                # YYYY-MM-DD
    regime: str
    vix: float
    pop_estimate: float        # Probability of profit (from delta)


# ─────────────────────────────────────────────────────────────────
# Main scoring service
# ─────────────────────────────────────────────────────────────────

class ScoringService:
    """
    Stateless scoring engine for the API layer.

    Wraps the validated production logic:
      1. VIX → regime → trade_enabled + strategy + delta target
      2. ATR filter → size_multiplier (or skip)
      3. State manager → circuit breaker check
      4. Strike selection from live chain (when available)
    """

    def __init__(
        self,
        account_size: float = 50_000.0,
        state_path: Optional[str] = None,
    ):
        self.adaptive = AdaptiveConfig()
        self.filters = ProductionFilters()
        self.state = StateManager(
            path=state_path or os.path.join(_PROJECT_ROOT, "data", "live_state.json"),
            account_size=account_size,
        )
        self.state.load()
        self.account_size = account_size
        self._cached_vix: Optional[float] = None
        self._cached_vix_ts: float = 0
        self._cached_daily_bars: Dict[str, Any] = {}

    # ─── VIX ─────────────────────────────────────────────────────

    def get_vix(self, ibkr_client=None) -> float:
        """
        Get current VIX. Tries IBKR first, falls back to yfinance.
        Caches for 60 seconds.
        """
        now = time.time()
        if self._cached_vix and (now - self._cached_vix_ts) < 60:
            return self._cached_vix

        vix = None

        # Try IBKR if client provided
        if ibkr_client and ibkr_client.is_connected():
            try:
                import ib_insync
                ib = ibkr_client._client
                vix_contract = ib_insync.Index("VIX", "CBOE")
                ib.qualifyContracts(vix_contract)
                ticker = ib.reqTickers(vix_contract)
                if ticker:
                    price = ticker[0].marketPrice()
                    if price and price > 0:
                        vix = float(price)
            except Exception as e:
                logger.warning("IBKR VIX fetch failed: %s", e)

        # Fallback to yfinance
        if vix is None:
            vix = _fetch_vix_yfinance()

        if vix is None:
            vix = 18.0  # Conservative default

        self._cached_vix = vix
        self._cached_vix_ts = now
        return vix

    # ─── Score Symbols ───────────────────────────────────────────

    def score_symbols(
        self,
        symbols: List[str],
        ibkr_client=None,
        daily_bars: Optional[Dict[str, Any]] = None,
    ) -> List[SymbolScore]:
        """
        Score a list of symbols using the real signal stack.

        This is the replacement for the hash-based stub.
        """
        vix = self.get_vix(ibkr_client)
        regime = _classify_regime(vix)
        params = self.adaptive.for_regime(regime)

        # State-level circuit breaker
        can_trade, state_reason = self.state.can_trade()

        # ATR filter (per-symbol if we have bars)
        results = []
        for sym in symbols:
            sym = sym.upper()

            # Run ATR filter if daily bars available
            filter_decision = FilterDecision()
            if daily_bars and sym in daily_bars:
                bars = daily_bars[sym]
                filter_decision = self.filters.pre_entry(
                    history_closes=bars.get("closes", []),
                    history_highs=bars.get("highs", []),
                    history_lows=bars.get("lows", []),
                )

            # Compound size multiplier
            size_mult = params.position_size_mult
            size_mult *= filter_decision.size_multiplier
            size_mult *= self.state.get_size_multiplier()

            # Trade enabled?
            trade_ok = params.trade_enabled and can_trade and not filter_decision.skip

            # Composite score (0–1):
            #   1.0 = GREEN + low ATR + no circuit breakers
            #   0.5 = YELLOW or elevated ATR
            #   0.0 = RED or circuit breaker hit
            if not trade_ok:
                score = 0.0
            else:
                regime_score = {"GREEN": 0.8, "YELLOW": 0.5, "RED": 0.0}.get(regime, 0.0)
                atr_score = filter_decision.size_multiplier  # 1.0, 0.75, 0.5, 0.0
                score = round(regime_score * atr_score, 4)

            # Reason string
            if not params.trade_enabled:
                reason = f"{regime} regime — trading disabled"
            elif not can_trade:
                reason = state_reason
            elif filter_decision.skip:
                reason = f"ATR filter: {filter_decision.reason}"
            else:
                reason = (
                    f"{regime} regime (VIX={vix:.1f}) | "
                    f"ATR={filter_decision.atr_pct:.2f}% | "
                    f"size={size_mult:.2f}×"
                )

            results.append(SymbolScore(
                symbol=sym,
                score=score,
                regime=regime,
                vix=vix,
                atr_pct=filter_decision.atr_pct,
                size_multiplier=round(size_mult, 4),
                direction="credit_sell",
                preferred_strategy=params.preferred_strategy,
                trade_enabled=trade_ok,
                reason=reason,
            ))

        # Sort by score descending
        results.sort(key=lambda x: x.score, reverse=True)
        return results

    # ─── Plan Generation ─────────────────────────────────────────

    def generate_plans(
        self,
        symbols: List[str],
        equity: float = 50_000.0,
        chain_quotes: Optional[Dict[str, List[Dict]]] = None,
    ) -> List[SpreadPlan]:
        """
        Generate real spread plans using live chain data.

        Args:
            symbols:       List of symbols to plan for
            equity:        Account equity for sizing
            chain_quotes:  {symbol: [quote_dicts]} from the options chain endpoint
                           Each quote dict has: strike, right, bid, ask, delta, gamma, iv, mark

        Returns:
            List of concrete SpreadPlan objects
        """
        scores = self.score_symbols(symbols)
        vix = self.get_vix()
        regime = _classify_regime(vix)
        params = self.adaptive.for_regime(regime)

        plans = []
        for sc in scores:
            if not sc.trade_enabled or sc.score == 0:
                continue

            sym = sc.symbol
            expiry = date.today().strftime("%Y-%m-%d")

            # If we have real chain data, find optimal strikes
            if chain_quotes and sym in chain_quotes:
                plan = self._plan_from_chain(
                    sym, chain_quotes[sym], params, sc, equity, expiry,
                )
                if plan:
                    plans.append(plan)
            else:
                # Without chain data, generate a "signal" plan
                # (the frontend can then fetch chain and refine)
                plans.append(SpreadPlan(
                    symbol=sym,
                    strategy=sc.preferred_strategy,
                    right="C" if "call" in sc.preferred_strategy else "P",
                    short_strike=0.0,  # Needs chain data
                    long_strike=0.0,
                    credit_per_contract=0.0,
                    max_loss_per_contract=0.0,
                    width=params.width,
                    num_contracts=0,
                    total_credit=0.0,
                    total_max_loss=0.0,
                    short_delta=params.delta,
                    short_iv=0.0,
                    expiry=expiry,
                    regime=regime,
                    vix=vix,
                    pop_estimate=round(1.0 - params.delta, 2) * 100,
                ))

        return plans

    def _plan_from_chain(
        self,
        symbol: str,
        quotes: List[Dict],
        params,
        score: SymbolScore,
        equity: float,
        expiry: str,
    ) -> Optional[SpreadPlan]:
        """Build a spread plan from real options chain quotes."""
        strategy = score.preferred_strategy

        if strategy == "iron_condor":
            # Build both sides — return call side for now
            # (full IC support comes with /options/place wiring)
            right = "C"
        elif "call" in strategy:
            right = "C"
        else:
            right = "P"

        # Filter to right side, valid delta
        side_quotes = [
            q for q in quotes
            if q.get("right", "").upper() == right
            and q.get("delta") is not None
            and abs(q.get("delta", 0)) > 0
        ]
        if not side_quotes:
            return None

        # Find short strike closest to target delta
        target_delta = params.delta
        side_quotes.sort(key=lambda q: abs(abs(q["delta"]) - target_delta))
        short_q = side_quotes[0]

        short_strike = short_q["strike"]
        width = params.width

        if right == "P":
            long_strike_target = short_strike - width
        else:
            long_strike_target = short_strike + width

        # Find closest long strike
        long_candidates = [q for q in quotes if q.get("right", "").upper() == right]
        long_candidates.sort(key=lambda q: abs(q["strike"] - long_strike_target))
        if not long_candidates:
            return None
        long_q = long_candidates[0]

        # Credit estimate
        credit = short_q.get("bid", 0) - long_q.get("ask", 0)
        if credit <= 0:
            credit = (short_q.get("mark", 0) - long_q.get("mark", 0)) * 0.85
        credit = max(0, round(credit, 2))

        actual_width = abs(short_strike - long_q["strike"])
        max_loss = actual_width - credit if credit > 0 else actual_width

        # Sizing: 3% risk budget × compound multiplier
        risk_per_contract = max_loss * 100 if max_loss > 0 else actual_width * 100
        risk_budget = equity * 0.03
        base_contracts = max(1, int(risk_budget / risk_per_contract)) if risk_per_contract > 0 else 1
        num_contracts = max(1, int(base_contracts * score.size_multiplier))

        pop = round((1.0 - abs(short_q.get("delta", target_delta))) * 100, 1)

        return SpreadPlan(
            symbol=symbol,
            strategy=strategy,
            right=right,
            short_strike=short_strike,
            long_strike=long_q["strike"],
            credit_per_contract=credit,
            max_loss_per_contract=round(max_loss, 2),
            width=actual_width,
            num_contracts=num_contracts,
            total_credit=round(credit * num_contracts * 100, 2),
            total_max_loss=round(max_loss * num_contracts * 100, 2),
            short_delta=round(abs(short_q.get("delta", 0)), 4),
            short_iv=round(short_q.get("iv", 0), 4),
            expiry=expiry,
            regime=score.regime,
            vix=score.vix,
            pop_estimate=pop,
        )

    # ─── Metrics from Live State ─────────────────────────────────

    def get_live_metrics(self) -> Dict[str, Any]:
        """Read real metrics from the state file."""
        self.state.load()
        s = self.state.state

        can_trade, reason = self.state.can_trade()

        # Calculate drawdown
        current_equity = self.account_size + s.monthly_pnl
        peak = s.peak_equity if s.peak_equity > 0 else self.account_size
        drawdown = (peak - current_equity) / peak if peak > 0 else 0

        return {
            "date": s.date,
            "trades": s.trades_today,
            "daily_pnl": round(s.daily_pnl, 2),
            "weekly_pnl": round(s.weekly_pnl, 2),
            "monthly_pnl": round(s.monthly_pnl, 2),
            "consecutive_losses": s.consecutive_losses,
            "open_positions": len(s.open_positions),
            "positions": s.open_positions,
            "in_recovery": s.in_recovery,
            "can_trade": can_trade,
            "can_trade_reason": reason,
            "max_drawdown_30d": round(drawdown, 4),
            "peak_equity": round(peak, 2),
            "current_equity": round(current_equity, 2),
            "closed_trades_today": s.closed_trades,
        }

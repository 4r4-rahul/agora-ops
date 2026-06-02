"""
DevilsAdvocate — deterministic pre-IBKR checklist. Zero LLM calls, zero network fetches.

Runs 5 pure-Python checks inside _submit_recommendation, after the risk council and before
the IBKR submit_trade() call. All checks use data already in scope at call time.

Checks (in order, any failure = block):
  1. Earnings spans expiry   — credit trade would survive through earnings gamma event
  2. Duplicate ticker         — already holding same ticker; concentration risk
  3. Macro opposing direction — risk_off + bullish non-event play
  4. Vol selling blocked      — MacroContext.vol_selling_ok=False; IVR inadequate for credit
  5. Conviction floor         — absolute minimum score gate; catch low-edge setups

Returns: list of (check_name, ok: bool, reason: str) so the caller can log granularly.
Convenience: `run(...)` returns (all_passed: bool, first_block_reason: str).
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..core.models import TradeRecommendation, OpenPosition, StrategyPillar

def _get_conviction_floor() -> float:
    from agora.core.config import get_settings
    return get_settings().disagreement_resolver_floor  # reuse the same paper-mode floor

_CONVICTION_FLOOR = 30.0  # kept for reference; runtime uses _get_conviction_floor()
_CREDIT_STRATEGY_TYPES = frozenset({
    "bull_put_spread", "bear_call_spread", "iron_condor", "iron_butterfly", "cash_secured_put",
})
_EVENT_PILLARS = frozenset({"event_fomc", "event_cpi", "post_earnings"})


def _check_earnings_spans_expiry(
    recommendation: Any,
    earnings_date: Any,
    is_pre_earnings: bool,
) -> tuple[bool, str]:
    """Block credit positions that inadvertently span an earnings date."""
    if not earnings_date or is_pre_earnings:
        return True, ""
    strategy_str = str(getattr(recommendation.strategy, "value", recommendation.strategy))
    if strategy_str not in _CREDIT_STRATEGY_TYPES:
        return True, ""
    expiry = min((leg.expiration for leg in recommendation.legs), default=None)
    if expiry is None:
        return True, ""
    if isinstance(earnings_date, date) and earnings_date <= expiry:
        dte = (expiry - date.today()).days
        return (
            False,
            f"Earnings on {earnings_date} falls within option expiry {expiry} "
            f"({dte} DTE) — unmodeled gamma risk for credit spread",
        )
    return True, ""


def _check_duplicate_ticker(
    recommendation: Any,
    positions: list[Any],
) -> tuple[bool, str]:
    """Block if we already have an open position in the same ticker."""
    for p in positions:
        if p.ticker == recommendation.ticker:
            return (
                False,
                f"Already holding open position in {recommendation.ticker} "
                f"(position_id={p.position_id}) — duplicate ticker concentration",
            )
    return True, ""


def _check_macro_opposing(
    recommendation: Any,
    macro_context: Any,
) -> tuple[bool, str]:
    """Block risk_off + bullish non-event plays."""
    if macro_context is None:
        return True, ""
    pillar_str = str(getattr(recommendation.pillar, "value", recommendation.pillar))
    if pillar_str in _EVENT_PILLARS:
        return True, ""
    stance = getattr(macro_context, "macro_stance", "neutral")
    direction = getattr(recommendation, "direction", "neutral")
    if stance == "risk_off" and direction == "bullish":
        confidence = getattr(macro_context, "confidence", 0.5)
        return (
            False,
            f"Macro is risk_off (confidence={confidence:.0%}) but trade is bullish {pillar_str} — "
            f"macro-direction opposition",
        )
    return True, ""


def _check_vol_selling_ok(
    recommendation: Any,
    macro_context: Any,
) -> tuple[bool, str]:
    """Block vol_premium entries when macro context says IVR is too low."""
    strategy_str = str(getattr(recommendation.strategy, "value", recommendation.strategy))
    if strategy_str not in _CREDIT_STRATEGY_TYPES:
        return True, ""
    if macro_context is None:
        return True, ""
    vol_ok = getattr(macro_context, "vol_selling_ok", True)
    if not vol_ok:
        from agora.core.config import get_settings
        if get_settings().force_vol_selling_ok:
            return True, ""
        return (
            False,
            f"MacroContext.vol_selling_ok=False — IVR inadequate for {strategy_str} credit entry",
        )
    return True, ""


def _check_conviction_floor(recommendation: Any) -> tuple[bool, str]:
    """Absolute minimum conviction gate — catches low-edge setups that slipped through scoring."""
    score = getattr(recommendation, "conviction_score", 100.0)
    floor = _get_conviction_floor()
    if score < floor:
        return (
            False,
            f"Conviction {score:.0f} below absolute floor {floor:.0f} — insufficient edge",
        )
    return True, ""


CheckResult = tuple[str, bool, str]   # (name, ok, reason)


def run(
    recommendation: Any,
    positions: list[Any],
    macro_context: Any,
    earnings_date: Any = None,
    is_pre_earnings: bool = False,
) -> tuple[bool, str, list[CheckResult]]:
    """
    Run all 5 DevilsAdvocate checks.

    Returns:
      all_passed: bool
      first_block_reason: str  (empty when all_passed=True)
      results: list of (check_name, ok, reason) for granular logging
    """
    checks: list[CheckResult] = []

    ok1, r1 = _check_earnings_spans_expiry(recommendation, earnings_date, is_pre_earnings)
    checks.append(("da_earnings_dte", ok1, r1))
    if not ok1:
        return False, r1, checks

    ok2, r2 = _check_duplicate_ticker(recommendation, positions)
    checks.append(("da_duplicate_ticker", ok2, r2))
    if not ok2:
        return False, r2, checks

    ok3, r3 = _check_macro_opposing(recommendation, macro_context)
    checks.append(("da_macro_direction", ok3, r3))
    if not ok3:
        return False, r3, checks

    ok4, r4 = _check_vol_selling_ok(recommendation, macro_context)
    checks.append(("da_vol_selling", ok4, r4))
    if not ok4:
        return False, r4, checks

    ok5, r5 = _check_conviction_floor(recommendation)
    checks.append(("da_conviction_floor", ok5, r5))
    if not ok5:
        return False, r5, checks

    return True, "", checks

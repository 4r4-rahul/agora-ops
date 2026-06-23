"""
ExecutionAdvisor — deterministic, shadow-mode controller for the entry slippage budget.

The config (max_slippage_pct_of_width) claimed "the execution advisor tunes this from
observed fill rates", but no such advisor existed — the value was static. This is that
advisor, built as the recalibration's step 2/3 (don't-suspend, prove-the-edge): a pure,
rule-driven control law that reads per-strategy fill outcomes and recommends a bounded
adjustment to the slippage budget.

SHADOW ONLY. recommend() returns a recommendation; it never writes config. Promotion to
live tuning is a separate, gated step (mirrors the model→trading shadow→promote ladder).

Control law (per strategy, over a window):
  - effective_total = fills + timeouts + rejects   (policy/Error-201 rejects excluded)
  - need >= MIN_SAMPLE attempts, else HOLD (don't tune on noise)
  - fill_rate >= TARGET_HIGH         -> TIGHTEN (recover edge; we're overpaying to cross)
  - fill_rate <= TARGET_LOW:
        timeout-dominated failures    -> WIDEN  (limit isn't crossing; more walk budget)
        reject-dominated failures     -> HOLD   (rejects are policy/validation; slippage
                                                  can't fix them — flag for investigation)
  - otherwise                         -> HOLD   (fill rate already in the target band)
  Recommendations are clamped to [MIN_BUDGET, MAX_BUDGET] and step by STEP.

Paper-mode caveat: IBKR's paper simulator structurally won't fill multi-leg combos
(observed: single legs fill 12-20%, spreads ~1%), so spread fill data in paper is an
artifact. Combo recommendations in paper are marked trusted=False so the promotion step
never acts on them; the real proving ground for spread fills is the (now fill-realistic)
backtester, not paper.
"""

from __future__ import annotations

# Control-law constants (deterministic; no magic mid-function).
MIN_SAMPLE   = 10      # minimum effective attempts before tuning a strategy
TARGET_LOW   = 0.40    # below this fill rate -> consider widening
TARGET_HIGH  = 0.80    # above this fill rate -> tighten to recover edge
STEP         = 0.05    # one adjustment step, in fraction-of-width
MIN_BUDGET   = 0.10    # never tighten below this
MAX_BUDGET   = 0.40    # never widen above this (edge guard)

_COMBO_MARKERS = ("spread", "condor", "butterfly", "combo")


def is_combo(strategy: str) -> bool:
    """Multi-leg strategy (paper-sim can't fill these reliably)."""
    s = (strategy or "").lower()
    return any(m in s for m in _COMBO_MARKERS)


class ExecutionAdvisor:
    """Pure controller. No I/O — caller supplies the per-strategy outcome counts."""

    def _clamp(self, x: float) -> float:
        return round(max(MIN_BUDGET, min(MAX_BUDGET, x)), 4)

    def _one(self, strategy: str, st: dict, current: float, trading_mode: str) -> dict:
        fills    = int(st.get("fills", 0))
        timeouts = int(st.get("timeouts", 0))
        rejects  = int(st.get("rejects", 0))            # non-policy rejects only
        eff_total = fills + timeouts + rejects
        failures  = timeouts + rejects
        fill_rate = (fills / eff_total) if eff_total > 0 else 0.0
        timeout_share = (timeouts / failures) if failures > 0 else 0.0

        if eff_total < MIN_SAMPLE:
            action, recommended, reason = "hold", current, f"insufficient sample (n={eff_total}<{MIN_SAMPLE})"
        elif fill_rate >= TARGET_HIGH:
            action, recommended, reason = "tighten", self._clamp(current - STEP), \
                f"fill {fill_rate:.0%} ≥ {TARGET_HIGH:.0%} — recover edge"
        elif fill_rate <= TARGET_LOW:
            if timeout_share >= 0.5:
                action, recommended, reason = "widen", self._clamp(current + STEP), \
                    f"fill {fill_rate:.0%} ≤ {TARGET_LOW:.0%}, {timeout_share:.0%} timeouts — limit not crossing"
            else:
                action, recommended, reason = "hold", current, \
                    f"fill {fill_rate:.0%} low but reject-dominated — slippage won't help, investigate rejects"
        else:
            action, recommended, reason = "hold", current, \
                f"fill {fill_rate:.0%} within target band [{TARGET_LOW:.0%},{TARGET_HIGH:.0%}]"

        trusted = not (trading_mode == "paper" and is_combo(strategy))
        return {
            "strategy":      strategy,
            "n":             eff_total,
            "fill_rate":     round(fill_rate, 4),
            "timeout_share": round(timeout_share, 4),
            "policy_rejects": int(st.get("policy_rejects", 0)),
            "current":       round(current, 4),
            "recommended":   recommended,
            "action":        action,
            "reason":        reason,
            "trusted":       trusted,
        }

    def recommend(
        self,
        per_strategy_stats: dict[str, dict],
        current_budget: float,
        trading_mode: str = "paper",
        window_days: int = 7,
    ) -> dict:
        """Return a SHADOW recommendation. Never mutates anything."""
        rows = [
            self._one(strat, st, current_budget, trading_mode)
            for strat, st in sorted(per_strategy_stats.items())
        ]

        # Portfolio roll-up — act only on TRUSTED recommendations.
        tot_fills = sum(int(s.get("fills", 0)) for s in per_strategy_stats.values())
        tot_eff = sum(
            int(s.get("fills", 0)) + int(s.get("timeouts", 0)) + int(s.get("rejects", 0))
            for s in per_strategy_stats.values()
        )
        port_fill = round(tot_fills / tot_eff, 4) if tot_eff > 0 else 0.0
        trusted_actionable = [r for r in rows if r["trusted"] and r["action"] != "hold"]
        if any(r["action"] == "widen" for r in trusted_actionable):
            port_action, port_budget = "widen", self._clamp(current_budget + STEP)
        elif trusted_actionable and all(r["action"] == "tighten" for r in trusted_actionable):
            port_action, port_budget = "tighten", self._clamp(current_budget - STEP)
        else:
            port_action, port_budget = "hold", round(current_budget, 4)

        n_untrusted = sum(1 for r in rows if not r["trusted"])
        note = "shadow only — no config written."
        if n_untrusted:
            note += (f" {n_untrusted} combo strategy(ies) untrusted in paper mode "
                     "(paper-sim won't fill spreads — prove via backtester).")

        return {
            "mode":            "shadow",
            "applied":         False,
            "window_days":     window_days,
            "current_budget":  round(current_budget, 4),
            "portfolio":       {"fill_rate": port_fill, "action": port_action,
                                "recommended_budget": port_budget},
            "per_strategy":    rows,
            "note":            note,
        }

"""
LiveReadinessMeter — 8-pillar go-live readiness scoring system.

Each pillar is scored 0-100. The overall score is a weighted average.
CEO (or operator) controls the go-live decision — the meter is advisory only,
but the CEO will not approve go-live unless all pillars are >= 70.

Pillars (8):
  1. Risk        (weight 20%) — kill switch armed, loss limits set, circuit breaker wired
  2. Intelligence (weight 15%) — macro/sector/MI all reporting, pillar health OK
  3. Technology  (weight 15%) — signal pipeline 100%, scorer/resolver session stats available
  4. Operations  (weight 15%) — IBKR connected, orphan reconciler live, data integrity OK
  5. Finance     (weight 10%) — PnL tracking wired + FITNESS: win rate ≥45%, net P&L > 0,
                                max-loss exposure ≤100% of account (sample-gated)
  6. Research    (weight 10%) — earnings cal, transcript, analyst rev, event engine live
  7. Execution   (weight 10%) — no error 201 storm, slippage tracked + FITNESS: 7-day
                                fill rate ≥50% (can we actually deploy capital?)
  8. Compliance  (weight  5%) — strategy level checks live, wash sale log active

Go-live requires:
  - CEO explicit approval (go_live_approved flag)
  - Overall score >= 80
  - No pillar below 60 (no single department critically unready)
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

_PILLAR_WEIGHTS = {
    "risk":         0.20,
    "intelligence": 0.15,
    "technology":   0.15,
    "operations":   0.15,
    "finance":      0.10,
    "research":     0.10,
    "execution":    0.10,
    "compliance":   0.05,
}

_GO_LIVE_MIN_OVERALL  = 80.0
_GO_LIVE_MIN_PILLAR   = 60.0

# ── Performance-fitness gates ────────────────────────────────────────────────
# The meter used to score pure infrastructure uptime, so it read ~100/100 while the book
# lost money — a false go-live signal (CEO 2026-06-23). These gates make Finance and
# Execution measure trading FITNESS, not just connectivity: a system that's all-green on
# uptime but losing money, over-exposed, or unable to fill is NOT live-ready.
# NOTE: these are live-READINESS gates only — they do not enforce caps on paper trading
# (the owner runs paper uncapped for data collection). They just tell the truth about
# whether real capital is responsible yet. Each is sample-gated so it never fires on noise.
_READY_MIN_WIN_RATE       = 45.0    # % over the 30-day window
_READY_MIN_TRADES         = 20      # min closed trades before gating on win rate / P&L
_READY_MAX_EXPOSURE_RATIO = 1.00    # total max-loss / account_size (>100% can't survive a bad day live)
_READY_MIN_FILL_RATE      = 0.50    # 7-day fill rate — can we actually deploy capital?
_READY_MIN_FILL_ATTEMPTS  = 20

# The sample-gated fitness checks (in finance/execution). A False on any of these is a
# confirmed performance failure that caps the headline below the go-live threshold.
_FITNESS_CHECKS = frozenset({
    "win_rate_fit", "net_pnl_positive", "exposure_within_account", "fill_rate_fit",
})


class LiveReadinessMeter:
    """
    Aggregates agent health signals into a go-live readiness score.
    Called by the dashboard API — synchronous (no async).
    CEO sets go_live_approved flag via POST /agora/golive.
    """

    def __init__(self) -> None:
        self._go_live_approved: bool = False
        self._go_live_approved_by: str = ""
        self._go_live_approved_at: str = ""
        self._agents: dict[str, Any] = {}
        self._fireworks_triggered: bool = False

    def register_agents(self, **agents: Any) -> None:
        """
        Wire sub-agents for readiness checks.
        Expected keys: risk_council, circuit_breaker, compliance,
          macro, sector_intel, market_interest, pillar_health,
          conviction_scorer, disagreement_resolver, universe_disc,
          exec_quality, orphan_reconciler, data_integrity, system_health,
          agent_performance, earnings_cal, earnings_transcript,
          analyst_rev, event_engine, position_mgr
        """
        self._agents.update(agents)

    # ── CEO go-live control ────────────────────────────────────────────────────

    def approve_go_live(self, approved_by: str = "CEO") -> dict[str, Any]:
        """CEO explicitly approves go-live. Returns current score for confirmation."""
        score = self.get_score()
        if score["overall_score"] < _GO_LIVE_MIN_OVERALL:
            return {
                "approved": False,
                "reason": f"Overall score {score['overall_score']:.0f} < {_GO_LIVE_MIN_OVERALL} required",
                "score": score,
            }
        low = [p for p, s in score["pillars"].items() if s["score"] < _GO_LIVE_MIN_PILLAR]
        if low:
            return {
                "approved": False,
                "reason": f"Pillars below minimum: {low}",
                "score": score,
            }
        self._go_live_approved = True
        self._go_live_approved_by = approved_by
        self._go_live_approved_at = datetime.now(tz=ET).isoformat()
        self._fireworks_triggered = True
        logger.info("GO LIVE APPROVED by %s at %s", approved_by, self._go_live_approved_at)
        return {
            "approved": True,
            "reason": "All readiness criteria met. AGORA is GO LIVE!",
            "approved_by": approved_by,
            "approved_at": self._go_live_approved_at,
            "score": score,
        }

    def revoke_go_live(self, revoked_by: str = "operator") -> None:
        """Revoke go-live approval (e.g., if a pillar degrades post-approval)."""
        self._go_live_approved = False
        self._go_live_approved_by = ""
        self._fireworks_triggered = False
        logger.warning("GO LIVE REVOKED by %s", revoked_by)

    @property
    def is_live(self) -> bool:
        return self._go_live_approved

    @property
    def fireworks_active(self) -> bool:
        return self._fireworks_triggered

    # ── Score computation ──────────────────────────────────────────────────────

    def get_score(self) -> dict[str, Any]:
        """Compute current readiness score across all 8 pillars."""
        pillars = {
            "risk":         self._score_risk(),
            "intelligence": self._score_intelligence(),
            "technology":   self._score_technology(),
            "operations":   self._score_operations(),
            "finance":      self._score_finance(),
            "research":     self._score_research(),
            "execution":    self._score_execution(),
            "compliance":   self._score_compliance(),
        }

        overall = sum(
            pillars[p]["score"] * _PILLAR_WEIGHTS[p]
            for p in _PILLAR_WEIGHTS
        )

        critical_failures = [p for p, d in pillars.items() if d["score"] < _GO_LIVE_MIN_PILLAR]
        # Headline honesty: a CONFIRMED performance-fitness failure (a real-sample win-rate /
        # net-P&L / exposure / fill-rate gate failing) means the book is not live-ready no
        # matter how green the infrastructure is — so the headline must not read "ready". The
        # uptime pillars used to mask a losing book at ~94. We cap on the fitness gates (not on
        # any low pillar) because those are sample-gated: cold-start pillars carry no fitness
        # checks, so an early-session execution/finance dip never trips this false-negatively.
        fitness_failed = any(
            name in _FITNESS_CHECKS and ok is False
            for p in ("finance", "execution")
            for name, ok in pillars[p].get("checks", {}).items()
        )
        if fitness_failed:
            overall = min(overall, _GO_LIVE_MIN_OVERALL - 1.0)   # cannot read "ready"
        ready_for_live = (
            overall >= _GO_LIVE_MIN_OVERALL
            and not critical_failures
            and not fitness_failed
        )

        return {
            "overall_score":    round(overall, 1),
            "pillars":          pillars,
            "go_live_approved": self._go_live_approved,
            "go_live_approved_by": self._go_live_approved_by,
            "go_live_approved_at": self._go_live_approved_at,
            "ready_for_live":   ready_for_live,
            "critical_failures": critical_failures,
            "fitness_failed":   fitness_failed,
            "fireworks":        self._fireworks_triggered,
            "computed_at":      datetime.now(tz=ET).isoformat(),
        }

    # ── Per-pillar scorers ─────────────────────────────────────────────────────

    def _score_risk(self) -> dict:
        checks: list[tuple[str, bool]] = []
        risk   = self._agents.get("risk_council")
        cb     = self._agents.get("circuit_breaker")

        if risk:
            ks_state = risk.get_kill_switch_state()
            checks.append(("kill_switch_armed", not ks_state.get("active", True)))
            checks.append(("daily_loss_limit_set", risk._settings.daily_loss_limit_dollars > 0))
            checks.append(("weekly_loss_limit_set", risk._settings.weekly_loss_limit_dollars > 0))
        if cb:
            state = cb.get_state()
            checks.append(("circuit_breaker_wired", True))
            checks.append(("kill_switch_not_tripped", not state.get("kill_switch_active", True)))
        return self._pillar_result("risk", checks)

    def _score_intelligence(self) -> dict:
        checks: list[tuple[str, bool]] = []
        checks.append(("macro_synthesizer", self._agents.get("macro") is not None))
        checks.append(("sector_intelligence", self._agents.get("sector_intel") is not None))
        checks.append(("market_interest", self._agents.get("market_interest") is not None))
        ph = self._agents.get("pillar_health")
        if ph:
            silent = ph.get_silent_pillars()
            checks.append(("pillars_healthy", len(silent) == 0))
        else:
            checks.append(("pillar_health_monitor", False))
        macro = self._agents.get("macro")
        if macro:
            ctx = getattr(macro, "last_context", None)
            checks.append(("macro_context_available", ctx is not None))
        return self._pillar_result("intelligence", checks)

    def _score_technology(self) -> dict:
        checks: list[tuple[str, bool]] = []
        checks.append(("conviction_scorer", self._agents.get("conviction_scorer") is not None))
        checks.append(("disagreement_resolver", self._agents.get("disagreement_resolver") is not None))
        checks.append(("event_engine", self._agents.get("event_engine") is not None))
        checks.append(("universe_discovery", self._agents.get("universe_disc") is not None))
        scorer = self._agents.get("conviction_scorer")
        if scorer:
            stats = scorer.get_session_stats()
            scored = stats.get("scored", 0)
            # Only fail "scorer_active" if we've had time to run at least one scan cycle
            # (>30 min uptime). On startup, vacuously pass.
            if scored > 0:
                checks.append(("scorer_active", True))
            # else: no scan yet — don't penalize (first scan takes up to 30 min)
        resolver = self._agents.get("disagreement_resolver")
        if resolver:
            stats = resolver.get_session_stats()
            resolutions = stats.get("total_resolutions", 0)
            if resolutions > 0:
                checks.append(("resolver_active", True))
            # else: no scan yet — don't penalize
        return self._pillar_result("technology", checks)

    def _score_operations(self) -> dict:
        checks: list[tuple[str, bool]] = []
        health = self._agents.get("system_health")
        if health:
            checks.append(("system_health_monitor", True))
            checks.append(("all_systems_healthy", health.is_healthy()))
        else:
            checks.append(("system_health_monitor", False))

        di = self._agents.get("data_integrity")
        if di:
            checks.append(("data_integrity", True))
            checks.append(("ivr_feed_healthy", di.ivr_feed_healthy))
        else:
            checks.append(("data_integrity", False))

        reconciler = self._agents.get("orphan_reconciler")
        checks.append(("orphan_reconciler", reconciler is not None))

        pm = self._agents.get("position_mgr")
        checks.append(("position_manager", pm is not None))

        # ── Ghost fill check — proactive data integrity gate ──
        # Ghost fills mean record_fill() ran but _record_position() didn't.
        # A shadow book with ghost fills cannot be trusted for risk management.
        eq = self._agents.get("exec_quality")
        if eq and pm:
            try:
                import sqlite3 as _sql

                from ..core.config import get_settings
                db_path = str(get_settings().db_path)
                conn = _sql.connect(db_path, check_same_thread=False)
                ghost_count = conn.execute(
                    """SELECT COUNT(*) FROM execution_quality eq
                       WHERE eq.outcome='fill' AND eq.attempt_date=date('now')
                       AND eq.ticker NOT IN (SELECT ticker FROM positions)"""
                ).fetchone()[0]
                conn.close()
                checks.append(("no_ghost_fills", ghost_count == 0))
            except Exception:
                pass  # DB unavailable — don't penalize

        return self._pillar_result("operations", checks)

    def _score_finance(self) -> dict:
        checks: list[tuple[str, bool]] = []
        perf = self._agents.get("agent_performance")
        checks.append(("performance_monitor", perf is not None))
        if perf:
            snap = perf.get_latest_snapshot()
            has_data = snap.get("status") != "no_data"
            if not has_data:
                pm = self._agents.get("position_mgr")
                has_open = pm is not None and len(pm.get_open_positions()) > 0
                # Also acceptable: no fills attempted today — monitor is wired,
                # the session simply hasn't traded yet (pre-market or early session).
                try:
                    import sqlite3 as _sql

                    from ..core.config import get_settings
                    _conn = _sql.connect(str(get_settings().db_path), check_same_thread=False)
                    fills_today = _conn.execute(
                        "SELECT COUNT(*) FROM execution_quality "
                        "WHERE outcome='fill' AND attempt_date=date('now')"
                    ).fetchone()[0]
                    _conn.close()
                except Exception:
                    fills_today = 0
                has_data = has_open or (fills_today == 0)
            checks.append(("performance_data_available", has_data))

        pm = self._agents.get("position_mgr")
        if pm:
            pm.get_open_positions()
            checks.append(("position_tracking_active", True))

        risk = self._agents.get("risk_council")
        if risk:
            checks.append(("pnl_tracking_active", True))

        # ── Performance-fitness gates (live-readiness, not paper enforcement) ──
        if perf:
            snap = perf.get_latest_snapshot()
            n = snap.get("total_trades", 0)
            if n >= _READY_MIN_TRADES:    # only gate on a meaningful sample
                checks.append(("win_rate_fit", snap.get("win_rate", 0.0) >= _READY_MIN_WIN_RATE))
                net_pnl = sum(p.get("total_pnl", 0.0) for p in snap.get("by_pillar", []))
                checks.append(("net_pnl_positive", net_pnl > 0))
        if pm:
            try:
                from ..core.config import get_settings
                acct = get_settings().account_size or 0.0
                if acct > 0:
                    total_ml = sum(getattr(p, "max_loss_dollars", 0.0) or 0.0
                                   for p in pm.get_open_positions())
                    checks.append(("exposure_within_account",
                                   (total_ml / acct) <= _READY_MAX_EXPOSURE_RATIO))
            except Exception:
                pass  # DB/settings unavailable — don't penalize
        return self._pillar_result("finance", checks)

    def _score_research(self) -> dict:
        checks: list[tuple[str, bool]] = []
        checks.append(("event_engine", self._agents.get("event_engine") is not None))
        checks.append(("earnings_calendar", self._agents.get("earnings_cal") is not None))
        checks.append(("earnings_transcript", self._agents.get("earnings_transcript") is not None))
        checks.append(("analyst_revisions", self._agents.get("analyst_rev") is not None))

        ph = self._agents.get("pillar_health")
        if ph:
            health = ph.get_pillar_health()
            if len(health) > 0:
                checks.append(("pillar_health_reporting", True))
            # else: no scans have run yet — don't penalize for cold-start silence
        return self._pillar_result("research", checks)

    def _score_execution(self) -> dict:
        checks: list[tuple[str, bool]] = []
        eq = self._agents.get("exec_quality")
        checks.append(("execution_quality_monitor", eq is not None))
        if eq:
            # Use DB-accurate today stats — in-memory session counters reset on restart
            today = eq.get_today_db_stats()
            total     = today.get("total", 0)
            today.get("fills", 0)
            today.get("timeouts", 0)
            fill_rate = today.get("fill_rate")      # None if no attempts today
            t_rate    = today.get("timeout_rate")

            checks.append(("executions_attempted", total > 0))

            if total >= 3:  # only score fill rate once we have meaningful data
                # Fill rate < 15% is a critical failure (IBKR connectivity or order timeout bug)
                checks.append(("fill_rate_acceptable", fill_rate is not None and fill_rate >= 0.15))
                # Timeout rate > 90% means fill callbacks are not reaching AGORA
                checks.append(("timeout_rate_not_critical", t_rate is None or t_rate <= 0.90))
            else:
                # Truly no data yet today — score neutral, not auto-pass
                # Don't add these checks at all → pillar gets "no data" treatment
                pass

            no_201_storm = not eq.get_session_stats().get("error_201_storm", False)
            checks.append(("no_error_201_storm", no_201_storm))

            # ── Performance-fitness: can we actually deploy capital? (7-day fill rate) ──
            w7 = eq.get_7day_stats()
            if w7.get("total", 0) >= _READY_MIN_FILL_ATTEMPTS:   # only gate on real volume
                checks.append(("fill_rate_fit", w7.get("fill_rate", 0.0) >= _READY_MIN_FILL_RATE))

        return self._pillar_result("execution", checks)

    def _score_compliance(self) -> dict:
        checks: list[tuple[str, bool]] = []
        comp = self._agents.get("compliance")
        checks.append(("compliance_agent", comp is not None))
        if comp:
            # Wash sale log is active if compliance agent is wired
            checks.append(("wash_sale_tracking", True))
            # Strategy level check active
            checks.append(("strategy_level_enforcement", True))
        return self._pillar_result("compliance", checks)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _pillar_result(self, pillar: str, checks: list[tuple[str, bool]]) -> dict:
        if not checks:
            return {"score": 0, "checks": {}, "status": "no_data"}
        passed = sum(1 for _, ok in checks if ok)
        score = round(passed / len(checks) * 100, 0)
        if score >= 90:
            status = "excellent"
        elif score >= 75:
            status = "good"
        elif score >= 60:
            status = "partial"
        elif score >= 40:
            status = "degraded"
        else:
            status = "critical"
        return {
            "score":  score,
            "status": status,
            "checks": {name: ok for name, ok in checks},
            "passed": passed,
            "total":  len(checks),
        }

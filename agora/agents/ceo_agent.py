"""
CEOAgent — autonomous daily operator of AGORA.

You (Rahul) are the Chairman/Owner.
This agent is the CEO: it runs daily operations, reports to you,
and acts autonomously within the risk bounds you've set.

Daily schedule:
  6:00 AM ET  — Morning brief: positions, what's coming, macro setup
  10:00 AM ET — Mid-morning check: any new positions, alerts, market conditions
  1:00 PM ET  — Midday status: P&L, risk, any regime shifts
  4:30 PM ET  — EOD report: full day summary, tomorrow's watchlist
  (on demand)  — Alert dispatch when critical events happen

The CEO makes autonomous decisions within these bounds:
  - Can approve/veto any trade that has cleared the risk council
  - Can raise the daily loss alert threshold (not actual loss limit — that's yours)
  - Can prioritize tickers for next scan cycle
  - Can trigger intraday macro refresh
  - CANNOT change position sizing, loss limits, or strategy parameters

Reporting channel: Discord webhook (if configured) + structured log file.
Claude model: Opus 4.7 with adaptive thinking for synthesis tasks.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import anthropic
import httpx
from pydantic import BaseModel, ValidationError

from ..core.config import AgoraSettings, get_settings
from ..core.session_plan import SessionPlan
from ..ops.llm_cost_log import log_call as _log_llm, log_message as _log_msg


class _SessionPlanSchema(BaseModel):
    """Pydantic schema for validating CEO board meeting SessionPlan JSON output."""
    stance: str = "neutral"
    macro_regime: str = "neutral"
    size_bias: str = "full"
    max_new_positions: int = 3
    preferred_pillar: str = "any"
    close_targets: list[str] = []
    risk_budget_remaining: float = 500.0
    notes: str = ""

if TYPE_CHECKING:
    from ..lifecycle.position_manager import PositionManager
    from ..risk.risk_council import RiskCouncil

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")


def _chunk_for_discord(message: str, limit: int = 1900) -> list[str]:
    """Split a report into <=limit-char chunks WITHOUT cutting a line in half (Discord rejects
    >2000-char posts with a 400, so an unsplit report is silently lost). Packs whole lines into a
    chunk; a single line longer than `limit` (rare) is hard-sliced as a last resort."""
    if len(message) <= limit:
        return [message] if message else []
    chunks: list[str] = []
    buf = ""
    for line in message.split("\n"):
        # A single over-long line can't fit any chunk — flush, then hard-slice it.
        if len(line) > limit:
            if buf:
                chunks.append(buf)
                buf = ""
            for i in range(0, len(line), limit):
                chunks.append(line[i:i + limit])
            continue
        candidate = line if not buf else buf + "\n" + line
        if len(candidate) > limit:
            chunks.append(buf)
            buf = line
        else:
            buf = candidate
    if buf:
        chunks.append(buf)
    return chunks

_CEO_SYSTEM_PROMPT = """\
You are the CEO Agent of AGORA, an autonomous options trading platform owned by Rahul.

Your role:
- You report directly to Rahul (Chairman/Owner)
- You run daily operations: morning brief, midday checks, EOD report
- You write concise, actionable, data-driven reports
- You flag risks proactively — never hide bad news
- You think like a senior quant trader, not a programmer

Your personality:
- Direct and precise — no fluff, no filler
- Numbers-first: lead with P&L, win rate, risk metrics
- Honest about uncertainty — say "unclear" not "might be"
- Flag concerns clearly: use ⚠️ for warnings, ✅ for all-clear, 🚨 for critical

Format: Discord-ready markdown. Keep each report under 1200 characters.
"""


class CEOAgent:
    """
    Autonomous daily operator that synthesizes all system state and reports to the owner.

    Wired into AgoraSession at startup. Runs on its own asyncio task.
    Reads state through callbacks/references — does NOT write to DB or submit orders.
    """

    def __init__(
        self,
        settings: AgoraSettings | None = None,
        position_mgr: Any = None,   # PositionManager
        risk_council: Any = None,   # RiskCouncil
        # C-suite direct reports
        cro: Any = None,
        cio: Any = None,
        cto: Any = None,
        coo: Any = None,
        cfo: Any = None,
        rnd: Any = None,
        ctech: Any = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._position_mgr = position_mgr
        self._risk_council = risk_council
        self._client = anthropic.AsyncAnthropic(api_key=self._settings.anthropic_api_key)
        self._running = False

        # C-suite direct reports (set after instantiation to avoid circular deps)
        self._cro   = cro
        self._cio   = cio
        self._cto   = cto
        self._coo   = coo
        self._cfo   = cfo
        self._rnd   = rnd
        self._ctech = ctech

        # Live readiness meter ref (wired in after session init)
        self._readiness: Any = None

        # Track which reports have fired today
        self._fired_today: set[str] = set()
        self._last_report_date: date | None = None
        self._last_patrol_et: datetime | None = None   # integrity patrol cadence

        # Upcoming earnings known to the CEO (populated by EarningsCalendarAgent callback)
        self._upcoming_earnings: list[dict] = []

        # Reference to exec quality agent (wired after session init)
        self._exec_quality: Any = None

        # ── Session Plan — published after morning board meeting ──────
        self._session_plan: SessionPlan = SessionPlan.default()

        # ── Trade feedback ledger — updated whenever a position closes ──
        # Keyed by conviction gate → list of realized P&L values
        self._gate_pnl_ledger: dict[str, list[float]] = {
            "high": [], "standard": [], "low": []
        }
        # Per-macro-stance outcomes (for macro accuracy tracking)
        self._macro_pnl_ledger: dict[str, list[float]] = {}
        # Total closed trades processed this session
        self._closed_trades_processed: int = 0

        # ── Autonomous action callbacks (wired by session post-init) ──────────
        # Callable[[ticker, reason], Awaitable[None]] — closes an open position
        self._close_position_cb: Any = None
        # Callable[[reason], Awaitable[None]] — triggers macro synthesis refresh
        self._macro_refresh_cb: Any = None

        # Alert dedup — suppress repeat dispatches of the same key within 10 min
        self._alert_last_fired: dict[str, datetime] = {}
        self._ALERT_COOLDOWN_SECS = 600

    def wire_exec_quality(self, eq: Any) -> None:
        self._exec_quality = eq

    def wire_c_suite(
        self,
        cro: Any = None, cio: Any = None, cto: Any = None,
        coo: Any = None, cfo: Any = None, rnd: Any = None,
        ctech: Any = None,
    ) -> None:
        """Wire C-suite agents post-instantiation (avoids circular deps in session.py)."""
        if cro:   self._cro   = cro
        if cio:   self._cio   = cio
        if cto:   self._cto   = cto
        if coo:   self._coo   = coo
        if cfo:   self._cfo   = cfo
        if rnd:   self._rnd   = rnd
        if ctech: self._ctech = ctech

    def wire_readiness(self, readiness: Any) -> None:
        """Wire the LiveReadinessMeter so CEO can approve go-live."""
        self._readiness = readiness

    def wire_close_callback(self, fn: Any) -> None:
        """Register session callback: async fn(ticker, reason) → closes an open position."""
        self._close_position_cb = fn

    def wire_macro_refresh_callback(self, fn: Any) -> None:
        """Register session callback: async fn(reason) → triggers macro synthesis refresh."""
        self._macro_refresh_cb = fn

    # ── Autonomous alert handling ──────────────────────────────────────────────

    async def _autonomous_response(self, level: str, message: str) -> str:
        """
        Classify an incoming C-suite alert and take corrective action without
        waiting for human approval.

        Returns a one-line description of the action taken (included in the
        Discord notification so the owner can see what the CEO did).

        Action map:
          stop-loss proximity / at stop   → force-close the position NOW
          macro context stale/missing     → trigger macro synthesis refresh
          Greek limit breached            → acknowledged (CRO self_heal already
                                            published size_bias_changed event)
          kill switch / circuit breaker   → acknowledged (already enforced)
          daily loss critical             → acknowledged (CRO reduced size to 50%)
          IBKR disconnect                 → acknowledged (auto-reconnects)
          execution / fill rate           → acknowledged (COO/CTO self_heal active)
          everything else                 → logged, no additional action
        """
        msg_lower = message.lower()

        # ── 1. Stop-loss proximity → close the position immediately ───────────
        if any(k in msg_lower for k in ("stop_loss_proximity", "stop loss proximity",
                                         "approaching stop", "near stop")):
            # Extract ticker from message — pattern "[Chief Risk Officer (CRO)] … TICKER …"
            ticker = self._extract_ticker_from_alert(message)
            if ticker and self._close_position_cb:
                try:
                    await self._close_position_cb(ticker, "CEO autonomous: stop-loss proximity — forced close")
                    return f"✅ Closed {ticker} position (stop-loss proximity — CEO forced exit)"
                except Exception as exc:
                    logger.error("CEO autonomous close failed for %s: %s", ticker, exc)
                    return f"⚠️ Close attempt for {ticker} failed: {exc}"
            return "⚠️ Stop-loss proximity detected but no close callback wired or ticker unresolvable"

        # ── 2. Macro context stale / missing → refresh now ────────────────────
        if any(k in msg_lower for k in ("macro_context_stale", "macro_context_missing",
                                         "macro context stale", "macro context missing",
                                         "macro context aging")):
            if self._macro_refresh_cb:
                try:
                    asyncio.create_task(self._macro_refresh_cb("CEO autonomous: stale macro context"))
                    return "✅ Triggered macro synthesis refresh (stale context)"
                except Exception as exc:
                    return f"⚠️ Macro refresh trigger failed: {exc}"
            return "ℹ️ Macro context stale — no refresh callback wired"

        # ── 3. Greek/delta/vega limit breach → CRO self_heal already acted ────
        if any(k in msg_lower for k in ("delta_limit_breached", "vega_limit_breached",
                                         "delta limit", "vega limit")):
            return "ℹ️ Acknowledged — CRO published size_bias=none to block new directional entries"

        # ── 4. Kill switch / circuit breaker → already enforced ───────────────
        if any(k in msg_lower for k in ("kill_switch", "kill switch", "circuit_breaker",
                                         "circuit breaker")):
            return "ℹ️ Acknowledged — kill switch/circuit breaker already enforced by risk layer"

        # ── 5. Daily loss → CRO already reduced sizing ────────────────────────
        if any(k in msg_lower for k in ("daily_loss_critical", "daily loss critical")):
            return "ℹ️ Acknowledged — CRO reduced position sizing to 50% automatically"

        if any(k in msg_lower for k in ("daily_loss_high", "daily loss high")):
            return "ℹ️ Acknowledged — monitoring daily loss trajectory"

        # ── 6. Pillar paused / strategy health ────────────────────────────────
        if any(k in msg_lower for k in ("pillar", "strategy_health", "strategy health",
                                         "underperforming", "paused")):
            return "ℹ️ Acknowledged — StrategyHealth agent managing pillar pause autonomously"

        # ── 7. IBKR / COO ops issues → auto-reconnects ────────────────────────
        if any(k in msg_lower for k in ("ibkr disconnect", "ibkr reconnect",
                                         "api connection failed", "ctech: ibkr")):
            return "ℹ️ Acknowledged — ib_insync auto-reconnect in progress"

        # ── 8. Execution / fill rate → CTO/COO managing ───────────────────────
        if any(k in msg_lower for k in ("fill rate", "execution quality", "orphan",
                                         "untracked")):
            return "ℹ️ Acknowledged — CTO/COO execution quality monitoring active"

        # ── 9. Conviction gate inversion → R&D flagged ────────────────────────
        if any(k in msg_lower for k in ("gate hierarchy", "conviction gate", "inverted")):
            return "ℹ️ Acknowledged — R&D agent will review conviction calibration"

        # ── Default: log and acknowledge ──────────────────────────────────────
        return "ℹ️ Logged — no automated corrective action for this alert class"

    def _extract_ticker_from_alert(self, message: str) -> str | None:
        """
        Heuristically extract a ticker symbol from a C-suite alert message.
        Looks for uppercase 1–5 letter tokens that are known open positions.
        """
        if not self._position_mgr:
            return None
        open_tickers = {p.ticker for p in self._position_mgr.get_open_positions()}
        import re
        for token in re.findall(r'\b([A-Z]{1,5})\b', message):
            if token in open_tickers:
                return token
        return None

    def get_session_plan(self) -> SessionPlan:
        """Return the current session plan (read by all C-suite via wire_session_plan)."""
        return self._session_plan

    def process_closed_trade_feedback(
        self,
        ticker: str,
        realized_pnl: float,
        conviction_at_entry: float | None,
        macro_stance_at_entry: str | None,
        strategy: str | None,
    ) -> None:
        """
        Called by PositionManager whenever a position closes.
        Accumulates outcome data for conviction gate calibration and macro accuracy.
        Fires calibration alerts when patterns emerge (no Claude call — synchronous).
        """
        self._closed_trades_processed += 1

        # Gate calibration ledger
        gate = "low"
        if conviction_at_entry:
            if conviction_at_entry >= 70:
                gate = "high"
            elif conviction_at_entry >= 55:
                gate = "standard"
        self._gate_pnl_ledger[gate].append(realized_pnl)

        # Macro stance ledger
        if macro_stance_at_entry:
            self._macro_pnl_ledger.setdefault(macro_stance_at_entry, []).append(realized_pnl)

        # Check gate hierarchy every 10 trades (enough signal to detect inversion)
        if self._closed_trades_processed % 10 == 0:
            asyncio.create_task(self._evaluate_gate_calibration())

    async def _evaluate_gate_calibration(self) -> None:
        """Alert if conviction gate hierarchy is inverted (high-gate losing vs low-gate)."""
        high  = self._gate_pnl_ledger["high"]
        std   = self._gate_pnl_ledger["standard"]
        low   = self._gate_pnl_ledger["low"]
        if len(high) < 5 or len(low) < 5:
            return
        avg_high = sum(high) / len(high)
        avg_std  = sum(std)  / len(std)  if std else 0
        avg_low  = sum(low)  / len(low)
        if avg_high <= avg_low:
            await self.dispatch_alert(
                "warning",
                f"⚠️ Conviction gate hierarchy INVERTED: high-gate avg P&L ${avg_high:.2f} "
                f"<= low-gate ${avg_low:.2f} over {len(high)+len(low)} trades. "
                f"ConvictionScorer may need recalibration — high scores not predicting better outcomes. "
                f"R&D review required.",
            )
        elif avg_std > avg_high and len(std) >= 5:
            await self.dispatch_alert(
                "warning",
                f"⚠️ Standard gate outperforming high gate: std=${avg_std:.2f} vs high=${avg_high:.2f}. "
                f"High-conviction sizing may be miscalibrated. R&D review required.",
            )

    async def start(self) -> None:
        self._running = True
        logger.info("CEOAgent started — reporting to owner Rahul")
        while self._running:
            now_et = datetime.now(tz=ET)
            today = now_et.date()

            # Reset daily fired set on new day
            if self._last_report_date != today:
                self._fired_today.clear()
                self._last_report_date = today

            h, m = now_et.hour, now_et.minute

            try:
                # Integrity patrol — every 30 min regardless of hour
                now_et_dt = datetime.now(tz=ET)
                if (
                    self._last_patrol_et is None
                    or (now_et_dt - self._last_patrol_et).total_seconds() >= 1800
                ):
                    self._last_patrol_et = now_et_dt
                    await self._integrity_patrol()

                # 6:00 AM — C-suite roundtable → SessionPlan published → morning brief
                if h == 6 and m == 0 and "morning" not in self._fired_today:
                    self._fired_today.add("morning")
                    await self._morning_board_meeting()   # publishes SessionPlan
                    await self._morning_brief()

                # 10:00 AM — mid-morning check
                elif h == 10 and m == 0 and "midmorning" not in self._fired_today:
                    self._fired_today.add("midmorning")
                    await self._midmorning_check()

                # 1:00 PM — midday status + C-suite ops check
                elif h == 13 and m == 0 and "midday" not in self._fired_today:
                    self._fired_today.add("midday")
                    await self._csuite_roundtable("midday")
                    await self._midday_status()

                # 3:30 PM — Afternoon plan review (update SessionPlan for end-of-day)
                elif h == 15 and m == 30 and "afternoon_plan" not in self._fired_today:
                    self._fired_today.add("afternoon_plan")
                    await self._afternoon_plan_review()

                # 4:30 PM — EOD report
                elif h == 16 and m == 30 and "eod" not in self._fired_today:
                    self._fired_today.add("eod")
                    await self._eod_report()

            except Exception as exc:
                logger.error("CEOAgent report failed: %s", exc)

            await asyncio.sleep(60)

    async def stop(self) -> None:
        self._running = False

    def register_earnings_setup(self, ticker: str, earnings_date: date, direction: str, confidence: float, reasoning: str) -> None:
        """Called by EarningsCalendarAgent to inform CEO of upcoming earnings plays."""
        self._upcoming_earnings.append({
            "ticker": ticker,
            "date": str(earnings_date),
            "direction": direction,
            "confidence": confidence,
            "reasoning": reasoning,
        })

    # ── C-suite management ─────────────────────────────────────────

    async def _morning_board_meeting(self) -> None:
        """
        6:00 AM: Full board meeting → synthesize a SessionPlan for the day.
        Every C-suite exec presents; CEO publishes a concrete operating directive.
        This replaces the old _csuite_roundtable("morning") call.
        """
        executives = {
            "CRO": self._cro, "CIO": self._cio, "CTO": self._cto,
            "COO": self._coo, "CFO": self._cfo, "R&D": self._rnd, "CTech": self._ctech,
        }
        briefs: dict[str, str] = {}
        readiness_tasks: list[str] = []
        for name, agent in executives.items():
            if agent is None:
                continue
            try:
                briefs[name] = await agent.produce_brief()
                tasks = agent.get_readiness_tasks() if hasattr(agent, "get_readiness_tasks") else []
                if tasks:
                    readiness_tasks.extend([f"[{name}] {t}" for t in tasks])
            except Exception as exc:
                briefs[name] = f"[brief failed: {exc}]"

        briefs_str = "\n\n".join(f"**{n}**: {b}" for n, b in briefs.items())
        tasks_str  = "\n".join(f"  - {t}" for t in readiness_tasks[:10]) if readiness_tasks else "  None"

        state = self._collect_state()
        ks_active = state.get("kill_switch_active", False)
        realized  = state.get("realized_pnl_today", 0.0)
        daily_lim = self._settings.daily_loss_limit_dollars or 500
        loss_pct  = abs(realized) / daily_lim if realized < 0 else 0

        prompt = (
            f"Morning board meeting. Today: {date.today().strftime('%A %b %d %Y')}.\n\n"
            f"C-suite briefs:\n{briefs_str}\n\n"
            f"Readiness gap tasks identified by agents:\n{tasks_str}\n\n"
            f"Kill switch active: {ks_active}. "
            f"Daily loss so far: ${realized:.2f} ({loss_pct:.0%} of ${daily_lim:.0f} limit).\n\n"
            f"Based on this information, produce a JSON SessionPlan with EXACTLY these fields:\n"
            f'{{"stance": "aggressive|neutral|defensive|halted", '
            f'"macro_regime": "risk_on|neutral|risk_off|crisis", '
            f'"size_bias": "full|half|quarter|none", '
            f'"max_new_positions": <0-5 int>, '
            f'"preferred_pillar": "vol_premium|catalyst|directional|event_fomc|any", '
            f'"close_targets": [<list of tickers to prioritize closing today>], '
            f'"risk_budget_remaining": <float dollars>, '
            f'"notes": "<1-2 sentence CEO rationale>"}}\n\n'
            f"Output ONLY the JSON, no prose before or after."
        )
        raw = await self._synthesize(
            prompt,
            "Morning Board Meeting SessionPlan",
            effort="xhigh",
            thinking_display="summarized",
            task_budget_tokens=20000,
        )

        # Parse and validate SessionPlan using Pydantic (replaces fragile regex)
        plan = SessionPlan.default()
        try:
            import re as _re
            m = _re.search(r"\{.*\}", raw, _re.DOTALL)
            if m:
                data = json.loads(m.group())
                validated = _SessionPlanSchema.model_validate(data)
                plan = SessionPlan(
                    date=date.today().isoformat(),
                    stance=validated.stance,
                    macro_regime=validated.macro_regime,
                    size_bias=validated.size_bias,
                    max_new_positions=validated.max_new_positions,
                    preferred_pillar=validated.preferred_pillar,
                    close_targets=validated.close_targets,
                    risk_budget_remaining=validated.risk_budget_remaining,
                    notes=validated.notes,
                )
        except ValidationError as exc:
            logger.warning("CEO morning board meeting: SessionPlan schema validation failed: %s", exc)
        except Exception as exc:
            logger.warning("CEO morning board meeting: failed to parse SessionPlan JSON: %s", exc)

        self._session_plan = plan
        logger.info(
            "SessionPlan published: stance=%s size_bias=%s max_positions=%d notes=%s",
            plan.stance, plan.size_bias, plan.max_new_positions, plan.notes[:100]
        )

        icon = "🌅"
        summary = (
            f"{icon} **CEO Morning Board Meeting** — {datetime.now(tz=ET).strftime('%H:%M ET')}\n\n"
            f"**Session Plan Published:**\n"
            f"  Stance: `{plan.stance}` | Size: `{plan.size_bias}` | "
            f"Max positions: `{plan.max_new_positions}` | Pillar: `{plan.preferred_pillar}`\n"
            f"  Close targets: {plan.close_targets or 'none'}\n"
            f"  Budget: ${plan.risk_budget_remaining:.0f}\n"
            f"  CEO notes: {plan.notes}\n"
        )
        if readiness_tasks:
            summary += f"\n**Readiness tasks for today:** {len(readiness_tasks)} gaps identified"
        await self._send_to_discord(summary)

    async def _afternoon_plan_review(self) -> None:
        """
        3:30 PM: Review session plan against actual performance.
        Update SessionPlan stance for end-of-day (conservative if losses, close targets if DTE near).
        """
        state = self._collect_state()
        realized = state.get("realized_pnl_today", 0.0)
        daily_lim = self._settings.daily_loss_limit_dollars or 500
        open_pos = state.get("open_positions", 0)

        old_plan = self._session_plan

        # Auto-tighten: if day's realized loss > 50% of limit, flip to defensive
        loss_pct = abs(realized) / daily_lim if realized < 0 else 0
        if loss_pct >= 0.85:
            new_stance = "halted"
            new_size   = "none"
            new_max    = 0
        elif loss_pct >= 0.60:
            new_stance = "defensive"
            new_size   = "half"
            new_max    = max(0, old_plan.max_new_positions - 1)
        else:
            new_stance = old_plan.stance
            new_size   = old_plan.size_bias
            new_max    = old_plan.max_new_positions

        # Find positions close to 21 DTE or profit target (auto add to close_targets)
        close_targets = list(old_plan.close_targets)
        if self._position_mgr:
            try:
                from datetime import date as _date
                today = _date.today()
                for p in self._position_mgr.get_open_positions():
                    try:
                        expiry = p.expiry_date if isinstance(p.expiry_date, _date) \
                            else _date.fromisoformat(str(p.expiry_date))
                        if (expiry - today).days <= 22 and p.ticker not in close_targets:
                            close_targets.append(p.ticker)
                    except Exception:
                        pass
            except Exception:
                pass

        self._session_plan = SessionPlan(
            date=old_plan.date,
            stance=new_stance,
            macro_regime=old_plan.macro_regime,
            size_bias=new_size,
            max_new_positions=new_max,
            preferred_pillar=old_plan.preferred_pillar,
            close_targets=close_targets,
            risk_budget_remaining=max(0.0, daily_lim + realized),
            notes=f"Afternoon update: realized P&L ${realized:.2f} ({loss_pct:.0%} of limit). "
                  f"Stance {'tightened' if new_stance != old_plan.stance else 'unchanged'}.",
            version=old_plan.version + 1,
        )
        logger.info(
            "SessionPlan updated [v%d]: stance=%s → %s, close_targets=%s",
            self._session_plan.version, old_plan.stance, new_stance, close_targets
        )
        summary = (
            f"📋 **Afternoon Plan Update** — {datetime.now(tz=ET).strftime('%H:%M ET')}\n"
            f"  Stance: `{old_plan.stance}` → `{new_stance}` | "
            f"Size: `{old_plan.size_bias}` → `{new_size}`\n"
            f"  Close targets: {close_targets or 'none'} | "
            f"Budget remaining: ${self._session_plan.risk_budget_remaining:.0f}\n"
            f"  {self._session_plan.notes}"
        )
        await self._send_to_discord(summary)

    async def _csuite_roundtable(self, session: str = "morning") -> None:
        """
        Collect a brief from each C-suite executive and synthesize a CEO summary.
        Called at 6 AM (morning session) and 1 PM (midday ops check).
        Each executive's brief is logged; critical items escalate immediately.
        """
        executives = {
            "CRO":   self._cro,
            "CIO":   self._cio,
            "CTO":   self._cto,
            "COO":   self._coo,
            "CFO":   self._cfo,
            "R&D":   self._rnd,
            "CTech": self._ctech,
        }

        briefs: dict[str, str] = {}
        for name, exec_agent in executives.items():
            if exec_agent is None:
                continue
            try:
                brief = await exec_agent.produce_brief()
                briefs[name] = brief
                logger.info("C-suite roundtable [%s] %s: %s", session, name, brief[:200])
            except Exception as exc:
                briefs[name] = f"[Brief unavailable: {exc}]"
                logger.warning("C-suite roundtable: %s brief failed: %s", name, exc)

        if not briefs:
            return

        # Synthesize a unified CEO roundtable summary
        briefs_str = "\n\n".join(f"**{name}**:\n{b}" for name, b in briefs.items())
        prompt = (
            f"You just completed the {session} C-suite roundtable. Here are all department briefs:\n\n"
            f"{briefs_str}\n\n"
            f"Write a 3-sentence CEO synthesis for Rahul (the owner):\n"
            f"1. Overall system status (1 sentence)\n"
            f"2. Top concern right now (1 sentence with ⚠️ or ✅)\n"
            f"3. CEO's decision or action for the next 4 hours (1 sentence)\n"
            f"Be direct. Use numbers."
        )
        synthesis = await self._synthesize(prompt, f"C-suite {session} roundtable")
        icon = "🌅" if session == "morning" else "☀️"
        header = f"{icon} **CEO Roundtable** [{session.title()}] — {datetime.now(tz=ET).strftime('%H:%M ET')}\n\n"
        await self._send_to_discord(header + synthesis)

    async def board_meeting(self, agenda: str = "") -> str:
        """
        Full board meeting: each C-suite executive presents, CEO synthesizes and decides.
        Called manually or during weekly performance review.
        Returns a full meeting transcript for the owner.
        """
        executives = {
            "Chief Risk Officer (CRO)":           self._cro,
            "Chief Intelligence Officer (CIO)":   self._cio,
            "Chief Trading Officer (CTO)":        self._cto,
            "Chief Operations Officer (COO)":     self._coo,
            "Chief Financial Officer (CFO)":      self._cfo,
            "Chief Research Officer (R&D)":       self._rnd,
            "Chief Technology Officer (CTech)":   self._ctech,
        }

        transcript_parts = [
            f"# AGORA Board Meeting — {datetime.now(tz=ET).strftime('%B %d, %Y %H:%M ET')}\n",
            f"**Agenda**: {agenda or 'Profitability review and system optimization'}\n",
            "---\n",
        ]

        all_briefs: dict[str, str] = {}
        for title, agent in executives.items():
            if agent is None:
                transcript_parts.append(f"## {title}\n*Not instantiated — skipped.*\n")
                continue
            try:
                intel = agent.collect_intelligence()
                brief = await agent.produce_brief(intel)
                all_briefs[title] = brief
                transcript_parts.append(f"## {title}\n{brief}\n")
            except Exception as exc:
                brief = f"*Brief failed: {exc}*"
                all_briefs[title] = brief
                transcript_parts.append(f"## {title}\n{brief}\n")

        transcript_parts.append("---\n## CEO Summary & Directives\n")

        # CEO synthesizes all briefs and issues directives
        briefs_str = "\n\n".join(f"{t}:\n{b}" for t, b in all_briefs.items())
        # Include live readiness score
        readiness_summary = ""
        if self._readiness:
            score = self._readiness.get_score()
            readiness_summary = (
                f"\n\n**Live Readiness Meter**: {score['overall_score']:.0f}/100 "
                f"({'GO LIVE APPROVED ✅' if score['go_live_approved'] else 'Pending approval'})\n"
                + "\n".join(
                    f"  - {p.title()}: {d['score']:.0f}/100 ({d['status']})"
                    for p, d in score["pillars"].items()
                )
            )

        ceo_prompt = (
            f"You chaired a board meeting with 7 C-suite executives. Here are their briefs:\n\n"
            f"{briefs_str}\n\n"
            f"Agenda: {agenda or 'system readiness review and go-live strategy'}\n"
            f"{readiness_summary}\n\n"
            f"CRITICAL INSTRUCTION: Base your analysis ONLY on what's in the briefs above. "
            f"No hallucinations. If data is missing, say 'data unavailable' — do not invent numbers.\n\n"
            f"As CEO reporting to Rahul (owner), produce:\n"
            f"1. **System Assessment** — 2-3 sentences on overall state (cite actual numbers)\n"
            f"2. **Critical Issues** — list any 🚨 items requiring immediate owner attention\n"
            f"3. **CEO Directives** — numbered list of specific actions you're authorizing in next 24-48h\n"
            f"4. **Readiness Status** — go-live meter summary and what needs to improve\n"
            f"5. **Owner Request** — what you need from Rahul (capital, config changes, strategy guidance)\n\n"
            f"Be specific. Quantify risks. Reference actual numbers from the briefs. No hallucinations."
        )
        ceo_summary = await self._synthesize(ceo_prompt, "Board Meeting Summary")
        transcript_parts.append(ceo_summary)

        full_transcript = "\n".join(transcript_parts)
        logger.info("Board meeting completed. Transcript length: %d chars", len(full_transcript))

        # Persist latest transcript to disk for /agora/board-meeting/latest
        import json as _json
        from pathlib import Path as _Path
        _log_dir = _Path(".agora")
        _log_dir.mkdir(exist_ok=True)
        _log_path = _log_dir / "last_board_meeting.json"
        _log_path.write_text(_json.dumps({
            "agenda": agenda,
            "transcript": full_transcript,
            "timestamp": datetime.now(tz=ET).isoformat(),
            "length": len(full_transcript),
        }, indent=2))

        # Send to Discord in chunks
        await self._send_to_discord(full_transcript)
        return full_transcript

    # ── Alert dispatch (called externally for critical events) ─────

    async def dispatch_alert(self, level: str, message: str, context: dict | None = None) -> None:
        """
        Receive a C-suite alert, take autonomous corrective action, and post
        a brief Discord notification describing what was done.

        The owner (Rahul) is NOT asked for approval — CEO acts and reports.
        Alert dedup: identical alert keys are suppressed for 10 minutes to
        prevent notification spam from rapid-fire self_audit cycles.
        """
        # ── Dedup: suppress repeat alerts within cooldown window ──────────────
        _dedup_key = message[:120]
        _now = datetime.now(tz=ET)
        _last = self._alert_last_fired.get(_dedup_key)
        if _last and (_now - _last).total_seconds() < self._ALERT_COOLDOWN_SECS:
            logger.debug("CEO dispatch_alert: suppressed duplicate alert (cooldown) [%s]", level)
            return
        self._alert_last_fired[_dedup_key] = _now

        # ── Take autonomous corrective action ─────────────────────────────────
        try:
            action_taken = await self._autonomous_response(level, message)
        except Exception as exc:
            action_taken = f"⚠️ Autonomous response error: {exc}"
            logger.error("CEO autonomous response raised: %s", exc)

        logger.warning("CEO [%s]: %s → %s", level, message[:120], action_taken)

        # ── Send compact Discord notification (action taken, not a request) ───
        icon = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}.get(level, "ℹ️")
        now_str = _now.strftime("%H:%M ET")
        lines = [
            f"{icon} **CEO** [{now_str}]",
            f"**Alert:** {message[:300]}",
            f"**Action:** {action_taken}",
        ]
        if context:
            ctx_str = "\n".join(f"  {k}: {v}" for k, v in context.items())
            lines.append(f"```\n{ctx_str}\n```")
        await self._send_to_discord("\n".join(lines))

    # ── Report generators ──────────────────────────────────────────

    async def _morning_brief(self) -> None:
        """6:00 AM: What do we have open? What's coming? What's the setup?"""
        state = self._collect_state()

        upcoming_str = ""
        if self._upcoming_earnings:
            upcoming_str = "\nUpcoming earnings plays identified:\n" + "\n".join(
                f"  {e['ticker']} on {e['date']}: {e['direction']} (conf={e['confidence']:.0%}) — {e['reasoning']}"
                for e in self._upcoming_earnings[:5]
            )

        prompt = f"""
Morning brief for AGORA. Today is {date.today().strftime('%A, %B %d, %Y')}.

Current system state:
{json.dumps(state, indent=2)}
{upcoming_str}

Write a morning brief for Rahul (owner) with these sections:
1. **Positions** — current open positions, unrealized P&L, any at risk
2. **Today's Watchlist** — what to watch, expected catalysts
3. **Risk Status** — daily loss used, any concerns
4. **Plan** — what AGORA will focus on today

Be concise. Use numbers. Flag concerns with ⚠️.
"""
        report = await self._synthesize(prompt, "Morning Brief")
        header = f"🌅 **AGORA Morning Brief** — {date.today().strftime('%b %d, %Y')}\n\n"
        await self._send_to_discord(header + report)

    async def _midmorning_check(self) -> None:
        """10:00 AM: Quick check — any new trades, any issues?"""
        state = self._collect_state()

        prompt = f"""
Mid-morning check for AGORA. It is 10:00 AM ET.

System state:
{json.dumps(state, indent=2)}

Write a very brief (3-4 bullet points) mid-morning update for Rahul:
- Any new positions opened this morning?
- Any positions at risk (near stop-loss)?
- P&L so far today
- Any immediate actions needed?

Keep it under 400 characters. Use ✅ if all clear.
"""
        report = await self._synthesize(prompt, "Mid-Morning Check")
        header = f"📊 **Mid-Morning Check** — {datetime.now(tz=ET).strftime('%H:%M ET')}\n\n"
        await self._send_to_discord(header + report)

    async def _midday_status(self) -> None:
        """1:00 PM: Midday status — P&L, risk, any regime shifts."""
        state = self._collect_state()

        prompt = f"""
Midday status report for AGORA. It is 1:00 PM ET.

System state:
{json.dumps(state, indent=2)}

Write a midday status for Rahul:
1. **P&L Update** — current unrealized P&L vs daily loss limit
2. **Risk Check** — portfolio delta, any concentrated exposure
3. **Regime** — any macro shifts since morning?
4. **Remaining Day** — what to watch for in afternoon session

Keep it concise and numbers-first.
"""
        report = await self._synthesize(prompt, "Midday Status")
        header = f"📈 **Midday Status** — {datetime.now(tz=ET).strftime('%H:%M ET')}\n\n"
        await self._send_to_discord(header + report)

    async def _eod_report(self) -> None:
        """4:30 PM: Full EOD summary — what happened, what we learned, tomorrow's setup."""
        state = self._collect_state()
        perf = self._get_performance_summary()

        prompt = f"""
End-of-day report for AGORA. Market closed. Today is {date.today().strftime('%A, %B %d')}.

System state:
{json.dumps(state, indent=2)}

30-day performance summary:
{json.dumps(perf, indent=2)}

Write a complete EOD report for Rahul with:
1. **Today's Summary** — trades executed, P&L, wins/losses
2. **Position Status** — all open positions, greeks, risk
3. **What Worked / What Didn't** — honest assessment
4. **Tomorrow's Setup** — what to watch, upcoming earnings, catalysts
5. **System Health** — any issues, data gaps, agent failures today

Be direct. Flag anything that needs Rahul's attention with 🚨.
"""
        report = await self._synthesize(prompt, "EOD Report")
        header = f"🌙 **AGORA EOD Report** — {date.today().strftime('%b %d, %Y')}\n\n"
        await self._send_to_discord(header + report)

        # Clear upcoming earnings list for next day
        self._upcoming_earnings.clear()

    # ── Integrity patrol (every 30 min, proactive) ────────────────

    async def _integrity_patrol(self) -> None:
        """
        Independent audit of system health — does NOT rely on what sub-agents self-report.
        Fires alerts directly to the owner if anomalies are found.
        Runs every 30 minutes whether the market is open or not.
        """
        issues: list[str] = []

        try:
            import sqlite3 as _sql
            db_path = str(self._settings.db_path)

            # ── Ghost fills: record_fill() ran but _record_position() didn't ──
            try:
                conn = _sql.connect(db_path, check_same_thread=False)
                ghost = conn.execute(
                    """SELECT ticker, fill_price FROM execution_quality
                       WHERE outcome='fill' AND attempt_date=date('now')
                       AND ticker NOT IN (SELECT ticker FROM positions)""",
                ).fetchall()
                conn.close()
                if ghost:
                    tickers = ", ".join(r[0] for r in ghost)
                    issues.append(
                        f"🚨 {len(ghost)} GHOST FILL(S): position not recorded after confirmed fill "
                        f"({tickers}) — session likely crashed between record_fill and _record_position. "
                        f"These trades exist in TWS but not in shadow book."
                    )
            except Exception as exc:
                logger.debug("CEO patrol: ghost fill check failed — %s", exc)

            # ── Fill/timeout rates from DB (accurate across restarts) ──
            try:
                conn = _sql.connect(db_path, check_same_thread=False)
                rows = conn.execute(
                    "SELECT outcome, COUNT(*) FROM execution_quality "
                    "WHERE attempt_date=date('now') GROUP BY outcome"
                ).fetchall()
                policy_rejects_201 = conn.execute(
                    "SELECT COUNT(*) FROM execution_quality "
                    "WHERE attempt_date=date('now') AND reject_code='201'"
                ).fetchone()[0]
                conn.close()
                m = {r[0]: r[1] for r in rows}
                fills    = m.get("fill", 0)
                timeouts = m.get("timeout", 0)
                total    = fills + timeouts + m.get("reject", 0)
                # Exclude Error 201 policy rejects: IBKR account restriction, not exec failure.
                effective_total = max(0, total - policy_rejects_201)
                if effective_total >= 5:
                    fill_rate = fills / effective_total
                    timeout_rate = timeouts / effective_total
                    if fill_rate < 0.15:
                        issues.append(
                            f"🚨 FILL RATE CRITICAL: {fill_rate:.1%} ({fills}/{effective_total} "
                            f"effective orders filled, {policy_rejects_201} policy-201 rejects excluded). "
                            f"Check IBKR connectivity and order timeout settings."
                        )
                    if timeout_rate > 0.85:
                        issues.append(
                            f"⚠️ TIMEOUT RATE HIGH: {timeout_rate:.1%} of orders timed out without "
                            f"fill confirmation. Fill callbacks may not be reaching AGORA."
                        )
            except Exception as exc:
                logger.debug("CEO patrol: fill rate check failed — %s", exc)

            # ── Kill switch (always alert if active, every patrol) ──
            try:
                if self._risk_council and self._risk_council.is_kill_switch_active():
                    ks = self._risk_council.get_kill_switch_state()
                    issues.append(
                        f"🚨 KILL SWITCH ACTIVE: {ks.get('reason', 'unknown')} "
                        f"(tripped {ks.get('tripped_at', '?')[:16]})"
                    )
            except Exception:
                pass

            # ── Untracked TWS round-trips ──
            # If TWS has both BOT and SLD for a ticker but no position in DB, that's a blind spot
            try:
                conn = _sql.connect(db_path, check_same_thread=False)
                closed_in_db = {
                    r[0] for r in conn.execute(
                        "SELECT ticker FROM positions WHERE close_date=date('now')"
                    ).fetchall()
                }
                conn.close()
                if self._coo and hasattr(self._coo, "_ibkr_agent") and self._coo._ibkr_agent:
                    last_scan = self._coo._ibkr_agent.get_status().get("last_scan") or {}
                    tws_fills = last_scan.get("tws_fills", [])
                    bag_bot = {f["symbol"] for f in tws_fills if f.get("secType") == "BAG" and f.get("action") == "BOT"}
                    bag_sld = {f["symbol"] for f in tws_fills if f.get("secType") == "BAG" and f.get("action") == "SLD"}
                    tws_closed = bag_bot & bag_sld
                    untracked = tws_closed - closed_in_db
                    if untracked:
                        issues.append(
                            f"⚠️ UNTRACKED TWS CLOSES: {', '.join(sorted(untracked))} show complete "
                            f"BOT+SLD in TWS but no position in shadow book — realized P&L not captured."
                        )
            except Exception as exc:
                logger.debug("CEO patrol: TWS untracked check failed — %s", exc)

        except Exception as exc:
            logger.warning("CEO integrity_patrol failed: %s", exc)
            return

        if issues:
            alert_body = (
                f"**CEO Integrity Patrol** — {datetime.now(tz=ET).strftime('%H:%M ET')}\n"
                + "\n".join(issues)
            )
            await self.dispatch_alert("critical", alert_body)
            logger.warning("CEO patrol: %d issue(s) found", len(issues))
        else:
            logger.debug("CEO patrol: all checks passed at %s", datetime.now(tz=ET).strftime("%H:%M"))

    # ── State collection ───────────────────────────────────────────

    def _collect_state(self) -> dict:
        """Collect all relevant system state into a dict for Claude."""
        state: dict[str, Any] = {
            "timestamp": datetime.now(tz=ET).isoformat(),
            "trading_mode": self._settings.trading_mode,
            "account_size": self._settings.account_size,
        }

        if self._position_mgr:
            try:
                positions = self._position_mgr.get_open_positions()
                greeks = self._position_mgr.get_portfolio_greeks()
                state["open_positions"] = len(positions)
                state["portfolio_delta"] = round(greeks.get("delta", 0), 3)
                state["portfolio_theta"] = round(greeks.get("theta", 0), 2)
                state["portfolio_vega"] = round(greeks.get("vega", 0), 2)
                state["total_unrealized_pnl"] = round(
                    sum(p.unrealized_pnl for p in positions), 2
                )
                # Position summary (compact)
                state["positions"] = [
                    {
                        "ticker": p.ticker,
                        "strategy": p.strategy,
                        "direction": p.direction,
                        "contracts": p.contracts,
                        "unrealized_pnl": round(p.unrealized_pnl, 2),
                        "entry_date": str(p.entry_date),
                        "expiry_date": str(p.expiry_date),
                        "conviction_at_entry": p.conviction_at_entry,
                        "regime_at_entry": p.regime_at_entry,
                    }
                    for p in positions
                ]
            except Exception as exc:
                state["positions_error"] = str(exc)

        if self._risk_council:
            try:
                state["kill_switch_active"] = self._risk_council.is_kill_switch_active()
                state["kill_switch_state"]  = self._risk_council.get_kill_switch_state()
                state["daily_loss_limit"]   = self._settings.daily_loss_limit_dollars
                state["weekly_loss_limit"]  = self._settings.weekly_loss_limit_dollars
            except Exception:
                pass

        # ── Operational truth — DB-accurate, not from in-memory session counters ──
        try:
            import sqlite3 as _sql
            db_path = str(self._settings.db_path)
            conn = _sql.connect(db_path, check_same_thread=False)

            # Today's execution quality from DB
            rows = conn.execute(
                "SELECT outcome, COUNT(*) FROM execution_quality "
                "WHERE attempt_date=date('now') GROUP BY outcome"
            ).fetchall()
            m = {r[0]: r[1] for r in rows}
            fills    = m.get("fill", 0)
            timeouts = m.get("timeout", 0)
            rejects  = m.get("reject", 0)
            total    = fills + timeouts + rejects
            state["exec_today"] = {
                "attempts": total,
                "fills":    fills,
                "timeouts": timeouts,
                "rejects":  rejects,
                "fill_rate": f"{fills/total:.1%}" if total else "no data",
                "timeout_rate": f"{timeouts/total:.1%}" if total else "no data",
            }

            # Ghost fills — fills with no position record
            ghost = conn.execute(
                """SELECT ticker FROM execution_quality
                   WHERE outcome='fill' AND attempt_date=date('now')
                   AND ticker NOT IN (SELECT ticker FROM positions)"""
            ).fetchall()
            state["ghost_fills"] = [r[0] for r in ghost]

            # Realized P&L today
            realized = conn.execute(
                "SELECT SUM(realized_pnl) FROM positions WHERE close_date=date('now')"
            ).fetchone()[0]
            state["realized_pnl_today"] = round(realized or 0.0, 2)

            # Positions closed today and how
            closed = conn.execute(
                "SELECT ticker, realized_pnl, close_source FROM positions WHERE close_date=date('now')"
            ).fetchall()
            state["closed_today"] = [
                {"ticker": r[0], "pnl": r[1], "source": r[2]} for r in closed
            ]

            conn.close()
        except Exception as exc:
            logger.debug("CEO _collect_state DB enrichment failed: %s", exc)

        return state

    def _get_performance_summary(self) -> dict:
        if self._position_mgr:
            try:
                return self._position_mgr.get_performance_summary(lookback_days=30)
            except Exception:
                pass
        return {}

    # ── Claude synthesis ───────────────────────────────────────────

    async def _synthesize(
        self,
        prompt: str,
        report_type: str,
        effort: str = "high",
        thinking_display: str | None = None,
        task_budget_tokens: int | None = None,
    ) -> str:
        """
        Stream an Opus 4.7 response for CEO reports.
        effort="xhigh" for board meetings; "high" for routine reports.
        thinking_display="summarized" enables visible reasoning in Opus 4.7 (default is omitted).
        task_budget_tokens: budget hint for multi-step synthesis (beta, min 20000).
        """
        thinking_cfg: dict = {"type": "adaptive"}
        if thinking_display:
            thinking_cfg["display"] = thinking_display

        output_cfg: dict = {"effort": effort}
        if task_budget_tokens and task_budget_tokens >= 20000:
            output_cfg["task_budget"] = {"type": "tokens", "total": task_budget_tokens}

        extra_hdrs: dict = {}
        if task_budget_tokens and task_budget_tokens >= 20000:
            extra_hdrs["anthropic-beta"] = "task-budgets-2026-03-13"

        try:
            async with self._client.messages.stream(
                model=self._settings.claude_model,
                max_tokens=1536,
                thinking=thinking_cfg,
                system=[{
                    "type": "text",
                    "text": _CEO_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": prompt}],
                output_config=output_cfg,
                **({"extra_headers": extra_hdrs} if extra_hdrs else {}),
            ) as stream:
                msg = await stream.get_final_message()
            if hasattr(msg, "usage"):
                _log_msg(
                    str(self._settings.db_path), "CEO",
                    self._settings.claude_model,
                    msg.usage,
                    purpose=report_type,
                )
            for block in reversed(msg.content):
                if hasattr(block, "text"):
                    return block.text.strip()
            return f"[{report_type}: synthesis failed — no text block]"
        except Exception as exc:
            logger.error("CEO synthesis failed for %s: %s", report_type, exc)
            return f"[{report_type} synthesis error: {exc}]"

    # ── Discord delivery ───────────────────────────────────────────

    async def _send_to_discord(self, message: str) -> None:
        """Send a message to Discord webhook. Silently no-ops if not configured."""
        webhook_url = self._settings.alert_webhook_url
        if not webhook_url:
            logger.info("CEO REPORT (no Discord configured):\n%s", message)
            return

        # Discord rejects messages >2000 chars with a 400 (the whole report is lost). Split on
        # line boundaries so markdown isn't cut mid-line; only hard-slice a single line that is
        # itself longer than the limit.
        chunks = _chunk_for_discord(message, limit=1900)
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                for chunk in chunks:
                    await client.post(webhook_url, json={"content": chunk})
                    if len(chunks) > 1:
                        await asyncio.sleep(0.5)
        except Exception as exc:
            logger.warning("Discord delivery failed: %s", exc)
            logger.info("CEO REPORT (Discord failed):\n%s", message)

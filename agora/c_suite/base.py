"""
ExecutiveAgent — base class for all C-suite agents.

Every executive runs three parallel loops:
  1. Brief loop    — produce a departmental brief every BRIEF_CADENCE hours (7 AM–8 PM)
  2. Patrol loop   — call self_audit() every PATROL_INTERVAL_SEC (30 min, 24/7)
                     → then calls self_heal(findings) to take corrective action
  3. Event loop    — consume events from the AgentEventBus and call on_peer_event()

The patrol loop:
  - calls self_audit() which each subclass implements with domain-specific checks
  - calls self_heal(findings) for corrective actions (subclass implements)
  - tracks each issue by a stable key across consecutive patrols (_issue_counts)
  - after 3 consecutive occurrences the issue is flagged as RECURRING and escalated
  - when an issue resolves the counter resets
  - keeps a ring-buffer of the last 48 patrol results (_audit_history) for trend analysis

Lateral communications (C-to-C without CEO relay):
  - notify_peers(event_type, payload) → AgentEventBus.publish()
  - on_peer_event(event, payload) — override to react to events from other C's

Session Plan (CEO morning directive):
  - CEO publishes a SessionPlan each morning after the board meeting
  - agents access it via get_session_plan() to self-configure stance/sizing

Reporting chain:
  Sub-agents → C-suite Executive → CEO → Owner (Rahul)
  Lateral:    C-suite ↔ C-suite (via AgentEventBus — no CEO relay for routine ops)
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

import anthropic

from ..core.config import AgoraSettings, get_settings
from ..core.events import AgentEventBus, AgentEvent
from ..core.session_plan import SessionPlan
from ..ops.llm_cost_log import log_call as _log_llm, log_message as _log_msg

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

# Issue seen this many times in a row → escalate to CEO as RECURRING
_RECURRENCE_THRESHOLD = 3


class ExecutiveAgent:
    """
    Abstract base for CRO, CIO, CTO, COO, CFO, R&D, CTech.

    Subclasses must define:
      TITLE              — "Chief Risk Officer" etc.
      BRIEF_CADENCE      — hours between brief reports (during market hours)
      PATROL_INTERVAL_SEC — seconds between self-audits (default 1800 = 30 min)
      _system_prompt     — domain expert knowledge for Claude
      collect_intelligence() — gather state from sub-agents
      self_audit()       — return list of (key, severity, message) findings
    """

    TITLE: str = "Executive"
    BRIEF_CADENCE: int = 4          # hours between briefs
    PATROL_INTERVAL_SEC: int = 1800  # 30 min patrol cadence

    def __init__(
        self,
        settings: AgoraSettings | None = None,
        ceo_agent: Any = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._ceo = ceo_agent
        self._client = anthropic.AsyncAnthropic(api_key=self._settings.anthropic_api_key)
        self._running = False

        # Latest intelligence snapshot — synchronously readable by CEO
        self._latest_intel: dict[str, Any] = {}
        self._latest_brief: str = ""
        self._last_brief_time: datetime | None = None

        # Patrol state
        self._issue_counts: dict[str, int] = {}
        self._audit_history: deque[dict] = deque(maxlen=48)
        self._recurring_escalated: set[str] = set()

        # ── Lateral event bus (wired by session.py after all agents are created) ──
        self._event_bus: AgentEventBus | None = None
        self._event_queue: asyncio.Queue | None = None   # our subscriber queue

        # ── Session plan (CEO morning directive — read-only access) ──
        self._session_plan_getter: Callable[[], SessionPlan] | None = None

        # ── Self-heal tracking (avoid infinite heal loops) ──
        # key → number of consecutive heal attempts (reset when issue resolves)
        self._heal_attempts: dict[str, int] = {}

    # ── Wiring (called by session.py after all agents are instantiated) ─────

    def wire_event_bus(self, bus: AgentEventBus, *subscribe_to: str) -> None:
        """
        Connect this agent to the shared event bus.
        subscribe_to: event types this agent wants to receive.
        """
        self._event_bus = bus
        if subscribe_to:
            self._event_queue = bus.subscribe(self.TITLE, *subscribe_to)
        logger.debug("%s wired to event bus (subscribed: %s)", self.TITLE, subscribe_to)

    def wire_session_plan(self, getter: Callable[[], SessionPlan]) -> None:
        """Provide a callable that returns the CEO's current SessionPlan."""
        self._session_plan_getter = getter

    def get_session_plan(self) -> SessionPlan:
        """Return the CEO's current session plan, or a neutral default."""
        if self._session_plan_getter:
            try:
                return self._session_plan_getter()
            except Exception:
                pass
        return SessionPlan.default()

    # ── Lifecycle ────────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        logger.info("%s started", self.TITLE)
        tasks = [self._brief_loop(), self._patrol_loop()]
        if self._event_queue is not None:
            tasks.append(self._event_listener_loop())
        await asyncio.gather(*tasks, return_exceptions=True)

    async def stop(self) -> None:
        self._running = False

    async def _brief_loop(self) -> None:
        """Produce a departmental brief every BRIEF_CADENCE hours during market hours."""
        await asyncio.sleep(120)  # stagger startup
        while self._running:
            now_et = datetime.now(tz=ET)
            if 7 <= now_et.hour < 20:
                try:
                    intel = self.collect_intelligence()
                    # Inject latest audit findings so Claude sees real problems
                    if self._audit_history:
                        intel["_latest_audit"] = list(self._audit_history)[-1]
                    self._latest_intel = intel
                    brief = await self.produce_brief(intel)
                    self._latest_brief = brief
                    self._last_brief_time = now_et
                    logger.info("%s brief produced", self.TITLE)
                except Exception as exc:
                    logger.error("%s brief loop error: %s", self.TITLE, exc)
            await asyncio.sleep(self.BRIEF_CADENCE * 3600)

    async def _patrol_loop(self) -> None:
        """
        Self-audit patrol — runs 24/7 every PATROL_INTERVAL_SEC.
        Detects problems, tracks recurrence, escalates when issues persist.
        """
        await asyncio.sleep(60)  # brief startup delay
        while self._running:
            try:
                await self._run_patrol()
            except Exception as exc:
                logger.error("%s patrol error: %s", self.TITLE, exc)
            await asyncio.sleep(self.PATROL_INTERVAL_SEC)

    async def _run_patrol(self) -> None:
        """
        Execute self_audit(), track recurrence, escalate persistent issues.
        """
        now_et = datetime.now(tz=ET)
        findings: list[tuple[str, str, str]] = []
        try:
            findings = self.self_audit()
        except Exception as exc:
            logger.warning("%s self_audit() raised: %s", self.TITLE, exc)

        # Keys found this patrol
        current_keys: set[str] = {key for key, _, _ in findings}

        # Increment counts for current issues; reset for resolved ones
        all_known = set(self._issue_counts.keys()) | current_keys
        for key in all_known:
            if key in current_keys:
                self._issue_counts[key] = self._issue_counts.get(key, 0) + 1
            else:
                # Issue resolved — reset counter and unblock future escalation
                if key in self._issue_counts:
                    del self._issue_counts[key]
                self._recurring_escalated.discard(key)

        # Decide which findings to escalate
        escalate_now: list[tuple[str, str, str]] = []
        for key, severity, message in findings:
            count = self._issue_counts[key]
            if severity == "critical":
                escalate_now.append((key, severity, message))
            elif count >= _RECURRENCE_THRESHOLD and key not in self._recurring_escalated:
                # Recurring warning → promote to critical escalation
                self._recurring_escalated.add(key)
                escalate_now.append((
                    key, "critical",
                    f"RECURRING ({count}× in a row): {message}"
                ))

        if escalate_now:
            body = await self._format_patrol_alert(escalate_now, now_et)
            await self._escalate_to_ceo("critical" if any(s == "critical" for _, s, _ in escalate_now) else "warning", body)
            logger.warning("%s patrol escalated %d issue(s)", self.TITLE, len(escalate_now))
        elif findings:
            logger.info("%s patrol: %d finding(s), none critical yet (counts: %s)",
                        self.TITLE, len(findings), {k: v for k, v in self._issue_counts.items()})
        else:
            logger.debug("%s patrol: all clear at %s", self.TITLE, now_et.strftime("%H:%M"))

        # Store patrol result in history
        self._audit_history.append({
            "ts":       now_et.isoformat(),
            "findings": [(k, s, m[:120]) for k, s, m in findings],
            "escalated": len(escalate_now),
        })

        # ── Self-heal: take corrective action on current findings ──
        if findings:
            resolved_keys = set(self._issue_counts.keys()) - current_keys
            for key in resolved_keys:
                self._heal_attempts.pop(key, None)
            try:
                await self.self_heal(findings)
            except Exception as exc:
                logger.error("%s self_heal() raised: %s", self.TITLE, exc)

    async def _event_listener_loop(self) -> None:
        """Consume events from the AgentEventBus and dispatch to on_peer_event()."""
        while self._running:
            try:
                event: AgentEvent = await asyncio.wait_for(
                    self._event_queue.get(), timeout=5.0
                )
                try:
                    await self.on_peer_event(event.event_type, event.publisher, event.payload)
                except Exception as exc:
                    logger.error("%s on_peer_event(%s) raised: %s", self.TITLE, event.event_type, exc)
            except asyncio.TimeoutError:
                continue
            except Exception as exc:
                logger.error("%s event_listener_loop error: %s", self.TITLE, exc)
                await asyncio.sleep(1)

    # ── Core API — subclasses override ───────────────────────────────

    def collect_intelligence(self) -> dict[str, Any]:
        """Gather current state from sub-agents. Override in each executive."""
        return {"timestamp": datetime.now(tz=ET).isoformat(), "title": self.TITLE}

    def self_audit(self) -> list[tuple[str, str, str]]:
        """
        Domain-specific proactive audit.
        Returns list of (issue_key, severity, message) tuples.
          issue_key — stable string identifying this class of problem
          severity  — "warning" | "critical"
          message   — human-readable description with numbers

        Override in every subclass. Called every PATROL_INTERVAL_SEC.
        """
        return []

    async def self_heal(self, findings: list[tuple[str, str, str]]) -> None:
        """
        Take corrective action on audit findings. Called automatically after
        every self_audit() that returns findings. Override in each subclass.

        findings: same list returned by self_audit() — (key, severity, message)
        Use _heal_attempts[key] to limit retries (already incremented by base).
        """
        pass

    async def notify_peers(self, event_type: str, payload: dict[str, Any]) -> None:
        """Publish an event to all subscribers on the shared event bus."""
        if self._event_bus:
            await self._event_bus.publish(event_type, self.TITLE, payload)
        else:
            logger.debug("%s notify_peers: no event bus wired — %s dropped", self.TITLE, event_type)

    async def on_peer_event(self, event_type: str, publisher: str, payload: dict[str, Any]) -> None:
        """
        Receive and react to an event from another C-suite agent.
        Override in each subclass to implement lateral decision-making.
        """
        logger.debug("%s received peer event [%s] from %s", self.TITLE, event_type, publisher)

    def get_readiness_tasks(self) -> list[str]:
        """
        Return the specific actions this department needs to take to improve
        its live readiness pillar score. CEO reads these at morning board meeting
        and publishes them so agents can work through gaps autonomously.
        Override in each subclass.
        """
        return []

    async def produce_brief(self, context: dict[str, Any] | None = None) -> str:
        """
        Generate a departmental brief using Claude Opus 4.7 + adaptive thinking.
        Includes latest audit findings so the brief reflects real problems.
        """
        intel = context or self.collect_intelligence()

        # Summarise recent audit history for Claude
        audit_summary = ""
        if self._audit_history:
            recent = list(self._audit_history)[-3:]  # last 3 patrols
            flat_findings = [f for p in recent for f in p.get("findings", [])]
            recurring = {k: self._issue_counts[k] for k in self._issue_counts if self._issue_counts[k] >= 2}
            if flat_findings or recurring:
                audit_summary = (
                    f"\n\nRecent self-audit findings (last 3 patrols):\n"
                    + "\n".join(f"  [{s}] {m}" for k, s, m in flat_findings[:10])
                    + (f"\nRecurring issues: {recurring}" if recurring else "")
                )

        prompt = (
            f"Produce your departmental brief for the CEO board meeting.\n\n"
            f"Current state:\n{json.dumps(intel, indent=2, default=str)}"
            f"{audit_summary}\n\n"
            f"Structure:\n"
            f"1. **Status** — 1 sentence on overall department health\n"
            f"2. **Key Metrics** — top 3-5 numbers that matter right now\n"
            f"3. **Concerns** — anything needing CEO or owner attention (⚠️ / 🚨)\n"
            f"4. **Self-Audit Findings** — what your patrol detected and what you did about it\n"
            f"5. **Recommendation** — 1-2 concrete actions for the next 24h to improve profitability\n\n"
            f"Be direct. Numbers-first. Under 900 characters."
        )
        return await self._synthesize(prompt)

    async def advise(self, question: str, context: dict[str, Any] | None = None) -> str:
        """Ask this executive a domain question. Used during board meetings."""
        intel = context or self._latest_intel
        prompt = (
            f"Question from CEO: {question}\n\n"
            f"Current department state:\n{json.dumps(intel, indent=2, default=str)}\n\n"
            f"Answer as a senior {self.TITLE}. Specific, quantitative, actionable. Under 600 chars."
        )
        return await self._synthesize(prompt)

    async def receive_alert(self, source: str, level: str, message: str) -> None:
        """
        Receive an alert from a sub-agent.
        Critical → always escalate to CEO immediately.
        Warning → log and include in next brief; escalate if it becomes recurring.
        """
        logger.info("%s received %s alert from %s: %s", self.TITLE, level, source, message[:120])
        if level == "critical":
            await self._escalate_to_ceo(level, f"[{source}] {message}")
        else:
            self._latest_intel.setdefault("pending_warnings", []).append(
                {"source": source, "message": message[:200], "ts": datetime.now(tz=ET).isoformat()}
            )

    def get_snapshot(self) -> dict[str, Any]:
        """Synchronous snapshot for CEO state collection — no blocking."""
        recurring = {k: v for k, v in self._issue_counts.items() if v >= 2}
        return {
            "title":           self.TITLE,
            "last_brief_time": self._last_brief_time.isoformat() if self._last_brief_time else None,
            "latest_brief":    self._latest_brief[:400] if self._latest_brief else None,
            "open_issues":     len(self._issue_counts),
            "recurring_issues": recurring,
            **self._latest_intel,
        }

    def get_audit_summary(self) -> dict[str, Any]:
        """Return patrol history and current issue state for dashboard/CEO."""
        return {
            "title":            self.TITLE,
            "open_issue_count": len(self._issue_counts),
            "recurring":        {k: v for k, v in self._issue_counts.items() if v >= _RECURRENCE_THRESHOLD},
            "last_patrol":      self._audit_history[-1] if self._audit_history else None,
            "patrol_count":     len(self._audit_history),
        }

    # ── Internals ────────────────────────────────────────────────────

    async def _format_patrol_alert(
        self,
        findings: list[tuple[str, str, str]],
        now_et: datetime,
    ) -> str:
        """
        Format patrol findings for Discord escalation.
        Uses Haiku for smart prioritization when 3+ findings exist.
        Falls back to Python string formatting on error or for < 3 findings.
        """
        header = f"**{self.TITLE} Patrol** — {now_et.strftime('%H:%M ET')}\n"
        simple_body = "\n".join(
            f"{'🚨' if s == 'critical' else '⚠️'} {m}" for _, s, m in findings
        )
        if len(findings) < 3:
            return header + simple_body

        # Haiku: prioritize and surface the single most actionable item
        raw_lines = "\n".join(
            f"{'CRITICAL' if s == 'critical' else 'WARNING'}: {m}" for _, s, m in findings
        )
        prompt = (
            f"You are the {self.TITLE}. Format these audit findings into a concise Discord alert:\n\n"
            f"{raw_lines}\n\n"
            f"Rules: Lead with the most urgent item. Suggest ONE specific immediate action. "
            f"Use 🚨 for critical, ⚠️ for warnings. Under 380 chars total."
        )
        try:
            resp = await self._client.messages.create(
                model=self._settings.claude_fast_model,
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )
            if hasattr(resp, "usage"):
                _log_msg(
                    str(self._settings.db_path), self.TITLE,
                    self._settings.claude_fast_model,
                    resp.usage,
                    purpose="patrol_format",
                )
            for block in reversed(resp.content):
                if hasattr(block, "text"):
                    return header + block.text.strip()
        except Exception as exc:
            logger.debug("%s Haiku patrol format failed: %s", self.TITLE, exc)
        return header + simple_body

    async def _synthesize(
        self,
        prompt: str,
        model: str | None = None,
        effort: str = "high",
    ) -> str:
        """
        Stream a Claude response.
        Defaults to Sonnet (claude_brief_model) for cost efficiency on high-frequency C-suite briefs.
        Pass model=self._settings.claude_model for Opus when synthesis quality matters.
        """
        _model = model or self._settings.claude_brief_model
        try:
            async with self._client.messages.stream(
                model=_model,
                max_tokens=1024,
                thinking={"type": "adaptive"},
                system=[{
                    "type": "text",
                    "text": self._system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": effort},
            ) as stream:
                msg = await stream.get_final_message()
            if hasattr(msg, "usage"):
                _log_msg(
                    str(self._settings.db_path), self.TITLE,
                    _model,
                    msg.usage,
                    purpose="brief",
                )
            for block in reversed(msg.content):
                if hasattr(block, "text"):
                    return block.text.strip()
            return f"[{self.TITLE}: synthesis failed]"
        except Exception as exc:
            logger.error("%s synthesis failed: %s", self.TITLE, exc)
            return f"[{self.TITLE} synthesis error: {exc}]"

    async def _escalate_to_ceo(self, level: str, message: str) -> None:
        if self._ceo:
            await self._ceo.dispatch_alert(level, f"[{self.TITLE}] {message}")
        else:
            logger.warning("%s escalation (no CEO wired): [%s] %s", self.TITLE, level, message)

    @property
    def _system_prompt(self) -> str:
        return f"You are the {self.TITLE} of AGORA, an autonomous options trading platform owned by Rahul."

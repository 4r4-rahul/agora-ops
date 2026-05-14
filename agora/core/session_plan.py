"""
SessionPlan — CEO's published daily operating plan.

Produced at the morning board meeting (7 AM) and updated at the afternoon
review (3:30 PM). Every C-suite agent reads this and self-configures
accordingly — no manual Rahul intervention needed for routine sessions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


@dataclass
class SessionPlan:
    """
    CEO's operating directive for the current session.

    Agents read this via CEO.get_session_plan() and adjust their behavior:
      - CTO: gates new entries against max_new_positions and checks close_targets
      - CRO: uses size_bias to scale all new position sizing
      - CIO: uses macro_regime to weight signal confidence
      - CFO: uses risk_budget_remaining as capital ceiling for new positions
    """

    date: str                                   # "2026-05-14"
    stance: str                                 # "aggressive" | "neutral" | "defensive" | "halted"
    macro_regime: str                           # "risk_on" | "neutral" | "risk_off" | "crisis"
    size_bias: str                              # "full" | "half" | "quarter" | "none"
    max_new_positions: int                      # 0 = no new entries
    preferred_pillar: str                       # "vol_premium" | "catalyst" | "directional" | "any"
    close_targets: list[str]                    # tickers CEO wants closed today
    risk_budget_remaining: float                # $ of daily risk budget left
    notes: str                                  # 1-2 sentence CEO rationale
    published_at: str = field(
        default_factory=lambda: datetime.now(tz=ET).isoformat()
    )
    version: int = 1                            # increments on each afternoon update

    # Computed convenience properties
    @property
    def is_halted(self) -> bool:
        return self.stance == "halted" or self.max_new_positions == 0

    @property
    def size_multiplier(self) -> float:
        return {"full": 1.0, "half": 0.5, "quarter": 0.25, "none": 0.0}.get(
            self.size_bias, 1.0
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "date":                  self.date,
            "stance":                self.stance,
            "macro_regime":          self.macro_regime,
            "size_bias":             self.size_bias,
            "max_new_positions":     self.max_new_positions,
            "preferred_pillar":      self.preferred_pillar,
            "close_targets":         self.close_targets,
            "risk_budget_remaining": self.risk_budget_remaining,
            "notes":                 self.notes,
            "published_at":          self.published_at,
            "version":               self.version,
        }

    @staticmethod
    def default() -> "SessionPlan":
        """Safe default — neutral stance, standard sizing, no restrictions."""
        return SessionPlan(
            date=datetime.now(tz=ET).strftime("%Y-%m-%d"),
            stance="neutral",
            macro_regime="neutral",
            size_bias="full",
            max_new_positions=3,
            preferred_pillar="any",
            close_targets=[],
            risk_budget_remaining=500.0,
            notes="Default plan — morning board meeting not yet completed.",
        )

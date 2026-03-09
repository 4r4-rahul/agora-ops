"""Structured audit logging skeleton for trade lifecycle tracking."""
from __future__ import annotations

import logging
import os
import pathlib
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

import json

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TradeAuditRecord:
    event: str
    symbol: str
    plan_id: str
    timestamp: float
    payload: Dict[str, Any]


class AuditLogger:
    """Persist trade lifecycle events to durable storage (JSONL for now)."""

    def __init__(self, base_dir: Optional[str] = None) -> None:
        base = base_dir or os.getenv("AUDIT_LOG_DIR", "./audit_logs")
        self.base_path = pathlib.Path(base)
        self.base_path.mkdir(parents=True, exist_ok=True)

    def log_event(self, record: TradeAuditRecord) -> None:
        path = self.base_path / f"audit-{time.strftime('%Y-%m-%d')}.jsonl"
        line = json.dumps(asdict(record), separators=(",", ":"))
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        logger.debug("Audit event %s for %s written", record.event, record.symbol)

    def log_plan_generated(self, plan: Dict[str, Any]) -> None:
        record = TradeAuditRecord(
            event="plan_generated",
            symbol=plan.get("symbol", "UNKNOWN"),
            plan_id=plan.get("plan_id", plan.get("symbol", "UNKNOWN")),
            timestamp=time.time(),
            payload=plan,
        )
        self.log_event(record)

    def log_execution_result(self, plan_id: str, result: Dict[str, Any]) -> None:
        record = TradeAuditRecord(
            event="execution_result",
            symbol=result.get("symbol", "UNKNOWN"),
            plan_id=plan_id,
            timestamp=time.time(),
            payload=result,
        )
        self.log_event(record)

    def summarize_day(self, day: Optional[str] = None) -> List[Dict[str, Any]]:
        """Load all records for a day; placeholder for retraining hook."""
        day_str = day or time.strftime("%Y-%m-%d")
        path = self.base_path / f"audit-{day_str}.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line]


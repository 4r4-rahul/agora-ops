"""
agora/ops/langfuse_tracer.py — Langfuse observability integration.

Design principles:
  - Zero-impact when LANGFUSE_SECRET_KEY is not set: every call is a no-op.
  - No changes needed in agent code — wired through log_call() and run_with_tools().
  - Trace grouping: chain_id (per-ticker evaluation) maps to one Langfuse trace
    so you see analyst → advocate → defender → swing_judge as a single timeline.
  - Thread-safe: Langfuse SDK batches and flushes in a background thread.

Dashboard: https://cloud.langfuse.com  (or your self-hosted instance)

To enable: set LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY in .env.
           Optionally LANGFUSE_HOST for self-hosted (default: cloud.langfuse.com).
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

logger = logging.getLogger(__name__)

_client: Any = None          # Langfuse | None
_enabled: bool | None = None  # None = not yet checked
_lock = threading.Lock()

# ── Pricing mirror (kept in sync with llm_cost_log.py) ───────────────────────
_PRICE_PER_M: dict[str, tuple[float, float]] = {
    "claude-opus-4-8":   (5.00, 25.00),
    "claude-opus-4-7":   (5.00, 25.00),
    "claude-opus-4-6":   (5.00, 25.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5":  (1.00,  5.00),
}


def _get_client() -> Any:
    """Lazy-init Langfuse client. Returns None if keys not configured."""
    global _client, _enabled
    if _enabled is not None:
        return _client
    with _lock:
        if _enabled is not None:
            return _client
        secret = os.getenv("LANGFUSE_SECRET_KEY", "")
        public = os.getenv("LANGFUSE_PUBLIC_KEY", "")
        host   = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com")
        if not secret or not public:
            _enabled = False
            logger.info("Langfuse disabled — LANGFUSE_SECRET_KEY / PUBLIC_KEY not set")
            return None
        try:
            from langfuse import Langfuse
            _client = Langfuse(
                secret_key=secret,
                public_key=public,
                host=host,
                flush_interval=5,   # batch every 5s
                flush_at=20,        # or every 20 events
            )
            _enabled = True
            logger.info("Langfuse enabled — project dashboard at %s", host)
        except Exception as exc:
            logger.warning("Langfuse init failed (disabling): %s", exc)
            _enabled = False
        return _client


def log_generation(
    *,
    agent:         str,
    model:         str,
    input_tokens:  int,
    output_tokens: int,
    purpose:       str   = "",
    session_id:    str   = "",
    trace_id:      str   = "",   # chain_id / decision_id for grouping
    metadata:      dict  | None = None,
    input_text:    str   = "",   # optional: first 500 chars of prompt
    output_text:   str   = "",   # optional: first 500 chars of response
) -> None:
    """
    Record one Claude API call to Langfuse.
    Silent no-op if Langfuse is disabled or errors.
    """
    client = _get_client()
    if client is None:
        return
    try:
        in_p, out_p = next(
            (v for k, v in _PRICE_PER_M.items() if model.startswith(k)),
            (5.00, 25.00),
        )
        cost_usd = (input_tokens * in_p + output_tokens * out_p) / 1_000_000

        # Build or re-use a trace for this chain_id
        trace = client.trace(
            id=trace_id or None,          # None → Langfuse auto-generates
            name=f"{agent}",
            session_id=session_id or None,
            metadata={"purpose": purpose, **(metadata or {})},
        )

        trace.generation(
            name=f"{agent}/{purpose}" if purpose else agent,
            model=model,
            usage={
                "input":        input_tokens,
                "output":       output_tokens,
                "total":        input_tokens + output_tokens,
                "unit":         "TOKENS",
                "input_cost":   round(input_tokens  * in_p  / 1_000_000, 6),
                "output_cost":  round(output_tokens * out_p / 1_000_000, 6),
                "total_cost":   round(cost_usd, 6),
            },
            input=input_text[:2000] if input_text else None,
            output=output_text[:2000] if output_text else None,
            metadata={"agent": agent, "purpose": purpose},
        )
    except Exception as exc:
        logger.debug("Langfuse log_generation error (non-fatal): %s", exc)


def score_trace(
    trace_id: str,
    name: str,
    value: float,
    comment: str = "",
) -> None:
    """
    Attach a score to a trace — used to record trade outcome (win/loss) after close.
    name examples: 'trade_pnl_pct', 'advocate_correct', 'defender_correct'
    value: float (e.g. pnl_pct, or 1.0/0.0 for boolean outcomes)
    """
    client = _get_client()
    if client is None:
        return
    try:
        client.score(
            trace_id=trace_id,
            name=name,
            value=value,
            comment=comment[:500] if comment else None,
        )
    except Exception as exc:
        logger.debug("Langfuse score_trace error (non-fatal): %s", exc)


def flush() -> None:
    """Force-flush pending events — call on shutdown."""
    client = _get_client()
    if client is None:
        return
    try:
        client.flush()
    except Exception:
        pass


def is_enabled() -> bool:
    """Returns True if Langfuse is configured and reachable."""
    return _get_client() is not None

"""
LLM cost logger — writes per-call token usage and estimated cost to llm_cost_log.

Pricing (USD per 1M tokens, 2026-05):
  Opus 4.8:   $5.00 input / $25.00 output
  Opus 4.7:   $5.00 input / $25.00 output
  Sonnet 4.6: $3.00 input / $15.00 output
  Haiku 4.5:  $1.00 input / $5.00 output

Prompt caching multipliers (applied to the model's input price):
  cache write (cache_creation_input_tokens):  1.25x input price
  cache read  (cache_read_input_tokens):       0.10x input price

When caching is enabled, the Anthropic usage object splits input into three
buckets — `input_tokens` (uncached only), `cache_creation_input_tokens`, and
`cache_read_input_tokens`. Logging only `input_tokens` undercounts real spend,
so log_message() captures all three. Prefer log_message() over log_call() at
call sites — pass the raw usage object and it extracts every bucket safely.

One API call = one row. Aggregates via daily_cost_summary().
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import date, datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

_PRICE_PER_M: dict[str, tuple[float, float]] = {
    "claude-opus-4-8":   (5.00, 25.00),
    "claude-opus-4-7":   (5.00, 25.00),
    "claude-opus-4-6":   (5.00, 25.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5":  (1.00,  5.00),
}

# Prompt-caching multipliers applied to the model's per-token input price.
CACHE_WRITE_MULT = 1.25   # cache_creation_input_tokens
CACHE_READ_MULT = 0.10    # cache_read_input_tokens

DAILY_CAP_USD = 20.00  # paper/dev mode — observed real spend runs ~$17/day with caching

_lock = threading.Lock()


def _price_for(model: str) -> tuple[float, float]:
    for prefix, prices in _PRICE_PER_M.items():
        if model.startswith(prefix):
            return prices
    return (5.00, 25.00)  # unknown model → conservative Opus pricing


def _cost_for(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> float:
    """Estimated USD cost, accounting for cache-write/read pricing tiers."""
    in_price, out_price = _price_for(model)
    return (
        input_tokens * in_price
        + cache_creation_tokens * in_price * CACHE_WRITE_MULT
        + cache_read_tokens * in_price * CACHE_READ_MULT
        + output_tokens * out_price
    ) / 1_000_000


def ensure_table(db_path: str) -> None:
    """Create llm_cost_log table if it doesn't exist, and migrate older schemas.
    Safe to call on every startup."""
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS llm_cost_log (
                id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                ts                     TEXT NOT NULL,
                date                   TEXT NOT NULL,
                agent                  TEXT NOT NULL,
                purpose                TEXT NOT NULL DEFAULT '',
                model                  TEXT NOT NULL,
                input_tokens           INTEGER NOT NULL DEFAULT 0,
                output_tokens          INTEGER NOT NULL DEFAULT 0,
                cache_creation_tokens  INTEGER NOT NULL DEFAULT 0,
                cache_read_tokens      INTEGER NOT NULL DEFAULT 0,
                cost_usd               REAL NOT NULL DEFAULT 0,
                session_id             TEXT NOT NULL DEFAULT ''
            )
        """)
        # Migrate pre-cache schemas: add columns if they're missing.
        existing = {row[1] for row in conn.execute("PRAGMA table_info(llm_cost_log)")}
        for col in ("cache_creation_tokens", "cache_read_tokens"):
            if col not in existing:
                conn.execute(
                    f"ALTER TABLE llm_cost_log ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0"
                )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_llm_cost_date ON llm_cost_log(date)"
        )
        conn.commit()


def log_call(
    db_path: str,
    agent: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    purpose: str = "",
    session_id: str = "",
    trace_id: str = "",      # chain_id / decision_id — groups per-ticker calls in Langfuse
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> float:
    """
    Record one Claude API call. Returns estimated cost in USD.
    Thread-safe — called from async contexts via run_in_executor or directly.
    Also pushes to Langfuse when LANGFUSE_SECRET_KEY is configured (silent no-op otherwise).

    `input_tokens` is the UNCACHED input bucket. Pass cache_creation_tokens and
    cache_read_tokens separately so caching tiers are priced correctly — or use
    log_message() to extract all buckets from a usage object automatically.
    """
    input_tokens = input_tokens or 0
    output_tokens = output_tokens or 0
    cache_creation_tokens = cache_creation_tokens or 0
    cache_read_tokens = cache_read_tokens or 0
    cost = _cost_for(
        model, input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens
    )
    now = datetime.now(tz=ET)
    with _lock:
        try:
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """INSERT INTO llm_cost_log
                       (ts, date, agent, purpose, model,
                        input_tokens, output_tokens,
                        cache_creation_tokens, cache_read_tokens,
                        cost_usd, session_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        now.isoformat(),
                        now.date().isoformat(),
                        agent,
                        purpose,
                        model,
                        input_tokens,
                        output_tokens,
                        cache_creation_tokens,
                        cache_read_tokens,
                        round(cost, 6),
                        session_id,
                    ),
                )
        except Exception:
            pass  # never let cost logging break a trade path

    # Langfuse observability — silent no-op when not configured.
    # Pass total input (all buckets) so token counts reflect reality.
    try:
        from agora.ops.langfuse_tracer import log_generation
        log_generation(
            agent=agent,
            model=model,
            input_tokens=input_tokens + cache_creation_tokens + cache_read_tokens,
            output_tokens=output_tokens,
            purpose=purpose,
            session_id=session_id,
            trace_id=trace_id,
        )
    except Exception:
        pass

    return cost


def log_message(
    db_path: str,
    agent: str,
    model: str,
    usage,
    purpose: str = "",
    session_id: str = "",
    trace_id: str = "",
) -> float:
    """
    Log a call directly from an Anthropic `usage` object (response.usage / msg.usage).
    Extracts uncached input, cache-creation, and cache-read buckets safely — preferred
    over log_call() because it can't accidentally drop the cache buckets.
    Returns estimated cost in USD; a None/missing usage object logs zero-cost.
    """
    if usage is None:
        return 0.0
    return log_call(
        db_path,
        agent,
        model,
        getattr(usage, "input_tokens", 0) or 0,
        getattr(usage, "output_tokens", 0) or 0,
        purpose=purpose,
        session_id=session_id,
        trace_id=trace_id,
        cache_creation_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
    )


def daily_cost_summary(db_path: str, for_date: str | None = None) -> dict:
    """
    Return today's cost breakdown by agent and model.
    Used by CFO patrol, dashboard /agora/costs endpoint.
    """
    target = for_date or date.today().isoformat()
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                """SELECT agent, model, COUNT(*) AS calls,
                          SUM(input_tokens) AS in_tok,
                          SUM(output_tokens) AS out_tok,
                          SUM(cache_creation_tokens) AS cc_tok,
                          SUM(cache_read_tokens) AS cr_tok,
                          SUM(cost_usd) AS cost
                   FROM llm_cost_log
                   WHERE date = ?
                   GROUP BY agent, model
                   ORDER BY cost DESC""",
                (target,),
            ).fetchall()
    except Exception:
        rows = []

    total = sum(r[7] for r in rows)
    return {
        "date":      target,
        "total_usd": round(total, 4),
        "cap_usd":   DAILY_CAP_USD,
        "pct_cap":   round(total / DAILY_CAP_USD * 100, 1) if total else 0.0,
        "over_cap":  total > DAILY_CAP_USD,
        "by_agent":  [
            {
                "agent":           r[0],
                "model":           r[1],
                "calls":           r[2],
                "in_tokens":       r[3],
                "out_tokens":      r[4],
                "cache_write_tokens": r[5],
                "cache_read_tokens":  r[6],
                "cost_usd":        round(r[7], 4),
            }
            for r in rows
        ],
    }

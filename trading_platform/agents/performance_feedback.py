"""
PerformanceFeedbackAgent — weekly learning loop.

Reads the trade journal, analyzes win/loss patterns, and produces a
feedback report that updates conviction scoring weights and session
configuration. Runs once per week (external cron or manual trigger).

No Claude call for data analysis — pure statistics.
Claude is called only to synthesize qualitative lessons from closed trades.

Output stored in: ./performance_feedback.json
The ConvictionAgent reads this file on startup to apply learned weights.

Usage:
    python -m trading_platform.agents.performance_feedback
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DB_PATH = Path("./trade_journal.db")
FEEDBACK_PATH = Path("./performance_feedback.json")

# Minimum trades before we trust the statistics
_MIN_TRADES_FOR_WEIGHT_UPDATE = 10


def _load_closed_trades(days: int = 90) -> list[dict]:
    """Load closed trades from the last `days` days."""
    if not DB_PATH.exists():
        return []
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT * FROM trade_journal
            WHERE status = 'closed'
              AND closed_at > ?
            ORDER BY closed_at DESC
            """,
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


def compute_statistics(trades: list[dict]) -> dict[str, Any]:
    """Pure statistics — no AI."""
    if not trades:
        return {"trade_count": 0}

    wins   = [t for t in trades if (t.get("realized_pnl") or 0) > 0]
    losses = [t for t in trades if (t.get("realized_pnl") or 0) <= 0]

    win_rate = len(wins) / len(trades)
    avg_win  = sum(t["realized_pnl"] for t in wins)  / len(wins)  if wins   else 0
    avg_loss = sum(t["realized_pnl"] for t in losses) / len(losses) if losses else 0
    expectancy = (win_rate * avg_win) + ((1 - win_rate) * avg_loss)

    gross_wins   = sum(t["realized_pnl"] for t in wins)   if wins   else 0
    gross_losses = abs(sum(t["realized_pnl"] for t in losses)) if losses else 1
    profit_factor = gross_wins / gross_losses

    # Kelly fraction (simplified: W - (1-W)/R)
    avg_rr = abs(avg_win / avg_loss) if avg_loss else 1.0
    kelly = win_rate - (1 - win_rate) / avg_rr if avg_rr > 0 else 0
    kelly_half = max(0, kelly / 2)  # half-kelly for safety

    # Performance by strategy
    by_strategy: dict[str, list] = defaultdict(list)
    for t in trades:
        by_strategy[t.get("strategy", "unknown")].append(t.get("realized_pnl", 0))

    strategy_stats = {}
    for strat, pnls in by_strategy.items():
        w = sum(1 for p in pnls if p > 0)
        strategy_stats[strat] = {
            "count": len(pnls),
            "win_rate": round(w / len(pnls), 2),
            "avg_pnl": round(sum(pnls) / len(pnls), 2),
            "total_pnl": round(sum(pnls), 2),
        }

    # Performance by exit reason
    by_exit: dict[str, list] = defaultdict(list)
    for t in trades:
        by_exit[t.get("exit_reason", "unknown")].append(t.get("realized_pnl", 0))

    exit_stats = {
        reason: {
            "count": len(pnls),
            "avg_pnl": round(sum(pnls) / len(pnls), 2),
        }
        for reason, pnls in by_exit.items()
    }

    # Day-of-week performance
    dow_pnl: dict[str, list] = defaultdict(list)
    dow_map = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri"}
    for t in trades:
        try:
            opened = datetime.fromisoformat(t["opened_at"])
            day = dow_map.get(opened.weekday(), "unknown")
            dow_pnl[day].append(t.get("realized_pnl", 0))
        except Exception:
            pass

    dow_stats = {
        day: {"count": len(pnls), "avg_pnl": round(sum(pnls) / len(pnls), 2)}
        for day, pnls in dow_pnl.items()
    }

    # Suggested conviction weight adjustments based on empirical results
    # If win rate < 45% on Mon/Fri, reinforce the DOW penalty
    weights_adjustment: dict[str, float] = {}
    for day, stats in dow_stats.items():
        if stats["count"] >= 5:
            wr = sum(1 for t in trades
                     if dow_map.get(datetime.fromisoformat(t["opened_at"]).weekday()) == day
                     and (t.get("realized_pnl") or 0) > 0) / stats["count"]
            if wr < 0.40:
                weights_adjustment[f"dow_{day.lower()}_penalty"] = 0.1  # increase penalty
            elif wr > 0.65:
                weights_adjustment[f"dow_{day.lower()}_bonus"] = 0.1    # reduce penalty

    return {
        "trade_count": len(trades),
        "win_rate": round(win_rate, 3),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "expectancy": round(expectancy, 2),
        "profit_factor": round(profit_factor, 2),
        "kelly_fraction": round(kelly, 3),
        "half_kelly": round(kelly_half, 3),
        "by_strategy": strategy_stats,
        "by_exit_reason": exit_stats,
        "by_day_of_week": dow_stats,
        "weight_adjustments": weights_adjustment,
        "computed_at": datetime.utcnow().isoformat(),
        "lookback_days": 90,
    }


async def run_feedback_loop(use_claude: bool = True) -> dict[str, Any]:
    """
    Full feedback loop: load trades → compute stats → optionally Claude summary → save.
    """
    trades = _load_closed_trades(days=90)
    stats = compute_statistics(trades)

    if not trades:
        logger.info("No closed trades found — nothing to learn from yet.")
        result = stats
    else:
        logger.info(
            "Analyzing %d trades: WR=%.1f%% PF=%.2f Kelly=%.2f",
            len(trades), stats["win_rate"] * 100,
            stats["profit_factor"], stats["kelly_fraction"],
        )

        if use_claude and len(trades) >= 3:
            try:
                lessons = await _claude_synthesize_lessons(trades, stats)
                stats["qualitative_lessons"] = lessons
            except Exception as exc:
                logger.warning("Claude lesson synthesis failed: %s", exc)

        result = stats

    # Save feedback for ConvictionAgent to read
    FEEDBACK_PATH.write_text(json.dumps(result, indent=2))
    logger.info("Performance feedback written to %s", FEEDBACK_PATH)
    return result


async def _claude_synthesize_lessons(trades: list[dict], stats: dict) -> list[str]:
    """Ask Claude to identify qualitative patterns in closed trades."""
    import anthropic

    # Build trade summary text
    trade_lines = []
    for t in trades[-20:]:  # last 20 trades
        pnl = t.get("realized_pnl", 0)
        trade_lines.append(
            f"  {t.get('opened_at', '')[:10]} {t.get('ticker'):5} {t.get('strategy'):20} "
            f"{t.get('direction'):7} exit={t.get('exit_reason','?'):25} PnL=${pnl:+,.0f} "
            f"thesis: {(t.get('thesis') or '')[:80]}"
        )

    trades_text = "\n".join(trade_lines)
    stats_summary = json.dumps({
        k: v for k, v in stats.items()
        if k not in ("computed_at", "weight_adjustments")
    }, indent=2)

    prompt = f"""You are a trading performance coach reviewing recent options trades.

STATISTICS (last 90 days):
{stats_summary}

RECENT TRADES:
{trades_text}

Identify 3-5 specific, actionable lessons from these results. Be concrete:
- What patterns lead to losses? (e.g., "trading on Fridays consistently loses")
- What exit reasons are most profitable vs least?
- What strategies work in the current market regime vs which to avoid?
- Any thesis invalidation patterns?

Return a JSON list of lesson strings: ["lesson 1", "lesson 2", ...]
"""

    try:
        from ..core.config import get_settings
        settings = get_settings()
    except Exception:
        from dotenv import load_dotenv
        import os
        load_dotenv()
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    client = anthropic.AsyncAnthropic(
        api_key=getattr(settings, "anthropic_api_key", api_key) if "settings" in dir() else api_key
    )

    response = await client.messages.create(
        model="claude-opus-4-7",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    text = response.content[0].text.strip()
    # Extract JSON array
    import re
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match:
        return json.loads(match.group())
    return [text]


if __name__ == "__main__":
    import asyncio
    logging.basicConfig(level=logging.INFO)
    result = asyncio.run(run_feedback_loop())
    print(json.dumps(result, indent=2))

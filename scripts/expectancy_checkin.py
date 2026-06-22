#!/usr/bin/env python3
"""
scripts/expectancy_checkin.py — periodic expectancy check-in (set up 2026-06-18).

Snapshots the North-Star meter + the post-close exit-regret report + the cell-gate state, prints a
human summary, appends a JSONL history row (so the trend is reviewable), and best-effort posts the
summary to Discord. Reads the DB directly via the agora modules, so it works whether or not the
live engine is up. Never raises — a check-in must never crash its scheduler.

Run ad-hoc:  python scripts/expectancy_checkin.py
Scheduled :  com.agora.expectancy_checkin (launchd, weekday EOD)
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _fmt_money(v):
    return "—" if v is None else (f"+${v:.0f}" if v >= 0 else f"-${abs(v):.0f}")


def build_snapshot() -> tuple[dict, str]:
    """Returns (snapshot_dict, human_summary). Never raises."""
    try:
        from agora.core.config import get_settings
        from agora.ops.cell_gate import blocked_cells
        from agora.ops.expectancy_meter import build_meter, credit_spread_stats
        from agora.ops.post_close_watch import evaluate_due, exit_regret_report

        s = get_settings()
        db = str(s.db_path)
        cutoff = getattr(s, "expectancy_legacy_cutoff_date", "2026-06-12")

        # score any post-close counterfactuals whose horizon has passed (idempotent)
        try:
            evaluate_due(db)
        except Exception:
            pass

        meter = build_meter(
            db,
            target_per_trade=getattr(s, "expectancy_target_per_trade", 25.0),
            target_date=getattr(s, "expectancy_target_date", "2026-09-30"),
            legacy_cutoff=cutoff,
            upgrade_milestone=getattr(s, "expectancy_upgrade_milestone_date", "2026-06-22"),
        )
        regret = exit_regret_report(db)
        cells = blocked_cells(
            db, cutoff=cutoff,
            min_samples=getattr(s, "cell_gate_min_samples", 8),
            min_expectancy=getattr(s, "cell_gate_min_expectancy", -15.0),
        )

        cur = meter.get("current", {})
        tgt = meter.get("target", {})
        snap = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "expectancy": cur.get("expectancy"),
            "n_post_fix": cur.get("n"),
            "win_rate": cur.get("win_rate"),
            "payoff": cur.get("payoff_ratio"),
            "baseline": meter.get("baseline_expectancy"),
            "progress_pct": meter.get("progress_pct"),
            "status": meter.get("status"),
            "target": tgt.get("per_trade"),
            "days_left": tgt.get("days_remaining"),
            "blocked_cells": list(cells.get("blocked", {}).keys()),
            "exit_regret": regret.get("by_exit_reason", {}),
            "regret_evaluated": regret.get("total_evaluated", 0),
            "credit_spreads": credit_spread_stats(db),
            "since_upgrade": meter.get("since_upgrade", {}),
        }

        wr = f"{cur.get('win_rate', 0) * 100:.0f}%" if cur.get("win_rate") is not None else "—"
        lines = [
            "📊 **Expectancy check-in**",
            f"Current **{_fmt_money(snap['expectancy'])}/trade** (n={snap['n_post_fix']}, WR {wr}, "
            f"payoff {snap['payoff'] or '—'}×) · baseline {_fmt_money(snap['baseline'])}",
            f"Target **{_fmt_money(snap['target'])}** · progress **{snap['progress_pct']}%** · "
            f"{snap['days_left']}d left · _{snap['status']}_",
        ]
        if snap["blocked_cells"]:
            lines.append(f"🚫 Benched cells: {', '.join(snap['blocked_cells'])}")
        if snap["regret_evaluated"]:
            worst = sorted(snap["exit_regret"].items(),
                           key=lambda kv: -kv[1].get("early_exit_rate", 0))[:3]
            lines.append("Exit-regret (early-exit rate by reason): "
                         + ", ".join(f"{r} {d['early_exit_rate']:.0%} (n={d['n']})" for r, d in worst))
        else:
            lines.append("_Exit-regret: no closed-trade counterfactuals scored yet — accumulating._")
        su = snap["since_upgrade"] or {}
        if su:
            _se = su.get("expectancy")
            _gl = ("✅ TRADABLE LIVE" if su.get("tradable_live")
                   else f"need n≥30 + positive (n={su.get('n')}, "
                        f"{'positive' if (_se or 0) > 0 else 'negative'})")
            _wr = f"{su.get('win_rate', 0) * 100:.0f}%" if su.get("win_rate") is not None else "—"
            lines.append(f"🎯 **Upgraded system** (since {su.get('date')}): "
                         f"**{_fmt_money(_se)}/trade** · n={su.get('n')} · WR {_wr} · {_gl}")
        cs = snap["credit_spreads"]
        if cs["entered"]:
            wr = f"{cs['win_rate'] * 100:.0f}%" if cs["win_rate"] is not None else "—"
            lines.append(f"📐 Credit spreads (W1, since 06-18): {cs['entered']} entered · "
                         f"{cs['open']} open · {cs['closed']} closed · win {wr}")
        else:
            lines.append("_📐 Credit spreads (W1): none traded yet — cr/w still clustering at the 0.30 gate._")
        return snap, "\n".join(lines)
    except Exception as exc:  # pragma: no cover - defensive
        return {"error": str(exc)}, f"⚠️ expectancy check-in failed: {exc}"


def _append_history(snap: dict) -> None:
    try:
        hist = ROOT / "agora" / "logs" / "expectancy_history.jsonl"
        hist.parent.mkdir(parents=True, exist_ok=True)
        with hist.open("a") as f:
            f.write(json.dumps(snap, default=str) + "\n")
    except Exception:
        pass


def _post_discord(summary: str) -> None:
    """Best-effort Discord push. Never echoes the webhook URL."""
    try:
        from agora.core.config import get_settings
        url = getattr(get_settings(), "alert_webhook_url", None)
        if not url:
            return
        import httpx
        for i in range(0, len(summary), 1900):
            httpx.post(url, json={"content": summary[i:i + 1900]}, timeout=10)
    except Exception:
        pass


def main() -> int:
    snap, summary = build_snapshot()
    print(summary)
    _append_history(snap)
    _post_discord(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
scripts/validation_watch.py — daily shadow→live promotion watch (set up 2026-06-24).

The engine carries a large SHADOW apparatus (entry recalibrations, per-ticker caps, news-response,
position surveillance) that must be VALIDATED before promotion. This job reports, every weekday before
the open, on each pending item and flags anything READY to promote — read-only, never auto-promotes.
Reads the local DB directly (works whether or not the engine is up). Never raises — a watch must never
crash its scheduler.

Run ad-hoc:  python scripts/validation_watch.py
Scheduled :  com.agora.validation_watch (launchd, weekday 8:00am local)
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RECAL_DATE = "2026-06-24"      # the entry-recalibration deploy date
PROMOTE_MIN_CLOSES = 6        # per-ticker closes before a shadow cap is Phase-3-eligible
PROMOTE_MIN_SURV = 25        # surveillance structure-stop verdicts to consider promoting them


def _q(c: sqlite3.Connection, sql: str, params=()):
    try:
        return c.execute(sql, params).fetchone()
    except Exception:
        return None


def build_report() -> str:
    lines = ["🔭 **AGORA validation watch** — shadow→live readiness"]
    ready: list[str] = []
    try:
        from agora.core.config import get_settings
        from agora.ops.edge_dashboard import _REAL_CLOSE
        s = get_settings()
        c = sqlite3.connect(str(s.db_path), timeout=10)
    except Exception as exc:
        return f"🔭 AGORA validation watch — could not open DB: {exc}"

    # 1. Win-rate + P&L since the recalibration (is the 55% holding?)
    r = _q(c, f"SELECT COUNT(*), COALESCE(SUM(realized_pnl),0), SUM(realized_pnl>0) "
              f"FROM positions WHERE {_REAL_CLOSE} AND realized_pnl IS NOT NULL AND close_date>=?",
           (RECAL_DATE,))
    if r and r[0]:
        n, pnl, w = r[0], r[1], (r[2] or 0)
        wr = w / n * 100
        lines.append(f"1️⃣ Since {RECAL_DATE}: n={n} | P&L=${pnl:.0f} | win={wr:.0f}% "
                     f"({'✅ holding ≥50%' if wr >= 50 else '⚠️ below 50%'})")
    else:
        lines.append(f"1️⃣ Since {RECAL_DATE}: no real closes yet")

    # 2. Surveillance verdicts — structure stops ready to promote?
    sv = _q(c, "SELECT COUNT(*), SUM(action='EXIT') FROM surveillance_log") \
        if _q(c, "SELECT 1 FROM sqlite_master WHERE name='surveillance_log'") else None
    if sv and sv[0]:
        struct = _q(c, "SELECT COUNT(*) FROM surveillance_log WHERE action='EXIT' AND reason LIKE '%stop%'")
        n_struct = struct[0] if struct else 0
        rdy = n_struct >= PROMOTE_MIN_SURV
        lines.append(f"2️⃣ Surveillance: {sv[0]} verdicts, {n_struct} structure-stop EXITs "
                     f"({'✅ enough to review promotion' if rdy else f'need ≥{PROMOTE_MIN_SURV}'})")
        if rdy:
            ready.append("Surveillance structure stops — review `surveillance_log`, then set "
                         "`SURVEILLANCE_ACT_ALL_STOPS=true`")
    else:
        lines.append("2️⃣ Surveillance: no verdicts logged yet")

    # 3. Per-ticker shadow caps — any ticker with enough closes for Phase 3?
    if _q(c, "SELECT 1 FROM sqlite_master WHERE name='ticker_settings'"):
        sc = _q(c, "SELECT COUNT(*) FROM ticker_settings WHERE active=0")
        lc = _q(c, "SELECT COUNT(*) FROM ticker_settings WHERE active=1")
        prom = _q(c, f"SELECT COUNT(*) FROM (SELECT ticker FROM positions WHERE {_REAL_CLOSE} "
                     f"AND realized_pnl IS NOT NULL GROUP BY ticker HAVING COUNT(*)>=?)",
                  (PROMOTE_MIN_CLOSES,))
        n_prom = prom[0] if prom else 0
        lines.append(f"3️⃣ Per-ticker caps: {sc[0] if sc else 0} shadow / {lc[0] if lc else 0} live | "
                     f"{n_prom} ticker(s) with ≥{PROMOTE_MIN_CLOSES} closes "
                     f"({'✅ Phase-3 candidates' if n_prom else 'none yet'})")
        if n_prom:
            ready.append(f"Per-ticker caps — {n_prom} ticker(s) have ≥{PROMOTE_MIN_CLOSES} closes; "
                         "review then promote down-only")
    else:
        lines.append("3️⃣ Per-ticker caps: store not initialized yet")

    # 4. News-response accrual
    if _q(c, "SELECT 1 FROM sqlite_master WHERE name='news_events'"):
        ne = _q(c, "SELECT COUNT(*), SUM(captured=1) FROM news_events")
        try:
            from agora.ops.news_events import news_response_profile
            nprof = len(news_response_profile(str(s.db_path)))
        except Exception:
            nprof = 0
        lines.append(f"4️⃣ News: {ne[0] if ne else 0} events ({(ne[1] or 0) if ne else 0} w/ fwd return) | "
                     f"{nprof} per-ticker response profile(s)")
    else:
        lines.append("4️⃣ News: capture not initialized yet")

    # 5. MFE/MAE accrual for exit tuning (#1)
    mm = _q(c, "SELECT SUM(peak_unrealized_pnl IS NOT NULL) FROM positions WHERE status='open'")
    cl = _q(c, f"SELECT COUNT(*) FROM positions WHERE {_REAL_CLOSE} AND peak_unrealized_pnl IS NOT NULL")
    lines.append(f"5️⃣ MFE/MAE: {(mm[0] or 0) if mm else 0} open tracking | "
                 f"{cl[0] if cl else 0} closed w/ clean path data "
                 f"({'✅ enough to tune exits' if cl and cl[0] >= 20 else 'accruing (need ~20 closes)'})")
    if cl and cl[0] >= 20:
        ready.append("Exit tuning (#1) — ≥20 closes now carry clean MFE/MAE; tune with evidence")

    c.close()

    if ready:
        lines.append("\n🚦 **READY TO PROMOTE:**")
        lines.extend(f"  • {r}" for r in ready)
    else:
        lines.append("\n⏳ Nothing ready to promote yet — keep accruing data.")
    return "\n".join(lines)


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
    report = build_report()
    print(report)
    _post_discord(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

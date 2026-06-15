"""
AgentPerformanceMonitor — tracks which agents and signals generate actual alpha.

The question: "Is each signal alpha-generating?"
This monitors performance by: pillar, regime, conviction band, agent source.

Runs analysis every Monday + EOD after enough trades accumulate.
Feeds insights back to:
  - CEOAgent: weekly performance attribution
  - DisagreementResolver: which pillars should get higher weight (future)
  - MacroSynthesizer: which regimes we perform best in

Data source: positions table real broker fills via the canonical _REAL_CLOSE predicate
(NOT trade_records — those are model marks that disagreed in SIGN with the fills).
No market data fetches — pure DB analytics.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from ..core.config import AgoraSettings, get_settings
from .edge_dashboard import _REAL_CLOSE

logger = logging.getLogger(__name__)

_MIN_TRADES_TO_REPORT = 5   # don't report stats for buckets with < 5 trades


@dataclass
class PillarStats:
    pillar: str
    trades: int
    wins: int
    losses: int
    win_rate: float
    avg_pnl: float
    total_pnl: float
    avg_conviction: float   # average conviction at entry for this pillar's trades


@dataclass
class AgentPerformanceReport:
    generated_at: date
    lookback_days: int
    total_trades: int
    overall_win_rate: float
    overall_pnl: float

    by_pillar: list[PillarStats] = field(default_factory=list)
    by_regime: dict[str, dict] = field(default_factory=dict)
    by_conviction_band: dict[str, dict] = field(default_factory=dict)

    # Actionable insights (Claude-generated if enough data)
    top_performing_pillar: str = ""
    worst_performing_pillar: str = ""
    best_regime: str = ""
    insights: list[str] = field(default_factory=list)


class AgentPerformanceMonitor:
    """
    Reads trade_records from SQLite and computes alpha attribution.
    All methods are synchronous — no async needed (pure DB reads).
    """

    def __init__(self, settings: AgoraSettings | None = None) -> None:
        self._settings = settings or get_settings()
        self._db_path  = str(self._settings.db_path)

    def get_latest_snapshot(self) -> dict:
        """Return a quick summary dict for CFO briefing (no full report computation)."""
        report = self.generate_report(lookback_days=30)
        if not report:
            return {"status": "no_data"}
        return {
            "total_trades": report.total_trades,
            "win_rate": round(report.overall_win_rate * 100, 1),
            "top_pillar": report.top_performing_pillar,
            "worst_pillar": report.worst_performing_pillar,
            "by_pillar": [
                {"pillar": p.pillar, "trades": p.trades, "win_rate": round(p.win_rate * 100, 1),
                 "total_pnl": round(p.total_pnl, 2)}
                for p in report.by_pillar
            ],
        }

    def generate_report(self, lookback_days: int = 30) -> AgentPerformanceReport | None:
        """Generate a full performance attribution report."""
        try:
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()

            # Fetch all REAL-fill closes in window. Was FROM trade_records (model marks that
            # disagree in SIGN with the broker fills — it read +$12.3k/65% win while the real
            # fills were -$3.2k/15% win). Missed in the 2026-06-12 fiction purge that repointed
            # attribution/strategy_health/circuit_breaker; this monitor kept emitting fiction into
            # the feed AND into the CFO's alpha attribution (best_pillar/best_regime), steering the
            # system toward pillars that only "win" on fake P&L. Now sourced from the canonical
            # _REAL_CLOSE predicate so every attribution number is honest.
            rows = conn.execute(f"""
                SELECT pillar, regime_at_entry, conviction_at_entry, realized_pnl,
                       strategy, close_date
                FROM positions
                WHERE {_REAL_CLOSE} AND close_date >= ? AND realized_pnl IS NOT NULL
                ORDER BY close_date DESC
            """, (cutoff,)).fetchall()
            conn.close()

            if not rows:
                return None

            report = AgentPerformanceReport(
                generated_at=date.today(),
                lookback_days=lookback_days,
                total_trades=len(rows),
                overall_win_rate=sum(1 for r in rows if r[3] > 0) / len(rows),
                overall_pnl=sum(r[3] for r in rows),
            )

            # By pillar
            report.by_pillar = self._compute_by_pillar(rows)

            # By regime
            report.by_regime = self._compute_by_group(rows, group_col=1, label="regime")

            # By conviction band
            report.by_conviction_band = self._compute_by_conviction(rows)

            # Insights
            report.insights = self._generate_insights(report)
            if report.by_pillar:
                best = max(report.by_pillar, key=lambda s: s.win_rate if s.trades >= _MIN_TRADES_TO_REPORT else -1)
                worst = min(report.by_pillar, key=lambda s: s.win_rate if s.trades >= _MIN_TRADES_TO_REPORT else 2)
                report.top_performing_pillar = best.pillar
                report.worst_performing_pillar = worst.pillar
            if report.by_regime:
                best_r = max(
                    report.by_regime.items(),
                    key=lambda kv: kv[1].get("win_rate", 0) if kv[1].get("trades", 0) >= _MIN_TRADES_TO_REPORT else -1,
                )
                report.best_regime = best_r[0]

            logger.info(
                "AgentPerformance: %d trades | win_rate=%.1f%% | total_pnl=$%.0f | "
                "best_pillar=%s | best_regime=%s",
                report.total_trades,
                report.overall_win_rate * 100,
                report.overall_pnl,
                report.top_performing_pillar,
                report.best_regime,
            )
            return report

        except Exception as exc:
            logger.error("AgentPerformanceMonitor.generate_report failed: %s", exc)
            return None

    def _compute_by_pillar(self, rows: list) -> list[PillarStats]:
        buckets: dict[str, list] = {}
        for row in rows:
            pillar = row[0] or "unknown"
            buckets.setdefault(pillar, []).append(row)

        stats = []
        for pillar, trades in buckets.items():
            if len(trades) < 2:
                continue
            pnls = [t[3] for t in trades]
            convictions = [float(t[2] or 0) for t in trades]
            wins = sum(1 for p in pnls if p > 0)
            stats.append(PillarStats(
                pillar=pillar,
                trades=len(trades),
                wins=wins,
                losses=len(trades) - wins,
                win_rate=wins / len(trades),
                avg_pnl=sum(pnls) / len(pnls),
                total_pnl=sum(pnls),
                avg_conviction=sum(convictions) / len(convictions) if convictions else 0,
            ))

        stats.sort(key=lambda s: s.total_pnl, reverse=True)
        return stats

    def _compute_by_group(self, rows: list, group_col: int, label: str) -> dict[str, dict]:
        buckets: dict[str, list[float]] = {}
        for row in rows:
            key = row[group_col] or "unknown"
            buckets.setdefault(key, []).append(row[3])

        result = {}
        for key, pnls in buckets.items():
            if len(pnls) < _MIN_TRADES_TO_REPORT:
                continue
            wins = sum(1 for p in pnls if p > 0)
            result[key] = {
                "trades":   len(pnls),
                "wins":     wins,
                "win_rate": wins / len(pnls),
                "avg_pnl":  sum(pnls) / len(pnls),
                "total_pnl": sum(pnls),
            }
        return result

    def _compute_by_conviction(self, rows: list) -> dict[str, dict]:
        bands = {"0-40": [], "40-60": [], "60-80": [], "80-100": []}
        for row in rows:
            conv = float(row[2] or 0)
            pnl  = row[3]
            if conv < 40:
                bands["0-40"].append(pnl)
            elif conv < 60:
                bands["40-60"].append(pnl)
            elif conv < 80:
                bands["60-80"].append(pnl)
            else:
                bands["80-100"].append(pnl)

        result = {}
        for band, pnls in bands.items():
            if len(pnls) < _MIN_TRADES_TO_REPORT:
                continue
            wins = sum(1 for p in pnls if p > 0)
            result[band] = {
                "trades":   len(pnls),
                "win_rate": wins / len(pnls),
                "avg_pnl":  sum(pnls) / len(pnls),
                "total_pnl": sum(pnls),
            }
        return result

    def _generate_insights(self, report: AgentPerformanceReport) -> list[str]:
        insights = []

        # Pillar insights
        for ps in report.by_pillar:
            if ps.trades >= _MIN_TRADES_TO_REPORT:
                if ps.win_rate >= 0.65:
                    insights.append(
                        f"✅ {ps.pillar}: strong alpha ({ps.win_rate:.0%} win rate, "
                        f"avg ${ps.avg_pnl:.0f}/trade)"
                    )
                elif ps.win_rate < 0.40:
                    insights.append(
                        f"⚠️ {ps.pillar}: underperforming ({ps.win_rate:.0%} win rate, "
                        f"avg ${ps.avg_pnl:.0f}/trade) — consider reducing allocation"
                    )

        # Conviction insights
        for band, stats in report.by_conviction_band.items():
            if stats["win_rate"] < 0.40 and band in ("60-80", "80-100"):
                insights.append(
                    f"⚠️ High-conviction ({band}) trades underperforming: "
                    f"{stats['win_rate']:.0%} win rate — review scoring model"
                )
            elif stats["win_rate"] >= 0.70 and band == "80-100":
                insights.append(
                    f"✅ Very high conviction (80-100) working: {stats['win_rate']:.0%} win rate"
                )

        # Regime insights
        for regime, stats in report.by_regime.items():
            if stats["win_rate"] < 0.35:
                insights.append(
                    f"⚠️ {regime} regime: losing money ({stats['win_rate']:.0%} win rate) — "
                    f"consider skipping this regime"
                )

        return insights[:5]   # cap to 5 most important

"""
P&L Attribution — break down realized P&L by strategy pillar.

Answers: which edge is actually working?
  - Vol Premium pillar: credit spread P&L
  - Directional pillar: debit spread P&L
  - Event FOMC: pre-FOMC drift P&L
  - Event CPI: IV crush condor P&L
  - Post-Earnings: skew reversion P&L
  - Catalyst: 8-K discovery P&L
  - Smart Money: 13D/Form4 cluster P&L

Also tracks:
  - Slippage: (expected_mid - actual_fill) per trade
  - Win rate and average win/loss by pillar
  - PSI (Population Stability Index): signal feature drift detection
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from ..core.config import AgoraSettings, get_settings

logger = logging.getLogger(__name__)


class PnlAttributor:
    """
    Reads from trade_records table and produces attribution reports.
    All reads — no writes except PSI data accumulation.
    """

    def __init__(self, settings: AgoraSettings | None = None) -> None:
        self._settings = settings or get_settings()
        self._db = sqlite3.connect(str(self._settings.db_path), check_same_thread=False)

    def attribution_report(
        self,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> dict[str, Any]:
        """
        Full P&L attribution by pillar for the specified date range.
        Defaults to last 30 days.
        """
        end_date = end_date or date.today()
        start_date = start_date or (end_date - timedelta(days=30))

        rows = self._db.execute("""
            SELECT pillar, realized_pnl, commission, slippage,
                   entry_date, close_date, ticker, strategy
            FROM trade_records
            WHERE close_date IS NOT NULL
              AND close_date >= ? AND close_date <= ?
        """, (start_date.isoformat(), end_date.isoformat())).fetchall()

        pillar_stats: dict[str, dict] = defaultdict(lambda: {
            "trades": 0, "wins": 0, "total_pnl": 0.0,
            "total_commission": 0.0, "total_slippage": 0.0,
            "win_pnl": 0.0, "loss_pnl": 0.0,
        })

        for row in rows:
            pillar, pnl, comm, slip = row[0], row[1] or 0.0, row[2] or 0.0, row[3] or 0.0
            net_pnl = pnl - comm - slip
            s = pillar_stats[pillar]
            s["trades"] += 1
            s["total_pnl"] += net_pnl
            s["total_commission"] += comm
            s["total_slippage"] += slip
            if net_pnl > 0:
                s["wins"] += 1
                s["win_pnl"] += net_pnl
            else:
                s["loss_pnl"] += net_pnl

        # Compute derived metrics
        result: dict[str, Any] = {
            "period": f"{start_date} to {end_date}",
            "total_pnl": sum(s["total_pnl"] for s in pillar_stats.values()),
            "total_trades": sum(s["trades"] for s in pillar_stats.values()),
            "pillars": {},
        }

        for pillar, s in pillar_stats.items():
            n = s["trades"]
            wins = s["wins"]
            result["pillars"][pillar] = {
                "trades": n,
                "win_rate": round(wins / n, 3) if n > 0 else 0.0,
                "total_pnl": round(s["total_pnl"], 2),
                "avg_win": round(s["win_pnl"] / wins, 2) if wins > 0 else 0.0,
                "avg_loss": round(s["loss_pnl"] / max(1, n - wins), 2),
                "profit_factor": round(
                    s["win_pnl"] / max(0.01, abs(s["loss_pnl"])), 3
                ),
                "total_slippage": round(s["total_slippage"], 2),
                "total_commission": round(s["total_commission"], 2),
            }

        return result

    def slippage_report(self, days: int = 30) -> dict[str, Any]:
        """
        Slippage analysis: how much are we paying vs mid?
        Expected mid is stored at recommendation time; actual fill from IBKR.
        """
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        rows = self._db.execute("""
            SELECT strategy, slippage, entry_price
            FROM trade_records
            WHERE entry_date >= ? AND slippage IS NOT NULL
        """, (cutoff,)).fetchall()

        if not rows:
            return {"days": days, "avg_slippage": 0.0, "total_slippage": 0.0}

        by_strategy: dict[str, list[float]] = defaultdict(list)
        for row in rows:
            strategy, slip, entry = row
            if entry and entry > 0:
                by_strategy[strategy].append(slip)

        return {
            "days": days,
            "avg_slippage": round(
                sum(v for vals in by_strategy.values() for v in vals)
                / max(1, sum(len(v) for v in by_strategy.values())),
                4,
            ),
            "total_slippage": round(
                sum(v for vals in by_strategy.values() for v in vals), 2
            ),
            "by_strategy": {
                k: round(sum(v) / len(v), 4)
                for k, v in by_strategy.items()
            },
        }

    def regime_accuracy_report(self) -> dict[str, Any]:
        """
        How accurate was our regime classification?
        Profitable trades in high_vol regime validate the vol premium edge.
        """
        rows = self._db.execute("""
            SELECT regime_at_entry, pillar,
                   COUNT(*) as trades,
                   SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
                   SUM(realized_pnl) as total_pnl
            FROM trade_records
            WHERE regime_at_entry IS NOT NULL AND realized_pnl IS NOT NULL
            GROUP BY regime_at_entry, pillar
        """).fetchall()

        result: dict[str, Any] = {}
        for row in rows:
            regime, pillar, trades, wins, pnl = row
            key = f"{regime}_{pillar}"
            result[key] = {
                "regime": regime,
                "pillar": pillar,
                "trades": trades,
                "win_rate": round(wins / trades, 3) if trades > 0 else 0.0,
                "total_pnl": round(pnl or 0.0, 2),
            }
        return result


class PsiMonitor:
    """
    Population Stability Index — detects feature drift in signal inputs.

    Compares current week's signal distributions against baseline (first 4 weeks).
    PSI > 0.25 → major drift → flag for review and potential model retraining.
    PSI 0.10–0.25 → moderate drift → monitor.
    PSI < 0.10 → stable.

    Features tracked: iv_rank, vix, vix_vix3m_ratio, hv10_hv30_ratio, spy_rsi
    """

    _PSI_PATH = Path(".agora/psi_data.json")
    _PSI_MINOR  = 0.10
    _PSI_MAJOR  = 0.25

    def __init__(self) -> None:
        self._data: dict[str, list[float]] = {}
        self._load()

    def _load(self) -> None:
        if self._PSI_PATH.exists():
            try:
                self._data = json.loads(self._PSI_PATH.read_text())
            except Exception:
                self._data = {}

    def record(self, features: dict[str, float | None]) -> None:
        """Record today's signal features for PSI tracking."""
        for k, v in features.items():
            if v is None:
                continue
            if k not in self._data:
                self._data[k] = []
            self._data[k].append(float(v))
            # Keep 252 trading days (1 year)
            self._data[k] = self._data[k][-252:]

        self._PSI_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._PSI_PATH.write_text(json.dumps(self._data))

    def compute_psi(self) -> dict[str, Any]:
        """
        Compute PSI for each feature: baseline (first 60 obs) vs recent (last 20 obs).
        Returns per-feature PSI and overall alert level.
        """
        results: dict[str, Any] = {}
        max_psi = 0.0

        for feature, values in self._data.items():
            if len(values) < 80:
                results[feature] = {"psi": None, "status": "insufficient_data"}
                continue

            baseline = values[:60]
            current  = values[-20:]
            psi = self._compute_feature_psi(baseline, current)
            max_psi = max(max_psi, psi)

            status = (
                "major_drift"    if psi >= self._PSI_MAJOR else
                "moderate_drift" if psi >= self._PSI_MINOR else
                "stable"
            )
            results[feature] = {"psi": round(psi, 4), "status": status}

        overall = (
            "ALERT"   if max_psi >= self._PSI_MAJOR else
            "MONITOR" if max_psi >= self._PSI_MINOR else
            "OK"
        )
        return {"features": results, "max_psi": round(max_psi, 4), "overall": overall}

    @staticmethod
    def _compute_feature_psi(baseline: list[float], current: list[float]) -> float:
        """PSI = Σ (actual% - expected%) × ln(actual% / expected%), 10 buckets."""
        import math
        n_bins = 10

        # Compute bucket edges from baseline
        baseline_sorted = sorted(baseline)
        edges = [
            baseline_sorted[int(len(baseline_sorted) * i / n_bins)]
            for i in range(1, n_bins)
        ]
        edges = [-float("inf")] + edges + [float("inf")]

        def bucket_proportions(data: list[float]) -> list[float]:
            counts = [0] * n_bins
            for v in data:
                for i in range(n_bins):
                    if edges[i] <= v < edges[i + 1]:
                        counts[i] += 1
                        break
            n = len(data)
            return [max(c / n, 1e-6) for c in counts]  # avoid log(0)

        base_props = bucket_proportions(baseline)
        curr_props = bucket_proportions(current)

        psi = sum(
            (c - b) * math.log(c / b)
            for b, c in zip(base_props, curr_props)
        )
        return psi

"""
PreMarketSetupAgent — 6:30 AM review of open positions vs overnight moves.

Runs every morning before the market opens. Answers:
  1. Which positions gapped overnight (good/bad)?
  2. Do any need to be closed at the open?
  3. What happened to our position's sector overnight (futures, ADRs)?
  4. Are any positions approaching their target_close_date (21 DTE)?

Output: structured setup dict consumed by CEOAgent morning brief.
Fires on_position_alert callback when a position needs immediate action.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Coroutine
from zoneinfo import ZoneInfo

import anthropic

from ..core.config import AgoraSettings, get_settings
from ..ops.llm_cost_log import log_call as _log_llm

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")


@dataclass
class PositionAlert:
    position_id: str
    ticker: str
    alert_type: str   # "gap_up" | "gap_down" | "dte_warning" | "stop_approach"
    severity: str     # "info" | "warning" | "critical"
    message: str
    recommended_action: str   # "hold" | "close_at_open" | "roll" | "review"
    overnight_move_pct: float = 0.0


@dataclass
class PreMarketSetupReport:
    generated_at: datetime
    open_positions_count: int
    alerts: list[PositionAlert] = field(default_factory=list)
    summary: str = ""
    tickers_to_close_at_open: list[str] = field(default_factory=list)
    tickers_to_watch: list[str] = field(default_factory=list)
    overnight_futures_note: str = ""


class PreMarketSetupAgent:
    """
    Runs once per morning at 6:30 AM ET.
    Reviews open positions against overnight market moves.
    """

    def __init__(
        self,
        settings: AgoraSettings | None = None,
        position_mgr: Any = None,
        on_position_alert: Callable[[PositionAlert], Coroutine] | None = None,
    ) -> None:
        self._settings    = settings or get_settings()
        self._position_mgr = position_mgr
        self._on_alert    = on_position_alert
        self._client      = anthropic.AsyncAnthropic(api_key=self._settings.anthropic_api_key)
        self._running     = False
        self._last_run_date: date | None = None
        self._last_report: PreMarketSetupReport | None = None

    @property
    def last_report(self) -> PreMarketSetupReport | None:
        return self._last_report

    async def start(self) -> None:
        self._running = True
        logger.info("PreMarketSetupAgent started")
        while self._running:
            now_et = datetime.now(tz=ET)
            # Run window: 6:25–6:55 AM ET, once per day
            if now_et.hour == 6 and 25 <= now_et.minute <= 55:
                today = now_et.date()
                if self._last_run_date != today:
                    try:
                        report = await self._run_setup()
                        self._last_report = report
                        self._last_run_date = today
                    except Exception as exc:
                        logger.error("PreMarket setup failed: %s", exc)
            await asyncio.sleep(60)

    async def stop(self) -> None:
        self._running = False

    # ── Main setup run ─────────────────────────────────────────────

    async def _run_setup(self) -> PreMarketSetupReport:
        positions = self._position_mgr.get_open_positions() if self._position_mgr else []
        today = date.today()

        report = PreMarketSetupReport(
            generated_at=datetime.now(tz=ET),
            open_positions_count=len(positions),
        )

        if not positions:
            report.summary = "No open positions — clean slate for the day."
            logger.info("PreMarket: no open positions")
            return report

        logger.info("PreMarket setup: reviewing %d open positions", len(positions))

        # Fetch overnight prices for all position tickers
        tickers = list({p.ticker for p in positions})
        overnight_moves = await self._fetch_overnight_moves(tickers)

        alerts: list[PositionAlert] = []
        close_at_open: list[str] = []

        for pos in positions:
            move = overnight_moves.get(pos.ticker, 0.0)
            dte_remaining = (pos.expiry_date - today).days
            days_to_target_close = (pos.target_close_date - today).days

            # Gap analysis (direction-aware)
            if abs(move) >= 0.03:   # 3%+ overnight move
                is_bad_gap = (
                    (pos.direction == "bullish" and move < -0.03) or
                    (pos.direction == "bearish" and move > 0.03) or
                    (pos.direction == "neutral" and abs(move) > 0.05)
                )
                alert_type  = "gap_down" if move < 0 else "gap_up"
                severity    = "critical" if (is_bad_gap and abs(move) >= 0.06) else "warning"
                recommended = "close_at_open" if is_bad_gap and abs(move) >= 0.05 else "review"

                alert = PositionAlert(
                    position_id=pos.position_id,
                    ticker=pos.ticker,
                    alert_type=alert_type,
                    severity=severity,
                    message=(
                        f"{pos.ticker} gapped {'down' if move < 0 else 'up'} {abs(move)*100:.1f}% overnight "
                        f"({'adverse' if is_bad_gap else 'favorable'} for {pos.direction} {pos.strategy})"
                    ),
                    recommended_action=recommended,
                    overnight_move_pct=move,
                )
                alerts.append(alert)
                if recommended == "close_at_open":
                    close_at_open.append(pos.ticker)

            # DTE warning: ≤ 7 DTE and not yet at target close date
            if dte_remaining <= 7:
                alerts.append(PositionAlert(
                    position_id=pos.position_id,
                    ticker=pos.ticker,
                    alert_type="dte_warning",
                    severity="warning" if dte_remaining > 3 else "critical",
                    message=f"{pos.ticker} expires in {dte_remaining} days — consider closing",
                    recommended_action="close_at_open" if dte_remaining <= 3 else "roll",
                ))
            elif days_to_target_close <= 2:
                alerts.append(PositionAlert(
                    position_id=pos.position_id,
                    ticker=pos.ticker,
                    alert_type="dte_warning",
                    severity="info",
                    message=f"{pos.ticker} reaches 21-DTE target close in {days_to_target_close} days",
                    recommended_action="review",
                ))

        report.alerts = alerts
        report.tickers_to_close_at_open = close_at_open

        # Fetch overnight futures context
        report.overnight_futures_note = await self._futures_context()

        # Synthesize summary
        report.summary = await self._synthesize_summary(positions, overnight_moves, alerts)

        # Fire callbacks
        for alert in alerts:
            if alert.severity in ("warning", "critical") and self._on_alert:
                try:
                    await self._on_alert(alert)
                except Exception as exc:
                    logger.debug("Alert callback failed: %s", exc)

        logger.info(
            "PreMarket setup complete: %d positions, %d alerts, close_at_open=%s",
            len(positions), len(alerts), close_at_open,
        )
        return report

    async def _fetch_overnight_moves(self, tickers: list[str]) -> dict[str, float]:
        """Fetch yesterday's close → current pre-market price for each ticker."""
        moves: dict[str, float] = {}
        try:
            import yfinance as yf
            for ticker in tickers:
                try:
                    tk = yf.Ticker(ticker)
                    info = tk.info or {}
                    # previousClose vs currentPrice (pre-market)
                    prev_close = float(info.get("previousClose") or info.get("regularMarketPreviousClose") or 0)
                    current    = float(info.get("currentPrice") or info.get("regularMarketPrice") or prev_close)
                    if prev_close > 0:
                        moves[ticker] = (current - prev_close) / prev_close
                except Exception:
                    continue
        except Exception as exc:
            logger.debug("Overnight moves fetch failed: %s", exc)
        return moves

    async def _futures_context(self) -> str:
        """One-line summary of overnight futures (ES, NQ, RTY)."""
        try:
            import yfinance as yf
            lines = []
            for sym, name in [("ES=F", "S&P futs"), ("NQ=F", "Nasdaq futs"), ("RTY=F", "Russell futs")]:
                info = yf.Ticker(sym).info or {}
                chg = float(info.get("regularMarketChangePercent") or 0)
                lines.append(f"{name} {'+' if chg >= 0 else ''}{chg:.1f}%")
            return " | ".join(lines)
        except Exception:
            return "Futures data unavailable"

    async def _synthesize_summary(
        self,
        positions: list[Any],
        overnight_moves: dict[str, float],
        alerts: list[PositionAlert],
    ) -> str:
        """Use Claude Haiku to write a 2-3 sentence pre-market summary."""
        try:
            pos_str = "\n".join(
                f"  {p.ticker}: {p.strategy} | {p.direction} | entry={p.entry_date} | "
                f"expiry={p.expiry_date} | overnight={overnight_moves.get(p.ticker, 0)*100:+.1f}% | "
                f"unrealized_pnl=${p.unrealized_pnl:.0f}"
                for p in positions
            )
            alert_str = "\n".join(f"  [{a.severity.upper()}] {a.message}" for a in alerts) or "  None"

            resp = await self._client.messages.create(
                model=self._settings.claude_fast_model,
                max_tokens=250,
                messages=[{
                    "role": "user",
                    "content": (
                        f"Pre-market position review. Write 2-3 sentences summarizing:\n"
                        f"Open positions:\n{pos_str}\n\n"
                        f"Alerts:\n{alert_str}\n\n"
                        f"Be direct and numbers-first. Flag what needs attention today."
                    ),
                }],
            )
            if hasattr(resp, "usage"):
                _log_llm(str(self._settings.db_path), "PremarketSetup", self._settings.claude_fast_model,
                         resp.usage.input_tokens, resp.usage.output_tokens, purpose="premarket_review")
            return resp.content[0].text.strip()
        except Exception:
            critical = [a for a in alerts if a.severity == "critical"]
            if critical:
                return f"{len(critical)} critical alert(s) require attention at open. " + "; ".join(a.message for a in critical[:2])
            return f"{len(positions)} open positions reviewed. " + (f"{len(alerts)} alerts." if alerts else "All clear.")

"""
AGORA Session — the main trading session orchestrator.

Coordinates all agents in one async event loop:
  1. Pre-market (7 AM ET): MacroSynthesizer runs extended thinking scan
  2. Market open (9:30 AM ET): Signal computation for universe
  3. Intraday (continuous): CatalystDiscoveryAgent + EarningsTranscriptAgent + SmartMoneyAgent
  4. Position lifecycle (every 60s): PositionManager checks targets/stops
  5. Risk gating: every trade goes through RiskCouncil before IBKR
  6. 3:30 PM ET: Auto-close expiring positions
  7. After-hours (4 PM ET): P&L attribution, PSI check, nightly report

Run: python -m agora.session
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from agora.agents import (
    ConvictionScorer,
    DisagreementResolver,
    MacroSynthesizer,
    SectorMomentumAgent,
    SignalInput,
)
from agora.core.config import AgoraSettings, get_settings
from agora.core.models import Catalyst, OpenPosition, PositionStatus, StrategyPillar
from agora.discovery.catalyst_agent import CatalystDiscoveryAgent
from agora.discovery.earnings_transcript import EarningsTranscriptAgent
from agora.discovery.smart_money import SmartMoneyAgent
from agora.lifecycle.position_manager import PositionManager
from agora.execution.ibkr_bridge import close_trade, submit_trade
from agora.ops.attribution import PnlAttributor, PsiMonitor
from agora.risk.risk_council import RiskCouncil
from agora.signals.event_patterns import EventPatternEngine
from agora.signals.iv_premium import IvPremiumScreen
from agora.signals.vol_regime import VolRegimeClassifier
from agora.strategies.rules_engine import StrategyRulesEngine

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")


class AgoraSession:
    """
    Top-level async orchestrator. Runs as a long-lived process.
    Paper trading by default; flip to live in .env: TRADING_MODE=live
    """

    def __init__(self, settings: AgoraSettings | None = None) -> None:
        self._settings = settings or get_settings()
        self._session_id = f"AGORA-{datetime.now(tz=ET).strftime('%Y%m%d-%H%M')}"

        # Signal generators
        self._vol_classifier  = VolRegimeClassifier()
        self._iv_screen       = IvPremiumScreen()
        self._event_engine    = EventPatternEngine()
        self._psi             = PsiMonitor()

        # Agents
        self._macro           = MacroSynthesizer(self._settings)
        self._sector          = SectorMomentumAgent(self._settings)
        self._scorer          = ConvictionScorer()
        self._resolver        = DisagreementResolver()
        self._strategy        = StrategyRulesEngine(self._settings)

        # Infrastructure
        self._position_mgr    = PositionManager(
            self._settings,
            on_close_order=self._execute_close,
            on_roll_order=self._execute_roll,
        )
        self._risk            = RiskCouncil(self._settings)
        self._attributor      = PnlAttributor(self._settings)

        # Discovery (fires async callbacks into _on_catalyst / _on_earnings)
        self._catalyst_agent  = CatalystDiscoveryAgent(
            self._settings,
            on_catalyst=self._on_catalyst,
        )
        self._earnings_agent  = EarningsTranscriptAgent(
            self._settings,
            on_earnings=self._on_earnings,
            on_catalyst=self._on_catalyst,
        )
        self._smart_money     = SmartMoneyAgent(
            self._settings,
            on_catalyst=self._on_catalyst,
        )

        # State
        self._macro_context   = None
        self._running         = False

    async def run(self) -> None:
        """Main entry point — runs all agents concurrently."""
        self._running = True
        logger.info("AGORA session started: %s | mode=%s",
                    self._session_id, self._settings.trading_mode)

        await asyncio.gather(
            self._catalyst_agent.start(),
            self._earnings_agent.start(),
            self._smart_money.start(),
            self._position_mgr.start(),
            self._session_loop(),
        )

    async def stop(self) -> None:
        self._running = False
        await asyncio.gather(
            self._catalyst_agent.stop(),
            self._earnings_agent.stop(),
            self._smart_money.stop(),
            self._position_mgr.stop(),
        )
        logger.info("AGORA session stopped: %s", self._session_id)

    # ── Session loop (market-hours intelligence cycle) ─────────────

    async def _session_loop(self) -> None:
        """
        Runs the scheduled intelligence tasks:
        - Pre-market: macro synthesis
        - Market open: universe scan
        - After hours: attribution + PSI
        """
        while self._running:
            now_et = datetime.now(tz=ET)
            hour = now_et.hour

            try:
                if hour == 7 and now_et.minute == 0:
                    await self._premarket_macro_scan()
                elif hour == 9 and now_et.minute == 30:
                    await self._universe_scan()
                elif hour == 16 and now_et.minute == 5:
                    await self._afterhours_report()
            except Exception as exc:
                logger.error("Session loop error: %s", exc)

            await asyncio.sleep(60)

    # ── Pre-market macro synthesis ─────────────────────────────────

    async def _premarket_macro_scan(self) -> None:
        logger.info("Pre-market macro scan starting")
        try:
            import yfinance as yf
            spy_info = yf.Ticker("SPY").info or {}
            vix_info = yf.Ticker("^VIX").info or {}
            vix3m_info = yf.Ticker("^VIX3M").info or {}

            vix = float(vix_info.get("regularMarketPrice") or 20.0)
            vix3m = float(vix3m_info.get("regularMarketPrice") or 20.0)

            from trading_platform.services.market_data.yfinance_provider import YFinanceProvider
            provider = YFinanceProvider()
            spy_snap = provider.get_snapshot("SPY")

            regime_result = self._vol_classifier.classify(
                iv_rank=spy_snap.iv_rank,
                vix=vix,
                vix3m=vix3m,
                hv10=None,
                hv30=None,
                spy_rsi=spy_snap.rsi_14,
                atm_iv=spy_snap.atm_iv,
                hv21=spy_snap.historical_vol_30d,
            )

            cal = self._event_engine._cal
            days_fomc, fomc_evt = cal.days_to_next_event(datetime.now(tz=ET).date())
            days_cpi = None
            if fomc_evt and "cpi" in str(fomc_evt).lower():
                days_cpi = days_fomc
                days_fomc = None

            self._macro_context = await self._macro.synthesize(
                regime=regime_result["regime"],
                iv_rank=spy_snap.iv_rank,
                vix=vix,
                vix_vix3m=vix / vix3m if vix3m > 0 else None,
                spy_rsi=spy_snap.rsi_14,
                days_to_fomc=days_fomc,
                days_to_cpi=days_cpi,
            )

            # Record features for PSI monitoring
            self._psi.record({
                "iv_rank":         spy_snap.iv_rank,
                "vix":             vix,
                "vix_vix3m_ratio": vix / vix3m if vix3m > 0 else None,
                "spy_rsi":         spy_snap.rsi_14,
            })

            logger.info(
                "Macro context: stance=%s | vol_selling_ok=%s | size_bias=%s",
                self._macro_context.macro_stance,
                self._macro_context.vol_selling_ok,
                self._macro_context.size_bias,
            )
        except Exception as exc:
            logger.error("Pre-market scan failed: %s", exc)

    # ── Universe scan ──────────────────────────────────────────────

    async def _universe_scan(self) -> None:
        """Score each ETF in universe and place high-conviction trades."""
        logger.info("Universe scan starting for %d tickers", len(self._settings.etf_universe))
        tasks = [self._evaluate_ticker(ticker) for ticker in self._settings.etf_universe]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _evaluate_ticker(self, ticker: str) -> None:
        """Full signal stack for one ticker → trade recommendation → risk gate → order."""
        try:
            from trading_platform.services.market_data.yfinance_provider import YFinanceProvider
            from trading_platform.services.options_flow import OptionsFlowService

            provider = YFinanceProvider()
            flow_svc = OptionsFlowService()

            snap = provider.get_snapshot(ticker)
            if not snap or not snap.price:
                return

            # IV premium signal
            iv_signal = self._iv_screen.check(
                ticker, snap.atm_iv, snap.historical_vol_30d
            )

            # GEX signal
            gex_raw = flow_svc.get_gex(ticker, snap.price)

            # Regime signal (reuse macro context regime)
            from agora.core.models import GexRegime, GexSignal, IvPremiumSignal, Regime, VolRegimeSignal
            gex = GexSignal(
                ticker=ticker,
                gex_total=gex_raw.get("gex_total", 0.0),
                regime=GexRegime(gex_raw.get("regime", "neutral")),
                dominant_strike=gex_raw.get("dominant_strike"),
                flip_level=gex_raw.get("flip_level"),
            ) if gex_raw else None

            iv_prem = IvPremiumSignal(
                ticker=ticker,
                atm_iv_30d=iv_signal.get("atm_iv_pct", 0) / 100,
                hv_21d=iv_signal.get("hv_21d_pct", 0) / 100,
                premium_ratio=iv_signal.get("premium_ratio") or 0.0,
                days_above_threshold=iv_signal.get("days_above_threshold", 0),
                signal_active=iv_signal.get("signal_active", False),
            ) if iv_signal else None

            # Event pattern
            today = datetime.now(tz=ET).date()
            event_signals = self._event_engine.get_signals(ticker, today)
            event = None
            if event_signals:
                es = event_signals[0]
                from ..core.models import EventSignal
                event = EventSignal(
                    event_type=es["event_type"],
                    ticker=ticker,
                    days_to_event=es.get("days_to_event", 5),
                    direction=es.get("direction", "neutral"),
                    confidence=es.get("confidence", 0.5),
                )

            # Build macro signal input
            macro_input = SignalInput(
                source="macro",
                direction=self._macro_context.macro_stance if self._macro_context else "neutral",
                confidence=self._macro_context.confidence if self._macro_context else 0.5,
            )

            # Microstructure signal
            micro_dir = "neutral"
            micro_conf = 0.5
            if gex and gex.regime == GexRegime.NEGATIVE and snap.rsi_14 and snap.rsi_14 > 55:
                micro_dir = "bullish"
                micro_conf = 0.65
            elif iv_prem and iv_prem.signal_active:
                micro_conf = 0.70

            micro_input = SignalInput(
                source="microstructure",
                direction=micro_dir,
                confidence=micro_conf,
            )

            # Score conviction
            from agora.core.models import VolRegimeSignal as VRS
            regime_signal = VRS(
                regime=Regime(self._macro_context.macro_stance if self._macro_context else "normal"),
                confidence=self._macro_context.confidence if self._macro_context else 0.5,
                iv_rank=snap.iv_rank,
                vix=None,
            ) if self._macro_context else None

            conviction = self._scorer.score(
                ticker=ticker,
                session_id=self._session_id,
                iv_premium=iv_prem,
                gex=gex,
                regime=regime_signal,
                macro=self._macro_context,
                event=event,
                catalyst=None,
            )

            # Resolve disagreement → size multiplier
            resolution = self._resolver.resolve(
                macro=macro_input,
                microstructure=micro_input,
                catalyst=None,
                regime=self._macro_context.macro_stance if self._macro_context else "normal",
                total_conviction=conviction.total_score,
            )

            if resolution["gate"] == "no_trade":
                logger.debug("No trade for %s: %s", ticker, resolution["reason"])
                return

            conviction.size_multiplier = resolution["size_multiplier"]
            conviction.gate = resolution["gate"]

            # Get options chain and build recommendation
            import yfinance as yf
            tk = yf.Ticker(ticker)
            if not tk.options:
                return

            chain_dict = {}
            for exp in tk.options[:8]:   # check up to 8 expiries
                try:
                    c = tk.option_chain(exp)
                    chain_dict[exp] = {"calls": c.calls, "puts": c.puts}
                except Exception:
                    continue

            recommendation = self._strategy.build_recommendation(
                conviction=conviction,
                spot=snap.price,
                options_chain=chain_dict,
                gex=gex,
            )

            if not recommendation:
                return

            await self._submit_recommendation(recommendation, ticker, snap.price)

        except Exception as exc:
            logger.error("Evaluate ticker %s failed: %s", ticker, exc)

    # ── Catalyst callback ──────────────────────────────────────────

    async def _on_catalyst(self, catalyst: Catalyst) -> None:
        """Called by discovery agents for real-time catalyst trades."""
        if self._risk.is_kill_switch_active():
            return

        logger.info("Processing catalyst: %s | %s | %s",
                    catalyst.ticker, catalyst.catalyst_type, catalyst.direction)

        # Minimal signal stack for catalyst plays
        from agora.core.models import ConvictionScore, StrategyPillar
        conviction = ConvictionScore(
            session_id=self._session_id,
            ticker=catalyst.ticker,
            total_score=65.0,   # catalyst trades start at 65 — info speed adds more
            info_speed_score=self._scorer._score_info_speed(catalyst),
            gate="standard",
            pillar=StrategyPillar.CATALYST,
            reasoning=f"Catalyst: {catalyst.catalyst_type} | {catalyst.direction}",
        )
        conviction.size_multiplier = 1.0

        # Get options chain
        try:
            import yfinance as yf
            tk = yf.Ticker(catalyst.ticker)
            if not tk.options:
                return
            chain_dict = {}
            for exp in tk.options[:4]:
                try:
                    c = tk.option_chain(exp)
                    chain_dict[exp] = {"calls": c.calls, "puts": c.puts}
                except Exception:
                    continue

            spot_info = tk.info or {}
            spot = float(spot_info.get("regularMarketPrice") or spot_info.get("currentPrice") or 0)
            if spot <= 0:
                return

            recommendation = self._strategy.build_recommendation(
                conviction=conviction,
                spot=spot,
                options_chain=chain_dict,
                direction_override=catalyst.direction,
            )

            if recommendation:
                await self._submit_recommendation(recommendation, catalyst.ticker, spot)
        except Exception as exc:
            logger.error("Catalyst trade build failed for %s: %s", catalyst.ticker, exc)

    async def _on_earnings(self, result: Any) -> None:
        """Called by EarningsTranscriptAgent with post-earnings analysis."""
        logger.info("Earnings result: %s | beat=%s | guide=%s",
                    result.ticker, result.beat_quality, result.guidance_tone)
        # Post-earnings skew reversion via EventPatternEngine
        from datetime import date
        signal = self._event_engine.get_post_earnings_signal(
            ticker=result.ticker,
            earnings_date=result.filing_date,
            beat_quality=result.beat_quality,
            guidance_tone=result.guidance_tone,
        )
        if signal:
            logger.info("Post-earnings skew signal: %s | conf=%.2f",
                        result.ticker, signal["confidence"])

    # ── Execution stubs (wired to IBKR in live mode) ──────────────

    async def _submit_recommendation(
        self, recommendation: Any, ticker: str, spot: float
    ) -> None:
        """Risk gate then submit to IBKR (or paper log)."""
        positions = self._position_mgr.get_open_positions()
        greeks = self._position_mgr.get_portfolio_greeks()

        risk_result = self._risk.approve_trade(
            recommendation, greeks, positions, spot
        )

        if not risk_result["approved"]:
            logger.info("BLOCKED by risk council: %s | %s",
                        ticker, risk_result["reason"])
            return

        order = await submit_trade(recommendation, self._settings, self._session_id)
        if order.get("status") not in ("Cancelled", "ApiCancelled", "Inactive"):
            self._record_paper_position(recommendation)

    def _record_paper_position(self, rec: Any) -> None:
        from datetime import date, timedelta
        from .core.models import OpenPosition, PositionStatus
        expiry = rec.legs[0].expiration if rec.legs else (date.today() + timedelta(days=45))
        pos = OpenPosition(
            position_id=str(uuid.uuid4()),
            ticker=rec.ticker,
            strategy=rec.strategy,
            pillar=rec.pillar,
            status=PositionStatus.OPEN,
            legs=rec.legs,
            contracts=rec.contracts,
            entry_price=rec.entry_debit_credit / max(1, rec.contracts * 100),
            entry_date=date.today(),
            expiry_date=expiry,
            target_close_date=expiry - timedelta(days=21),
            max_loss_dollars=rec.max_loss_dollars,
            max_gain_dollars=rec.max_gain_dollars,
        )
        self._position_mgr.add_position(pos)

    async def _execute_close(self, position: Any, reason: str) -> None:
        logger.info("Closing %s | reason=%s", position.ticker, reason)
        try:
            await close_trade(position, self._settings, self._session_id)
        except Exception as exc:
            logger.error("Close failed for %s: %s", position.ticker, exc)

    async def _execute_roll(self, position: Any, new_expiry: Any) -> None:
        # Roll = close current leg + open new one; close half here, open via session loop
        logger.info("Rolling %s → expiry %s", position.ticker, new_expiry)
        try:
            await close_trade(position, self._settings, self._session_id)
        except Exception as exc:
            logger.error("Roll-close failed for %s: %s", position.ticker, exc)

    # ── After-hours reporting ──────────────────────────────────────

    async def _afterhours_report(self) -> None:
        logger.info("After-hours reporting")

        psi_result = self._psi.compute_psi()
        if psi_result["overall"] != "OK":
            logger.warning("PSI ALERT: %s | max_psi=%.3f",
                           psi_result["overall"], psi_result["max_psi"])

        attribution = self._attributor.attribution_report()
        logger.info(
            "Today P&L: $%.0f | trades=%d | pillars=%s",
            attribution["total_pnl"],
            attribution["total_trades"],
            list(attribution["pillars"].keys()),
        )


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    session = AgoraSession()
    try:
        await session.run()
    except KeyboardInterrupt:
        await session.stop()


if __name__ == "__main__":
    asyncio.run(main())

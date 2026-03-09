"""
Options Trading Engine — Main Orchestrator
===========================================
Wires all 12 modules together into a unified workflow.

Usage:
    from trading_engine.engine import TradingEngine
    engine = TradingEngine()
    engine.run_morning_analysis()
"""

from datetime import datetime, date
from typing import Dict, Any, Optional, List

from .config import EngineConfig
from .models import MarketSnapshot, PerformanceReport
from .market_data import MarketDataProvider
from .formatters import header, sub_header, kv, C

# Import all 12 modules
from .modules.credit_spread_scanner import CreditSpreadScanner
from .modules.regime_classifier import RegimeClassifier
from .modules.theta_calculator import ThetaCalculator
from .modules.strike_selector import StrikeSelector
from .modules.iron_condor import IronCondorMachine
from .modules.premarket_analyzer import PreMarketAnalyzer
from .modules.risk_manager import RiskManager
from .modules.skew_analyzer import SkewExploiter
from .modules.weekly_calendar import WeeklyCalendar
from .modules.earnings_crusher import EarningsCrusher
from .modules.eod_scalper import EODScalper
from .modules.performance_dashboard import PerformanceDashboard


class TradingEngine:
    """
    Institutional Options Trading Engine — 12 Modules.

    ╔══════════════════════════════════════════════════════════════╗
    ║  Module  1: 0DTE Credit Spread Scanner      (Tastytrade)   ║
    ║  Module  2: Market Regime Classifier         (Citadel)      ║
    ║  Module  3: Theta Decay Calculator           (SIG)          ║
    ║  Module  4: Probability Strike Selection     (Two Sigma)    ║
    ║  Module  5: Iron Condor Machine              (D.E. Shaw)    ║
    ║  Module  6: Pre-Market Analyzer              (Jane Street)  ║
    ║  Module  7: Risk Management System           (Wolverine)    ║
    ║  Module  8: Volatility Skew Exploiter        (Akuna)        ║
    ║  Module  9: Weekly Income Calendar           (Peak6)        ║
    ║  Module 10: Earnings Theta Crusher           (IMC Trading)  ║
    ║  Module 11: EOD Theta Scalper                (Optiver)      ║
    ║  Module 12: Monthly Performance Dashboard    (Citadel)      ║
    ╚══════════════════════════════════════════════════════════════╝
    """

    def __init__(self, config: Optional[EngineConfig] = None,
                 use_ibkr: bool = False):
        self.config = config or EngineConfig()
        self.data = MarketDataProvider(self.config)
        self.use_ibkr = use_ibkr

        if use_ibkr:
            self.data.connect()

        # Initialize all modules
        # Config-only modules
        self.regime = RegimeClassifier(self.config)
        self.theta = ThetaCalculator(self.config)
        self.strikes = StrikeSelector(self.config)
        self.premarket = PreMarketAnalyzer(self.config)
        self.risk = RiskManager(self.config)
        self.calendar = WeeklyCalendar(self.config)
        self.dashboard = PerformanceDashboard(self.config)

        # Modules that need market data provider
        self.scanner = CreditSpreadScanner(self.config, self.data)
        self.condor = IronCondorMachine(self.config, self.data)
        self.skew = SkewExploiter(self.config, self.data)
        self.earnings = EarningsCrusher(self.config, self.data)
        self.eod = EODScalper(self.config, self.data)

        # Load previous trade journal
        self.dashboard.load_journal()

        self._snap: Optional[MarketSnapshot] = None

    # ─────────────────────────────────────────────────────────────
    # Data Retrieval
    # ─────────────────────────────────────────────────────────────

    def get_snapshot(self, use_manual: bool = False) -> MarketSnapshot:
        """Get current market snapshot."""
        if use_manual:
            self._snap = self.data.get_snapshot(mode="manual")
        else:
            self._snap = self.data.get_snapshot()
        return self._snap

    def _ensure_snap(self):
        """Ensure we have a market snapshot."""
        if self._snap is None:
            print(f"\n{C.YELLOW}No market data loaded. Enter manually:{C.RESET}\n")
            self._snap = self.data.get_snapshot(mode="manual")

    # ─────────────────────────────────────────────────────────────
    # Individual Module Runners
    # ─────────────────────────────────────────────────────────────

    def run_regime(self) -> str:
        """Module 2: Market regime classification."""
        self._ensure_snap()
        report = self.regime.classify(self._snap)
        return self.regime.format_report(report, self._snap)

    def run_scanner(self) -> str:
        """Module 1: 0DTE credit spread scanner."""
        self._ensure_snap()
        result = self.scanner.scan(self._snap)
        return self.scanner.format_report(result, self._snap)

    def run_theta(self) -> str:
        """Module 3: Theta decay calculator."""
        self._ensure_snap()
        report = self.theta.calculate([], self._snap)
        return self.theta.format_report(report, self._snap)

    def run_strikes(self) -> str:
        """Module 4: Probability strike selection."""
        self._ensure_snap()
        report = self.strikes.select(self._snap)
        return self.strikes.format_report(report, self._snap)

    def run_condor(self) -> str:
        """Module 5: Iron condor machine."""
        self._ensure_snap()
        result = self.condor.build(self._snap)
        return self.condor.format_report(result, self._snap)

    def run_premarket(self) -> str:
        """Module 6: Pre-market analysis."""
        self._ensure_snap()
        result = self.premarket.analyze(self._snap)
        return self.premarket.format_report(result, self._snap)

    def run_risk(self) -> str:
        """Module 7: Risk management check."""
        self._ensure_snap()
        return self.risk.format_report(self._snap)

    def run_skew(self) -> str:
        """Module 8: Volatility skew analysis."""
        self._ensure_snap()
        report = self.skew.analyze(self._snap)
        return self.skew.format_report(report, self._snap)

    def run_calendar(self) -> str:
        """Module 9: Weekly income calendar."""
        self._ensure_snap()
        plan = self.calendar.get_daily_plan(self._snap)
        return self.calendar.format_report(plan, self._snap)

    def run_earnings(self, ticker: str = "TSLA",
                     earnings_date: Optional[date] = None,
                     current_iv: float = 65.0,
                     stock_price: float = 250.0,
                     bias: str = "neutral") -> str:
        """Module 10: Earnings theta crusher."""
        if earnings_date is None:
            from datetime import timedelta
            earnings_date = date.today() + timedelta(days=3)
        self._ensure_snap()
        result = self.earnings.analyze(
            ticker, earnings_date, current_iv, stock_price, bias, self._snap
        )
        return self.earnings.format_report(result)

    def run_eod(self) -> str:
        """Module 11: EOD theta scalper."""
        self._ensure_snap()
        result = self.eod.scan(self._snap)
        return self.eod.format_report(result, self._snap)

    def run_dashboard(self) -> str:
        """Module 12: Monthly performance dashboard."""
        report = self.dashboard.generate_report()
        return self.dashboard.format_report(report)

    # ─────────────────────────────────────────────────────────────
    # Composite Workflows
    # ─────────────────────────────────────────────────────────────

    def run_morning_analysis(self) -> str:
        """
        Full morning workflow — run before market open.
        Modules: 6 (Pre-Market) → 2 (Regime) → 4 (Strikes) → 8 (Skew) → 1 (Scanner) → 7 (Risk)
        """
        self._ensure_snap()

        output = []
        output.append(self._banner("MORNING ANALYSIS WORKFLOW"))

        # Step 1: Pre-market conditions
        output.append(f"\n{C.BOLD}  STEP 1/6 — PRE-MARKET ANALYSIS{C.RESET}")
        output.append(self.run_premarket())

        # Step 2: Classify regime
        output.append(f"\n{C.BOLD}  STEP 2/6 — MARKET REGIME{C.RESET}")
        output.append(self.run_regime())

        # Step 3: Strike levels
        output.append(f"\n{C.BOLD}  STEP 3/6 — STRIKE SELECTION{C.RESET}")
        output.append(self.run_strikes())

        # Step 4: Skew check
        output.append(f"\n{C.BOLD}  STEP 4/6 — VOLATILITY SKEW{C.RESET}")
        output.append(self.run_skew())

        # Step 5: Specific trade setups
        output.append(f"\n{C.BOLD}  STEP 5/6 — 0DTE CREDIT SPREAD SCAN{C.RESET}")
        output.append(self.run_scanner())

        # Step 6: Risk check
        output.append(f"\n{C.BOLD}  STEP 6/6 — RISK MANAGEMENT{C.RESET}")
        output.append(self.run_risk())

        output.append(self._banner("MORNING ANALYSIS COMPLETE"))
        return "\n".join(output)

    def run_midday_check(self) -> str:
        """Midday position management check."""
        self._ensure_snap()
        output = []
        output.append(self._banner("MIDDAY CHECK"))
        output.append(self.run_theta())
        output.append(self.run_risk())
        output.append(self.run_calendar())
        return "\n".join(output)

    def run_eod_workflow(self) -> str:
        """End-of-day scalping workflow."""
        self._ensure_snap()
        output = []
        output.append(self._banner("EOD THETA SCALP WORKFLOW"))
        output.append(self.run_regime())
        output.append(self.run_eod())
        output.append(self.run_risk())
        return "\n".join(output)

    def run_weekly_review(self) -> str:
        """End-of-week review."""
        output = []
        output.append(self._banner("WEEKLY REVIEW"))
        output.append(self.run_dashboard())
        output.append(self.run_calendar())
        return "\n".join(output)

    def run_all(self) -> str:
        """Run ALL 12 modules. Full diagnostic."""
        self._ensure_snap()
        output = []
        output.append(self._banner("FULL ENGINE DIAGNOSTIC — ALL 12 MODULES"))

        modules = [
            ("Module  1: 0DTE Credit Spread Scanner", self.run_scanner),
            ("Module  2: Market Regime Classifier", self.run_regime),
            ("Module  3: Theta Decay Calculator", self.run_theta),
            ("Module  4: Probability Strike Selection", self.run_strikes),
            ("Module  5: Iron Condor Machine", self.run_condor),
            ("Module  6: Pre-Market Analyzer", self.run_premarket),
            ("Module  7: Risk Management System", self.run_risk),
            ("Module  8: Volatility Skew Exploiter", self.run_skew),
            ("Module  9: Weekly Income Calendar", self.run_calendar),
            ("Module 10: Earnings Theta Crusher", lambda: self.run_earnings()),
            ("Module 11: EOD Theta Scalper", self.run_eod),
            ("Module 12: Performance Dashboard", self.run_dashboard),
        ]

        for i, (name, runner) in enumerate(modules, 1):
            output.append(f"\n{C.BOLD}{C.CYAN}  [{i}/12] {name}{C.RESET}")
            try:
                output.append(runner())
            except Exception as e:
                output.append(f"  {C.RED}ERROR: {e}{C.RESET}")

        output.append(self._banner("FULL DIAGNOSTIC COMPLETE"))
        return "\n".join(output)

    # ─────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────

    def _banner(self, text: str) -> str:
        return (
            f"\n{C.BOLD}{C.MAGENTA}"
            f"{'╔' + '═' * 76 + '╗'}\n"
            f"{'║'} {text:^74s} {'║'}\n"
            f"{'╚' + '═' * 76 + '╝'}"
            f"{C.RESET}\n"
        )

    def status(self) -> str:
        """Print engine status."""
        lines = []
        lines.append(header("OPTIONS TRADING ENGINE v1.0", f"Account: ${self.config.account.account_size:,.0f}"))
        lines.append(sub_header("ENGINE STATUS"))
        lines.append(kv("Data Mode", "IBKR Live" if self.use_ibkr else "Manual Input"))
        lines.append(kv("Account Size", f"${self.config.account.account_size:,.2f}"))
        lines.append(kv("Max Risk/Trade", f"{self.config.account.max_risk_per_trade_pct*100:.1f}%"))
        lines.append(kv("Daily Loss Limit", f"{self.config.account.max_daily_loss_pct*100:.1f}%"))
        lines.append(kv("Weekly Loss Limit", f"{self.config.account.max_weekly_loss_pct*100:.1f}%"))
        lines.append(kv("Short Delta Range", f"{self.config.trading.short_delta_min:.2f} - {self.config.trading.short_delta_max:.2f}"))
        lines.append(kv("Spread Width", f"${self.config.trading.spread_width_min} - ${self.config.trading.spread_width_max}"))
        lines.append(kv("Trade Journal", f"{len(self.dashboard.trades)} trades recorded"))
        lines.append(kv("Market Data", "Loaded" if self._snap else "Not loaded"))

        lines.append(sub_header("AVAILABLE MODULES"))
        modules = [
            " 1. 0DTE Credit Spread Scanner      (run_scanner)",
            " 2. Market Regime Classifier         (run_regime)",
            " 3. Theta Decay Calculator            (run_theta)",
            " 4. Probability Strike Selection      (run_strikes)",
            " 5. Iron Condor Machine               (run_condor)",
            " 6. Pre-Market Analyzer               (run_premarket)",
            " 7. Risk Management System            (run_risk)",
            " 8. Volatility Skew Exploiter         (run_skew)",
            " 9. Weekly Income Calendar            (run_calendar)",
            "10. Earnings Theta Crusher            (run_earnings)",
            "11. EOD Theta Scalper                 (run_eod)",
            "12. Monthly Performance Dashboard     (run_dashboard)",
        ]
        for m in modules:
            lines.append(f"  {m}")

        lines.append(sub_header("WORKFLOWS"))
        lines.append("  run_morning_analysis()  — Full pre-market + regime + scanner + risk")
        lines.append("  run_midday_check()      — Theta + risk + calendar check")
        lines.append("  run_eod_workflow()      — EOD scalp setup + risk check")
        lines.append("  run_weekly_review()     — Performance + calendar review")
        lines.append("  run_all()               — All 12 modules")

        lines.append(f"\n{C.CYAN}{'═' * 78}{C.RESET}\n")
        return "\n".join(lines)

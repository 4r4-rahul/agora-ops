"""
Core configuration for the Options Trading Engine.
"""

from dataclasses import dataclass, field
from typing import Optional
import os
from dotenv import load_dotenv

load_dotenv()


@dataclass
class AccountConfig:
    """Trading account configuration."""
    account_size: float = 50_000.0
    max_risk_per_trade_pct: float = 0.03        # 3% max per trade
    max_daily_loss_pct: float = 0.05             # 5% daily stop
    max_weekly_loss_pct: float = 0.08            # 8% weekly stop
    max_monthly_drawdown_pct: float = 0.10       # 10% monthly circuit breaker
    max_buying_power_usage_pct: float = 0.50     # Never use more than 50% BP
    weekly_income_target_pct: float = 0.01       # 1% per week target
    daily_income_target_pct: float = 0.002       # 0.2% per day target
    can_monitor_intraday: bool = True


@dataclass
class IBKRConfig:
    """Interactive Brokers connection config."""
    host: str = field(default_factory=lambda: os.getenv("IBKR_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.getenv("IBKR_PORT", "7497")))
    client_id: int = field(default_factory=lambda: int(os.getenv("IBKR_CLIENT_ID", "1")))
    account: str = field(default_factory=lambda: os.getenv("IBKR_ACCOUNT", ""))
    timeout: int = field(default_factory=lambda: int(os.getenv("IBKR_TIMEOUT", "30")))


@dataclass
class TradingConfig:
    """Strategy-level trading parameters."""
    # 0DTE parameters
    zero_dte_min_credit: float = 0.50            # Minimum $0.50 credit per spread
    zero_dte_max_credit: float = 2.00            # Cap credit (wider = more risk)
    zero_dte_entry_start: str = "09:45"          # Earliest entry after open vol settles
    zero_dte_entry_end: str = "10:30"            # Latest entry for full-day 0DTE
    zero_dte_stop_multiplier: float = 2.0        # Close at 2x credit collected
    zero_dte_profit_target_pct: float = 0.50     # Close at 50% profit before 2 PM

    # Credit spread parameters
    short_delta_min: float = 0.10                # Minimum short strike delta
    short_delta_max: float = 0.15                # Maximum short strike delta
    spread_width_min: int = 5                    # Minimum spread width (points)
    spread_width_max: int = 10                   # Maximum spread width (points)

    # Iron condor parameters
    ic_min_total_credit: float = 1.00            # Min combined credit for iron condor
    ic_adjustment_trigger_pct: float = 0.30      # Roll when within 30% of short strike

    # Weekly parameters
    weekly_short_delta: float = 0.12             # Weekly short strike delta target
    weekly_profit_target_pct: float = 0.50       # Close weeklies at 50% profit

    # EOD scalping parameters
    eod_entry_start: str = "14:30"               # 2:30 PM entry window
    eod_entry_end: str = "15:00"                 # 3:00 PM latest entry
    eod_close_by: str = "15:50"                  # Close by 3:50 PM
    eod_min_credit: float = 0.30                 # Min $0.30 for EOD scalps
    eod_stop_multiplier: float = 1.5             # Tighter stop for EOD
    eod_short_delta_max: float = 0.12            # Lower delta for EOD safety

    # Earnings parameters
    earnings_max_risk_pct: float = 0.02          # 2% max for earnings trades
    earnings_entry_days_before: int = 2          # Enter 1-3 days before

    # Risk reward
    min_reward_to_risk: float = 0.33             # 1:3 risk-to-reward (credit:max_loss)


@dataclass
class RegimeParams:
    """Per-regime optimized parameters (from optimizer sweep)."""
    delta: float = 0.12
    stop_mult: float = 3.0
    profit_target: float = 0.75
    width: float = 2.0
    entry_start_min: int = 30     # Minutes after open
    entry_end_min: int = 60       # Minutes after open
    gamma_limit: float = 0.15
    preferred_strategy: str = "iron_condor"
    trade_enabled: bool = True
    position_size_mult: float = 1.0  # Scale position vs normal


@dataclass
class AdaptiveConfig:
    """
    Regime-adaptive trading parameters.
    REALITY-TESTED on 1-minute IBKR bars (Sep 2025 – Mar 2026).

    Critical finding: 5-min bars hide stop blowthrough.
    Parameters below are validated on 1-min data with worst-case
    intra-bar stop execution (bar HIGH/LOW adverse excursion).

    SPY 1-min results (129 days, full 2400-combo sweep):
    - GREEN:  Only call_credit profitable (PF=1.36, +$1,359, 76% WR)
    - YELLOW: No profitable params at 1-min resolution
    - RED:    No profitable params → skip

    QQQ 1-min results (129 days, full 2400-combo sweep):
    - GREEN:  All strategies profitable. IC best (PF=6.86, 98.8% WR)
    - YELLOW: All strategies profitable. IC best (PF=13.18, 84.2% WR)
    - RED:    No profitable params → skip
    """
    green: RegimeParams = field(default_factory=lambda: RegimeParams(
        delta=0.15, stop_mult=2.5, profit_target=0.75,
        width=2.0, entry_start_min=30, entry_end_min=60,
        gamma_limit=0.15, preferred_strategy="call_credit",
        trade_enabled=True, position_size_mult=1.0,
    ))
    yellow: RegimeParams = field(default_factory=lambda: RegimeParams(
        delta=0.15, stop_mult=3.0, profit_target=0.30,
        width=2.0, entry_start_min=30, entry_end_min=90,
        gamma_limit=0.15, preferred_strategy="call_credit",
        trade_enabled=True, position_size_mult=0.5,  # Half size
    ))
    red: RegimeParams = field(default_factory=lambda: RegimeParams(
        delta=0.08, stop_mult=1.5, profit_target=0.40,
        width=1.0, entry_start_min=45, entry_end_min=90,
        gamma_limit=0.08, preferred_strategy="none",
        trade_enabled=False, position_size_mult=0.0,
    ))

    def for_regime(self, regime: str) -> RegimeParams:
        """Get optimal parameters for a given regime."""
        regime = regime.upper()
        if regime == "GREEN":
            return self.green
        elif regime == "YELLOW":
            return self.yellow
        elif regime == "RED":
            return self.red
        return self.green  # Default to conservative GREEN


@dataclass
class EngineConfig:
    """Master engine configuration."""
    account: AccountConfig = field(default_factory=AccountConfig)
    ibkr: IBKRConfig = field(default_factory=IBKRConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    adaptive: AdaptiveConfig = field(default_factory=AdaptiveConfig)
    data_dir: str = field(default_factory=lambda: os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "data"
    ))
    log_dir: str = field(default_factory=lambda: os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "logs"
    ))
    trade_journal_path: str = field(default_factory=lambda: os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "data", "trade_journal.json"
    ))

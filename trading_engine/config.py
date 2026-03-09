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
    account_size: float = field(
        default_factory=lambda: float(os.getenv("ACCOUNT_SIZE", "10000"))
    )
    max_risk_per_trade_pct: float = 0.02        # 2% max per trade ($200 on $10K)
    max_daily_loss_pct: float = 0.03             # 3% daily stop ($300 on $10K)
    max_weekly_loss_pct: float = 0.06            # 6% weekly stop
    max_monthly_drawdown_pct: float = 0.10       # 10% monthly circuit breaker
    max_buying_power_usage_pct: float = 0.15     # 15% BP max (conservative for small acct)
    weekly_income_target_pct: float = 0.02       # 2% per week target
    daily_income_target_pct: float = 0.005       # 0.5% per day target
    can_monitor_intraday: bool = True
    # Long options budget (PRIMARY strategy for small accounts)
    lotto_daily_budget_pct: float = 0.05         # 5% of account per day ($500 on $10K)
    lotto_max_per_trade_pct: float = 0.02        # 2% max per single trade ($200)
    lotto_max_positions: int = 5                 # Max 5 open positions


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
class LottoConfig:
    """
    Configuration for long-option scanner (BUY calls / BUY puts).

    This is the PRIMARY strategy for small accounts (<$25K).
    Instead of selling premium for small gains, we buy options
    when momentum triggers fire and ride gamma acceleration.
    """
    # Budget (sized for primary strategy, not side play)
    max_premium_per_trade: float = 2.00          # Max $2.00/contract ($200 per contract)
    min_premium: float = 0.05                    # Min $0.05 (avoid penny options)
    max_contracts_per_trade: int = 5             # Max 5 contracts per trade

    # Strike selection tiers
    # Tier 1 — Sniper (cheap OTM, big gamma, 5x-50x potential)
    sniper_otm_distance_pct: float = 0.005       # 0.5% OTM from current price
    sniper_max_premium: float = 0.50             # Max $0.50 for sniper entries
    sniper_delta_min: float = 0.05               # Cheapest gamma plays
    sniper_delta_max: float = 0.20               # Not too far OTM

    # Tier 2 — Momentum (near-ATM, higher cost, 2x-10x potential)
    momentum_otm_distance_pct: float = 0.002     # 0.2% OTM — almost ATM
    momentum_max_premium: float = 2.00           # Up to $2.00 for strong signals
    momentum_delta_min: float = 0.20             # Closer to money
    momentum_delta_max: float = 0.45             # Near ATM

    # Legacy (backward compat)
    otm_distance_pct: float = 0.005
    max_otm_distance_pct: float = 0.02
    target_delta_min: float = 0.05
    target_delta_max: float = 0.45               # Widened to include momentum tier

    # Triggers (momentum thresholds)
    orb_breakout_min_pct: float = 0.003          # 0.3% beyond 15-min range = breakout
    volume_surge_mult: float = 3.0               # 3x avg volume = surge
    mean_rev_flush_pct: float = 0.008            # 0.8% flush for mean-rev entry
    vwap_reclaim_bars: int = 2                   # 2 consecutive bars above VWAP

    # Exit rules
    stop_loss_pct: float = 0.50                  # Close at 50% loss of premium
    profit_target_mult: float = 3.0              # Take first profit at 3x (was 5x)
    runner_keep_pct: float = 0.30                # Keep 30% as runner after target
    runner_floor_mult: float = 1.5               # Close runner if drops below 1.5x
    trailing_start_mult: float = 2.0             # Start trailing after 2x
    trailing_drop_pct: float = 0.40              # Close if 40% drop from HWM
    time_exit_minutes_before_close: int = 15     # Close 15 min before close (was 30)

    # Scan timing (expanded — primary strategy scans all day)
    scan_start_min: int = 16                     # Start 16 min after open (after 15-min ORB)
    scan_end_min: int = 360                      # Scan until 15 min before close
    scan_interval_sec: int = 20                  # Check triggers every 20s (was 30)


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
        gamma_limit=0.15, preferred_strategy="put_credit",
        trade_enabled=True, position_size_mult=1.0,
    ))
    yellow: RegimeParams = field(default_factory=lambda: RegimeParams(
        delta=0.15, stop_mult=3.0, profit_target=0.30,
        width=2.0, entry_start_min=30, entry_end_min=90,
        gamma_limit=0.15, preferred_strategy="put_credit",
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
    lotto: LottoConfig = field(default_factory=LottoConfig)
    data_dir: str = field(default_factory=lambda: os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "data"
    ))
    log_dir: str = field(default_factory=lambda: os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "logs"
    ))
    trade_journal_path: str = field(default_factory=lambda: os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "data", "trade_journal.json"
    ))

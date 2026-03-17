"""
Core configuration for the Options Trading Engine.
"""

from dataclasses import dataclass, field
from typing import Optional, Dict
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
class ScalpConfig:
    """
    Configuration for 0DTE Gamma Scalping strategy.

    This replaces the "lottery ticket" approach with professional
    directional scalping: ATM strikes, underlying-price exits,
    confirmation stacking, and strict time discipline.

    Calibrated from 180 days of SPY 1m data (49,950 bars),
    optimized in SPX mode (×10 scaling, 129 trading days):
      SPX ATR(15) median = $2.60, mean = $3.10
      $10+ SPX move in 30 min: 54.4% of the time
      ATM delta ~0.50, gamma asymmetry favors buyer

    OPTIMAL PARAMETERS (from 120-combo sweep, PF=1.30, +36.1%):
      stop  = 3.0 × ATR  ($7.80 SPX / $0.78 SPY)
      target = 3.5 × ATR ($9.10 SPX / $0.91 SPY)
      time_stop = 30 min (give trades time to develop)
      min_confirmations = 3 (EMA+VOLUME+VWAP required)
      R:R ≈ 1:1.17, breakeven WR = 46%, actual WR = 45.5%

    Results (129 days SPX): 33 trades, 45.5% WR, PF=1.30,
      +$3,613 (+36.1%), avg win $1,033 vs avg loss $660.
    """
    # ── Strike Selection ────────────────────────────────────────
    max_otm_pct: float = 0.001          # Max 0.1% OTM ($0.65 SPY / $5.70 SPX)
    target_delta_min: float = 0.40      # Minimum delta (ATM zone)
    target_delta_max: float = 0.55      # Maximum delta

    # ── Entry Signals ───────────────────────────────────────────
    min_confirmations: int = 3          # Need 3+ signals (EMA+VOLUME+VWAP minimum)

    # ── ATR-Based Exits (in ATR multiples) ──────────────────────
    atr_period: int = 15                # 15-bar rolling ATR (= 15 min on 1m)
    stop_atr_mult: float = 3.0          # Stop: 3 × ATR adverse ($7.80 SPX)
    stop_close_confirm: bool = False     # Tested True: -$804 drag from theta decay on delayed exits
    profit_target_atr_mult: float = 3.5  # Target: 3.5 × ATR favorable ($9.10 SPX)
    trailing_activation_atr: float = 3.5  # Start trailing after 3.5 × ATR
    trailing_distance_atr: float = 1.5   # Trail distance: 1.5 × ATR

    # ── Time-Based Exits ────────────────────────────────────────
    time_stop_minutes: int = 30         # No move in 30 min → scratch exit
    time_stop_atr_mult: float = 0.5     # "No move" = within 0.5 × ATR of entry
    max_hold_minutes: int = 60          # Absolute max hold time
    eod_exit_minutes: int = 15          # Close before market close

    # ── Time Windows (minutes after open) ───────────────────────
    # Sweep finding: W1:15-50 W2:250-350 → PF=1.58, +$5,776 (33 trades)
    # vs default W1:20-60 W2:270-360 → PF=1.34, +$4,099 (36 trades)
    # Keeping defaults conservative; optimize on out-of-sample data.
    window_1_start: int = 20            # 9:50 AM — post-ORB
    window_1_end: int = 60              # 10:30 AM — before dead zone
    window_2_start: int = 270           # 2:00 PM — power hour
    window_2_end: int = 360             # 3:30 PM — before theta cliff
    enable_midday: bool = False         # PROVEN UNPROFITABLE: WR=34%, PF=0.74

    # ── Risk Management ─────────────────────────────────────────
    max_trades_per_day: int = 3         # Quality over quantity
    no_reentry_same_direction: bool = True  # Once stopped, direction is dead
    daily_loss_limit: float = 500.0     # Stop trading after $500 loss
    max_risk_per_trade: float = 200.0   # Max dollar risk per trade
    cooldown_bars: int = 15             # Bars to wait after a trade

    # ── Budget ──────────────────────────────────────────────────
    max_premium: float = 4.00           # Max per-contract premium (SPY scale)
    min_premium: float = 0.30           # Minimum viable premium
    max_contracts: int = 3              # Max contracts per trade

    # ── Volatility Filter ────────────────────────────────────────
    min_atr: float = 0.15               # Min ATR to trade (skip dead-flat periods)

    # ── IV Discount Filter (buy cheap options) ───────────────────
    # Core edge: only buy when realized vol > implied vol.
    # When RV > IV, options are underpriced → the underlying is moving
    # faster than the market has priced in → you get a vol discount.
    iv_discount_enabled: bool = True     # Enable RV > IV filter
    rv_lookback_bars: int = 20           # Bars to measure recent realized vol
    rv_iv_min_ratio: float = 0.8         # Min RV/IV ratio (0.8 = RV must be 80%+ of IV)
    rv_iv_min_ratio_w1: float = 0.9      # Stricter IV gate for morning window (AM loses $)
    rv_iv_premium_ratio: float = 1.2     # Bonus: if RV/IV > 1.2, option is deeply cheap

    # ── Chop Filter (anti-signal gate) ──────────────────────────
    chop_ema_pct: float = 0.0003        # EMA9/21 within 0.03% = chop
    chop_vwap_pct: float = 0.0003       # Price within 0.03% of VWAP = chop

    # ── Signal Thresholds ───────────────────────────────────────
    volume_surge_mult: float = 1.5      # 1.5x avg volume = confirmation
    candle_body_ratio_min: float = 0.55  # Min body/range ratio for "strong" (avg is 0.47)
    ema_slope_min_pct: float = 0.0001   # Min EMA slope per bar (0.01%)
    vwap_distance_min_pct: float = 0.0005  # Min 0.05% from VWAP for signal

    # ── New Signal Components ───────────────────────────────────
    # CANDLE_MOM: 3+ consecutive directional candles with strong bodies
    candle_mom_bars: int = 3            # Consecutive candle count for momentum
    candle_mom_body_pct: float = 0.40   # Min body/range ratio for each candle
    # RANGE_BRK: Donchian channel breakout (works all day, replaces ORB after 60m)
    range_brk_lookback: int = 20        # Bars to define the range (20-bar high/low)
    # PREV_HL: Previous day high/low breakout (key institutional level)
    prev_hl_confirm_bars: int = 2       # Bars to confirm fresh breakout

    # ── Runner Tier (OTM Power-Hour Plays) ──────────────────────
    # The "vacation fund": cheap OTM options that ride big moves.
    # Enters ONLY in power hour when momentum is confirmed.
    # Let winners run with trailing stop, no time stop.
    runner_enabled: bool = True             # Enable runner tier
    runner_max_per_day: int = 1             # Max 1 runner/day
    runner_budget_pct: float = 0.02         # 2% of account per runner ($200 on $10K)
    runner_max_premium: float = 2.00        # Max $2.00/contract (SPY scale, ×10 for SPX)
    runner_min_premium: float = 0.10        # Min $0.10 viable premium
    runner_max_contracts: int = 5           # Up to 5 cheap contracts

    # Strike selection: OTM for leverage
    runner_otm_pct: float = 0.005           # 0.5% OTM ($3.40 SPY / $34 SPX)
    runner_target_delta_min: float = 0.15   # Cheap but not lottery
    runner_target_delta_max: float = 0.35   # Upper bound

    # Entry gates (stricter than scalp)
    runner_min_confirmations: int = 3       # Same signal quality as scalp
    runner_window_start: int = 270          # Power hour only: 2:00 PM ET
    runner_window_end: int = 330            # Until 3:00 PM ET
    runner_min_atr_mult: float = 1.2        # ATR must be >1.2× day median (momentum day)
    runner_require_winning_scalp: bool = False  # If True, need a winning scalp first

    # Exits: let winners run
    runner_stop_atr_mult: float = 3.0       # Tighter stop: 3×ATR (sweep optimal)
    runner_trail_activation_atr: float = 3.0  # Start trailing at 3×ATR favorable
    runner_trail_distance_atr: float = 1.5  # Trail 1.5×ATR from best (tight capture)
    runner_max_hold_minutes: int = 120      # Up to 2 hours (rest of power hour + close)
    runner_eod_exit_minutes: int = 5        # Close 5 min before close (ride to end)


@dataclass
class ORBConfig:
    """
    Configuration for ORB (Opening Range Breakout) strategy.

    Trades breakouts above/below the first N minutes' high/low.
    Integrated as Strategy B: fires on days where the momentum engine
    has NO signal, providing coverage on 50-60 additional days.

    Backtested on 129 days SPX 0DTE (Sep 2025 – Mar 2026):
      ORB30 (30-bar range, target=1.5×range, stop=0.7×range):
        Raw: 60 days, 53.3% WR, PF=1.71, +$7,645
      Regime-filtered (MODERATE_TREND + STRONG_TREND only),
      max_hold=120 bars, full-day classify:
        17 days, 64.5% WR, PF=3.16, +$41,955

    Key insight: ORB captures TRENDS that the momentum engine's
    strict confirmation stack (VOL+VWAP+3 confirms) rejects.
    On flat days, ORB is a coin flip → regime filter is essential.
    """
    # ── Master Enable ───────────────────────────────────────────
    enabled: bool = True                 # Enable ORB30 strategy

    # ── ORB Formation ───────────────────────────────────────────
    orb_bars: int = 30                   # 30 bars (30 min) for range formation
    min_range_pct: float = 0.001         # Min ORB range 0.1% of open ($5.70 SPX)
    max_range_pct: float = 0.025         # Max ORB range 2.5% (skip gap days)

    # ── Breakout Detection ──────────────────────────────────────
    max_wait_bars: int = 90              # Max bars after ORB to detect breakout
    require_close_break: bool = True     # Require close (not just wick) above/below

    # ── ATR-Based Exits (in ORB range multiples) ────────────────
    target_range_mult: float = 1.5       # Target: 1.5× ORB range
    stop_range_mult: float = 0.6         # Stop: 0.6× ORB range back inside (was 0.7, tighter saves theta)
    stop_close_confirm: bool = False     # Tested True: -$804 drag from theta decay on delayed exits
    max_hold_bars: int = 120             # Max bars to hold (2h, lets trend develop)

    # ── Trailing Stop (Phase 3: protect profits on runners) ────
    trailing_enabled: bool = True         # Enable trailing stop for ORB
    trailing_activation_pct: float = 0.25 # Activate after 25% of target distance
    trailing_breakeven: bool = True       # First move stop to breakeven at activation
    trailing_distance_pct: float = 0.50   # Trail at 50% of favorable move behind peak
    trailing_min_bars: int = 60             # Min bars before trailing activates (preserves early target hits)

    # ── Time Window ─────────────────────────────────────────────
    # ORB forms 9:30-10:00, then we watch for breakouts 10:00-11:30
    entry_start_bar: int = 30            # Earliest entry (after ORB forms)
    entry_end_bar: int = 120             # Latest entry (bar 120 = ~11:30)
    eod_exit_minutes: int = 15           # Close before market close

    # ── Risk Management ─────────────────────────────────────────
    max_trades_per_day: int = 1          # 1 ORB trade per day (breakout is binary)
    max_risk_per_trade: float = 500.0    # Max dollar risk ($500 SPX scale)
    max_risk_pct: float = 0.05           # 5% of account per trade

    # ── Budget ──────────────────────────────────────────────────
    max_premium: float = 4.00            # Max per-contract premium (SPY scale)
    min_premium: float = 0.30            # Minimum viable premium
    max_contracts: int = 3               # Max contracts per trade

    # ── Strike Selection ────────────────────────────────────────
    max_otm_pct: float = 0.001           # ATM: 0.1% OTM

    # ── Regime Filter ───────────────────────────────────────────
    # Only trade ORB on days classified as trending.
    # DEAD_FLAT days lose money with ORB (-$1,049 on 48 trades).
    regime_filter_enabled: bool = True   # Filter by day regime
    skip_dead_flat: bool = True          # Skip DEAD_FLAT days
    skip_choppy: bool = True             # Skip CHOPPY days
    skip_range_bound: bool = True        # Skip RANGE_BOUND days (choppy for breakouts)
    skip_mixed: bool = True              # Skip MIXED days (ambiguous regime)

    # ── Priority ────────────────────────────────────────────────
    # ORB is Strategy B: only fires when momentum engine has no signal.
    # This prevents conflicts and preserves momentum's superior edge.
    only_when_no_momentum: bool = True   # Defer to momentum signals


@dataclass
class MeanReversionConfig:
    """
    Configuration for Mean-Reversion Scalping strategy.

    Complements the momentum/breakout scalp by trading MIDDAY CHOP.
    When momentum fails (10:30-2:00, choppy, range-bound), this
    strategy fades extremes: buy oversold, sell overbought.

    Philosophy: During chop, price oscillates around VWAP. When it
    reaches Bollinger Band extremes with RSI confirmation, it snaps
    back. We capture that snap-back with tight stops and quick targets.

    Components:
      1. BB_EXTREME — Price at/beyond Bollinger Band (2σ)
      2. RSI_EXTREME — RSI overbought (>70) or oversold (<30)
      3. VWAP_REVERT — Price significantly from VWAP + turning back

    Anti-Trend Gate:
      If EMAs are strongly trending (spread > 0.1%), skip — don't
      fade a strong trend, that's the momentum engine's territory.

    STATUS: DISABLED by default.
    128-config parameter sweep (Sep 2025 – Mar 2026, SPX 0DTE) found
    ZERO profitable configurations. Best MR PF was 0.75.
    Root cause: theta decay on long ATM 0DTE options during midday
    overwhelms the small reversion profits. Even 60% WR configs lose
    money because avg loss >> avg win (theta + adverse excursion).
    Architecture preserved for future credit spread / premium selling.
    """
    # ── Master Enable ───────────────────────────────────────────
    enabled: bool = False               # DISABLED: not profitable with long options
    # ── Bollinger Band Parameters ───────────────────────────────
    bb_period: int = 20                 # SMA period for middle band
    bb_std: float = 2.0                 # Standard deviations for upper/lower bands

    # ── RSI Parameters ──────────────────────────────────────────
    rsi_period: int = 14                # RSI calculation period
    rsi_overbought: float = 70.0        # RSI above this → PUT signal
    rsi_oversold: float = 30.0          # RSI below this → CALL signal

    # ── VWAP Reversion ──────────────────────────────────────────
    vwap_extreme_pct: float = 0.001     # 0.1% from VWAP = extended
    vwap_turn_bars: int = 2             # Need 2 bars turning back

    # ── Entry Signals ───────────────────────────────────────────
    min_confirmations: int = 2          # Need 2+ of {BB, RSI, VWAP_REVERT}

    # ── Anti-Trend Gate ─────────────────────────────────────────
    ema_trend_threshold: float = 0.002  # EMA9/21 spread > 0.2% → strong trend, skip

    # ── ATR-Based Exits ─────────────────────────────────────────
    atr_period: int = 15                # Same ATR as momentum
    stop_atr_mult: float = 2.0          # Tighter stop: 2.0×ATR (chop = smaller moves)
    profit_target_atr_mult: float = 2.0  # Quick target: 2.0×ATR (don't expect big runs)
    trailing_activation_atr: float = 1.5  # Trail earlier (capture quick reversions)
    trailing_distance_atr: float = 0.75   # Tight trail (mean-rev moves are fast)

    # ── Time-Based Exits ────────────────────────────────────────
    time_stop_minutes: int = 15         # Quicker scratch (mean-rev should work fast)
    time_stop_atr_mult: float = 0.3     # Tighter "no move" threshold
    max_hold_minutes: int = 30          # Max 30 min (chop doesn't sustain)
    eod_exit_minutes: int = 15          # Same as momentum

    # ── Time Window (midday only) ───────────────────────────────
    window_start: int = 60              # 10:30 AM (after morning momentum window)
    window_end: int = 270               # 2:00 PM (before power hour)

    # ── Risk Management ─────────────────────────────────────────
    max_trades_per_day: int = 4         # More trades allowed (shorter holds)
    daily_loss_limit: float = 300.0     # Smaller daily limit (mean-rev is riskier)
    max_risk_per_trade: float = 150.0   # Smaller per-trade risk
    cooldown_bars: int = 10             # Shorter cooldown

    # ── Budget ──────────────────────────────────────────────────
    max_premium: float = 4.00           # Same ATM premiums
    min_premium: float = 0.30
    max_contracts: int = 2              # Smaller size (mean-rev has lower WR)

    # ── Volatility Filter ────────────────────────────────────────
    min_atr: float = 0.10               # Can trade in lower vol (chop has lower ATR)
    max_atr_enabled: bool = False       # Anti-trend gate handles this better than ATR cap


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
class RangeFadeConfig:
    """
    Configuration for Range-Bound Fade strategy.

    Trades mean-reversion on RANGE_BOUND days: buy calls at range lows,
    buy puts at range highs. Price oscillates between support/resistance
    levels identified from the first 60 bars.

    Target regime: RANGE_BOUND (11-14 days / 129, avg 1.5% day range,
    wide swings but no net direction).

    Key insight: On RANGE_BOUND days, ORB breakouts fail (price reverts)
    but the large range provides excellent fade opportunities.
    We identify the high/low zone from first 60 bars, then fade touches.

    Confirmations:
      1. RANGE_TOUCH — Price reaches upper/lower boundary zone
      2. REVERSAL_BAR — Bar reversal pattern (wick rejection, engulfing)
      3. VWAP_CROSS — Price crossing VWAP supports mean reversion
      4. RSI_EXTREME — RSI > 70 at range high or RSI < 30 at range low
    """
    # ── Master Enable ───────────────────────────────────────────
    enabled: bool = True

    # ── Range Formation ────────────────────────────────────────
    formation_bars: int = 60           # 60 bars (1 hour) to establish range
    # Boundary zone: price must enter top/bottom X% of range to trigger
    boundary_zone_pct: float = 0.15    # Top/bottom 15% of range = fade zone

    # ── Regime Requirement ─────────────────────────────────────
    # Only trade on RANGE_BOUND days (detected by full-day classify in backtest)
    require_range_bound: bool = True
    also_trade_mixed: bool = True      # Also trade on MIXED days (13 more days)

    # ── Entry Confirmations ────────────────────────────────────
    min_confirmations: int = 2         # Need ≥2 of {RANGE_TOUCH, REVERSAL_BAR, VWAP_CROSS, RSI_EXTREME}

    # ── Entry Window ───────────────────────────────────────────
    entry_start_bar: int = 60          # After range forms
    entry_end_bar: int = 350           # Until ~15 min before close
    eod_exit_minutes: int = 15         # Close before market close

    # ── Exits (ATR-based) ──────────────────────────────────────
    # Mean-reversion targets are SMALL — we're fading, not trending
    target_range_pct: float = 0.50     # Target: 50% of range (fade past midpoint)
    stop_range_pct: float = 0.12       # Stop: 12% of range beyond boundary (was 15%, tighter saves theta)
    stop_close_confirm: bool = False   # Tested True: -$804 drag from theta decay on delayed exits
    max_hold_bars: int = 60            # Max 60 bars (1 hour) — reversion should be fast

    # ── Risk Management ────────────────────────────────────────
    max_trades_per_day: int = 1        # 1 fade per day (2nd trade = revenge trading)
    max_risk_per_trade: float = 400.0  # Max dollar risk
    max_risk_pct: float = 0.04         # 4% of account per trade

    # ── Budget ─────────────────────────────────────────────────
    max_premium: float = 4.00          # Max per-contract premium (SPY scale)
    min_premium: float = 0.30          # Minimum viable premium
    max_contracts: int = 2             # Conservative sizing (fades are riskier)

    # ── Strike Selection ───────────────────────────────────────
    max_otm_pct: float = 0.001         # ATM: 0.1% OTM

    # ── Priority ───────────────────────────────────────────────
    # Range fade defers to momentum (Strategy A) but can coexist with ORB.
    # On RANGE_BOUND days, ORB is already disabled, so no conflict.
    only_when_no_momentum: bool = True

    # ── RSI Parameters ─────────────────────────────────────────
    rsi_period: int = 14
    rsi_overbought: float = 70.0
    rsi_oversold: float = 30.0


@dataclass
class VWAPMRConfig:
    """
    Configuration for VWAP Mean-Reversion strategy (Strategy F).

    Trades DEAD_FLAT days where price oscillates tightly around VWAP.
    When price drifts too far from VWAP, fade the deviation expecting
    a snap-back. On dead-flat days (avg range 0.59%, avg max VWAP
    deviation 0.286%), VWAP acts as a magnet — every deviation reverts.

    Target regime: DEAD_FLAT (63 days / 129, avg range <0.8%)

    Key insight: On DEAD_FLAT days, there are NO strong trends, NO
    wide ranges to fade, and ORB/momentum/RF all skip. But price
    DOES oscillate ±0.15-0.30% around VWAP, creating micro mean-
    reversion opportunities. We buy calls when price dips below VWAP,
    puts when price rises above VWAP.

    Confirmations:
      1. VWAP_DEV — Price deviation exceeds threshold from VWAP
      2. REVERSAL_BAR — Bar shows rejection (wick, engulfing)
      3. RSI_EXTREME — RSI confirms overextension
      4. SNAP_BACK — Price starting to revert toward VWAP

    Tuned parameters (Phase 2b):
      - Selective entries (0.25% deviation threshold) — only the deepest oscillations
      - 1 trade per day — avoids overtrading on flat days
      - Quick holds (25 bars max) — exit before theta eats profits
      - Wide stop (0.40%) — accommodate noise without premature stops
      - Tight target (0.04% from VWAP) — take quick profits on snap-back
      - Result: 37 trades, 43.2% WR, R:R=2.1:1, +$4,180 VM P&L
    """
    # ── Master Enable ───────────────────────────────────────────
    enabled: bool = False  # Disabled: standalone +$4K but -$981 total from compounding drag

    # ── Regime Requirement ─────────────────────────────────────
    require_dead_flat: bool = True     # Only trade on DEAD_FLAT days

    # ── VWAP Deviation Thresholds ──────────────────────────────
    # Minimum deviation from VWAP to trigger a signal
    # DEAD_FLAT avg max dev = 0.286%, so 0.25% catches only the deepest dips
    min_vwap_deviation_pct: float = 0.25  # 0.25% = ~$14.25 on SPX $5700

    # ── Entry Confirmations ────────────────────────────────────
    min_confirmations: int = 2         # Need ≥2 of {VWAP_DEV, REVERSAL_BAR, RSI_EXTREME, SNAP_BACK}

    # ── Entry Window ───────────────────────────────────────────
    entry_start_bar: int = 30          # After VWAP establishes
    entry_end_bar: int = 350           # Until ~15 min before close
    eod_exit_minutes: int = 15         # Close before market close

    # ── Exits ──────────────────────────────────────────────────
    # Target: price snaps back toward VWAP (take quick profits)
    target_vwap_return_pct: float = 0.04  # Target: deviation shrinks to ≤0.04% of VWAP
    # Stop: deviation grows past 0.40% — breakout, not reverting
    stop_deviation_pct: float = 0.40   # Wide stop — flat days have noise
    max_hold_bars: int = 25            # Max 25 bars — quick in/out before theta

    # ── Risk Management ────────────────────────────────────────
    max_trades_per_day: int = 1        # 1 trade per day (avoids overtrading on flat days)
    max_risk_per_trade: float = 300.0  # Moderate risk (flat days = smaller moves)
    max_risk_pct: float = 0.03         # 3% of account per trade

    # ── Budget ─────────────────────────────────────────────────
    max_premium: float = 4.00          # Max per-contract premium (SPY scale)
    min_premium: float = 0.30          # Minimum viable premium
    max_contracts: int = 2             # Conservative sizing

    # ── Strike Selection ───────────────────────────────────────
    max_otm_pct: float = 0.001         # ATM: 0.1% OTM (need high delta for small moves)

    # ── Priority ───────────────────────────────────────────────
    only_when_no_momentum: bool = True  # Defer to momentum signals

    # ── RSI Parameters ─────────────────────────────────────────
    rsi_period: int = 14
    rsi_overbought: float = 70.0       # Standard thresholds (selective deviation filters enough)
    rsi_oversold: float = 30.0         # Standard thresholds

    # ── VWAP Computation ───────────────────────────────────────
    vwap_warmup_bars: int = 20         # Minimum bars for reliable VWAP


@dataclass
class PositionSizingConfig:
    """
    Risk Parity + Phased Capital Scaling position sizer.

    Tested on 167 trades (SPY+QQQ, 10K MC paths, 30% degradation):
      Risk Parity 15%: RAR=64.1 (#1), MC Median=$118K, MedDD=13.7%
      Under 30% degradation: Median=$40K, 0% blowup risk.

    Methodology:
      1. Risk Parity: allocate risk inversely proportional to strategy
         volatility. Lower-vol strategies get bigger positions.
         Weights derived from backtest: Runner 33%, RangeFade 31%,
         Scalp 20%, ORB 17%. Rebalances via inverse-vol automatically.
      2. Vol Target: normalise each trade by ATR / median_ATR so
         every trade contributes ~equal dollar risk.
      3. Phased scaling: as account grows, shift from conservative
         (Risk Parity 15%) to aggressive (Larry Williams 15%):
           Phase 1: $10K → $25K — Risk Parity 15% (survival)
           Phase 2: $25K → $50K — Larry Williams 10% (accelerate)
           Phase 3: $50K+       — Larry Williams 15% (full throttle)

    Final contracts = min(strategy_budget, vol_adjusted, abs_cap)
    """
    # ── Risk Parity parameters ─────────────────────────────────
    # Per-strategy volatilities ($ std of trade PnL from backtest).
    # Used to compute inverse-vol weights at init time.
    # Lower vol → higher allocation weight.
    strategy_vols: dict = field(default_factory=lambda: {
        "scalp": 1347.0,
        "runner": 817.0,
        "orb": 1629.0,
        "range_fade": 875.0,
    })
    base_risk_pct: float = 0.15         # Total risk budget (Risk Parity phase)

    # ── Larry Williams parameters (Phase 2-3) ─────────────────
    lw_risk_pct: float = 0.10           # Phase 2: 10% of balance
    lw_risk_pct_full: float = 0.15      # Phase 3: 15% of balance
    lw_lookback: int = 20               # Rolling window for worst loss
    lw_default_loss: float = 500.0      # Default worst-loss when no history

    # ── Volatility Target parameters ───────────────────────────
    vol_target_pct: float = 0.15        # Vol-normalised budget cap

    # ── Phased capital scaling thresholds ──────────────────────
    phase_2_balance: float = 25_000.0   # Switch to LW 10% above this
    phase_3_balance: float = 50_000.0   # Switch to LW 15% above this

    # ── Hard caps ──────────────────────────────────────────────
    absolute_max_pct: float = 0.15      # Never risk more than 15% of balance
    min_contracts: int = 1              # Always trade at least 1 contract
    balance_gate_pct: float = 0.50      # Don't enter if cost > 50% of balance

    # ── Runner override ────────────────────────────────────────
    runner_budget_pct: float = 0.02     # 2% of balance for runners
    runner_balance_gate: float = 0.10   # Runner cost gate: 10% of balance


@dataclass
class EngineConfig:
    """Master engine configuration."""
    account: AccountConfig = field(default_factory=AccountConfig)
    ibkr: IBKRConfig = field(default_factory=IBKRConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    adaptive: AdaptiveConfig = field(default_factory=AdaptiveConfig)
    lotto: LottoConfig = field(default_factory=LottoConfig)
    scalp: ScalpConfig = field(default_factory=ScalpConfig)
    orb: ORBConfig = field(default_factory=ORBConfig)
    mean_reversion: MeanReversionConfig = field(default_factory=MeanReversionConfig)
    range_fade: RangeFadeConfig = field(default_factory=RangeFadeConfig)
    vwap_mr: VWAPMRConfig = field(default_factory=VWAPMRConfig)
    sizing: PositionSizingConfig = field(default_factory=PositionSizingConfig)
    data_dir: str = field(default_factory=lambda: os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "data"
    ))
    log_dir: str = field(default_factory=lambda: os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "logs"
    ))
    trade_journal_path: str = field(default_factory=lambda: os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "data", "trade_journal.json"
    ))

    @staticmethod
    def for_qqq() -> "EngineConfig":
        """
        QQQ-optimized configuration.

        QQQ has 1.36× the daily range of SPY (higher beta).
        Calibrated via fine-grid sweep on 129 QQQ trading days:
          - ORB: wider target (1.75× vs 1.5×), tighter stop (0.45× vs 0.6×)
            captures QQQ's bigger trends without giving back on reversals
          - RF: wider stop (0.25 vs 0.12) + wider target (0.60 vs 0.50)
            QQQ's per-bar noise is ~36% larger than SPY; default 0.12 stop
            produces $2-3 stops on small-range days → 6 flash-stops (≤3 min).
            Wider stop eliminates all flash stops, +$2.3K RF improvement.
          - Scalp: same params (momentum signals are % based, scale-invariant)
          - Trailing: SPY defaults (0.25/0.50/60) work well for QQQ too

        Sweep results (129 days, $10K start):
          ORB+RF tuned: 98t, PF=2.39, PnL=$+41,033, RF=43t/47%WR/$+10,904
        """
        cfg = EngineConfig()
        # ORB: QQQ trends harder → wider target, tighter stop
        cfg.orb.stop_range_mult = 0.45   # 0.45 vs SPY's 0.6 (tighter)
        cfg.orb.target_range_mult = 1.75 # 1.75 vs SPY's 1.5 (wider target)
        # RF: QQQ needs wider stops — per-bar volatility is ~36% higher
        # Sweep: stop 0.25 eliminates all flash-stops, target 0.60 captures
        # more of the range swing.  RF PnL: +$8,550 → +$10,904 (+28%)
        cfg.range_fade.stop_range_pct = 0.25   # 0.25 vs SPY's 0.12
        cfg.range_fade.target_range_pct = 0.60  # 0.60 vs SPY's 0.50
        return cfg

    @staticmethod
    def for_10k() -> tuple:
        """
        Recommended configuration for a $10,000 starting account.

        Returns (spy_config, qqq_config) tuple.

        Validated on 129 trading days (Sep 2025 – Mar 2026) in shared-balance
        mode (single $10K account for both SPY + QQQ):
          169 trades, PF=3.49, PnL=$+192,245, MaxDD=25.1%, Sharpe=5.38
          ROI: 1,922% on $10K

        Key findings from $10K optimization study:
          1. Position sizing ALREADY adapts to $10K via budget_pct gates:
             - ORB: 25% budget → 1 contract at $10K (cost ~$2-3K)
             - Scalp: 30% budget → 1-2 contracts (cost ~$1-2K)
             - RF: 20% budget → 1 contract (cost ~$1.5-2.5K)
             - Runner: 2% budget → 1 contract (cost ~$150)
          2. First-month concurrent premium max: $5,906 (59% of $10K) — fits
          3. Worst day: -$2,001 (day 3, Sep 4) → recovers next day
          4. Balance progression: $10K → $13.4K (month 1) → $21.5K (month 2)
          5. max_contracts=3 is optimal (budget math limits to 1-2 at $10K anyway)
          6. No circuit breaker needed (25% breaker never triggers; 20% would
             trigger day 3 and miss $188K of gains)

        No parameter changes from defaults — the system self-adapts.
        Use with run_portfolio_backtest.py --shared --account 10000.
        """
        return EngineConfig(), EngineConfig.for_qqq()


# ─────────────────────────────────────────────────────────────────
# Ticker Profiles
# ─────────────────────────────────────────────────────────────────

@dataclass
class TickerProfile:
    """
    Per-ticker properties for correct IBKR routing and strike math.

    SPX is an INDEX, not a stock — it requires different IBKR contract
    types, exchange routing, and has different strike increments.
    """
    symbol: str
    sec_type: str          # "STK" for stocks/ETFs, "IND" for indices
    exchange: str          # "SMART" for stocks, "CBOE" for SPX/VIX
    option_exchange: str   # Where options trade ("SMART" or "CBOE")
    currency: str = "USD"
    strike_increment: float = 1.0     # $1 for SPY/QQQ, $5 for SPX
    multiplier: int = 100             # Option multiplier (100 for all US options)
    is_cash_settled: bool = False     # True for SPX (no assignment risk)
    is_european: bool = False         # True for SPX (no early exercise)
    tax_1256: bool = False            # True for SPX (60/40 tax treatment)
    notional_scale: float = 1.0       # SPX ≈ 10x SPY
    # Premium scaling: SPX options cost more in absolute $ because
    # the underlying is ~$5,700 vs SPY ~$570. We scale max premiums.
    premium_scale: float = 1.0        # 1.0 for SPY, ~10.0 for SPX


# Pre-built profiles for supported tickers
TICKER_PROFILES: Dict[str, TickerProfile] = {
    "SPY": TickerProfile(
        symbol="SPY", sec_type="STK", exchange="SMART",
        option_exchange="SMART", strike_increment=1.0,
        multiplier=100, is_cash_settled=False, is_european=False,
        tax_1256=False, notional_scale=1.0, premium_scale=1.0,
    ),
    "QQQ": TickerProfile(
        symbol="QQQ", sec_type="STK", exchange="SMART",
        option_exchange="SMART", strike_increment=1.0,
        multiplier=100, is_cash_settled=False, is_european=False,
        tax_1256=False, notional_scale=1.0, premium_scale=1.0,
    ),
    "SPX": TickerProfile(
        symbol="SPX", sec_type="IND", exchange="CBOE",
        option_exchange="SMART", strike_increment=5.0,
        multiplier=100, is_cash_settled=True, is_european=True,
        tax_1256=True, notional_scale=10.0, premium_scale=10.0,
    ),
    "NDX": TickerProfile(
        symbol="NDX", sec_type="IND", exchange="CBOE",
        option_exchange="SMART", strike_increment=25.0,
        multiplier=100, is_cash_settled=True, is_european=True,
        tax_1256=True, notional_scale=40.0, premium_scale=40.0,
    ),
    "IWM": TickerProfile(
        symbol="IWM", sec_type="STK", exchange="SMART",
        option_exchange="SMART", strike_increment=1.0,
        multiplier=100, is_cash_settled=False, is_european=False,
        tax_1256=False, notional_scale=1.0, premium_scale=1.0,
    ),
    "AAPL": TickerProfile(
        symbol="AAPL", sec_type="STK", exchange="SMART",
        option_exchange="SMART", strike_increment=2.5,
        multiplier=100, is_cash_settled=False, is_european=False,
        tax_1256=False, notional_scale=1.0, premium_scale=1.0,
    ),
}


def get_ticker_profile(ticker: str) -> TickerProfile:
    """Get the profile for a ticker. Falls back to generic STK/SMART defaults."""
    return TICKER_PROFILES.get(
        ticker.upper(),
        TickerProfile(
            symbol=ticker.upper(), sec_type="STK", exchange="SMART",
            option_exchange="SMART", strike_increment=1.0,
        ),
    )

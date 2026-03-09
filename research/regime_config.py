
# regime_config.py
from typing import Dict, NamedTuple

class RegimeThresholds(NamedTuple):
    """Thresholds for identifying a regime"""
    adx_min: float = 0
    adx_max: float = 100
    daily_return_min: float = -100
    daily_return_max: float = 100
    ema_bullish_pct_min: float = 0
    ema_bullish_pct_max: float = 100
    atr_pct_min: float = 0
    atr_pct_max: float = 100
    swing_ratio_max: float = 1.0

class StrategyParams(NamedTuple):
    """Parameters for a regime-specific strategy"""
    side_filter: str  # 'CALLS_ONLY', 'PUTS_ONLY', 'BOTH', 'AVOID'
    adx_entry_min: float
    adx_entry_max: float
    cci_min: float
    cci_max: float
    ema_alignment_required: bool
    target_r: float
    stop_r: float
    time_limit_bars: int
    position_size_multiplier: float

# Regime detection thresholds
REGIME_THRESHOLDS: Dict[str, RegimeThresholds] = {
    'STRONG_UPTREND': RegimeThresholds(
        adx_min=35,
        daily_return_min=2.0,
        ema_bullish_pct_min=60
    ),
    'STRONG_DOWNTREND': RegimeThresholds(
        adx_min=35,
        daily_return_max=-2.0,
        ema_bullish_pct_max=40
    ),
    'RANGE_CHOP': RegimeThresholds(
        adx_max=25,
        daily_return_min=-1.5,
        daily_return_max=1.5,
        swing_ratio_max=0.08
    ),
    'HIGH_VOL_EXPANSION': RegimeThresholds(
        atr_pct_min=3.5
    ),
    'LOW_VOL_DRIFT': RegimeThresholds(
        atr_pct_max=2.0,
        adx_min=20
    )
}

# Strategy parameters per regime
STRATEGY_PARAMS: Dict[str, StrategyParams] = {
    'STRONG_UPTREND': StrategyParams(
        side_filter='CALLS_ONLY',
        adx_entry_min=30,
        adx_entry_max=100,
        cci_min=-100,
        cci_max=100,
        ema_alignment_required=True,  # EMA9 > EMA21
        target_r=2.0,
        stop_r=1.0,
        time_limit_bars=30,
        position_size_multiplier=1.0
    ),
    'STRONG_DOWNTREND': StrategyParams(
        side_filter='PUTS_ONLY',
        adx_entry_min=30,
        adx_entry_max=100,
        cci_min=-100,
        cci_max=100,
        ema_alignment_required=True,  # EMA9 < EMA21
        target_r=2.0,
        stop_r=1.0,
        time_limit_bars=30,
        position_size_multiplier=1.0
    ),
    'RANGE_CHOP': StrategyParams(
        side_filter='BOTH',
        adx_entry_min=0,
        adx_entry_max=25,
        cci_min=-200,  # Extreme reversals
        cci_max=-150,  # or CCI > 150
        ema_alignment_required=False,
        target_r=1.0,
        stop_r=0.75,
        time_limit_bars=15,
        position_size_multiplier=0.5
    ),
    'HIGH_VOL_EXPANSION': StrategyParams(
        side_filter='BOTH',
        adx_entry_min=25,
        adx_entry_max=100,
        cci_min=-200,
        cci_max=200,
        ema_alignment_required=False,
        target_r=2.5,
        stop_r=1.0,
        time_limit_bars=45,
        position_size_multiplier=1.0
    ),
    'LOW_VOL_DRIFT': StrategyParams(
        side_filter='BOTH',
        adx_entry_min=20,
        adx_entry_max=35,
        cci_min=-100,
        cci_max=100,
        ema_alignment_required=True,
        target_r=1.5,
        stop_r=0.75,
        time_limit_bars=60,
        position_size_multiplier=0.75
    ),
    'MIXED_TRANSITION': StrategyParams(
        side_filter='AVOID',
        adx_entry_min=0,
        adx_entry_max=100,
        cci_min=-200,
        cci_max=200,
        ema_alignment_required=False,
        target_r=1.5,
        stop_r=1.0,
        time_limit_bars=30,
        position_size_multiplier=0.25
    )
}


# regime_engine.py
import pandas as pd
import numpy as np
from regime_config import REGIME_THRESHOLDS, STRATEGY_PARAMS

class RegimeEngine:
    """Regime-aware trading engine"""
    
    def __init__(self, config_override=None):
        self.current_regime = None
        self.current_params = None
        self.daily_pnl_r = 0
        self.daily_loss_limit_r = -5.0
        self.config_override = config_override or {}
        
    def detect_regime(self, day_data: pd.DataFrame) -> str:
        """
        Detect regime from first 30-60 minutes of session data
        
        Args:
            day_data: DataFrame with OHLCV + indicators for start of day
            
        Returns:
            regime_label: str
        """
        # Calculate metrics
        daily_return_pct = (day_data['close'].iloc[-1] - day_data['open'].iloc[0]) / day_data['open'].iloc[0] * 100
        avg_adx = day_data['ADX'].mean()
        ema_bullish_pct = (day_data['EMA 9'] > day_data['EMA 21']).sum() / len(day_data) * 100
        avg_atr_pct = (day_data['ATR'].mean() / day_data['close'].mean()) * 100
        
        # Count swings
        highs = day_data['high']
        lows = day_data['low']
        swing_highs = ((highs.shift(1) < highs) & (highs > highs.shift(-1))).sum()
        swing_lows = ((lows.shift(1) > lows) & (lows < lows.shift(-1))).sum()
        swing_ratio = (swing_highs + swing_lows) / len(day_data)
        
        # Check each regime in priority order
        if (daily_return_pct > 2 and avg_adx > 35 and ema_bullish_pct > 60):
            return 'STRONG_UPTREND'
        elif (daily_return_pct < -2 and avg_adx > 35 and ema_bullish_pct < 40):
            return 'STRONG_DOWNTREND'
        elif (avg_atr_pct > 3.5):
            return 'HIGH_VOL_EXPANSION'
        elif (swing_ratio > 0.08 and abs(daily_return_pct) < 1.5 and avg_adx < 25):
            return 'RANGE_CHOP'
        elif (avg_atr_pct < 2.0 and avg_adx > 20):
            return 'LOW_VOL_DRIFT'
        else:
            return 'MIXED_TRANSITION'
    
    def load_strategy(self, regime: str):
        """Load strategy parameters for detected regime"""
        self.current_regime = regime
        self.current_params = STRATEGY_PARAMS[regime]
        print(f"[REGIME] Detected: {regime}")
        print(f"[STRATEGY] Loaded: {self.current_params}")
        
    def should_enter_trade(self, bar: pd.Series, side: str) -> bool:
        """Check if trade entry is valid under current regime"""
        if self.current_params is None:
            return False
        
        # Fail-safe: daily loss limit
        if self.daily_pnl_r <= self.daily_loss_limit_r:
            return False
        
        # Side filter
        if self.current_params.side_filter == 'AVOID':
            return False
        if self.current_params.side_filter == 'CALLS_ONLY' and side != 'CALL':
            return False
        if self.current_params.side_filter == 'PUTS_ONLY' and side != 'PUT':
            return False
        
        # ADX check
        if not (self.current_params.adx_entry_min <= bar['ADX'] <= self.current_params.adx_entry_max):
            return False
        
        # CCI check
        if not (self.current_params.cci_min <= bar['CCI'] <= self.current_params.cci_max):
            # For RANGE_CHOP, also allow extreme positive CCI for PUTs
            if self.current_regime == 'RANGE_CHOP' and side == 'PUT' and bar['CCI'] > 150:
                pass  # Allow
            else:
                return False
        
        # EMA alignment check
        if self.current_params.ema_alignment_required:
            if side == 'CALL' and bar['EMA 9'] <= bar['EMA 21']:
                return False
            if side == 'PUT' and bar['EMA 9'] >= bar['EMA 21']:
                return False
        
        return True
    
    def get_exit_params(self):
        """Get exit parameters for current regime"""
        if self.current_params is None:
            return {'target_r': 1.5, 'stop_r': 1.0, 'time_limit_bars': 30}
        
        return {
            'target_r': self.current_params.target_r,
            'stop_r': self.current_params.stop_r,
            'time_limit_bars': self.current_params.time_limit_bars
        }
    
    def get_position_size(self, base_size: float) -> float:
        """Calculate position size based on regime"""
        if self.current_params is None:
            return base_size * 0.5
        
        return base_size * self.current_params.position_size_multiplier
    
    def update_daily_pnl(self, trade_r: float):
        """Update daily PnL tracker"""
        self.daily_pnl_r += trade_r
    
    def reset_daily_state(self):
        """Reset state for new trading day"""
        self.daily_pnl_r = 0
        self.current_regime = None
        self.current_params = None

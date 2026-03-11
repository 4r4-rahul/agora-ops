"""Deeper ORB sweep with more configs around the winner."""
import pandas as pd
import sys
sys.path.insert(0, ".")

from trading_engine.config import EngineConfig
from trading_engine.data.scalp_backtester import ScalpBacktester
from trading_engine.regime import RegimeDetector

df = pd.read_csv("data/intraday/SPY_ibkr_1m_180d.csv", parse_dates=["timestamp"], index_col="timestamp")
df.index = pd.to_datetime(df.index, utc=True)
print(f"Data loaded: {len(df)} bars\n")

rd = RegimeDetector()

configs = [
    # target_mult, stop_mult, max_hold, skip_regimes, label
    (1.5, 0.7, 90, {"RANGE_BOUND"}, "WINNER: t1.5 s0.7 h90 -RB"),
    (1.5, 0.7, 120, {"RANGE_BOUND"}, "t1.5 s0.7 h120 -RB"),
    (1.5, 1.0, 90, {"RANGE_BOUND"}, "t1.5 s1.0 h90 -RB"),
    (2.0, 0.7, 90, {"RANGE_BOUND"}, "t2.0 s0.7 h90 -RB"),
    (2.0, 1.0, 120, {"RANGE_BOUND"}, "t2.0 s1.0 h120 -RB"),
    (1.5, 0.7, 90, set(), "t1.5 s0.7 h90 all_regimes"),
    (1.5, 0.7, 120, set(), "t1.5 s0.7 h120 all_regimes"),
    # Only trending days (skip RB + MIXED)
    (1.5, 0.7, 90, {"RANGE_BOUND", "MIXED"}, "t1.5 s0.7 h90 -RB-MIX"),
    # Wider stop
    (1.5, 0.5, 90, {"RANGE_BOUND"}, "t1.5 s0.5 h90 -RB"),
    # More time
    (1.5, 0.7, 150, {"RANGE_BOUND"}, "t1.5 s0.7 h150 -RB"),
    # Only TREND days
    (1.5, 0.7, 90, {"RANGE_BOUND", "MIXED", "DEAD_FLAT", "CHOPPY"}, "t1.5 s0.7 h90 TREND_ONLY"),
]

print(f"{'Label':<35s} {'#':>3s} {'WR':>6s} {'PF':>6s} {'ORB PnL':>10s} {'Total':>10s} {'ExitReasons'}")
print("-" * 110)

for target_mult, stop_mult, max_hold, skip_regimes, label in configs:
    cfg = EngineConfig()
    cfg.orb.target_range_mult = target_mult
    cfg.orb.stop_range_mult = stop_mult
    cfg.orb.max_hold_bars = max_hold
    
    bt = ScalpBacktester(config=cfg, account_size=10_000.0, spx_mode=True)
    r = bt.run(df, ticker="SPY", interval="1m", verbose=False)
    
    orb_t = [t for t in r.trades if t.tier == "orb"]
    
    # Filter by regime
    if skip_regimes:
        filtered = []
        for t in orb_t:
            day_bars = df[df.index.date == t.expiry_date]
            if len(day_bars) > 0:
                day_bars_scaled = day_bars.copy()
                for col in ["open", "high", "low", "close"]:
                    day_bars_scaled[col] = day_bars_scaled[col] * 10.0
                regime = rd.classify(day_bars_scaled)
                if regime.regime not in skip_regimes:
                    filtered.append(t)
        orb_t = filtered
    
    n = len(orb_t)
    if n == 0:
        print(f"{label:<35s} {0:>3d}   N/A     N/A      $0          N/A")
        continue
    
    wins = sum(1 for t in orb_t if t.total_pnl > 5)
    orb_pnl = sum(t.total_pnl for t in orb_t)
    total_loss = abs(sum(t.total_pnl for t in orb_t if t.total_pnl < -5))
    pf = orb_pnl / total_loss if total_loss > 0 else float('inf') if orb_pnl > 0 else 0
    wr = wins / n * 100
    
    momentum_pnl = r.scalp_pnl + r.runner_pnl
    total = momentum_pnl + orb_pnl
    
    # Exit reasons
    from collections import Counter
    exits = Counter(t.exit_reason for t in orb_t)
    exit_str = " ".join(f"{k}={v}" for k, v in exits.most_common())
    
    print(f"{label:<35s} {n:>3d} {wr:>5.1f}% {pf:>6.2f} ${orb_pnl:>+9,.0f} ${total:>+9,.0f}  {exit_str}")

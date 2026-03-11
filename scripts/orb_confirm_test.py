"""Test ORB with minimum 3 confirmations (require volume) vs 2."""
import pandas as pd
import sys
sys.path.insert(0, ".")

from trading_engine.config import EngineConfig
from trading_engine.data.scalp_backtester import ScalpBacktester

df = pd.read_csv("data/intraday/SPY_ibkr_1m_180d.csv", parse_dates=["timestamp"], index_col="timestamp")
df.index = pd.to_datetime(df.index, utc=True)

# Also add MIXED to skip (only trade MODERATE_TREND + STRONG_TREND)
configs = [
    ("Current (no min conf)", False, 1),
    ("Min 3 confirms (need vol)", False, 3),
    ("Skip MIXED too", True, 1),
    ("Skip MIXED + min 3 conf", True, 3),
]

for label, skip_mixed, min_conf in configs:
    cfg = EngineConfig()
    cfg.orb.max_hold_bars = 120
    
    bt = ScalpBacktester(config=cfg, account_size=10_000.0, spx_mode=True)
    r = bt.run(df, ticker="SPY", interval="1m", verbose=False)
    
    orb_t = [t for t in r.trades if t.tier == "orb"]
    
    # Post-filter
    if skip_mixed or min_conf > 1:
        from trading_engine.regime import RegimeDetector
        rd = RegimeDetector()
        filtered = []
        for t in orb_t:
            if len(t.confirmations) < min_conf:
                continue
            if skip_mixed:
                day_bars = df[df.index.date == t.expiry_date]
                day_bars_scaled = day_bars.copy()
                for col in ["open", "high", "low", "close"]:
                    day_bars_scaled[col] = day_bars_scaled[col] * 10.0
                regime = rd.classify(day_bars_scaled)
                if regime.regime == "MIXED":
                    continue
            filtered.append(t)
        orb_t = filtered
    
    n = len(orb_t)
    wins = sum(1 for t in orb_t if t.total_pnl > 5)
    pnl = sum(t.total_pnl for t in orb_t)
    losses = abs(sum(t.total_pnl for t in orb_t if t.total_pnl < -5))
    wr = wins / n * 100 if n > 0 else 0
    pf = pnl / losses if losses > 0 else float('inf')
    mom_pnl = r.scalp_pnl + r.runner_pnl
    
    print(f"{label:<30s} | {n:>3d} trades | {wr:>5.1f}% WR | PF={pf:>5.2f} | "
          f"ORB=${pnl:>+9,.0f} | Total=${mom_pnl + pnl:>+9,.0f}")

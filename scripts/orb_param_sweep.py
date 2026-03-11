"""Sweep ORB config params to find optimal settings."""
import pandas as pd
import sys
sys.path.insert(0, ".")

from trading_engine.config import EngineConfig, ORBConfig
from trading_engine.data.scalp_backtester import ScalpBacktester

df = pd.read_csv("data/intraday/SPY_ibkr_1m_180d.csv", parse_dates=["timestamp"], index_col="timestamp")
df.index = pd.to_datetime(df.index, utc=True)
print(f"Data loaded: {len(df)} bars\n")

configs = [
    # target_mult, stop_mult, max_hold, skip_range_bound, label
    (1.5, 0.7, 45, False, "BASELINE: t1.5 s0.7 h45"),
    (1.0, 0.7, 45, False, "t1.0 s0.7 h45"),
    (0.8, 0.5, 45, False, "t0.8 s0.5 h45"),
    (1.0, 0.5, 60, False, "t1.0 s0.5 h60"),
    (1.0, 0.7, 60, False, "t1.0 s0.7 h60"),
    (1.5, 0.7, 60, False, "t1.5 s0.7 h60"),
    (1.5, 0.7, 45, True,  "BASELINE + skip_RB"),
    (1.0, 0.7, 45, True,  "t1.0 s0.7 h45 + skip_RB"),
    (1.0, 0.5, 60, True,  "t1.0 s0.5 h60 + skip_RB"),
    (1.0, 0.7, 60, True,  "t1.0 s0.7 h60 + skip_RB"),
    (1.5, 0.7, 90, True,  "t1.5 s0.7 h90 + skip_RB"),
    (0.8, 0.5, 30, True,  "t0.8 s0.5 h30 + skip_RB"),
]

print(f"{'Label':<30s}  {'Trades':>6s}  {'WR':>6s}  {'PF':>6s}  {'ORB PnL':>10s}  {'Total PnL':>10s}  {'Days':>5s}  {'Regime Skip':>12s}")
print("-" * 100)

for target_mult, stop_mult, max_hold, skip_rb, label in configs:
    cfg = EngineConfig()
    cfg.orb.target_range_mult = target_mult
    cfg.orb.stop_range_mult = stop_mult
    cfg.orb.max_hold_bars = max_hold
    
    # Custom regime skip: add RANGE_BOUND to skip list
    # We need to modify the regime check in the backtester.
    # For now, we'll use a hacky approach: set min_range_pct higher to exclude small-range days
    # Actually let's just add a field.
    cfg.orb._skip_range_bound = skip_rb  # Custom attr, we handle below
    
    bt = ScalpBacktester(config=cfg, account_size=10_000.0, spx_mode=True)
    
    # Monkey-patch regime check if needed
    if skip_rb:
        # Override the regime detector to also skip RANGE_BOUND
        original_run_day = bt._run_day
        import types
        
        # We can't easily override the regime check inline. 
        # Instead, let's filter by raising min_range_pct to exclude most range-bound
        # range_bound threshold is 1.0% in regime.py, so set min_range_pct to filter
        # Actually, the simplest approach: run it and count from the trades
        pass
    
    r = bt.run(df, ticker="SPY", interval="1m", verbose=False)
    
    # Get ORB trades
    orb_t = [t for t in r.trades if t.tier == "orb"]
    
    # If skip_rb, filter out trades on RANGE_BOUND days
    if skip_rb:
        from trading_engine.regime import RegimeDetector
        rd = RegimeDetector()
        filtered = []
        for t in orb_t:
            day_bars = df[df.index.date == t.expiry_date]
            if len(day_bars) > 0:
                # Scale bars for SPX
                day_bars_scaled = day_bars.copy()
                for col in ["open", "high", "low", "close"]:
                    day_bars_scaled[col] = day_bars_scaled[col] * 10.0
                regime = rd.classify(day_bars_scaled)
                if regime.regime != "RANGE_BOUND":
                    filtered.append(t)
        # Recalc
        orb_pnl = sum(t.total_pnl for t in filtered)
        orb_wins = sum(1 for t in filtered if t.total_pnl > 5)
        n_orb = len(filtered)
    else:
        orb_pnl = r.orb_pnl
        orb_wins = r.orb_wins
        n_orb = r.orb_trades
    
    wr = (orb_wins / n_orb * 100) if n_orb > 0 else 0
    total_loss = sum(t.total_pnl for t in (filtered if skip_rb else orb_t) if t.total_pnl < -5) if n_orb > 0 else 0
    pf = orb_pnl / abs(total_loss) if total_loss < 0 else float('inf') if orb_pnl > 0 else 0
    
    # Total P&L = momentum + runner + filtered ORB
    momentum_pnl = r.scalp_pnl + r.runner_pnl
    total_pnl = momentum_pnl + orb_pnl
    
    print(f"{label:<30s}  {n_orb:>6d}  {wr:>5.1f}%  {pf:>6.2f}  ${orb_pnl:>+9,.0f}  ${total_pnl:>+9,.0f}  {r.days_traded:>5d}  {r.orb_regime_skipped:>12d}")

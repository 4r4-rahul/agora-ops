"""Analyze ORB trades in detail to understand why performance is weak."""
import pandas as pd
import sys
sys.path.insert(0, ".")

from trading_engine.config import EngineConfig
from trading_engine.data.scalp_backtester import ScalpBacktester

df = pd.read_csv("data/intraday/SPY_ibkr_1m_180d.csv", parse_dates=["timestamp"], index_col="timestamp")
df.index = pd.to_datetime(df.index, utc=True)

bt = ScalpBacktester(config=EngineConfig(), account_size=10_000.0, spx_mode=True)
r = bt.run(df, ticker="SPY", interval="1m", verbose=False)

# Extract ORB trades
orb_trades = [t for t in r.trades if t.tier == "orb"]
print(f"=== ORB TRADE DETAIL ({len(orb_trades)} trades) ===\n")

for t in orb_trades:
    hold_bars = int(t.hold_minutes) if t.hold_minutes else 0
    delta = t.exit_underlying - t.entry_underlying if t.exit_underlying else 0
    direction_sign = 1 if t.direction == "CALL" else -1
    favorable_delta = delta * direction_sign
    
    print(f"  {t.expiry_date} {t.direction} {t.strike}{t.right}")
    print(f"    Entry: ${t.entry_underlying:.2f} @ {t.entry_time}")
    print(f"    Exit:  ${t.exit_underlying:.2f} @ {t.exit_time} ({t.exit_reason})")
    print(f"    Premium: ${t.entry_premium:.2f} → ${t.exit_premium:.2f}")
    print(f"    Contracts: {t.num_contracts} | Hold: {t.hold_minutes:.0f}m")
    print(f"    P&L: ${t.total_pnl:+,.2f} | Move: ${delta:+.2f} (favorable: ${favorable_delta:+.2f})")
    print(f"    Confirmations: {', '.join(t.confirmations)}")
    print(f"    ATR: ${t.atr_at_entry:.3f} | Stop: ${t.stop_price:.2f} | Target: ${t.target_price:.2f}")
    
    # Compute target/stop distances
    if t.direction == "CALL":
        target_dist = t.target_price - t.entry_underlying
        stop_dist = t.entry_underlying - t.stop_price
    else:
        target_dist = t.entry_underlying - t.target_price
        stop_dist = t.stop_price - t.entry_underlying
    
    print(f"    Target dist: ${target_dist:.2f} | Stop dist: ${stop_dist:.2f} | R:R = {target_dist/stop_dist:.1f}x")
    print()

# Summary stats
wins = [t for t in orb_trades if t.total_pnl > 5]
losses = [t for t in orb_trades if t.total_pnl < -5]
print(f"Wins: {len(wins)} | Losses: {len(losses)}")
print(f"Exit reasons: {', '.join(t.exit_reason for t in orb_trades)}")
print(f"Avg win: ${sum(t.total_pnl for t in wins)/len(wins):+,.0f}" if wins else "No wins")
print(f"Avg loss: ${sum(t.total_pnl for t in losses)/len(losses):+,.0f}" if losses else "No losses")

# Check what regime each ORB trade was in  
print("\n=== ORB TRADES BY REGIME ===")
from trading_engine.regime import RegimeDetector
rd = RegimeDetector()
all_dates = sorted(df.index.normalize().unique())
for t in orb_trades:
    import pytz
    trade_date = t.expiry_date
    day_bars = df[df.index.date == trade_date]
    if len(day_bars) > 0:
        regime = rd.classify(day_bars)
        print(f"  {trade_date} {t.direction}: {regime.regime} (range={regime.day_range_pct:.2f}%, "
              f"trend_ratio={regime.trend_ratio:.2f}) → ${t.total_pnl:+,.2f}")

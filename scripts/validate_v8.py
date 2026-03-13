"""Validate v8.0 trade management upgrade."""
import pandas as pd
import sys
sys.path.insert(0, ".")

from trading_engine.config import EngineConfig
from trading_engine.data.scalp_backtester import ScalpBacktester

df = pd.read_csv("data/intraday/SPY_ibkr_1m_180d.csv", parse_dates=["timestamp"], index_col="timestamp")
df.index = pd.to_datetime(df.index, utc=True)
print(f"Data loaded: {len(df)} bars")

bt = ScalpBacktester(config=EngineConfig(), account_size=10_000.0, spx_mode=True)
r = bt.run(df, ticker="SPY", interval="1m", verbose=False)

print()
print("=== v8.0 TRADE MANAGEMENT UPGRADE ===")
print(f"Total: trades={r.total_trades} WR={r.win_rate:.1f}% PF={r.profit_factor:.2f} PnL=${r.total_pnl:+,.0f}")
print(f"  Scalp:  {r.scalp_trades} trades, ${r.scalp_pnl:+,.0f}")
print(f"  Runner: {r.runner_trades} trades, ${r.runner_pnl:+,.0f}")
print(f"  ORB:    {r.orb_trades} trades, ${r.orb_pnl:+,.0f}")
print(f"  MR:     {r.mr_trades} trades, ${r.mr_pnl:+,.0f}")
print(f"  RF:     {r.rf_trades} trades, ${r.rf_pnl:+,.0f}")
print(f"  VM:     {r.vm_trades} trades, ${r.vm_pnl:+,.0f}")
print(f"Days traded: {r.days_traded}/{r.total_days}")
print(f"Equity: ${r.starting_balance:,.0f} -> ${r.ending_balance:,.0f}")
print(f"Max DD: {r.max_drawdown_pct:.1%}")

# Average contracts per trade
if r.trades:
    avg_contracts = sum(t.num_contracts for t in r.trades) / len(r.trades)
    max_contracts = max(t.num_contracts for t in r.trades)
    print(f"Avg contracts: {avg_contracts:.1f}, Max: {max_contracts}")

# Check for partial exits
partial_count = sum(1 for t in r.trades if t.partial_pnl != 0)
print(f"Trades with partial exits: {partial_count}")

print()
print("=== BASELINE COMPARISON ===")
print(f"Old: 69 trades, WR=62.3%, PF=3.52, PnL=$+36,767, Equity=$46,767")
print(f"New: {r.total_trades} trades, WR={r.win_rate:.1f}%, PF={r.profit_factor:.2f}, PnL=${r.total_pnl:+,.0f}, Equity=${r.ending_balance:,.0f}")
delta = r.total_pnl - 36767
print(f"Delta PnL: ${delta:+,.0f} ({delta/36767*100:+.1f}%)")

# Per-trade breakdown
print()
print("=== PER-TRADE DETAILS (last 15 trades) ===")
for t in r.trades[-15:]:
    partial_flag = " [PARTIAL]" if t.partial_pnl != 0 else ""
    print(f"  {t.entry_time.strftime('%Y-%m-%d %H:%M')} {t.tier:10s} {t.direction:4s} "
          f"{t.strike}{t.right} x{t.num_contracts} "
          f"-> {t.exit_reason:15s} PnL=${t.total_pnl:+,.0f} "
          f"hold={t.hold_minutes:.0f}m{partial_flag}")

# Equity curve monthly
print()
print("=== MONTHLY PnL ===")
monthly = {}
for t in r.trades:
    key = t.entry_time.strftime("%Y-%m")
    monthly[key] = monthly.get(key, 0) + t.total_pnl
for m, pnl in sorted(monthly.items()):
    print(f"  {m}: ${pnl:+,.0f}")

"""Final v8.0 validation — compounding + partial exits + runner prune."""
import pandas as pd
import sys
sys.path.insert(0, ".")

from trading_engine.config import EngineConfig
from trading_engine.data.scalp_backtester import ScalpBacktester

df = pd.read_csv("data/intraday/SPY_ibkr_1m_180d.csv", parse_dates=["timestamp"], index_col="timestamp")
df.index = pd.to_datetime(df.index, utc=True)

cfg = EngineConfig()
bt = ScalpBacktester(config=cfg, account_size=10_000.0, spx_mode=True)
r = bt.run(df, ticker="SPY", interval="1m", verbose=False)

print("=" * 60)
print("  v8.0 TRADE MANAGEMENT — FINAL VALIDATION")
print("=" * 60)
print()
print(f"Trades: {r.total_trades}  |  WR: {r.win_rate:.1f}%  |  PF: {r.profit_factor:.2f}")
print(f"PnL: ${r.total_pnl:+,.0f}  |  Equity: ${r.starting_balance:,.0f} -> ${r.ending_balance:,.0f}")
print(f"Max DD: {r.max_drawdown_pct:.1%}  |  Days traded: {r.days_traded}/{r.total_days}")
print()

print("Per-tier breakdown:")
print(f"  ORB:        {r.orb_trades:3d} trades  ${r.orb_pnl:+10,.0f}")
print(f"  Scalp:      {r.scalp_trades:3d} trades  ${r.scalp_pnl:+10,.0f}")
print(f"  RangeFade:  {r.rf_trades:3d} trades  ${r.rf_pnl:+10,.0f}")
print(f"  Runner:     {r.runner_trades:3d} trades  ${r.runner_pnl:+10,.0f}  (disabled)")
print(f"  MR:         {r.mr_trades:3d} trades  ${r.mr_pnl:+10,.0f}")

# Compounding metrics
avg_contracts = sum(t.num_contracts for t in r.trades) / len(r.trades) if r.trades else 0
max_contracts = max(t.num_contracts for t in r.trades) if r.trades else 0
partial_count = sum(1 for t in r.trades if t.partial_pnl != 0)
total_partial_pnl = sum(t.partial_pnl for t in r.trades)

print()
print("Position sizing & partial exits:")
print(f"  Avg contracts/trade: {avg_contracts:.1f}")
print(f"  Max contracts:       {max_contracts}")
print(f"  Partial exits:       {partial_count}/{r.total_trades} trades")
print(f"  Partial PnL booked:  ${total_partial_pnl:+,.0f}")

# Contract size evolution
early = [t for t in r.trades[:20]]
late = [t for t in r.trades[-20:]]
early_avg = sum(t.num_contracts for t in early) / len(early) if early else 0
late_avg = sum(t.num_contracts for t in late) / len(late) if late else 0
print(f"  Avg contracts (first 20): {early_avg:.1f}")
print(f"  Avg contracts (last 20):  {late_avg:.1f}")

# Monthly
print()
print("Monthly PnL:")
monthly = {}
for t in r.trades:
    key = t.entry_time.strftime("%Y-%m")
    monthly[key] = monthly.get(key, 0) + t.total_pnl
for m, pnl in sorted(monthly.items()):
    bar = "+" * int(abs(pnl) / 500) if pnl > 0 else "-" * int(abs(pnl) / 500)
    print(f"  {m}: ${pnl:+8,.0f}  {bar}")

# Exit reasons
print()
print("Exit reasons:")
reasons = {}
for t in r.trades:
    reasons[t.exit_reason] = reasons.get(t.exit_reason, {"count": 0, "pnl": 0.0})
    reasons[t.exit_reason]["count"] += 1
    reasons[t.exit_reason]["pnl"] += t.total_pnl
for reason, data in sorted(reasons.items(), key=lambda x: -x[1]["pnl"]):
    print(f"  {reason:17s}  {data['count']:3d} trades  ${data['pnl']:+10,.0f}")

print()
print("=" * 60)
print("  BEFORE vs AFTER")
print("=" * 60)
print(f"  Old v7.3:  69 trades, WR=62.3%, PF=3.52, PnL=$36,767, DD=13.5%")
print(f"  New v8.0:  {r.total_trades} trades, WR={r.win_rate:.1f}%, PF={r.profit_factor:.2f}, PnL=${r.total_pnl:+,.0f}, DD={r.max_drawdown_pct:.1%}")
delta = r.total_pnl - 36767
print(f"  Delta:     ${delta:+,.0f} ({delta/36767*100:+.1f}%)")
print()
print("  Key improvements:")
print(f"    Scalp:    $4,621 -> ${r.scalp_pnl:+,.0f}  ({(r.scalp_pnl-4621)/4621*100:+.0f}%)")
print(f"    ORB:     $24,270 -> ${r.orb_pnl:+,.0f}  ({(r.orb_pnl-24270)/24270*100:+.0f}%)")
print(f"    RF:       $6,796 -> ${r.rf_pnl:+,.0f}  ({(r.rf_pnl-6796)/6796*100:+.0f}%)" if r.rf_pnl > 0 else "")
print(f"    Contracts:    1.2 -> {avg_contracts:.1f}  (compounding working)")
print(f"    Partial exits:   0 -> {partial_count}  (locking in gains)")

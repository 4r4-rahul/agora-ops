"""Test ORB30 integration - Phase 1 validation (optimized config)."""
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

print("\n=== PHASE 1 FINAL VALIDATION ===")
print(f"Total: trades={r.total_trades} WR={r.win_rate:.1f}% PF={r.profit_factor:.2f} PnL=${r.total_pnl:+,.0f}")
print(f"  Momentum: {r.scalp_trades} trades, ${r.scalp_pnl:+,.0f}")
print(f"  Runner:   {r.runner_trades} trades, ${r.runner_pnl:+,.0f}")
print(f"  ORB:      {r.orb_trades} trades, ${r.orb_pnl:+,.0f} (regime skipped={r.orb_regime_skipped} days)")
print(f"  MR:       {r.mr_trades} trades, ${r.mr_pnl:+,.0f}")
print(f"Days traded: {r.days_traded}/{r.total_days}")

# Regression check  
print("\n=== REGRESSION CHECK ===")
momentum_total = r.scalp_trades + r.runner_trades
momentum_pnl = r.scalp_pnl + r.runner_pnl
if r.scalp_trades == 12 and r.runner_trades == 3:
    print(f"✅ Momentum: 12 scalp + 3 runner = 15 total, ${momentum_pnl:+,.0f} (no regression)")
else:
    print(f"⚠️  Momentum: {r.scalp_trades} scalp + {r.runner_trades} runner = {momentum_total}, ${momentum_pnl:+,.0f}")

if r.orb_trades > 0:
    orb_wr = r.orb_wins / r.orb_trades * 100 if r.orb_trades > 0 else 0
    print(f"✅ ORB: {r.orb_trades} trades, {orb_wr:.0f}% WR, ${r.orb_pnl:+,.0f}")
    if hasattr(r, "orb_biggest_win"):
        print(f"   Biggest ORB win: ${r.orb_biggest_win:+,.0f}")
    if hasattr(r, "orb_avg_hold"):
        print(f"   Avg ORB hold: {r.orb_avg_hold:.0f} bars")
else:
    print("⚠️  ORB: 0 trades")

# ORB trade details
orb_trades = [t for t in r.trades if t.tier == "orb"]
if orb_trades:
    print("\n=== ORB TRADE DETAILS ===")
    for t in orb_trades:
        delta = t.exit_underlying - t.entry_underlying
        print(f"  {t.expiry_date} {t.direction} {t.strike}{t.right} "
              f"| {t.exit_reason:13s} | hold={t.hold_minutes:.0f}m "
              f"| move=${delta:+.2f} | P&L=${t.total_pnl:+,.2f}")

bt.print_report(r)

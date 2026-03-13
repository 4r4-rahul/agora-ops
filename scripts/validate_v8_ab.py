"""Compare A/B: with vs without RangeFade + compounding."""
import pandas as pd
import sys
sys.path.insert(0, ".")

from trading_engine.config import EngineConfig
from trading_engine.data.scalp_backtester import ScalpBacktester

df = pd.read_csv("data/intraday/SPY_ibkr_1m_180d.csv", parse_dates=["timestamp"], index_col="timestamp")
df.index = pd.to_datetime(df.index, utc=True)
print(f"Data loaded: {len(df)} bars")

configs = {
    "A: No RF, No Runner": lambda: EngineConfig(),  # current defaults
    "B: With RF, No Runner": None,
}

# Config B: enable RF
def make_config_b():
    cfg = EngineConfig()
    cfg.range_fade.enabled = True
    return cfg

configs["B: With RF, No Runner"] = make_config_b

for label, cfg_fn in configs.items():
    cfg = cfg_fn()
    bt = ScalpBacktester(config=cfg, account_size=10_000.0, spx_mode=True)
    r = bt.run(df, ticker="SPY", interval="1m", verbose=False)
    
    partial_count = sum(1 for t in r.trades if t.partial_pnl != 0)
    avg_contracts = sum(t.num_contracts for t in r.trades) / len(r.trades) if r.trades else 0
    max_contracts = max(t.num_contracts for t in r.trades) if r.trades else 0
    
    print(f"\n{label}:")
    print(f"  Trades={r.total_trades} WR={r.win_rate:.1f}% PF={r.profit_factor:.2f} PnL=${r.total_pnl:+,.0f}")
    print(f"  Scalp=${r.scalp_pnl:+,.0f} ORB=${r.orb_pnl:+,.0f} RF=${r.rf_pnl:+,.0f}")
    print(f"  Equity=${r.ending_balance:,.0f}  MaxDD={r.max_drawdown_pct:.1%}")
    print(f"  Avg contracts={avg_contracts:.1f} Max={max_contracts} Partials={partial_count}")

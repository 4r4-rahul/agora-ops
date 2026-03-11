import sys
sys.path.insert(0, ".")
import pandas as pd
from trading_engine.config import EngineConfig as EC
from trading_engine.data.scalp_backtester import ScalpBacktester as SB

df = pd.read_csv("data/intraday/SPY_ibkr_1m_180d.csv", parse_dates=["timestamp"], index_col="timestamp")
df.index = pd.to_datetime(df.index, utc=True)

print("=== IV DISCOUNT FILTER TEST ===")

# BASELINE: IV filter OFF
c = EC()
c.scalp.iv_discount_enabled = False
r = SB(config=c, account_size=10000, spx_mode=True).run(df, ticker="SPY", interval="1m", verbose=False)
print("BASELINE (IV OFF): trades=%d WR=%.1f%% PF=%.2f PnL=$%+.0f blocked=%d" % (r.total_trades, r.win_rate, r.profit_factor, r.total_pnl, r.iv_blocked_signals))

# IV filter ON with default ratio (0.8)
c = EC()
c.scalp.iv_discount_enabled = True
c.scalp.rv_iv_min_ratio = 0.8
r = SB(config=c, account_size=10000, spx_mode=True).run(df, ticker="SPY", interval="1m", verbose=False)
print("IV ON r=0.80: trades=%d WR=%.1f%% PF=%.2f PnL=$%+.0f blocked=%d" % (r.total_trades, r.win_rate, r.profit_factor, r.total_pnl, r.iv_blocked_signals))

# Sweep ratios
print("\n=== IV RATIO SWEEP ===")
for ratio in [0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.5]:
    c = EC()
    c.scalp.iv_discount_enabled = True
    c.scalp.rv_iv_min_ratio = ratio
    r = SB(config=c, account_size=10000, spx_mode=True).run(df, ticker="SPY", interval="1m", verbose=False)
    pf_str = "%.2f" % r.profit_factor if r.profit_factor < 100 else "INF"
    print("ratio=%.1f: trades=%d WR=%.1f%% PF=%s PnL=$%+.0f blk=%d s=$%+.0f r=$%+.0f" % (ratio, r.total_trades, r.win_rate, pf_str, r.total_pnl, r.iv_blocked_signals, r.scalp_pnl, r.runner_pnl))

# Also test lookback periods
print("\n=== RV LOOKBACK SWEEP (ratio=0.8) ===")
for lb in [10, 15, 20, 30, 40]:
    c = EC()
    c.scalp.iv_discount_enabled = True
    c.scalp.rv_iv_min_ratio = 0.8
    c.scalp.rv_lookback_bars = lb
    r = SB(config=c, account_size=10000, spx_mode=True).run(df, ticker="SPY", interval="1m", verbose=False)
    pf_str = "%.2f" % r.profit_factor if r.profit_factor < 100 else "INF"
    print("lb=%d: trades=%d WR=%.1f%% PF=%s PnL=$%+.0f blk=%d" % (lb, r.total_trades, r.win_rate, pf_str, r.total_pnl, r.iv_blocked_signals))

print("\nDONE")

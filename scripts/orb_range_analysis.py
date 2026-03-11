"""Analyze ORB range vs trade outcome to find a causal filter.
No look-ahead bias: only uses info available at time of entry (bar 30)."""
import pandas as pd
import sys
sys.path.insert(0, ".")

from trading_engine.config import EngineConfig
from trading_engine.data.scalp_backtester import ScalpBacktester
from trading_engine.regime import RegimeDetector

df = pd.read_csv("data/intraday/SPY_ibkr_1m_180d.csv", parse_dates=["timestamp"], index_col="timestamp")
df.index = pd.to_datetime(df.index, utc=True)

# Run with early classification (no look-ahead) and wide-open regime filter
cfg = EngineConfig()
cfg.orb.skip_range_bound = False  # Don't skip anything
cfg.orb.skip_dead_flat = False
cfg.orb.skip_choppy = False
cfg.orb.regime_filter_enabled = False  # Disable regime filter entirely
cfg.orb.max_hold_bars = 120

bt = ScalpBacktester(config=cfg, account_size=10_000.0, spx_mode=True)
r = bt.run(df, ticker="SPY", interval="1m", verbose=False)

orb_trades = [t for t in r.trades if t.tier == "orb"]
print(f"Total ORB trades (no filter): {len(orb_trades)}\n")

# For each trade, get ORB range (which is available at entry time)
# and check full-day regime for analysis purposes
rd = RegimeDetector()

print(f"{'Date':<12s} {'Dir':>4s} {'ORBRange':>9s} {'ORBRng%':>8s} {'Move':>8s} {'P&L':>10s} {'Exit':>14s} {'Regime':>16s} {'TrendR':>7s}")
print("-" * 100)

wins_by_range = {}
for t in orb_trades:
    day_bars = df[df.index.date == t.expiry_date]
    day_bars_scaled = day_bars.copy()
    for col in ["open", "high", "low", "close"]:
        day_bars_scaled[col] = day_bars_scaled[col] * 10.0
    regime = rd.classify(day_bars_scaled)
    
    # ORB range = stop_dist / stop_mult
    # From the trade: stop_dist = entry_underlying - stop_price (for CALL)
    if t.direction == "CALL":
        stop_dist = t.entry_underlying - t.stop_price
    else:
        stop_dist = t.stop_price - t.entry_underlying
    orb_range = stop_dist / 0.7  # stop_range_mult = 0.7
    orb_range_pct = orb_range / t.entry_underlying * 100
    
    delta = t.exit_underlying - t.entry_underlying
    
    win = "W" if t.total_pnl > 5 else "L"
    
    print(f"  {t.expiry_date} {t.direction:>4s} ${orb_range:>7.2f} {orb_range_pct:>7.3f}% ${delta:>+7.2f} ${t.total_pnl:>+9,.2f} {t.exit_reason:>14s} {regime.regime:>16s} {regime.trend_ratio:>6.2f}")
    
    # Bucket by ORB range pct
    bucket = ">=0.9%" if orb_range_pct >= 0.9 else ">=0.8%" if orb_range_pct >= 0.8 else ">=0.7%" if orb_range_pct >= 0.7 else "<0.7%"
    if bucket not in wins_by_range:
        wins_by_range[bucket] = {"n": 0, "w": 0, "pnl": 0}
    wins_by_range[bucket]["n"] += 1
    wins_by_range[bucket]["w"] += 1 if t.total_pnl > 5 else 0
    wins_by_range[bucket]["pnl"] += t.total_pnl

print(f"\n{'Bucket':<10s} {'#':>4s} {'WR':>6s} {'PnL':>10s}")
print("-" * 35)
for bucket in ["<0.7%", ">=0.7%", ">=0.8%", ">=0.9%"]:
    if bucket in wins_by_range:
        d = wins_by_range[bucket]
        wr = d["w"] / d["n"] * 100
        print(f"{bucket:<10s} {d['n']:>4d} {wr:>5.1f}% ${d['pnl']:>+9,.0f}")

# Also bucket by absolute range
print(f"\n{'AbsRange':<12s} {'#':>4s} {'WR':>6s} {'PnL':>10s}")
print("-" * 37)
abs_buckets = {}
for t in orb_trades:
    if t.direction == "CALL":
        stop_dist = t.entry_underlying - t.stop_price
    else:
        stop_dist = t.stop_price - t.entry_underlying
    orb_range = stop_dist / 0.7
    
    if orb_range >= 60:
        b = ">=60"
    elif orb_range >= 50:
        b = ">=50"
    elif orb_range >= 40:
        b = ">=40"
    else:
        b = "<40"
    
    if b not in abs_buckets:
        abs_buckets[b] = {"n": 0, "w": 0, "pnl": 0}
    abs_buckets[b]["n"] += 1
    abs_buckets[b]["w"] += 1 if t.total_pnl > 5 else 0
    abs_buckets[b]["pnl"] += t.total_pnl

for b in ["<40", ">=40", ">=50", ">=60"]:
    if b in abs_buckets:
        d = abs_buckets[b]
        wr = d["w"] / d["n"] * 100
        print(f"  ${b:<10s} {d['n']:>4d} {wr:>5.1f}% ${d['pnl']:>+9,.0f}")

# Volume confirmation analysis
print(f"\n{'VolumeConf':<14s} {'#':>4s} {'WR':>6s} {'PnL':>10s}")
print("-" * 39)
vol_buckets = {"with_vol": {"n":0, "w":0, "pnl":0}, "without_vol": {"n":0, "w":0, "pnl":0}}
for t in orb_trades:
    if "ORB_VOLUME" in t.confirmations:
        k = "with_vol"
    else:
        k = "without_vol"
    vol_buckets[k]["n"] += 1
    vol_buckets[k]["w"] += 1 if t.total_pnl > 5 else 0
    vol_buckets[k]["pnl"] += t.total_pnl

for k in ["with_vol", "without_vol"]:
    d = vol_buckets[k]
    wr = d["w"] / d["n"] * 100 if d["n"] > 0 else 0
    print(f"  {k:<12s} {d['n']:>4d} {wr:>5.1f}% ${d['pnl']:>+9,.0f}")

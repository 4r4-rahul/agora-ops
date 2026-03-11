"""Compare early vs full-day regime classification on the 8 trades that get through."""
import pandas as pd
import sys
sys.path.insert(0, ".")

from trading_engine.config import EngineConfig
from trading_engine.data.scalp_backtester import ScalpBacktester
from trading_engine.regime import RegimeDetector

df = pd.read_csv("data/intraday/SPY_ibkr_1m_180d.csv", parse_dates=["timestamp"], index_col="timestamp")
df.index = pd.to_datetime(df.index, utc=True)

# Run with early classification (the default) + skip_dead_flat + skip_choppy + skip_range_bound
cfg = EngineConfig()
cfg.orb.max_hold_bars = 120
# Revert to early classification for this test
cfg.orb.skip_range_bound = True

bt = ScalpBacktester(config=cfg, account_size=10_000.0, spx_mode=True)

# Temporarily patch to use classify_early
from trading_engine import regime as regime_mod
original_classify = regime_mod.RegimeDetector.classify

# We want to see what classify_early says for each day
rd = RegimeDetector()

# For all 129 days, compare early vs full
all_dates = sorted(set(df.index.date))
print(f"{'Date':<12s} {'Early':>16s} {'Full':>16s} {'Match':>6s}")
print("-" * 55)

mismatches = []
for d in all_dates:
    day_bars = df[df.index.date == d]
    if len(day_bars) < 30:
        continue
    day_bars_scaled = day_bars.copy()
    for col in ["open", "high", "low", "close"]:
        day_bars_scaled[col] = day_bars_scaled[col] * 10.0
    
    early = rd.classify_early(day_bars_scaled, 30)
    full = rd.classify(day_bars_scaled)
    
    match = "✅" if early.regime == full.regime else "❌"
    if early.regime != full.regime:
        mismatches.append((d, early.regime, full.regime))
        print(f"  {d} {early.regime:>16s} {full.regime:>16s} {match:>6s}")

print(f"\nTotal days: {len(all_dates)}")
print(f"Mismatches: {len(mismatches)}")
print(f"\nMismatch breakdown:")
from collections import Counter
mismatch_types = Counter((m[1], m[2]) for m in mismatches)
for (early_r, full_r), count in mismatch_types.most_common():
    print(f"  Early={early_r:>16s} → Full={full_r:>16s}  ({count}x)")

# Now check specifically: which days would early-classify as NOT skipped
# but full-classify as skipped (these are the leak-through days)
print(f"\n=== LEAK-THROUGH ANALYSIS ===")
skip_regimes = {"DEAD_FLAT", "CHOPPY", "RANGE_BOUND"}
for d in all_dates:
    day_bars = df[df.index.date == d]
    if len(day_bars) < 30:
        continue
    day_bars_scaled = day_bars.copy()
    for col in ["open", "high", "low", "close"]:
        day_bars_scaled[col] = day_bars_scaled[col] * 10.0
    
    early = rd.classify_early(day_bars_scaled, 30)
    full = rd.classify(day_bars_scaled)
    
    early_skip = early.regime in skip_regimes
    full_skip = full.regime in skip_regimes
    
    if not early_skip and full_skip:
        print(f"  LEAK: {d} early={early.regime} (range={early.day_range_pct:.4f}, trend={early.trend_ratio:.2f}) "
              f"→ full={full.regime} (range={full.day_range_pct:.4f}, trend={full.trend_ratio:.2f})")
    elif early_skip and not full_skip:
        print(f"  OVER-SKIP: {d} early={early.regime} → full={full.regime}")

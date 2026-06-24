#!/usr/bin/env python3
"""Quick analysis of lotto backtest results."""
import json

with open("lotto_backtest_results.json") as f:
    data = json.load(f)

trades = data["trades"]
stops = [t for t in trades if "STOP_LOSS" in t["exit_reason"]]
winners = [t for t in trades if t["pnl"] > 0]

print("=== STOP-LOSS ANALYSIS ===")
print(f"Stop-losses: {len(stops)}/{len(trades)} = {len(stops)/len(trades)*100:.0f}%")
print(f"Total stop loss damage: ${sum(t['pnl'] for t in stops):,.2f}")
print()

print("=== RE-ENTRY PROBLEM ===")
from collections import Counter

day_dir = Counter()
for t in trades:
    key = f"{t['date']}_{t['direction']}"
    day_dir[key] += 1
repeats = {k: v for k, v in day_dir.items() if v > 1}
print(f"Days with repeated same-direction entries: {len(repeats)}")
for k, v in sorted(repeats.items()):
    print(f"  {k}: {v} trades")

print()
print("=== TRIGGER QUALITY ===")
for trig, stats in data["trigger_stats"].items():
    edge = stats["avg_mult"] - 1.0
    print(f"  {trig:18s}: avg {stats['avg_mult']:.2f}x  edge={edge:+.2f}  WR={stats['win_rate']}%")

print()
print("=== WHAT WOULD BREAKEVEN REQUIRE? ===")
avg_loss = sum(t["pnl"] for t in trades if t["pnl"] <= 0) / max(1, len([t for t in trades if t["pnl"] <= 0]))
avg_win = sum(t["pnl"] for t in trades if t["pnl"] > 0) / max(1, len(winners))
needed_wr = abs(avg_loss) / (abs(avg_loss) + avg_win) * 100
print(f"  Avg win:  ${avg_win:+.2f}")
print(f"  Avg loss: ${avg_loss:+.2f}")
print(f"  Win rate needed for breakeven: {needed_wr:.0f}%")
print(f"  Current win rate: {len(winners)/len(trades)*100:.0f}%")
print(f"  Gap: need {needed_wr:.0f}% but only have {len(winners)/len(trades)*100:.0f}%")

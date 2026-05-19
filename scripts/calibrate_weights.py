#!/usr/bin/env python
"""
Quarterly ConvictionWeightCalibrator — run this every 90 days.

Usage:
    python scripts/calibrate_weights.py
    python scripts/calibrate_weights.py --days 60 --out /tmp/weights.json

Reads from agora.db (via AGORA_DB_PATH env var or default .agora/agora.db).
Writes proposed_weights.json and prints a human-readable summary.

REVIEW the output before touching any code. Never auto-apply.
"""

import argparse
import json
import os
import sys
from pathlib import Path

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).parent.parent))

from agora.ops.conviction_calibrator import calibrate, MIN_TRADES_FOR_PROPOSAL


def main() -> None:
    parser = argparse.ArgumentParser(description="Quarterly conviction weight calibration")
    parser.add_argument("--days",  type=int, default=90,  help="Lookback window in days (default: 90)")
    parser.add_argument("--out",   type=str, default=".agora/proposed_weights.json",
                        help="Output path for proposed_weights.json")
    args = parser.parse_args()

    db_path = os.environ.get("AGORA_DB_PATH", ".agora/agora.db")
    if not Path(db_path).exists():
        print(f"ERROR: DB not found at {db_path}. Set AGORA_DB_PATH env var.")
        sys.exit(1)

    output_path = args.out
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    print(f"Running calibration: db={db_path}, lookback={args.days}d, output={output_path}")
    result = calibrate(db_path, output_path, lookback_days=args.days)

    # Print summary
    n = result["total_closed_trades"]
    print(f"\nTotal closed trades in window: {n}")
    if n < MIN_TRADES_FOR_PROPOSAL:
        print(f"  (below {MIN_TRADES_FOR_PROPOSAL} minimum — no weight changes proposed)")

    print("\nPer-pillar performance:")
    for pillar, stats in result["per_pillar"].items():
        wr  = f"{stats['win_rate']:.0%}" if stats["win_rate"] is not None else "N/A"
        pf  = f"{stats['profit_factor']:.2f}" if stats["profit_factor"] is not None else "N/A"
        pnl = f"${stats['avg_pnl']:.0f}" if stats["avg_pnl"] is not None else "N/A"
        print(f"  {pillar:<18} n={stats['count']:<4} win={wr:<6} PF={pf:<6} avg_pnl={pnl}")

    print("\nConviction quintiles (Q1=lowest, Q5=highest):")
    for q, stats in result["conviction_quintiles"].items():
        wr  = f"{stats['win_rate']:.0%}" if stats["win_rate"] is not None else "N/A"
        pnl = f"${stats['avg_pnl']:.0f}" if stats["avg_pnl"] is not None else "N/A"
        print(f"  {q:<22} n={stats['count']:<4} win={wr:<6} avg_pnl={pnl}")

    print("\nProposal notes:")
    for note in result["proposal_notes"]:
        print(f"  • {note}")

    print(f"\nFull output written to: {output_path}")
    print("\n*** HUMAN REVIEW REQUIRED before applying any changes ***")


if __name__ == "__main__":
    main()

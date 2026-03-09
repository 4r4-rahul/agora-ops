#!/usr/bin/env python3
"""
Options Trading Engine — CLI Entry Point
==========================================
Run from project root:
    python -m trading_engine
    python -m trading_engine --module regime
    python -m trading_engine --morning
    python -m trading_engine --all
"""

import argparse
import sys
from datetime import date, datetime

from .engine import TradingEngine
from .config import EngineConfig


def parse_args():
    parser = argparse.ArgumentParser(
        description="Institutional Options Trading Engine — 12 Modules",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
MODULES:
  scanner    — 0DTE SPX Credit Spread Scanner      (Tastytrade)
  regime     — Market Regime Classifier             (Citadel)
  theta      — Theta Decay Calculator               (SIG)
  strikes    — Probability Strike Selection         (Two Sigma)
  condor     — Iron Condor Machine                  (D.E. Shaw)
  premarket  — Pre-Market Analyzer                  (Jane Street)
  risk       — Risk Management System               (Wolverine)
  skew       — Volatility Skew Exploiter            (Akuna Capital)
  calendar   — Weekly Income Calendar               (Peak6)
  earnings   — Earnings Theta Crusher               (IMC Trading)
  eod        — EOD Theta Scalper                    (Optiver)
  dashboard  — Monthly Performance Dashboard        (Citadel)

WORKFLOWS:
  --morning  — Full morning analysis (6 modules)
  --midday   — Midday position check
  --eod-flow — EOD scalping workflow
  --weekly   — Weekly review
  --all      — Run all 12 modules

EXAMPLES:
  python -m trading_engine                    # Interactive status
  python -m trading_engine --module regime    # Run just regime classifier
  python -m trading_engine --morning          # Full morning workflow
  python -m trading_engine --module scanner   # 0DTE credit spread scan
  python -m trading_engine --all              # Everything
        """
    )

    parser.add_argument("--module", "-m", type=str,
                        help="Run a specific module (see MODULES above)")
    parser.add_argument("--morning", action="store_true",
                        help="Run morning analysis workflow")
    parser.add_argument("--midday", action="store_true",
                        help="Run midday check")
    parser.add_argument("--eod-flow", action="store_true",
                        help="Run EOD scalping workflow")
    parser.add_argument("--weekly", action="store_true",
                        help="Run weekly review")
    parser.add_argument("--all", action="store_true",
                        help="Run all 12 modules")
    parser.add_argument("--ibkr", action="store_true",
                        help="Use IBKR live data (default: manual input)")
    parser.add_argument("--account-size", type=float, default=50000,
                        help="Account size in dollars (default: 50000)")
    parser.add_argument("--earnings-ticker", type=str, default="TSLA",
                        help="Ticker for earnings analysis")
    parser.add_argument("--earnings-iv", type=float, default=65.0,
                        help="Current IV for earnings ticker")
    parser.add_argument("--earnings-price", type=float, default=250.0,
                        help="Current price for earnings ticker")

    return parser.parse_args()


def main():
    args = parse_args()

    # Build config
    config = EngineConfig()
    config.account.account_size = args.account_size

    # Create engine
    engine = TradingEngine(config, use_ibkr=args.ibkr)

    # Module dispatch
    module_map = {
        "scanner": engine.run_scanner,
        "regime": engine.run_regime,
        "theta": engine.run_theta,
        "strikes": engine.run_strikes,
        "condor": engine.run_condor,
        "premarket": engine.run_premarket,
        "risk": engine.run_risk,
        "skew": engine.run_skew,
        "calendar": engine.run_calendar,
        "earnings": lambda: engine.run_earnings(
            ticker=args.earnings_ticker,
            current_iv=args.earnings_iv,
            stock_price=args.earnings_price,
        ),
        "eod": engine.run_eod,
        "dashboard": engine.run_dashboard,
    }

    try:
        if args.morning:
            print(engine.run_morning_analysis())
        elif args.midday:
            print(engine.run_midday_check())
        elif args.eod_flow:
            print(engine.run_eod_workflow())
        elif args.weekly:
            print(engine.run_weekly_review())
        elif args.all:
            print(engine.run_all())
        elif args.module:
            mod_name = args.module.lower().strip()
            if mod_name in module_map:
                print(module_map[mod_name]())
            else:
                print(f"Unknown module: {mod_name}")
                print(f"Available: {', '.join(module_map.keys())}")
                sys.exit(1)
        else:
            # Default: show status + interactive menu
            print(engine.status())
            _interactive_menu(engine, module_map)

    except KeyboardInterrupt:
        print("\n\nExiting engine.")
        sys.exit(0)


def _interactive_menu(engine: TradingEngine, module_map: dict):
    """Interactive module selection menu."""
    print("\n  Enter a module name or workflow command (Ctrl+C to exit):\n")

    menu = """
  MODULES:                           WORKFLOWS:
  ─────────                          ──────────
  1) scanner    — 0DTE Spreads       morning  — Full AM analysis
  2) regime     — Market Regime      midday   — Midday check
  3) theta      — Theta Calculator   eod-flow — EOD workflow
  4) strikes    — Strike Selection   weekly   — Weekly review
  5) condor     — Iron Condor        all      — All 12 modules
  6) premarket  — Pre-Market
  7) risk       — Risk Manager       OTHER:
  8) skew       — Skew Exploiter     ──────
  9) calendar   — Weekly Calendar    data     — Enter/refresh market data
  10) earnings  — Earnings Crush     status   — Engine status
  11) eod       — EOD Scalper        quit     — Exit
  12) dashboard — Performance
"""
    print(menu)

    number_map = {
        "1": "scanner", "2": "regime", "3": "theta", "4": "strikes",
        "5": "condor", "6": "premarket", "7": "risk", "8": "skew",
        "9": "calendar", "10": "earnings", "11": "eod", "12": "dashboard",
    }

    while True:
        try:
            choice = input("\n  ▸ ").strip().lower()

            if choice in ("q", "quit", "exit"):
                print("\n  Goodbye. Good trading.\n")
                break
            elif choice == "data":
                engine.get_snapshot(use_manual=True)
                print("  ✅ Market data updated.")
            elif choice == "status":
                print(engine.status())
            elif choice == "morning":
                print(engine.run_morning_analysis())
            elif choice == "midday":
                print(engine.run_midday_check())
            elif choice in ("eod-flow", "eod_flow"):
                print(engine.run_eod_workflow())
            elif choice == "weekly":
                print(engine.run_weekly_review())
            elif choice == "all":
                print(engine.run_all())
            elif choice in number_map:
                mod = number_map[choice]
                print(module_map[mod]())
            elif choice in module_map:
                print(module_map[choice]())
            else:
                print(f"  Unknown: '{choice}'. Type a module name, number, or workflow.")

        except KeyboardInterrupt:
            print("\n\n  Goodbye. Good trading.\n")
            break
        except Exception as e:
            print(f"  Error: {e}")


if __name__ == "__main__":
    main()

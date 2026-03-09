#!/usr/bin/env python3
"""
Options Trading Engine — Quick Launcher
========================================

Usage:
    python trading_engine.py                  # Interactive menu
    python trading_engine.py regime           # Single module
    python trading_engine.py morning          # Morning workflow
    python trading_engine.py all              # All 12 modules

Or use as a Python package:
    python -m trading_engine --module regime
    python -m trading_engine --morning
    python -m trading_engine --all

Or import in code:
    from trading_engine.engine import TradingEngine
    engine = TradingEngine()
    print(engine.run_regime())
"""

import sys
import os

# Ensure this file can import the trading_engine package
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from trading_engine.engine import TradingEngine
from trading_engine.config import EngineConfig


def main():
    config = EngineConfig()
    engine = TradingEngine(config, use_ibkr=False)

    if len(sys.argv) > 1:
        cmd = sys.argv[1].lower().strip()
    else:
        cmd = "menu"

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
        "earnings": lambda: engine.run_earnings(),
        "eod": engine.run_eod,
        "dashboard": engine.run_dashboard,
    }

    try:
        if cmd == "morning":
            print(engine.run_morning_analysis())
        elif cmd == "midday":
            print(engine.run_midday_check())
        elif cmd in ("eod-flow", "eod_flow"):
            print(engine.run_eod_workflow())
        elif cmd == "weekly":
            print(engine.run_weekly_review())
        elif cmd == "all":
            print(engine.run_all())
        elif cmd == "status":
            print(engine.status())
        elif cmd in module_map:
            print(module_map[cmd]())
        else:
            # Interactive
            print(engine.status())
            from trading_engine.__main__ import _interactive_menu
            _interactive_menu(engine, module_map)

    except KeyboardInterrupt:
        print("\n\nGoodbye. Good trading.")


if __name__ == "__main__":
    main()

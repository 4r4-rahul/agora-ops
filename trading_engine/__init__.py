"""
Institutional-Grade Options Trading Engine
==========================================

DEPRECATED — superseded by trading_platform/
---------------------------------------------
This module is the original scalp trading engine. It is preserved for
historical reference and research scripts. New development should target
trading_platform/ — the multi-agent options platform with full Claude
integration, walk-forward backtester, and IBKR live execution.

Migration path:
  - Analysis pipeline:  trading_platform.agents.orchestrator
  - Backtesting:        trading_platform.backtester.engine
  - Live execution:     trading_platform.services.ibkr_client
  - Paper monitoring:   trading_platform.agents.monitor
"""

import warnings as _warnings
_warnings.warn(
    "trading_engine is deprecated and will be removed after paper trading "
    "validation. Use trading_platform instead.",
    DeprecationWarning,
    stacklevel=2,
)

__version__ = "1.0.0"

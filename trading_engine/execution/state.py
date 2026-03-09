"""
State Persistence + Position Synchronizer
============================================
Keeps risk manager state and position data alive across restarts.
Syncs with actual IBKR account positions on every cycle.

State stored in data/live_state.json:
  • Daily/weekly/monthly P&L
  • Open positions
  • Consecutive loss count
  • Recovery mode status
  • Order history for the day

Position sync:
  • Reads real positions from IBKR (ib.positions())
  • Compares against our tracked state
  • Detects discrepancies (manual trades, fills missed during disconnect)
  • Updates risk manager with ground truth

Usage:
    from trading_engine.execution.state import StateManager

    state = StateManager()
    state.load()                         # Load from disk
    state.sync_positions(ibkr_provider)  # Sync with IBKR
    state.record_trade(fill_result)      # After a fill
    state.save()                         # Persist to disk
"""

import os
import json
import logging
from datetime import datetime, date, timedelta
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class DailyState:
    """Persistent daily trading state."""
    date: str = ""                           # YYYY-MM-DD
    daily_pnl: float = 0.0
    weekly_pnl: float = 0.0
    monthly_pnl: float = 0.0
    trades_today: int = 0
    consecutive_losses: int = 0
    buying_power_used: float = 0.0
    peak_equity: float = 50_000.0
    # Recovery mode
    in_recovery: bool = False
    recovery_size_reduction: float = 0.0     # 0.0 = none, 0.5 = half size
    recovery_days_remaining: int = 0
    # Positions
    open_positions: List[Dict[str, Any]] = field(default_factory=list)
    closed_trades: List[Dict[str, Any]] = field(default_factory=list)


class StateManager:
    """
    Manages persistent state for the live trading loop.

    Automatically handles:
      • Day rollover (reset daily P&L, keep weekly/monthly)
      • Week rollover (reset weekly P&L)
      • Month rollover (reset monthly P&L)
      • Recovery mode countdown
    """

    DEFAULT_PATH = os.path.join("data", "live_state.json")

    def __init__(self, path: Optional[str] = None, account_size: float = 50_000.0):
        self.path = path or self.DEFAULT_PATH
        self.account_size = account_size
        self.state = DailyState(date=str(date.today()))

    # ─── Load / Save ─────────────────────────────────────────────

    def load(self) -> DailyState:
        """Load state from disk. Handles day/week/month rollover."""
        if not os.path.exists(self.path):
            logger.info(f"No state file found at {self.path}, starting fresh")
            self.state = DailyState(
                date=str(date.today()),
                peak_equity=self.account_size,
            )
            return self.state

        try:
            with open(self.path) as f:
                data = json.load(f)
            self.state = DailyState(**{
                k: v for k, v in data.items()
                if k in DailyState.__dataclass_fields__
            })
            logger.info(f"Loaded state from {self.path} (date={self.state.date})")
        except Exception as e:
            logger.error(f"Failed to load state: {e}, starting fresh")
            self.state = DailyState(
                date=str(date.today()),
                peak_equity=self.account_size,
            )
            return self.state

        # ── Day rollover ──
        today = str(date.today())
        if self.state.date != today:
            prev_date = self.state.date
            logger.info(f"Day rollover: {prev_date} → {today}")

            # Keep weekly/monthly, reset daily
            self.state.daily_pnl = 0.0
            self.state.trades_today = 0
            self.state.closed_trades = []
            self.state.date = today

            # Recovery countdown
            if self.state.recovery_days_remaining > 0:
                self.state.recovery_days_remaining -= 1
                if self.state.recovery_days_remaining <= 0:
                    self.state.in_recovery = False
                    self.state.recovery_size_reduction = 0.0
                    logger.info("Recovery mode ended")

            # ── Week rollover (Monday) ──
            if date.today().weekday() == 0:  # Monday
                prev = date.fromisoformat(prev_date) if prev_date else date.today()
                if prev.weekday() >= 4:  # Previous state was from Fri or later
                    logger.info(f"Week rollover: resetting weekly P&L ({self.state.weekly_pnl:+.2f})")
                    self.state.weekly_pnl = 0.0

            # ── Month rollover ──
            prev_month = prev_date[:7] if prev_date else ""
            this_month = today[:7]
            if prev_month != this_month:
                logger.info(f"Month rollover: resetting monthly P&L ({self.state.monthly_pnl:+.2f})")
                self.state.monthly_pnl = 0.0
                self.state.peak_equity = self.account_size + self.state.monthly_pnl

        return self.state

    def save(self):
        """Persist state to disk."""
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        data = asdict(self.state)
        with open(self.path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        logger.debug(f"State saved to {self.path}")

    # ─── Trade Recording ─────────────────────────────────────────

    def record_fill(self, fill_result, realized_pnl: float):
        """
        Record a completed trade (entry or exit).

        Args:
            fill_result:   FillResult from OrderExecutor
            realized_pnl:  P&L from closing trade (0 for new entries)
        """
        self.state.daily_pnl += realized_pnl
        self.state.weekly_pnl += realized_pnl
        self.state.monthly_pnl += realized_pnl
        self.state.trades_today += 1

        if realized_pnl < 0:
            self.state.consecutive_losses += 1
        elif realized_pnl > 0:
            self.state.consecutive_losses = 0

        # Track peak equity for drawdown
        current_equity = self.account_size + self.state.monthly_pnl
        self.state.peak_equity = max(self.state.peak_equity, current_equity)

        # Check if we should enter recovery mode
        daily_limit = self.account_size * 0.05  # 5%
        if abs(self.state.daily_pnl) > daily_limit * 0.8:
            self.state.in_recovery = True
            self.state.recovery_size_reduction = 0.5
            self.state.recovery_days_remaining = 3
            logger.warning(f"Entering recovery mode: daily P&L ${self.state.daily_pnl:+.2f}")

        # Log the trade
        trade_record = {
            "time": datetime.now().isoformat(),
            "pnl": realized_pnl,
            "daily_pnl_after": self.state.daily_pnl,
            "ticker": getattr(fill_result, 'ticker', ''),
            "strategy": getattr(fill_result, 'strategy', ''),
        }
        self.state.closed_trades.append(trade_record)

        self.save()

    # ─── Position Tracking ───────────────────────────────────────

    def add_position(self, position_data: Dict[str, Any]):
        """Track a new open position."""
        self.state.open_positions.append(position_data)
        self.save()

    def remove_position(self, ticker: str, short_strike: float, right: str):
        """Remove a closed position from tracking."""
        self.state.open_positions = [
            p for p in self.state.open_positions
            if not (p.get("ticker") == ticker and
                    p.get("short_strike") == short_strike and
                    p.get("right") == right)
        ]
        self.save()

    # ─── IBKR Position Sync ─────────────────────────────────────

    def sync_positions(self, ibkr_provider) -> Dict[str, Any]:
        """
        Sync our tracked positions with actual IBKR account.

        Returns dict with sync results:
          matched:      Positions we track that IBKR confirms
          untracked:    IBKR positions we don't have in our state
          stale:        Positions we track that IBKR doesn't have
        """
        if not ibkr_provider or not ibkr_provider.is_connected():
            return {"error": "IBKR not connected"}

        # Get real positions from IBKR
        ibkr_positions = ibkr_provider._ib.positions()
        ibkr_options = [
            p for p in ibkr_positions
            if p.contract.secType == "OPT" and p.position != 0
        ]

        # Build lookup from IBKR
        ibkr_lookup = {}
        for p in ibkr_options:
            c = p.contract
            key = f"{c.symbol}_{c.strike}_{c.right}_{c.lastTradeDateOrContractMonth}"
            ibkr_lookup[key] = {
                "symbol": c.symbol,
                "strike": c.strike,
                "right": c.right,
                "expiry": c.lastTradeDateOrContractMonth,
                "position": int(p.position),
                "avg_cost": p.avgCost,
            }

        # Compare against our tracked positions
        matched = []
        stale = []
        for pos in self.state.open_positions:
            key = (f"{pos.get('ticker', '')}_{pos.get('short_strike', 0)}_"
                   f"{pos.get('right', '')}_{pos.get('expiry', '')}")
            if key in ibkr_lookup:
                matched.append(pos)
                del ibkr_lookup[key]
            else:
                stale.append(pos)

        untracked = list(ibkr_lookup.values())

        # Remove stale positions from our state
        if stale:
            logger.warning(f"Removing {len(stale)} stale positions from state")
            for s in stale:
                logger.warning(f"  Stale: {s}")
            self.state.open_positions = [
                p for p in self.state.open_positions if p not in stale
            ]

        # Add untracked IBKR positions to our state
        if untracked:
            logger.warning(f"Found {len(untracked)} untracked IBKR positions")
            for u in untracked:
                logger.warning(f"  Untracked: {u}")
                self.state.open_positions.append({
                    "ticker": u["symbol"],
                    "short_strike": u["strike"],
                    "right": u["right"],
                    "expiry": u["expiry"],
                    "quantity": u["position"],
                    "avg_cost": u["avg_cost"],
                    "source": "ibkr_sync",
                })

        if stale or untracked:
            self.save()

        result = {
            "matched": len(matched),
            "untracked": len(untracked),
            "stale": len(stale),
            "total_ibkr": len(ibkr_options),
            "total_tracked": len(self.state.open_positions),
        }

        if result["untracked"] or result["stale"]:
            print(f"  ⚠️  Position sync: {result['matched']} matched, "
                  f"{result['untracked']} untracked, {result['stale']} stale")
        else:
            print(f"  ✅ Position sync: {result['matched']} positions confirmed")

        return result

    # ─── Risk Check Helpers ──────────────────────────────────────

    def can_trade(self) -> Tuple:
        """
        Quick pre-trade check using persisted state.

        Returns (allowed: bool, reason: str)
        """
        # Daily loss limit (5% of account)
        daily_limit = self.account_size * 0.05
        if self.state.daily_pnl < -daily_limit:
            return False, f"Daily loss limit hit: ${self.state.daily_pnl:+.2f} (limit: -${daily_limit:.0f})"

        # Weekly loss limit (8%)
        weekly_limit = self.account_size * 0.08
        if self.state.weekly_pnl < -weekly_limit:
            return False, f"Weekly loss limit hit: ${self.state.weekly_pnl:+.2f}"

        # Monthly circuit breaker (10%)
        monthly_limit = self.account_size * 0.10
        if self.state.monthly_pnl < -monthly_limit:
            return False, f"Monthly circuit breaker: ${self.state.monthly_pnl:+.2f}"

        # Consecutive losses
        if self.state.consecutive_losses >= 5:
            return False, f"5+ consecutive losses ({self.state.consecutive_losses})"

        return True, "OK"

    def get_size_multiplier(self) -> float:
        """
        Get position size multiplier based on current state.
        Accounts for recovery mode.
        """
        mult = 1.0
        if self.state.in_recovery:
            mult *= (1.0 - self.state.recovery_size_reduction)
        return mult

    def print_status(self):
        """Print current state summary."""
        s = self.state
        print(f"\n  {'═' * 60}")
        print(f"  TRADING STATE — {s.date}")
        print(f"  {'═' * 60}")
        print(f"  Daily P&L:        ${s.daily_pnl:+,.2f}")
        print(f"  Weekly P&L:       ${s.weekly_pnl:+,.2f}")
        print(f"  Monthly P&L:      ${s.monthly_pnl:+,.2f}")
        print(f"  Trades Today:     {s.trades_today}")
        print(f"  Consecutive L:    {s.consecutive_losses}")
        print(f"  Open Positions:   {len(s.open_positions)}")
        if s.in_recovery:
            print(f"  ⚠️  RECOVERY MODE: {s.recovery_size_reduction*100:.0f}% reduction, "
                  f"{s.recovery_days_remaining} days left")
        can, reason = self.can_trade()
        status = f"✅ {reason}" if can else f"❌ {reason}"
        print(f"  Can Trade:        {status}")
        print(f"  {'═' * 60}")
